"""A7-01 owner-event to Context-learning bridge core.

Flow:
  trusted owner receipt -> existing OwnerEvent -> local relevance/applicability
  -> injected Context-owned durable learning append+readback port.

The guarded sensing runtime owns no datastore implementation. Concrete durable
proof/provider adapters live outside src/signalvev_sensing and are injected
through ContextLearningSink.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable, Protocol

from .a7_owner_receipt import TrustedOwnerCommitReader, read_trusted_owner_event
from .applicability import ApplicabilityPolicy
from .model import (
    APPLICABILITY_HOLD,
    APPLICABILITY_NOT_APPLICABLE,
    APPLIES,
    SensingError,
    canonical,
    is_id,
    sha256_hex,
)

LEARNING_COMMITTED = "LEARNING_COMMITTED"
LEARNING_COMMIT_UNKNOWN = "LEARNING_COMMIT_UNKNOWN"
NOT_APPLICABLE = "NOT_APPLICABLE"
HOLD_APPLICABILITY = "HOLD_APPLICABILITY"
OUTCOMES = frozenset({"POSITIVE", "NEGATIVE", "UNKNOWN"})
ORIGINS = frozenset({"HUMAN", "MACHINE", "PROVIDER", "SYSTEM"})


class LearningConflict(SensingError):
    pass


@dataclass(frozen=True)
class LearningRecord:
    learning_key: str
    event_id: str
    owner_ref: str
    referent_type: str
    referent_id: str
    revision_after: str
    expected_sha256: str
    classifier_revision: str
    basis_fingerprint: str
    payload_fingerprint: str
    outcome: str
    origin: str
    evidence_refs: tuple[str, ...]
    way_home: tuple[str, ...]
    owner_receipt_ref: str
    owner_receipt_fingerprint: str
    provider_revision: int

    def semantic_payload(self) -> dict[str, Any]:
        """Fields that define learning identity/content.

        Receipt/provider evidence is intentionally excluded so an equivalent
        reread/receipt does not manufacture a second learning event.
        """
        return {
            "event_id": self.event_id,
            "owner_ref": self.owner_ref,
            "referent_type": self.referent_type,
            "referent_id": self.referent_id,
            "revision_after": self.revision_after,
            "expected_sha256": self.expected_sha256,
            "classifier_revision": self.classifier_revision,
            "outcome": self.outcome,
            "origin": self.origin,
            "evidence_refs": list(self.evidence_refs),
            "way_home": list(self.way_home),
        }

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["evidence_refs"] = list(self.evidence_refs)
        value["way_home"] = list(self.way_home)
        return value


@dataclass(frozen=True)
class LearningReceipt:
    learning_key: str
    record_fingerprint: str
    provider_revision: int
    readback_verified: bool
    idempotent_replay: bool
    pending_id: str


@dataclass(frozen=True)
class A7ProcessResult:
    disposition: str
    event_id: str
    applicability: str
    learning_receipt: LearningReceipt | None = None
    reason: str = ""


class ContextLearningSink(Protocol):
    """Injected owner of durable learning persistence/readback.

    Implementations are outside the guarded sensing runtime. A successful
    append must return a receipt only after durable append + fresh readback.
    """

    def append_and_readback(self, record: LearningRecord) -> LearningReceipt: ...


class A7LearningBridge:
    def __init__(
        self,
        *,
        receipt_reader: TrustedOwnerCommitReader,
        interests: ApplicabilityPolicy,
        sink: ContextLearningSink,
        classifier_revision: str,
    ) -> None:
        if not is_id(classifier_revision):
            raise SensingError("A7_CLASSIFIER_REVISION_INVALID")
        self._reader = receipt_reader
        self._interests = interests
        self._sink = sink
        self._classifier_revision = classifier_revision

    def process(
        self,
        *,
        receipt_ref: str,
        expected_owner_ref: str,
        outcome: str,
        origin: str,
        evidence_refs: Iterable[str],
    ) -> A7ProcessResult:
        trusted = read_trusted_owner_event(
            self._reader,
            receipt_ref=receipt_ref,
            expected_owner_ref=expected_owner_ref,
        )
        event = trusted.event
        applicability = self._interests.evaluate(
            event.owner_ref, event.referent_type, event.referent_id
        )
        if applicability == APPLICABILITY_NOT_APPLICABLE:
            return A7ProcessResult(
                NOT_APPLICABLE,
                event.event_id,
                APPLICABILITY_NOT_APPLICABLE,
                reason="LOCAL_NOT_APPLICABLE",
            )
        if applicability != APPLIES:
            return A7ProcessResult(
                HOLD_APPLICABILITY,
                event.event_id,
                APPLICABILITY_HOLD,
                reason="LOCAL_APPLICABILITY_UNDECIDABLE",
            )
        if outcome not in OUTCOMES:
            raise SensingError("A7_OUTCOME_INVALID")
        if origin not in ORIGINS:
            raise SensingError("A7_ORIGIN_INVALID")
        evidence = tuple(evidence_refs)
        if not evidence or not all(is_id(ref) for ref in evidence):
            raise SensingError("A7_EVIDENCE_REFS_INVALID")

        basis = {
            "event_id": event.event_id,
            "owner_ref": event.owner_ref,
            "referent_type": event.referent_type,
            "referent_id": event.referent_id,
            "revision_after": event.revision_after,
            "expected_sha256": event.expected_sha256,
            "classifier_revision": self._classifier_revision,
        }
        basis_fp = sha256_hex(canonical(basis))
        semantic = {
            **basis,
            "outcome": outcome,
            "origin": origin,
            "evidence_refs": list(evidence),
            "way_home": list(event.way_home),
        }
        payload_fp = sha256_hex(canonical(semantic))
        learning_key = sha256_hex(
            canonical(
                {
                    "event_id": event.event_id,
                    "owner_ref": event.owner_ref,
                    "revision_after": event.revision_after,
                    "classifier_revision": self._classifier_revision,
                }
            )
        )
        record = LearningRecord(
            learning_key=learning_key,
            event_id=event.event_id,
            owner_ref=event.owner_ref,
            referent_type=event.referent_type,
            referent_id=event.referent_id,
            revision_after=event.revision_after,
            expected_sha256=event.expected_sha256,
            classifier_revision=self._classifier_revision,
            basis_fingerprint=basis_fp,
            payload_fingerprint=payload_fp,
            outcome=outcome,
            origin=origin,
            evidence_refs=evidence,
            way_home=event.way_home,
            owner_receipt_ref=trusted.receipt_ref,
            owner_receipt_fingerprint=trusted.receipt_fingerprint,
            provider_revision=trusted.provider_revision,
        )
        # Once the persistence port is invoked, a malformed/missing result cannot
        # prove either durable commit or no-write. Fail closed as UNKNOWN_COMMIT.
        # A semantic conflict is already a typed, known outcome and keeps its
        # existing exception contract.
        try:
            receipt = self._sink.append_and_readback(record)
        except LearningConflict:
            raise
        except Exception:
            return A7ProcessResult(
                LEARNING_COMMIT_UNKNOWN,
                event.event_id,
                APPLIES,
                reason="CONTEXT_LEARNING_APPEND_OUTCOME_UNKNOWN",
            )

        invalid_reason = None
        if not isinstance(receipt, LearningReceipt):
            invalid_reason = "LEARNING_RECEIPT_MISSING_OR_WRONG_TYPE"
        elif receipt.readback_verified is not True:
            invalid_reason = "LEARNING_RECEIPT_READBACK_UNVERIFIED"
        elif receipt.learning_key != record.learning_key:
            invalid_reason = "LEARNING_RECEIPT_KEY_MISMATCH"
        elif receipt.record_fingerprint != record.payload_fingerprint:
            invalid_reason = "LEARNING_RECEIPT_FINGERPRINT_MISMATCH"
        elif not is_id(receipt.pending_id):
            invalid_reason = "LEARNING_RECEIPT_PENDING_ID_INVALID"

        if invalid_reason is not None:
            return A7ProcessResult(
                LEARNING_COMMIT_UNKNOWN,
                event.event_id,
                APPLIES,
                reason=invalid_reason,
            )

        return A7ProcessResult(
            LEARNING_COMMITTED,
            event.event_id,
            APPLIES,
            learning_receipt=receipt,
            reason="CONTEXT_LEARNING_APPENDED_AND_READ_BACK",
        )
