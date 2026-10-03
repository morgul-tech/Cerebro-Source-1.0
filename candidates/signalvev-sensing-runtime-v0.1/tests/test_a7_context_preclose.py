from __future__ import annotations

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
CANDIDATE = TESTS.parent
sys.path.insert(0, str(CANDIDATE / "adapters"))

from a7_context_preclose import (  # noqa: E402
    APPLIED,
    DEFER_BUSY,
    HOLD_STALE,
    HOLD_TARGET_SCOPE_UNVERIFIED,
    HOLD_OWNER_UNAVAILABLE_OR_STALE,
    OPEN,
    CONSUMED,
    REVOKED,
    NO_CHANGE,
    NO_PENDING,
    NOT_APPLICABLE,
    POINTER_READY,
    ActorDispositionReceipt,
    ConsumerExpectation,
    ConsumerReadback,
    CurrentObligationReadback,
    OwnerAppendReceipt,
    OwnerContextLearningSink,
    OwnerLearningReadback,
    PendingAckReadback,
    PendingLearningReadback,
    apply_actor_disposition,
    observe_transport_ack,
    prepare_pre_close_projection,
)
from signalvev_sensing.a7_learning import (  # noqa: E402
    A7LearningBridge,
    LEARNING_COMMITTED,
    LEARNING_COMMIT_UNKNOWN,
)
from signalvev_sensing.a7_owner_receipt import (  # noqa: E402
    VerifiedOwnerCommitReceipt,
)
from signalvev_sensing.applicability import Interest, InterestTable  # noqa: E402


OWNER = "owner:pm"
ACTOR = "actor:r1"
GEN = "generation:r1"
ROLE = "RESEARCHER"
OBJECTIVE = "objective:next-question"
SCOPE = "scope:p22"
INTEREST = "interest:terminal-next-question"
VISIBILITY = "visibility:scope-p22"
PENDING = "pending:c1001"
OBLIGATION = "obligation:c1001-next-question"
PROVENANCE = "provenance:pm9455-pm9457"
BINDING = "binding:r1-c1001"
OWNER_HEAD = "owner-head:10"
OWNER_REV = 10
OWNER_HEAD_REV = 10
OWNER_FENCE = 10
SHA = "a" * 64
RECEIPT_SHA = "b" * 64


def owner_event_payload():
    return {
        "event_id": "event:c1001",
        "owner_ref": OWNER,
        "source_ref": OWNER,
        "referent": {"type": "pm-terminal", "id": "terminal:pm9455"},
        "owner_seq": 1,
        "revision_basis": {"after": "rev:pm9457", "before": None},
        "change_class": "SEMANTIC",
        "delta": {
            "kind": "INLINE",
            "expected_sha256": SHA,
            "fields": {
                "machine_attempt_state": "UNKNOWN_SEND",
                "human_wake": True,
            },
        },
        "way_home": ["pm:9457", "p22:9459"],
    }


class FakeReceiptReader:
    def read_verified(self, receipt_ref: str, *, expected_owner_ref: str):
        return VerifiedOwnerCommitReceipt(
            receipt_ref=receipt_ref,
            receipt_fingerprint=RECEIPT_SHA,
            owner_ref=expected_owner_ref,
            provider_revision=7,
            readback_ref="readback:owner",
            observed_at="2026-10-03T00:00:00Z",
            event_payload=owner_event_payload(),
            verified=True,
            readback_verified=True,
        )


class FakeContextOwnerPort:
    def __init__(self):
        self.records = {}
        self.pending = {}
        self.consumer = ConsumerReadback(
            actor_ref=ACTOR,
            generation_ref=GEN,
            role=ROLE,
            objective_ref=OBJECTIVE,
            scope_ref=SCOPE,
            interest_ref=INTEREST,
            owner_revision=OWNER_REV,
            visibility_ref=VISIBILITY,
            binding_ref=BINDING,
            pointer_visible=True,
            current=True,
        )
        self.obligations = {}
        self.dispositions = {}
        self.acks = []
        self.false_learning_readback = False
        self.false_append_receipt = False
        self.raise_on_append = False

    def append_learning(self, record, *, machine_liveness_credit: bool):
        if self.raise_on_append:
            raise OSError("injected owner append failure")
        duplicate = record.learning_key in self.records
        pending_id = (
            self.records[record.learning_key]["pending_id"]
            if duplicate
            else PENDING
        )
        if not duplicate:
            self.records[record.learning_key] = {
                "record": record,
                "pending_id": pending_id,
                "revision": 1,
            }
            self.pending[pending_id] = PendingLearningReadback(
                learning_key=record.learning_key,
                record_fingerprint=record.payload_fingerprint,
                pending_id=pending_id,
                owner_revision=OWNER_REV,
                origin=record.origin,
                machine_attempt_state="UNKNOWN_SEND",
                machine_liveness_credit=machine_liveness_credit,
                scope_ref=SCOPE,
                interest_ref=INTEREST,
                visibility_ref=VISIBILITY,
                obligation_id=OBLIGATION,
                provenance_ref=PROVENANCE,
                binding_ref=BINDING,
                owner_head_ref=OWNER_HEAD,
                owner_head_revision=OWNER_HEAD_REV,
                owner_fence=OWNER_FENCE,
                current=True,
            )
            self.obligations[OBLIGATION] = CurrentObligationReadback(
                obligation_id=OBLIGATION,
                provenance_ref=PROVENANCE,
                status=OPEN,
                actor_ref=ACTOR,
                generation_ref=GEN,
                scope_ref=SCOPE,
                binding_ref=BINDING,
                owner_head_ref=OWNER_HEAD,
                owner_head_revision=OWNER_HEAD_REV,
                owner_revision=OWNER_REV,
                owner_fence=OWNER_FENCE,
                disposition_ref=None,
                tombstone_ref=None,
                readback_ref="obligation-readback:10",
                readback_verified=True,
            )
        return OwnerAppendReceipt(
            learning_key=record.learning_key,
            record_fingerprint=(
                "0" * 64 if self.false_append_receipt else record.payload_fingerprint
            ),
            pending_id=pending_id,
            owner_revision=OWNER_REV,
            receipt_ref="owner-receipt:1",
            committed=True,
            duplicate=duplicate,
            machine_liveness_credit=machine_liveness_credit,
        )

    def read_learning(self, learning_key: str):
        row = self.records[learning_key]
        record = row["record"]
        return OwnerLearningReadback(
            learning_key=learning_key,
            record_fingerprint=record.payload_fingerprint,
            pending_id=row["pending_id"],
            owner_revision=OWNER_REV,
            readback_ref="owner-readback:1",
            readback_verified=not self.false_learning_readback,
            machine_liveness_credit=False,
        )

    def read_pending(self, pending_id: str):
        return self.pending.get(pending_id)

    def read_current_obligation(self, obligation_id: str):
        return self.obligations.get(obligation_id)

    def read_consumer(self, actor_ref: str, generation_ref: str):
        return self.consumer

    def read_disposition(self, receipt_ref: str):
        return self.dispositions.get(receipt_ref)

    def ack_pending(self, pending_id: str, disposition_receipt_ref: str):
        self.acks.append((pending_id, disposition_receipt_ref))
        return PendingAckReadback(
            pending_id=pending_id,
            disposition_receipt_ref=disposition_receipt_ref,
            acked=True,
            readback_verified=True,
        )


def pulse():
    return {
        "stage": "PRE_CLOSE",
        "currentness": "CURRENT",
        "persistence_readback_verified": True,
    }


def expected(**overrides):
    values = dict(
        actor_ref=ACTOR,
        generation_ref=GEN,
        role=ROLE,
        objective_ref=OBJECTIVE,
        scope_ref=SCOPE,
        interest_ref=INTEREST,
        owner_revision=OWNER_REV,
        visibility_ref=VISIBILITY,
        binding_ref=BINDING,
    )
    values.update(overrides)
    return ConsumerExpectation(**values)


def build_learning(port: FakeContextOwnerPort):
    bridge = A7LearningBridge(
        receipt_reader=FakeReceiptReader(),
        interests=InterestTable([Interest(OWNER, "pm-terminal", None)]),
        sink=OwnerContextLearningSink(port),
        classifier_revision="a7-preclose:v1",
    )
    return bridge.process(
        receipt_ref="receipt:c1001",
        expected_owner_ref=OWNER,
        outcome="UNKNOWN",
        origin="HUMAN",
        evidence_refs=["pm:9451", "pm:9454", "pm:9455", "pm:9457", "pros:690"],
    )


def project(port: FakeContextOwnerPort, exp=None):
    return prepare_pre_close_projection(
        port,
        pulse=pulse(),
        pending_id=PENDING,
        expected_consumer=exp or expected(),
    )


class A7ContextPreCloseTests(unittest.TestCase):
    def test_owner_append_readback_required_before_learning_committed(self):
        port = FakeContextOwnerPort()
        result = build_learning(port)
        self.assertEqual(result.disposition, LEARNING_COMMITTED)
        self.assertTrue(result.learning_receipt.readback_verified)
        self.assertEqual(result.learning_receipt.pending_id, PENDING)
        self.assertFalse(port.pending[PENDING].machine_liveness_credit)

    def test_false_owner_readback_is_unknown_not_learning_committed(self):
        port = FakeContextOwnerPort()
        port.false_learning_readback = True
        result = build_learning(port)
        self.assertEqual(result.disposition, LEARNING_COMMIT_UNKNOWN)
        self.assertIsNone(result.learning_receipt)

    def test_false_owner_append_receipt_is_unknown_not_learning_committed(self):
        port = FakeContextOwnerPort()
        port.false_append_receipt = True
        result = build_learning(port)
        self.assertEqual(result.disposition, LEARNING_COMMIT_UNKNOWN)
        self.assertIsNone(result.learning_receipt)

    def test_duplicate_owner_append_reuses_pending_identity(self):
        port = FakeContextOwnerPort()
        first = build_learning(port)
        second = build_learning(port)
        self.assertEqual(first.learning_receipt.pending_id, second.learning_receipt.pending_id)
        self.assertTrue(second.learning_receipt.idempotent_replay)
        self.assertEqual(len(port.pending), 1)

    def test_missing_current_obligation_holds_zero_emission(self):
        port = FakeContextOwnerPort()
        build_learning(port)
        port.obligations.clear()
        res = project(port)
        self.assertEqual(res.result, HOLD_OWNER_UNAVAILABLE_OR_STALE)
        self.assertFalse(res.emitted)
        self.assertIsNone(res.pointer)
        self.assertEqual(port.acks, [])

    def test_unknown_current_obligation_status_holds_zero_emission(self):
        port = FakeContextOwnerPort()
        build_learning(port)
        current = port.obligations[OBLIGATION]
        port.obligations[OBLIGATION] = CurrentObligationReadback(
            **{**current.__dict__, "status": "UNKNOWN"}
        )
        res = project(port)
        self.assertEqual(res.result, HOLD_OWNER_UNAVAILABLE_OR_STALE)
        self.assertFalse(res.emitted)

    def test_owner_revision_or_fence_behind_pending_holds(self):
        port = FakeContextOwnerPort()
        build_learning(port)
        current = port.obligations[OBLIGATION]
        port.obligations[OBLIGATION] = CurrentObligationReadback(
            **{
                **current.__dict__,
                "owner_revision": OWNER_REV - 1,
                "owner_head_revision": OWNER_HEAD_REV - 1,
                "owner_fence": OWNER_FENCE - 1,
            }
        )
        res = project(port)
        self.assertEqual(res.result, HOLD_OWNER_UNAVAILABLE_OR_STALE)
        self.assertFalse(res.emitted)

    def test_owner_provenance_mismatch_holds(self):
        port = FakeContextOwnerPort()
        build_learning(port)
        current = port.obligations[OBLIGATION]
        port.obligations[OBLIGATION] = CurrentObligationReadback(
            **{**current.__dict__, "provenance_ref": "provenance:other"}
        )
        res = project(port)
        self.assertEqual(res.result, HOLD_OWNER_UNAVAILABLE_OR_STALE)
        self.assertFalse(res.emitted)

    def test_identical_restored_rev10_histories_diverge_only_on_fresh_owner_readback(self):
        open_port = FakeContextOwnerPort()
        consumed_port = FakeContextOwnerPort()
        build_learning(open_port)
        build_learning(consumed_port)

        # Local restored history is byte-equivalent at rev10.
        self.assertEqual(open_port.pending[PENDING], consumed_port.pending[PENDING])
        self.assertTrue(consumed_port.pending[PENDING].current)
        consumed_port.acks.append((PENDING, "old-ack:rev10"))

        consumed_port.obligations[OBLIGATION] = CurrentObligationReadback(
            obligation_id=OBLIGATION,
            provenance_ref=PROVENANCE,
            status=CONSUMED,
            actor_ref=ACTOR,
            generation_ref="generation:r1-rev12",
            scope_ref=SCOPE,
            binding_ref="binding:r1-rev12",
            owner_head_ref="owner-head:11",
            owner_head_revision=11,
            owner_revision=11,
            owner_fence=11,
            disposition_ref="disposition:rev11",
            tombstone_ref="tombstone:rev11",
            readback_ref="obligation-readback:12",
            readback_verified=True,
        )

        open_result = project(open_port)
        consumed_result = project(consumed_port)
        self.assertEqual(open_result.result, POINTER_READY)
        self.assertTrue(open_result.emitted)
        self.assertEqual(consumed_result.result, HOLD_OWNER_UNAVAILABLE_OR_STALE)
        self.assertFalse(consumed_result.emitted)
        self.assertIsNone(consumed_result.pointer)

    def test_local_current_true_or_old_ack_never_infers_open(self):
        port = FakeContextOwnerPort()
        build_learning(port)
        self.assertTrue(port.pending[PENDING].current)
        port.acks.append((PENDING, "old-ack:rev10"))
        current = port.obligations[OBLIGATION]
        port.obligations[OBLIGATION] = CurrentObligationReadback(
            **{
                **current.__dict__,
                "status": REVOKED,
                "owner_revision": 11,
                "owner_head_revision": 11,
                "owner_head_ref": "owner-head:11",
                "owner_fence": 11,
                "disposition_ref": "disposition:revoke11",
                "tombstone_ref": "tombstone:revoke11",
            }
        )
        res = project(port)
        self.assertEqual(res.result, HOLD_OWNER_UNAVAILABLE_OR_STALE)
        self.assertFalse(res.emitted)

    def test_wrong_generation_holds_before_pointer_emission(self):
        port = FakeContextOwnerPort()
        build_learning(port)
        port.consumer = ConsumerReadback(
            **{**port.consumer.__dict__, "generation_ref": "generation:stale"}
        )
        res = project(port)
        self.assertEqual(res.result, HOLD_TARGET_SCOPE_UNVERIFIED)
        self.assertFalse(res.emitted)
        self.assertIsNone(res.pointer)

    def test_cross_scope_privacy_holds_before_pointer_emission(self):
        port = FakeContextOwnerPort()
        build_learning(port)
        port.consumer = ConsumerReadback(
            **{**port.consumer.__dict__, "scope_ref": "scope:other"}
        )
        res = project(port)
        self.assertEqual(res.result, HOLD_TARGET_SCOPE_UNVERIFIED)
        self.assertFalse(res.emitted)
        self.assertIsNone(res.pointer)

    def test_interest_or_visibility_mismatch_holds_before_emission(self):
        port = FakeContextOwnerPort()
        build_learning(port)
        port.consumer = ConsumerReadback(
            **{**port.consumer.__dict__, "interest_ref": "interest:other"}
        )
        res = project(port)
        self.assertEqual(res.result, HOLD_TARGET_SCOPE_UNVERIFIED)
        self.assertFalse(res.emitted)

    def test_exact_current_consumer_emits_minimal_human_origin_pointer(self):
        port = FakeContextOwnerPort()
        build_learning(port)
        res = project(port)
        self.assertEqual(res.result, POINTER_READY)
        self.assertTrue(res.emitted)
        self.assertEqual(res.pointer.origin, "HUMAN")
        self.assertEqual(res.pointer.machine_attempt_state, "UNKNOWN_SEND")
        self.assertFalse(res.pointer.machine_liveness_credit)
        self.assertEqual(res.pointer.authority, "NONE")

    def test_pros690_advisory_alone_is_not_pending_learning(self):
        port = FakeContextOwnerPort()
        res = project(port)
        self.assertEqual(res.result, NO_PENDING)
        self.assertFalse(res.emitted)
        self.assertIsNone(res.pointer)

    def test_transport_ack_never_consumes_or_acks_pending(self):
        port = FakeContextOwnerPort()
        build_learning(port)
        ptr = project(port).pointer
        obs = observe_transport_ack(delivered=True)
        self.assertTrue(obs.delivered)
        self.assertFalse(obs.consumed)
        self.assertFalse(obs.effect)
        self.assertEqual(port.acks, [])
        self.assertIn(ptr.pending_id, port.pending)

    def _disposition(self, port, disposition, *, generation=GEN):
        ptr = project(port).pointer
        ref = "disposition:1"
        port.dispositions[ref] = ActorDispositionReceipt(
            receipt_ref=ref,
            projection_id=ptr.pointer_id,
            learning_key=ptr.learning_key,
            pending_id=ptr.pending_id,
            actor_ref=ACTOR,
            generation_ref=generation,
            owner_revision=ptr.owner_revision,
            disposition=disposition,
            currentness_readback_verified=True,
        )
        return ptr, apply_actor_disposition(
            port,
            pointer=ptr,
            expected_consumer=expected(),
            disposition_receipt_ref=ref,
        )

    def test_applied_disposition_consumes_after_exact_readback(self):
        port = FakeContextOwnerPort()
        build_learning(port)
        _, result = self._disposition(port, APPLIED)
        self.assertEqual(result.disposition, APPLIED)
        self.assertTrue(result.consumed)
        self.assertTrue(result.pending_acked)
        self.assertEqual(len(port.acks), 1)

    def test_no_change_is_valid_zero_work_consumed_disposition(self):
        port = FakeContextOwnerPort()
        build_learning(port)
        _, result = self._disposition(port, NO_CHANGE)
        self.assertTrue(result.consumed)
        self.assertTrue(result.pending_acked)
        self.assertEqual(result.disposition, NO_CHANGE)

    def test_not_applicable_is_valid_zero_work_consumed_disposition(self):
        port = FakeContextOwnerPort()
        build_learning(port)
        _, result = self._disposition(port, NOT_APPLICABLE)
        self.assertTrue(result.consumed)
        self.assertTrue(result.pending_acked)
        self.assertEqual(result.disposition, NOT_APPLICABLE)

    def test_hold_stale_keeps_pending_for_reroute(self):
        port = FakeContextOwnerPort()
        build_learning(port)
        _, result = self._disposition(port, HOLD_STALE)
        self.assertFalse(result.consumed)
        self.assertFalse(result.pending_acked)
        self.assertEqual(port.acks, [])

    def test_defer_busy_keeps_pending_for_later_encounter(self):
        port = FakeContextOwnerPort()
        build_learning(port)
        _, result = self._disposition(port, DEFER_BUSY)
        self.assertFalse(result.consumed)
        self.assertFalse(result.pending_acked)
        self.assertEqual(port.acks, [])

    def test_wrong_disposition_generation_cannot_consume(self):
        port = FakeContextOwnerPort()
        build_learning(port)
        _, result = self._disposition(
            port, APPLIED, generation="generation:stale"
        )
        self.assertEqual(result.disposition, HOLD_STALE)
        self.assertFalse(result.consumed)
        self.assertEqual(port.acks, [])

    def test_no_encounter_or_disposition_leaves_pending_unconsumed(self):
        port = FakeContextOwnerPort()
        build_learning(port)
        ptr = project(port).pointer
        self.assertIsNotNone(ptr)
        self.assertEqual(port.acks, [])
        self.assertIn(PENDING, port.pending)

    def test_unverified_preclose_pulse_never_emits_pointer(self):
        port = FakeContextOwnerPort()
        build_learning(port)
        bad = pulse()
        bad["persistence_readback_verified"] = False
        res = prepare_pre_close_projection(
            port,
            pulse=bad,
            pending_id=PENDING,
            expected_consumer=expected(),
        )
        self.assertEqual(res.result, HOLD_TARGET_SCOPE_UNVERIFIED)
        self.assertFalse(res.emitted)


if __name__ == "__main__":
    unittest.main()


from a7_context_preclose import (  # noqa: E402
    ELIGIBLE,
    HOLD_CURRENT_COLLISION,
    CurrentActorWork,
    HistoricalClaimResidue,
    assess_task_eligibility,
)


class A7TaskEligibilityCanaryTests(unittest.TestCase):
    def test_pm9480_historical_retained_claim_is_not_current_collision(self):
        result = assess_task_eligibility(
            actor_ref="actor:c4",
            generation_ref="generation:c4-current",
            carrier_ref="carrier:c4-current",
            current_work=(),
            historical_residue=(
                HistoricalClaimResidue(claim_ref="C817", released=False),
            ),
        )
        self.assertEqual(result.result, ELIGIBLE)
        self.assertTrue(result.eligible)
        self.assertIsNone(result.blocking_claim_ref)

    def test_exact_current_actor_generation_carrier_work_blocks(self):
        result = assess_task_eligibility(
            actor_ref="actor:c4",
            generation_ref="generation:c4-current",
            carrier_ref="carrier:c4-current",
            current_work=(
                CurrentActorWork(
                    actor_ref="actor:c4",
                    generation_ref="generation:c4-current",
                    carrier_ref="carrier:c4-current",
                    claim_ref="C-current",
                    active=True,
                    current_verified=True,
                ),
            ),
            historical_residue=(
                HistoricalClaimResidue(claim_ref="C817", released=False),
            ),
        )
        self.assertEqual(result.result, HOLD_CURRENT_COLLISION)
        self.assertFalse(result.eligible)
        self.assertEqual(result.blocking_claim_ref, "C-current")

    def test_old_generation_or_unverified_activity_cannot_be_promoted_to_collision(self):
        result = assess_task_eligibility(
            actor_ref="actor:c4",
            generation_ref="generation:c4-current",
            carrier_ref="carrier:c4-current",
            current_work=(
                CurrentActorWork(
                    actor_ref="actor:c4",
                    generation_ref="generation:c4-old",
                    carrier_ref="carrier:c4-old",
                    claim_ref="C-old",
                    active=True,
                    current_verified=True,
                ),
                CurrentActorWork(
                    actor_ref="actor:c4",
                    generation_ref="generation:c4-current",
                    carrier_ref="carrier:c4-current",
                    claim_ref="C-unverified",
                    active=True,
                    current_verified=False,
                ),
            ),
        )
        self.assertEqual(result.result, ELIGIBLE)
        self.assertTrue(result.eligible)
