"""ControlledEffectExecutor: the only component in this package that calls a provider adapter.

It accepts only a valid FENCED AdmissionReceipt that matches the exact stored BatchSpec, records the attempt BEFORE
the provider is called, calls the injected adapter at most once per admission, and resolves every outcome through
read-only authoritative readback. It has no retry policy and no path from UNKNOWN_EFFECT / NO_COMMIT back to a
provider call: any new attempt is a new batch with a new idempotency key admitted by the owner.

PROOF CEILING: provider login, transport success, an ack, or "the client is connected" are never treated as Cerebro
authority or as proof of effect. Passing tests prove local candidate semantics only.
"""
from __future__ import annotations

import threading
from typing import Callable

from .canonical import sha256_hex
from .errors import LedgerBasisError
from .grant import _SEAL, ExecutionGrant
from .ports import ProviderAck, ProviderEffectPort, ProviderReadbackPort, TargetObservation
from .receipts import AdmissionReceipt, ExecutionResult
from .spec import BatchSpec
from .states import COMMITTED_READBACK, DENIED, FENCED, IN_FLIGHT, NO_COMMIT, UNKNOWN_EFFECT


def classify_observation(correlation_ref: str, spec: BatchSpec, obs: object, *, ack_seen: bool) -> tuple:
    """Pure. (classification, reason). classification is COMMITTED | NO_COMMIT | INDETERMINATE.

    NO_COMMIT is claimed only from an authoritative observation with a COMPLETE applied-history that lacks this
    attempt's correlation, and never when the provider acked (an ack contradicted by readback stays ambiguous)."""
    if not isinstance(obs, TargetObservation):
        return "INDETERMINATE", "READBACK_MALFORMED"
    if not obs.authoritative:
        return "INDETERMINATE", "READBACK_NOT_AUTHORITATIVE"
    if obs.target_identity != spec.target_identity:
        return "INDETERMINATE", "READBACK_TARGET_MISMATCH"
    if correlation_ref in obs.applied_correlation_refs:
        if obs.artifact_version == spec.artifact_version:
            return "COMMITTED", "READBACK_MATCHES_INTENDED_STATE"
        return "INDETERMINATE", "READBACK_APPLIED_BUT_ARTIFACT_MISMATCH"
    if not obs.history_complete:
        return "INDETERMINATE", "READBACK_HISTORY_INCOMPLETE"
    if ack_seen:
        return "INDETERMINATE", "ACK_CONTRADICTED_BY_READBACK"
    return "NO_COMMIT", "READBACK_PROVES_NO_COMMIT"


class ControlledEffectExecutor:
    def __init__(self, *, store, provider: ProviderEffectPort, readback: ProviderReadbackPort,
                 clock: Callable[[], float]) -> None:
        self._store = store
        self._key = store.issue_writer_key()  # one executor per store: only it may write progress to the ledger
        self._active: set = set()  # admissions with a provider call running IN THIS executor (in-process knowledge only)
        self._active_lock = threading.RLock()
        self._provider = provider
        self._readback = readback
        self._clock = clock

    # -- public API ------------------------------------------------------------------------------------------------
    def execute(self, receipt: AdmissionReceipt, spec: BatchSpec) -> ExecutionResult:
        record = self._verified(receipt, spec)
        if isinstance(record, ExecutionResult):
            return record
        ref = receipt.admission_ref
        state = self._store.state_of(ref)
        if state != FENCED:
            return self._result(ref, state, "NOT_FENCED_NO_NEW_ATTEMPT", replayed=True)
        expiry = record.spec.delegation_expiry
        if expiry is not None and self._clock() >= expiry:
            return self._result(ref, DENIED, "ADMISSION_EXPIRED_BEFORE_START")
        attempt_ref = self._store.begin_attempt(ref, spec.digest, writer_key=self._key)
        if attempt_ref is None:  # lost the compare-and-set to another caller: never call the provider
            return self._result(ref, self._store.state_of(ref), "ATTEMPT_ALREADY_STARTED", replayed=True)
        grant = ExecutionGrant._mint(
            _SEAL, admission_ref=ref, attempt_ref=attempt_ref, batch_digest=spec.digest, correlation_ref=ref,
            target_identity=spec.target_identity, target_precondition_version=spec.target_precondition_version,
            artifact_version=spec.artifact_version, operations=spec.operations)
        with self._active_lock:
            self._active.add(ref)
        try:
            try:
                ack = self._provider.apply(grant)
            except Exception as exc:  # noqa: BLE001 - ANY failure after the attempt began may hide a commit
                self._store.record_provider_result(ref, attempt_ref, spec.digest, acked=False,
                                                   failure_type=type(exc).__name__, writer_key=self._key)
                return self._result(ref, UNKNOWN_EFFECT, "PROVIDER_RESPONSE_NOT_RECEIVED")
            if not isinstance(ack, ProviderAck) or ack.correlation_ref != ref:
                self._store.record_provider_result(ref, attempt_ref, spec.digest, acked=False, failure_type="BadAck",
                                                   writer_key=self._key)
                return self._result(ref, UNKNOWN_EFFECT, "PROVIDER_ACK_MALFORMED")
            self._store.record_provider_result(ref, attempt_ref, spec.digest, acked=True, writer_key=self._key)
        finally:
            with self._active_lock:
                self._active.discard(ref)
        return self._reconcile_recorded(ref, record.spec, attempt_ref)

    def reconcile(self, admission_ref: str) -> ExecutionResult:
        """Read-only authoritative inspection. Never calls the provider's mutation path."""
        record = self._store.get_admission(admission_ref)
        if record is None:
            return self._result(None, DENIED, "ADMISSION_NOT_FOUND")
        state = self._store.state_of(admission_ref)
        attempt_ref = self._store.attempt_ref_of(admission_ref)
        acked_in_flight = state == IN_FLIGHT and self._store.provider_ack_recorded(admission_ref)
        if attempt_ref is None or not (state == UNKNOWN_EFFECT or acked_in_flight):
            return self._result(admission_ref, state, "RECONCILE_NOT_APPLICABLE", replayed=True)
        return self._reconcile_recorded(admission_ref, record.spec, attempt_ref)

    def declare_in_flight_lost(self, admission_ref: str, *, owner_reason_ref: str) -> ExecutionResult:
        record = self._store.get_admission(admission_ref)
        if record is None:
            return self._result(None, DENIED, "ADMISSION_NOT_FOUND")
        with self._active_lock:
            if admission_ref in self._active:  # never declare a call lost while this executor is still inside it
                return self._result(admission_ref, self._store.state_of(admission_ref),
                                    "DECLARE_LOST_REFUSED_CALL_STILL_ACTIVE", replayed=True)
        try:
            self._store.declare_in_flight_lost(admission_ref, record.spec.digest, owner_reason_ref,
                                               writer_key=self._key)
        except LedgerBasisError:
            return self._result(admission_ref, self._store.state_of(admission_ref), "DECLARE_LOST_NOT_APPLICABLE",
                                replayed=True)
        return self._result(admission_ref, UNKNOWN_EFFECT, "OWNER_DECLARED_IN_FLIGHT_LOST")

    def status(self, admission_ref: str) -> ExecutionResult:
        state = self._store.state_of(admission_ref)
        if state is None:
            return self._result(None, DENIED, "ADMISSION_NOT_FOUND")
        return self._result(admission_ref, state, "STATUS_ONLY", replayed=True)

    # -- internals ---------------------------------------------------------------------------------------------------
    def _verified(self, receipt: AdmissionReceipt, spec: BatchSpec):
        if not isinstance(receipt, AdmissionReceipt) or not isinstance(spec, BatchSpec) or not receipt.verify():
            return self._result(None, DENIED, "RECEIPT_FORGED_OR_MALFORMED")
        record = self._store.get_admission(receipt.admission_ref)
        if record is None:
            return self._result(None, DENIED, "ADMISSION_NOT_FOUND")
        if record.receipt != receipt:
            return self._result(receipt.admission_ref, DENIED, "RECEIPT_NOT_THE_STORED_ADMISSION")
        if spec.digest != record.receipt.batch_digest or spec != record.spec:
            return self._result(receipt.admission_ref, DENIED, "RECEIPT_SPEC_MISMATCH")
        return record

    def _reconcile_recorded(self, ref: str, spec: BatchSpec, attempt_ref: str) -> ExecutionResult:
        ack_seen = self._store.provider_ack_recorded(ref)
        try:
            obs = self._readback.read_target_state(spec.target_identity)
        except Exception:  # noqa: BLE001 - unavailable readback proves nothing
            obs = None
        classification, reason = classify_observation(ref, spec, obs, ack_seen=ack_seen)
        obs_digest = sha256_hex(obs.as_dict()) if isinstance(obs, TargetObservation) else None
        state_after = {"COMMITTED": COMMITTED_READBACK, "NO_COMMIT": NO_COMMIT}.get(classification, UNKNOWN_EFFECT)
        try:
            self._store.record_reconciliation(ref, attempt_ref, spec.digest, state_after=state_after,
                                              classification=classification, reason_code=reason,
                                              observation_digest=obs_digest, writer_key=self._key)
        except LedgerBasisError:
            current = self._store.state_of(ref)  # a concurrent resolution won: report it, never raise, never re-call
            return self._result(ref, current, "CONCURRENT_RESOLUTION_ALREADY_RECORDED", replayed=True)
        return self._result(ref, state_after, reason)

    def _result(self, ref: "str | None", state: "str | None", reason: str, *, replayed: bool = False) -> ExecutionResult:
        state = state or DENIED
        started = self._store.attempts_started(ref) if ref else 0
        attempt = self._store.attempt_ref_of(ref) if ref else None
        digest = None
        if ref:
            rec = self._store.get_admission(ref)
            digest = rec.receipt.batch_digest if rec else None
        return ExecutionResult(
            state=state, reason_code=reason, admission_ref=ref, attempt_ref=attempt, batch_digest=digest,
            attempts_started=started, replayed=replayed, automatic_retry_allowed=False,
            owner_action_required=state in (NO_COMMIT, UNKNOWN_EFFECT))
