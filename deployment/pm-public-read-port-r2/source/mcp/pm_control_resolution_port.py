"""Constructor-bound public PM resolution over an existing open Context event.

No identity, event, profile authority, trust key or repository root comes from
the request. This port resolves and attests; existing completion remains separate.
"""
from __future__ import annotations

import copy
from pathlib import Path

from control_resolution_host import BoundControlResolutionHost


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


class PublicPmControlResolution:
    def __init__(self, *, state_port, host, attestor, attestation_verifier, root):
        require(type(host) is BoundControlResolutionHost, "pm-public-bound-host-required")
        require(host._pm_profile_verifier is not None, "pm-public-profile-verifier-required")
        require(callable(getattr(state_port, "read_open_event_binding", None)),
                "pm-public-open-event-reader-required")
        require(callable(getattr(attestor, "seal", None)), "pm-public-attestor-required")
        require(callable(getattr(attestation_verifier, "verify", None)),
                "pm-public-attestation-verifier-required")
        self.state_port = state_port
        self.host = host
        self.attestor = attestor
        self.attestation_verifier = attestation_verifier
        self.root = Path(root).resolve(strict=True)

    def _binding(self, identity, context, event_id):
        require("project_state:transition" in identity.state_scopes,
                "required-scope-missing:project_state:transition")
        binding = self.state_port.read_open_event_binding(
            tenant_ref=identity.tenant_ref, workspace_ref=identity.workspace_ref,
            principal_ref=identity.principal_ref, consumer_ref=identity.consumer_ref,
            session_ref=context.session_ref(), event_id=event_id, scopes=identity.state_scopes,
        )
        session = binding["session"]
        expected = (identity.tenant_ref, identity.workspace_ref, identity.principal_ref,
                    identity.consumer_ref, context.session_ref())
        require(tuple(session.get(k) for k in ("tenant_ref", "workspace_ref", "principal_ref",
                                               "consumer_ref", "session_ref")) == expected,
                "pm-public-persisted-identity-mismatch")
        require(binding.get("event_id") == event_id, "pm-public-event-mismatch")
        return binding

    def resolve(self, args, context):
        require(isinstance(args, dict) and set(args) == {"event_id", "request"},
                "pm-public-exact-arguments-required")
        require(isinstance(args["event_id"], str) and bool(args["event_id"].strip()),
                "pm-public-event-required")
        require(isinstance(args["request"], dict), "pm-public-request-object-required")
        context.identity.validate()
        identity = context.identity
        binding = self._binding(identity, context, args["event_id"])
        request = copy.deepcopy(args["request"])
        require(set(request) <= {"objective_ref", "consequence", "uncertainty",
                "control_context_intent_assessment", "context_navigation_candidate",
                "project_manager_governance_candidate", "project_manager_profile_binding"},
                "pm-public-first-use-review-fields-only")
        # Initial public exposure is the selected read/reconciliation task, not
        # arbitrary server-local material file access or a delivery/mutation API.
        request.update(stage="UNDERSTAND_FRAME", material=False)
        # These values are supplied only by the actual persisted Context event.
        require(not set(request).intersection({"control_context_binding", "project_bound",
                "project_ref", "session_ref", "event_id", "authoritative_source_commit"}),
                "pm-public-server-binding-injection")
        candidate = request.get("project_manager_governance_candidate")
        require(isinstance(candidate, dict) and candidate.get("event_ref") == args["event_id"],
                "pm-public-governance-event-mismatch")
        require(isinstance(request.get("project_manager_profile_binding"), dict),
                "pm-public-profile-binding-required")
        request.update(project_bound=True, control_context_binding=binding,
                       project_ref=binding["project"]["project_ref"],
                       session_ref=binding["session"]["session_ref"],
                       authoritative_source_commit=binding["project"]["source_revision"])
        # Existing canonical resolver invokes the real profile verifier/governor.
        resolution = self.host.resolve(request, root=self.root, require_git_ancestry=True)
        governance = resolution.get("project_manager_control_governance")
        require(isinstance(governance, dict) and governance.get("result") == "PASS",
                "pm-public-canonical-governance-nonpass")
        require(governance.get("event_ref") == args["event_id"]
                and governance.get("session_ref") == context.session_ref(),
                "pm-public-resolved-event-session-mismatch")
        require(self._binding(identity, context, args["event_id"]) == binding,
                "pm-public-event-changed-during-resolution")
        payload = {"event_id": args["event_id"], "resolution": resolution}
        attestation = self.attestor.seal(operation="resolve_project_manager_control_event",
                                         payload=payload, context=context)
        self.attestation_verifier.verify(operation="resolve_project_manager_control_event",
                                        payload=payload, attestation=attestation, context=context)
        # A resolution receipt cannot be presented as a completion permit.
        # Actual state writes still require the existing complete operation and
        # its separately bound attestation, normal currentness checks and CAS.
        return {"schema": "cerebro-public-pm-control-resolution/v1", **payload,
                "resolution_attestation": attestation, "repository_permission_required": False,
                "state_mutation_by_resolution": False, "completion_performed": False}
