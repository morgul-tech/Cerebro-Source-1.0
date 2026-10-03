"""Default-off A7 Context learning / PRE_CLOSE projection candidate.

This module is deliberately outside guarded src/signalvev_sensing. It binds
the already-merged A7LearningBridge persistence port to a constructor-injected
Context owner port, then exposes one bounded Operational Pulse PRE_CLOSE
projection seam. It performs no network, storage, scheduling or live effect.

Transport delivery/ACK is never consumption. A pending learning pointer is
emitted only after exact owner currentness/privacy readback for the intended
consumer. Consumption is recorded only after a separate actor disposition
receipt is read back exactly.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from signalvev_sensing.a7_learning import LearningReceipt, LearningRecord
from signalvev_sensing.model import canonical, is_id, sha256_hex

PRE_CLOSE = "PRE_CLOSE"
POINTER_READY = "POINTER_READY"
HOLD_TARGET_SCOPE_UNVERIFIED = "HOLD_TARGET_SCOPE_UNVERIFIED"
NO_PENDING = "NO_PENDING"

APPLIED = "APPLIED"
NO_CHANGE = "NO_CHANGE"
NOT_APPLICABLE = "NOT_APPLICABLE"
HOLD_STALE = "HOLD_STALE"
DEFER_BUSY = "DEFER_BUSY"
ACTOR_DISPOSITIONS = frozenset(
    {APPLIED, NO_CHANGE, NOT_APPLICABLE, HOLD_STALE, DEFER_BUSY}
)
CONSUMING_DISPOSITIONS = frozenset({APPLIED, NO_CHANGE, NOT_APPLICABLE})


class ContextLearningOutcomeUnknown(RuntimeError):
    """The owner port was invoked but durable commit/readback was not proven."""


@dataclass(frozen=True)
class OwnerAppendReceipt:
    learning_key: str
    record_fingerprint: str
    pending_id: str
    owner_revision: int
    receipt_ref: str
    committed: bool
    duplicate: bool
    machine_liveness_credit: bool = False


@dataclass(frozen=True)
class OwnerLearningReadback:
    learning_key: str
    record_fingerprint: str
    pending_id: str
    owner_revision: int
    readback_ref: str
    readback_verified: bool
    machine_liveness_credit: bool = False


@dataclass(frozen=True)
class PendingLearningReadback:
    learning_key: str
    record_fingerprint: str
    pending_id: str
    owner_revision: int
    origin: str
    machine_attempt_state: str
    machine_liveness_credit: bool
    scope_ref: str
    interest_ref: str
    visibility_ref: str
    current: bool = True


@dataclass(frozen=True)
class ConsumerExpectation:
    actor_ref: str
    generation_ref: str
    role: str
    objective_ref: str
    scope_ref: str
    interest_ref: str
    owner_revision: int
    visibility_ref: str


@dataclass(frozen=True)
class ConsumerReadback:
    actor_ref: str
    generation_ref: str
    role: str
    objective_ref: str
    scope_ref: str
    interest_ref: str
    owner_revision: int
    visibility_ref: str
    pointer_visible: bool
    current: bool


@dataclass(frozen=True)
class PendingPointer:
    pointer_id: str
    learning_key: str
    record_fingerprint: str
    pending_id: str
    owner_revision: int
    origin: str
    machine_attempt_state: str
    machine_liveness_credit: bool
    visibility_ref: str
    authority: str = "NONE"


@dataclass(frozen=True)
class ProjectionResult:
    result: str
    pointer: PendingPointer | None
    reason: str
    emitted: bool


@dataclass(frozen=True)
class ActorDispositionReceipt:
    receipt_ref: str
    projection_id: str
    learning_key: str
    pending_id: str
    actor_ref: str
    generation_ref: str
    owner_revision: int
    disposition: str
    currentness_readback_verified: bool


@dataclass(frozen=True)
class PendingAckReadback:
    pending_id: str
    disposition_receipt_ref: str
    acked: bool
    readback_verified: bool


@dataclass(frozen=True)
class DispositionResult:
    disposition: str
    consumed: bool
    pending_acked: bool
    reason: str


@dataclass(frozen=True)
class TransportObservation:
    delivered: bool
    consumed: bool = False
    effect: bool = False
    reason: str = "TRANSPORT_ACK_NE_CONSUMPTION"


class ContextLearningOwnerPort(Protocol):
    def append_learning(
        self, record: LearningRecord, *, machine_liveness_credit: bool
    ) -> OwnerAppendReceipt: ...

    def read_learning(self, learning_key: str) -> OwnerLearningReadback: ...

    def read_pending(self, pending_id: str) -> PendingLearningReadback | None: ...

    def read_consumer(
        self, actor_ref: str, generation_ref: str
    ) -> ConsumerReadback | None: ...

    def read_disposition(self, receipt_ref: str) -> ActorDispositionReceipt | None: ...

    def ack_pending(
        self, pending_id: str, disposition_receipt_ref: str
    ) -> PendingAckReadback: ...


def _valid_pending_id(value: object) -> bool:
    return isinstance(value, str) and is_id(value)


def _validate_learning_readback(
    record: LearningRecord,
    append: object,
    readback: object,
) -> tuple[OwnerAppendReceipt, OwnerLearningReadback]:
    if not isinstance(append, OwnerAppendReceipt):
        raise ContextLearningOutcomeUnknown("OWNER_APPEND_RECEIPT_INVALID")
    if append.committed is not True:
        raise ContextLearningOutcomeUnknown("OWNER_APPEND_COMMIT_UNPROVEN")
    if append.learning_key != record.learning_key:
        raise ContextLearningOutcomeUnknown("OWNER_APPEND_LEARNING_KEY_MISMATCH")
    if append.record_fingerprint != record.payload_fingerprint:
        raise ContextLearningOutcomeUnknown("OWNER_APPEND_FINGERPRINT_MISMATCH")
    if not _valid_pending_id(append.pending_id):
        raise ContextLearningOutcomeUnknown("OWNER_APPEND_PENDING_ID_INVALID")
    if not is_id(append.receipt_ref):
        raise ContextLearningOutcomeUnknown("OWNER_APPEND_RECEIPT_REF_INVALID")
    if type(append.owner_revision) is not int or append.owner_revision < 0:
        raise ContextLearningOutcomeUnknown("OWNER_APPEND_REVISION_INVALID")
    if append.machine_liveness_credit is not False:
        raise ContextLearningOutcomeUnknown("OWNER_APPEND_MACHINE_LIVENESS_FORBIDDEN")

    if not isinstance(readback, OwnerLearningReadback):
        raise ContextLearningOutcomeUnknown("OWNER_LEARNING_READBACK_INVALID")
    if readback.readback_verified is not True:
        raise ContextLearningOutcomeUnknown("OWNER_LEARNING_READBACK_UNVERIFIED")
    if readback.learning_key != record.learning_key:
        raise ContextLearningOutcomeUnknown("OWNER_READBACK_LEARNING_KEY_MISMATCH")
    if readback.record_fingerprint != record.payload_fingerprint:
        raise ContextLearningOutcomeUnknown("OWNER_READBACK_FINGERPRINT_MISMATCH")
    if readback.pending_id != append.pending_id or not _valid_pending_id(
        readback.pending_id
    ):
        raise ContextLearningOutcomeUnknown("OWNER_READBACK_PENDING_ID_MISMATCH")
    if readback.owner_revision != append.owner_revision:
        raise ContextLearningOutcomeUnknown("OWNER_READBACK_REVISION_MISMATCH")
    if not is_id(readback.readback_ref):
        raise ContextLearningOutcomeUnknown("OWNER_READBACK_REF_INVALID")
    if readback.machine_liveness_credit is not False:
        raise ContextLearningOutcomeUnknown("OWNER_READBACK_MACHINE_LIVENESS_FORBIDDEN")
    return append, readback


class OwnerContextLearningSink:
    """Production-facing shape over a constructor-bound Context owner port.

    The adapter itself grants no storage authority. The bound owner port must
    commit and reread the learning record. A malformed or unavailable proof is
    surfaced as an exception so A7LearningBridge returns LEARNING_COMMIT_UNKNOWN.
    """

    def __init__(self, port: ContextLearningOwnerPort) -> None:
        self._port = port

    def append_and_readback(self, record: LearningRecord) -> LearningReceipt:
        try:
            append = self._port.append_learning(
                record, machine_liveness_credit=False
            )
            readback = self._port.read_learning(record.learning_key)
        except Exception as exc:
            raise ContextLearningOutcomeUnknown(
                "OWNER_APPEND_OR_READBACK_OUTCOME_UNKNOWN"
            ) from exc

        append, readback = _validate_learning_readback(record, append, readback)
        return LearningReceipt(
            learning_key=record.learning_key,
            record_fingerprint=record.payload_fingerprint,
            provider_revision=readback.owner_revision,
            readback_verified=True,
            idempotent_replay=append.duplicate,
            pending_id=readback.pending_id,
        )


def _pulse_current_pre_close(pulse: object) -> bool:
    return (
        isinstance(pulse, dict)
        and pulse.get("stage") == PRE_CLOSE
        and pulse.get("currentness") == "CURRENT"
        and pulse.get("persistence_readback_verified") is True
    )


def prepare_pre_close_projection(
    port: ContextLearningOwnerPort,
    *,
    pulse: dict,
    pending_id: str,
    expected_consumer: ConsumerExpectation,
) -> ProjectionResult:
    """Return a minimal pointer only after exact owner pre-emission readback."""

    if not _pulse_current_pre_close(pulse):
        return ProjectionResult(
            HOLD_TARGET_SCOPE_UNVERIFIED,
            None,
            "PRE_CLOSE_CURRENTNESS_OR_PERSISTENCE_UNVERIFIED",
            False,
        )
    if not _valid_pending_id(pending_id):
        return ProjectionResult(
            HOLD_TARGET_SCOPE_UNVERIFIED,
            None,
            "PENDING_ID_INVALID",
            False,
        )

    try:
        pending = port.read_pending(pending_id)
        consumer = port.read_consumer(
            expected_consumer.actor_ref, expected_consumer.generation_ref
        )
    except Exception:
        return ProjectionResult(
            HOLD_TARGET_SCOPE_UNVERIFIED,
            None,
            "OWNER_PREEMISSION_READBACK_FAILED",
            False,
        )

    if pending is None:
        return ProjectionResult(NO_PENDING, None, "PENDING_NOT_FOUND", False)
    if not isinstance(pending, PendingLearningReadback) or not isinstance(
        consumer, ConsumerReadback
    ):
        return ProjectionResult(
            HOLD_TARGET_SCOPE_UNVERIFIED,
            None,
            "OWNER_PREEMISSION_READBACK_INVALID",
            False,
        )

    exact = (
        pending.current is True
        and consumer.current is True
        and consumer.actor_ref == expected_consumer.actor_ref
        and consumer.generation_ref == expected_consumer.generation_ref
        and consumer.role == expected_consumer.role
        and consumer.objective_ref == expected_consumer.objective_ref
        and consumer.scope_ref == expected_consumer.scope_ref
        and consumer.interest_ref == expected_consumer.interest_ref
        and consumer.owner_revision == expected_consumer.owner_revision
        and consumer.visibility_ref == expected_consumer.visibility_ref
        and consumer.pointer_visible is True
        and pending.owner_revision == expected_consumer.owner_revision
        and pending.scope_ref == expected_consumer.scope_ref
        and pending.interest_ref == expected_consumer.interest_ref
        and pending.visibility_ref == expected_consumer.visibility_ref
        and pending.machine_liveness_credit is False
        and _valid_pending_id(pending.pending_id)
        and pending.pending_id == pending_id
        and is_id(pending.learning_key)
        and is_id(pending.visibility_ref)
    )
    if not exact:
        return ProjectionResult(
            HOLD_TARGET_SCOPE_UNVERIFIED,
            None,
            "TARGET_OR_PRIVACY_CURRENTNESS_MISMATCH",
            False,
        )

    pointer_material = {
        "learning_key": pending.learning_key,
        "record_fingerprint": pending.record_fingerprint,
        "pending_id": pending.pending_id,
        "owner_revision": pending.owner_revision,
        "actor_ref": expected_consumer.actor_ref,
        "generation_ref": expected_consumer.generation_ref,
        "scope_ref": expected_consumer.scope_ref,
        "visibility_ref": expected_consumer.visibility_ref,
    }
    pointer = PendingPointer(
        pointer_id="a7ptr:" + sha256_hex(canonical(pointer_material))[:40],
        learning_key=pending.learning_key,
        record_fingerprint=pending.record_fingerprint,
        pending_id=pending.pending_id,
        owner_revision=pending.owner_revision,
        origin=pending.origin,
        machine_attempt_state=pending.machine_attempt_state,
        machine_liveness_credit=False,
        visibility_ref=pending.visibility_ref,
    )
    return ProjectionResult(POINTER_READY, pointer, "EXACT_TARGET_VERIFIED", True)


def observe_transport_ack(*, delivered: bool) -> TransportObservation:
    """Transport state is observable, but never consumption/effect."""
    return TransportObservation(delivered=bool(delivered))


def apply_actor_disposition(
    port: ContextLearningOwnerPort,
    *,
    pointer: PendingPointer,
    expected_consumer: ConsumerExpectation,
    disposition_receipt_ref: str,
) -> DispositionResult:
    """Consume one pointer only from an exact actor-owned disposition readback."""

    if not is_id(disposition_receipt_ref):
        return DispositionResult(HOLD_STALE, False, False, "RECEIPT_REF_INVALID")
    try:
        receipt = port.read_disposition(disposition_receipt_ref)
    except Exception:
        return DispositionResult(
            DEFER_BUSY, False, False, "DISPOSITION_READBACK_UNAVAILABLE"
        )

    if not isinstance(receipt, ActorDispositionReceipt):
        return DispositionResult(
            DEFER_BUSY, False, False, "DISPOSITION_READBACK_MISSING"
        )
    exact = (
        receipt.receipt_ref == disposition_receipt_ref
        and receipt.projection_id == pointer.pointer_id
        and receipt.learning_key == pointer.learning_key
        and receipt.pending_id == pointer.pending_id
        and receipt.actor_ref == expected_consumer.actor_ref
        and receipt.generation_ref == expected_consumer.generation_ref
        and receipt.owner_revision == expected_consumer.owner_revision
        and receipt.currentness_readback_verified is True
        and receipt.disposition in ACTOR_DISPOSITIONS
    )
    if not exact:
        return DispositionResult(
            HOLD_STALE, False, False, "DISPOSITION_CURRENTNESS_OR_BINDING_MISMATCH"
        )

    if receipt.disposition not in CONSUMING_DISPOSITIONS:
        return DispositionResult(
            receipt.disposition,
            False,
            False,
            "ACTOR_DISPOSITION_RECORDED_PENDING_RETAINS",
        )

    try:
        ack = port.ack_pending(pointer.pending_id, disposition_receipt_ref)
    except Exception:
        return DispositionResult(
            DEFER_BUSY, False, False, "PENDING_ACK_READBACK_UNAVAILABLE"
        )
    if not (
        isinstance(ack, PendingAckReadback)
        and ack.pending_id == pointer.pending_id
        and ack.disposition_receipt_ref == disposition_receipt_ref
        and ack.acked is True
        and ack.readback_verified is True
    ):
        return DispositionResult(
            DEFER_BUSY, False, False, "PENDING_ACK_UNVERIFIED"
        )
    return DispositionResult(
        receipt.disposition,
        True,
        True,
        "ACTOR_DISPOSITION_READBACK_AND_PENDING_ACK_VERIFIED",
    )


ELIGIBLE = "ELIGIBLE"
HOLD_CURRENT_COLLISION = "HOLD_CURRENT_COLLISION"


@dataclass(frozen=True)
class CurrentActorWork:
    actor_ref: str
    generation_ref: str
    carrier_ref: str
    claim_ref: str
    active: bool
    current_verified: bool


@dataclass(frozen=True)
class HistoricalClaimResidue:
    claim_ref: str
    released: bool
    historical: bool = True


@dataclass(frozen=True)
class TaskEligibilityResult:
    result: str
    eligible: bool
    reason: str
    blocking_claim_ref: str | None = None


def assess_task_eligibility(
    *,
    actor_ref: str,
    generation_ref: str,
    carrier_ref: str,
    current_work: tuple[CurrentActorWork, ...],
    historical_residue: tuple[HistoricalClaimResidue, ...] = (),
) -> TaskEligibilityResult:
    """Block only on current exact-carrier work, never historical retained residue.

    Historical rows are accepted as provenance input but cannot become a current
    collision by themselves. This is the PM9480 negative-canary boundary.
    """
    for work in current_work:
        if (
            work.current_verified is True
            and work.active is True
            and work.actor_ref == actor_ref
            and work.generation_ref == generation_ref
            and work.carrier_ref == carrier_ref
        ):
            return TaskEligibilityResult(
                HOLD_CURRENT_COLLISION,
                False,
                "CURRENT_EXACT_ACTOR_GENERATION_CARRIER_WORK_EXISTS",
                work.claim_ref,
            )
    return TaskEligibilityResult(
        ELIGIBLE,
        True,
        "NO_CURRENT_EXACT_ACTOR_GENERATION_CARRIER_COLLISION",
    )
