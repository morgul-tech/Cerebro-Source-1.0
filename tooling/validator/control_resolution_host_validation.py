#!/usr/bin/env python3
"""Contract validation for trusted normal-host dependency binding and sequence."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Callable


SOURCE_ROOT = Path(__file__).resolve().parents[2]
if str(SOURCE_ROOT / "mcp") not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT / "mcp"))

import adaptive_control_resolver  # noqa: E402
from control_owner_effect_receipt import build_owner_effect_receipt  # noqa: E402
from control_owner_routing import _consolidation_fixture  # noqa: E402
from control_resolution_host import (  # noqa: E402
    BoundControlResolutionHost,
    BoundRuntimeCapabilityResolver,
    CompositeOwnerPersistenceVerifier,
    ControlResolutionHostError,
    PM_AUTHORIZED_COMMAND_STATE_SCHEMA,
    consume_pm_authorized_command,
)


def _expect_error(function: Callable[[], Any], expected: type[BaseException]) -> bool:
    try:
        function()
    except expected:
        return True
    return False


class FixtureVerifier:
    """Selftest-only durable subject registry, never production evidence."""

    def __init__(self, owner: str):
        self.owner = owner
        self.subjects: dict[str, dict[str, Any]] = {}

    def register(self, receipt: dict[str, Any]) -> None:
        self.subjects[receipt["persistence_evidence_ref"]] = copy.deepcopy(receipt)

    def verify(self, *, receipt: dict[str, Any]) -> dict[str, Any]:
        expected = self.subjects.get(receipt.get("persistence_evidence_ref"))
        if expected != receipt or receipt.get("owner") != self.owner:
            raise ValueError("fixture-durable-subject-mismatch")
        return {
            "schema": "cerebro-owner-state-persistence-verification/v1",
            "result": "PASS",
            "verifier_ref": f"FIXTURE-{self.owner.upper()}-VERIFIER",
            "owner": self.owner,
            "owner_effect_receipt_ref": receipt["receipt_ref"],
            "owner_effect_receipt_fingerprint": receipt["receipt_fingerprint"],
            "persistence_evidence_ref": receipt["persistence_evidence_ref"],
            "output_state_ref": receipt["output_state_ref"],
            "output_state_fingerprint": receipt["output_state_fingerprint"],
        }


class FixtureExecutor:
    def __init__(self, owner: str, verifier: FixtureVerifier, call_order: list[str]):
        self.owner = owner
        self.verifier = verifier
        self.call_order = call_order
        self.calls: list[dict[str, Any]] = []

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        effect = kwargs["owner_effect"]
        decision = kwargs["control_decision"]
        consolidation = kwargs["consolidation_result"]
        prerequisites = kwargs["prerequisite_receipt_refs"]
        if effect["owner"] != self.owner:
            raise ValueError("fixture-owner-mismatch")
        self.call_order.append(self.owner)
        self.calls.append(copy.deepcopy(kwargs))
        input_fingerprint = hashlib.sha256(f"{self.owner}:input".encode()).hexdigest()
        output_fingerprint = hashlib.sha256(f"{self.owner}:output".encode()).hexdigest()
        receipt = build_owner_effect_receipt(
            owner=self.owner,
            control_decision_ref=decision["control_decision_id"],
            consolidation_result_ref=consolidation["result_ref"],
            effect=effect["effect"],
            input_state_ref=f"{self.owner.upper()}-INPUT",
            input_state_fingerprint=input_fingerprint,
            output_state_ref=f"{self.owner.upper()}-OUTPUT",
            output_state_fingerprint=output_fingerprint,
            affected_refs=[f"{self.owner.upper()}-AFFECTED"],
            evidence_refs=sorted(set([consolidation["result_ref"], *prerequisites])),
            unaffected_state_preserved=True,
            state_mutated=True,
            persistence_evidence_ref=f"FIXTURE-DURABLE-{self.owner.upper()}-{len(self.calls)}",
        )
        self.verifier.register(receipt)
        return {
            "schema": "fixture-owner-completion/v1",
            "result": "PASS",
            "owner": self.owner,
            "receipt": receipt,
        }


class PMCommandExecutorFixture:
    def __init__(self, *, available: bool = True, stale: bool = False):
        self.available = available
        self.stale = stale
        self.calls: list[dict[str, Any]] = []

    def is_available(self, *, command: dict[str, Any], carrier: dict[str, Any]) -> bool:
        return self.available

    def execute(self, *, command: dict[str, Any], carrier: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({"command": copy.deepcopy(command), "carrier": copy.deepcopy(carrier)})
        if self.stale:
            return {
                "result": "STALE_PRECONDITION",
                "exact_blocker": "STALE_PRECONDITION:CANONICAL_STATE_REVISION_ADVANCED",
            }
        precondition = command["precondition"]
        after_fingerprint = hashlib.sha256(
            f"{command['command_id']}:{precondition['state_fingerprint']}:after".encode()
        ).hexdigest()
        return {
            "result": "PASS",
            "command_id": command["command_id"],
            "action_ref": command["action_ref"],
            "precondition_fingerprint": precondition["state_fingerprint"],
            "state_delta": {
                "before_state_ref": precondition["state_ref"],
                "before_state_fingerprint": precondition["state_fingerprint"],
                "after_state_ref": "PM-COMMAND-STATE-AFTER",
                "after_state_fingerprint": after_fingerprint,
                "mutated": True,
            },
            "readback": {
                "state_ref": "PM-COMMAND-STATE-AFTER",
                "state_fingerprint": after_fingerprint,
                "provider_revision": command["canonical_state_revision"] + 1,
                "verified": True,
            },
        }


def _runtime(enabled: set[str] | None = None) -> tuple[
    BoundRuntimeCapabilityResolver,
    CompositeOwnerPersistenceVerifier,
    dict[str, FixtureExecutor],
    list[str],
]:
    order: list[str] = []
    verifiers = {owner: FixtureVerifier(owner) for owner in ("project", "quality", "convergence", "context")}
    executors = {
        owner: FixtureExecutor(owner, verifiers[owner], order)
        for owner in ("project", "quality", "convergence", "context")
    }
    capability = BoundRuntimeCapabilityResolver(
        executors=executors,
        enabled_owners=set(executors) if enabled is None else enabled,
    )
    composite = CompositeOwnerPersistenceVerifier(verifiers=verifiers)
    return capability, composite, executors, order


def selftest() -> dict[str, Any]:
    tests: list[dict[str, str]] = []

    def check(name: str, condition: bool) -> None:
        tests.append({"name": name, "result": "PASS" if condition else "FAIL"})

    decision = {
        "authority": "MCP",
        "control_decision_id": "MCPD-HOST-1",
        "outcome": "CONTINUE",
    }
    consolidation = _consolidation_fixture(
        [
            "PROJECT_REVISION_REQUIRED",
            "QUALITY_INVALIDATION_REQUIRED",
            "CONVERGENCE_REVALIDATION_REQUIRED",
            "CONTEXT_ENRICHMENT",
        ]
    )
    capability, composite, executors, order = _runtime()

    class CombinedPmLifecycleVerifierFixture:
        def verify(self, *, binding: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
            return {
                "schema": "cerebro-project-manager-profile-verification/v1",
                "result": "PASS",
                "profile": "PROJECT_MANAGER",
                "session_ref": session.get("session_ref"),
                "binding_fingerprint": "a" * 64,
                "verifier_ref": "HOST-COMBINED-SELFTEST",
            }

        def verify_lifecycle_effect(self, **_: Any) -> dict[str, Any]:
            return {
                "schema": "cerebro-context-lifecycle-effect-verification/v1",
                "result": "PASS",
            }

    combined_pm_verifier = CombinedPmLifecycleVerifierFixture()
    injected: dict[str, Any] = {}

    def canonical_stub(request: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        injected.update(kwargs)
        return {"schema": "fixture-canonical-resolution/v1", "request": copy.deepcopy(request)}

    host = BoundControlResolutionHost(
        persistence_verifier=composite,
        capability_resolver=capability,
        canonical_resolver=canonical_stub,
        pm_profile_verifier=combined_pm_verifier,
    )
    resolved = host.resolve({"objective_ref": "OBJ"}, root=SOURCE_ROOT, require_git_ancestry=False)
    check(
        "normal-host-injects-trusted-dependencies-outside-event-payload",
        resolved["schema"] == "fixture-canonical-resolution/v1"
        and injected["owner_persistence_verifier"] is composite
        and injected["runtime_capability_resolver"] is capability
        and injected["pm_profile_verifier"] is combined_pm_verifier,
    )
    check(
        "event-payload-cannot-inject-runtime-verifier-or-capability",
        _expect_error(
            lambda: host.resolve(
                {"nested": {"capability_resolver": "SELF_ASSERTED"}},
                root=SOURCE_ROOT,
                require_git_ancestry=False,
            ),
            ControlResolutionHostError,
        ),
    )
    check(
        "event-payload-cannot-inject-pm-or-lifecycle-verifier",
        _expect_error(
            lambda: host.resolve(
                {"nested": {"pm_profile_verifier": "SELF_ASSERTED"}},
                root=SOURCE_ROOT,
                require_git_ancestry=False,
            ),
            ControlResolutionHostError,
        ),
    )
    sequence = host.execute_owner_sequence(
        decision=decision,
        consolidation_result=consolidation,
        execution_inputs={owner: {} for owner in executors},
    )
    check(
        "normal-host-executes-canonical-owner-order-and-reresolves-each-step",
        sequence["result"] == "PASS"
        and order == ["project", "quality", "convergence", "context"]
        and len(sequence["completions"]) == 4,
    )
    check(
        "final-disposition-gate-requires-all-four-independently-verified-receipts",
        sequence["final_plan"]["branch_disposition_gate"]["ready"] is True
        and sequence["final_plan"]["branch_disposition_gate"]["missing_owner_receipt_refs"] == []
        and len(sequence["final_plan"]["branch_disposition_gate"]["verified_owner_receipt_refs"]) == 4,
    )
    check(
        "normal-host-never-creates-cross-owner-atomic-merge",
        sequence["automatic_cross_owner_transaction"] is False
        and sequence["final_plan"]["automatic_cross_owner_transaction"] is False,
    )
    check(
        "each-downstream-receipt-carries-all-prerequisite-receipt-evidence",
        all(
            set(completion["receipt"]["evidence_refs"]).issuperset(
                {
                    prior["receipt"]["receipt_ref"]
                    for prior in sequence["completions"][:index]
                }
            )
            for index, completion in enumerate(sequence["completions"])
        ),
    )

    limited_capability, limited_composite, limited_executors, limited_order = _runtime({"project"})
    limited_host = BoundControlResolutionHost(
        persistence_verifier=limited_composite,
        capability_resolver=limited_capability,
        canonical_resolver=canonical_stub,
    )
    pending = limited_host.execute_owner_sequence(
        decision=decision,
        consolidation_result=consolidation,
        execution_inputs={owner: {} for owner in limited_executors},
    )
    check(
        "missing-runtime-capability-stops-before-owner-call-and-preserves-prerequisite-commit",
        pending["result"] == "PENDING_CAPABILITY"
        and pending["blocked_owner"] == "quality"
        and limited_order == ["project"]
        and len(pending["completions"]) == 1,
    )

    fake_receipt = copy.deepcopy(sequence["completions"][0]["receipt"])
    fake_receipt["persistence_evidence_ref"] = "SELF-ASSERTED"
    check(
        "self-asserted-current-receipt-cannot-pass-composite-verifier",
        _expect_error(lambda: composite.verify(receipt=fake_receipt), ValueError),
    )

    strict_capability, strict_composite, strict_executors, _ = _runtime({"project"})

    class IncompleteVerificationProxy:
        def verify(self, *, receipt: dict[str, Any]) -> dict[str, Any]:
            verified = strict_composite.verify(receipt=receipt)
            return {
                "schema": verified["schema"],
                "result": verified["result"],
                "verifier_ref": verified["verifier_ref"],
                "owner": verified["owner"],
            }

    strict_host = BoundControlResolutionHost(
        persistence_verifier=IncompleteVerificationProxy(),
        capability_resolver=strict_capability,
        canonical_resolver=canonical_stub,
    )
    check(
        "host-requires-verifier-to-bind-exact-receipt-and-output-state",
        _expect_error(
            lambda: strict_host.execute_owner_sequence(
                decision=decision,
                consolidation_result=consolidation,
                execution_inputs={owner: {} for owner in strict_executors},
            ),
            ControlResolutionHostError,
        ),
    )

    # Packet553 C1-C20: bounded PM-authorized command consumer + existing reorientation physiology.
    def adaptive_live_stub(request: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return adaptive_control_resolver.resolve(request)

    pm_host = BoundControlResolutionHost(
        persistence_verifier=composite,
        capability_resolver=capability,
        canonical_resolver=adaptive_live_stub,
    )
    governance = {
        "next_action": {
            "action_ref": "PM-ACTION-1",
            "owner": "MACHINE",
            "pm_actor": "PROJECT_MANAGER",
            "internally_executable": True,
            "required_before_event_closure": True,
        }
    }
    source_head = "9e5625ea3de06840489d5b145a9e63d08650e0fe"
    pre_fingerprint = hashlib.sha256(b"pm-command-before").hexdigest()
    command = {
        "schema": PM_AUTHORIZED_COMMAND_STATE_SCHEMA,
        "authority": "PROJECT_MANAGER",
        "command_id": "PMCMD-1",
        "action_ref": "PM-ACTION-1",
        "source_head": source_head,
        "canonical_state_ref": "CONTEXT:PMCMD:1",
        "canonical_state_revision": 7,
        "authorized_carrier_ref": "L-OLD-CARRIER",
        "precondition": {
            "state_ref": "PM-COMMAND-STATE-BEFORE",
            "state_fingerprint": pre_fingerprint,
        },
        "payload": {"operation": "EXECUTE_ALREADY_AUTHORIZED_COMMAND"},
    }
    carrier = {
        "carrier_ref": "L-FRESH-CARRIER",
        "identity_verified": True,
        "currentness_verified": True,
        "source_head": source_head,
        "canonical_state_ref": "CONTEXT:PMCMD:1",
        "canonical_state_revision": 7,
    }
    executor = PMCommandExecutorFixture()
    consumed = consume_pm_authorized_command(
        pm_host,
        governance=governance,
        command_state=command,
        carrier=carrier,
        command_executor=executor,
        root=SOURCE_ROOT,
        require_git_ancestry=False,
    )
    check(
        "C1-pm-decides-consumer-does-not-decide",
        command["authority"] == "PROJECT_MANAGER"
        and consumed["result"] == "PASS_STATE_DELTA_READBACK"
        and len(executor.calls) == 1,
    )
    check(
        "C2-authorized-command-consumed-same-cycle-with-exact-readback",
        consumed["command_executed"] is True
        and consumed["state_delta_observed"] is True
        and consumed["state_delta"]["after_state_fingerprint"] == consumed["readback"]["state_fingerprint"],
    )
    no_effect = consume_pm_authorized_command(
        pm_host,
        governance={"next_action": {"action_ref": "NONE", "owner": "NONE"}},
        command_state=None,
        carrier=carrier,
        command_executor=executor,
        root=SOURCE_ROOT,
        require_git_ancestry=False,
    )
    check("C3-no-authorized-command-means-no-effect", no_effect["result"] == "NO_EFFECT")

    stale = consume_pm_authorized_command(
        pm_host,
        governance=governance,
        command_state=command,
        carrier=carrier,
        command_executor=PMCommandExecutorFixture(stale=True),
        root=SOURCE_ROOT,
        require_git_ancestry=False,
    )
    check(
        "C4-stale-precondition-returns-exact-blocker-no-retry",
        stale["result"] == "EXACT_BLOCKER"
        and stale["exact_blocker"].startswith("STALE_PRECONDITION:")
        and stale["retry_allowed"] is False,
    )
    unavailable_carrier = copy.deepcopy(carrier)
    unavailable_carrier["capable_carrier_ref"] = "L-CAPABLE"
    unavailable = consume_pm_authorized_command(
        pm_host,
        governance=governance,
        command_state=command,
        carrier=unavailable_carrier,
        command_executor=PMCommandExecutorFixture(available=False),
        root=SOURCE_ROOT,
        require_git_ancestry=False,
    )
    check(
        "C5-capability-unavailable-returns-exact-external-blocker",
        unavailable["result"] == "EXACT_BLOCKER"
        and unavailable["exact_blocker"].startswith("CARRIER_COMMAND_EXECUTOR_UNAVAILABLE:")
        and unavailable["hmi"]["next_owner"] == "L-CAPABLE",
    )
    check(
        "C6-carrier-replacement-recovers-pending-command-from-canonical-state",
        command["authorized_carrier_ref"] != carrier["carrier_ref"]
        and consumed["result"] == "PASS_STATE_DELTA_READBACK",
    )
    check(
        "C7-terminal-result-fronting-does-not-auto-admit",
        "admission" not in consumed and consumed["hmi"]["next_owner"] == "PROJECT_MANAGER",
    )
    check(
        "C8-consumer-has-no-disjoint-lane-global-stop",
        consumed["result"] == "PASS_STATE_DELTA_READBACK" and "global_stop" not in consumed,
    )
    check(
        "C9-zero-human-pulse-dependency",
        consumed["hmi"]["human_action"] == "NONE",
    )
    check(
        "C10-consumer-is-not-authority",
        command["authority"] == "PROJECT_MANAGER"
        and consumed.get("authority") is None,
    )
    check(
        "C11-hmi-renders-next-machine-action-owner-human-action",
        set(consumed["hmi"]) == {"next_machine_action", "next_owner", "human_action"}
        and consumed["hmi"]["next_machine_action"] == "RERESOLVE_CONTROL",
    )
    check(
        "C12-carrier-capability-does-not-collapse-system-capability",
        unavailable["hmi"]["next_owner"] == "L-CAPABLE"
        and "SYSTEM_CAPABILITY_UNAVAILABLE" not in unavailable["exact_blocker"],
    )

    reflex = {
        "repeated_same_family": True,
        "no_state_advance": True,
        "material_human_recontact": True,
        "elapsed_time_only": False,
        "fallback_state": "AVAILABLE",
        "fallback_proven": True,
        "fallback_ref": "KNOWN-FALLBACK",
        "reorientation_request": {
            "objective_ref": "P553-REFLEX",
            "current_execution_mechanism": "same-wall-A",
            "proposed_execution_mechanism": "known-fallback-B",
            "requested_capabilities": [
                {
                    "id": "primary-route",
                    "required": False,
                    "fallback": "known-fallback-B",
                    "fallback_proven": True,
                }
            ],
            "governing_basis_refs": ["FAILURE_INDEX", "LEVERINGSARV", "VINKELPASS"],
        },
    }
    reoriented = consume_pm_authorized_command(
        pm_host,
        governance=governance,
        command_state=command,
        carrier=carrier,
        command_executor=executor,
        progress_evidence=reflex,
        root=SOURCE_ROOT,
        require_git_ancestry=False,
    )
    check(
        "C13-repeated-same-family-no-advance-human-burden-routes-reorientation",
        reoriented["result"] == "REORIENTED_BEFORE_IDENTICAL_RETRY"
        and reoriented["reflex_resolution"]["mcp_control_decision"]["outcome"] == "REORIENT",
    )
    elapsed_only = copy.deepcopy(reflex)
    elapsed_only["repeated_same_family"] = False
    elapsed_only["no_state_advance"] = False
    elapsed_only["material_human_recontact"] = False
    elapsed_only["elapsed_time_only"] = True
    elapsed_executor = PMCommandExecutorFixture()
    elapsed_result = consume_pm_authorized_command(
        pm_host,
        governance=governance,
        command_state=command,
        carrier=carrier,
        command_executor=elapsed_executor,
        progress_evidence=elapsed_only,
        root=SOURCE_ROOT,
        require_git_ancestry=False,
    )
    check(
        "C14-elapsed-time-alone-does-not-trigger-reflex",
        elapsed_result["result"] == "PASS_STATE_DELTA_READBACK"
        and len(elapsed_executor.calls) == 1,
    )
    check(
        "C15-proven-sufficient-fallback-routes-before-new-architecture",
        reoriented["reflex_resolution"]["execution_profile"]["execution_mechanism"] == "known-fallback-B"
        and reoriented["reflex_resolution"]["mcp_control_decision"]["outcome"] == "REORIENT",
    )
    stale_fallback = copy.deepcopy(reflex)
    stale_fallback["fallback_state"] = "STALE"
    stale_fallback_result = consume_pm_authorized_command(
        pm_host,
        governance=governance,
        command_state=command,
        carrier=carrier,
        command_executor=executor,
        progress_evidence=stale_fallback,
        root=SOURCE_ROOT,
        require_git_ancestry=False,
    )
    check(
        "C16-stale-or-unknown-fallback-requires-refresh-not-blind-use",
        stale_fallback_result["result"] == "EXACT_BLOCKER"
        and stale_fallback_result["exact_blocker"] == "FALLBACK_CURRENTNESS_UNRESOLVED:STALE",
    )
    no_fallback = copy.deepcopy(reflex)
    no_fallback["fallback_state"] = "NONE_PROVEN"
    no_fallback["fallback_proven"] = False
    no_fallback["fallback_ref"] = ""
    no_fallback["semantic_owner_ref"] = "MCP_CONTROL_ARCHITECTURE"
    no_fallback["reorientation_request"]["proposed_execution_mechanism"] = "bounded-regrounding-owner-route"
    regrounded = consume_pm_authorized_command(
        pm_host,
        governance=governance,
        command_state=command,
        carrier=carrier,
        command_executor=executor,
        progress_evidence=no_fallback,
        root=SOURCE_ROOT,
        require_git_ancestry=False,
    )
    check(
        "C17-no-proven-fallback-routes-bounded-regrounding-to-lawful-owner",
        regrounded["result"] == "REORIENTED_BEFORE_IDENTICAL_RETRY"
        and no_fallback["semantic_owner_ref"] == "MCP_CONTROL_ARCHITECTURE",
    )
    invalid_owner = copy.deepcopy(no_fallback)
    invalid_owner["semantic_owner_ref"] = "PROJECT_MANAGER"
    check(
        "C18-host-or-pm-is-not-architecture-authority",
        _expect_error(
            lambda: consume_pm_authorized_command(
                pm_host,
                governance=governance,
                command_state=command,
                carrier=carrier,
                command_executor=executor,
                progress_evidence=invalid_owner,
                root=SOURCE_ROOT,
                require_git_ancestry=False,
            ),
            ControlResolutionHostError,
        ),
    )
    check(
        "C19-behavior-canary-executes-real-adaptive-routing",
        reoriented["reflex_resolution"]["schema"] == "cerebro-adaptive-control-resolution/v0.1"
        and reoriented["reflex_resolution"]["mcp_control_decision"]["outcome"] == "REORIENT",
    )
    same_wall = copy.deepcopy(reflex)
    same_wall["reorientation_request"]["proposed_execution_mechanism"] = "same-wall-A"
    same_wall_result = consume_pm_authorized_command(
        pm_host,
        governance=governance,
        command_state=command,
        carrier=carrier,
        command_executor=executor,
        progress_evidence=same_wall,
        root=SOURCE_ROOT,
        require_git_ancestry=False,
    )
    check(
        "C20-same-wall-retry-without-material-delta-blocks-no-progress",
        same_wall_result["result"] == "EXACT_BLOCKER"
        and same_wall_result["exact_blocker"] == "NO_PROGRESS_REORIENTATION_BLOCK"
        and "REORIENTATION_INVALID_UNCHANGED_PATH"
        in same_wall_result["reflex_resolution"]["mcp_control_decision"]["invalidates"],
    )

    result = "PASS" if all(item["result"] == "PASS" for item in tests) else "FAIL"
    return {
        "schema": "cerebro-control-resolution-host-contract-selftest/v1",
        "result": result,
        "evidence_class": "LOCAL_NORMAL_HOST_HARNESS_NOT_REMOTE_ACTIVATION",
        "remote_host_executed": False,
        "test_count": len(tests),
        "failures": [item for item in tests if item["result"] != "PASS"],
        "tests": tests,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["selftest"])
    parser.parse_args()
    result = selftest()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
