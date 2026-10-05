"""AdmissionStore: the atomic one-use admission fence plus the append-only attempt ledger (in-memory reference).

NOT a production store, migration, service or daemon. It exists so the exact semantics are executable and testable:

* admit() performs the owner read and the admission insert inside ONE owner-linearizable section
  (ports.OwnerFencePort.atomic_read) and one store-lock section, so a revoke is ordered strictly before or strictly
  after the fence; never "between the read and the insert".
* at most one admission per idempotency key; exact replay returns the existing receipt; a different batch under the
  same key is denied; a DENIED request creates no admission and consumes no key.
* the BatchSpec and the AdmissionReceipt are immutable facts. Progress is only an append-only, hash-chained event
  list whose every event must carry the admission's exact basis; only the transitions in states.TRANSITIONS exist.
"""
from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from typing import Callable

from .errors import LedgerBasisError
from .ports import OwnerFencePort, OwnerSnapshot
from .receipts import AdmissionDecision, AdmissionReceipt, LedgerEvent, Provenance, reconciliation_problem
from .spec import BatchSpec
from .states import DENIED, FENCED, IN_FLIGHT, TRANSITIONS, UNKNOWN_EFFECT

SYNTHETIC_PREFIX = "SYNTH-"  # the reference store refuses any authority-bearing ref that is not obviously synthetic


@dataclass(frozen=True)
class AdmissionRecord:
    spec: BatchSpec
    receipt: AdmissionReceipt


class InMemoryAdmissionStore:
    def __init__(self, owner: OwnerFencePort, *, clock: Callable[[], float]) -> None:
        self._owner = owner
        self._clock = clock
        self._lock = threading.RLock()
        self._records: dict[str, AdmissionRecord] = {}
        self._by_key: dict[str, str] = {}
        self._events: dict[str, list] = {}
        self._denials: list = []
        self._writer_key: object = object()
        self._writer_key_issued = False

    # -- the fence ------------------------------------------------------------------------------------------------
    def admit(self, spec: BatchSpec, presented_digest: str) -> AdmissionDecision:
        if not isinstance(spec, BatchSpec):
            return self._deny("SPEC_INVALID", None)
        digest = spec.digest
        if presented_digest != digest:
            return self._deny("DIGEST_MISMATCH", digest)
        for value in (spec.delegation_ref, spec.actor_ref, spec.human_approval_ref, spec.target_identity):
            if not value.startswith(SYNTHETIC_PREFIX):
                return self._deny("NON_SYNTHETIC_REF_REFUSED", digest)
        with self._owner.atomic_read() as snap:
            with self._lock:
                known = self._by_key.get(spec.idempotency_key)
                if known is not None:
                    rec = self._records[known]
                    if rec.receipt.batch_digest == digest:
                        return AdmissionDecision(self._state(known), "EXACT_REPLAY", rec.receipt, True, digest)
                    return self._deny("IDEMPOTENCY_KEY_CONFLICT", digest)
                reason, view = self._check(spec, snap)
                if reason is not None:
                    return self._deny(reason, digest)
                assert view is not None
                live = snap.currentness()
                if live != view.owner_currentness:  # compare-and-set: the owner moved between our read and our insert
                    return self._deny("OWNER_MOVED_DURING_ADMISSION", digest)
                receipt = AdmissionReceipt.seal(
                    state=FENCED, batch_ref=spec.batch_ref, batch_digest=digest,
                    operations_digest=spec.operations_digest, work_order_ref=spec.work_order_ref,
                    idempotency_key=spec.idempotency_key, delegation_ref=spec.delegation_ref,
                    delegation_revision=spec.delegation_revision, delegation_currentness=view.owner_currentness,
                    owner_currentness_at_fence=live,
                    delegation_expiry=spec.delegation_expiry, actor_ref=spec.actor_ref,
                    actor_generation=spec.actor_generation, approval_ref=spec.human_approval_ref,
                    target_identity=spec.target_identity, artifact_version=spec.artifact_version,
                    fenced_at=int(self._clock()))
                self._records[receipt.admission_ref] = AdmissionRecord(spec, receipt)
                self._by_key[spec.idempotency_key] = receipt.admission_ref
                self._events[receipt.admission_ref] = []
                self._append(receipt.admission_ref, "ADMISSION_FENCED", FENCED, "FENCED_ONCE", None, digest)
                return AdmissionDecision(FENCED, "FENCED_ONCE", receipt, False, digest)

    def _check(self, spec: BatchSpec, snap: OwnerSnapshot) -> tuple:
        """(reason|None, view|None)."""
        view = snap.read_delegation(spec.delegation_ref)
        if view is None or view.delegation_ref != spec.delegation_ref:
            return "DELEGATION_NOT_FOUND", None
        if view.status == "REVOKED":
            return "DELEGATION_REVOKED", None
        now = self._clock()
        if (view.status == "EXPIRED" or (view.expires_at is not None and now >= view.expires_at)
                or (spec.delegation_expiry is not None and now >= spec.delegation_expiry)):
            return "DELEGATION_EXPIRED", None
        if spec.delegation_revision < view.delegation_revision:
            return "STALE_DELEGATION_REVISION", None
        if spec.delegation_revision > view.delegation_revision:
            return "DELEGATION_REVISION_MISMATCH", None
        if spec.actor_ref != view.actor_ref:
            return "ACTOR_MISMATCH", None
        if spec.actor_generation != view.actor_generation:
            return "ACTOR_GENERATION_MISMATCH", None
        if spec.delegation_expiry != view.expires_at:
            return "DELEGATION_EXPIRY_MISMATCH", None
        if not set(spec.operation_names) <= set(view.allowed_operations):
            return "OPERATION_OUT_OF_SCOPE", None
        entry = next((e for e in view.target_scope if e.target_identity == spec.target_identity), None)
        if entry is None:
            return "TARGET_OUT_OF_SCOPE", None
        if entry.artifact_versions is not None and spec.artifact_version not in entry.artifact_versions:
            return "VERSION_OUT_OF_SCOPE", None
        approval = snap.read_approval(spec.human_approval_ref)
        if approval is None or approval.approval_ref != spec.human_approval_ref:
            return "APPROVAL_NOT_FOUND", None
        if approval.status != "APPROVED":
            return "APPROVAL_NOT_ACTIVE", None
        if (approval.target_identity, approval.artifact_version, approval.operations_digest) != (
                spec.target_identity, spec.artifact_version, spec.operations_digest):
            return "APPROVAL_BINDING_MISMATCH", None
        return None, view

    def _deny(self, reason: str, digest: "str | None") -> AdmissionDecision:
        with self._lock:
            self._denials.append((reason, digest))
        return AdmissionDecision(DENIED, reason, None, False, digest)

    # -- immutable reads ------------------------------------------------------------------------------------------
    def get_admission(self, admission_ref: str) -> "AdmissionRecord | None":
        with self._lock:
            return self._records.get(admission_ref)

    def state_of(self, admission_ref: str) -> "str | None":
        with self._lock:
            return self._state(admission_ref) if admission_ref in self._records else None

    def _state(self, admission_ref: str) -> str:
        return self._events[admission_ref][-1].state_after

    def attempts_started(self, admission_ref: str) -> int:
        with self._lock:
            return sum(1 for e in self._events.get(admission_ref, ()) if e.event_kind == "ATTEMPT_STARTED")

    def attempt_ref_of(self, admission_ref: str) -> "str | None":
        with self._lock:
            for e in self._events.get(admission_ref, ()):
                if e.event_kind == "ATTEMPT_STARTED":
                    return e.attempt_ref
            return None

    def provider_ack_recorded(self, admission_ref: str) -> bool:
        with self._lock:
            return any(e.event_kind == "PROVIDER_RESULT" and dict(e.detail).get("acked") is True
                       for e in self._events.get(admission_ref, ()))

    def denials(self) -> tuple:
        with self._lock:
            return tuple(self._denials)

    def provenance(self, admission_ref: str) -> Provenance:
        with self._lock:
            rec = self._records[admission_ref]
            return Provenance(rec.spec.basis(), rec.receipt, tuple(self._events[admission_ref]))

    # -- the only writes after admission (append-only) -------------------------------------------------------------
    def issue_writer_key(self) -> object:
        """One-shot capability for the ledger-writing methods below. The executor takes it at construction; nobody who
        merely holds the store can then resolve or advance an attempt. HONEST LIMIT: an in-process API boundary, not
        isolation (code that can reach the executor's private attribute can reach the key)."""
        with self._lock:
            if self._writer_key_issued:
                raise LedgerBasisError("WRITER_KEY_ALREADY_ISSUED")
            self._writer_key_issued = True
            return self._writer_key

    def _require_writer(self, writer_key: object) -> None:
        if writer_key is None or writer_key is not self._writer_key:
            raise LedgerBasisError("WRITER_CAPABILITY_REQUIRED")

    def begin_attempt(self, admission_ref: str, batch_digest: str, *, writer_key: object = None) -> "str | None":
        """FENCED -> IN_FLIGHT, once. Returns the attempt ref, or None if this admission is not FENCED (any other
        state, including a lost race): the caller must then NOT call the provider."""
        with self._lock:
            self._require_writer(writer_key)
            self._require_basis(admission_ref, batch_digest)
            if self._state(admission_ref) != FENCED:
                return None
            attempt_ref = "ATT-" + hashlib.sha256((admission_ref + ":attempt:1").encode()).hexdigest()[:24].upper()
            self._append(admission_ref, "ATTEMPT_STARTED", IN_FLIGHT, "ATTEMPT_RECORDED_BEFORE_PROVIDER_CALL",
                         attempt_ref, batch_digest)
            return attempt_ref

    def record_provider_result(self, admission_ref: str, attempt_ref: str, batch_digest: str, *, acked: bool,
                               failure_type: "str | None" = None, writer_key: object = None) -> None:
        with self._lock:
            self._require_writer(writer_key)
            self._require_basis(admission_ref, batch_digest, attempt_ref)
            state_after = IN_FLIGHT if acked else UNKNOWN_EFFECT
            self._append(admission_ref, "PROVIDER_RESULT", state_after,
                         "PROVIDER_ACK_UNVERIFIED" if acked else "PROVIDER_RESPONSE_NOT_RECEIVED", attempt_ref,
                         batch_digest, {"acked": acked, "failure_type": failure_type})

    def record_reconciliation(self, admission_ref: str, attempt_ref: str, batch_digest: str, *, state_after: str,
                              classification: str, reason_code: str, observation_digest: "str | None",
                              writer_key: object = None) -> None:
        with self._lock:
            self._require_writer(writer_key)
            self._require_basis(admission_ref, batch_digest, attempt_ref)
            problem = reconciliation_problem(state_after, classification, reason_code, observation_digest,
                                             self.provider_ack_recorded(admission_ref))
            if problem:
                raise LedgerBasisError(problem)
            self._append(admission_ref, "RECONCILIATION", state_after, reason_code, attempt_ref, batch_digest,
                         {"classification": classification, "observation_digest": observation_digest})

    def declare_in_flight_lost(self, admission_ref: str, batch_digest: str, reason_ref: str, *,
                               writer_key: object = None) -> None:
        """Owner-facing escalation IN_FLIGHT -> UNKNOWN_EFFECT. No timeout exists in this package; WHEN an owner
        declares an in-flight attempt lost is the owner's policy, not ours."""
        with self._lock:
            self._require_writer(writer_key)
            self._require_basis(admission_ref, batch_digest)
            if self._state(admission_ref) != IN_FLIGHT:
                raise LedgerBasisError("DECLARE_LOST_REQUIRES_IN_FLIGHT")
            attempt_ref = self.attempt_ref_of(admission_ref)
            self._append(admission_ref, "IN_FLIGHT_DECLARED_LOST", UNKNOWN_EFFECT, "OWNER_DECLARED_IN_FLIGHT_LOST",
                         attempt_ref, batch_digest, {"owner_reason_ref": reason_ref})

    def _require_basis(self, admission_ref: str, batch_digest: str, attempt_ref: "str | None" = None) -> None:
        rec = self._records.get(admission_ref)
        if rec is None:
            raise LedgerBasisError("ADMISSION_UNKNOWN")
        if rec.receipt.batch_digest != batch_digest or rec.spec.digest != batch_digest:
            raise LedgerBasisError("BASIS_MISMATCH")
        if attempt_ref is not None and attempt_ref != self.attempt_ref_of(admission_ref):
            raise LedgerBasisError("ATTEMPT_MISMATCH")

    def _append(self, admission_ref: str, kind: str, state_after: str, reason: str, attempt_ref: "str | None",
                batch_digest: str, detail: "dict | None" = None) -> None:
        events = self._events[admission_ref]
        if events:
            current = events[-1].state_after
            if state_after not in TRANSITIONS.get(current, frozenset()):
                raise LedgerBasisError(f"TRANSITION_NOT_ALLOWED:{current}->{state_after}")
            prev = events[-1].event_fingerprint
        else:
            prev = self._records[admission_ref].receipt.receipt_fingerprint
        events.append(LedgerEvent.seal(
            event_kind=kind, seq=len(events) + 1, admission_ref=admission_ref, batch_digest=batch_digest,
            attempt_ref=attempt_ref, state_after=state_after, reason_code=reason,
            detail=tuple((detail or {}).items()), prev_fingerprint=prev))
