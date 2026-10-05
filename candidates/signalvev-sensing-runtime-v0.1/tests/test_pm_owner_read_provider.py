"""Focused server-provider contract tests; all ports below are synthetic."""
from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "providers"))

from pm_owner_read import (  # noqa: E402
    AuthenticatedPmPrincipal, PmProviderError, PmReadyRecord,
    ServerPmOwnerProvider, snapshot_sha256,
)


OWNER = "CURRENT_PM_PROJECT_MANAGER_C1A05B39"
AUDIENCE = "signalvev-pm-owner-read"
PACKET_SHA = "a" * 64


def record(*, seq: int = 1, revision: str = "rev:1", event: str = "pm-event:1",
           packet_sha: str = PACKET_SHA) -> PmReadyRecord:
    value = PmReadyRecord(
        owner_ref=OWNER, receipt_ref="pm-receipt:1", event_id=event,
        referent_id="pm-ready:claim1", owner_seq=seq, provider_revision=seq,
        revision_after=revision, revision_before=None,
        claim_ref="WORK_CLAIMS:1", packet_ref="WORK_PACKETS:1",
        queue_ref="READY_QUEUE:1", claim_revision=revision,
        packet_revision=revision, queue_revision=revision,
        packet_sha256=packet_sha, ready_state="MATERIAL_READY",
        snapshot_ref=f"pm-snapshot:{seq}", snapshot_sha256="0" * 64,
        commit_ref=f"pm-commit:{seq}", readback_ref=f"pm-readback:{seq}",
        observed_at="2026-10-05T12:00:00Z", way_home=("pm-home:1",),
    )
    return replace(value, snapshot_sha256=snapshot_sha256(value))


class Credentials:
    def __init__(self) -> None:
        self.calls = []

    def authenticate_and_authorize(self, credential, *, owner_ref, audience, action):
        self.calls.append(action)
        if credential != "synthetic-allowed" or action not in {"read_receipt", "read_current"}:
            return None
        return AuthenticatedPmPrincipal("principal:reader", owner_ref, audience)


class Snapshots:
    def __init__(self, initial: PmReadyRecord) -> None:
        self.receipt = initial
        self.current = initial
        self.calls = []

    def read_receipt(self, receipt_ref, *, owner_ref, principal_ref):
        self.calls.append(("receipt", receipt_ref, principal_ref))
        return self.receipt

    def read_current(self, referent_id, *, owner_ref, principal_ref):
        self.calls.append(("current", referent_id, principal_ref))
        return self.current


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.auth = Credentials()
        self.store = Snapshots(record())
        self.provider = ServerPmOwnerProvider(
            owner_ref=OWNER, audience=AUDIENCE, credentials=self.auth,
            snapshots=self.store, enabled=True)

    def test_default_off_and_bad_credential_read_nothing(self):
        off = ServerPmOwnerProvider(owner_ref=OWNER, audience=AUDIENCE,
                                    credentials=self.auth, snapshots=self.store)
        with self.assertRaisesRegex(PmProviderError, "PM_PROVIDER_PORT_UNBOUND"):
            off.read_committed_ready_hint("pm-receipt:1", expected_owner_ref=OWNER,
                                          credential="synthetic-allowed")
        with self.assertRaisesRegex(PmProviderError, "PM_AUTH_DENIED"):
            self.provider.read_committed_ready_hint(
                "pm-receipt:1", expected_owner_ref=OWNER, credential="bad")
        self.assertEqual(self.store.calls, [])

    def test_one_committed_owner_record_requires_two_authorized_reads(self):
        got = self.provider.read_committed_ready_hint(
            "pm-receipt:1", expected_owner_ref=OWNER,
            credential="synthetic-allowed")
        self.assertEqual(got.packet_sha256, PACKET_SHA)
        self.assertEqual(self.auth.calls, ["read_receipt", "read_current"])
        self.assertEqual([x[0] for x in self.store.calls], ["receipt", "current"])

    def test_receipt_permission_alone_cannot_skip_current_read_acl(self):
        class ReceiptOnly(Credentials):
            def authenticate_and_authorize(self, credential, *, owner_ref, audience, action):
                self.calls.append(action)
                return (AuthenticatedPmPrincipal("principal:reader", owner_ref, audience)
                        if action == "read_receipt" else None)

        auth = ReceiptOnly()
        provider = ServerPmOwnerProvider(owner_ref=OWNER, audience=AUDIENCE,
                                         credentials=auth, snapshots=self.store,
                                         enabled=True)
        with self.assertRaisesRegex(PmProviderError, "PM_AUTH_DENIED"):
            provider.read_committed_ready_hint(
                "pm-receipt:1", expected_owner_ref=OWNER,
                credential="synthetic-allowed")
        self.assertEqual(auth.calls, ["read_receipt", "read_current"])
        self.assertEqual([x[0] for x in self.store.calls], ["receipt"])

    def test_split_revision_and_caller_hash_are_rejected(self):
        self.store.receipt = replace(record(), queue_revision="rev:other")
        with self.assertRaisesRegex(PmProviderError, "PM_SPLIT_REVISION"):
            self.provider.read_committed_ready_hint(
                "pm-receipt:1", expected_owner_ref=OWNER,
                credential="synthetic-allowed")
        self.store.receipt = replace(record(), packet_sha256="b" * 64)
        with self.assertRaisesRegex(PmProviderError, "PM_SNAPSHOT_HASH_MISMATCH"):
            self.provider.read_committed_ready_hint(
                "pm-receipt:1", expected_owner_ref=OWNER,
                credential="synthetic-allowed")

    def test_row_only_identity_or_missing_commit_cannot_be_current(self):
        self.store.receipt = replace(record(), event_id="PM_PRINCIPAL_CHANNEL:10017")
        with self.assertRaisesRegex(PmProviderError, "PM_STABLE_ID_REQUIRED"):
            self.provider.read_committed_ready_hint(
                "pm-receipt:1", expected_owner_ref=OWNER,
                credential="synthetic-allowed")
        self.store.receipt = replace(record(), commit_ref="")
        with self.assertRaisesRegex(PmProviderError, "PM_RECORD_FIELD_MISSING"):
            self.provider.read_committed_ready_hint(
                "pm-receipt:1", expected_owner_ref=OWNER,
                credential="synthetic-allowed")

    def test_stale_and_duplicate_changed_event_are_rejected(self):
        first = record()
        self.store.receipt = first
        self.store.current = record(seq=2, revision="rev:2", event="pm-event:2")
        with self.assertRaisesRegex(PmProviderError, "PM_SNAPSHOT_STALE_OR_CONFLICT"):
            self.provider.read_committed_ready_hint(
                "pm-receipt:1", expected_owner_ref=OWNER,
                credential="synthetic-allowed")
        self.store.current = first
        self.provider.read_committed_ready_hint(
            "pm-receipt:1", expected_owner_ref=OWNER,
            credential="synthetic-allowed")
        changed = record(packet_sha="b" * 64)
        self.store.receipt = changed
        self.store.current = changed
        with self.assertRaisesRegex(PmProviderError, "PM_EVENT_ID_CONTENT_CONFLICT"):
            self.provider.read_committed_ready_hint(
                "pm-receipt:1", expected_owner_ref=OWNER,
                credential="synthetic-allowed")

    def test_reread_and_bounded_missed_d0_recovery(self):
        first = record()
        relation, got = self.provider.reread_ready_hint(
            first.referent_id, expected_owner_ref=OWNER,
            expected_revision=first.revision_after, expected_seq=1,
            expected_sha256=first.snapshot_sha256, credential="synthetic-allowed")
        self.assertEqual((relation, got.owner_seq), ("SAME", 1))
        self.assertIsNone(self.provider.recover_current_ready_hint(
            first.referent_id, after_seq=1, credential="synthetic-allowed"))
        second = record(seq=2, revision="rev:2", event="pm-event:2")
        self.store.current = second
        relation, _ = self.provider.reread_ready_hint(
            first.referent_id, expected_owner_ref=OWNER,
            expected_revision=first.revision_after, expected_seq=1,
            expected_sha256=first.snapshot_sha256, credential="synthetic-allowed")
        self.assertEqual(relation, "SUPERSEDED")
        self.assertEqual(self.provider.recover_current_ready_hint(
            first.referent_id, after_seq=1,
            credential="synthetic-allowed").owner_seq, 2)
        relation, _ = self.provider.reread_ready_hint(
            first.referent_id, expected_owner_ref=OWNER,
            expected_revision="rev:2", expected_seq=2,
            expected_sha256="0" * 64, credential="synthetic-allowed")
        self.assertEqual(relation, "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
