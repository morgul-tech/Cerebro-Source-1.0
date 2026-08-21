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

from control_owner_effect_receipt import build_owner_effect_receipt  # noqa: E402
from control_owner_routing import _consolidation_fixture  # noqa: E402
from control_resolution_host import (  # noqa: E402
    BoundControlResolutionHost,
    BoundRuntimeCapabilityResolver,
    CompositeOwnerPersistenceVerifier,
    ControlResolutionHostError,
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


class FixturePMProfileVerifier:
    def verify(self, *, binding: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema": "cerebro-project-manager-profile-verification/v1",
            "result": "PASS",
            "profile": "PROJECT_MANAGER",
            "session_ref": session.get("session_ref"),
            "binding_fingerprint": "c" * 64,
            "verifier_ref": "FIXTURE-CONSTRUCTOR-BOUND-PM-PROFILE",
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
    pm_profile_verifier = FixturePMProfileVerifier()
    injected: dict[str, Any] = {}
    canonical_calls: list[dict[str, Any]] = []

    def canonical_stub(request: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        canonical_calls.append(copy.deepcopy(request))
        injected.update(kwargs)
        return {"schema": "fixture-canonical-resolution/v1", "request": copy.deepcopy(request)}

    host = BoundControlResolutionHost(
        persistence_verifier=composite,
        capability_resolver=capability,
        pm_profile_verifier=pm_profile_verifier,
        canonical_resolver=canonical_stub,
    )
    resolved = host.resolve({"objective_ref": "OBJ"}, root=SOURCE_ROOT, require_git_ancestry=False)
    check(
        "normal-host-injects-trusted-dependencies-outside-event-payload",
        resolved["schema"] == "fixture-canonical-resolution/v1"
        and injected["owner_persistence_verifier"] is composite
        and injected["runtime_capability_resolver"] is capability
        and injected["pm_profile_verifier"] is pm_profile_verifier,
    )
    check("normal-host-invokes-one-canonical-control-path", len(canonical_calls) == 1)
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
        "event-payload-cannot-inject-PM-profile-verifier",
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
        pm_profile_verifier=pm_profile_verifier,
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
        pm_profile_verifier=pm_profile_verifier,
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
