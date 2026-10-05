"""SYNTHETIC_TEST_ONLY doubles for the BK07 PM-to-X9 binding. NOT PM, NOT a channel, NOT a NATS server.

Every class/function here carries ``SYNTHETIC_TEST_ONLY = True``; ``pm_x9.build_binding`` refuses them in PRODUCTION
mode and accepts nothing else in SYNTHETIC_TEST_ONLY mode. They exist so the SAME factory/composition path can be
exercised offline, in a source checkout and from the installed wheel, without credentials.

* SyntheticPmPort          -- an "authenticated" PM owner store with opaque revisions. The PROVIDER judges the relation
                              (current => SAME, a revision it has superseded => SUPERSEDED, anything else => UNKNOWN).
* SyntheticChannelStore    -- append-once-by-event-id storage shared by any number of composition instances, with
  SyntheticChannelPort        per-principal views (ACL: pointers only from the producer, dispositions only from X9).
* SyntheticLoopbackBroker  -- an in-process connect_fn with the nats-py client shape, used through the EXISTING
                              SendClient/ListenClient boundaries (fault switches: lose, write_then_drop, hold).
"""
from __future__ import annotations

import atexit
import tempfile
import threading
import types
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import parse_config
from .pm_x9 import MODE_SYNTHETIC, PM_READY_HINT, PmX9HostPorts, PmX9Settings, pm, x9

SYNTHETIC_TEST_ONLY = True
LABEL = "SYNTHETIC_TEST_ONLY"
OWNER = "CURRENT_PM_SYNTHETIC"
PACKET_SHA = "a" * 64
START = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def synthetic_settings(**over: Any) -> PmX9Settings:
    base = dict(mode=MODE_SYNTHETIC, owner_ref=OWNER, claim_ref="WORK_CLAIMS:synthetic-1",
                packet_ref="WORK_PACKETS:synthetic-1", queue_ref="READY_QUEUE:synthetic-1", packet_sha256=PACKET_SHA,
                producer_principal="pm:producer-synthetic", x9_principal="x9:consumer-synthetic",
                x9_session_ref="x9:session-synthetic", attempt_ref="attempt:synthetic-1", pointer_ttl_seconds=900,
                max_pending_ingress=16, ports_factory="signalvev_client.pm_x9_synthetic:make_ports")
    base.update(over)
    return PmX9Settings(**base)


# ------------------------------------------------------------------------------------------- clock
class SyntheticClock:
    SYNTHETIC_TEST_ONLY = True

    def __init__(self, start: datetime = START) -> None:
        self._now, self._lock = start, threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now = self._now + timedelta(seconds=seconds)


# ------------------------------------------------------------------------------------------- PM owner
class SyntheticPmPort:
    """The provider's own state is the truth here; the bridge never sees how revisions relate."""

    SYNTHETIC_TEST_ONLY = True

    def __init__(self, settings: PmX9Settings) -> None:
        self.s = settings
        self._lock = threading.Lock()
        self.hints: dict[str, dict[str, Any]] = {}          # receipt_ref -> fixed hint facts
        self.current: dict[str, dict[str, Any]] = {}        # referent_id -> current owner cut
        self.superseded: dict[str, set[str]] = {}            # referent_id -> revisions this owner has superseded
        self.read_calls = 0
        self.reread_calls = 0
        self.offline = False
        self.timeout = False
        self.force_relation: str | None = None
        self.unauthenticated = False
        self.unverified_readback = False
        self.split_snapshot = False
        self.wrong_owner = False
        self.wrong_claim = False
        self.drop_projection = False
        self.gate: threading.Event | None = None             # when set: reread blocks until the event is set

    def seed_hint(self, receipt_ref: str, *, n: int = 7, ready_state: str = "MATERIAL_READY",
                  material_sha256: str = "c" * 64) -> dict[str, Any]:
        referent = f"pm-ready:synthetic-{n}"
        facts = {"receipt_ref": receipt_ref, "event_id": f"pm-event:synthetic-{n}", "referent_id": referent,
                 "owner_seq": n, "revision": f"rev:{n}", "revision_before": f"rev:{n - 1}",
                 "snapshot_ref": f"pm-snapshot:synthetic-{n}", "snapshot_sha256": f"{n:x}".rjust(64, "b")[-64:]}
        with self._lock:
            self.hints[receipt_ref] = facts
            self.current[referent] = {"revision": facts["revision"], "owner_seq": n,
                                      "snapshot_sha256": facts["snapshot_sha256"], "packet_sha256": self.s.packet_sha256,
                                      "ready_state": ready_state, "material_sha256": material_sha256,
                                      "source_cut": f"pm-cut:synthetic-{n}"}
        return facts

    def supersede(self, referent_id: str, new_revision: str, *, owner_seq: int) -> None:
        """The OWNER moves on; only it knows the old revision is now superseded (opaque ids, no ordering)."""
        with self._lock:
            cur = self.current[referent_id]
            self.superseded.setdefault(referent_id, set()).add(cur["revision"])
            self.current[referent_id] = {**cur, "revision": new_revision, "owner_seq": owner_seq,
                                         "snapshot_sha256": "e" * 64, "source_cut": f"pm-cut:{new_revision}"}

    def update_current(self, referent_id: str, **changes: Any) -> None:
        with self._lock:
            self.current[referent_id] = {**self.current[referent_id], **changes}

    def _check(self) -> None:
        if self.offline:
            raise ConnectionError("SYNTHETIC_PM_OFFLINE")
        if self.timeout:
            raise TimeoutError("SYNTHETIC_PM_TIMEOUT")

    def read_committed_ready_hint(self, receipt_ref: str, *, expected_owner_ref: str):
        with self._lock:
            self.read_calls += 1
        self._check()
        facts = self.hints[receipt_ref]
        cur = self.current[facts["referent_id"]]
        rev = facts["revision"]
        return pm.PmCommittedReadyHint(
            authenticated=not self.unauthenticated, committed=True, readback_verified=not self.unverified_readback,
            consistent_snapshot=True, owner_ref="CURRENT_PM_OTHER" if self.wrong_owner else self.s.owner_ref,
            receipt_ref=receipt_ref, event_id=facts["event_id"], referent_id=facts["referent_id"],
            owner_seq=facts["owner_seq"], revision_after=rev, revision_before=facts["revision_before"],
            provider_revision=facts["owner_seq"], claim_ref=self.s.claim_ref, packet_ref=self.s.packet_ref,
            queue_ref=self.s.queue_ref, claim_revision=rev,
            packet_revision=f"{rev}-split" if self.split_snapshot else rev, queue_revision=rev,
            packet_sha256=cur["packet_sha256"], ready_state=cur["ready_state"], snapshot_ref=facts["snapshot_ref"],
            snapshot_sha256=facts["snapshot_sha256"], readback_ref=f"pm-readback:{facts['owner_seq']}",
            observed_at="2026-10-05T12:00:00Z", way_home=(facts["snapshot_ref"], receipt_ref))

    def reread_ready_hint(self, referent_id: str, *, expected_owner_ref: str, expected_revision: str):
        with self._lock:
            self.reread_calls += 1
        gate = self.gate
        if gate is not None and not gate.wait(10.0):
            raise TimeoutError("SYNTHETIC_PM_GATE_TIMEOUT")
        self._check()
        with self._lock:
            cur = dict(self.current[referent_id])
            superseded = set(self.superseded.get(referent_id, ()))
        if self.force_relation is not None:
            relation = self.force_relation
        elif expected_revision == cur["revision"]:
            relation = "SAME"
        elif expected_revision in superseded:
            relation = "SUPERSEDED"
        else:
            relation = "UNKNOWN"
        projection = {} if self.drop_projection else dict(
            claim_ref="WORK_CLAIMS:other" if self.wrong_claim else self.s.claim_ref, packet_ref=self.s.packet_ref,
            queue_ref=self.s.queue_ref, packet_sha256=cur["packet_sha256"], ready_state=cur["ready_state"],
            material_sha256=cur["material_sha256"], source_cut=cur["source_cut"],
            consistent_snapshot=not self.split_snapshot, active_hold=cur.get("active_hold"))
        return pm.PmCurrentRead(
            authenticated=not self.unauthenticated, readback_verified=not self.unverified_readback,
            owner_ref="CURRENT_PM_OTHER" if self.wrong_owner else self.s.owner_ref, referent_id=referent_id,
            current_revision=cur["revision"], relation=relation, snapshot_sha256=cur["snapshot_sha256"],
            owner_seq=cur["owner_seq"], **projection)


# ------------------------------------------------------------------------------------------- channel
class SyntheticChannelStore:
    """Append-once by event_id, shared across composition instances (stands in for the provider's cross-process
    uniqueness). Readback always comes from this store, never from an append ACK."""

    SYNTHETIC_TEST_ONLY = True

    def __init__(self, producer_principal: str, x9_principal: str) -> None:
        self.producer_principal, self.x9_principal = producer_principal, x9_principal
        self._lock = threading.Lock()
        self.pointers: dict[str, tuple[Any, str, str]] = {}
        self.dispositions: dict[str, tuple[Any, str, str]] = {}
        self.pointer_appends = 0
        self.disposition_appends = 0
        self.pointer_mode = "ok"            # ok | unknown_written | unknown_lost | raise_written | not_sent
        self.disposition_mode = "ok"        # ok | unknown_lost | raise_written
        self.read_unavailable = False
        self.corrupt_pointer_readback = False
        self._rev = 0

    def _token(self) -> str:
        self._rev += 1
        return f"synthetic-channel:rev{self._rev}"

    def append_pointer(self, principal: str, record: Any) -> str:
        with self._lock:
            self.pointer_appends += 1
            if principal != self.producer_principal:
                return "NOT_SENT"                        # provider ACL refuses before write
            if record.event_id in self.pointers:
                return "NOT_SENT"                        # append-once: provider refuses a second row
            mode = self.pointer_mode
            if mode in ("ok", "unknown_written", "raise_written"):
                self.pointers[record.event_id] = (record, principal, self._token())
        if mode == "raise_written":
            raise ConnectionError("SYNTHETIC_ACK_LOST_AFTER_WRITE")
        return {"ok": "ACCEPTED", "unknown_written": "UNKNOWN_SEND", "unknown_lost": "UNKNOWN_SEND",
                "not_sent": "NOT_SENT"}[mode]

    def append_disposition(self, principal: str, record: Any) -> str:
        with self._lock:
            self.disposition_appends += 1
            if principal != self.x9_principal or record.event_id in self.dispositions:
                return "NOT_SENT"
            mode = self.disposition_mode
            if mode in ("ok", "raise_written"):
                self.dispositions[record.event_id] = (record, principal, self._token())
        if mode == "raise_written":
            raise ConnectionError("SYNTHETIC_ACK_LOST_AFTER_WRITE")
        return "ACCEPTED" if mode == "ok" else "UNKNOWN_SEND"

    def read(self, table: str, event_id: str):
        if self.read_unavailable:
            raise ConnectionError("SYNTHETIC_CHANNEL_READ_UNAVAILABLE")
        with self._lock:
            row = getattr(self, table).get(event_id)
        if row is None:
            return None
        record, principal, token = row
        digest = record.content_sha256
        if table == "pointers" and self.corrupt_pointer_readback:
            digest = "f" * 64
        return x9.Readback(record, digest, principal, token)


class SyntheticChannelPort:
    SYNTHETIC_TEST_ONLY = True

    def __init__(self, store: SyntheticChannelStore, principal: str, *, authenticated: bool = True,
                 append_allowed: bool = True, read_allowed: bool = True) -> None:
        self.store, self.principal = store, principal
        self.authenticated, self.append_allowed, self.read_allowed = authenticated, append_allowed, read_allowed

    def identity(self):
        return x9.ChannelIdentity(x9.CHANNEL, self.principal, self.authenticated, self.append_allowed,
                                  self.read_allowed)

    def append_pointer_once(self, record: Any) -> str:
        return self.store.append_pointer(self.principal, record)

    def read_pointer_by_event_id(self, event_id: str):
        return self.store.read("pointers", event_id)

    def append_disposition_once(self, record: Any) -> str:
        return self.store.append_disposition(self.principal, record)

    def read_disposition_by_event_id(self, event_id: str):
        return self.store.read("dispositions", event_id)


# ------------------------------------------------------------------------------------------- transport
class _Msg:
    def __init__(self, subject: str, data: bytes) -> None:
        self.subject, self.data = subject, data


class _SyntheticNC:
    """Same shape the binding uses from nats-py (publish/flush/subscribe/drain/close + state flags)."""

    def __init__(self, broker: "SyntheticLoopbackBroker") -> None:
        self.broker = broker
        self.is_connected, self.is_closed, self.is_reconnecting = True, False, False
        self.connected_url = types.SimpleNamespace(netloc="synthetic-loopback")
        self.connected_server_version = "SYNTHETIC_TEST_ONLY"

    async def publish(self, subject: str, payload: bytes) -> None:
        mode = self.broker.next_mode()
        self.broker.published.append((subject, payload))
        if mode == "lose":
            return
        self.broker.route(subject, payload)
        if mode == "write_then_drop":
            raise OSError("SYNTHETIC_CONNECTION_RESET_AFTER_WRITE")

    async def flush(self, timeout: float = 10) -> None:
        return None

    async def subscribe(self, subject: str, cb: Any = None, **_: Any) -> None:
        self.broker.subscribe(subject, cb, self)

    async def drain(self) -> None:
        self.is_closed, self.is_connected = True, False

    async def close(self) -> None:
        self.is_closed, self.is_connected = True, False


class SyntheticLoopbackBroker:
    """In-process switchboard. ``connect`` is the injected connect_fn (labelled SYNTHETIC_TEST_ONLY)."""

    SYNTHETIC_TEST_ONLY = True

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.subs: dict[str, list[tuple[Any, _SyntheticNC]]] = {}
        self.callback_errors = 0
        self.published: list[tuple[str, bytes]] = []
        self.routed: list[tuple[str, bytes]] = []
        self.held: list[tuple[str, bytes]] = []
        self.script: list[str] = []
        self.hold = False

    def next_mode(self) -> str:
        with self._lock:
            return self.script.pop(0) if self.script else "ok"

    def subscribe(self, subject: str, cb: Any, nc: _SyntheticNC) -> None:
        with self._lock:
            self.subs.setdefault(subject, []).append((cb, nc))

    def route(self, subject: str, payload: bytes) -> None:
        with self._lock:
            if self.hold:
                self.held.append((subject, payload))
                return
            self.routed.append((subject, payload))
            targets = list(self.subs.get(subject, []))
        for cb, nc in targets:
            if nc.is_closed:
                continue
            # The binding's message callback only hands the frame to its bounded Dispatcher (no await), so the
            # coroutine completes on its first step; the receiver itself still runs on the Dispatcher worker.
            coro = cb(_Msg(subject, payload))
            try:
                coro.send(None)
            except StopIteration:
                continue
            coro.close()
            with self._lock:
                self.callback_errors += 1

    def inject(self, subject: str, payload: bytes) -> None:
        """A foreign publisher (e.g. a forged frame) on the same synthetic bus."""
        self.route(subject, payload)

    def release(self) -> None:
        with self._lock:
            held, self.held, self.hold = self.held, [], False
        for subject, payload in held:
            self.route(subject, payload)

    async def _connect(self, **kwargs: Any) -> _SyntheticNC:
        return _SyntheticNC(self)

    def connect_fn(self):
        def connect(**kwargs: Any):
            return self._connect(**kwargs)
        connect.SYNTHETIC_TEST_ONLY = True        # type: ignore[attr-defined]
        return connect


# ------------------------------------------------------------------------------------------- world
class SyntheticWorld:
    """Everything a test or the installed self-test needs, wired through the ONE factory path."""

    SYNTHETIC_TEST_ONLY = True

    def __init__(self, settings: PmX9Settings | None = None, *, evidence_dir: Path | None = None,
                 max_queue: int = 64, ttl_seconds: int = 30, store: SyntheticChannelStore | None = None,
                 pm_port: SyntheticPmPort | None = None, broker: SyntheticLoopbackBroker | None = None,
                 clock: SyntheticClock | None = None) -> None:
        self.settings = settings or synthetic_settings()
        self._tmp = None
        self._forks: list["SyntheticWorld"] = []
        if evidence_dir is None:
            self._tmp = tempfile.TemporaryDirectory(prefix="signalvev-pmx9-synthetic-")
            evidence_dir = Path(self._tmp.name) / "evidence"
        self.evidence_dir = evidence_dir
        self.clock = clock or SyntheticClock()
        self.pm = pm_port or SyntheticPmPort(self.settings)
        self.store = store or SyntheticChannelStore(self.settings.producer_principal, self.settings.x9_principal)
        self.broker = broker or SyntheticLoopbackBroker()
        self.producer_channel = SyntheticChannelPort(self.store, self.settings.producer_principal)
        self.x9_channel = SyntheticChannelPort(self.store, self.settings.x9_principal)
        self.client_config = parse_config({
            "node": {"id": "node-pmx9-synthetic"},
            "nats": {"server": "nats://127.0.0.1:4222", "connect_timeout_seconds": 2.0, "flush_timeout_seconds": 1.0},
            "evidence": {"dir": str(evidence_dir)}, "send": {"ttl_seconds": ttl_seconds},
            "listen": {"interest": [{"owner_ref": self.settings.owner_ref, "referent_type": PM_READY_HINT}],
                       "max_queue": max_queue},
        }, check_files=False)

    def ports(self) -> PmX9HostPorts:
        return PmX9HostPorts(pm_port=self.pm, producer_channel=self.producer_channel, x9_channel=self.x9_channel,
                             clock=self.clock, client_config=self.client_config, connect_fn=self.broker.connect_fn())

    def fork(self, **over: Any) -> "SyntheticWorld":
        """A NEW composition instance (fresh evidence dir/cursor/ledger) against the SAME provider storage."""
        settings = replace(self.settings, **over) if over else self.settings
        child = SyntheticWorld(settings, store=self.store, pm_port=self.pm, broker=SyntheticLoopbackBroker(),
                               clock=self.clock)
        self._forks.append(child)
        return child

    def close(self) -> None:
        for child in self._forks:
            child.close()
        self._forks = []
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None


def make_ports(settings: PmX9Settings) -> PmX9HostPorts:
    """``ports_factory`` for SYNTHETIC_TEST_ONLY configurations (used by the installed self-test)."""
    world = SyntheticWorld(settings)
    atexit.register(world.close)              # disposable test state is removed at interpreter exit
    return world.ports()
