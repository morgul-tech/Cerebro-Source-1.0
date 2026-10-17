#!/usr/bin/env python3
"""No-effect fault batch for the PostgreSQL Rom A admission prototype."""

from __future__ import annotations

import copy
import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
for name in ("mcp", "tooling/context", "tooling/owner_state", "tooling/validator",
             "engines/project", "engines/quality", "engines/convergence"):
    sys.path.insert(0, str(ROOT / name))

from control_context_postgres_validation import ScriptedConnection, ScriptedCursor, ScriptedDatabaseError
from control_context_state_postgres import apply_postgres_migrations
from control_context_state_port import StateAuthorizationError, StateConflict, StateServiceUnavailable
from rom_a_owner_episode_candidate import PostgresRomAOwnerEpisodePort, _sha256


class Verifier:
    """Synthetic ingress only; this does not implement an actual owner fence."""

    def __init__(self, state):
        self.state = state
        self.authenticated = True
        self.fence_active = False
        self.on_enter = None
        self.verify_error = None
        self.exit_error = None
        self.swallow_error = False
        self.events = []
        self.fence_requests = []
        self.verify_requests = []
        self.verified_under_fence = []

    @contextmanager
    def current_fence(self, *, operation, request):
        self.events.append("fence-enter")
        self.fence_requests.append((operation, copy.deepcopy(request)))
        self.fence_active = True
        try:
            if self.on_enter is not None:
                self.on_enter(self)
            yield
        except Exception:
            self.events.append("fence-error")
            if not self.swallow_error:
                raise
        finally:
            self.fence_active = False
            self.events.append("fence-exit")
            if self.exit_error is not None:
                raise self.exit_error

    def verify_current(self, *, operation, request):
        self.events.append("verify")
        self.verify_requests.append((operation, copy.deepcopy(request)))
        self.verified_under_fence.append(self.fence_active)
        if self.verify_error is not None:
            raise self.verify_error
        return {"authenticated": self.authenticated, "currentness": "CURRENT",
                "owner_ref": "A1", "human_mandate_readback": True,
                "tenant_ref": "TENANT-1", "workspace_ref": "WORKSPACE-1",
                "principal_ref": "PRINCIPAL-1", "owner_state": copy.deepcopy(self.state)}


class FencedCursor(ScriptedCursor):
    def __init__(self, scripted_steps, verifier):
        super().__init__(scripted_steps)
        self.verifier = verifier
        self.bindings = []

    def execute(self, sql, params=None):
        if not self.verifier.fence_active:
            raise AssertionError("SQL-outside-synthetic-owner-fence")
        self.verifier.events.append("sql")
        self.bindings.append((sql, copy.deepcopy(params)))
        super().execute(sql, params)


class FencedConnection(ScriptedConnection):
    """Instrument the existing in-memory connection, without opening a database."""

    def __init__(self, scripted_steps, verifier, *, commit_error=None):
        super().__init__(scripted_steps, commit_error=commit_error)
        self.verifier = verifier
        self.cursor_instance = FencedCursor(scripted_steps, verifier)

    def commit(self):
        if not self.verifier.fence_active:
            raise AssertionError("commit-outside-synthetic-owner-fence")
        self.verifier.events.append("commit")
        super().commit()

    def rollback(self):
        if not self.verifier.fence_active:
            raise AssertionError("rollback-outside-synthetic-owner-fence")
        self.verifier.events.append("rollback")
        super().rollback()


def steps(*tail):
    return [{"contains": "set_config('cerebro.tenant_ref'"},
            {"contains": "set_config('cerebro.workspace_ref'"},
            {"contains": "set_config('cerebro.principal_ref'"},
            {"contains": "SET CONSTRAINTS ALL DEFERRED"}, *tail]


class RomAOwnerTransaction(unittest.TestCase):
    def setUp(self):
        self.state = {
            "schema": "cerebro-rom-a-owner-episode/v1", "owner_ref": "A1",
            "episode_ref": "EP-1", "task_ref": "TASK-1", "task_revision": "TREV-1",
            "actor_ref": "ACTOR-1", "scope_ref": "SCOPE-1",
            "mandate_ref": "MANDATE-1", "mandate_revision": "MREV-1",
            "human_provenance_ref": "HUMAN-1", "dependency_ref": "DEP-1",
            "dependency_revision": "DREV-1", "dependency_source_ref": "SOURCE-1",
            "selected_sha256": "a" * 64, "progress_state": "WAITING_DEPENDENCY",
            "dependency_state": "RESOLVED", "dependency_disposition": "SOURCE_AFTER_DEPENDENCY",
            "paused": False, "revoked": False, "all_dependencies_resolved": True,
            "dependency_applicable": True, "other_unresolved_gates": [],
        }
        self.verifier = Verifier(self.state)

    def port(self, connection):
        return PostgresRomAOwnerEpisodePort(lambda: connection, self.verifier)

    def row(self, revision=1, state=None):
        return {"owner_revision": revision, "owner_payload": copy.deepcopy(state or self.state),
                "admission_basis": None, "admission_state": "NONE"}

    def admission(self, basis=None, revision=1):
        return {"basis_fingerprint": basis or _sha256(self.state), "owner_revision": revision,
                "admission_state": "ADMITTED_UNCERTAIN"}

    def admission_steps(self):
        return steps(
            {"contains": "FROM cerebro_rom_a_owner_episodes", "rows": [self.row()]},
            {"contains": "FROM cerebro_rom_a_admissions", "rows": []},
            {"contains": "INSERT INTO cerebro_rom_a_admissions"},
            {"contains": "UPDATE cerebro_rom_a_owner_episodes"})

    def read_steps(self, admissions, *, revision=1):
        return steps(
            {"contains": "FROM cerebro_rom_a_owner_episodes", "rows": [self.row(revision)]},
            {"contains": "SELECT basis_fingerprint, owner_revision, admission_state FROM cerebro_rom_a_admissions",
             "rows": admissions})

    def test_provision_authentication_and_commit(self):
        connection = ScriptedConnection(steps(
            {"contains": "FROM cerebro_rom_a_owner_episodes", "rows": []},
            {"contains": "INSERT INTO cerebro_rom_a_owner_episodes"}))
        self.assertEqual(self.port(connection).provision(episode_ref="EP-1")["state"], "PROVISIONED")
        self.assertTrue(connection.commit_called)
        self.assertFalse(connection.cursor_instance.steps)
        self.verifier.authenticated = False
        blocked = ScriptedConnection([])
        with self.assertRaisesRegex(Exception, "authenticated-owner-ingress-required"):
            self.port(blocked).provision(episode_ref="EP-1")
        self.assertFalse(blocked.commit_called)

    def test_admission_is_unique_uncertain_and_not_delivery(self):
        connection = ScriptedConnection(steps(
            {"contains": "FROM cerebro_rom_a_owner_episodes", "rows": [self.row()]},
            {"contains": "FROM cerebro_rom_a_admissions", "rows": []},
            {"contains": "INSERT INTO cerebro_rom_a_admissions"},
            {"contains": "UPDATE cerebro_rom_a_owner_episodes"}))
        result = self.port(connection).admit(
            episode_ref="EP-1", expected_owner_revision=1,
            expected_basis_fingerprint=_sha256(self.state))
        self.assertEqual((result["state"], result["delivery"]),
                         ("ADMITTED_UNCERTAIN", "NOT_PROVEN"))
        self.assertTrue(connection.commit_called)
        self.assertFalse(connection.cursor_instance.steps)
        duplicate = ScriptedConnection(steps(
            {"contains": "FROM cerebro_rom_a_owner_episodes", "rows": [self.row()]},
            {"contains": "FROM cerebro_rom_a_admissions", "rows": [{"basis_fingerprint": _sha256(self.state)}]}))
        with self.assertRaises(StateConflict):
            self.port(duplicate).admit(episode_ref="EP-1", expected_owner_revision=1,
                                       expected_basis_fingerprint=_sha256(self.state))
        self.assertFalse(duplicate.commit_called)

    def test_pause_revoke_refall_and_revision_drift_prevent_admission(self):
        for field, value in (("paused", True), ("revoked", True),
                             ("dependency_state", "REOPENED")):
            with self.subTest(field=field):
                state = copy.deepcopy(self.state); state[field] = value
                self.verifier.state = state
                connection = ScriptedConnection([])
                with self.assertRaises(StateConflict):
                    self.port(connection).admit(episode_ref="EP-1", expected_owner_revision=1,
                                               expected_basis_fingerprint=_sha256(state))
                self.assertFalse(connection.commit_called)
        self.verifier.state = self.state
        stale = ScriptedConnection(steps(
            {"contains": "FROM cerebro_rom_a_owner_episodes", "rows": [self.row(2)]}))
        with self.assertRaises(StateConflict):
            self.port(stale).admit(episode_ref="EP-1", expected_owner_revision=1,
                                   expected_basis_fingerprint=_sha256(self.state))
        self.assertTrue(stale.rollback_called)
        changed = copy.deepcopy(self.state); changed["paused"] = True
        drift = ScriptedConnection(steps(
            {"contains": "FROM cerebro_rom_a_owner_episodes", "rows": [self.row(state=changed)]}))
        with self.assertRaises(StateConflict):
            self.port(drift).admit(episode_ref="EP-1", expected_owner_revision=1,
                                   expected_basis_fingerprint=_sha256(self.state))
        self.assertTrue(drift.rollback_called)

    def test_cold_read_reconstructs_owner_basis_and_admission_debt(self):
        basis = _sha256(self.state)
        connection = ScriptedConnection(self.read_steps([self.admission()]))
        result = self.port(connection).read_episode(episode_ref="EP-1")
        self.assertEqual((result["state"], result["currentness"]), ("CURRENT_READBACK", "CURRENT"))
        self.assertEqual(result["owner_basis_fingerprint"], basis)
        self.assertEqual(result["current_owner_basis_fingerprint"], basis)
        self.assertEqual(result["admitted_basis_fingerprints"], [basis])
        self.assertEqual(result["admissions"], [{"basis_fingerprint": basis, "owner_revision": 1,
                                                 "state": "ADMITTED_UNCERTAIN"}])
        self.assertEqual(result["delivery"], "NOT_PROVEN")
        self.assertEqual(result["effect"], "NOT_PROVEN")
        self.assertTrue(connection.commit_called)

    def test_missing_or_noncallable_fence_blocks_before_owner_or_database_access(self):
        for fence in ("MISSING", None, False):
            with self.subTest(fence=fence):
                class UnfencedVerifier:
                    verify_current = self.verifier.verify_current

                ingress = UnfencedVerifier()
                if fence != "MISSING":
                    ingress.current_fence = fence
                connection = ScriptedConnection([])
                with self.assertRaisesRegex(StateAuthorizationError, "currentness-fence-unbound"):
                    PostgresRomAOwnerEpisodePort(lambda: connection, ingress).provision(episode_ref="EP-1")
                self.assertFalse(self.verifier.verify_requests)
                self.assertFalse(connection.cursor_instance.executed)
                self.assertFalse(connection.commit_called)

    def test_fence_covers_verification_sql_and_commit_for_each_operation(self):
        cases = (
            ("provision", "PROVISION", {}, steps(
                {"contains": "FROM cerebro_rom_a_owner_episodes", "rows": []},
                {"contains": "INSERT INTO cerebro_rom_a_owner_episodes"})),
            ("revise", "REVISE", {"expected_owner_revision": 1}, steps(
                {"contains": "FROM cerebro_rom_a_owner_episodes", "rows": [self.row()]},
                {"contains": "UPDATE cerebro_rom_a_owner_episodes"})),
            ("read_episode", "READ", {}, self.read_steps([self.admission()])),
            ("admit", "ADMIT", {"expected_owner_revision": 1,
                                  "expected_basis_fingerprint": _sha256(self.state)}, self.admission_steps()),
        )
        for method, operation, arguments, scripted_steps in cases:
            with self.subTest(operation=operation):
                self.verifier = Verifier(self.state)
                connection = FencedConnection(scripted_steps, self.verifier)
                request = {"episode_ref": "EP-1", **arguments}
                getattr(self.port(connection), method)(**request)
                self.assertEqual(self.verifier.fence_requests, [(operation, request)])
                self.assertEqual(self.verifier.verify_requests, [(operation, request)])
                self.assertEqual(self.verifier.verified_under_fence, [True])
                events = self.verifier.events
                self.assertLess(events.index("fence-enter"), events.index("verify"))
                self.assertLess(events.index("verify"), events.index("sql"))
                self.assertLess(events.index("sql"), events.index("commit"))
                self.assertLess(events.index("commit"), events.index("fence-exit"))
                self.assertFalse(self.verifier.fence_active)
                self.assertTrue(connection.commit_called)
                self.assertFalse(connection.cursor_instance.steps)

    def test_pause_revoke_or_refall_at_fence_entry_prevents_admission(self):
        original_basis = _sha256(self.state)
        for field, value in (("paused", True), ("revoked", True), ("dependency_state", "REOPENED")):
            with self.subTest(field=field):
                self.verifier = Verifier(copy.deepcopy(self.state))
                self.verifier.on_enter = lambda verifier: verifier.state.__setitem__(field, value)
                connection = ScriptedConnection([])
                with self.assertRaisesRegex(StateConflict, "rom-a-current-owner-(basis-mismatch|gate-closed)"):
                    self.port(connection).admit(episode_ref="EP-1", expected_owner_revision=1,
                                                expected_basis_fingerprint=original_basis)
                self.assertEqual(self.verifier.state[field], value)
                self.assertEqual(self.verifier.verified_under_fence, [True])
                self.assertFalse(connection.cursor_instance.executed)
                self.assertFalse(connection.commit_called)
                self.assertFalse(self.verifier.fence_active)

    def test_fence_entry_or_verification_error_does_not_open_transaction(self):
        for stage in ("entry", "verify"):
            with self.subTest(stage=stage):
                self.verifier = Verifier(self.state)
                failure = StateConflict("synthetic-ingress-failure")
                if stage == "entry":
                    def fail_entry(_verifier):
                        raise failure
                    self.verifier.on_enter = fail_entry
                else:
                    self.verifier.verify_error = failure
                connection = ScriptedConnection([])
                with self.assertRaisesRegex(StateConflict, "synthetic-ingress-failure"):
                    self.port(connection).provision(episode_ref="EP-1")
                self.assertFalse(connection.cursor_instance.executed)
                self.assertFalse(connection.commit_called)
                self.assertFalse(self.verifier.fence_active)

    def test_fence_exit_error_cannot_return_success_after_commit(self):
        self.verifier.exit_error = StateConflict("synthetic-fence-exit-failure")
        connection = FencedConnection(steps(
            {"contains": "FROM cerebro_rom_a_owner_episodes", "rows": []},
            {"contains": "INSERT INTO cerebro_rom_a_owner_episodes"}), self.verifier)
        with self.assertRaisesRegex(StateConflict, "synthetic-fence-exit-failure"):
            self.port(connection).provision(episode_ref="EP-1")
        self.assertTrue(connection.commit_called)
        self.assertFalse(self.verifier.fence_active)

    def test_fence_cannot_swallow_verification_sql_or_commit_failure(self):
        for stage in ("verify", "sql", "commit"):
            with self.subTest(stage=stage):
                self.verifier = Verifier(self.state)
                self.verifier.swallow_error = True
                commit_error = None
                if stage == "verify":
                    self.verifier.verify_error = StateConflict("synthetic-verify-failure")
                    scripted_steps = []
                elif stage == "sql":
                    scripted_steps = steps({"contains": "FROM cerebro_rom_a_owner_episodes",
                                            "error": ScriptedDatabaseError("08006")})
                else:
                    scripted_steps = steps(
                        {"contains": "FROM cerebro_rom_a_owner_episodes", "rows": []},
                        {"contains": "INSERT INTO cerebro_rom_a_owner_episodes"})
                    commit_error = ScriptedDatabaseError("08006")
                connection = FencedConnection(scripted_steps, self.verifier, commit_error=commit_error)
                with self.assertRaisesRegex(StateServiceUnavailable, "rom-a-owner-fence-suppressed-failure"):
                    self.port(connection).provision(episode_ref="EP-1")
                self.assertEqual(connection.commit_called, stage == "commit")
                self.assertEqual(connection.rollback_called, stage != "verify")
                self.assertIn("fence-error", self.verifier.events)
                self.assertFalse(self.verifier.fence_active)

    def test_cold_read_keeps_immutable_admission_debt_after_owner_basis_drift(self):
        persisted_basis = _sha256(self.state)
        self.verifier.state = {**self.state, "paused": True, "dependency_revision": "DREV-2"}
        current_basis = _sha256(self.verifier.state)
        ledger = [self.admission("b" * 64, 1), self.admission(persisted_basis, 2)]
        connection = ScriptedConnection(self.read_steps(ledger, revision=2))
        result = self.port(connection).read_episode(episode_ref="EP-1")
        self.assertEqual((result["state"], result["currentness"]), ("OWNER_BASIS_DRIFT", "STALE"))
        self.assertEqual(result["owner_basis_fingerprint"], persisted_basis)
        self.assertEqual(result["current_owner_basis_fingerprint"], current_basis)
        self.assertEqual(result["admissions"], sorted(
            ({"basis_fingerprint": row["basis_fingerprint"], "owner_revision": row["owner_revision"],
              "state": "ADMITTED_UNCERTAIN"} for row in ledger), key=lambda row: row["basis_fingerprint"]))
        self.assertEqual(result["admitted_basis_fingerprints"], sorted(("b" * 64, persisted_basis)))
        self.assertEqual((result["delivery"], result["recipient_use"], result["effect"]),
                         ("NOT_PROVEN", "NOT_PROVEN", "NOT_PROVEN"))
        self.assertTrue(connection.commit_called)
        self.assertFalse(any(sql.startswith(("INSERT", "UPDATE", "DELETE"))
                             for sql in connection.cursor_instance.executed))

    def test_cold_read_still_requires_authenticated_current_ingress(self):
        self.verifier.authenticated = False
        connection = ScriptedConnection(self.read_steps([self.admission()]))
        with self.assertRaisesRegex(StateAuthorizationError, "authenticated-owner-ingress-required"):
            self.port(connection).read_episode(episode_ref="EP-1")
        self.assertFalse(connection.cursor_instance.executed)
        self.assertFalse(connection.commit_called)

    def test_cold_read_rejects_malformed_admission_ledger_after_drift(self):
        self.verifier.state = {**self.state, "revoked": True}
        malformed = [
            {**self.admission(), field: value}
            for field, values in (
                ("basis_fingerprint", (None, "a" * 63, "g" * 64)),
                ("owner_revision", (True, False, 0, -1, "1", 1.0, 3)),
                ("admission_state", (None, "NONE", "DELIVERED")),
            ) for value in values
        ]
        malformed.extend({key: value for key, value in self.admission().items() if key != field}
                         for field in ("basis_fingerprint", "owner_revision", "admission_state"))
        for row in malformed:
            with self.subTest(row=row):
                connection = ScriptedConnection(self.read_steps([row], revision=2))
                with self.assertRaisesRegex(StateConflict, "rom-a-persisted-admission-invalid"):
                    self.port(connection).read_episode(episode_ref="EP-1")
                self.assertFalse(connection.commit_called)
                self.assertTrue(connection.rollback_called)

    def test_cold_read_normalizes_tuple_ledger_rows_from_cursor_description(self):
        basis = _sha256(self.state)
        connection = ScriptedConnection(self.read_steps([(basis, 1, "ADMITTED_UNCERTAIN")]))
        connection.cursor_instance.description = [(name,) for name in
                                                   ("basis_fingerprint", "owner_revision", "admission_state")]
        result = self.port(connection).read_episode(episode_ref="EP-1")
        self.assertEqual(result["admissions"], [{"basis_fingerprint": basis, "owner_revision": 1,
                                                 "state": "ADMITTED_UNCERTAIN"}])
        self.assertTrue(connection.commit_called)

    def test_revise_and_admit_require_positive_strict_integer_revision(self):
        for method in ("revise", "admit"):
            for revision in (True, False, 0, -1, 1.0, "1", None):
                with self.subTest(method=method, revision=revision):
                    arguments = {"episode_ref": "EP-1", "expected_owner_revision": revision}
                    if method == "admit":
                        arguments["expected_basis_fingerprint"] = _sha256(self.state)
                    connection = ScriptedConnection([])
                    with self.assertRaisesRegex(StateConflict, "positive-integer-owner-revision-required"):
                        getattr(self.port(connection), method)(**arguments)
                    self.assertFalse(connection.cursor_instance.executed)
                    self.assertFalse(connection.commit_called)

    def test_revise_and_admit_reject_stale_owner_revision_without_writes(self):
        for method in ("revise", "admit"):
            with self.subTest(method=method):
                arguments = {"episode_ref": "EP-1", "expected_owner_revision": 1}
                if method == "admit":
                    arguments["expected_basis_fingerprint"] = _sha256(self.state)
                connection = ScriptedConnection(steps(
                    {"contains": "FROM cerebro_rom_a_owner_episodes", "rows": [self.row(2)]}))
                with self.assertRaisesRegex(StateConflict, "rom-a-owner-revision-conflict"):
                    getattr(self.port(connection), method)(**arguments)
                self.assertTrue(connection.rollback_called)
                self.assertFalse(connection.commit_called)
                self.assertFalse(any(sql.startswith(("INSERT", "UPDATE"))
                                     for sql in connection.cursor_instance.executed))

    def test_revise_persists_new_basis_observed_inside_fence(self):
        self.verifier = Verifier(copy.deepcopy(self.state))
        self.verifier.on_enter = lambda verifier: verifier.state.__setitem__("dependency_revision", "DREV-2")
        connection = FencedConnection(steps(
            {"contains": "FROM cerebro_rom_a_owner_episodes", "rows": [self.row()]},
            {"contains": "UPDATE cerebro_rom_a_owner_episodes"}), self.verifier)
        result = self.port(connection).revise(episode_ref="EP-1", expected_owner_revision=1)
        self.assertEqual((result["state"], result["owner_revision"]), ("REVISED", 2))
        update = connection.cursor_instance.bindings[-1][1]
        self.assertEqual(update[0], 2)
        self.assertEqual(json.loads(update[1]), self.verifier.state)
        self.assertEqual(result["delivery"], "NOT_ATTEMPTED")
        self.assertTrue(connection.commit_called)

    def test_commit_uncertainty_never_returns_admission_success(self):
        connection = ScriptedConnection(steps(
            {"contains": "FROM cerebro_rom_a_owner_episodes", "rows": [self.row()]},
            {"contains": "FROM cerebro_rom_a_admissions", "rows": []},
            {"contains": "INSERT INTO cerebro_rom_a_admissions"},
            {"contains": "UPDATE cerebro_rom_a_owner_episodes"}),
            commit_error=ScriptedDatabaseError("08006"))
        with self.assertRaises(StateServiceUnavailable):
            self.port(connection).admit(episode_ref="EP-1", expected_owner_revision=1,
                                        expected_basis_fingerprint=_sha256(self.state))
        self.assertTrue(connection.commit_called)
        self.assertTrue(connection.rollback_called)

    def test_schema_enforces_principal_rls_and_immutable_unique_ledger(self):
        path = ROOT / "tooling/context/control_context_state_postgres_0007_rom_a_owner_admission.sql"
        schema = path.read_text()
        self.assertIn("PRIMARY KEY (tenant_ref, workspace_ref, principal_ref, episode_ref, basis_fingerprint)", schema)
        self.assertIn("CREATE TRIGGER cerebro_rom_a_admissions_immutable", schema)
        self.assertIn("CREATE POLICY cerebro_rom_a_admission_principal_isolation", schema)
        manifest = json.loads((ROOT / "tooling/context/control_context_rom_a_owner_migrations.json").read_text())
        default = json.loads((ROOT / "tooling/context/control_context_postgres_migrations.json").read_text())
        self.assertEqual(manifest["migrations"][:5], default["migrations"])
        self.assertEqual(manifest["migrations"][-1]["checksum_sha256"],
                         hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest())
        self.assertFalse(hasattr(PostgresRomAOwnerEpisodePort, "send_selected_under_owner_fence"))

    def test_main_port_and_default_migrations_do_not_select_a1_candidate(self):
        import owner_state_persistence
        from control_context_state_postgres import DEFAULT_MANIFEST

        self.assertEqual(owner_state_persistence.OWNERS, {"project", "quality", "convergence"})
        self.assertFalse(hasattr(owner_state_persistence, "PostgresRomAOwnerEpisodePort"))
        self.assertEqual(DEFAULT_MANIFEST.name, "control_context_postgres_migrations.json")
        default = json.loads(DEFAULT_MANIFEST.read_text())
        self.assertNotIn("0007-rom-a-owner-admission",
                         {entry["migration_id"] for entry in default["migrations"]})

    def test_candidate_migration_is_separate_and_checksum_governed(self):
        names = ("cerebro_project_instances", "cerebro_actor_generation_shadow_heads",
                 "cerebro_human_t3_break_glass_heads",
                 "cerebro_principal_succession_permit_heads",
                 "cerebro_pre_role_generation_heads", "cerebro_rom_a_owner_episodes")
        migration_steps = [{"contains": "pg_advisory_xact_lock"},
                           {"contains": "CREATE TABLE IF NOT EXISTS cerebro_schema_migrations"}]
        for index, name in enumerate(names):
            migration_steps.extend((
                {"contains": "SELECT schema_version, checksum_sha256", "rows": []},
                {"contains": ("CREATE TABLE IF NOT EXISTS " if index < 5 else "CREATE TABLE ") + name},
                {"contains": "INSERT INTO cerebro_schema_migrations"},
            ))
        connection = ScriptedConnection(migration_steps)
        result = apply_postgres_migrations(
            lambda: connection,
            manifest_path=ROOT / "tooling/context/control_context_rom_a_owner_migrations.json")
        self.assertEqual(result["applied"][-1], "0007-rom-a-owner-admission")
        self.assertTrue(connection.commit_called)
        self.assertFalse(connection.cursor_instance.steps)


if __name__ == "__main__":
    unittest.main()
