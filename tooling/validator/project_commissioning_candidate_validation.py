#!/usr/bin/env python3
"""Offline negative checks for the default-off Project A2 candidate."""

from __future__ import annotations

import copy
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT / "mcp", ROOT / "tooling" / "context"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from control_context_state_postgres import (  # noqa: E402
    PostgresControlContextStatePort, StateBindingError, StateConflict,
    StatePortError, _mapped_database_error,
)
from control_context_tools import (  # noqa: E402
    ControlContextToolAuthorizationError, VerifiedMcpIdentity, tool_definitions,
)
from project_commissioning_candidate import (  # noqa: E402
    ProjectCommissioningBridge, ProjectCommissioningHold,
)


class FixtureAttestor:
    def seal(self, *, operation, payload, context):
        if operation != "create_project_control_instance":
            raise AssertionError("unexpected operation")
        return {"fixture_only": True, "operation": operation,
                "session_ref": context.session_ref(), "payload": copy.deepcopy(payload)}

    def verify(self, *, operation, payload, attestation, context):
        if attestation != self.seal(operation=operation, payload=payload, context=context):
            raise AssertionError("fixture-attestation-mismatch")


class FixtureLineage:
    def authorize(self, *, identity, project_ref, aggregate_id):
        return {"result": "PASS", "project_ref": project_ref,
                "aggregate_id": aggregate_id, "lineage_ref": "X9-OFFLINE-FIXTURE"}


class FixtureState:
    def __init__(self):
        self.project = None
        self.sessions = {}
        self.default = {"status": "ABSENT"}
        self.default_mutates = False
        self.stale_session = False
        self.project_drift = False
        self.bootstrap_payload = None
        self.index_installed = True
        self.hidden_bind_conflict = False

    def require_project_commissioning_index(self, **kw):
        if not self.index_installed:
            raise StateBindingError("commissioning-project-unique-index-unproven")

    def read_principal_default_binding(self, **kw):
        if self.default_mutates and self.project is not None:
            return {"status": "PRESENT", "active_project_ref": "OTHER",
                    "binding_revision": 1, "binding_fingerprint": "f" * 64}
        return copy.deepcopy(self.default)

    def read_project(self, **kw):
        if self.project is None or kw["project_ref"] != self.project["project_ref"]:
            raise StateBindingError("project-instance-not-found")
        value = copy.deepcopy(self.project)
        if self.project_drift:
            value["fingerprint"] = "DRIFT"
        return value

    def read_session(self, **kw):
        key = (kw["principal_ref"], kw["session_ref"])
        if key not in self.sessions:
            raise StateBindingError("control-session-not-bound")
        value = copy.deepcopy(self.sessions[key])
        if self.stale_session:
            value["project_revision"] = 0
        return value

    def bind_session(self, **kw):
        key = (kw["principal_ref"], kw["session_ref"])
        if self.hidden_bind_conflict:
            raise StateConflict("commissioning-project-session-conflict")
        if kw["reject_project_commissioning_existing"] and any(
                value.get("project_ref") == kw["project_ref"]
                and value.get("session_ref", "").startswith("project-commissioning:")
                for value in self.sessions.values()):
            raise StateConflict("commissioning-project-session-already-bound")
        if key in self.sessions and kw["reject_existing"]:
            raise StateBindingError("commissioning-session-collision")
        self.sessions[key] = {
            "tenant_ref": kw["tenant_ref"], "workspace_ref": kw["workspace_ref"],
            "principal_ref": kw["principal_ref"], "consumer_ref": kw["consumer_ref"],
            "session_ref": kw["session_ref"], "session_binding_id": kw["session_binding_id"],
            "project_ref": kw["project_ref"], "project_revision": self.project["revision"],
            "session_revision": 1, "fingerprint": "SESSION-FP",
        }
        return copy.deepcopy(self.sessions[key])


class FixtureTools:
    def __init__(self, state, attestor):
        self.state = state
        self.attestor = attestor

    def create_project_control_instance(self, args, context):
        payload = {k: copy.deepcopy(v) for k, v in args.items()
                   if k != "control_resolution_attestation"}
        self.attestor.verify(
            operation="create_project_control_instance", payload=payload,
            attestation=args["control_resolution_attestation"], context=context,
        )
        assert payload["make_default"] is False
        if self.state.project is not None:
            if payload != self.state.bootstrap_payload:
                raise StateBindingError("project-bootstrap-replayed-with-different-request")
            return {"structuredContent": {"project": copy.deepcopy(self.state.project),
                                          "receipt": {"fixture_only": True}}}
        project = {"project_ref": payload["project_ref"],
                   "aggregate_id": payload["aggregate_id"],
                   "revision": 1, "fingerprint": "PROJECT-FP"}
        self.state.project = copy.deepcopy(project)
        self.state.bootstrap_payload = copy.deepcopy(payload)
        return {"structuredContent": {"project": project, "receipt": {"fixture_only": True}}}


def identity(principal="P"):
    return VerifiedMcpIdentity(
        tenant_ref="T", workspace_ref="W", principal_ref=principal,
        scopes=frozenset({"project_state:read", "project_state:transition"}),
        token_verified=True,
    )


def start_args():
    return {"project_ref": "PROJECT", "aggregate_id": "AGG-PROJECT",
            "source_revision": "SOURCE", "event_id": "EVENT", "decision_ref": "DECISION",
            "root": {"context_id": "ROOT"}}


class ProjectCommissioningCandidateTests(unittest.TestCase):
    def setUp(self):
        self.state = FixtureState()
        self.attestor = FixtureAttestor()
        self.bridge = ProjectCommissioningBridge(
            state_port=self.state, tools=FixtureTools(self.state, self.attestor),
            issuer=self.attestor, verifier=self.attestor,
            lineage_authorizer=FixtureLineage(),
        )

    def test_start_and_resume(self):
        result = self.bridge.start(identity=identity(), args=start_args())
        self.assertEqual(result["phase"], "CONTEXT_ONLY")
        self.assertFalse(result["project_basis_initialized"])
        self.assertNotIn("signature", str(result))
        self.assertNotIn("attestation", str(result))
        resumed = self.bridge.resume(identity=identity(), args={
            "project_ref": "PROJECT", "aggregate_id": "AGG-PROJECT",
            "session_handle": result["session_handle"],
        })
        self.assertEqual(resumed["result"], "PASS")
        self.assertNotIn("commission_project_engine_v01",
                         {item["name"] for item in tool_definitions()})

    def test_distinct_signer_rejected(self):
        with self.assertRaisesRegex(ProjectCommissioningHold, "shared"):
            ProjectCommissioningBridge(
                state_port=self.state, tools=FixtureTools(self.state, self.attestor),
                issuer=FixtureAttestor(), verifier=self.attestor,
                lineage_authorizer=FixtureLineage(),
            )

    def test_default_binding_reader_checks_fingerprint_and_absence(self):
        class Cursor:
            row = None
            def execute(self, sql, params):
                assert "cerebro_principal_project_bindings" in sql
                assert params == ("T", "W", "P")
            def fetchone(self):
                return self.row
        cursor = Cursor()
        port = object.__new__(PostgresControlContextStatePort)
        @contextmanager
        def transaction(**kwargs):
            assert kwargs["read_only"] is True
            yield cursor
        port._transaction = transaction
        scope = {"tenant_ref": "T", "workspace_ref": "W", "principal_ref": "P",
                 "scopes": {"project_state:read"}}
        self.assertEqual(port.read_principal_default_binding(**scope), {"status": "ABSENT"})
        fingerprint = port._binding_fingerprint(
            tenant_ref="T", workspace_ref="W", principal_ref="P",
            project_ref="OLD", revision=2)
        cursor.row = {"active_project_ref": "OLD", "binding_revision": 2,
                      "binding_fingerprint": fingerprint}
        self.assertEqual(port.read_principal_default_binding(**scope)["status"], "PRESENT")
        cursor.row["binding_fingerprint"] = "0" * 64
        with self.assertRaisesRegex(StatePortError, "fingerprint-mismatch"):
            port.read_principal_default_binding(**scope)

    def test_unverified_identity_rejected(self):
        bad = VerifiedMcpIdentity(
            tenant_ref="T", workspace_ref="W", principal_ref="P",
            scopes=frozenset({"project_state:read", "project_state:transition"}),
            token_verified=False,
        )
        with self.assertRaisesRegex(ControlContextToolAuthorizationError,
                                    "verified-OAuth-identity-required"):
            self.bridge.start(identity=bad, args=start_args())

    def test_caller_authority_fields_rejected(self):
        for field in ("session_ref", "session_binding_id", "control_resolution_attestation",
                      "operation", "make_default"):
            with self.subTest(field=field), self.assertRaisesRegex(
                    ProjectCommissioningHold, "exact-fields"):
                self.bridge.start(identity=identity(), args={**start_args(), field: "CALLER"})

    def test_project_collision_rejected(self):
        self.state.project = {"project_ref": "PROJECT", "aggregate_id": "AGG-PROJECT",
                              "revision": 1, "fingerprint": "OLD"}
        with self.assertRaisesRegex(ProjectCommissioningHold, "project-ref-collision"):
            self.bridge.start(identity=identity(), args=start_args())

    def test_missing_project_index_holds_before_bootstrap(self):
        self.state.index_installed = False
        with self.assertRaisesRegex(StateBindingError, "unique-index-unproven"):
            self.bridge.start(identity=identity(), args=start_args())
        self.assertIsNone(self.state.project)

    def test_hidden_db_unique_conflict_returns_no_handle(self):
        self.state.hidden_bind_conflict = True
        with self.assertRaisesRegex(ProjectCommissioningHold, "session-conflict"):
            self.bridge.start(identity=identity(), args=start_args())
        self.assertIsNotNone(self.state.project)  # Context-only partial state
        self.assertEqual(self.state.sessions, {})

    def test_recovery_requires_exact_existing_bootstrap(self):
        result = self.bridge.start(identity=identity(), args=start_args())
        with self.assertRaisesRegex(ProjectCommissioningHold, "session-conflict"):
            self.bridge.start(identity=identity(), args={
                **start_args(), "recover_partial": True})
        self.state.sessions.clear()  # offline simulation: bootstrap committed, bind did not
        result = self.bridge.start(identity=identity(), args={
            **start_args(), "recover_partial": True})
        self.assertEqual(result["phase"], "RECOVERED_CONTEXT_ONLY")
        self.state.sessions.clear()
        with self.assertRaisesRegex(ProjectCommissioningHold, "unknown-no-auto-retry"):
            self.bridge.start(identity=identity(), args={
                **start_args(), "source_revision": "CHANGED", "recover_partial": True})

    def test_server_handle_collision_holds_before_bootstrap(self):
        handle = "A" * 43
        self.state.sessions[("P", "project-commissioning:" + handle)] = {
            "session_binding_id": "OTHER", "session_ref": "project-commissioning:" + handle}
        with patch("project_commissioning_candidate.secrets.token_urlsafe", return_value=handle):
            with self.assertRaises(ProjectCommissioningHold):
                self.bridge.start(identity=identity(), args=start_args())
        self.assertIsNone(self.state.project)

    def test_second_principal_consumer_conflicts_for_same_project(self):
        self.bridge.start(identity=identity(), args=start_args())
        with self.assertRaisesRegex(StateConflict, "session-already-bound"):
            self.state.bind_session(
                tenant_ref="T", workspace_ref="W", principal_ref="OTHER",
                consumer_ref="OTHER_CONSUMER", session_ref="project-commissioning:" + "B" * 43,
                session_binding_id="OTHER-BINDING", project_ref="PROJECT",
                reject_existing=True, reject_project_commissioning_existing=True,
            )
        self.state.project = {"project_ref": "OTHER_PROJECT", "aggregate_id": "OTHER_AGG",
                              "revision": 1, "fingerprint": "OTHER_FP"}
        other = self.state.bind_session(
            tenant_ref="T", workspace_ref="W", principal_ref="OTHER",
            consumer_ref="OTHER_CONSUMER", session_ref="project-commissioning:" + "C" * 43,
            session_binding_id="OTHER-BINDING-2", project_ref="OTHER_PROJECT",
            reject_existing=True, reject_project_commissioning_existing=True,
        )
        self.assertEqual(other["project_ref"], "OTHER_PROJECT")
        normal = self.state.bind_session(
            tenant_ref="T", workspace_ref="W", principal_ref="OTHER",
            consumer_ref="OTHER_CONSUMER", session_ref="ordinary-session",
            session_binding_id="ORDINARY-BINDING", project_ref="PROJECT",
            reject_existing=False, reject_project_commissioning_existing=False,
        )
        self.assertEqual(normal["project_ref"], "PROJECT")

    def test_unique_index_violation_is_typed(self):
        class Diagnostic:
            constraint_name = "cerebro_one_project_commissioning_session"
        class UniqueError(Exception):
            sqlstate = "23505"
            diag = Diagnostic()
        self.assertEqual(str(_mapped_database_error(UniqueError())),
                         "commissioning-project-session-conflict")

    def test_default_mutation_holds(self):
        self.state.default_mutates = True
        with self.assertRaisesRegex(ProjectCommissioningHold, "PARTIAL_CONTEXT_ONLY"):
            self.bridge.start(identity=identity(), args=start_args())

    def test_provider_drift_holds(self):
        self.state.project_drift = True
        with self.assertRaisesRegex(ProjectCommissioningHold, "PARTIAL_CONTEXT_ONLY"):
            self.bridge.start(identity=identity(), args=start_args())

    def test_stale_session_holds(self):
        self.state.stale_session = True
        with self.assertRaisesRegex(ProjectCommissioningHold, "PARTIAL_CONTEXT_ONLY"):
            self.bridge.start(identity=identity(), args=start_args())

    def test_wrong_principal_and_project_resume_rejected(self):
        result = self.bridge.start(identity=identity(), args=start_args())
        selector = {"project_ref": "PROJECT", "aggregate_id": "AGG-PROJECT",
                    "session_handle": result["session_handle"]}
        with self.assertRaises(StateBindingError):
            self.bridge.resume(identity=identity("OTHER"), args=selector)
        with self.assertRaisesRegex(ProjectCommissioningHold, "project-lineage-unproven|project-resume"):
            self.bridge.resume(identity=identity(), args={**selector, "project_ref": "OTHER"})

    def test_reused_handle_binding_mismatch_rejected(self):
        result = self.bridge.start(identity=identity(), args=start_args())
        key = ("P", "project-commissioning:" + result["session_handle"])
        self.state.sessions[key]["session_binding_id"] = "SPOOF"
        with self.assertRaisesRegex(ProjectCommissioningHold, "server-session-readback-mismatch"):
            self.bridge.resume(identity=identity(), args={
                "project_ref": "PROJECT", "aggregate_id": "AGG-PROJECT",
                "session_handle": result["session_handle"]})


if __name__ == "__main__":
    unittest.main()
