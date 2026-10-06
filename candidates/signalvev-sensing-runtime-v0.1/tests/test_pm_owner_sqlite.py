"""Real disk/SQLite transaction risks; authentication is explicitly synthetic."""
import multiprocessing
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from providers.pm_owner_read import AuthenticatedPmPrincipal, PmProviderError
from providers.pm_owner_projection import PmOwnerProjectionAdapter, PmProjectionBinding, projection_sha256
from providers.pm_owner_sqlite import (SqlitePmOwnerStore, PmReadyTransition,
                                      ContextPmCredentialPort, PmContextCustody)
from adapters.pm_owner_commit import ReadyHintExpectation, PmOwnerCommitReader

OWNER, AUD, PRINCIPAL = "test:pm-owner", "test:audience", "test:producer"


def context_custody(actions):
    return PmContextCustody(OWNER, AUD, "t", "w", "p", PRINCIPAL,
        "CHATGPT_REMOTE_MCP", "test:session", 7, "test:binding", 3, "a" * 64,
        frozenset(actions))


class Auth:
    SYNTHETIC_TEST_ONLY = True
    def __init__(self):
        self.denied = set()
    def authenticate_and_authorize(self, credential, *, owner_ref, audience, action):
        if credential == "offline-only" and action not in self.denied:
            return AuthenticatedPmPrincipal(PRINCIPAL, OWNER, AUD)


class Session:
    SYNTHETIC_TEST_ONLY = True
    def __init__(self):
        self.current = True
        self.calls = 0
        self.revoke_on = None
    def __call__(self, principal, session, action):
        self.calls += 1
        return self.current and self.calls != self.revoke_on \
            and (principal, session) == (PRINCIPAL, "test:session")


def store(path, **kwargs):
    return SqlitePmOwnerStore(database_path=path, owner_ref=OWNER,
        provider_ref="test:sqlite-provider", audience=AUD,
        principal_ref=PRINCIPAL, session_ref="test:session", credentials=kwargs.pop("auth", Auth()),
        credential_reader=lambda: "offline-only", session_check=kwargs.pop("session", Session()),
        enabled=kwargs.pop("enabled", True), **kwargs)


def transition(**kwargs):
    return replace(PmReadyTransition("attempt:1", "semantic:case", None,
        "claim:test", "packet:test", "queue:test", b"exact packet", b"exact material",
        "MATERIAL_READY", "owner-decision:test", None, ("test:owner-home",)), **kwargs)


def race_writer(path, index, ready, go, result):
    db = store(path)
    ready.put(index)
    go.wait(10)
    try:
        cut = db.commit_ready(transition(attempt_ref=f"race:{index}"))
        result.put(("commit", cut.record.owner_seq))
    except PmProviderError as exc:
        result.put(("deny", str(exc)))


class SqliteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "pm.sqlite3")
        self.auth, self.session = Auth(), Session()
        self.db = store(self.path, auth=self.auth, session=self.session)
        self.db.initialize()
    def tearDown(self):
        self.temp.cleanup()

    def test_restart_keeps_atomic_cut_and_client_projection(self):
        cut = self.db.commit_ready(transition(active_hold={"action_due": False}))
        restarted = store(self.path)
        again = restarted.read_current(cut.record.referent_id, owner_ref=OWNER, principal_ref=PRINCIPAL)
        self.assertEqual(cut, again)
        r = cut.record
        expectation = ReadyHintExpectation(OWNER, r.claim_ref, r.packet_ref, r.queue_ref, r.packet_sha256)
        binding = PmProjectionBinding(expectation, r.receipt_ref, AUD,
            restarted.provider_ref, PRINCIPAL, restarted.session_ref,
            restarted.credentials, restarted, restarted.credential_reader, True)
        port = PmOwnerProjectionAdapter(binding)
        reader = PmOwnerCommitReader(expectation, port=port, enabled=True)
        self.assertEqual(reader.read_verified(r.receipt_ref, expected_owner_ref=OWNER).event_payload["event_id"], r.event_id)
        read = reader.reread_current(r.referent_id, r.revision_after,
            expected_sha256=projection_sha256(cut), expected_seq=r.owner_seq, require_projection=True)
        self.assertEqual(read.relation, "SAME")
        self.assertEqual(read.material_sha256, cut.material_sha256)
        with self.assertRaisesRegex(PmProviderError, "SYNTHETIC"):
            restarted.source_port(expectation=expectation, receipt_ref=r.receipt_ref)

    def test_idempotency_survives_restart_and_payload_conflicts(self):
        cut = self.db.commit_ready(transition())
        self.assertEqual(store(self.path).commit_ready(transition()), cut)
        for delta in ({"packet_bytes": b"other"}, {"material_bytes": b"other"},
                      {"source_cut": "other:cut"}, {"active_hold": {"action_due": True}}):
            with self.assertRaisesRegex(PmProviderError, "ATTEMPT_CONTENT_CONFLICT"):
                store(self.path).commit_ready(transition(**delta))

    def test_cas_allocates_next_revision_and_preserves_receipt(self):
        first = self.db.commit_ready(transition())
        with self.assertRaisesRegex(PmProviderError, "REVISION_CONFLICT"):
            self.db.commit_ready(transition(attempt_ref="attempt:2"))
        second = self.db.commit_ready(transition(attempt_ref="attempt:2",
            expected_revision=first.record.revision_after, material_bytes=b"next"))
        self.assertEqual(second.record.owner_seq, 2)
        self.assertEqual(second.record.revision_before, first.record.revision_after)
        self.assertEqual(second.record.claim_revision, second.record.packet_revision)
        self.assertEqual(second.record.packet_revision, second.record.queue_revision)
        self.assertEqual(store(self.path).read_receipt(first.record.receipt_ref,
            owner_ref=OWNER, principal_ref=PRINCIPAL), first)

    def test_two_processes_cannot_admit_same_expected_cut(self):
        ctx = multiprocessing.get_context("spawn")
        ready, result, go = ctx.Queue(), ctx.Queue(), ctx.Event()
        jobs = [ctx.Process(target=race_writer, args=(self.path, i, ready, go, result)) for i in range(2)]
        try:
            for job in jobs:
                job.start()
            for _ in jobs:
                ready.get(timeout=15)
            go.set()
            outcomes = [result.get(timeout=15) for _ in jobs]
            for job in jobs:
                job.join(15)
                self.assertEqual(job.exitcode, 0)
            self.assertCountEqual(outcomes, [("commit", 1), ("deny", "PM_COMMIT_REVISION_CONFLICT")])
        finally:
            for job in jobs:
                if job.is_alive():
                    job.terminate()
                    job.join()
            for q in (ready, result):
                q.close()

    def test_auth_session_and_default_off_fail_before_read_or_write(self):
        cut = self.db.commit_ready(transition())
        self.auth.denied.add("commit_ready")
        with self.assertRaisesRegex(PmProviderError, "AUTH_DENIED"):
            self.db.commit_ready(transition(attempt_ref="attempt:2", expected_revision=cut.record.revision_after))
        self.session.current = False
        with self.assertRaisesRegex(PmProviderError, "AUTH_DENIED"):
            self.db.read_receipt(cut.record.receipt_ref, owner_ref=OWNER, principal_ref=PRINCIPAL)
        with self.assertRaisesRegex(PmProviderError, "DEFAULT_OFF"):
            store(str(Path(self.temp.name) / "absent.sqlite3"), enabled=False).initialize()
        self.assertFalse((Path(self.temp.name) / "absent.sqlite3").exists())
        with self.assertRaisesRegex(PmProviderError, "IDENTITY_MISMATCH"):
            store(self.path).read_current(cut.record.referent_id, owner_ref="wrong", principal_ref=PRINCIPAL)

    def test_revocation_before_commit_rolls_back_without_consuming_seq(self):
        self.session.revoke_on = self.session.calls + 2
        with self.assertRaisesRegex(PmProviderError, "AUTH_DENIED"):
            self.db.commit_ready(transition())
        self.session.revoke_on = None
        cut = store(self.path).commit_ready(transition())
        self.assertEqual(cut.record.owner_seq, 1)

    def test_readback_failure_after_commit_is_reconciled_by_same_attempt(self):
        self.auth.denied.add("read_receipt")
        with self.assertRaisesRegex(PmProviderError, "AUTH_DENIED"):
            self.db.commit_ready(transition())
        # Read failure is not proof of rollback. Reconcile, never invent another attempt.
        self.auth.denied.clear()
        cut = store(self.path).commit_ready(transition())
        self.assertEqual(cut.record.owner_seq, 1)

    def test_mutable_hold_and_database_tamper_do_not_cross_read_boundary(self):
        hold = {"action_due": True}
        cut = self.db.commit_ready(transition(active_hold=hold))
        hold["action_due"] = False
        read = self.db.read_receipt(cut.record.receipt_ref, owner_ref=OWNER, principal_ref=PRINCIPAL)
        self.assertTrue(read.active_hold["action_due"])
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE pm_owner_commits SET payload='{}'")
            db.commit()
        with self.assertRaisesRegex(PmProviderError, "PAYLOAD_HASH_MISMATCH"):
            self.db.read_current(cut.record.referent_id, owner_ref=OWNER, principal_ref=PRINCIPAL)

    def test_no_database_creation_from_reads_and_wrong_provider_refused(self):
        missing = store(str(Path(self.temp.name) / "never-created.sqlite3"))
        with self.assertRaisesRegex(PmProviderError, "TRANSACTION_UNAVAILABLE"):
            missing.read_current("pm-ready:missing", owner_ref=OWNER, principal_ref=PRINCIPAL)
        self.assertFalse(missing.path.exists())
        self.db.provider_ref = "wrong:provider"
        with self.assertRaisesRegex(PmProviderError, "DATABASE_CUSTODY_MISMATCH"):
            self.db.commit_ready(transition())

    def test_context_auth_requires_exact_owner_identity_session_and_project(self):
        custody = context_custody({"read_current", "commit_ready"})
        calls = []
        identity = SimpleNamespace(tenant_ref="t", workspace_ref="w", principal_ref=PRINCIPAL,
            consumer_ref="CHATGPT_REMOTE_MCP", token_verified=True,
            scopes=frozenset({"project_state:read", "project_state:transition"}))
        session = {k: getattr(custody, k) for k in ("tenant_ref", "workspace_ref", "project_ref",
            "principal_ref", "consumer_ref", "session_ref", "project_revision", "session_binding_id",
            "session_revision", "session_fingerprint")}
        class Authenticator:
            SYNTHETIC_TEST_ONLY = True
            def authenticate(self, headers, *, required_scope):
                calls.append(required_scope)
                return identity
        class State:
            SYNTHETIC_TEST_ONLY = True
            def read_session(self, **kwargs):
                return session
        port = ContextPmCredentialPort(custody=custody, authenticator=Authenticator(),
            state_port=State(), credential_reader=lambda: "offline-only")
        self.assertIsNotNone(port.authenticate_and_authorize("offline-only", owner_ref=OWNER,
            audience=AUD, action="commit_ready"))
        self.assertEqual(calls, ["project_state:transition"])
        self.assertTrue(port.session_check(PRINCIPAL, "test:session", "read_current"))
        session["project_ref"] = "different:project"
        self.assertFalse(port.session_check(PRINCIPAL, "test:session", "read_current"))
        session["project_ref"] = "p"
        identity.principal_ref = "other:principal"
        self.assertIsNone(port.authenticate_and_authorize("offline-only", owner_ref=OWNER,
            audience=AUD, action="read_current"))
        self.assertIsNone(port.authenticate_and_authorize("offline-only", owner_ref=OWNER,
            audience=AUD, action="initialize"))

    def test_context_same_principal_session_rejects_stale_project_or_session_revision(self):
        custody = context_custody({"read_current"})
        identity = SimpleNamespace(tenant_ref="t", workspace_ref="w", principal_ref=PRINCIPAL,
            consumer_ref="CHATGPT_REMOTE_MCP", token_verified=True,
            scopes=frozenset({"project_state:read"}))
        session = {k: getattr(custody, k) for k in (
            "tenant_ref", "workspace_ref", "project_ref", "principal_ref", "consumer_ref", "session_ref",
            "project_revision", "session_binding_id", "session_revision", "session_fingerprint")}
        class Authenticator:
            SYNTHETIC_TEST_ONLY = True
            def authenticate(self, headers, *, required_scope):
                return identity
        class State:
            SYNTHETIC_TEST_ONLY = True
            def read_session(self, **kwargs):
                return session
        port = ContextPmCredentialPort(custody=custody, authenticator=Authenticator(),
            state_port=State(), credential_reader=lambda: "offline-only")
        for name, stale in (("project_revision", 8), ("session_revision", 4)):
            with self.subTest(field=name):
                session[name] = stale
                self.assertFalse(port.session_check(PRINCIPAL, "test:session", "read_current"))
                session[name] = getattr(custody, name)

    def test_context_same_principal_session_rejects_stale_binding_or_fingerprint(self):
        custody = context_custody({"read_current"})
        identity = SimpleNamespace(tenant_ref="t", workspace_ref="w", principal_ref=PRINCIPAL,
            consumer_ref="CHATGPT_REMOTE_MCP", token_verified=True,
            scopes=frozenset({"project_state:read"}))
        session = {k: getattr(custody, k) for k in (
            "tenant_ref", "workspace_ref", "project_ref", "principal_ref", "consumer_ref", "session_ref",
            "project_revision", "session_binding_id", "session_revision", "session_fingerprint")}
        class Authenticator:
            SYNTHETIC_TEST_ONLY = True
            def authenticate(self, headers, *, required_scope):
                return identity
        class State:
            SYNTHETIC_TEST_ONLY = True
            def read_session(self, **kwargs):
                return session
        port = ContextPmCredentialPort(custody=custody, authenticator=Authenticator(),
            state_port=State(), credential_reader=lambda: "offline-only")
        for name, stale in (("session_binding_id", "test:other-binding"), ("session_fingerprint", "b" * 64)):
            with self.subTest(field=name):
                session[name] = stale
                self.assertFalse(port.session_check(PRINCIPAL, "test:session", "read_current"))
                session[name] = getattr(custody, name)

    def test_context_stale_session_and_scope_errors_never_grant_access(self):
        custody = context_custody({"read_current"})
        class Authenticator:
            SYNTHETIC_TEST_ONLY = True
            def authenticate(self, headers, *, required_scope):
                raise ValueError("synthetic expired-token")
        class State:
            def read_session(self, **kwargs):
                raise AssertionError("must not read with expired-token")
        port = ContextPmCredentialPort(custody=custody, authenticator=Authenticator(),
            state_port=State(), credential_reader=lambda: "offline-only")
        with self.assertRaisesRegex(PmProviderError, "AUTH_OR_SESSION_UNAVAILABLE"):
            port.session_check(PRINCIPAL, "test:session", "read_current")
        self.assertFalse(port.session_check(PRINCIPAL, "wrong:session", "read_current"))


if __name__ == "__main__":
    unittest.main()
