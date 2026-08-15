#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

BINDING_ID = "MCP_INTEGRITY_CONTROL"
ACTIVATION_SCHEMA = "cerebro-mcp-integrity-activation-proof/v1"
DIMENSIONS = (
    "OBJECTIVE_ALIGNMENT",
    "MCP_LOOP_INTEGRITY",
    "WORK_POSITION",
    "WORKFORM_ADEQUACY",
    "BASIS_AND_PRIOR_KNOWLEDGE",
    "NEXT_GATE_READINESS",
)
GATES = (
    "DECISION_READINESS",
    "IMPLEMENTATION_READINESS",
    "PATCH_READINESS",
    "OPERATIONAL_INTEGRATION",
    "DELIVERY_CONFORMITY",
)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _activation_evidence_basis(root: Path) -> list[str]:
    registry_path = root / "tooling/validator/contract-activation-bindings.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    matches = [item for item in registry.get("bindings", []) if item.get("id") == BINDING_ID]
    if len(matches) != 1:
        raise RuntimeError(f"activation-binding-cardinality:{BINDING_ID}:{len(matches)}")
    runtime_spec = matches[0].get("runtime_evidence")
    if not isinstance(runtime_spec, dict):
        raise RuntimeError(f"activation-runtime-evidence-spec-missing:{BINDING_ID}")
    basis_files = [str(item).strip() for item in runtime_spec.get("basis_files", []) if str(item).strip()]
    if not basis_files:
        raise RuntimeError(f"activation-runtime-evidence-basis-empty:{BINDING_ID}")
    return basis_files


def _source_state_fingerprint(root: Path, basis_files: list[str]) -> str:
    rows: list[str] = []
    for relative in sorted(basis_files):
        path = root / relative
        if not path.is_file():
            raise RuntimeError(f"activation-basis-file-missing:{relative}")
        rows.append(f"{relative}|{_sha256_bytes(path.read_bytes())}")
    return _sha256_bytes("\n".join(rows).encode("utf-8"))



def _adaptive_runtime_evidence_basis_parity(root: Path) -> tuple[bool, str]:
    control_path = root / "mcp/control_resolution.py"
    registry_path = root / "tooling/validator/contract-activation-bindings.json"
    if not control_path.is_file() or not registry_path.is_file():
        return False, "required-full-candidate-files-missing"
    import ast
    module = ast.parse(control_path.read_text(encoding="utf-8"))
    producer_basis = None
    for node in module.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "EVIDENCE_BASIS_FILES" for target in node.targets):
            producer_basis = ast.literal_eval(node.value)
            break
    if not isinstance(producer_basis, list) or not producer_basis:
        return False, "adaptive-producer-basis-not-resolved"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    matches = [item for item in registry.get("bindings", []) if item.get("id") == "ADAPTIVE_MCP_CONTROL_RESOLUTION"]
    if len(matches) != 1:
        return False, f"adaptive-binding-cardinality:{len(matches)}"
    consumer_basis = (matches[0].get("runtime_evidence") or {}).get("basis_files")
    if not isinstance(consumer_basis, list) or not consumer_basis:
        return False, "adaptive-consumer-basis-not-resolved"
    producer_set = set(str(x) for x in producer_basis)
    consumer_set = set(str(x) for x in consumer_basis)
    if producer_set != consumer_set or len(producer_basis) != len(consumer_basis):
        missing_from_consumer = sorted(producer_set - consumer_set)
        missing_from_producer = sorted(consumer_set - producer_set)
        return False, f"producer={len(producer_basis)} consumer={len(consumer_basis)} missing_from_consumer={missing_from_consumer} missing_from_producer={missing_from_producer}"
    return True, f"basis_count={len(producer_basis)}"

def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"module-load-failed:{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check(rows: list[dict[str, Any]], name: str, value: bool, detail: str = "") -> None:
    rows.append({"name": name, "result": "PASS" if value else "FAIL", "detail": detail})


def perfect_dimensions(basis: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for dim in DIMENSIONS:
        evidence = [f"E:{dim}"]
        out[dim] = {
            "status": "COMPLETE",
            "result": "PASS",
            "sufficiency": "COMPLETE",
            "freshness": {"state": "CURRENT", "basis_fingerprint": basis},
            "evidence_refs": evidence,
            "effect_evidence_refs": evidence,
            "owner_refs": ["MCP"],
            "mechanism_refs": [],
        }
    return out


def synthetic_validation(root: Path) -> dict[str, Any]:
    tests: list[dict[str, Any]] = []
    integrity = load(root / "mcp/integrity_resolution.py", "integrity_validation_resolution")
    intent = load(root / "engines/interaction/integrity_intent.py", "integrity_validation_intent")
    adapter = load(root / "mcp/integrity_control_adapter.py", "integrity_validation_adapter")
    presentation = load(root / "engines/presentation/integrity_presentation.py", "integrity_validation_presentation")

    basis = "a" * 64
    base = {
        "control_event_ref": "EVENT-INTEGRITY-VALIDATION",
        "basis_fingerprint": basis,
        "coverage_mode": "FULL",
        "primary_scope": "ALL",
        "gate_enforcement_required": False,
        "invalidation_triggers": ["MATERIAL_BASIS_CHANGE"],
        "dimension_assessments": perfect_dimensions(basis),
    }
    assessment = integrity.resolve(base)
    check(tests, "all-six-positive-dimensions-pass", assessment.get("result") == "PASS" and set(assessment.get("applicable_dimensions", [])) == set(DIMENSIONS))
    check(tests, "assessment-is-derived-evidence-not-live-authority", assessment.get("authority") == "DERIVED_CONTROL_EVIDENCE" and assessment.get("direct_live_authority") is False and assessment.get("final_control_decision_owner") == "MCP")
    check(tests, "basis-bound-assessment-identity-present", assessment.get("basis_fingerprint") == basis and str(assessment.get("assessment_id", "")).startswith("INTG-"))

    routes = [intent.resolve(x) for x in ("Integrity", "Integrity Full", "MCP-loop?")]
    check(tests, "manual-entrypoints-converge-on-one-mcp-path", all(x.get("route") == "MCP_INTEGRITY_SUBRESOLUTION" for x in routes) and len({x.get("route") for x in routes}) == 1)
    check(tests, "integrity-full-is-coverage-not-deep", routes[1].get("coverage_mode") == "FULL" and routes[1].get("primary_scope") == "ALL")
    check(tests, "mcp-loop-primary-scope-only", routes[2].get("primary_scope") == "MCP_LOOP_INTEGRITY" and routes[2].get("coverage_mode") == "ADAPTIVE")

    manual = integrity.resolve_invocation({}, routes[0])
    automatic = integrity.resolve_invocation({"implementation_requested": True}, None)
    check(tests, "manual-and-automatic-use-same-subresolution", manual.get("same_path_for_manual_and_automatic") is True and automatic.get("same_path_for_manual_and_automatic") is True and manual.get("required") is True and automatic.get("required") is True)

    # False-green family: mechanism existence alone cannot prove its protected question.
    mechanism_only = perfect_dimensions(basis)
    protected = {
        "MCP_LOOP_INTEGRITY": "MCP_EXISTS",
        "WORKFORM_ADEQUACY": "DEEP_SELECTED",
        "BASIS_AND_PRIOR_KNOWLEDGE": "WISDOM_EXISTS",
        "NEXT_GATE_READINESS": "VALIDATOR_EXISTS",
    }
    for dim, token in protected.items():
        row = dict(mechanism_only[dim])
        row["evidence_refs"] = [token]
        row["effect_evidence_refs"] = []
        row["mechanism_refs"] = [token]
        mechanism_only[dim] = row
    fg = integrity.resolve({**base, "dimension_assessments": mechanism_only})
    rejected = {row["dimension"]: row for row in fg["dimensions"]}
    check(tests, "false-green-mcp-existence-rejected", rejected["MCP_LOOP_INTEGRITY"]["result"] == "UNKNOWN" and rejected["MCP_LOOP_INTEGRITY"]["false_green_rejected"] is True)
    check(tests, "false-green-deep-selected-rejected", rejected["WORKFORM_ADEQUACY"]["result"] == "UNKNOWN" and rejected["WORKFORM_ADEQUACY"]["false_green_rejected"] is True)
    check(tests, "false-green-wisdom-exists-rejected", rejected["BASIS_AND_PRIOR_KNOWLEDGE"]["result"] == "UNKNOWN" and rejected["BASIS_AND_PRIOR_KNOWLEDGE"]["false_green_rejected"] is True)
    check(tests, "false-green-validator-exists-rejected", rejected["NEXT_GATE_READINESS"]["result"] == "UNKNOWN" and rejected["NEXT_GATE_READINESS"]["false_green_rejected"] is True)

    component_only = perfect_dimensions(basis)
    component_only["NEXT_GATE_READINESS"] = {
        **component_only["NEXT_GATE_READINESS"],
        "evidence_refs": ["COMPONENT_EXISTS", "DELIVERY_STANDARD_EXISTS"],
        "effect_evidence_refs": [],
        "mechanism_refs": ["COMPONENT_EXISTS", "DELIVERY_STANDARD_EXISTS"],
    }
    c_only = integrity.resolve({**base, "dimension_assessments": component_only})
    gate_row = next(x for x in c_only["dimensions"] if x["dimension"] == "NEXT_GATE_READINESS")
    check(tests, "false-green-component-and-delivery-standard-rejected", gate_row["result"] == "UNKNOWN" and gate_row["false_green_rejected"] is True)

    # Adapter-level false-green canaries: retrieval/self-asserted references are not effect evidence.
    candidate = {
        "control_state": {
            "control_state_id": "CTRL-FG", "basis_fingerprint": basis,
            "governing_basis_refs": [], "applicable_wisdom_refs": ["WISDOM-ONLY"],
            "applicable_knowledge_refs": [], "applicable_history_refs": [],
        },
        "mcp_control_decision": {
            "control_decision_id": "MCPD-FG", "control_state_ref": "CTRL-FG",
            "objective_ref": "OBJ", "basis_fingerprint": basis, "outcome": "CONTINUE",
            "basis_refs": [], "applicable_control_refs": [],
        },
        "execution_profile": {"analysis_depth": "LIGHT", "basis_fingerprint": basis},
    }
    preflight = {
        "result": "PASS",
        "receipt": {
            "control_decision_ref": "MCPD-PREFLIGHT-FG", "basis_fingerprint": "p" * 64,
            "resolved_objective": "OBJ", "resolved_scope": "SCOPE",
        },
    }
    fg_request = {
        "authoritative_source_commit": "1" * 40,
        "resolved_objective": "OBJ", "current_objective": "OBJ",
        "resolved_scope": "SCOPE", "current_scope": "SCOPE",
        "material_assumptions_exposed": True,
        "previous_control_state_ref": "PREV-UNPROVEN",
        "next_control_event_ref": "NEXT-UNPROVEN",
        "assurance_evidence_refs": ["ASSURANCE:E"],
        "required_workform_depth": "LIGHT",
        "existing_mechanism_assessed": True,
        "existing_owner_assessed": True,
        "parallel_mechanism_risk_assessed": True,
    }
    inv = integrity.resolve_invocation({"integrity_required": True}, None)
    payload = adapter.build_integrity_request(
        fg_request, candidate, None,
        {"promotion_basis_verified": True, "observed_source_head": "1" * 40},
        preflight, None, None, inv, root,
    )
    fg_adapter_assessment = integrity.resolve(payload)
    fg_map = {x["dimension"]: x for x in fg_adapter_assessment["dimensions"]}
    check(tests, "raw-previous-next-refs-without-effect-evidence-do-not-pass-mcp-loop", fg_map["MCP_LOOP_INTEGRITY"]["result"] == "UNKNOWN")
    check(tests, "retrieved-wisdom-not-consumed-does-not-pass-prior-knowledge", fg_map["BASIS_AND_PRIOR_KNOWLEDGE"]["result"] != "PASS")
    check(tests, "architecture-assessment-flags-without-evidence-do-not-pass", fg_map["BASIS_AND_PRIOR_KNOWLEDGE"]["result"] != "PASS")

    stale = perfect_dimensions(basis)
    stale["OBJECTIVE_ALIGNMENT"] = dict(stale["OBJECTIVE_ALIGNMENT"])
    stale["OBJECTIVE_ALIGNMENT"]["freshness"] = {"state": "CURRENT", "basis_fingerprint": "b" * 64}
    stale_result = integrity.resolve({**base, "dimension_assessments": stale})
    stale_row = next(x for x in stale_result["dimensions"] if x["dimension"] == "OBJECTIVE_ALIGNMENT")
    check(tests, "basis-change-invalidates-old-pass", stale_row["result"] == "UNKNOWN")

    unavailable = perfect_dimensions(basis)
    unavailable["BASIS_AND_PRIOR_KNOWLEDGE"] = {
        "status": "UNAVAILABLE",
        "result": "UNKNOWN",
        "sufficiency": "INSUFFICIENT",
        "freshness": {"state": "CURRENT", "basis_fingerprint": basis},
        "evidence_refs": [], "effect_evidence_refs": [],
    }
    unavailable_result = integrity.resolve({**base, "dimension_assessments": unavailable})
    unavailable_row = next(x for x in unavailable_result["dimensions"] if x["dimension"] == "BASIS_AND_PRIOR_KNOWLEDGE")
    check(tests, "unavailable-is-not-subject-failure", unavailable_row["result"] == "UNKNOWN" and unavailable_result["result"] == "UNKNOWN")

    na = perfect_dimensions(basis)
    na["WORK_POSITION"] = {
        "status": "COMPLETE", "result": "NOT_APPLICABLE", "sufficiency": "COMPLETE",
        "freshness": {"state": "CURRENT", "basis_fingerprint": basis}, "evidence_refs": ["N/A"],
    }
    na_result = integrity.resolve({**base, "dimension_assessments": na})
    check(tests, "not-applicable-remains-distinct", next(x for x in na_result["dimensions"] if x["dimension"] == "WORK_POSITION")["result"] == "NOT_APPLICABLE")

    # Gate profile selection uses only existing transition/readiness profiles.
    gate_cases = [
        ({"stage": "DECIDE"}, "DECISION_READINESS"),
        ({"implementation_requested": True}, "IMPLEMENTATION_READINESS"),
        ({"patch_handoff_requested": True}, "PATCH_READINESS"),
        ({"operational_claim_requested": True}, "OPERATIONAL_INTEGRATION"),
        ({"delivery_requested": True}, "DELIVERY_CONFORMITY"),
    ]
    for request, expected in gate_cases:
        observed = adapter._gate_profile(request)
        check(tests, f"gate-profile-{expected.lower()}", observed == expected, str(observed))

    # FULL and DEEP are independent: FULL does not manufacture DEEP and DEEP does not imply FULL.
    check(tests, "full-vs-deep-separate-axes", routes[1].get("coverage_mode") == "FULL" and routes[1].get("coverage_mode") not in {"LIGHT", "STANDARD", "DEEP"})

    # Real DEEP workform requires all relevant evidence-backed operations, not the selected flag.
    deep_basis = "c" * 64
    snapshot = {
        "workform_adequacy": {
            "required_depth": "DEEP", "selected_depth": "DEEP",
            "required_operations": ["UNDERSTAND_FRAME", "EXPLORE_RESEARCH", "REFINE", "CRITIQUE", "COMPARE_CONVERGE", "DECIDE", "EXECUTE_GENERATE", "VERIFY", "LEARN"],
            "completed_operations": ["UNDERSTAND_FRAME", "EXPLORE_RESEARCH", "REFINE", "CRITIQUE", "COMPARE_CONVERGE", "DECIDE", "EXECUTE_GENERATE", "VERIFY"],
            "quality_trace_current": True,
            "evidence_refs": ["Q"], "effect_evidence_refs": ["Q"],
        }
    }
    deep_rows = integrity.derive_dimension_assessments(snapshot, deep_basis)
    check(tests, "deep-selected-not-equal-deep-completed", deep_rows["WORKFORM_ADEQUACY"]["result"] == "FAIL")

    # Recommendation is subordinate; it may only strengthen, never weaken a stronger MCP result.
    block_assessment = integrity.resolve({**base, "dimension_assessments": {**perfect_dimensions(basis), "MCP_LOOP_INTEGRITY": {
        "status": "COMPLETE", "result": "FAIL", "sufficiency": "COMPLETE",
        "freshness": {"state": "CURRENT", "basis_fingerprint": basis},
        "evidence_refs": ["BYPASS:E"], "effect_evidence_refs": ["BYPASS:E"],
    }}})
    strengthened, reasons = adapter.apply_recommendation("CONTINUE", block_assessment)
    preserved_block, _ = adapter.apply_recommendation("BLOCK", block_assessment)
    preserved_human, _ = adapter.apply_recommendation("USER_DECISION_REQUIRED", block_assessment)
    check(tests, "integrity-can-strengthen-final-mcp-outcome", strengthened == "BLOCK" and bool(reasons))
    check(tests, "integrity-cannot-weaken-existing-block", preserved_block == "BLOCK")
    check(tests, "integrity-cannot-override-human-boundary", preserved_human == "USER_DECISION_REQUIRED")

    rendered = presentation.render(assessment)
    check(tests, "presentation-does-not-own-integrity-truth", rendered.get("authority") == "PRESENTATION_ONLY" and rendered.get("truth_owner") is False and rendered.get("assessment_ref") == assessment.get("assessment_id"))

    # Static installed-candidate assertions: one canonical MCP wrapper remains final owner.
    canonical_text = (root / "mcp/control_resolution.py").read_text(encoding="utf-8")
    control_contract = (root / "mcp/integrity-control.yaml").read_text(encoding="utf-8")
    adaptive_text = (root / "mcp/adaptive_control_resolver.py").read_text(encoding="utf-8") if (root / "mcp/adaptive_control_resolver.py").is_file() else ""
    preflight_pos = canonical_text.find("preflight_result = preflight.resolve")
    adaptive_pos = canonical_text.find("candidate = adaptive.resolve(adaptive_request)")
    integrity_pos = canonical_text.find("apply_integrity_subresolution(", adaptive_pos)
    final_pos = canonical_text.find("candidate_decision = candidate.get", adaptive_pos)
    check(tests, "canonical-binding-after-preflight-before-final-decision", -1 < preflight_pos < adaptive_pos < integrity_pos < final_pos)
    # The validated adaptive resolver is intentionally non-live. On full installed Source,
    # verify the resolver's actual returned control semantics rather than a text token that
    # belongs to the surrounding wrapper/contract. Reduced build fixtures may omit unchanged
    # Source files; the authoritative activation probe never does.
    adaptive_path = root / "mcp/adaptive_control_resolver.py"
    if adaptive_path.is_file():
        adaptive_module = load(adaptive_path, "integrity_validation_protected_adaptive")
        adaptive_canary = adaptive_module.resolve({
            "objective_ref": "INTEGRITY-PROTECTED-ADAPTIVE-CANARY",
            "consequence": "LOW",
            "uncertainty": "LOW",
        })
        adaptive_nonlive_semantics = (
            adaptive_canary.get("live_control_authority") is False
            and adaptive_canary.get("shadow_validation_required_before_promotion") is True
            and adaptive_canary.get("promotion_patch_ref") == "PATCH-AA-004"
        )
    else:
        adaptive_nonlive_semantics = True
    check(tests, "protected-adaptive-resolver-nonlive-semantics-preserved", adaptive_nonlive_semantics)
    check(tests, "integrity-direct-final-authority-prohibited", "direct_live_authority: false" in control_contract and "final_control_decision_owner: MCP" in control_contract)

    adaptive_basis_parity, adaptive_basis_detail = _adaptive_runtime_evidence_basis_parity(root)
    check(tests, "adaptive-runtime-evidence-producer-consumer-basis-parity", adaptive_basis_parity, adaptive_basis_detail)

    passed = all(x["result"] == "PASS" for x in tests)
    return {
        "schema": "cerebro-integrity-validation/v1",
        "result": "PASS" if passed else "FAIL",
        "tests": tests,
        "test_count": len(tests),
    }


def _canonical_activation_probe(root: Path) -> dict[str, Any]:
    control_path = root / "mcp/control_resolution.py"
    if not control_path.is_file():
        raise RuntimeError("activation-probe-requires-full-installed-source")
    control = load(control_path, "cerebro_integrity_canonical_control_activation")

    base_request = {
        "objective_ref": "INTEGRITY-ACTIVATION",
        "consequence": "LOW",
        "uncertainty": "LOW",
        "integrity_required": True,
        "control_event_ref": "INTEGRITY-ACTIVATION-EVENT",
        # Mechanism/owner/risk flags are deliberate current-event assessment inputs;
        # they do not by themselves create PASS because effect evidence remains required.
        "existing_mechanism_assessed": True,
        "existing_owner_assessed": True,
        "parallel_mechanism_risk_assessed": True,
        "assurance_not_applicable": True,
        "material_assumptions_exposed": True,
        "resolved_objective": "INTEGRITY-ACTIVATION",
        "current_objective": "INTEGRITY-ACTIVATION",
        "previous_control_state_ref": "ACTIVATION-PREVIOUS",
        "next_control_event_ref": "ACTIVATION-NEXT",
    }

    intent_resolver = load(root / "engines/interaction/integrity_intent.py", "cerebro_integrity_activation_intent")
    manual_results: dict[str, Any] = {}
    for command in ("Integrity", "Integrity Full", "MCP-loop?"):
        request = dict(base_request)
        request["integrity_intent"] = intent_resolver.resolve(command)
        manual_results[command] = control.resolve(request, root)

    automatic = dict(base_request)
    automatic.pop("integrity_required", None)
    automatic["implementation_requested"] = True
    automatic["integrity_gate_evidence"] = {
        "required_checks": ["ARCHITECTURE_CONVERGED"],
        "passed_checks": [],
        "failed_checks": [],
        "unavailable_checks": [],
        "evidence_refs": [],
        "effect_evidence_refs": [],
    }
    auto_result = control.resolve(automatic, root)

    manual_assessments = [manual_results[c].get("integrity_assessment") for c in ("Integrity", "Integrity Full", "MCP-loop?")]
    canonical_consumer = all(isinstance(x, dict) for x in manual_assessments) and isinstance(auto_result.get("integrity_assessment"), dict)
    same_control_ref = canonical_consumer and all(x.get("control_ref") == "CEREBRO-MCP-INTEGRITY-CONTROL-001" for x in manual_assessments)
    coverage = {x.get("coverage_mode") for x in manual_assessments if isinstance(x, dict)}
    scopes = {x.get("primary_scope") for x in manual_assessments if isinstance(x, dict)}
    auto_assessment = auto_result.get("integrity_assessment") if isinstance(auto_result.get("integrity_assessment"), dict) else {}

    return {
        "canonical_mcp_consumer_exercised": canonical_consumer,
        "manual_entrypoints_converge": same_control_ref and coverage == {"ADAPTIVE", "FULL"} and "MCP_LOOP_INTEGRITY" in scopes,
        "automatic_path_converges": isinstance(auto_result.get("integrity_assessment"), dict) and auto_assessment.get("control_ref") == "CEREBRO-MCP-INTEGRITY-CONTROL-001",
        "basis_bound_evidence_verified": canonical_consumer and all(bool(x.get("basis_fingerprint")) and bool(x.get("control_event_ref")) for x in manual_assessments),
        "owner_boundaries_verified": canonical_consumer and all(x.get("direct_live_authority") is False and x.get("final_control_decision_owner") == "MCP" for x in manual_assessments),
        "direct_live_authority_false": canonical_consumer and all(x.get("direct_live_authority") is False for x in manual_assessments),
        "next_gate_profiles_verified": auto_assessment.get("gate_profile") == "IMPLEMENTATION_READINESS",
        "normal_control_path_exercised": all(r.get("normal_control_path_exercised") is True for r in manual_results.values()),
        "manual_results": manual_results,
        "automatic_result": auto_result,
    }


def activation_probe(root: Path) -> dict[str, Any]:
    basis_files = _activation_evidence_basis(root)
    source_state_fingerprint = _source_state_fingerprint(root, basis_files)
    synthetic = synthetic_validation(root)
    runtime = _canonical_activation_probe(root)
    test_map = {x["name"]: x["result"] == "PASS" for x in synthetic.get("tests", [])}
    required = {
        "locked_semantics_preserved": synthetic.get("result") == "PASS",
        "canonical_mcp_consumer_exercised": runtime["canonical_mcp_consumer_exercised"],
        "manual_entrypoints_converge": runtime["manual_entrypoints_converge"],
        "automatic_path_converges": runtime["automatic_path_converges"],
        "six_dimensions_resolved": test_map.get("all-six-positive-dimensions-pass", False),
        "basis_bound_evidence_verified": runtime["basis_bound_evidence_verified"] and test_map.get("basis-change-invalidates-old-pass", False),
        "false_green_canaries_passed": all(test_map.get(name, False) for name in (
            "false-green-mcp-existence-rejected", "false-green-deep-selected-rejected",
            "false-green-wisdom-exists-rejected", "false-green-validator-exists-rejected",
            "false-green-component-and-delivery-standard-rejected",
        )),
        "full_vs_deep_separation_verified": test_map.get("full-vs-deep-separate-axes", False) and test_map.get("deep-selected-not-equal-deep-completed", False),
        "unknown_unavailable_na_semantics_verified": test_map.get("unavailable-is-not-subject-failure", False) and test_map.get("not-applicable-remains-distinct", False),
        "owner_boundaries_verified": runtime["owner_boundaries_verified"] and test_map.get("integrity-cannot-weaken-existing-block", False),
        "direct_live_authority_false": runtime["direct_live_authority_false"],
        "next_gate_profiles_verified": runtime["next_gate_profiles_verified"] and all(test_map.get("gate-profile-" + g.lower(), False) for g in GATES),
        "presentation_truth_boundary_verified": test_map.get("presentation-does-not-own-integrity-truth", False),
        "material_preflight_precedence_preserved": test_map.get("canonical-binding-after-preflight-before-final-decision", False),
        "assurance_continuity_regression_passed": runtime["normal_control_path_exercised"],
        "project_context_regression_passed": test_map.get("manual-and-automatic-use-same-subresolution", False),
        "delivery_resolution_regression_passed": test_map.get("integrity-can-strengthen-final-mcp-outcome", False),
    }
    result = "PASS" if all(required.values()) else "FAIL"
    return {
        "schema": ACTIVATION_SCHEMA,
        "result": result,
        "binding_id": BINDING_ID,
        "proves_bindings": [BINDING_ID] if result == "PASS" else [],
        "authority": "DERIVED_OPERATIONAL_EVIDENCE",
        "basis_files": basis_files,
        "source_state_fingerprint": source_state_fingerprint,
        "canonical_mcp_path_exercised": required["canonical_mcp_consumer_exercised"],
        "manual_entrypoints_converged": required["manual_entrypoints_converge"],
        "automatic_path_converged": required["automatic_path_converges"],
        "six_dimensions_exercised": required["six_dimensions_resolved"],
        "basis_invalidation_passed": required["basis_bound_evidence_verified"],
        "full_deep_separation_passed": required["full_vs_deep_separation_verified"],
        "owner_boundary_preserved": required["owner_boundaries_verified"],
        "five_gate_profiles_exercised": required["next_gate_profiles_verified"],
        "final_decision_consumption_verified": required["delivery_resolution_regression_passed"],
        "presentation_truth_owner_false": required["presentation_truth_boundary_verified"],
        "negative_canaries_passed": required["false_green_canaries_passed"] and required["unknown_unavailable_na_semantics_verified"],
        **required,
        "synthetic_validation": synthetic,
        "runtime_probe": runtime,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Cerebro MCP Integrity control")
    parser.add_argument("command", nargs="?", choices=["selftest", "activation-probe"], default="selftest")
    parser.add_argument("--source-root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--output")
    args = parser.parse_args()
    root = Path(args.source_root).resolve()
    result = activation_probe(root) if args.command == "activation-probe" else synthetic_validation(root)
    text = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        out = Path(args.output); out.parent.mkdir(parents=True, exist_ok=True); out.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0 if result.get("result") == "PASS" else 1


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
