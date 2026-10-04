#!/usr/bin/env python3
"""In-process authenticated Project A2 host custody proof; no DB or route."""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "mcp"), str(ROOT / "tooling" / "context")]

from control_context_remote_service import (  # noqa: E402
    ControlContextRemoteMcpService, RemoteMcpAuthenticationError,
    RemoteMcpServiceConfig, VerifiedBearerToken,
)
from control_context_state_port import (  # noqa: E402
    InMemoryControlContextStatePort, StateConflict,
)
from control_context_state_postgres import PostgresControlContextStatePort  # noqa: E402
from control_context_tools import (  # noqa: E402
    ControlContextMcpTools, ControlContextToolError, HmacControlResolutionAttestor,
)
from project_commissioning_candidate import (  # noqa: E402
    ProjectCommissioningBridge, ProjectCommissioningHold,
)

NOW = 2_000_000_000.0
RESOURCE = "https://mcp.cerebro.invalid"
ISSUER = "https://auth.cerebro.invalid"
ARGS = {
    "project_ref": "PROJECT", "aggregate_id": "AGG-PROJECT",
    "source_revision": "CI-SOURCE", "event_id": "BOOT-PROJECT",
    "decision_ref": "CI-X9-DECISION",
    "root": {"context_id": "ROOT-PROJECT", "human_label": "Project",
             "objective_ref": "CI-OBJECTIVE", "scope_ref": "CI-SCOPE"},
}


class BearerFixture:
    def verify(self, token):
        claims = {
            "iss": ISSUER, "aud": RESOURCE, "exp": NOW + 3600,
            "scope": "project_state:read project_state:transition",
            "sub": "P1", "cerebro_tenant": "T", "cerebro_workspace": "W",
        }
        if token == "transition-only":
            claims["scope"] = "project_state:transition"
        return VerifiedBearerToken(claims=claims, signature_verified=token in {
            "signed-fixture", "transition-only"})


class X9LineageFixture:
    """An explicitly owner-bound, current fixture; never a production X9 reader."""

    def __init__(self):
        self.owner_ref = "X9"
        self.revision = 7
        self.current_revision = 7
        self.allowed = {"PROJECT": "AGG-PROJECT"}
        self.deny = False

    def authorize(self, *, identity, project_ref, aggregate_id):
        if (self.deny or self.owner_ref != "X9" or self.revision != self.current_revision
                or self.allowed.get(project_ref) != aggregate_id
                or identity.tenant_ref != "T" or identity.workspace_ref != "W"
                or identity.principal_ref != "P1"):
            return {"result": "HOLD"}
        return {"result": "PASS", "project_ref": project_ref,
                "aggregate_id": aggregate_id, "lineage_ref": "X9-CI-REV7"}


class RecordingState(InMemoryControlContextStatePort):
    def __init__(self):
        super().__init__()
        self.bootstrap_calls = 0
        self.bind_calls = 0

    def snapshot(self):
        return copy.deepcopy((self._projects, self._sessions, self._active_projects,
                              self._bootstraps, self.bootstrap_calls, self.bind_calls))

    def bootstrap_project(self, **kwargs):
        self.bootstrap_calls += 1
        return super().bootstrap_project(**kwargs)

    def require_project_commissioning_index(self, **kwargs):
        self._require_scope(kwargs["scopes"], "project_state:read")

    def read_principal_default_binding(self, *, tenant_ref, workspace_ref,
                                       principal_ref, scopes):
        self._require_scope(scopes, "project_state:read")
        project = self._active_projects.get((tenant_ref, workspace_ref, principal_ref))
        if project is None:
            return {"status": "ABSENT"}
        return {"status": "PRESENT", "active_project_ref": project,
                "binding_revision": 1,
                "binding_fingerprint": PostgresControlContextStatePort._binding_fingerprint(
                    tenant_ref=tenant_ref, workspace_ref=workspace_ref,
                    principal_ref=principal_ref, project_ref=project, revision=1)}

    def bind_session(self, *, reject_existing=False,
                     reject_project_commissioning_existing=False, **kwargs):
        self.bind_calls += 1
        key = self._session_key(kwargs["tenant_ref"], kwargs["workspace_ref"],
                                kwargs["principal_ref"], kwargs["consumer_ref"],
                                kwargs["session_ref"])
        if reject_existing and key in self._sessions:
            raise StateConflict("commissioning-session-collision")
        if reject_project_commissioning_existing and any(
            value["project_ref"] == kwargs["project_ref"]
            and value["session_ref"].startswith("project-commissioning:")
            for value in self._sessions.values()
        ):
            raise StateConflict("commissioning-project-session-conflict")
        return super().bind_session(**kwargs)


class HostBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.state = RecordingState()
        self.attestor = HmacControlResolutionAttestor(
            key_id="CI-HOST-ONLY", secret=b"synthetic-host-fixture-secret-32-bytes-minimum")
        self.tools = ControlContextMcpTools(self.state, self.attestor)
        self.lineage = X9LineageFixture()
        self.config = RemoteMcpServiceConfig(
            resource=RESOURCE, authorization_servers=(ISSUER,),
            resource_documentation="https://docs.cerebro.invalid/project-control")
        self.state.bootstrap_project(
            tenant_ref="T", workspace_ref="W", principal_ref="P1",
            project_ref="DEFAULT", aggregate_id="AGG-DEFAULT",
            source_revision="CI-SOURCE", event_id="BOOT-DEFAULT",
            decision_ref="CI-DEFAULT-DECISION",
            root={"context_id": "ROOT-DEFAULT", "human_label": "Default",
                  "objective_ref": "CI-OBJECTIVE", "scope_ref": "CI-SCOPE"},
            scopes={"project_state:read", "project_state:transition"},
            make_default=True)
        self.before = self.state.snapshot()

    def service(self, *, bridge=True):
        project_bridge = (ProjectCommissioningBridge(
            state_port=self.state, tools=self.tools, issuer=self.attestor,
            verifier=self.attestor, lineage_authorizer=self.lineage)
            if bridge else None)
        return ControlContextRemoteMcpService(
            config=self.config, tools=self.tools, token_verifier=BearerFixture(),
            readiness_probe=lambda: True, clock=lambda: NOW,
            project_commissioning_bridge=project_bridge)

    def invoke(self, service, args=None, token="signed-fixture"):
        return service.commission_project_engine_v01(
            args=copy.deepcopy(ARGS if args is None else args),
            headers={"Authorization": "Bearer " + token})

    def assert_unchanged(self):
        self.assertEqual(self.state.snapshot(), self.before)

    def test_missing_or_wrong_project_issuer_holds_before_mutation(self):
        with self.assertRaisesRegex(ControlContextToolError, "bridge-unbound"):
            self.invoke(self.service(bridge=False))
        self.assert_unchanged()
        other_issuer = HmacControlResolutionAttestor(
            key_id="WRONG", secret=b"synthetic-other-issuer-secret-32-bytes-minimum")
        with self.assertRaisesRegex(ProjectCommissioningHold, "shared"):
            ProjectCommissioningBridge(
                state_port=self.state, tools=self.tools, issuer=other_issuer,
                verifier=self.attestor, lineage_authorizer=self.lineage)
        self.assert_unchanged()

    def test_missing_lineage_authorizer_holds_before_mutation(self):
        with self.assertRaisesRegex(ProjectCommissioningHold, "lineage-authorizer-required"):
            ProjectCommissioningBridge(
                state_port=self.state, tools=self.tools, issuer=self.attestor,
                verifier=self.attestor, lineage_authorizer=None)
        self.assert_unchanged()

    def test_denied_stale_wrong_owner_or_wrong_aggregate_holds(self):
        service = self.service()
        for variant in ("denied", "stale", "wrong-owner", "wrong-aggregate"):
            with self.subTest(variant=variant):
                self.lineage.deny = variant == "denied"
                self.lineage.revision = 6 if variant == "stale" else 7
                self.lineage.owner_ref = "OTHER" if variant == "wrong-owner" else "X9"
                args = {**ARGS, "aggregate_id": "CALLER-SUPPLIED-WRONG"} if variant == "wrong-aggregate" else ARGS
                with self.assertRaisesRegex(ProjectCommissioningHold, "project-lineage-unproven"):
                    self.invoke(service, args=args)
                self.assert_unchanged()
        self.lineage.deny = False
        self.lineage.revision = 7
        self.lineage.owner_ref = "X9"

    def test_caller_meta_or_authority_fields_cannot_substitute(self):
        service = self.service()
        for field in ("request_meta", "lineage_ref", "control_resolution_attestation",
                      "make_default", "principal_ref"):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ProjectCommissioningHold, "exact-fields"):
                    self.invoke(service, args={**ARGS, field: "CALLER"})
                self.assert_unchanged()

    def test_invalid_signature_or_missing_read_scope_holds(self):
        service = self.service()
        with self.assertRaises(RemoteMcpAuthenticationError):
            self.invoke(service, token="unsigned")
        self.assert_unchanged()
        with self.assertRaisesRegex(ProjectCommissioningHold, "scopes-required"):
            self.invoke(service, token="transition-only")
        self.assert_unchanged()

    def test_positive_exact_server_custody_reaches_context_only(self):
        service = self.service()
        default_before = self.state.read_principal_default_binding(
            tenant_ref="T", workspace_ref="W", principal_ref="P1",
            scopes={"project_state:read"})
        result = self.invoke(service)
        self.assertEqual(result["phase"], "CONTEXT_ONLY")
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(result["default_binding"], default_before)
        self.assertFalse(result["project_basis_initialized"])
        self.assertEqual(self.state.bootstrap_calls, self.before[-2] + 1)
        self.assertEqual(self.state.bind_calls, self.before[-1] + 1)
        self.assertEqual(len(self.state._sessions), 1)
        self.assertEqual(self.state.read_principal_default_binding(
            tenant_ref="T", workspace_ref="W", principal_ref="P1",
            scopes={"project_state:read"}), default_before)
        self.assertNotIn("commission_project_engine_v01",
                         {item["name"] for item in service.list_tools()})


if __name__ == "__main__":
    unittest.main(verbosity=2)
