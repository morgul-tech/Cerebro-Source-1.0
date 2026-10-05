"""DurableControlledEffectExecutor: V0.1's execution semantics on a durable store, plus restart recovery.

Differences from V0.1's executor (everything else, including grant, receipts and state vocabulary, is V0.1's):

* Progress lives in PostgreSQL (``PgAdmissionStore``). Every store call is its own short transaction; NO database
  transaction is open while the provider is being called.
* ``begin_attempt`` commits BEFORE the provider is called and is a database-level compare-and-set across processes.
* ``recover(admission_ref)`` is the restart path. It NEVER calls the provider's mutation path and never creates an
  admission, idempotency key or attempt. For an attempt that was reserved but whose result was never recorded it does
  a read-only readback: committed effect -> COMMITTED_READBACK; anything else -> UNKNOWN_EFFECT (never silently
  NO_COMMIT). NO_COMMIT additionally needs the provider's explicit settlement evidence (see ``settlement``).
* A FENCED admission that was never started is NOT started by recovery: the caller continues it with ``execute``,
  which is the ordinary first start of that same admission (no second admission).

HONEST LIMITS: recovery cannot know whether another process is still inside its provider call; its answer is the
conservative UNKNOWN_EFFECT, which blocks any retry and lets the other process's later result be refused or reconciled.
This is local executor recovery, not a global Cerebro owner index, and not credential isolation.
"""
from __future__ import annotations

import threading
from typing import Callable

from controlled_effect_executor.canonical import sha256_hex
from controlled_effect_executor.errors import LedgerBasisError
from controlled_effect_executor.grant import _SEAL, ExecutionGrant
from controlled_effect_executor.ports import ProviderAck, ProviderEffectPort, TargetObservation
from controlled_effect_executor.receipts import AdmissionReceipt, ExecutionResult
from controlled_effect_executor.spec import BatchSpec
from controlled_effect_executor.states import (COMMITTED_READBACK, DENIED, FENCED, IN_FLIGHT, NO_COMMIT, TERMINAL,
                                               UNKNOWN_EFFECT)

from .errors import ReservationNotConfirmed, StorageError
from .settlement import SettlementEvidence, classify_durable, read_evidence
from .store_pg import MAX_PAGE_LIMIT, PgAdmissionStore, Progress, RecoveryPage


class BoundedRecoverySweep:
    """One bounded page per call; a completed sweep always restarts at the head.

    An admit_seq may be allocated before its transaction commits. A later-visible
    low sequence can be missed by the current cursor, so the cursor is never a
    durable resume token and is reset after the tail (including an empty page).
    Restarting this object also starts at the head. Repeated calls are required
    while unresolved work may still appear; listing never calls the provider.
    """

    def __init__(self, store: PgAdmissionStore, *, limit: int = 100,
                 work_order_ref: str | None = None) -> None:
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE_LIMIT:
            raise ValueError("limit-out-of-bounds")
        self._store = store
        self._limit = limit
        self._work_order_ref = work_order_ref
        self._cursor: str | None = None
        self._through_seq: int | None = None
        self.completed_sweeps = 0

    def next_page(self) -> RecoveryPage:
        if self._through_seq is None:
            self._through_seq = self._store.unresolved_high_water(work_order_ref=self._work_order_ref)
        page = self._store.list_unresolved(after_cursor=self._cursor, limit=self._limit,
                                           work_order_ref=self._work_order_ref,
                                           through_seq=self._through_seq)
        if page.next_cursor is None:
            self._cursor = None
            self._through_seq = None
            self.completed_sweeps += 1
        else:
            self._cursor = page.next_cursor
        return page


class DurableControlledEffectExecutor:
    def __init__(self, *, store: PgAdmissionStore, provider: ProviderEffectPort, readback: object,
                 clock: Callable[[], float]) -> None:
        self._store = store
        self._key = store.issue_writer_key()  # one executor per store instance
        self._active: set = set()  # admissions with a provider call running IN THIS executor (in-process knowledge)
        self._active_lock = threading.RLock()
        self._provider = provider
        self._readback = readback
        self._clock = clock

    # -- public API ----------------------------------------------------------------------------------------------------
    def execute(self, receipt: AdmissionReceipt, spec: BatchSpec) -> ExecutionResult:
        """Raises StorageError only BEFORE any provider call (then it is safe to call again). After a provider call it
        never raises on storage trouble: it reports the last durable state and leaves the rest to ``recover``."""
        record = self._verified(receipt, spec)
        if isinstance(record, ExecutionResult):
            return record
        ref = receipt.admission_ref
        prog = self._store.progress_of(ref)
        if prog is None:
            return self._plain(None, DENIED, "ADMISSION_NOT_FOUND")
        if prog.state != FENCED:
            return self._result(prog, "NOT_FENCED_NO_NEW_ATTEMPT", replayed=True)
        expiry = record.spec.delegation_expiry
        if expiry is not None and self._clock() >= expiry:
            return self._plain(ref, DENIED, "ADMISSION_EXPIRED_BEFORE_START", digest=prog.batch_digest)
        try:
            attempt_ref = self._store.begin_attempt(ref, spec.digest, writer_key=self._key)
        except ReservationNotConfirmed:
            now = self._best_effort_progress(ref)
            if now is not None:
                return self._result(now, "ATTEMPT_RESERVATION_NOT_CONFIRMED", replayed=True)
            return self._plain(ref, FENCED, "ATTEMPT_RESERVATION_NOT_CONFIRMED", digest=prog.batch_digest)
        if attempt_ref is None:  # lost the cross-process compare-and-set: NEVER call the provider
            return self._result(self._store.progress_of(ref) or prog, "ATTEMPT_ALREADY_STARTED", replayed=True)
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
                return self._after_call(ref, attempt_ref, spec, acked=False, failure_type=type(exc).__name__,
                                        reason="PROVIDER_RESPONSE_NOT_RECEIVED")
            if not isinstance(ack, ProviderAck) or ack.correlation_ref != ref:
                return self._after_call(ref, attempt_ref, spec, acked=False, failure_type="BadAck",
                                        reason="PROVIDER_ACK_MALFORMED")
            return self._after_call(ref, attempt_ref, spec, acked=True, failure_type=None, reason="")
        finally:
            with self._active_lock:
                self._active.discard(ref)

    def recover(self, admission_ref: str) -> ExecutionResult:
        """Restart path for ONE admission (found via ``list_unresolved``). Read-only toward the provider."""
        prog = self._store.progress_of(admission_ref)
        if prog is None:
            return self._plain(None, DENIED, "ADMISSION_NOT_FOUND")
        try:
            record = self._store.get_admission(admission_ref)
        except LedgerBasisError:
            return self._plain(admission_ref, DENIED, "STORED_ADMISSION_UNPARSEABLE")
        if record is None:
            return self._plain(None, DENIED, "ADMISSION_NOT_FOUND")
        if prog.state == FENCED:
            return self._result(prog, "RECOVERY_NOT_STARTED_USE_EXECUTE", replayed=True)
        if prog.state in TERMINAL:
            return self._result(prog, "RECOVERY_NOT_APPLICABLE_TERMINAL", replayed=True)
        with self._active_lock:
            if admission_ref in self._active:
                return self._result(prog, "RECOVERY_REFUSED_CALL_ACTIVE_IN_THIS_PROCESS", replayed=True)
        return self._resolve(admission_ref, record.spec, prog)

    def reconcile(self, admission_ref: str) -> ExecutionResult:
        """Alias of ``recover`` for V0.1 callers (read-only inspection; never calls the provider's mutation path)."""
        return self.recover(admission_ref)

    def list_unresolved(self, **kwargs: object) -> RecoveryPage:
        return self._store.list_unresolved(**kwargs)  # type: ignore[arg-type]

    def new_recovery_sweep(self, *, limit: int = 100,
                           work_order_ref: str | None = None) -> BoundedRecoverySweep:
        """Create a read-only restart-safe pager; call next_page repeatedly across sweeps."""
        return BoundedRecoverySweep(self._store, limit=limit, work_order_ref=work_order_ref)

    def declare_in_flight_lost(self, admission_ref: str, *, owner_reason_ref: str) -> ExecutionResult:
        prog = self._store.progress_of(admission_ref)
        if prog is None:
            return self._plain(None, DENIED, "ADMISSION_NOT_FOUND")
        with self._active_lock:
            if admission_ref in self._active:
                return self._result(prog, "DECLARE_LOST_REFUSED_CALL_STILL_ACTIVE", replayed=True)
        try:
            self._store.declare_in_flight_lost(admission_ref, prog.batch_digest, owner_reason_ref, writer_key=self._key)
        except LedgerBasisError:
            return self._result(self._store.progress_of(admission_ref) or prog, "DECLARE_LOST_NOT_APPLICABLE",
                                replayed=True)
        return self._result(self._store.progress_of(admission_ref) or prog, "OWNER_DECLARED_IN_FLIGHT_LOST")

    def status(self, admission_ref: str) -> ExecutionResult:
        prog = self._store.progress_of(admission_ref)
        if prog is None:
            return self._plain(None, DENIED, "ADMISSION_NOT_FOUND")
        return self._result(prog, "STATUS_ONLY", replayed=True)

    # -- internals ---------------------------------------------------------------------------------------------------------
    def _verified(self, receipt: AdmissionReceipt, spec: BatchSpec):
        if not isinstance(receipt, AdmissionReceipt) or not isinstance(spec, BatchSpec) or not receipt.verify():
            return self._plain(None, DENIED, "RECEIPT_FORGED_OR_MALFORMED")
        try:
            record = self._store.get_admission(receipt.admission_ref)
        except LedgerBasisError:
            return self._plain(receipt.admission_ref, DENIED, "STORED_ADMISSION_UNPARSEABLE")
        if record is None:
            return self._plain(None, DENIED, "ADMISSION_NOT_FOUND")
        if record.receipt != receipt:
            return self._plain(receipt.admission_ref, DENIED, "RECEIPT_NOT_THE_STORED_ADMISSION")
        if spec.digest != record.receipt.batch_digest or spec != record.spec:
            return self._plain(receipt.admission_ref, DENIED, "RECEIPT_SPEC_MISMATCH")
        return record

    def _after_call(self, ref: str, attempt_ref: str, spec: BatchSpec, *, acked: bool, failure_type: "str | None",
                    reason: str) -> ExecutionResult:
        """Record what happened at the provider. The provider call is over; storage trouble here must not raise."""
        try:
            self._store.record_provider_result(ref, attempt_ref, spec.digest, acked=acked, failure_type=failure_type,
                                               writer_key=self._key)
        except LedgerBasisError:  # a concurrent recovery already moved this attempt: report it, then reconcile
            prog = self._best_effort_progress(ref)
            if prog is None:
                return self._plain(ref, IN_FLIGHT, "PROVIDER_RESULT_NOT_RECORDED_STORAGE_ERROR")
            if prog.state in TERMINAL:
                return self._result(prog, "CONCURRENT_RESOLUTION_ALREADY_RECORDED", replayed=True)
            return self._resolve(ref, spec, prog)
        except StorageError:
            return self._plain(ref, IN_FLIGHT, "PROVIDER_RESULT_NOT_RECORDED_STORAGE_ERROR", attempt=attempt_ref,
                               digest=spec.digest, started=1)
        prog = self._best_effort_progress(ref)
        if not acked:
            return self._result(prog, reason) if prog else self._plain(ref, UNKNOWN_EFFECT, reason, attempt=attempt_ref,
                                                                         digest=spec.digest, started=1)
        if prog is None:
            return self._plain(ref, IN_FLIGHT, "PROVIDER_ACK_RECORDED_STATE_UNREADABLE", attempt=attempt_ref,
                               digest=spec.digest, started=1)
        return self._resolve(ref, spec, prog)

    def _resolve(self, ref: str, spec: BatchSpec, prog: Progress) -> ExecutionResult:
        obs, settlement = read_evidence(self._readback, ref, spec.target_identity)
        classification, reason = classify_durable(ref, spec, obs, settlement, ack_seen=prog.ack_recorded)
        obs_digest = sha256_hex(obs.as_dict()) if isinstance(obs, TargetObservation) else None
        st_status = settlement.status if isinstance(settlement, SettlementEvidence) else None
        st_digest = sha256_hex(settlement.as_dict()) if isinstance(settlement, SettlementEvidence) else None
        target_state = {"COMMITTED": COMMITTED_READBACK, "NO_COMMIT": NO_COMMIT}.get(classification, UNKNOWN_EFFECT)
        attempt = prog.attempt_ref
        assert attempt is not None
        common = dict(settlement_status=st_status, settlement_digest=st_digest, writer_key=self._key)
        try:
            seq = prog.last_seq
            if prog.state == IN_FLIGHT and target_state == NO_COMMIT:
                # IN_FLIGHT -> UNKNOWN_EFFECT is the only legal edge toward NO_COMMIT: say first, honestly, that the
                # result of this attempt was never recorded, then let the explicit settlement evidence close it.
                self._store.record_reconciliation(
                    ref, attempt, spec.digest, state_after=UNKNOWN_EFFECT, classification="INDETERMINATE",
                    reason_code="RECOVERY_ATTEMPT_RESULT_NOT_RECORDED", observation_digest=obs_digest,
                    basis_reason=reason, expected_last_seq=seq, **common)
                seq += 1
            elif prog.state == IN_FLIGHT and target_state == UNKNOWN_EFFECT and not prog.ack_recorded:
                reason_to_store = "RECOVERY_ATTEMPT_RESULT_NOT_RECORDED"
                self._store.record_reconciliation(
                    ref, attempt, spec.digest, state_after=UNKNOWN_EFFECT, classification=classification,
                    reason_code=reason_to_store, observation_digest=obs_digest, basis_reason=reason,
                    expected_last_seq=seq, **common)
                return self._after(ref, reason_to_store)
            self._store.record_reconciliation(
                ref, attempt, spec.digest, state_after=target_state, classification=classification,
                reason_code=reason, observation_digest=obs_digest, expected_last_seq=seq, **common)
        except LedgerBasisError:  # StateMoved included: a concurrent writer won; report it, never overwrite or re-call
            return self._after(ref, "CONCURRENT_RESOLUTION_ALREADY_RECORDED", fallback=prog)
        except StorageError:
            return self._result(prog, "RECONCILIATION_NOT_RECORDED_STORAGE_ERROR", replayed=True)
        return self._after(ref, reason)

    def _after(self, ref: str, reason: str, fallback: "Progress | None" = None) -> ExecutionResult:
        prog = self._best_effort_progress(ref) or fallback
        assert prog is not None
        return self._result(prog, reason, replayed=reason == "CONCURRENT_RESOLUTION_ALREADY_RECORDED")

    def _best_effort_progress(self, ref: str) -> "Progress | None":
        try:
            return self._store.progress_of(ref)
        except StorageError:
            return None

    def _result(self, prog: Progress, reason: str, *, replayed: bool = False) -> ExecutionResult:
        return ExecutionResult(
            state=prog.state, reason_code=reason, admission_ref=prog.admission_ref, attempt_ref=prog.attempt_ref,
            batch_digest=prog.batch_digest, attempts_started=1 if prog.attempt_ref else 0, replayed=replayed,
            automatic_retry_allowed=False, owner_action_required=prog.state in (NO_COMMIT, UNKNOWN_EFFECT))

    def _plain(self, ref: "str | None", state: str, reason: str, *, attempt: "str | None" = None,
               digest: "str | None" = None, started: int = 0) -> ExecutionResult:
        return ExecutionResult(
            state=state, reason_code=reason, admission_ref=ref, attempt_ref=attempt, batch_digest=digest,
            attempts_started=started, replayed=False, automatic_retry_allowed=False,
            owner_action_required=state in (NO_COMMIT, UNKNOWN_EFFECT))
