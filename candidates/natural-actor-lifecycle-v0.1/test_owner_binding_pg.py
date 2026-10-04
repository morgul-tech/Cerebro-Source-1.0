"""C995 disposable same-PG owner-binding proof; skipped without local test DSNs."""
from __future__ import annotations

import os
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor

import psycopg

from lifecycle import Hold, Lifecycle
from owner_binding_pg import PgOwnerBindingEvidence, PgOwnerBindingIssuer
from postgres_store import PostgresStore
import test_lifecycle as fixture


CLOSER_DSN = os.environ.get("NAL_C995_CLOSER_DSN")
ISSUER_DSN = os.environ.get("NAL_C995_ISSUER_DSN")


@unittest.skipUnless(CLOSER_DSN and ISSUER_DSN, "disposable C995 PostgreSQL DSNs required")
class OwnerBindingPgProof(unittest.TestCase):
    def setUp(self):
        self.suffix = uuid.uuid4().hex[:10]
        self.case = fixture.Episode()
        self.case.setUp()
        self.case.actor = "actor-" + self.suffix
        self.case.generation = "gen-" + self.suffix
        self.connect_closer = lambda: psycopg.connect(CLOSER_DSN)
        self.connect_issuer = lambda: psycopg.connect(ISSUER_DSN)
        self.store = PostgresStore(self.connect_closer, enabled=True)
        self.case.store = self.store
        self.evidence = PgOwnerBindingEvidence(self.connect_closer, self.case.evidence,
                                               enabled=True)
        self.case.nal = Lifecycle(self.store, self.evidence, enabled=True)
        self.issuer = PgOwnerBindingIssuer(self.connect_issuer, enabled=True)

    def issue_task_binding(self):
        self.case.ready()
        self.case.task(self.suffix)
        ref = self.case.binding(self.suffix)
        payload = self.case.evidence.rows[("OWNER_BINDING", ref)]
        issued = self.issuer.issue(payload)
        self.assertEqual(issued["binding_ref"], ref)
        self.assertEqual(self.case.nal.issue_binding(self.case.actor, ref).status,
                         "BINDING_ISSUED_READBACK")
        return ref, issued

    def terminal_ready(self):
        ref, issued = self.issue_task_binding()
        consume = self.case.worker("WORKER_CONSUME", "consume-" + self.suffix)
        self.case.nal.worker_consume(self.case.actor, consume)
        start = self.case.worker("WORKER_START", "start-" + self.suffix)
        self.case.nal.worker_start(self.case.actor, start)
        self.case.terminal(self.suffix)
        admission = self.case.admission(self.suffix)
        return ref, issued, admission

    def close(self, admission):
        return self.case.nal.admit_release_once(
            self.case.actor, "terminal-" + self.suffix, admission)

    def db_counts(self):
        with self.connect_closer() as conn, conn.cursor() as cur:
            cur.execute("SELECT (SELECT count(*) FROM nal.close_receipt WHERE actor_ref=%s), "
                        "(SELECT count(*) FROM nal.outbox_intent WHERE event_json->>'actor_ref'=%s)",
                        (self.case.actor, self.case.actor))
            return cur.fetchone()

    def test_owner_adapter_default_off_before_connection(self):
        def forbidden_connection():
            self.fail("default-off adapter opened a connection")

        issuer = PgOwnerBindingIssuer(forbidden_connection)
        with self.assertRaises(Hold) as held:
            issuer.issue({})
        self.assertEqual(held.exception.code, "PM_BINDING_ISSUER_DEFAULT_OFF")
        reader = PgOwnerBindingEvidence(forbidden_connection, self.case.evidence)
        with self.assertRaises(Hold) as held:
            reader.read("OWNER_BINDING", "none")
        self.assertEqual(held.exception.code, "PM_BINDING_READER_DEFAULT_OFF")

    def test_issue_readback_and_issuer_closer_rights(self):
        ref, issued = self.issue_task_binding()
        self.assertEqual(self.evidence.read("OWNER_BINDING", ref), issued)
        self.assertTrue(issued["issue_receipt"].startswith("PMB-I-"))
        issue_receipt = self.issuer.read_receipt(issued["issue_receipt"])
        self.assertEqual(issue_receipt["operation"], "ISSUE")
        self.assertEqual(issue_receipt["provider_revision"], int(issued["provider_revision"]))
        with self.connect_closer() as conn, conn.cursor() as cur:
            cur.execute("SELECT current_user")
            self.assertEqual(cur.fetchone()[0], "cerebro_nal_owner")
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                cur.execute("SELECT pm_binding.revoke(%s,%s)",
                            (ref, int(issued["provider_revision"])))
        with self.connect_issuer() as conn, conn.cursor() as cur:
            cur.execute("SELECT current_user")
            self.assertEqual(cur.fetchone()[0], "cerebro_pm_binding_issuer")
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                cur.execute("SELECT pm_binding.acquire_close_fence(" +
                            ",".join(["%s"] * 8) + ")",
                            (ref, int(issued["provider_revision"]), self.case.actor,
                             self.case.generation, "task-" + self.suffix,
                             "claim-" + self.suffix, "packet-" + self.suffix,
                             "queue-" + self.suffix))

    def test_revoke_first_zero_close_first_one_and_replay(self):
        ref, issued, admission = self.terminal_ready()
        rev = int(issued["provider_revision"])
        receipt = self.issuer.revoke(ref, rev)
        self.assertEqual(receipt["operation"], "REVOKE")
        self.assertEqual(self.issuer.read_receipt(receipt["receipt_ref"]), receipt)
        self.assertEqual(self.evidence.read("OWNER_BINDING", ref)["revoked"], True)
        with self.assertRaises(Hold) as held:
            self.close(admission)
        self.assertEqual(held.exception.code, "OWNER_BINDING_CHANGED_BEFORE_COMMIT")
        self.assertEqual(self.db_counts(), (0, 0))

        # A separate actor/frontier proves the other linearization order.
        self.setUp()
        ref, issued, admission = self.terminal_ready()
        first = self.close(admission)
        self.issuer.revoke(ref, int(issued["provider_revision"]))
        replay = self.close(admission)
        self.assertEqual(replay.status, "REPLAY_SAME_RECEIPT")
        self.assertEqual(replay.receipt, first.receipt)
        self.assertEqual(self.db_counts(), (1, 1))

    def test_concurrent_revoke_waits_for_same_pg_close_commit(self):
        ref, issued, admission = self.terminal_ready()
        at_commit, permit, revoking_started, revoked = (threading.Event() for _ in range(4))
        original = self.store._append_ledgers

        def paused(cur, old, new):
            original(cur, old, new)
            at_commit.set()
            if not permit.wait(4):
                raise RuntimeError("test commit permit timeout")

        def revoke():
            revoking_started.set()
            value = self.issuer.revoke(ref, int(issued["provider_revision"]))
            revoked.set()
            return value

        self.store._append_ledgers = paused
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                closing = pool.submit(self.close, admission)
                self.assertTrue(at_commit.wait(4))
                revoking = pool.submit(revoke)
                self.assertTrue(revoking_started.wait(4))
                self.assertFalse(revoked.wait(0.05))
                permit.set()
                self.assertEqual(closing.result(timeout=4).status,
                                 "TASK_CLOSED_RECEIPTED_OUTBOXED")
                self.assertEqual(revoking.result(timeout=4)["operation"], "REVOKE")
        finally:
            permit.set()
            self.store._append_ledgers = original
        self.assertEqual(self.db_counts(), (1, 1))

    def test_wrong_actor_revision_and_direct_bypass_rejected(self):
        ref, issued = self.issue_task_binding()
        rev = int(issued["provider_revision"])
        for actor, expected in (("wrong-actor", rev), (self.case.actor, rev + 1)):
            with self.subTest(actor=actor, expected=expected):
                with self.connect_closer() as conn, conn.cursor() as cur:
                    with self.assertRaises(psycopg.errors.RaiseException):
                        cur.execute("SELECT pm_binding.acquire_close_fence(" +
                                    ",".join(["%s"] * 8) + ")",
                                    (ref, expected, actor, self.case.generation,
                                     "task-" + self.suffix, "claim-" + self.suffix,
                                     "packet-" + self.suffix, "queue-" + self.suffix))
        with self.connect_issuer() as conn, conn.cursor() as cur:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                cur.execute("UPDATE pm_binding.current_binding SET status='REVOKED' "
                            "WHERE binding_ref=%s", (ref,))
        with self.connect_closer() as conn, conn.cursor() as cur:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                cur.execute("UPDATE pm_binding.current_binding SET status='REVOKED' "
                            "WHERE binding_ref=%s", (ref,))
        with self.assertRaises(psycopg.errors.RaiseException):
            self.issuer.revoke(ref, rev + 1)
        self.assertEqual(self.evidence.read("OWNER_BINDING", ref)["current"], True)

    def test_second_active_binding_cannot_bypass_supersession(self):
        first, _ = self.issue_task_binding()
        second = "parallel-" + self.suffix
        self.case.binding(self.suffix, binding_ref=second)
        payload = self.case.evidence.rows[("OWNER_BINDING", second)]
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.issuer.issue(payload)
        bypass = "bypass-" + self.suffix
        self.case.binding(self.suffix, binding_ref=bypass, supersedes=first)
        payload = self.case.evidence.rows[("OWNER_BINDING", bypass)]
        with self.assertRaises(psycopg.errors.RaiseException):
            self.issuer.issue(payload)
        self.assertEqual(self.evidence.read("OWNER_BINDING", first)["current"], True)

    def test_supersede_receipts_and_stale_old_binding(self):
        first, issued = self.issue_task_binding()
        second = "successor-" + self.suffix
        self.case.binding(self.suffix, binding_ref=second, supersedes=first)
        payload = self.case.evidence.rows[("OWNER_BINDING", second)]
        result = self.issuer.supersede(first, int(issued["provider_revision"]), payload)
        self.assertTrue(result["issue_receipt"].startswith("PMB-I-"))
        self.assertTrue(result["supersede_receipt"].startswith("PMB-S-"))
        self.assertEqual(self.issuer.read_receipt(result["issue_receipt"])["operation"], "ISSUE")
        self.assertEqual(self.issuer.read_receipt(result["supersede_receipt"])["operation"],
                         "SUPERSEDE")
        self.assertFalse(self.evidence.read("OWNER_BINDING", first)["current"])
        self.assertEqual(self.case.nal.supersede_binding(self.case.actor, second).status,
                         "BINDING_SUPERSEDED_READBACK")
        consume = self.case.worker("WORKER_CONSUME", "consume-" + self.suffix)
        self.assertEqual(self.case.nal.worker_consume(self.case.actor, consume).status,
                         "WORK_CONSUMED")

    def test_wrong_role_issue_and_supersession_fail_closed(self):
        first, issued = self.issue_task_binding()
        successor = "wrong-successor-" + self.suffix
        self.case.binding(self.suffix, binding_ref=successor, supersedes=first,
                          role="RESEARCHER")
        payload = self.case.evidence.rows[("OWNER_BINDING", successor)]
        with self.assertRaises(psycopg.errors.RaiseException):
            self.issuer.supersede(first, int(issued["provider_revision"]), payload)
        self.assertEqual(self.evidence.read("OWNER_BINDING", first)["current"], True)
        self.assertEqual(self.store.read(self.case.actor)["current_task"]["binding_ref"], first)

        self.setUp()  # New actor/task; a malformed initial issue remains inert to NAL.
        self.case.ready()
        self.case.task(self.suffix)
        wrong_ref = self.case.binding(self.suffix, role="RESEARCHER")
        wrong = self.case.evidence.rows[("OWNER_BINDING", wrong_ref)]
        self.issuer.issue(wrong)
        with self.assertRaises(Hold) as held:
            self.case.nal.issue_binding(self.case.actor, wrong_ref)
        self.assertEqual(held.exception.code, "OWNER_BINDING_STALE_WRONG_ROLE_OR_FINGERPRINT")

    def test_before_commit_abort_rolls_back_and_releases_owner_fence(self):
        _, _, admission = self.terminal_ready()
        original = self.store._append_ledgers

        def abort(cur, old, new):
            original(cur, old, new)
            raise RuntimeError("synthetic precommit interruption")

        self.store._append_ledgers = abort
        try:
            with self.assertRaises(Hold) as held:
                self.close(admission)
            self.assertEqual(held.exception.code, "PG_TRANSACTION_UNAVAILABLE")
        finally:
            self.store._append_ledgers = original
        self.assertEqual(self.db_counts(), (0, 0))
        self.assertEqual(self.close(admission).status, "TASK_CLOSED_RECEIPTED_OUTBOXED")
        self.assertEqual(self.db_counts(), (1, 1))

    def test_unknown_postcommit_reconciles_one_receipt_and_pending_intent(self):
        _, _, admission = self.terminal_ready()
        original = self.store.read_commit_evidence
        self.store.read_commit_evidence = lambda *_: (_ for _ in ()).throw(
            RuntimeError("synthetic postcommit readback timeout"))
        try:
            with self.assertRaises(Hold) as held:
                self.close(admission)
            self.assertEqual(held.exception.code, "PG_COMMIT_OUTCOME_UNKNOWN")
        finally:
            self.store.read_commit_evidence = original
        recovered_store = PostgresStore(self.connect_closer, enabled=True)
        recovered = Lifecycle(recovered_store, self.evidence, enabled=True)
        replay = recovered.admit_release_once(self.case.actor,
                                              "terminal-" + self.suffix, admission)
        self.assertEqual(replay.status, "REPLAY_SAME_RECEIPT")
        self.assertEqual(len(recovered.pending_outbox(self.case.actor)), 1)
        self.assertEqual(self.db_counts(), (1, 1))
