"""Public client API: SendClient, ListenClient, health().

Everything here COMPOSES the existing core: sender semantics (SensingSender + durable SendLedger, UNKNOWN_SEND =>
NO_REPLAY), the CoreNatsAdapter seam (now bound to a real nats-py connection), and the existing SensingReceiver /
DedupeCursor / FlightRecorder / owner-resolver port. Nothing in the send/receive/dedupe/TTL/stale logic is
re-implemented here. TRANSPORT_ACCEPTED != DELIVERED != ACK_READ != WORK_CONSUMED != EFFECT.
"""
from __future__ import annotations

import importlib
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from . import _bootstrap
from signalvev_sensing import (CoreNatsAdapter, DedupeCursor, FlightRecorder, InterestTable, SendLedger, SensingReceiver,
                               SensingSender, accept_owner_event, build_frame)
from signalvev_sensing.model import SUBJECT_MESSAGE_TYPE

from . import __version__
from .closure_log import ClosureLogSink
from .config import RESOLVER_FACTORY, ClientConfig
from .dispatch import Dispatcher
from .errors import BindingError, ConfigError, ConnectError
from .lock import EvidenceLock
from .nats_binding import LISTEN, PROBE, SEND, NatsPyConnection, nats_py_version
from .synthetic_owner import SyntheticOwnerResolver

CLAIMS = {
    "transport_accepted": "TRANSPORT_EVIDENCE_ONLY",
    "delivered": "NOT_PROVEN",
    "peer_receiver_liveness": "NOT_PROVEN",
    "work_consumed": "NOT_CLAIMED",
    "effect": "NONE_CLAIMED",
    "authority": "NONE",
}
NOT_PROOF_OF = ("delivery", "peer receiver liveness", "owner truth", "work consumed", "effect")


def precheck_event(raw_event: Mapping[str, Any], *, ttl_seconds: int, now: float | None = None) -> None:
    """Offline validation with the existing owner-event gate and frame builder. Raises SensingError; no network."""
    build_frame(accept_owner_event(raw_event), now_epoch=time.time() if now is None else now, ttl_seconds=ttl_seconds)


class SendClient:
    """One real private Core NATS connection + the existing durable-ledger sender. Single process per evidence dir."""

    def __init__(self, cfg: ClientConfig, *, connect_fn: Callable[..., Any] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._cfg, self._connect_fn, self._clock = cfg, connect_fn, clock
        self._lock: EvidenceLock | None = None
        self._ledger: SendLedger | None = None
        self._conn: NatsPyConnection | None = None
        self._sender: SensingSender | None = None

    def __enter__(self) -> "SendClient":
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def connect(self) -> None:
        if self._sender is not None:
            raise BindingError("ALREADY_CONNECTED")
        self._lock = EvidenceLock(self._cfg.evidence_dir, "sender").acquire()
        try:
            self._ledger = SendLedger(self._cfg.evidence_dir / "sender.jsonl")
            conn = NatsPyConnection(self._cfg, role=SEND, connect_fn=self._connect_fn)
            conn.connect()
            self._conn = conn
            self._sender = SensingSender(transport=CoreNatsAdapter(conn, flush_timeout=self._cfg.flush_timeout),
                                         ledger=self._ledger, clock=self._clock, ttl_seconds=self._cfg.ttl_seconds)
        except BaseException:
            self.close()
            raise

    def send(self, raw_event: Mapping[str, Any]):
        """-> SendOutcome. ACCEPTED means the server flushed the publish: transport evidence only."""
        if self._sender is None:
            raise BindingError("NOT_CONNECTED")
        return self._sender.send(raw_event)

    def status(self) -> dict[str, Any]:
        base = self._conn.status() if self._conn is not None else {"role": SEND, "state": "NEW"}
        return {**base, "claims": dict(CLAIMS)}

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
        if self._ledger is not None:
            self._ledger.close()
        if self._lock is not None:
            self._lock.release()
        self._conn = self._ledger = self._lock = self._sender = None


class _FanOutSink:
    def __init__(self, *sinks: Any) -> None:
        self._sinks = [s for s in sinks if s is not None]

    def deliver(self, closure: Any) -> None:
        first: Exception | None = None
        for sink in self._sinks:
            try:
                sink.deliver(closure)
            except Exception as exc:  # noqa: BLE001 - every sink gets the closure; the receiver reports the failure
                first = first or exc
        if first is not None:
            raise first


class ListenClient:
    """Real private Core NATS subscription -> the EXISTING receiver path (frames are handled off the event loop)."""

    def __init__(self, cfg: ClientConfig, *, resolver: Any, sink: Any = None,
                 connect_fn: Callable[..., Any] | None = None, clock: Callable[[], float] = time.time,
                 on_result: Callable[[Any], None] | None = None) -> None:
        self._cfg, self._resolver, self._extra_sink = cfg, resolver, sink
        self._connect_fn, self._clock, self._user_on_result = connect_fn, clock, on_result
        self._lock: EvidenceLock | None = None
        self._stores: list[Any] = []
        self._conn: NatsPyConnection | None = None
        self._dispatcher: Dispatcher | None = None
        self._receiver: SensingReceiver | None = None
        self._by_disposition: dict[str, int] = {}
        self._count_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.worker_stopped_cleanly: bool | None = None   # None until stop(); False = a handler was still running

    def __enter__(self) -> "ListenClient":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def receiver(self) -> SensingReceiver | None:
        return self._receiver

    def _on_result(self, result: Any) -> None:
        with self._count_lock:
            self._by_disposition[result.disposition] = self._by_disposition.get(result.disposition, 0) + 1
        if self._user_on_result is not None:
            self._user_on_result(result)

    def start(self) -> None:
        cfg = self._cfg
        if not cfg.interests:
            raise ConfigError("LISTEN_NEEDS_AT_LEAST_ONE_INTEREST", "add [[listen.interest]] to the configuration")
        if self._receiver is not None:
            raise BindingError("ALREADY_STARTED")
        self._lock = EvidenceLock(cfg.evidence_dir, "receiver").acquire()
        try:
            cursor = DedupeCursor(cfg.evidence_dir / "cursor.jsonl")
            recorder = FlightRecorder(cfg.evidence_dir / "recorder.jsonl")
            closures = ClosureLogSink(cfg.evidence_dir / "closures.jsonl")
            self._stores = [cursor, recorder, closures]
            self._receiver = SensingReceiver(interests=InterestTable(cfg.interests), resolver=self._resolver,
                                             cursor=cursor, recorder=recorder, clock=self._clock,
                                             sink=_FanOutSink(closures, self._extra_sink))
            self._dispatcher = Dispatcher(max_queue=cfg.max_queue, on_result=self._on_result)
            self._dispatcher.start()
            self._conn = NatsPyConnection(cfg, role=LISTEN, dispatcher=self._dispatcher, connect_fn=self._connect_fn)
            self._conn.connect()
            self._receiver.attach(CoreNatsAdapter(self._conn, flush_timeout=cfg.flush_timeout))
            try:
                self._conn.flush(cfg.flush_timeout)   # "listening" only after the server has the subscriptions
            except Exception as exc:  # noqa: BLE001 - a server that never answers PING is a connect failure
                raise ConnectError("SUBSCRIPTION_ROUNDTRIP_FAILED", type(exc).__name__) from None
        except BaseException:
            self.stop()
            raise

    def _poll_once(self, deadline: float | None, max_frames: int | None) -> str | None:
        if self.stop_event.wait(0.1):
            return "STOP_REQUESTED"
        if self._conn is not None and self._conn.state() == "CLOSED":
            return "CONNECTION_LOST"
        if max_frames is not None and self._dispatcher is not None and self._dispatcher.counters["dispatched"] >= max_frames:
            return "MAX_FRAMES_REACHED"
        if deadline is not None and time.monotonic() >= deadline:
            return "DURATION_ELAPSED"
        return None

    def wait(self, *, duration: float | None = None, max_frames: int | None = None) -> str:
        """Foreground wait. Returns the reason it ended."""
        deadline = None if duration is None else time.monotonic() + duration
        reason = self._poll_once(deadline, max_frames)
        while reason is None:
            reason = self._poll_once(deadline, max_frames)
        return reason

    def summary(self) -> dict[str, Any]:
        with self._count_lock:
            by_disposition = dict(self._by_disposition)
        counters = dict(self._dispatcher.counters) if self._dispatcher else {}
        return {"frames": counters, "by_disposition": by_disposition,
                "worker_stopped_cleanly": self.worker_stopped_cleanly}

    def status(self) -> dict[str, Any]:
        base = self._conn.status() if self._conn is not None else {"role": LISTEN, "state": "NEW"}
        return {**base, **self.summary(), "subjects": sorted(SUBJECT_MESSAGE_TYPE),
                "resolver": getattr(self._resolver, "description", "CUSTOM_RESOLVER"), "claims": dict(CLAIMS)}

    def stop(self) -> None:
        """Idempotent. Stops new frames, lets already-received ones finish, then releases everything."""
        if self._conn is not None:
            self._conn.close()
        if self._dispatcher is not None:
            self.worker_stopped_cleanly = self._dispatcher.stop()
        for store in self._stores:
            store.close()
        if self._lock is not None:
            self._lock.release()
        self._stores, self._conn, self._lock = [], None, None


def _can_write(directory: Path) -> bool:
    """Real write probe (os.access is unreliable for directories on Windows). The temporary file is removed at once."""
    try:
        with tempfile.TemporaryFile(dir=directory):
            return True
    except OSError:
        return False


def load_resolver(cfg: ClientConfig) -> Any:
    """The CLI's resolver: the labelled synthetic fixture, or an operator-named factory behind the production port."""
    if cfg.resolver_kind == RESOLVER_FACTORY:
        module_name, _, attr = (cfg.resolver_factory or "").partition(":")
        try:
            resolver = getattr(importlib.import_module(module_name), attr)(cfg)
        except Exception as exc:  # noqa: BLE001
            raise ConfigError("RESOLVER_FACTORY_FAILED", type(exc).__name__) from None
        if not callable(getattr(resolver, "resolve", None)):
            raise ConfigError("RESOLVER_FACTORY_FAILED", "result has no resolve()")
        return resolver
    if cfg.resolver_fixture is None:
        raise ConfigError("SYNTHETIC_FIXTURE_REQUIRED", "set resolver.fixture_file (a labelled SYNTHETIC fixture)")
    return SyntheticOwnerResolver.from_file(cfg.resolver_fixture)


def health(cfg: ClientConfig, *, connect: bool = False, connect_fn: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Config + environment report. With connect=True also one bounded connect + server round-trip (PING/PONG).

    A healthy result is TRANSPORT evidence only: it is not proof of delivery, peer receiver liveness, work or effect.
    """
    probe = cfg.evidence_dir
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    writable = _can_write(probe)
    report: dict[str, Any] = {
        "config": "VALID", "summary": cfg.public_summary(), "client_version": __version__,
        "nats_py_version": nats_py_version(), "python": sys.version.split()[0], "platform": sys.platform,
        "reference_bootstrap": dict(_bootstrap.BOOTSTRAP_RESULT),
        "evidence_dir": {"exists": cfg.evidence_dir.exists(), "writable": writable},
        "transport": {"checked": False}, "claims": dict(CLAIMS), "not_proof_of": list(NOT_PROOF_OF)}
    ok = report["evidence_dir"]["writable"]
    if connect:
        conn = NatsPyConnection(cfg, role=PROBE, connect_fn=connect_fn)
        try:
            conn.connect()
            conn.flush(cfg.flush_timeout)
            st = conn.status()
            report["transport"] = {"checked": True, "result": "CONNECTED_AND_SERVER_ROUNDTRIP_OK", "server": st["server"],
                                   "server_version": st["server_version"]}
        except ConnectError as exc:
            report["transport"] = {"checked": True, "result": "CONNECT_FAILED", "detail": exc.detail}
            ok = False
        except Exception as exc:  # noqa: BLE001
            report["transport"] = {"checked": True, "result": "ROUNDTRIP_FAILED", "detail": type(exc).__name__}
            ok = False
        finally:
            conn.close()
    report["ok"] = bool(ok)
    return report
