"""C1175 default-off projection of a trusted PM atomic cut onto BK07.

Reuses PR51 ServerPmOwnerProvider, including its authentication/currentness
checks. This module supplies no credential, database, endpoint or commit:
the existing PM/control-host custodian must bind real read_receipt/read_current
operations returning AtomicProjection from ONE committed owner transaction.
Separate Sheets reads or a caller's COMMITTED flag cannot supply that backing.
The same cut must contain material/source_cut/active_hold; None hold means the
OWNER explicitly observed no hold, not that a field was omitted.
"""
from __future__ import annotations

import copy
import hashlib
import json
import threading
from dataclasses import asdict, dataclass
from typing import Callable, Protocol

from .pm_owner_read import (PmReadyRecord, ServerPmOwnerProvider, PmProviderError,
                            PmCredentialPort, _valid)
from adapters.pm_owner_commit import (PmCommittedReadyHint, PmCurrentRead,
                                      ReadyHintExpectation)


def _need(ok, code):
    if not ok:
        raise PmProviderError(code)


@dataclass(frozen=True)
class AtomicProjection:
    record: PmReadyRecord
    material_sha256: str
    source_cut: str
    active_hold: dict | None
    provider_ref: str
    principal_ref: str
    session_ref: str


class AtomicProjectionPort(Protocol):
    """Same read coordinates as PR51; projection included in that atomic read.

    Each operation authenticates current session custody, enforces read ACL,
    and returns the store's durable receipt/event/sequence, not caller values.
    Current lookup remains owner-owned after process restart/missed D0.
    """
    def read_receipt(self, receipt_ref: str, *, owner_ref: str,
                     principal_ref: str) -> AtomicProjection: ...
    def read_current(self, referent_id: str, *, owner_ref: str,
                     principal_ref: str) -> AtomicProjection: ...


def projection_sha256(cut: AtomicProjection) -> str:
    # Read identity is checked separately: distinct authorized readers must
    # agree on the same semantic source cut, without identity-dependent hashes.
    data = {"record": asdict(cut.record), "material_sha256": cut.material_sha256,
            "source_cut": cut.source_cut, "active_hold": cut.active_hold}
    raw = json.dumps(data, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, allow_nan=False).encode("utf-8")
    _need(len(raw) <= 65536, "PM_PROJECTION_BOUND_EXCEEDED")
    return hashlib.sha256(raw).hexdigest()


class _SnapshotBridge:
    def __init__(self, binding):
        self.binding = binding
        self.receipt = self.current = None
        self._seen = {}  # defense only; durable uniqueness remains owner's obligation

    def _read(self, method, ref, *, owner_ref, principal_ref):
        b = self.binding
        try:
            cut = getattr(b.projections, method)(
                ref, owner_ref=owner_ref, principal_ref=principal_ref)
            _need(type(cut) is AtomicProjection, "PM_ATOMIC_PROJECTION_REQUIRED")
            cut = copy.deepcopy(cut)  # no mutable active_hold alias crosses the read
            record = _valid(cut.record, owner_ref)
            _need((cut.provider_ref, cut.principal_ref, cut.session_ref)
                  == (b.provider_ref, b.principal_ref, b.session_ref)
                  and principal_ref == b.principal_ref, "PM_READ_CUSTODY_MISMATCH")
            e = b.expectation
            _need((record.claim_ref, record.packet_ref, record.queue_ref)
                  == (e.claim_ref, e.packet_ref, e.queue_ref), "PM_SOURCE_COORDINATES_MISMATCH")
            _need(isinstance(cut.material_sha256, str) and len(cut.material_sha256) == 64
                  and all(c in "0123456789abcdef" for c in cut.material_sha256),
                  "PM_MATERIAL_SHA_INVALID")
            _need(isinstance(cut.source_cut, str) and cut.source_cut.strip()
                  and "\n" not in cut.source_cut and "\r" not in cut.source_cut,
                  "PM_SOURCE_CUT_REQUIRED")
            _need(cut.active_hold is None or type(cut.active_hold) is dict,
                  "PM_HOLD_PROJECTION_INVALID")
            digest = projection_sha256(cut)
            prior = self._seen.get(record.event_id)
            _need(prior is None or prior == digest, "PM_EVENT_PROJECTION_CONFLICT")
            self._seen[record.event_id] = digest
            if method == "read_receipt":
                _need(record.receipt_ref == ref, "PM_RECEIPT_MISMATCH")
                self.receipt = cut
            else:
                _need(record.referent_id == ref, "PM_REFERENT_MISMATCH")
                self.current = cut
            return record
        except PmProviderError:
            raise
        except Exception:
            raise PmProviderError("PM_ATOMIC_PROJECTION_UNAVAILABLE") from None

    def read_receipt(self, receipt_ref, **kwargs):
        return self._read("read_receipt", receipt_ref, **kwargs)

    def read_current(self, referent_id, **kwargs):
        return self._read("read_current", referent_id, **kwargs)


@dataclass(frozen=True)
class PmProjectionBinding:
    expectation: ReadyHintExpectation
    receipt_ref: str
    audience: str
    provider_ref: str
    principal_ref: str
    session_ref: str
    credentials: PmCredentialPort
    projections: AtomicProjectionPort
    credential_reader: Callable[[], str]
    enabled: bool = False


class PmOwnerProjectionAdapter:
    """Client PmOwnerReadPort. No process-local cache supplies owner currentness."""
    def __init__(self, binding: PmProjectionBinding):
        self.binding = binding
        self._lock = threading.RLock()
        self._snapshots = _SnapshotBridge(binding)
        self._server = ServerPmOwnerProvider(
            owner_ref=binding.expectation.owner_ref, audience=binding.audience,
            credentials=binding.credentials, snapshots=self._snapshots,
            enabled=binding.enabled)
        self.SYNTHETIC_TEST_ONLY = any(
            getattr(p, "SYNTHETIC_TEST_ONLY", False) is True
            for p in (binding.credentials, binding.projections, binding.credential_reader))

    def _credential(self):
        b = self.binding
        _need(b.enabled is True, "PM_PROJECTION_DEFAULT_OFF")
        _need(all(isinstance(v, str) and v.strip() and "\n" not in v and "\r" not in v
                  for v in (b.provider_ref, b.principal_ref, b.session_ref, b.receipt_ref)),
              "PM_HOST_CUSTODY_UNBOUND")
        try:
            return b.credential_reader()
        except Exception:
            raise PmProviderError("PM_CREDENTIAL_READ_UNAVAILABLE") from None

    def read_committed_ready_hint(self, receipt_ref, *, expected_owner_ref):
        with self._lock:
            b = self.binding
            _need(receipt_ref == b.receipt_ref, "PM_RECEIPT_NOT_BOUND")
            try:
                record = self._server.read_committed_ready_hint(
                    receipt_ref, expected_owner_ref=expected_owner_ref,
                    credential=self._credential())
            except PmProviderError as exc:
                raise PmProviderError(str(exc)) from None
            receipt, current = self._snapshots.receipt, self._snapshots.current
            digest = projection_sha256(receipt)
            _need(digest == projection_sha256(current), "PM_PROJECTION_CHANGED_DURING_READ")
            _need(record.packet_sha256 == b.expectation.packet_sha256, "PM_PACKET_BINDING_MISMATCH")
            fields = asdict(record)
            fields.pop("commit_ref")
            fields["way_home"] = record.way_home
            fields["snapshot_sha256"] = digest
            return PmCommittedReadyHint(authenticated=True, committed=True,
                readback_verified=True, consistent_snapshot=True, **fields)

    def reread_ready_hint(self, referent_id, *, expected_owner_ref, expected_revision):
        with self._lock:
            b = self.binding
            _need(expected_owner_ref == b.expectation.owner_ref, "PM_OWNER_MISMATCH")
            credential = self._credential()
            # The historic expectation is reread through authenticated PR51
            # receipt custody, not recovered from caller seq/hash or local memory.
            try:
                principal = self._server._authorize(credential, "read_receipt")
                old = self._snapshots.read_receipt(
                    b.receipt_ref, owner_ref=expected_owner_ref, principal_ref=principal)
                baseline = self._snapshots.receipt
                _need(old.referent_id == referent_id and old.revision_after == expected_revision
                      and old.packet_sha256 == b.expectation.packet_sha256,
                      "PM_BASELINE_BINDING_MISMATCH")
                relation, current = self._server.reread_ready_hint(
                    referent_id, expected_owner_ref=expected_owner_ref,
                    expected_revision=expected_revision, expected_seq=old.owner_seq,
                    expected_sha256=old.snapshot_sha256, credential=credential)
            except PmProviderError as exc:
                raise PmProviderError(str(exc)) from None
            cut = self._snapshots.current
            digest = projection_sha256(cut)
            if relation == "SAME" and digest != projection_sha256(baseline):
                relation = "UNKNOWN"
            return PmCurrentRead(authenticated=True, readback_verified=True,
                owner_ref=current.owner_ref, referent_id=current.referent_id,
                current_revision=current.revision_after, relation=relation,
                snapshot_sha256=digest, owner_seq=current.owner_seq,
                claim_ref=current.claim_ref, packet_ref=current.packet_ref,
                queue_ref=current.queue_ref, packet_sha256=current.packet_sha256,
                ready_state=current.ready_state, material_sha256=cut.material_sha256,
                source_cut=cut.source_cut, consistent_snapshot=True,
                active_hold=copy.deepcopy(cut.active_hold))

    def recover_current_ready_hint(self, referent_id, *, after_seq):
        """One existing PR51 owner read after missed D0; never publish/dispatch."""
        with self._lock:
            credential = self._credential()
            try:
                record = self._server.recover_current_ready_hint(
                    referent_id, after_seq=after_seq, credential=credential)
            except PmProviderError as exc:
                raise PmProviderError(str(exc)) from None
            return copy.deepcopy(self._snapshots.current) if record is not None else None


def create_pm_source_port(binding: PmProjectionBinding):
    """Trusted control-host composition, not a whole X4 ports_factory replacement."""
    _need(type(binding) is PmProjectionBinding, "PM_HOST_BINDING_INVALID")
    port = PmOwnerProjectionAdapter(binding)
    _need(binding.enabled is True, "PM_PROJECTION_DEFAULT_OFF")
    _need(not port.SYNTHETIC_TEST_ONLY, "PM_SYNTHETIC_PRODUCTION_REFUSED")
    _need(callable(binding.credential_reader)
          and callable(getattr(binding.credentials, "authenticate_and_authorize", None))
          and all(callable(getattr(binding.projections, method, None))
                  for method in ("read_receipt", "read_current")),
          "PM_ACTUAL_PORT_BINDING_REQUIRED")
    return port
