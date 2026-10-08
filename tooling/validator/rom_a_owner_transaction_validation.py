#!/usr/bin/env python3
"""No-effect fault batch for the PostgreSQL Rom A admission prototype."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
for name in ("mcp", "tooling/context", "tooling/owner_state", "tooling/validator",
             "engines/project", "engines/quality", "engines/convergence"):
    sys.path.insert(0, str(ROOT / name))

from control_context_postgres_validation import ScriptedConnection, ScriptedDatabaseError
from control_context_state_postgres import apply_postgres_migrations
from control_context_state_port import StateConflict, StateServiceUnavailable
from owner_state_persistence import PostgresRomAOwnerEpisodePort, _sha256


class Verifier:
    def __init__(self, state):
        self.state = state
        self.authenticated = True

    def verify_current(self, *, operation, request):
        return {"authenticated": self.authenticated, "currentness": "CURRENT",
                "owner_ref": "A1", "human_mandate_readback": True,
                "tenant_ref": "TENANT-1", "workspace_ref": "WORKSPACE-1",
                "principal_ref": "PRINCIPAL-1", "owner_state": copy.deepcopy(self.state)}


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
        connection = ScriptedConnection(steps(
            {"contains": "FROM cerebro_rom_a_owner_episodes", "rows": [self.row()]},
            {"contains": "FROM cerebro_rom_a_admissions", "rows": [{"basis_fingerprint": basis}]}))
        result = self.port(connection).read_episode(episode_ref="EP-1")
        self.assertEqual(result["admitted_basis_fingerprints"], [basis])
        self.assertEqual(result["delivery"], "NOT_PROVEN")
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
