from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from a7_sqlite_learning_sink import SqliteContextLearningSink
from signalvev_sensing.a7_learning import (
    A7LearningBridge,
    LEARNING_COMMITTED,
    LEARNING_COMMIT_UNKNOWN,
    NOT_APPLICABLE,
    LearningConflict,
    LearningReceipt,
)
from signalvev_sensing.a7_owner_receipt import VerifiedOwnerCommitReceipt
from signalvev_sensing.applicability import Interest, InterestTable
from signalvev_sensing.model import SensingError


OWNER = "owner:alpha"
OTHER_OWNER = "owner:other"
SHA = "a" * 64
RECEIPT_SHA = "b" * 64


def event_payload(event_id: str = "event:1", *, owner: str = OWNER, seq: int = 1):
    return {
        "event_id": event_id,
        "owner_ref": owner,
        "source_ref": owner,
        "referent": {"type": "doc", "id": "artifact:alpha"},
        "owner_seq": seq,
        "revision_basis": {"after": f"rev:{seq}", "before": None if seq == 1 else f"rev:{seq - 1}"},
        "change_class": "SEMANTIC",
        "delta": {
            "kind": "INLINE",
            "expected_sha256": SHA,
            "fields": {"status": "READY"},
        },
        "way_home": ["way:owner", "way:context"],
    }


def receipt(
    receipt_ref: str = "receipt:1",
    *,
    event_id: str = "event:1",
    owner: str = OWNER,
    seq: int = 1,
    fingerprint: str = RECEIPT_SHA,
    provider_revision: int = 7,
    verified: bool = True,
    readback_verified: bool = True,
    payload=None,
):
    return VerifiedOwnerCommitReceipt(
        receipt_ref=receipt_ref,
        receipt_fingerprint=fingerprint,
        owner_ref=owner,
        provider_revision=provider_revision,
        readback_ref=f"readback:{seq}",
        observed_at="2026-10-03T00:00:00Z",
        event_payload=event_payload(event_id, owner=owner, seq=seq) if payload is None else payload,
        verified=verified,
        readback_verified=readback_verified,
    )


class FakeReceiptReader:
    def __init__(self, mapping):
        self.mapping = dict(mapping)
        self.calls = []

    def read_verified(self, receipt_ref: str, *, expected_owner_ref: str):
        self.calls.append((receipt_ref, expected_owner_ref))
        return self.mapping[receipt_ref]


class Encounter:
    def __init__(self, *, accept=True):
        self.accept = accept
        self.calls = []

    def encounter(self, record, *, idempotency_key: str) -> bool:
        self.calls.append((record, idempotency_key))
        return self.accept


class ReceiptProbeSink:
    """Port double for malformed/ambiguous persistence receipts."""

    def __init__(self, mode):
        self.mode = mode
        self.calls = []

    def append_and_readback(self, record):
        self.calls.append(record)
        if self.mode == "raise":
            raise OSError("injected after-port-invocation failure")
        if self.mode == "none":
            return None
        key = record.learning_key if self.mode != "wrong_key" else "0" * 64
        fingerprint = (
            record.payload_fingerprint if self.mode != "wrong_fingerprint" else "1" * 64
        )
        return LearningReceipt(
            learning_key=key,
            record_fingerprint=fingerprint,
            provider_revision=record.provider_revision,
            readback_verified=self.mode != "false_readback",
            idempotent_replay=False,
            pending_id="bad pending id" if self.mode == "invalid_pending" else "pending:valid",
        )


class A7LearningBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "learning.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def bridge(self, reader, *, relevant=True):
        interests = InterestTable([Interest(OWNER, "doc", None)] if relevant else [])
        sink = SqliteContextLearningSink(self.db)
        return A7LearningBridge(
            receipt_reader=reader,
            interests=interests,
            sink=sink,
            classifier_revision="a7-classifier:v1",
        ), sink

    def test_unverified_receipt_is_denied_before_learning(self):
        reader = FakeReceiptReader({"receipt:1": receipt(verified=False)})
        bridge, sink = self.bridge(reader)
        with self.assertRaisesRegex(SensingError, "OWNER_RECEIPT_NOT_VERIFIED"):
            bridge.process(
                receipt_ref="receipt:1",
                expected_owner_ref=OWNER,
                outcome="NEGATIVE",
                origin="HUMAN",
                evidence_refs=["pm:9310"],
            )
        self.assertEqual((sink.learning_count(), sink.pending_count()), (0, 0))
        sink.close()

    def test_committed_readback_string_in_event_payload_is_not_auth(self):
        payload = event_payload()
        payload["commit"] = {
            "state": "COMMITTED_READBACK",
            "readback_ref": "caller:self",
            "observed_at": "2026-10-03T00:00:00Z",
        }
        reader = FakeReceiptReader({"receipt:1": receipt(payload=payload)})
        bridge, sink = self.bridge(reader)
        with self.assertRaisesRegex(SensingError, "OWNER_RECEIPT_EVENT_MUST_NOT_SELF_ATTEST"):
            bridge.process(
                receipt_ref="receipt:1",
                expected_owner_ref=OWNER,
                outcome="NEGATIVE",
                origin="HUMAN",
                evidence_refs=["pm:9310"],
            )
        self.assertEqual(sink.learning_count(), 0)
        sink.close()

    def test_wrong_owner_receipt_is_denied(self):
        reader = FakeReceiptReader(
            {"receipt:1": receipt(owner=OTHER_OWNER, payload=event_payload(owner=OTHER_OWNER))}
        )
        bridge, sink = self.bridge(reader)
        with self.assertRaisesRegex(SensingError, "OWNER_RECEIPT_OWNER_MISMATCH"):
            bridge.process(
                receipt_ref="receipt:1",
                expected_owner_ref=OWNER,
                outcome="NEGATIVE",
                origin="HUMAN",
                evidence_refs=["pm:9310"],
            )
        self.assertEqual(sink.learning_count(), 0)
        sink.close()

    def test_same_event_and_basis_is_persisted_once(self):
        reader = FakeReceiptReader({"receipt:1": receipt()})
        bridge, sink = self.bridge(reader)
        first = bridge.process(
            receipt_ref="receipt:1",
            expected_owner_ref=OWNER,
            outcome="NEGATIVE",
            origin="HUMAN",
            evidence_refs=["pm:9310"],
        )
        second = bridge.process(
            receipt_ref="receipt:1",
            expected_owner_ref=OWNER,
            outcome="NEGATIVE",
            origin="HUMAN",
            evidence_refs=["pm:9310"],
        )
        self.assertEqual(first.disposition, LEARNING_COMMITTED)
        self.assertFalse(first.learning_receipt.idempotent_replay)
        self.assertTrue(second.learning_receipt.idempotent_replay)
        self.assertEqual(first.learning_receipt.learning_key, second.learning_receipt.learning_key)
        self.assertEqual((sink.learning_count(), sink.pending_count()), (1, 1))
        sink.close()

    def test_equivalent_owner_reread_receipt_does_not_duplicate_learning(self):
        reader = FakeReceiptReader(
            {
                "receipt:1": receipt(),
                "receipt:2": receipt(
                    "receipt:2",
                    fingerprint="c" * 64,
                    provider_revision=8,
                ),
            }
        )
        bridge, sink = self.bridge(reader)
        a = bridge.process(
            receipt_ref="receipt:1",
            expected_owner_ref=OWNER,
            outcome="NEGATIVE",
            origin="HUMAN",
            evidence_refs=["pm:9310"],
        )
        b = bridge.process(
            receipt_ref="receipt:2",
            expected_owner_ref=OWNER,
            outcome="NEGATIVE",
            origin="HUMAN",
            evidence_refs=["pm:9310"],
        )
        self.assertEqual(a.learning_receipt.learning_key, b.learning_receipt.learning_key)
        self.assertTrue(b.learning_receipt.idempotent_replay)
        self.assertEqual((sink.learning_count(), sink.pending_count()), (1, 1))
        sink.close()

    def test_same_event_basis_changed_semantic_payload_conflicts(self):
        reader = FakeReceiptReader({"receipt:1": receipt()})
        bridge, sink = self.bridge(reader)
        bridge.process(
            receipt_ref="receipt:1",
            expected_owner_ref=OWNER,
            outcome="NEGATIVE",
            origin="HUMAN",
            evidence_refs=["pm:9310"],
        )
        with self.assertRaisesRegex(LearningConflict, "A7_LEARNING_CONFLICT"):
            bridge.process(
                receipt_ref="receipt:1",
                expected_owner_ref=OWNER,
                outcome="POSITIVE",
                origin="HUMAN",
                evidence_refs=["pm:9310"],
            )
        self.assertEqual((sink.learning_count(), sink.pending_count()), (1, 1))
        sink.close()

    def test_not_applicable_writes_no_learning_or_pending(self):
        reader = FakeReceiptReader({"receipt:1": receipt()})
        bridge, sink = self.bridge(reader, relevant=False)
        result = bridge.process(
            receipt_ref="receipt:1",
            expected_owner_ref=OWNER,
            outcome="NEGATIVE",
            origin="HUMAN",
            evidence_refs=["pm:9310"],
        )
        self.assertEqual(result.disposition, NOT_APPLICABLE)
        self.assertIsNone(result.learning_receipt)
        self.assertEqual((sink.learning_count(), sink.pending_count()), (0, 0))
        sink.close()

    def test_crash_restart_keeps_pending_until_later_encounter_ack(self):
        reader = FakeReceiptReader({"receipt:1": receipt()})
        bridge, sink = self.bridge(reader)
        result = bridge.process(
            receipt_ref="receipt:1",
            expected_owner_ref=OWNER,
            outcome="NEGATIVE",
            origin="HUMAN",
            evidence_refs=["pm:9310", "kartotek:1462"],
        )
        key = result.learning_receipt.learning_key
        sink.close()

        reopened = SqliteContextLearningSink(self.db)
        self.assertEqual((reopened.learning_count(), reopened.pending_count()), (1, 1))
        consumer = Encounter(accept=True)
        self.assertEqual(reopened.dispatch_pending(consumer), 1)
        self.assertEqual(reopened.pending_count(), 0)
        self.assertEqual(len(consumer.calls), 1)
        record, idem = consumer.calls[0]
        self.assertEqual(idem, key)
        self.assertEqual((record.outcome, record.origin), ("NEGATIVE", "HUMAN"))
        self.assertEqual(record.evidence_refs, ("pm:9310", "kartotek:1462"))
        reopened.close()

    def test_failed_later_encounter_keeps_recoverable_pending_obligation(self):
        reader = FakeReceiptReader({"receipt:1": receipt()})
        bridge, sink = self.bridge(reader)
        bridge.process(
            receipt_ref="receipt:1",
            expected_owner_ref=OWNER,
            outcome="UNKNOWN",
            origin="PROVIDER",
            evidence_refs=["provider:event"],
        )
        consumer = Encounter(accept=False)
        self.assertEqual(sink.dispatch_pending(consumer), 0)
        self.assertEqual(sink.pending_count(), 1)
        sink.close()

    def test_replay_after_ack_does_not_recreate_encounter_obligation(self):
        reader = FakeReceiptReader({"receipt:1": receipt()})
        bridge, sink = self.bridge(reader)
        first = bridge.process(
            receipt_ref="receipt:1",
            expected_owner_ref=OWNER,
            outcome="NEGATIVE",
            origin="HUMAN",
            evidence_refs=["pm:9310"],
        )
        self.assertEqual(sink.dispatch_pending(Encounter(accept=True)), 1)
        self.assertEqual(sink.pending_count(), 0)

        replay = bridge.process(
            receipt_ref="receipt:1",
            expected_owner_ref=OWNER,
            outcome="NEGATIVE",
            origin="HUMAN",
            evidence_refs=["pm:9310"],
        )
        self.assertTrue(replay.learning_receipt.idempotent_replay)
        self.assertEqual(replay.learning_receipt.learning_key, first.learning_receipt.learning_key)
        self.assertEqual(sink.pending_count(), 0)
        sink.close()

    def _bridge_with_probe_sink(self, mode):
        reader = FakeReceiptReader({"receipt:1": receipt()})
        sink = ReceiptProbeSink(mode)
        bridge = A7LearningBridge(
            receipt_reader=reader,
            interests=InterestTable([Interest(OWNER, "doc", None)]),
            sink=sink,
            classifier_revision="a7-classifier:v1",
        )
        return bridge, sink

    def test_none_receipt_is_unknown_commit_not_committed(self):
        bridge, sink = self._bridge_with_probe_sink("none")
        result = bridge.process(
            receipt_ref="receipt:1",
            expected_owner_ref=OWNER,
            outcome="NEGATIVE",
            origin="HUMAN",
            evidence_refs=["pm:9310"],
        )
        self.assertEqual(result.disposition, LEARNING_COMMIT_UNKNOWN)
        self.assertIsNone(result.learning_receipt)
        self.assertEqual(result.reason, "LEARNING_RECEIPT_MISSING_OR_WRONG_TYPE")
        self.assertEqual(len(sink.calls), 1)

    def test_false_readback_receipt_is_unknown_commit(self):
        bridge, sink = self._bridge_with_probe_sink("false_readback")
        result = bridge.process(
            receipt_ref="receipt:1",
            expected_owner_ref=OWNER,
            outcome="NEGATIVE",
            origin="HUMAN",
            evidence_refs=["pm:9310"],
        )
        self.assertEqual(result.disposition, LEARNING_COMMIT_UNKNOWN)
        self.assertEqual(result.reason, "LEARNING_RECEIPT_READBACK_UNVERIFIED")
        self.assertEqual(len(sink.calls), 1)

    def test_wrong_learning_key_receipt_is_unknown_commit(self):
        bridge, _ = self._bridge_with_probe_sink("wrong_key")
        result = bridge.process(
            receipt_ref="receipt:1",
            expected_owner_ref=OWNER,
            outcome="NEGATIVE",
            origin="HUMAN",
            evidence_refs=["pm:9310"],
        )
        self.assertEqual(result.disposition, LEARNING_COMMIT_UNKNOWN)
        self.assertEqual(result.reason, "LEARNING_RECEIPT_KEY_MISMATCH")

    def test_wrong_fingerprint_receipt_is_unknown_commit(self):
        bridge, _ = self._bridge_with_probe_sink("wrong_fingerprint")
        result = bridge.process(
            receipt_ref="receipt:1",
            expected_owner_ref=OWNER,
            outcome="NEGATIVE",
            origin="HUMAN",
            evidence_refs=["pm:9310"],
        )
        self.assertEqual(result.disposition, LEARNING_COMMIT_UNKNOWN)
        self.assertEqual(result.reason, "LEARNING_RECEIPT_FINGERPRINT_MISMATCH")

    def test_invalid_pending_identity_receipt_is_unknown_commit(self):
        bridge, _ = self._bridge_with_probe_sink("invalid_pending")
        result = bridge.process(
            receipt_ref="receipt:1",
            expected_owner_ref=OWNER,
            outcome="NEGATIVE",
            origin="HUMAN",
            evidence_refs=["pm:9310"],
        )
        self.assertEqual(result.disposition, LEARNING_COMMIT_UNKNOWN)
        self.assertEqual(result.reason, "LEARNING_RECEIPT_PENDING_ID_INVALID")

    def test_sink_exception_after_invocation_is_unknown_commit(self):
        bridge, sink = self._bridge_with_probe_sink("raise")
        result = bridge.process(
            receipt_ref="receipt:1",
            expected_owner_ref=OWNER,
            outcome="NEGATIVE",
            origin="HUMAN",
            evidence_refs=["pm:9310"],
        )
        self.assertEqual(result.disposition, LEARNING_COMMIT_UNKNOWN)
        self.assertEqual(result.reason, "CONTEXT_LEARNING_APPEND_OUTCOME_UNKNOWN")
        self.assertEqual(len(sink.calls), 1)

    def test_positive_negative_unknown_and_human_origin_are_preserved(self):
        mapping = {
            "receipt:1": receipt("receipt:1", event_id="event:1", seq=1),
            "receipt:2": receipt("receipt:2", event_id="event:2", seq=2),
            "receipt:3": receipt("receipt:3", event_id="event:3", seq=3),
        }
        reader = FakeReceiptReader(mapping)
        bridge, sink = self.bridge(reader)
        for ref, outcome, origin in (
            ("receipt:1", "POSITIVE", "SYSTEM"),
            ("receipt:2", "NEGATIVE", "HUMAN"),
            ("receipt:3", "UNKNOWN", "PROVIDER"),
        ):
            bridge.process(
                receipt_ref=ref,
                expected_owner_ref=OWNER,
                outcome=outcome,
                origin=origin,
                evidence_refs=[f"evidence:{ref[-1]}"],
            )
        observed = {(p.record.event_id, p.record.outcome, p.record.origin) for p in sink.pending()}
        self.assertEqual(
            observed,
            {
                ("event:1", "POSITIVE", "SYSTEM"),
                ("event:2", "NEGATIVE", "HUMAN"),
                ("event:3", "UNKNOWN", "PROVIDER"),
            },
        )
        sink.close()


if __name__ == "__main__":
    unittest.main()
