"""BK07 PM-to-X9 read flow: one thin composition of EXISTING parts. Authority NONE; default OFF.

    PM receipt --PmOwnerCommitReader.read_verified--> trusted OwnerEvent (POINTER, referent PM_READY_HINT)
      --SendClient (durable ledger, UNKNOWN_SEND => no replay)--> D0 frame on Core NATS
      --ListenClient (bounded Dispatcher, off the event loop) --> SensingReceiver
      --PmRereadResolver (fresh PM reread, owner-judged SAME/SUPERSEDED/UNKNOWN)--> ACK_READ closure
      --X9DepositSink--> X9ChannelIngress.deposit (producer-scoped channel, exact pointer readback)
      --queued for the ORDINARY X9 pulse--> consume_one(): X9ChannelIngress.consume with a fresh hint-bound PM read
      --> STALE | NO_MATERIAL_DELTA | MATERIAL_ROUTE with exact disposition readback (or a typed HOLD/COLLISION).

Nothing here re-implements send/receive/dedupe/TTL/owner-truth/replay rules; it binds them. The host supplies every
port through ONE factory entry point (``build_binding``); production mode never falls back to synthetic data and a
missing port is reported, not invented. Hashes are consistency checks; authentication and same-revision snapshot
custody are the configured providers' responsibility. A transport ACK, a stored row or a pointer is never work.
"""
from __future__ import annotations

import collections
import importlib
import inspect
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from . import _adapters, _bootstrap  # noqa: F401  (bootstrap binds the pinned references first)
from signalvev_sensing import ResolveRequest, ResolverResult, ResolverUnavailable, SensingError
from signalvev_sensing.a7_owner_receipt import read_trusted_owner_event
from signalvev_sensing.model import ACK_READ, DEPTH_POINTER_GROUND, HEX64, ID_RE
from signalvev_sensing.owner_event import COMMITTED, OwnerEvent

from .config import ClientConfig
from .errors import BindingError
from .nats_binding import nats_py_version
from .session import ListenClient, SendClient

from signalvev_adapters import pm_owner_commit as pm  # noqa: E402  (bound by _adapters: bundle or source tree)
from signalvev_adapters import x9_channel_ingress as x9  # noqa: E402

PM_READY_HINT = "PM_READY_HINT"
MODE_OFF, MODE_PRODUCTION, MODE_SYNTHETIC = "OFF", "PRODUCTION", "SYNTHETIC_TEST_ONLY"
MODES = (MODE_OFF, MODE_PRODUCTION, MODE_SYNTHETIC)
SYNTHETIC_LABEL = "SYNTHETIC_TEST_ONLY"
DIAGNOSTIC_RETENTION = 256
CLAIMS = {"authority": "NONE", "work_consumed": "NOT_CLAIMED", "effect": "NONE_CLAIMED",
          "runtime_authentication": "PROVIDER_RESPONSIBILITY_NOT_PROVEN_HERE", "delivery": "NOT_PROVEN_BY_TRANSPORT_ACK"}

_SETTINGS_KEYS = {"mode", "owner_ref", "claim_ref", "packet_ref", "queue_ref", "packet_sha256", "producer_principal",
                  "x9_principal", "x9_session_ref", "attempt_ref", "pointer_ttl_seconds", "max_pending_ingress",
                  "ports_factory"}
_SECRET_FRAGMENTS = ("password", "passwd", "token", "secret", "seed", "nkey", "jwt", "apikey", "api_key", "private_key",
                     "credential")


class PmX9Unbound(Exception):
    """The binding is default-off or a required host port is missing/unsuitable. Raised BEFORE any I/O."""

    def __init__(self, code: str, diagnostics: list[dict[str, str]] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.diagnostics = list(diagnostics or [])


class PmX9ConfigError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


# ------------------------------------------------------------------------------------------- bounded settings
@dataclass(frozen=True)
class PmX9Settings:
    """Bounded NON-SECRET configuration. Expected PM coordinates are the trusted host's, never an incoming D0's."""

    mode: str
    owner_ref: str
    claim_ref: str
    packet_ref: str
    queue_ref: str
    packet_sha256: str
    producer_principal: str
    x9_principal: str
    x9_session_ref: str
    attempt_ref: str
    pointer_ttl_seconds: int = 900
    max_pending_ingress: int = 64
    ports_factory: str | None = None

    def expectation(self):
        return pm.ReadyHintExpectation(owner_ref=self.owner_ref, claim_ref=self.claim_ref, packet_ref=self.packet_ref,
                                       queue_ref=self.queue_ref, packet_sha256=self.packet_sha256)

    def public_summary(self) -> dict[str, Any]:
        return {"mode": self.mode, "owner_ref": self.owner_ref, "claim_ref": self.claim_ref,
                "packet_ref": self.packet_ref, "queue_ref": self.queue_ref, "packet_sha256": self.packet_sha256,
                "producer_principal": self.producer_principal, "x9_principal": self.x9_principal,
                "x9_session_ref": self.x9_session_ref, "attempt_ref": self.attempt_ref,
                "pointer_ttl_seconds": self.pointer_ttl_seconds, "max_pending_ingress": self.max_pending_ingress,
                "ports_factory": self.ports_factory or "absent"}


def parse_settings(doc: Mapping[str, Any]) -> PmX9Settings:
    """Validate the ``[pm_x9]`` table. Unknown keys and inline secrets are refused; mode defaults to OFF."""
    if not isinstance(doc, Mapping):
        raise PmX9ConfigError("PM_X9_CONFIG_NOT_A_TABLE")
    for key in doc:
        lowered = str(key).lower()
        if any(f in lowered for f in _SECRET_FRAGMENTS):
            raise PmX9ConfigError("INLINE_SECRET_NOT_ALLOWED", str(key))
        if key not in _SETTINGS_KEYS:
            raise PmX9ConfigError("UNKNOWN_KEY", str(key))
    mode = doc.get("mode", MODE_OFF)
    if mode not in MODES:
        raise PmX9ConfigError("MODE_INVALID", f"mode must be one of {', '.join(MODES)}")
    ids = {}
    for key in ("owner_ref", "claim_ref", "packet_ref", "queue_ref", "producer_principal", "x9_principal",
                "x9_session_ref", "attempt_ref"):
        value = doc.get(key)
        if mode != MODE_OFF and not (isinstance(value, str) and ID_RE.match(value)):
            raise PmX9ConfigError("ID_INVALID_OR_MISSING", key)
        ids[key] = value if isinstance(value, str) else ""
    packet_sha = doc.get("packet_sha256", "")
    if mode != MODE_OFF and not (isinstance(packet_sha, str) and HEX64.match(packet_sha)):
        raise PmX9ConfigError("PACKET_SHA256_INVALID_OR_MISSING", "packet_sha256")
    if mode != MODE_OFF and ids["producer_principal"] == ids["x9_principal"]:
        raise PmX9ConfigError("PRINCIPALS_NOT_DISTINCT", "producer and X9 principals must be separately scoped")
    ttl = doc.get("pointer_ttl_seconds", 900)
    if type(ttl) is not int or not 30 <= ttl <= 86_400:
        raise PmX9ConfigError("VALUE_OUT_OF_BOUNDS", "pointer_ttl_seconds in [30, 86400]")
    pending = doc.get("max_pending_ingress", 64)
    if type(pending) is not int or not 1 <= pending <= 4096:
        raise PmX9ConfigError("VALUE_OUT_OF_BOUNDS", "max_pending_ingress in [1, 4096]")
    factory = doc.get("ports_factory")
    if factory is not None and not (isinstance(factory, str) and factory.count(":") == 1 and all(factory.split(":"))):
        raise PmX9ConfigError("PORTS_FACTORY_INVALID", "ports_factory must be 'package.module:callable'")
    return PmX9Settings(mode=mode, packet_sha256=packet_sha if isinstance(packet_sha, str) else "",
                        pointer_ttl_seconds=ttl, max_pending_ingress=pending, ports_factory=factory, **ids)


# ------------------------------------------------------------------------------------------- host ports
@dataclass(frozen=True)
class PmX9HostPorts:
    """Everything the host binds. Credentials never appear here: ports carry their own runtime authentication."""

    pm_port: Any                                   # pm_owner_commit.PmOwnerReadPort (read + reread)
    producer_channel: Any                          # x9_channel_ingress.ChannelPort scoped to the producer principal
    x9_channel: Any                                # x9_channel_ingress.ChannelPort scoped to the X9 principal
    clock: Callable[[], datetime]                  # injected, timezone-aware
    client_config: ClientConfig                    # existing client transport config (paths only, no secrets)
    connect_fn: Callable[..., Any] | None = None   # None => the installed real nats-py binding
    pm_reread_port: Any = None                     # optional separately scoped receiver/X9 PM port (default pm_port)


_PORT_METHODS = {
    "pm_port": ("read_committed_ready_hint", "reread_ready_hint"),
    "pm_reread_port": ("reread_ready_hint",),
    "producer_channel": ("identity", "append_pointer_once", "read_pointer_by_event_id", "append_disposition_once",
                         "read_disposition_by_event_id"),
    "x9_channel": ("identity", "append_pointer_once", "read_pointer_by_event_id", "append_disposition_once",
                   "read_disposition_by_event_id"),
}


def is_synthetic(obj: Any) -> bool:
    return getattr(obj, SYNTHETIC_LABEL, False) is True


def diagnose(settings: PmX9Settings, ports: PmX9HostPorts | None) -> list[dict[str, str]]:
    """Per-port status WITHOUT calling any port: BOUND | MISSING | INVALID | SYNTHETIC_IN_PRODUCTION |
    NOT_SYNTHETIC_IN_TEST_MODE | DEFAULT_OFF. Honest about what is absent; never fills a gap."""
    if settings.mode == MODE_OFF:
        return [{"port": "*", "status": "DEFAULT_OFF", "detail": "mode = OFF; nothing is bound or contacted"}]
    names = ("pm_port", "pm_reread_port", "producer_channel", "x9_channel", "clock", "client_config", "transport")
    if ports is None:
        return [{"port": n, "status": "MISSING", "detail": "no host ports supplied"} for n in names]
    out: list[dict[str, str]] = []

    def label_ok(name: str, obj: Any) -> dict[str, str] | None:
        if settings.mode == MODE_PRODUCTION and is_synthetic(obj):
            return {"port": name, "status": "SYNTHETIC_IN_PRODUCTION", "detail": "a SYNTHETIC_TEST_ONLY double"}
        if settings.mode == MODE_SYNTHETIC and not is_synthetic(obj):
            return {"port": name, "status": "NOT_SYNTHETIC_IN_TEST_MODE", "detail": "test mode accepts labelled doubles only"}
        return None

    for name, methods in _PORT_METHODS.items():
        obj = getattr(ports, name)
        if obj is None and name == "pm_reread_port":
            out.append({"port": name, "status": "BOUND", "detail": "uses pm_port"})
            continue
        if obj is None:
            out.append({"port": name, "status": "MISSING", "detail": "required"})
            continue
        missing = [m for m in methods if not callable(getattr(obj, m, None))]
        if missing:
            out.append({"port": name, "status": "INVALID", "detail": "lacks " + ",".join(missing)})
            continue
        if name in {"pm_port", "pm_reread_port"}:
            # PR51's server requires credentials plus sequence/hash inputs.
            # It needs a trusted host adapter, not a raw method-name match.
            calls = {"read_committed_ready_hint": (("pm-receipt:diagnose",),
                     {"expected_owner_ref": settings.owner_ref}),
                     "reread_ready_hint": (("pm-ready:diagnose",),
                     {"expected_owner_ref": settings.owner_ref, "expected_revision": "rev:diagnose"})}
            try:
                for method in methods:
                    args, kwargs = calls[method]
                    inspect.signature(getattr(obj, method)).bind(*args, **kwargs)
            except (TypeError, ValueError):
                out.append({"port": name, "status": "INVALID",
                            "detail": "PM_HOST_ADAPTER_REQUIRED: client read/reread signature"})
                continue
        out.append(label_ok(name, obj) or {"port": name, "status": "BOUND", "detail": type(obj).__name__})
    if ports.producer_channel is not None and ports.producer_channel is ports.x9_channel:
        out.append({"port": "channels", "status": "INVALID",
                    "detail": "producer and X9 need separately scoped channel ports (no shared-principal switch)"})
    if not callable(ports.clock):
        out.append({"port": "clock", "status": "MISSING", "detail": "an injected timezone-aware clock is required"})
    else:
        out.append(label_ok("clock", ports.clock) or {"port": "clock", "status": "BOUND", "detail": "injected"})
    if not isinstance(ports.client_config, ClientConfig):
        out.append({"port": "client_config", "status": "MISSING", "detail": "existing ClientConfig required"})
    else:
        interest = any(i.owner_ref == settings.owner_ref and i.referent_type == PM_READY_HINT
                       for i in ports.client_config.interests)
        out.append({"port": "client_config", "status": "BOUND" if interest else "INVALID",
                    "detail": "listen interest present" if interest
                    else f"needs [[listen.interest]] owner_ref={settings.owner_ref} referent_type={PM_READY_HINT}"})
    if ports.connect_fn is None:
        if settings.mode == MODE_SYNTHETIC:
            out.append({"port": "transport", "status": "NOT_SYNTHETIC_IN_TEST_MODE", "detail": "real nats-py in test mode"})
        elif nats_py_version() is None:
            out.append({"port": "transport", "status": "MISSING", "detail": "nats-py is not installed"})
        else:
            out.append({"port": "transport", "status": "BOUND", "detail": f"nats-py {nats_py_version()}"})
    else:
        out.append(label_ok("transport", ports.connect_fn) or {"port": "transport", "status": "BOUND",
                                                               "detail": "host connect_fn"})
    return out


def build_binding(settings: PmX9Settings, ports: PmX9HostPorts | None) -> "PmX9Binding":
    """THE explicit factory entry point. Raises PmX9Unbound (with per-port diagnostics) before any I/O."""
    if not isinstance(settings, PmX9Settings):
        raise PmX9Unbound("PM_X9_SETTINGS_INVALID")
    diagnostics = diagnose(settings, ports)
    if settings.mode == MODE_OFF:
        raise PmX9Unbound("PM_X9_DEFAULT_OFF", diagnostics)
    if any(d["status"] != "BOUND" for d in diagnostics):
        raise PmX9Unbound("PM_X9_PORTS_NOT_BOUND", diagnostics)
    return PmX9Binding(settings, ports)


def load_ports(settings: PmX9Settings, *, host_runtime: Any = None) -> PmX9HostPorts:
    """Resolve ``ports_factory = 'module:callable'``. Host runtime is explicit and never ambient."""
    if settings.mode == MODE_OFF:
        raise PmX9Unbound("PM_X9_DEFAULT_OFF", diagnose(settings, None))
    if not settings.ports_factory:
        raise PmX9Unbound("PM_X9_PORTS_FACTORY_MISSING", diagnose(settings, None))
    module_name, _, attr = settings.ports_factory.partition(":")
    try:
        factory = getattr(importlib.import_module(module_name), attr)
        ports = factory(settings, host_runtime=host_runtime) if host_runtime is not None else factory(settings)
    except PmX9Unbound:
        raise
    except Exception as exc:  # noqa: BLE001 - the host factory's failure is reported, never papered over
        raise PmX9Unbound("PM_X9_PORTS_FACTORY_FAILED",
                          [{"port": "ports_factory", "status": "MISSING", "detail": type(exc).__name__}]) from None
    if not isinstance(ports, PmX9HostPorts):
        raise PmX9Unbound("PM_X9_PORTS_FACTORY_FAILED",
                          [{"port": "ports_factory", "status": "INVALID", "detail": "did not return PmX9HostPorts"}])
    return ports


# ------------------------------------------------------------------------------------------- results
@dataclass(frozen=True)
class HintSendResult:
    state: str                 # TRANSPORT_ACCEPTED | UNKNOWN_SEND | NOT_SENT | IN_FLIGHT | PM_EVIDENCE_REJECTED
    reason: str
    event_id: str | None = None
    attempt_ref: str | None = None
    referent_type: str | None = None
    referent_id: str | None = None
    owner_seq: int | None = None
    revision: str | None = None
    packet_sha256: str | None = None
    snapshot_sha256: str | None = None
    replay_refused: bool = False


@dataclass(frozen=True)
class DepositRecord:
    event_id: str
    state: str                 # DEPOSITED_READBACK | NOT_DEPOSITED | HOLD_UNREADABLE | COLLISION | REFINE_...
    reason: str
    receiver_disposition: str
    attempt_ref: str | None = None
    referent_id: str | None = None
    owner_seq: int | None = None
    revision: str | None = None
    packet_sha256: str | None = None
    pointer_sha256: str | None = None
    queued_for_pulse: bool = False


@dataclass(frozen=True)
class ConsumeResult:
    state: str                 # DISPOSITION_READBACK | ALREADY_DISPOSED | HOLD_UNREADABLE | COLLISION | REFINE_...
    reason: str
    event_id: str | None
    disposition: str | None = None          # STALE | NO_MATERIAL_DELTA | MATERIAL_ROUTE (read back) or None
    record: Any = None                      # x9_channel_ingress.X9Disposition when read back
    pointer_sha256: str | None = None
    bridge_reason: str | None = None        # typed PM-side reason when the fresh read failed closed
    work_consumed: bool = False
    effect: str = "NONE_CLAIMED"


@dataclass(frozen=True)
class PulseReport:
    deferred: bool
    reason: str
    processed: tuple[ConsumeResult, ...] = ()
    pending: int = 0
    lost_owner_pulse_required: int = 0


# ------------------------------------------------------------------------------------------- receive side
@dataclass(frozen=True)
class _ReceiveEvidence:
    resolve_req: ResolveRequest
    current: Any               # pm_owner_commit.PmCurrentRead with projection


class PmRereadResolver:
    """Receiver OwnerResolver built from FRESH PM reread semantics. Exactly one PM reread per resolve() call; the
    owner judges the relation. Runs on the ListenClient Dispatcher worker, never on the NATS event loop."""

    description = "PM_REREAD_RESOLVER_BK07"

    def __init__(self, settings: PmX9Settings, reader: Any, *, bound: int = 256) -> None:
        self._settings, self._reader, self._bound = settings, reader, bound
        self._lock = threading.Lock()
        self._evidence: collections.OrderedDict[str, _ReceiveEvidence] = collections.OrderedDict()
        self.notes: dict[str, str] = {}
        self.calls = 0
        self.thread_ids: collections.deque[int] = collections.deque(maxlen=DIAGNOSTIC_RETENTION)

    def _note(self, event_id: str, code: str) -> None:
        with self._lock:
            self.notes[event_id] = code
            while len(self.notes) > self._bound:
                self.notes.pop(next(iter(self.notes)))

    def resolve(self, req: ResolveRequest) -> ResolverResult:
        with self._lock:
            self.calls += 1
            self.thread_ids.append(threading.get_ident())
        s = self._settings
        if (req.owner_ref != s.owner_ref or req.referent_type != PM_READY_HINT
                or req.depth != DEPTH_POINTER_GROUND or not req.pointer_ref):
            self._note(req.event_id, "PM_HINT_BINDING_MISMATCH")
            raise ResolverUnavailable("PM_HINT_BINDING_MISMATCH")
        try:
            cur = self._reader.reread_current(req.referent_id, req.expected_revision,
                                              expected_sha256=req.expected_sha256, expected_seq=req.owner_seq,
                                              require_projection=True)
        except SensingError as exc:
            self._note(req.event_id, exc.code)
            raise ResolverUnavailable(exc.code) from None
        if cur.relation == "SAME" and cur.packet_sha256 != s.packet_sha256:
            self._note(req.event_id, "PM_PACKET_CONTENT_CHANGED_AT_SAME_REVISION")
            raise ResolverUnavailable("PM_PACKET_CONTENT_CHANGED_AT_SAME_REVISION")
        with self._lock:
            self._evidence[req.event_id] = _ReceiveEvidence(req, cur)
            while len(self._evidence) > self._bound:
                self._evidence.popitem(last=False)
        return ResolverResult(
            source_ref=cur.owner_ref, referent_type=req.referent_type, referent_id=cur.referent_id,
            current_revision=cur.current_revision, revision_relation=cur.relation,
            observed_sha256=cur.snapshot_sha256, owner_seq=cur.owner_seq,
            grounding={"snapshot_ref": req.pointer_ref, "source_cut": cur.source_cut, "ready_state": cur.ready_state})

    def take(self, event_id: str) -> _ReceiveEvidence | None:
        with self._lock:
            return self._evidence.pop(event_id, None)


class PulseInbox:
    """Bounded queue of deposited event_ids for the ORDINARY X9 pulse. Depositing never wakes, starts or binds
    anything. Overflow is lawful loss: the pointer stays in the channel and the owner pulse must find it."""

    def __init__(self, max_pending: int) -> None:
        self._max = max_pending
        self._q: collections.deque[str] = collections.deque()
        self._lock = threading.Lock()
        self.lost_owner_pulse_required = 0

    def offer(self, event_id: str) -> bool:
        with self._lock:
            if event_id in self._q:
                return True
            if len(self._q) >= self._max:
                self.lost_owner_pulse_required += 1
                return False
            self._q.append(event_id)
            return True

    def snapshot(self) -> list[str]:
        """One bounded pulse batch; obligations stay pending until acknowledged."""
        with self._lock:
            return list(self._q)

    def acknowledge(self, event_id: str) -> None:
        """Used only after exact disposition readback, never on a transient HOLD."""
        with self._lock:
            if event_id in self._q:
                self._q.remove(event_id)

    def __len__(self) -> int:
        with self._lock:
            return len(self._q)


class X9DepositSink:
    """ListenClient's extra closure sink. Builds the PointerContext from the VALIDATED D0 (the receiver's own resolve
    request), the fresh owner read and the trusted host coordinates, then calls X9ChannelIngress.deposit."""

    def __init__(self, binding: "PmX9Binding") -> None:
        self._b = binding
        self.records: collections.deque[DepositRecord] = collections.deque(maxlen=DIAGNOSTIC_RETENTION)
        self.total_records = 0
        self._lock = threading.Lock()

    def _keep(self, rec: DepositRecord) -> None:
        with self._lock:
            self.records.append(rec)
            self.total_records += 1

    def deliver(self, closure: Any) -> None:
        b, s = self._b, self._b.settings
        if closure.disposition != ACK_READ:
            self._keep(DepositRecord(closure.event_id, "NOT_DEPOSITED", f"RECEIVER_{closure.disposition}:{closure.reason}",
                                     closure.disposition))
            b.resolver.take(closure.event_id)
            return
        ev = b.resolver.take(closure.event_id)
        if ev is None:
            self._keep(DepositRecord(closure.event_id, x9.HOLD, "RECEIVE_EVIDENCE_MISSING", closure.disposition))
            return
        req, cur = ev.resolve_req, ev.current
        if ((closure.owner_ref, closure.referent_type, closure.referent_id, closure.revision_after,
             closure.observed_sha256) != (req.owner_ref, req.referent_type, req.referent_id, req.expected_revision,
                                          req.expected_sha256)):
            self._keep(DepositRecord(closure.event_id, x9.HOLD, "CLOSURE_D0_REQUEST_MISMATCH", closure.disposition))
            return
        now = b.now()
        context = x9.PointerContext(
            event_id=closure.event_id, attempt_id=s.attempt_ref, owner_ref=closure.owner_ref,
            referent_type=closure.referent_type, revision=closure.revision_after,
            expected_sha256=closure.observed_sha256, claim_ref=s.claim_ref, packet_ref=s.packet_ref,
            packet_sha256=s.packet_sha256, queue_ref=s.queue_ref, producer_id=s.producer_principal,
            receiver_ref=s.x9_session_ref, source_cut=cur.source_cut,
            expires_at=(now + timedelta(seconds=s.pointer_ttl_seconds)).astimezone(timezone.utc).isoformat(),
            way_home=tuple(closure.way_home), referent_id=closure.referent_id, owner_seq=req.owner_seq)
        result = b.producer_ingress.deposit(closure, context, now=now)
        queued = result.state == "DEPOSITED_READBACK" and b.inbox.offer(closure.event_id)
        self._keep(DepositRecord(closure.event_id, result.state, result.reason, closure.disposition, s.attempt_ref,
                                 closure.referent_id, req.owner_seq, closure.revision_after, s.packet_sha256,
                                 result.pointer_sha256, queued))


class PmHintCutReader:
    """X9's fresh PM read for one pointer: PM reread of the pointer's own referent at its opaque revision, projected
    losslessly to PMOwnerCut. Any failure raises (=> X9 HOLD) and leaves a typed reason in ``last_error``."""

    def __init__(self, reader: Any) -> None:
        self._reader = reader
        self.calls = 0
        self.last_error: str | None = None

    def read_current_for_pointer(self, pointer: Any):
        self.calls += 1
        self.last_error = None
        if pointer.referent_type != PM_READY_HINT or pointer.referent_id is None or pointer.owner_seq is None:
            self.last_error = "POINTER_LACKS_PM_REFERENT_BINDING"
            raise SensingError(self.last_error)
        try:
            cur = self._reader.reread_current(pointer.referent_id, pointer.revision,
                                              expected_sha256=pointer.expected_sha256, expected_seq=pointer.owner_seq,
                                              require_projection=True)
        except SensingError as exc:
            self.last_error = exc.code
            raise
        return x9.PMOwnerCut(
            owner_ref=cur.owner_ref, claim_ref=cur.claim_ref, packet_ref=cur.packet_ref,
            packet_sha256=cur.packet_sha256, queue_ref=cur.queue_ref, revision=cur.current_revision,
            relation_to_hint=cur.relation, material_sha256=cur.material_sha256, source_cut=cur.source_cut,
            committed_readback=cur.readback_verified is True, authenticated=cur.authenticated is True,
            material_ready=cur.ready_state == "MATERIAL_READY", active_hold=cur.active_hold)


def owner_event_to_raw(ev: OwnerEvent) -> dict[str, Any]:
    """Re-express a TRUSTED OwnerEvent (commit evidence came from the verified receipt) for SendClient.send()."""
    delta: dict[str, Any] = {"kind": ev.delta_kind, "expected_sha256": ev.expected_sha256}
    if ev.delta_kind == "POINTER":
        delta["ref"] = ev.pointer_ref
    else:
        delta["fields"] = dict(ev.inline_fields or {})
    return {"event_id": ev.event_id, "owner_ref": ev.owner_ref, "source_ref": ev.source_ref,
            "referent": {"type": ev.referent_type, "id": ev.referent_id}, "owner_seq": ev.owner_seq,
            "revision_basis": {"after": ev.revision_after, "before": ev.revision_before},
            "change_class": ev.change_class, "delta": delta,
            "commit": {"state": COMMITTED, "readback_ref": ev.commit_readback_ref, "observed_at": ev.commit_observed_at},
            "way_home": list(ev.way_home), "requires_ack": ev.requires_ack}


# ------------------------------------------------------------------------------------------- the binding
class PmX9Binding:
    """Public operations: open_sender / send_hint / start_listener / wait_for_ingress / consume_one / pulse / close.
    Construct only through ``build_binding``."""

    def __init__(self, settings: PmX9Settings, ports: PmX9HostPorts) -> None:
        self.settings, self.ports = settings, ports
        expectation = settings.expectation()
        self.sender_reader = pm.PmOwnerCommitReader(expectation, port=ports.pm_port, enabled=True)
        self.reread_reader = pm.PmOwnerCommitReader(expectation, port=ports.pm_reread_port or ports.pm_port,
                                                    enabled=True)
        self.resolver = PmRereadResolver(settings, self.reread_reader)
        self.hint_reader = PmHintCutReader(self.reread_reader)
        common = {"enabled": True, "producer_principal": settings.producer_principal,
                  "x9_principal": settings.x9_principal, "x9_session_ref": settings.x9_session_ref}
        self.producer_ingress = x9.X9ChannelIngress(channel=ports.producer_channel, **common)
        self.x9_ingress = x9.X9ChannelIngress(channel=ports.x9_channel, pm_hint_reader=self.hint_reader, **common)
        self.inbox = PulseInbox(settings.max_pending_ingress)
        self.sink = X9DepositSink(self)
        self._sender: SendClient | None = None
        self._listener: ListenClient | None = None
        self._results: collections.deque[Any] = collections.deque(maxlen=DIAGNOSTIC_RETENTION)
        self._ingress_count = 0
        self._results_cv = threading.Condition()
        self._pulse_lock = threading.Lock()

    # -- clock
    def now(self) -> datetime:
        value = self.ports.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise BindingError("CLOCK_NOT_TIMEZONE_AWARE")
        return value

    def _epoch(self) -> float:
        return self.now().timestamp()

    # -- 1) send one hint
    def open_sender(self) -> None:
        if self._sender is None:
            client = SendClient(self.ports.client_config, connect_fn=self.ports.connect_fn, clock=self._epoch)
            try:
                client.connect()
            except Exception:
                try:
                    client.close()
                except Exception:
                    pass
                raise
            self._sender = client

    def send_hint(self, receipt_ref: str) -> HintSendResult:
        """Expected receipt -> PmOwnerCommitReader (authenticated, committed, read-back, re-read SAME) -> SendClient.
        Rejected PM evidence sends nothing. A repeat of the same event is refused by the durable ledger."""
        if self._sender is None:
            raise BindingError("SENDER_NOT_OPEN")
        try:
            trusted = read_trusted_owner_event(self.sender_reader, receipt_ref=receipt_ref,
                                               expected_owner_ref=self.settings.owner_ref)
        except SensingError as exc:
            return HintSendResult("PM_EVIDENCE_REJECTED", exc.code, attempt_ref=self.settings.attempt_ref)
        ev = trusted.event
        if ev.referent_type != PM_READY_HINT or ev.delta_kind != "POINTER":
            return HintSendResult("PM_EVIDENCE_REJECTED", "PM_EVENT_NOT_A_READY_HINT_POINTER", ev.event_id,
                                  self.settings.attempt_ref)
        try:
            outcome = self._sender.send(owner_event_to_raw(ev))
        except SensingError as exc:
            return HintSendResult("NOT_SENT", exc.code, ev.event_id, self.settings.attempt_ref, ev.referent_type,
                                  ev.referent_id, ev.owner_seq, ev.revision_after, self.settings.packet_sha256,
                                  ev.expected_sha256)
        state = "TRANSPORT_ACCEPTED" if outcome.state == "ACCEPTED" else outcome.state
        return HintSendResult(state, outcome.reason, ev.event_id, self.settings.attempt_ref, ev.referent_type,
                              ev.referent_id, ev.owner_seq, ev.revision_after, self.settings.packet_sha256,
                              ev.expected_sha256, outcome.replay_refused)

    # -- 2) receiving resolver + deposit sink on the existing ListenClient
    def _on_result(self, result: Any) -> None:
        with self._results_cv:
            self._results.append(result)
            self._ingress_count += 1
            self._results_cv.notify_all()

    def start_listener(self) -> None:
        if self._listener is None:
            client = ListenClient(self.ports.client_config, resolver=self.resolver, sink=self.sink,
                                  connect_fn=self.ports.connect_fn, clock=self._epoch, on_result=self._on_result)
            try:
                client.start()
            except Exception:
                try:
                    client.stop()
                except Exception:
                    pass
                raise
            self._listener = client

    @property
    def listener(self) -> ListenClient | None:
        return self._listener

    def ingress_results(self) -> list[Any]:
        """Recent diagnostic tail only; never a durable receipt or pending queue."""
        with self._results_cv:
            return list(self._results)

    @property
    def ingress_count(self) -> int:
        """Total handled frames, independent of diagnostic tail eviction."""
        with self._results_cv:
            return self._ingress_count

    def wait_for_ingress(self, count: int, timeout: float = 5.0) -> bool:
        """Test/ops helper: wait until ``count`` frames have been handled by the receiver worker."""
        with self._results_cv:
            return self._results_cv.wait_for(lambda: self._ingress_count >= count, timeout)

    # -- 3) explicit X9 pulse call
    def consume_one(self, event_id: str | None, prior_material_sha256: str | None = None,
                    now: datetime | None = None) -> ConsumeResult:
        """Called by the ORDINARY X9 pulse. A missing/expired D0 returns the owner-pulse-required outcome; an
        already read-back disposition is returned as-is; otherwise ONE fresh PM read decides."""
        when = self.now() if now is None else now
        self.hint_reader.last_error = None
        res = self.x9_ingress.consume(event_id, now=when, prior_material_sha256=prior_material_sha256)
        disp = res.disposition.disposition if res.disposition is not None else None
        bridge = self.hint_reader.last_error if res.state in (x9.HOLD, x9.COLLISION) else None
        return ConsumeResult(res.state, res.reason, res.event_id if res.event_id is not None else event_id, disp,
                             res.disposition, res.pointer_sha256, bridge)

    def pulse(self, *, human_conversation_active: bool,
              prior_material_sha256: Mapping[str, str] | None = None, now: datetime | None = None) -> PulseReport:
        """One explicit X9 pulse over the queued ingress. An active Human conversation defers it untouched; nothing is
        woken, started or bound here (carrier scheduling is a later internal binding step)."""
        if human_conversation_active is not False:
            return PulseReport(True, "HUMAN_CONVERSATION_ACTIVE_DEFERRED", (), len(self.inbox),
                               self.inbox.lost_owner_pulse_required)
        prior = dict(prior_material_sha256 or {})
        with self._pulse_lock:
            done = []
            for eid in self.inbox.snapshot():
                # Existing consume reconciles exact disposition readback before
                # any append and refuses ambiguous-send replay. An exception
                # leaves this event and the unprocessed tail in the same queue.
                result = self.consume_one(eid, prior.get(eid), now)
                done.append(result)
                if (result.state in {"DISPOSITION_READBACK", "ALREADY_DISPOSED"}
                        and result.event_id == eid and result.record is not None):
                    self.inbox.acknowledge(eid)
            pending = len(self.inbox)
            return PulseReport(False, "PULSE_PENDING_RECONCILIATION" if pending else "PULSE_PROCESSED",
                               tuple(done), pending, self.inbox.lost_owner_pulse_required)

    # -- lifecycle / status
    def status(self) -> dict[str, Any]:
        return {"settings": self.settings.public_summary(), "adapters": _adapters.origin(),
                "pending_for_pulse": len(self.inbox), "lost_owner_pulse_required": self.inbox.lost_owner_pulse_required,
                "deposits": self.sink.total_records, "resolver_calls": self.resolver.calls,
                "ingress_count": self.ingress_count, "diagnostic_retention": DIAGNOSTIC_RETENTION,
                "x9_pm_reads": self.hint_reader.calls,
                "listener": self._listener.summary() if self._listener is not None else None,
                "claims": dict(CLAIMS)}

    def close(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
        if self._sender is not None:
            self._sender.close()
            self._sender = None
