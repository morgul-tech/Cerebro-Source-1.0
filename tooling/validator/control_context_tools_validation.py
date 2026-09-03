#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Callable

import yaml


SOURCE_ROOT = Path(__file__).resolve().parents[2]
for path in (SOURCE_ROOT / "mcp", SOURCE_ROOT / "tooling" / "context"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from control_context_registry import DIRECTIVE_SCHEMA, validate_transition_receipt  # noqa: E402
from control_context_state_port import (  # noqa: E402
    InMemoryControlContextStatePort,
    StateAuthorizationError,
    StateBindingError,
    StateConflict,
)
from control_context_tools import (  # noqa: E402
    ContextLifecycleEffectAdapter,
    ControlContextMcpTools,
    ControlContextToolAuthorizationError,
    HmacControlResolutionAttestor,
    McpToolCallContext,
    VerifiedMcpIdentity,
    tool_definitions,
)


def _expect_error(function: Callable[[], Any], expected: type[BaseException]) -> bool:
    try:
        function()
    except expected:
        return True
    return False


def _identity(*, verified: bool = True, scopes: frozenset[str] | None = None) -> VerifiedMcpIdentity:
    return VerifiedMcpIdentity(
        tenant_ref="TENANT-1",
        workspace_ref="WORKSPACE-1",
        principal_ref="OAUTH-PRINCIPAL-1",
        scopes=scopes or frozenset({"project_state:read", "project_state:transition", "repo:write"}),
        token_verified=verified,
    )


def _context(*, verified: bool = True, include_session: bool = True) -> McpToolCallContext:
    meta: dict[str, Any] = {"openai/subject": "HOST-CORRELATION-NOT-AUTHORITY"}
    if include_session:
        meta["openai/session"] = "ANON-CHAT-1"
    return McpToolCallContext(identity=_identity(verified=verified), request_meta=meta)


def _root() -> dict[str, Any]:
    return {
        "context_id": "CTX-ROOT",
        "human_label": "Hovedspor",
        "objective_ref": "OBJ-TOTAL-MCP-REVISION",
        "scope_ref": "SCOPE-TOTAL-MCP-REVISION",
        "basis_refs": ["HANDOFF-SHA256-C2F93D"],
        "project_basis_ref": "PROJECT-BASIS-TOTAL-MCP-REVISION-V1",
        "quality_trace_ref": "QUALITY-DEEP-V1",
        "completion_criteria_refs": ["V1-ACCEPTANCE"],
    }


def _navigation_candidate(binding: dict[str, Any], directive: dict[str, Any]) -> dict[str, Any]:
    project = binding["project"]
    session = binding["session"]
    continuation = session["active_continuation_binding"]
    assert isinstance(continuation, dict)
    # The no-op domain receipt is deterministic, so derive the exact expected proof.
    from control_context_registry import apply_transition

    project_after, session_after, receipt = apply_transition(project, session, directive)
    action = {
        "action_id": "HNSA-TOOL-SELFTEST",
        "surface_kind": "HNS",
        "binding_id": continuation["binding_id"],
        "alias": continuation["alias"],
        "operation": continuation["operation"],
        "target_ref": continuation["target_ref"],
        "approved_by_mcp": True,
    }
    candidate = {
        "schema": "cerebro-mcp-context-navigation-options-candidate/v1",
        "authority": "MCP",
        "state_basis": "PREDICTED_POST_COMMIT_STATE",
        "render_authorized": False,
        "activation_precondition": "ACTUAL_TRANSITION_RECEIPT_AND_COMMITTED_STATE_EXACTLY_MATCH_PREDICTION",
        "expected_transition_receipt_ref": receipt["receipt_id"],
        "expected_transition_receipt_fingerprint": receipt["receipt_fingerprint"],
        "control_decision_ref": directive["decision_ref"],
        "project_ref": project_after["project_ref"],
        "session_ref": session_after["session_ref"],
        "source_context_ref": session_after["active_context_ref"],
        "project_revision": project_after["revision"],
        "session_revision": session_after["session_revision"],
        "project_fingerprint": project_after["fingerprint"],
        "session_fingerprint": session_after["fingerprint"],
        "primary": action,
        "optional": [],
        "candidate_fingerprint": "",
    }
    subject = copy.deepcopy(candidate)
    subject.pop("candidate_fingerprint")
    candidate["candidate_fingerprint"] = hashlib.sha256(
        json.dumps(subject, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    return candidate


def _signed_args(
    attestor: HmacControlResolutionAttestor,
    operation: str,
    payload: dict[str, Any],
    context: McpToolCallContext,
) -> dict[str, Any]:
    value = copy.deepcopy(payload)
    value["control_resolution_attestation"] = attestor.seal(
        operation=operation,
        payload=payload,
        context=context,
    )
    return value


def selftest() -> dict[str, Any]:
    tests: list[dict[str, str]] = []

    def check(name: str, condition: bool) -> None:
        tests.append({"name": name, "result": "PASS" if condition else "FAIL"})

    port = InMemoryControlContextStatePort()
    attestor = HmacControlResolutionAttestor(key_id="SELFTEST-KEY", secret=b"cerebro-selftest-attestation-key-0001")
    tools = ControlContextMcpTools(port, attestor)
    context = _context()
    definitions = tool_definitions()
    serialized_definitions = json.dumps(definitions, sort_keys=True).lower()
    capability_surface = json.dumps(
        [
            {
                "name": item.get("name"),
                "description": item.get("description"),
                "inputSchema": item.get("inputSchema"),
            }
            for item in definitions
        ],
        sort_keys=True,
    ).lower()
    check(
        "tool-surface-exposes-no-repository-mutation",
        "repository" not in capability_surface
        and "github" not in capability_surface
        and '"repository_permission_required": {"const": false}' in serialized_definitions,
    )
    check(
        "tool-annotations-never-claim-destructive-delete",
        all(item.get("annotations", {}).get("destructiveHint") is False for item in definitions),
    )
    check(
        "every-tool-declares-a-bounded-structured-output-schema",
        all(
            item.get("outputSchema", {}).get("type") == "object"
            and isinstance(item.get("outputSchema", {}).get("required"), list)
            and bool(item["outputSchema"]["required"])
            for item in definitions
        ),
    )

    create_payload = {
        "project_ref": "TOTAL_MCP_REVISION",
        "aggregate_id": "AGG-TOTAL-MCP-REVISION",
        "source_revision": "b49110d16f363f58d1cd79432acb236ab3ac3014",
        "event_id": "EVENT-CREATE",
        "decision_ref": "MCPD-CREATE",
        "root": _root(),
        "make_default": True,
    }
    created = tools.dispatch(
        "create_project_control_instance",
        _signed_args(attestor, "create_project_control_instance", create_payload, context),
        context,
    )
    created_content = created["structuredContent"]
    check(
        "new-project-tool-creates-distinct-single-root-project",
        created_content["project"]["project_ref"] == "TOTAL_MCP_REVISION"
        and len(created_content["project"]["contexts"]) == 1
        and created["_meta"]["cerebro/repositoryPermissionRequired"] is False,
    )
    created_replay = tools.dispatch(
        "create_project_control_instance",
        _signed_args(attestor, "create_project_control_instance", create_payload, context),
        context,
    )
    check(
        "project-bootstrap-exact-retry-is-idempotent",
        created_replay["structuredContent"] == created_content,
    )
    conflicting_create = copy.deepcopy(create_payload)
    conflicting_create["root"]["objective_ref"] = "DIFFERENT-OBJECTIVE"
    check(
        "project-bootstrap-same-event-with-different-request-conflicts",
        _expect_error(
            lambda: tools.dispatch(
                "create_project_control_instance",
                _signed_args(
                    attestor,
                    "create_project_control_instance",
                    conflicting_create,
                    context,
                ),
                context,
            ),
            StateConflict,
        ),
    )

    begun = tools.dispatch(
        "begin_project_control_event",
        {"event_id": "EVENT-1", "idempotency_key": "IDEMPOTENCY-1"},
        context,
    )["structuredContent"]
    check(
        "R03-bound-tool-begins-project-event-before-reasoning",
        begun["schema"] == "cerebro-control-context-event-binding/v1"
        and begun["session"]["session_ref"] == "chatgpt:ANON-CHAT-1"
        and begun["session"]["principal_ref"] == "OAUTH-PRINCIPAL-1"
        and begun["repository_permission_required"] is False,
    )
    check(
        "host-subject-metadata-is-correlation-not-authorization",
        begun["session"]["principal_ref"] != context.subject_correlation,
    )

    set_binding_directive = {
        "schema": DIRECTIVE_SCHEMA,
        "event_id": "EVENT-1",
        "decision_ref": "MCPD-SET-HNS",
        "expected_project_revision": begun["expected_project_revision"],
        "expected_project_fingerprint": begun["expected_project_fingerprint"],
        "expected_session_revision": begun["expected_session_revision"],
        "expected_session_fingerprint": begun["expected_session_fingerprint"],
        "project_operations": [],
        "session_operations": [{
            "operation": "SET_CONTINUATION_BINDING",
            "binding": {
                "binding_id": "BIND-TOOLS-HNS", "surface_kind": "HNS", "alias": "Fortsett hovedsporet nå",
                "operation": "CONTINUE_CURRENT", "target_ref": "CTX-ROOT", "context_ref": "CTX-ROOT",
            },
        }],
    }
    check(
        "state-mutation-requires-event-and-identity-bound-MCP-attestation",
        _expect_error(
            lambda: tools.dispatch(
                "complete_project_control_event",
                {"event_id": "EVENT-1", "directive": set_binding_directive},
                context,
            ),
            ControlContextToolAuthorizationError,
        ),
    )
    first_completion = tools.dispatch(
        "complete_project_control_event",
        _signed_args(
            attestor,
            "complete_project_control_event",
            {"event_id": "EVENT-1", "directive": set_binding_directive, "navigation_options_candidate": None},
            context,
        ),
        context,
    )["structuredContent"]
    check(
        "completion-without-navigation-candidate-cannot-invent-HNS",
        first_completion["mcp_context_navigation_options"] is None
        and first_completion["human_navigation_surface_required"] is False,
    )

    begun = tools.dispatch(
        "begin_project_control_event",
        {"event_id": "EVENT-2", "idempotency_key": "IDEMPOTENCY-2"},
        context,
    )["structuredContent"]

    directive = {
        "schema": DIRECTIVE_SCHEMA,
        "event_id": "EVENT-2",
        "decision_ref": "MCPD-NOOP",
        "expected_project_revision": begun["expected_project_revision"],
        "expected_project_fingerprint": begun["expected_project_fingerprint"],
        "expected_session_revision": begun["expected_session_revision"],
        "expected_session_fingerprint": begun["expected_session_fingerprint"],
        "project_operations": [],
        "session_operations": [],
    }
    completed = tools.dispatch(
        "complete_project_control_event",
        _signed_args(attestor, "complete_project_control_event", {
            "event_id": "EVENT-2",
            "directive": directive,
            "navigation_options_candidate": _navigation_candidate(begun, directive),
        }, context),
        context,
    )["structuredContent"]
    check(
        "bound-tool-completes-event-with-valid-noop-receipt",
        completed["result"] == "PASS"
        and validate_transition_receipt(completed["receipt"])["mutated"] is False
        and completed["mcp_context_navigation_options"]["commit_verified"] is True
        and completed["mcp_context_navigation_options"]["commit_receipt_ref"] == completed["receipt"]["receipt_id"]
        and completed["human_navigation_surface_required"] is True,
    )
    begun_mismatch = tools.dispatch(
        "begin_project_control_event",
        {"event_id": "EVENT-3", "idempotency_key": "IDEMPOTENCY-3"},
        context,
    )["structuredContent"]
    mismatch_directive = {
        "schema": DIRECTIVE_SCHEMA,
        "event_id": "EVENT-3",
        "decision_ref": "MCPD-MISMATCH",
        "expected_project_revision": begun_mismatch["expected_project_revision"],
        "expected_project_fingerprint": begun_mismatch["expected_project_fingerprint"],
        "expected_session_revision": begun_mismatch["expected_session_revision"],
        "expected_session_fingerprint": begun_mismatch["expected_session_fingerprint"],
        "project_operations": [],
        "session_operations": [],
    }
    mismatched_candidate = _navigation_candidate(begun_mismatch, mismatch_directive)
    mismatched_candidate["expected_transition_receipt_ref"] = "CTR-000000000000000000000000"
    check(
        "precommit-navigation-candidate-requires-exact-actual-receipt",
        (
            lambda mismatch_completion: (
                mismatch_completion["result"] == "PASS"
                and mismatch_completion["navigation_activation"]["result"] == "BLOCK"
                and mismatch_completion["navigation_activation"]["state_commit_remains_valid"] is True
                and mismatch_completion["mcp_context_navigation_options"] is None
                and mismatch_completion["human_navigation_surface_required"] is False
            )
        )(
            tools.dispatch(
                "complete_project_control_event",
                _signed_args(attestor, "complete_project_control_event", {
                    "event_id": "EVENT-3", "directive": mismatch_directive,
                    "navigation_options_candidate": mismatched_candidate,
                }, context),
                context,
            )["structuredContent"]
        ),
    )
    tools.dispatch(
        "create_project_control_instance",
        _signed_args(attestor, "create_project_control_instance", {
            "project_ref": "SECOND_PROJECT",
            "aggregate_id": "AGG-SECOND-PROJECT",
            "source_revision": "b49110d16f363f58d1cd79432acb236ab3ac3014",
            "event_id": "EVENT-CREATE-SECOND",
            "decision_ref": "MCPD-CREATE-SECOND",
            "root": {**_root(), "context_id": "CTX-SECOND-ROOT"},
            "make_default": False,
        }, context),
        context,
    )
    check(
        "existing-control-session-cannot-silently-switch-project-on-begin",
        _expect_error(
            lambda: tools.dispatch(
                "begin_project_control_event",
                {"event_id": "EVENT-WRONG-PROJECT", "idempotency_key": "IDEM-WRONG", "project_ref": "SECOND_PROJECT"},
                context,
            ),
            StateBindingError,
        ),
    )

    read = tools.dispatch(
        "read_project_control_state",
        {"project_ref": "TOTAL_MCP_REVISION"},
        context,
    )["structuredContent"]
    check("read-tool-uses-authenticated-workspace", read["project"]["tenant_ref"] == "TENANT-1")
    check(
        "tool-arguments-cannot-override-authenticated-identity",
        _expect_error(
            lambda: tools.dispatch(
                "read_project_control_state",
                {"project_ref": "TOTAL_MCP_REVISION", "principal_ref": "ATTACKER"},
                context,
            ),
            ControlContextToolAuthorizationError,
        ),
    )
    check(
        "missing-stable-host-session-blocks-session-bound-tool",
        _expect_error(
            lambda: tools.dispatch(
                "begin_project_control_event",
                {"event_id": "EVENT-NO-SESSION", "idempotency_key": "IDEM-NO-SESSION"},
                _context(include_session=False),
            ),
            ControlContextToolAuthorizationError,
        ),
    )
    check(
        "unverified-OAuth-context-is-rejected",
        _expect_error(
            lambda: tools.dispatch(
                "read_project_control_state",
                {"project_ref": "TOTAL_MCP_REVISION"},
                _context(verified=False),
            ),
            ControlContextToolAuthorizationError,
        ),
    )
    read_only_context = McpToolCallContext(
        identity=_identity(scopes=frozenset({"project_state:read"})),
        request_meta={"openai/session": "READ-ONLY"},
    )
    check(
        "begin-tool-requires-bounded-transition-scope",
        _expect_error(
            lambda: tools.dispatch(
                "begin_project_control_event",
                {"event_id": "EVENT-READ-ONLY", "idempotency_key": "IDEM-READ-ONLY"},
                read_only_context,
            ),
            StateAuthorizationError,
        ),
    )
    other_identity = VerifiedMcpIdentity(
        tenant_ref="TENANT-OTHER",
        workspace_ref="WORKSPACE-1",
        principal_ref="OAUTH-PRINCIPAL-1",
        scopes=frozenset({"project_state:read"}),
        token_verified=True,
    )
    check(
        "tenant-boundary-does-not-leak-project-state",
        _expect_error(
            lambda: tools.dispatch(
                "read_project_control_state",
                {"project_ref": "TOTAL_MCP_REVISION"},
                McpToolCallContext(identity=other_identity, request_meta={"openai/session": "OTHER"}),
            ),
            StateBindingError,
        ),
    )


    # Packet554: existing complete tool is the bounded lifecycle-effect consumer.
    from control_context_registry import bootstrap_actor_generation_shadow
    import project_manager_control_governor

    class LifecycleProfileVerifier:
        def verify(self, *, binding: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
            return {
                "schema": "cerebro-project-manager-profile-verification/v1",
                "result": "PASS",
                "profile": "PROJECT_MANAGER",
                "session_ref": session["session_ref"],
                "binding_fingerprint": "a" * 64,
                "verifier_ref": "LIFECYCLE-TOOLS-SELFTEST",
            }

    class LifecycleFixturePort:
        def __init__(self):
            self.inner = InMemoryControlContextStatePort()
            self.commit_evidence: dict[str, dict[str, Any]] = {}

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

        def complete_event(self, request: dict[str, Any], *, scopes: set[str]) -> dict[str, Any]:
            completion = self.inner.complete_event(request, scopes=scopes)
            directive = copy.deepcopy(request["directive"])
            commit_fingerprint = hashlib.sha256(
                json.dumps(
                    directive,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()
            commit_ref = "SSC-LIFECYCLE-" + commit_fingerprint[:16].upper()
            commit = {"commit_ref": commit_ref, "commit_fingerprint": commit_fingerprint}
            completion["state_commit"] = copy.deepcopy(commit)
            self.commit_evidence[commit_ref] = {
                "schema": "cerebro-state-service-commit-evidence-bundle/v1",
                "commit": copy.deepcopy(commit),
                "directive": directive,
            }
            return completion

        def read_state_commit_evidence(self, *, commit_ref: str, **_: Any) -> dict[str, Any]:
            if commit_ref not in self.commit_evidence:
                raise StateBindingError("state-commit-evidence-not-found")
            return copy.deepcopy(self.commit_evidence[commit_ref])

    lifecycle_port = LifecycleFixturePort()
    lifecycle_attestor = HmacControlResolutionAttestor(
        key_id="LIFECYCLE-TOOLS-SELFTEST",
        secret=b"lifecycle-tools-selftest-attestation-0001",
    )
    lifecycle_adapter = ContextLifecycleEffectAdapter(
        lifecycle_port,
        LifecycleProfileVerifier(),
    )
    lifecycle_tools = ControlContextMcpTools(
        lifecycle_port,
        lifecycle_attestor,
        lifecycle_effect_adapter=lifecycle_adapter,
    )
    lifecycle_context = _context()
    lifecycle_tools.dispatch(
        "create_project_control_instance",
        _signed_args(
            lifecycle_attestor,
            "create_project_control_instance",
            create_payload,
            lifecycle_context,
        ),
        lifecycle_context,
    )
    lifecycle_begin = lifecycle_tools.dispatch(
        "begin_project_control_event",
        {"event_id": "EVENT-LIFECYCLE", "idempotency_key": "IDEMPOTENCY-LIFECYCLE"},
        lifecycle_context,
    )["structuredContent"]
    shadow = bootstrap_actor_generation_shadow(
        tenant_ref="TENANT-1",
        workspace_ref="WORKSPACE-1",
        actor_ref="W-LIFECYCLE",
        role="WORKER",
        generation_ref="W-LIFECYCLE",
        source_revision="1" * 40,
    )
    lifecycle_port.write_actor_generation_shadow(
        shadow,
        expected_revision=0,
        scopes={"project_state:transition"},
    )
    lifecycle_candidate = {
        "schema": "cerebro-actor-lifecycle-mutation/v1",
        "mutation_id": "MUT-LIFECYCLE-1",
        "idempotency_key": "IDEMPOTENCY-LIFECYCLE-1",
        "operation": "BIND",
        "actor_generation_id": "W-LIFECYCLE",
        "slot_pointer_ref": "READY_QUEUE:LIFECYCLE",
        "expected_slot_pointer": "W-LIFECYCLE",
        "observed_slot_pointer": "W-LIFECYCLE",
        "expected_lifecycle_revision": 1,
        "expected_lifecycle_state": "REQUALIFICATION_REQUIRED",
        "expected_claim_revision": "NOT_APPLICABLE",
        "packet_ref": "WORK_PACKETS:534",
        "claim_ref": None,
        "authority_source": "PROJECT_MANAGER+MCP",
        "observed_event_frontier": 3988,
        "source_transition": {
            "intent": "SAME_GENERATION_SOURCE_REQUALIFICATION",
            "previous_source_head": "1" * 40,
            "target_source_head": "2" * 40,
            "changed_contracts": ["mcp/control-context"],
            "gates_rerun": ["SOURCE", "CURRENTNESS", "ROLE", "INTEGRITY"],
            "prerequisite_fingerprint": "3" * 64,
            "boot_fingerprint": "4" * 64,
            "current_source_verified": True,
            "actor_nonretired_verified": True,
            "no_active_claim_verified": True,
            "generation_pointer_verified": True,
        },
    }
    lifecycle_candidate["candidate_fingerprint"] = hashlib.sha256(
        json.dumps(
            lifecycle_candidate,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    lifecycle_directive = {
        "schema": DIRECTIVE_SCHEMA,
        "event_id": "EVENT-LIFECYCLE",
        "decision_ref": "MCPD-LIFECYCLE",
        "expected_project_revision": lifecycle_begin["expected_project_revision"],
        "expected_project_fingerprint": lifecycle_begin["expected_project_fingerprint"],
        "expected_session_revision": lifecycle_begin["expected_session_revision"],
        "expected_session_fingerprint": lifecycle_begin["expected_session_fingerprint"],
        "project_operations": [],
        "session_operations": [],
    }
    lifecycle_payload = {
        "event_id": "EVENT-LIFECYCLE",
        "directive": lifecycle_directive,
        "navigation_options_candidate": None,
        "actor_lifecycle_mutation_candidate": lifecycle_candidate,
    }
    lifecycle_completion = lifecycle_tools.dispatch(
        "complete_project_control_event",
        _signed_args(
            lifecycle_attestor,
            "complete_project_control_event",
            lifecycle_payload,
            lifecycle_context,
        ),
        lifecycle_context,
    )["structuredContent"]
    lifecycle_evidence = lifecycle_completion["actor_lifecycle_effect_evidence"]
    lifecycle_post = lifecycle_port.read_actor_generation_shadow(
        tenant_ref="TENANT-1",
        workspace_ref="WORKSPACE-1",
        role="WORKER",
        generation_ref="W-LIFECYCLE",
        scopes={"project_state:read"},
    )
    check(
        "P554-existing-complete-tool-produces-derived-ready-current-evidence",
        lifecycle_evidence["post_lifecycle_state"] == "READY_CURRENT"
        and lifecycle_evidence["post_source_head"] == "2" * 40
        and lifecycle_evidence["provider_revision"] == 2,
    )
    check(
        "P554-shadow-remains-READY-and-SHADOW_ONLY",
        lifecycle_post["authority"] == "SHADOW_ONLY"
        and lifecycle_post["lifecycle"] == "READY"
        and lifecycle_post["source_revision"] == "2" * 40,
    )
    verified_candidate = copy.deepcopy(lifecycle_candidate)
    verified_candidate["effect_evidence"] = copy.deepcopy(lifecycle_evidence)
    verified_candidate["candidate_fingerprint"] = hashlib.sha256(
        json.dumps(
            {key: value for key, value in verified_candidate.items() if key != "candidate_fingerprint"},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    lifecycle_gate = project_manager_control_governor._lifecycle_mutation_gate(
        {"lifecycle_mutation": verified_candidate},
        lifecycle_effect_verifier=lifecycle_adapter,
        session=lifecycle_begin["session"],
    )
    check(
        "P554-governor-accepts-only-after-constructor-bound-durable-readback",
        lifecycle_gate["result"] == "PASS_EFFECT_VERIFIED"
        and lifecycle_gate["ready_effect_allowed"] is True,
    )
    replay_evidence = lifecycle_adapter.execute_lifecycle_effect(
        candidate=lifecycle_candidate,
        context=lifecycle_context,
        completion=lifecycle_completion,
        bound_directive={
            "actor_lifecycle_mutation_candidate_fingerprint":
                lifecycle_adapter._pre_effect_fingerprint(lifecycle_candidate)
        },
    )
    check(
        "P554-exact-replay-is-idempotent-no-second-shadow-revision",
        replay_evidence == lifecycle_evidence
        and lifecycle_port.read_actor_generation_shadow(
            tenant_ref="TENANT-1",
            workspace_ref="WORKSPACE-1",
            role="WORKER",
            generation_ref="W-LIFECYCLE",
            scopes={"project_state:read"},
        )["revision"] == 2,
    )
    check(
        "P554-unbound-adapter-fails-closed",
        _expect_error(
            lambda: ControlContextMcpTools(
                LifecycleFixturePort(),
                lifecycle_attestor,
            ).dispatch(
                "complete_project_control_event",
                _signed_args(
                    lifecycle_attestor,
                    "complete_project_control_event",
                    lifecycle_payload,
                    lifecycle_context,
                ),
                lifecycle_context,
            ),
            ControlContextToolAuthorizationError,
        ),
    )
    stale_candidate = copy.deepcopy(lifecycle_candidate)
    stale_candidate["mutation_id"] = "MUT-LIFECYCLE-STALE"
    stale_candidate["expected_lifecycle_revision"] = 2
    stale_candidate["candidate_fingerprint"] = hashlib.sha256(
        json.dumps(
            {key: value for key, value in stale_candidate.items() if key != "candidate_fingerprint"},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    check(
        "P554-stale-shadow-source-or-revision-blocks",
        _expect_error(
            lambda: lifecycle_adapter.execute_lifecycle_effect(
                candidate=stale_candidate,
                context=lifecycle_context,
                completion=lifecycle_completion,
                bound_directive={
                    "actor_lifecycle_mutation_candidate_fingerprint":
                        lifecycle_adapter._pre_effect_fingerprint(stale_candidate)
                },
            ),
            ControlContextToolAuthorizationError,
        ),
    )
    check(
        "P554-no-new-public-MCP-tool",
        [item["name"] for item in tool_definitions()] == [
            "read_project_control_state",
            "begin_project_control_event",
            "complete_project_control_event",
            "create_project_control_instance",
            "set_default_project_control_instance",
        ],
    )

    manifest = yaml.safe_load((SOURCE_ROOT / "mcp/manifest.yaml").read_text(encoding="utf-8"))
    adapter = manifest["control_adapters"]["project_control_context"]
    check(
        "R56-local-host-harness-cannot-claim-remote-project-control-enforced",
        adapter["status"]
        == "CANDIDATE_DURABLE_ADAPTER_LOCAL_RUNTIME_ASSEMBLED_NOT_DEPLOYED"
        and adapter["local_normal_host_harness_contract_proven"] is True
        and adapter["local_official_MCP_SDK_protocol_proven"] is True
        and adapter["remote_transport_and_durable_backend_deployed"] is False
        and adapter["local_contract_evidence_is_remote_activation"] is False
        and adapter["activation_claim_before_both_proofs"] == "PROHIBITED",
    )
    return {
        "schema": "cerebro-control-context-tools-selftest/v1",
        "result": "PASS" if all(item["result"] == "PASS" for item in tests) else "FAIL",
        "test_count": len(tests),
        "failures": [item for item in tests if item["result"] != "PASS"],
        "tests": tests,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="?", choices=["selftest"], default="selftest")
    parser.add_argument("--output")
    args = parser.parse_args()
    try:
        result = selftest()
    except Exception as exc:
        result = {"result": "BLOCK", "error": str(exc)}
    text = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0 if result.get("result") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
