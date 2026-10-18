"""C1175 new projection risk checks only; all auth/store data are offline doubles."""
import sys
import unittest
from dataclasses import replace, asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from test_pm_owner_read_provider import record as old_record, OWNER, AUDIENCE
from providers.pm_owner_read import PmProviderError, PmReadyRecord, AuthenticatedPmPrincipal
from providers.pm_owner_projection import (AtomicProjection, PmProjectionBinding,
    PmOwnerProjectionAdapter, create_pm_source_port, projection_sha256)
from adapters.pm_owner_commit import ReadyHintExpectation, PmOwnerCommitReader


def record(**kwargs):
    return PmReadyRecord(**asdict(old_record(**kwargs)))


class Credentials:
    SYNTHETIC_TEST_ONLY = True
    def authenticate_and_authorize(self, credential, *, owner_ref, audience, action):
        if credential != "synthetic-allowed":
            return None
        return AuthenticatedPmPrincipal("principal:reader", owner_ref, audience)


class AtomicStore:
    SYNTHETIC_TEST_ONLY = True
    def __init__(self):
        self.receipt = self.current = self.cut(record())
        self.calls = []
    def cut(self, r):
        return AtomicProjection(r, "b" * 64, "owner-cut:durable-7", None,
                                "provider:owner", "principal:reader", "session:bound")
    def read_receipt(self, ref, **identity):
        self.calls.append(("receipt", identity))
        return self.receipt
    def read_current(self, ref, **identity):
        self.calls.append(("current", identity))
        return self.current


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.store, self.auth = AtomicStore(), Credentials()
        r = self.store.receipt.record
        self.expect = ReadyHintExpectation(OWNER, r.claim_ref, r.packet_ref,
                                          r.queue_ref, r.packet_sha256)
        self.binding = PmProjectionBinding(self.expect, r.receipt_ref, AUDIENCE,
            "provider:owner", "principal:reader", "session:bound",
            self.auth, self.store, lambda: "synthetic-allowed", True)
        self.port = PmOwnerProjectionAdapter(self.binding)

    def test_default_off_factory_and_fixture_refuse_before_source_io(self):
        off = PmOwnerProjectionAdapter(replace(self.binding, enabled=False))
        with self.assertRaisesRegex(PmProviderError, "DEFAULT_OFF"):
            off.read_committed_ready_hint("pm-receipt:1", expected_owner_ref=OWNER)
        with self.assertRaisesRegex(PmProviderError, "SYNTHETIC"):
            create_pm_source_port(self.binding)
        self.assertEqual(self.store.calls, [])

    def test_exact_projection_composes_existing_client_and_is_restart_stable(self):
        digest = projection_sha256(self.store.receipt)
        for port in (self.port, PmOwnerProjectionAdapter(self.binding)):
            reader = PmOwnerCommitReader(self.expect, port=port, enabled=True)
            receipt = reader.read_verified("pm-receipt:1", expected_owner_ref=OWNER)
            current = reader.reread_current("pm-ready:claim1", "rev:1",
                expected_sha256=digest, expected_seq=1, require_projection=True)
            self.assertEqual(receipt.event_payload["event_id"], "pm-event:1")
            self.assertEqual(current.snapshot_sha256, digest)
            self.assertEqual(current.material_sha256, "b" * 64)
            self.assertNotEqual(current.material_sha256, current.packet_sha256)
            self.assertEqual(current.source_cut, "owner-cut:durable-7")
            self.assertEqual(current.relation, "SAME")

    def test_source_current_superseded_stale_sender_and_missed_d0(self):
        self.store.current = self.store.cut(record(seq=2, revision="rev:2", event="pm-event:2"))
        with self.assertRaisesRegex(PmProviderError, "STALE"):
            self.port.read_committed_ready_hint("pm-receipt:1", expected_owner_ref=OWNER)
        current = self.port.reread_ready_hint("pm-ready:claim1",
            expected_owner_ref=OWNER, expected_revision="rev:1")
        self.assertEqual((current.relation, current.owner_seq), ("SUPERSEDED", 2))
        self.assertEqual(self.port.recover_current_ready_hint(
            "pm-ready:claim1", after_seq=1).record.event_id, "pm-event:2")
        self.assertIsNone(self.port.recover_current_ready_hint("pm-ready:claim1", after_seq=2))

    def test_mixed_revision_and_forged_snapshot_refused(self):
        original = self.store.current
        for r in (replace(original.record, queue_revision="rev:torn"),
                  replace(original.record, packet_sha256="c" * 64)):
            self.store.current = replace(original, record=r)
            with self.assertRaises(PmProviderError):
                self.port.read_committed_ready_hint("pm-receipt:1", expected_owner_ref=OWNER)

    def test_wrong_auth_identity_and_session_provider_refuse(self):
        denied = PmOwnerProjectionAdapter(replace(self.binding, credential_reader=lambda: "denied"))
        with self.assertRaisesRegex(PmProviderError, "AUTH_DENIED"):
            denied.read_committed_ready_hint("pm-receipt:1", expected_owner_ref=OWNER)
        self.assertEqual(self.store.calls, [])
        for field in ("principal_ref", "session_ref", "provider_ref"):
            self.store.receipt = replace(self.store.cut(record()), **{field: "wrong:identity"})
            with self.assertRaisesRegex(PmProviderError, "UNAVAILABLE"):
                self.port.read_committed_ready_hint("pm-receipt:1", expected_owner_ref=OWNER)

    def test_duplicate_projection_mutation_refuses(self):
        self.port.read_committed_ready_hint("pm-receipt:1", expected_owner_ref=OWNER)
        self.store.current = replace(self.store.current, material_sha256="c" * 64)
        with self.assertRaises(PmProviderError):
            self.port.read_committed_ready_hint("pm-receipt:1", expected_owner_ref=OWNER)

    def test_hold_payload_preserved_without_mutable_alias(self):
        hold = {"owner_ref": OWNER, "action_due": True, "reason": "owner-example"}
        self.store.receipt = self.store.current = replace(self.store.current, active_hold=hold)
        current = self.port.reread_ready_hint("pm-ready:claim1",
            expected_owner_ref=OWNER, expected_revision="rev:1")
        self.assertEqual(current.active_hold, hold)
        current.active_hold["action_due"] = False
        self.assertTrue(hold["action_due"])

    def test_priority_or_independent_rows_do_not_qualify_atomic_projection(self):
        self.store.receipt = {"priority": "BK07", "committed": True}
        with self.assertRaises(PmProviderError):
            self.port.read_committed_ready_hint("pm-receipt:1", expected_owner_ref=OWNER)


if __name__ == "__main__":
    unittest.main()
