"""Adversarial, offline tests for the default-off PM port consumer.

The fake below is *only* a test double. It is not an authenticated Sheets or
production PM reader and cannot make a live owner claim.
"""
from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "adapters"))
sys.path.insert(0, str(ROOT / "src"))

from pm_owner_commit import (  # noqa: E402
    PmCommittedReadyHint, PmCurrentRead, PmOwnerCommitReader,
    ReadyHintExpectation,
)
from signalvev_sensing.a7_owner_receipt import read_trusted_owner_event  # noqa: E402
from signalvev_sensing.d0 import d0_from_event  # noqa: E402
from signalvev_sensing.model import SensingError  # noqa: E402


OWNER = "CURRENT_PM"
RECEIPT = "pm-receipt:stable-7"
PACKET_SHA = "a" * 64
SNAPSHOT_SHA = "b" * 64
EXPECTED = ReadyHintExpectation(
    owner_ref=OWNER, claim_ref="WORK_CLAIMS:1849",
    packet_ref="WORK_PACKETS:2559", queue_ref="READY_QUEUE:863",
    packet_sha256=PACKET_SHA,
)


def snapshot(**changes):
    return replace(PmCommittedReadyHint(
        authenticated=True, committed=True, readback_verified=True,
        consistent_snapshot=True, owner_ref=OWNER, receipt_ref=RECEIPT,
        event_id="pm-event:ready-7", referent_id="pm-ready:claim-1849",
        owner_seq=7, revision_after="pm-revision:7",
        revision_before="pm-revision:6", provider_revision=7,
        claim_ref=EXPECTED.claim_ref, packet_ref=EXPECTED.packet_ref,
        queue_ref=EXPECTED.queue_ref,
        claim_revision="pm-revision:7", packet_revision="pm-revision:7",
        queue_revision="pm-revision:7", packet_sha256=PACKET_SHA,
        ready_state="MATERIAL_READY", snapshot_ref="pm-snapshot:ready-7",
        snapshot_sha256=SNAPSHOT_SHA, readback_ref="pm-readback:7",
        observed_at="2026-10-04T21:00:00Z",
        way_home=("pm-snapshot:ready-7", "pm-receipt:stable-7"),
    ), **changes)


class FakePort:
    def __init__(self, snap=None, current=None):
        self.snap = snap or snapshot()
        self.current = current or PmCurrentRead(
            authenticated=True, readback_verified=True, owner_ref=OWNER,
            referent_id=self.snap.referent_id,
            current_revision=self.snap.revision_after, relation="SAME",
            snapshot_sha256=self.snap.snapshot_sha256, owner_seq=self.snap.owner_seq,
        )
        self.calls = 0

    def read_committed_ready_hint(self, receipt_ref, *, expected_owner_ref):
        self.calls += 1
        return self.snap

    def reread_ready_hint(self, referent_id, *, expected_owner_ref,
                          expected_revision):
        self.calls += 1
        return self.current


class OfflinePort:
    def read_committed_ready_hint(self, receipt_ref, *, expected_owner_ref):
        raise ConnectionError("offline")


def reader(port=None, *, enabled=True):
    return PmOwnerCommitReader(EXPECTED, port=port, enabled=enabled)


def code(exc):
    return exc.exception.code


class PmOwnerCommitTests(unittest.TestCase):
    def test_default_off_and_missing_port_fail_before_any_read(self):
        p = FakePort()
        with self.assertRaises(SensingError) as cm:
            reader(p, enabled=False).read_verified(RECEIPT, expected_owner_ref=OWNER)
        self.assertEqual(code(cm), "PM_OWNER_PORT_UNBOUND")
        self.assertEqual(p.calls, 0)
        with self.assertRaises(SensingError) as cm:
            reader(None).read_verified(RECEIPT, expected_owner_ref=OWNER)
        self.assertEqual(code(cm), "PM_OWNER_PORT_UNBOUND")

    def test_valid_bound_test_port_builds_existing_pointer_event(self):
        p = FakePort()
        trusted = read_trusted_owner_event(
            reader(p), receipt_ref=RECEIPT, expected_owner_ref=OWNER,
        )
        d0 = d0_from_event(trusted.event)
        self.assertEqual(d0["delta"], {
            "kind": "POINTER", "ref": "pm-snapshot:ready-7",
            "expected_sha256": SNAPSHOT_SHA,
        })
        self.assertEqual(d0["owner_ref"], OWNER)
        self.assertEqual(d0["revision_after"], "pm-revision:7")
        self.assertEqual(p.calls, 2)
        self.assertNotIn("WORK_CONSUMED", str(d0))
        self.assertNotIn("ACK_READ", str(d0))

    def test_row_address_cannot_be_receipt_or_snapshot_identity(self):
        p = FakePort()
        with self.assertRaises(SensingError) as cm:
            reader(p).read_verified("PM10017", expected_owner_ref=OWNER)
        self.assertEqual(code(cm), "PM_RECEIPT_ID_UNSTABLE")
        self.assertEqual(p.calls, 0)
        with self.assertRaises(SensingError) as cm:
            reader(FakePort(snapshot(snapshot_ref="PM10017"))).read_verified(
                RECEIPT, expected_owner_ref=OWNER,
            )
        self.assertEqual(code(cm), "PM_OWNER_SNAPSHOT_INVALID")

    def test_moved_pm10017_row_fails_exact_owner_reread(self):
        p = FakePort()
        p.current = replace(p.current, current_revision="pm-revision:8",
                            relation="SUPERSEDED", snapshot_sha256="c" * 64)
        with self.assertRaises(SensingError) as cm:
            reader(p).read_verified(RECEIPT, expected_owner_ref=OWNER)
        self.assertEqual(code(cm), "PM_OWNER_SNAPSHOT_STALE_OR_UNKNOWN")

    def test_forged_or_revoked_owner_attestation_fails(self):
        for change in ({"authenticated": False}, {"committed": False},
                       {"readback_verified": False},
                       {"consistent_snapshot": False}):
            with self.subTest(change=change):
                with self.assertRaises(SensingError) as cm:
                    reader(FakePort(snapshot(**change))).read_verified(
                        RECEIPT, expected_owner_ref=OWNER,
                    )
                self.assertEqual(code(cm), "PM_OWNER_SNAPSHOT_UNVERIFIED")

    def test_claim_packet_queue_or_packet_hash_mismatch_fails(self):
        for change in ({"claim_ref": "WORK_CLAIMS:other"},
                       {"packet_ref": "WORK_PACKETS:other"},
                       {"queue_ref": "READY_QUEUE:other"},
                       {"packet_sha256": "c" * 64}):
            with self.subTest(change=change):
                with self.assertRaises(SensingError) as cm:
                    reader(FakePort(snapshot(**change))).read_verified(
                        RECEIPT, expected_owner_ref=OWNER,
                    )
                self.assertEqual(code(cm), "PM_OWNER_BINDING_MISMATCH")

    def test_split_row_revisions_fail_consistent_join(self):
        for field in ("claim_revision", "packet_revision", "queue_revision"):
            with self.subTest(field=field):
                with self.assertRaises(SensingError) as cm:
                    reader(FakePort(snapshot(**{field: "pm-revision:8"}))).read_verified(
                        RECEIPT, expected_owner_ref=OWNER,
                    )
                self.assertEqual(code(cm), "PM_OWNER_JOIN_REVISION_MISMATCH")

    def test_unreadable_and_unknown_revision_fail_closed(self):
        with self.assertRaises(SensingError) as cm:
            reader(OfflinePort()).read_verified(RECEIPT, expected_owner_ref=OWNER)
        self.assertEqual(code(cm), "PM_OWNER_PORT_UNAVAILABLE")
        p = FakePort()
        p.current = replace(p.current, relation="UNKNOWN")
        with self.assertRaises(SensingError) as cm:
            reader(p).read_verified(RECEIPT, expected_owner_ref=OWNER)
        self.assertEqual(code(cm), "PM_OWNER_SNAPSHOT_STALE_OR_UNKNOWN")

    def test_false_same_revision_digest_or_sequence_fails(self):
        for change in ({"current_revision": "pm-revision:8"},
                       {"snapshot_sha256": "c" * 64}, {"owner_seq": 8}):
            with self.subTest(change=change):
                p = FakePort()
                p.current = replace(p.current, **change)
                with self.assertRaises(SensingError) as cm:
                    reader(p).read_verified(RECEIPT, expected_owner_ref=OWNER)
                self.assertEqual(code(cm), "PM_REREAD_SAME_MISMATCH")

    def test_malformed_provider_types_fail_typed(self):
        for change in ({"snapshot_sha256": None}, {"ready_state": []}):
            with self.subTest(change=change):
                with self.assertRaises(SensingError) as cm:
                    reader(FakePort(snapshot(**change))).read_verified(
                        RECEIPT, expected_owner_ref=OWNER,
                    )
                self.assertEqual(code(cm), "PM_OWNER_SNAPSHOT_INVALID")
        p = FakePort()
        p.current = replace(p.current, relation=[])
        with self.assertRaises(SensingError) as cm:
            reader(p).read_verified(RECEIPT, expected_owner_ref=OWNER)
        self.assertEqual(code(cm), "PM_REREAD_INVALID")

    def test_duplicate_event_same_content_idempotent_changed_content_conflicts(self):
        p = FakePort()
        adapter = reader(p)
        first = adapter.read_verified(RECEIPT, expected_owner_ref=OWNER)
        self.assertEqual(first.receipt_fingerprint,
                         adapter.read_verified(RECEIPT, expected_owner_ref=OWNER).receipt_fingerprint)
        p.snap = replace(p.snap, ready_state="BIND_AVAILABLE")
        with self.assertRaises(SensingError) as cm:
            adapter.read_verified(RECEIPT, expected_owner_ref=OWNER)
        self.assertEqual(code(cm), "PM_EVENT_ID_CONTENT_CONFLICT")

    def test_wrong_owner_or_unauthenticated_reread_fails(self):
        p = FakePort()
        with self.assertRaises(SensingError) as cm:
            reader(p).read_verified(RECEIPT, expected_owner_ref="OTHER_PM")
        self.assertEqual(code(cm), "PM_OWNER_MISMATCH")
        self.assertEqual(p.calls, 0)
        p.current = replace(p.current, authenticated=False)
        with self.assertRaises(SensingError) as cm:
            reader(p).read_verified(RECEIPT, expected_owner_ref=OWNER)
        self.assertEqual(code(cm), "PM_REREAD_UNVERIFIED")

    def test_later_reread_preserves_owner_judgment(self):
        p = FakePort()
        adapter = reader(p)
        p.current = replace(p.current, relation="SUPERSEDED",
                            current_revision="pm-revision:8")
        self.assertEqual(adapter.reread_current(
            p.snap.referent_id, p.snap.revision_after,
            expected_sha256=SNAPSHOT_SHA, expected_seq=7,
        ).relation, "SUPERSEDED")

    # ---- BK07: optional projection on PmCurrentRead (required by the PM->X9 bridge)
    def _projected(self, **changes):
        p = FakePort()
        p.current = replace(p.current, claim_ref=EXPECTED.claim_ref, packet_ref=EXPECTED.packet_ref,
                            queue_ref=EXPECTED.queue_ref, packet_sha256=PACKET_SHA, ready_state="MATERIAL_READY",
                            material_sha256="d" * 64, source_cut="pm-cut:7", consistent_snapshot=True)
        p.current = replace(p.current, **changes)
        return p

    def _reread(self, p, *, require=True):
        return reader(p).reread_current(p.snap.referent_id, p.snap.revision_after, expected_sha256=SNAPSHOT_SHA,
                                        expected_seq=7, require_projection=require)

    def test_bk07_projection_is_validated_and_carried(self):
        cur = self._reread(self._projected())
        self.assertEqual((cur.claim_ref, cur.source_cut, cur.material_sha256), (EXPECTED.claim_ref, "pm-cut:7", "d" * 64))
        # legacy callers without projection keep working; the bridge's require_projection refuses it
        self.assertEqual(self._reread(FakePort(), require=False).relation, "SAME")
        with self.assertRaises(SensingError) as cm:
            self._reread(FakePort())
        self.assertEqual(code(cm), "PM_REREAD_PROJECTION_MISSING")

    def test_bk07_projection_negatives(self):
        cases = (({"consistent_snapshot": False}, "PM_REREAD_SPLIT_SNAPSHOT"),
                 ({"source_cut": None}, "PM_REREAD_PROJECTION_MISSING"),
                 ({"ready_state": "LIVE"}, "PM_REREAD_PROJECTION_INVALID"),
                 ({"material_sha256": "x"}, "PM_REREAD_PROJECTION_INVALID"),
                 ({"claim_ref": "WORK_CLAIMS:other"}, "PM_REREAD_COORDINATE_MISMATCH"),
                 ({"queue_ref": "READY_QUEUE:other"}, "PM_REREAD_COORDINATE_MISMATCH"))
        for change, expected in cases:
            with self.subTest(change=change):
                with self.assertRaises(SensingError) as cm:
                    self._reread(self._projected(**change), require=False)
                self.assertEqual(code(cm), expected)


if __name__ == "__main__":
    unittest.main()
