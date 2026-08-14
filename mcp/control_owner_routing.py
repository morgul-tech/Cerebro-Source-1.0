#!/usr/bin/env python3
"""MCP-owned routing of derived consolidation effects to existing owners.

The returned object is a subordinate part of one canonical MCP control decision,
never a second decision.  It plans a multi-event owner sequence and does not mutate
Project, Quality, Convergence or Context state by itself.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


sys.dont_write_bytecode = True
SOURCE_ROOT = Path(__file__).resolve().parents[1]
INTERACTION = SOURCE_ROOT / "engines" / "interaction"
if str(INTERACTION) not in sys.path:
    sys.path.insert(0, str(INTERACTION))

from context_consolidation import (  # noqa: E402
    REQUEST_SCHEMA,
    build_context_consolidation_result,
    validate_context_consolidation_result,
)
from control_owner_effect_receipt import (  # noqa: E402
    build_owner_effect_receipt,
    validate_owner_effect_receipt,
)


PLAN_SCHEMA = "cerebro-mcp-owner-effect-plan/v1"
PERSISTENCE_VERIFICATION_SCHEMA = "cerebro-owner-state-persistence-verification/v1"
CONTROL_OUTCOMES = {"CONTINUE", "REMEDIATE", "RETRY", "REORIENT", "USER_DECISION_REQUIRED", "BLOCK"}
OWNER_ORDER = ("project", "quality", "convergence", "context")
EFFECT_MAP = {
    "project": ("PROJECT_REVISION_REQUIRED", "REVISION_REQUIRED"),
    "quality": ("QUALITY_INVALIDATION_REQUIRED", "INVALIDATE_AFFECTED"),
    "convergence": ("CONVERGENCE_REVALIDATION_REQUIRED", "REVALIDATE_AFFECTED"),
}


class ControlOwnerRoutingError(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ControlOwnerRoutingError(message)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _fingerprint(value: dict[str, Any], *fields: str) -> str:
    subject = copy.deepcopy(value)
    for field in fields:
        subject.pop(field, None)
    return hashlib.sha256(_canonical(subject)).hexdigest()


def _receipt_state(
    owner: str,
    required: bool,
    raw: Any,
    *,
    control_decision_ref: str,
    consolidation_result_ref: str,
    effect: str,
    persistence_evidence_verifier: Any | None,
    capability_resolver: Any | None,
) -> dict[str, Any]:
    if not required:
        return {
            "owner": owner,
            "status": "NOT_REQUIRED",
            "current": True,
            "receipt_ref": None,
            "receipt": None,
            "capability_available": None,
            "persistence_evidence_verified": None,
        }
    value = raw if isinstance(raw, dict) else {}
    receipt = value.get("receipt") if isinstance(value.get("receipt"), dict) else (
        value if value.get("schema") == "cerebro-owner-effect-receipt/v1" else None
    )
    capability = None
    if capability_resolver is not None:
        resolve_capability = getattr(capability_resolver, "is_available", None)
        _require(callable(resolve_capability), "owner-capability-resolver-invalid")
        try:
            capability = resolve_capability(owner=owner, effect=effect)
        except Exception as exc:
            raise ControlOwnerRoutingError(f"owner-capability-resolution-failed:{owner}:{exc}") from exc
        _require(isinstance(capability, bool), f"{owner}-capability-resolution-boolean-required")
    if receipt is not None:
        try:
            validated = validate_owner_effect_receipt(
                receipt,
                expected_owner=owner,
                expected_control_decision_ref=control_decision_ref,
                expected_consolidation_result_ref=consolidation_result_ref,
                expected_effect=effect,
            )
        except Exception as exc:
            raise ControlOwnerRoutingError(str(exc)) from exc
        persistence_verified = False
        if validated["current"]:
            verify = getattr(persistence_evidence_verifier, "verify", None)
            if persistence_evidence_verifier is not None:
                _require(callable(verify), "owner-persistence-evidence-verifier-invalid")
                try:
                    verification = verify(receipt=copy.deepcopy(receipt))
                except Exception as exc:
                    raise ControlOwnerRoutingError(f"owner-persistence-evidence-verification-failed:{owner}:{exc}") from exc
                _require(isinstance(verification, dict), "owner-persistence-verification-object-required")
                expected_verification = {
                    "schema": PERSISTENCE_VERIFICATION_SCHEMA,
                    "result": "PASS",
                    "owner": owner,
                    "owner_effect_receipt_ref": receipt["receipt_ref"],
                    "owner_effect_receipt_fingerprint": receipt["receipt_fingerprint"],
                    "persistence_evidence_ref": receipt["persistence_evidence_ref"],
                    "output_state_ref": receipt["output_state_ref"],
                    "output_state_fingerprint": receipt["output_state_fingerprint"],
                }
                for field, expected in expected_verification.items():
                    _require(verification.get(field) == expected, f"owner-persistence-verification-{field}-mismatch")
                _require(
                    isinstance(verification.get("verifier_ref"), str) and bool(verification["verifier_ref"].strip()),
                    "owner-persistence-verifier-ref-required",
                )
                persistence_verified = True
        current = validated["current"] and persistence_verified
        receipt_ref = validated["receipt_ref"] if current else None
    else:
        current = False
        receipt_ref = None
        persistence_verified = False
    return {
        "owner": owner,
        "status": "SATISFIED" if current else "PENDING",
        "current": current,
        "receipt_ref": receipt_ref,
        "receipt": copy.deepcopy(receipt),
        "capability_available": capability,
        "persistence_evidence_verified": persistence_verified,
    }


def validate_owner_effect_plan(plan: dict[str, Any]) -> dict[str, Any]:
    _require(plan.get("schema") == PLAN_SCHEMA, "owner-effect-plan-schema-mismatch")
    _require(plan.get("authority") == "MCP", "owner-effect-plan-authority-must-be-MCP")
    _require(plan.get("parallel_control_decision") is False, "owner-effect-plan-cannot-be-parallel-decision")
    _require(plan.get("automatic_cross_owner_transaction") is False, "cross-owner-atomic-merge-prohibited")
    effects = plan.get("owner_effects")
    _require(isinstance(effects, dict) and set(effects) == {"project", "quality", "convergence", "context", "human"}, "owner-effect-set-invalid")
    stages = plan.get("ordered_owner_steps")
    _require(isinstance(stages, list), "ordered-owner-steps-array-required")
    owners = [item.get("owner") for item in stages if isinstance(item, dict)]
    _require(owners == [owner for owner in OWNER_ORDER if effects[owner]["effect"] != "NONE"], "owner-step-order-invalid")
    statuses = {item["owner"]: item["status"] for item in stages}
    seen_pending = False
    for owner in owners:
        status = statuses[owner]
        _require(status in {"PENDING", "SATISFIED", "BLOCKED_BY_PREREQUISITE"}, "owner-step-status-invalid")
        step = next(item for item in stages if item["owner"] == owner)
        _require(isinstance(step.get("persistence_evidence_verified"), bool), "owner-step-persistence-verification-required")
        if status == "SATISFIED":
            _require(step["persistence_evidence_verified"] is True, "satisfied-owner-step-requires-verified-persistence")
            _require(isinstance(step.get("receipt_ref"), str) and bool(step["receipt_ref"]), "satisfied-owner-step-receipt-required")
        else:
            _require(step["persistence_evidence_verified"] is False, "pending-owner-step-cannot-claim-verified-persistence")
            _require(step.get("receipt_ref") is None, "pending-owner-step-cannot-expose-current-receipt")
        if status != "SATISFIED":
            seen_pending = True
        elif seen_pending:
            _require(False, "downstream-owner-cannot-be-satisfied-before-prerequisite")
    gate = plan.get("branch_disposition_gate")
    _require(isinstance(gate, dict), "branch-disposition-gate-required")
    if gate.get("ready") is True:
        _require(not gate.get("missing_owner_receipt_refs"), "ready-disposition-gate-cannot-miss-receipts")
        _require(effects["human"]["effect"] == "NONE", "human-decision-pending-blocks-disposition")
    expected = _fingerprint(plan, "plan_ref", "plan_fingerprint")
    _require(plan.get("plan_fingerprint") == expected, "owner-effect-plan-fingerprint-mismatch")
    _require(plan.get("plan_ref") == "OEP-" + expected[:24].upper(), "owner-effect-plan-ref-mismatch")
    return {
        "result": "PASS",
        "plan_ref": plan["plan_ref"],
        "branch_disposition_ready": gate["ready"],
        "next_action_owner": plan["next_action"]["owner"],
    }


def build_owner_effect_plan(
    decision: dict[str, Any],
    consolidation_result: dict[str, Any],
    owner_state: dict[str, Any] | None = None,
    persistence_evidence_verifier: Any | None = None,
    capability_resolver: Any | None = None,
) -> dict[str, Any]:
    """Route effect candidates and order their owner receipts."""

    _require(isinstance(decision, dict), "mcp-control-decision-object-required")
    _require(decision.get("authority") == "MCP", "owner-routing-requires-MCP-decision")
    decision_ref = decision.get("control_decision_id")
    _require(isinstance(decision_ref, str) and bool(decision_ref.strip()), "mcp-control-decision-ref-required")
    _require(decision.get("outcome") in CONTROL_OUTCOMES, "mcp-control-outcome-invalid")
    validate_context_consolidation_result(consolidation_result)
    candidates = set(consolidation_result["effect_candidates"])
    if decision["outcome"] != "CONTINUE":
        allowed = {"HUMAN_DECISION_REQUIRED"} if decision["outcome"] == "USER_DECISION_REQUIRED" else set()
        _require(candidates.issubset(allowed), "non-CONTINUE-decision-cannot-apply-machine-owner-effects")
    _require(
        ("HUMAN_DECISION_REQUIRED" in candidates) == (decision["outcome"] == "USER_DECISION_REQUIRED"),
        "human-owner-effect-must-match-canonical-user-decision-outcome",
    )
    state = owner_state if isinstance(owner_state, dict) else {}

    effects: dict[str, dict[str, Any]] = {}
    required_by_owner: dict[str, bool] = {}
    for owner, (candidate, effect_name) in EFFECT_MAP.items():
        required = candidate in candidates
        required_by_owner[owner] = required
        effects[owner] = {
            "owner": owner,
            "effect": effect_name if required else "NONE",
            "candidate_ref": consolidation_result["result_ref"] if required else None,
            "state_mutation_by_MCP": False,
        }

    dispositions = consolidation_result["branch_disposition_candidates"]
    final_dispositions = [item for item in dispositions if item["candidate"] != "PENDING_JOIN"]
    upstream_required = any(required_by_owner.get(owner, False) for owner in ("project", "quality", "convergence"))
    context_required = "CONTEXT_ENRICHMENT" in candidates or upstream_required
    required_by_owner["context"] = context_required
    context_effect = "REFRESH_GOVERNING_REFS" if context_required else "NONE"
    effects["context"] = {
        "owner": "context",
        "effect": context_effect,
        "candidate_ref": consolidation_result["result_ref"] if context_required else None,
        "state_mutation_by_MCP": False,
    }
    human_required = "HUMAN_DECISION_REQUIRED" in candidates
    effects["human"] = {
        "owner": "human",
        "effect": "DECISION_REQUIRED" if human_required else "NONE",
        "candidate_ref": consolidation_result["result_ref"] if human_required else None,
        "state_mutation_by_MCP": False,
    }

    receipt_states = {
        owner: _receipt_state(
            owner,
            required_by_owner[owner],
            state.get(owner),
            control_decision_ref=decision_ref,
            consolidation_result_ref=consolidation_result["result_ref"],
            effect=effects[owner]["effect"],
            persistence_evidence_verifier=persistence_evidence_verifier,
            capability_resolver=capability_resolver,
        )
        for owner in OWNER_ORDER
    }
    steps: list[dict[str, Any]] = []
    prerequisite_pending = False
    prior_required: list[str] = []
    for owner in OWNER_ORDER:
        if not required_by_owner[owner]:
            continue
        owner_receipt = receipt_states[owner]
        status = owner_receipt["status"]
        if prerequisite_pending:
            _require(not owner_receipt["current"], f"{owner}-owner-current-before-prerequisite")
            status = "BLOCKED_BY_PREREQUISITE"
        elif status != "SATISFIED":
            prerequisite_pending = True
        if status == "SATISFIED":
            prior_receipt_refs = {
                receipt_states[prior]["receipt_ref"] for prior in prior_required
                if receipt_states[prior]["receipt_ref"] is not None
            }
            receipt_evidence = set(owner_receipt["receipt"].get("evidence_refs", []))
            _require(
                prior_receipt_refs.issubset(receipt_evidence),
                f"{owner}-owner-receipt-missing-prerequisite-evidence",
            )
        steps.append({
            "step_ref": f"OWNER-{len(steps) + 1}-{owner.upper()}",
            "owner": owner,
            "effect": effects[owner]["effect"],
            "status": status,
            "depends_on_owner_receipts": list(prior_required),
            "receipt_ref": owner_receipt["receipt_ref"] if status == "SATISFIED" else None,
            "capability_available": owner_receipt["capability_available"],
            "persistence_evidence_verified": owner_receipt["persistence_evidence_verified"],
        })
        prior_required.append(owner)

    missing_receipts = [item["owner"] for item in steps if item["status"] != "SATISFIED"]
    disposition_ready = bool(final_dispositions) and not missing_receipts and not human_required
    receipt_refs = [item["receipt_ref"] for item in steps if item["status"] == "SATISFIED" and item["receipt_ref"] is not None]
    disposition_candidates = [
        {
            "project_ref": item["project_ref"],
            "context_ref": item["context_ref"],
            "candidate": item["candidate"],
            "application_ready": disposition_ready,
            "requires_new_canonical_context_event": True,
        }
        for item in final_dispositions
    ]

    first_incomplete = next((item for item in steps if item["status"] != "SATISFIED"), None)
    if human_required:
        next_action = {
            "action_ref": "HUMAN-DECISION-" + consolidation_result["result_ref"],
            "action_class": "HUMAN_DECISION",
            "owner": "HUMAN",
            "internally_executable": False,
            "required_before_event_closure": False,
            "basis_fingerprint": consolidation_result["basis_fingerprint"],
        }
    elif first_incomplete is not None:
        capability = first_incomplete["capability_available"] is True
        next_action = {
            "action_ref": first_incomplete["step_ref"],
            "action_class": "CONTROL",
            "owner": "MACHINE",
            "internally_executable": capability,
            "required_before_event_closure": True,
            "basis_fingerprint": consolidation_result["basis_fingerprint"],
        }
    else:
        next_action = {
            "action_ref": "NONE",
            "action_class": "CONTROL",
            "owner": "NONE",
            "internally_executable": False,
            "required_before_event_closure": False,
            "basis_fingerprint": consolidation_result["basis_fingerprint"],
        }

    plan: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "authority": "MCP",
        "control_decision_ref": decision_ref,
        "control_outcome": decision["outcome"],
        "consolidation_result_ref": consolidation_result["result_ref"],
        "consolidation_basis_fingerprint": consolidation_result["basis_fingerprint"],
        "owner_effects": effects,
        "ordered_owner_steps": steps,
        "requires_canonical_reresolution_after_project_revision": required_by_owner["project"],
        "automatic_cross_owner_transaction": False,
        "parallel_control_decision": False,
        "branch_disposition_gate": {
            "ready": disposition_ready,
            "missing_owner_receipt_refs": missing_receipts,
            "verified_owner_receipt_refs": receipt_refs,
            "disposition_candidates": disposition_candidates,
            "unresolved_branches_remain_pending": any(item["candidate"] == "PENDING_JOIN" for item in dispositions),
        },
        "next_action": next_action,
        "plan_fingerprint": "",
        "plan_ref": "",
    }
    plan["plan_fingerprint"] = _fingerprint(plan, "plan_ref", "plan_fingerprint")
    plan["plan_ref"] = "OEP-" + plan["plan_fingerprint"][:24].upper()
    validate_owner_effect_plan(plan)
    return plan


def _consolidation_fixture(effect_candidates: list[str]) -> dict[str, Any]:
    from context_consolidation import _project  # selftest fixture, not production API

    project = _project("PROJECT-A", "AGG-A", "A", "A")
    request = {
        "schema": REQUEST_SCHEMA, "event_ref": "E-OWNER", "target_kind": "CONTROL_CONTEXTS",
        "selectors": [
            {"kind": "CONTEXT_REF", "project_ref": "PROJECT-A", "value": "A-B"},
            {"kind": "CONTEXT_REF", "project_ref": "PROJECT-A", "value": "A-C"},
        ],
        "structural_join_requested": True,
    }
    synthesis = {
        "synthesis_ref": "SYNTH-OWNER", "evidence_refs": ["EV-1"], "material_conflicts": [],
        "branch_disposition_candidates": [
            {"project_ref": "PROJECT-A", "context_ref": "A-B", "candidate": "INCORPORATED"},
            {"project_ref": "PROJECT-A", "context_ref": "A-C", "candidate": "PENDING_JOIN"},
        ],
        "effect_candidates": effect_candidates,
    }
    return build_context_consolidation_result(request, [project], synthesis)


def _load_selftest_module(relative: str, name: str):
    path = SOURCE_ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"selftest-module-load-failed:{relative}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def selftest() -> dict[str, Any]:
    decision = {"authority": "MCP", "control_decision_id": "MCPD-OWNER-1", "outcome": "CONTINUE"}
    tests: list[dict[str, str]] = []

    def check(name: str, ok: bool) -> None:
        tests.append({"name": name, "result": "PASS" if ok else "FAIL"})

    result = _consolidation_fixture([
        "CONTEXT_ENRICHMENT", "PROJECT_REVISION_REQUIRED", "QUALITY_INVALIDATION_REQUIRED", "CONVERGENCE_REVALIDATION_REQUIRED"
    ])
    pending = build_owner_effect_plan(decision, result)
    check("R30-project-revision-is-routed-to-project-owner", pending["owner_effects"]["project"]["effect"] == "REVISION_REQUIRED")
    check("R31-quality-invalidation-is-routed-to-quality-owner", pending["owner_effects"]["quality"]["effect"] == "INVALIDATE_AFFECTED")
    check("R33-convergence-revalidation-is-routed-not-duplicated", pending["owner_effects"]["convergence"]["effect"] == "REVALIDATE_AFFECTED")
    check("owner-sequence-blocks-downstream-before-project-receipt", pending["ordered_owner_steps"][1]["status"] == "BLOCKED_BY_PREREQUISITE")
    no_quality = build_owner_effect_plan(decision, _consolidation_fixture(["CONTEXT_ENRICHMENT"]))
    check("R32-unaffected-quality-remains-NONE", no_quality["owner_effects"]["quality"]["effect"] == "NONE")
    loose_flags = {
        owner: {"current": True, "receipt_ref": f"UNVERIFIED-{owner.upper()}"}
        for owner in ("project", "quality", "convergence", "context")
    }
    loose_plan = build_owner_effect_plan(decision, result, loose_flags)
    check(
        "loose-current-flags-cannot-satisfy-owner-receipt-gate",
        loose_plan["branch_disposition_gate"]["ready"] is False
        and len(loose_plan["branch_disposition_gate"]["missing_owner_receipt_refs"]) == 4,
    )
    project_consumer = _load_selftest_module("engines/project/project_owner_effect.py", "cerebro_project_owner_consumer_fixture")
    quality_consumer = _load_selftest_module("engines/quality/quality_owner_effect.py", "cerebro_quality_owner_consumer_fixture")
    convergence_consumer = _load_selftest_module("engines/convergence/convergence_owner_effect.py", "cerebro_convergence_owner_consumer_fixture")
    context_consumer = _load_selftest_module("tooling/context/control_owner_effect.py", "cerebro_context_owner_consumer_fixture")

    current_basis = project_consumer.create_project_basis("PROJECT-A", {"objective": "old"})
    revised_basis, project_receipt = project_consumer.consume_project_revision_effect(
        owner_effect=pending["owner_effects"]["project"],
        control_decision_ref=decision["control_decision_id"],
        consolidation_result_ref=result["result_ref"],
        current_basis=current_basis,
        revised_payload={"objective": "revised", "synthesis_ref": result["synthesis_ref"]},
        affected_refs=["A"], evidence_refs=[result["synthesis_ref"]],
    )
    old_quality_basis = hashlib.sha256(b"quality-old").hexdigest()
    trace = quality_consumer.new_quality_trace("QUALITY-PROJECT-A", "DEEP", old_quality_basis)
    quality_consumer.pass_stage(trace, "REFINE", old_quality_basis, ["E-REFINE"])
    _, quality_receipt = quality_consumer.consume_quality_invalidation_effect(
        owner_effect=pending["owner_effects"]["quality"],
        control_decision_ref=decision["control_decision_id"],
        consolidation_result_ref=result["result_ref"],
        current_trace=trace,
        new_basis_fingerprint=revised_basis["basis_fingerprint"],
        affected_stage_refs=["REFINE"], evidence_refs=[project_receipt["receipt_ref"]],
    )
    convergence_state = convergence_consumer.create_convergence_state(
        "CONVERGENCE-PROJECT-A", old_quality_basis,
        [{"family_id": "F-A", "state": "PASS", "depends_on": [], "pass_basis_fingerprint": old_quality_basis, "invalidated_by": []}],
    )
    _, convergence_receipt = convergence_consumer.consume_convergence_revalidation_effect(
        owner_effect=pending["owner_effects"]["convergence"],
        control_decision_ref=decision["control_decision_id"],
        consolidation_result_ref=result["result_ref"],
        current_state=convergence_state,
        new_basis_fingerprint=revised_basis["basis_fingerprint"],
        directly_affected_family_refs=["F-A"],
        evidence_refs=[project_receipt["receipt_ref"], quality_receipt["receipt_ref"]],
    )
    from context_consolidation import _project
    from control_context_registry import DIRECTIVE_SCHEMA, bind_control_session

    context_project = _project("PROJECT-A", "AGG-A-CONTEXT", "A", "A")
    context_session = bind_control_session(
        context_project, session_binding_id="SB-OWNER", principal_ref="USER", consumer_ref="TEST", session_ref="SESSION"
    )
    context_directive = {
        "schema": DIRECTIVE_SCHEMA, "event_id": "E-CONTEXT-OWNER", "decision_ref": decision["control_decision_id"],
        "expected_project_revision": context_project["revision"], "expected_project_fingerprint": context_project["fingerprint"],
        "expected_session_revision": context_session["session_revision"], "expected_session_fingerprint": context_session["fingerprint"],
        "project_operations": [{
            "operation": "REFRESH_GOVERNING_REFS", "context_ref": "A",
            "project_basis_ref": revised_basis["basis_ref"], "quality_trace_ref": "QUALITY-PROJECT-A-REVISED",
            "basis_refs": [revised_basis["basis_ref"]],
        }],
        "session_operations": [],
    }
    _, _, _, context_receipt = context_consumer.apply_context_refresh_effect(
        owner_effect=pending["owner_effects"]["context"],
        control_decision_ref=decision["control_decision_id"],
        consolidation_result_ref=result["result_ref"],
        project=context_project, session=context_session, directive=context_directive,
        evidence_refs=[project_receipt["receipt_ref"], quality_receipt["receipt_ref"], convergence_receipt["receipt_ref"]],
    )
    candidate_state = {
        "project": {"receipt": project_receipt, "capability_available": True},
        "quality": {"receipt": quality_receipt, "capability_available": True},
        "convergence": {"receipt": convergence_receipt, "capability_available": True},
        "context": {"receipt": context_receipt, "capability_available": True},
    }
    persistence_blocked = build_owner_effect_plan(decision, result, candidate_state)
    check(
        "R34-owner-candidates-without-persistence-do-not-open-gate",
        persistence_blocked["branch_disposition_gate"]["ready"] is False
        and len(persistence_blocked["branch_disposition_gate"]["missing_owner_receipt_refs"]) == 4,
    )
    check(
        "self-asserted-capability-availability-is-ignored",
        persistence_blocked["ordered_owner_steps"][0]["capability_available"] is None
        and persistence_blocked["next_action"]["internally_executable"] is False,
    )
    check("R29-unresolved-sibling-remains-pending", persistence_blocked["branch_disposition_gate"]["unresolved_branches_remain_pending"] is True)

    def persisted_claim(candidate: dict[str, Any], evidence_ref: str, prerequisite_refs: list[str]) -> dict[str, Any]:
        return build_owner_effect_receipt(
            owner=candidate["owner"],
            control_decision_ref=candidate["control_decision_ref"],
            consolidation_result_ref=candidate["consolidation_result_ref"],
            effect=candidate["effect"],
            input_state_ref=candidate["input_state_ref"],
            input_state_fingerprint=candidate["input_state_fingerprint"],
            output_state_ref=candidate["output_state_ref"],
            output_state_fingerprint=candidate["output_state_fingerprint"],
            affected_refs=candidate["affected_refs"],
            evidence_refs=prerequisite_refs,
            unaffected_state_preserved=candidate["unaffected_state_preserved"],
            state_mutated=candidate["state_mutated"],
            persistence_evidence_ref=evidence_ref,
        )

    persisted_project = persisted_claim(project_receipt, "FIXTURE-COMMIT-PROJECT", project_receipt["evidence_refs"])
    persisted_quality = persisted_claim(quality_receipt, "FIXTURE-COMMIT-QUALITY", [persisted_project["receipt_ref"]])
    persisted_convergence = persisted_claim(
        convergence_receipt,
        "FIXTURE-COMMIT-CONVERGENCE",
        [persisted_project["receipt_ref"], persisted_quality["receipt_ref"]],
    )
    persisted_context = persisted_claim(
        context_receipt,
        "FIXTURE-COMMIT-CONTEXT",
        [persisted_project["receipt_ref"], persisted_quality["receipt_ref"], persisted_convergence["receipt_ref"]],
    )
    claimed_current_state = {
        "project": {"receipt": persisted_project, "capability_available": True},
        "quality": {"receipt": persisted_quality, "capability_available": True},
        "convergence": {"receipt": persisted_convergence, "capability_available": True},
        "context": {"receipt": persisted_context, "capability_available": True},
    }
    unverified_current = build_owner_effect_plan(decision, result, claimed_current_state)
    check(
        "self-claimed-persistence-PASS-cannot-open-gate",
        unverified_current["branch_disposition_gate"]["ready"] is False
        and not unverified_current["branch_disposition_gate"]["verified_owner_receipt_refs"],
    )

    class FixturePersistenceVerifier:
        """Selftest-only contract fixture; not production persistence evidence."""

        def __init__(self, receipts: list[dict[str, Any]]):
            self._subjects = {
                item["persistence_evidence_ref"]: (
                    item["owner"], item["output_state_ref"], item["output_state_fingerprint"]
                )
                for item in receipts
            }

        def verify(self, *, receipt: dict[str, Any]) -> dict[str, Any]:
            expected = self._subjects.get(receipt.get("persistence_evidence_ref"))
            actual = (receipt.get("owner"), receipt.get("output_state_ref"), receipt.get("output_state_fingerprint"))
            if expected != actual:
                raise ValueError("fixture-persistence-subject-mismatch")
            return {
                "schema": PERSISTENCE_VERIFICATION_SCHEMA,
                "result": "PASS",
                "verifier_ref": "SELFTEST-ONLY-NOT-PRODUCTION",
                "owner": receipt["owner"],
                "owner_effect_receipt_ref": receipt["receipt_ref"],
                "owner_effect_receipt_fingerprint": receipt["receipt_fingerprint"],
                "persistence_evidence_ref": receipt["persistence_evidence_ref"],
                "output_state_ref": receipt["output_state_ref"],
                "output_state_fingerprint": receipt["output_state_fingerprint"],
            }

    class FixtureCapabilityResolver:
        """Selftest-only runtime capability fixture."""

        def is_available(self, *, owner: str, effect: str) -> bool:
            return owner in OWNER_ORDER and effect != "NONE"

    capability_bound = build_owner_effect_plan(
        decision,
        result,
        candidate_state,
        capability_resolver=FixtureCapabilityResolver(),
    )
    check(
        "injected-runtime-capability-resolver-controls-executable-claim",
        capability_bound["ordered_owner_steps"][0]["capability_available"] is True
        and capability_bound["next_action"]["internally_executable"] is True,
    )

    fixture_verifier = FixturePersistenceVerifier(
        [persisted_project, persisted_quality, persisted_convergence, persisted_context]
    )
    fixture_verified = build_owner_effect_plan(
        decision,
        result,
        claimed_current_state,
        persistence_evidence_verifier=fixture_verifier,
    )
    check(
        "verified-persistence-contract-opens-gate-in-selftest-only",
        fixture_verified["branch_disposition_gate"]["ready"] is True
        and len(fixture_verified["branch_disposition_gate"]["verified_owner_receipt_refs"]) == 4,
    )
    tampered_state = copy.deepcopy(candidate_state)
    tampered_state["project"]["receipt"]["output_state_ref"] = "TAMPERED"
    tampered_rejected = False
    try:
        build_owner_effect_plan(decision, result, tampered_state)
    except ControlOwnerRoutingError:
        tampered_rejected = True
    check("tampered-owner-receipt-is-rejected", tampered_rejected)
    human_decision = {"authority": "MCP", "control_decision_id": "MCPD-OWNER-HUMAN", "outcome": "USER_DECISION_REQUIRED"}
    human = build_owner_effect_plan(human_decision, _consolidation_fixture(["HUMAN_DECISION_REQUIRED"]))
    check("human-decision-remains-human-boundary", human["next_action"]["owner"] == "HUMAN" and human["branch_disposition_gate"]["ready"] is False)
    rejected_parallel = False
    try:
        build_owner_effect_plan({"authority": "interaction", "control_decision_id": "BAD", "outcome": "CONTINUE"}, result)
    except ControlOwnerRoutingError:
        rejected_parallel = True
    check("only-canonical-MCP-decision-can-route-owner-effects", rejected_parallel)
    return {
        "schema": "cerebro-mcp-owner-effect-routing-selftest/v1",
        "result": "PASS" if all(item["result"] == "PASS" for item in tests) else "FAIL",
        "test_count": len(tests),
        "failures": [item for item in tests if item["result"] != "PASS"],
        "tests": tests,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["selftest"])
    args = parser.parse_args()
    result = selftest() if args.command == "selftest" else {"result": "BLOCK"}
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("result") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
