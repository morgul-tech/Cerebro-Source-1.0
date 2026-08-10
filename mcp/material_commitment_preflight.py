#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

SOURCE_ROOT = Path(__file__).resolve().parents[1]
MATERIAL_STAGES = {"DECIDE", "LOCK", "MATERIAL_EXECUTE", "MATERIAL_AUTHORIZE", "GOVERNING_PUBLISH"}
EXPLORATORY_STAGES = {"UNDERSTAND_FRAME", "EXPLORE_RESEARCH", "CLARIFY", "REFINE", "CRITIQUE", "COMPARE_CONVERGE"}
CONFLICT_STATES = {"NOT_ASSESSED", "NONE_FOUND", "POTENTIAL", "CONFIRMED", "UNRESOLVED", "ASSESSMENT_FAILED"}
ACTIVATION_BASIS_FILES = [
    "standards/development/material-commitment-preflight.yaml",
    "standards/development/relevance-retrieval.yaml",
    "standards/development/wisdom-control-binding.yaml",
    "standards/control-architecture.yaml",
    "standards/mcp.yaml",
    "mcp/manifest.yaml",
    "mcp/material_commitment_preflight.py",
    "tooling/context/relevance_engine.py",
    "standards/delivery-kernel.yaml",
    "tooling/delivery/Cerebro.StandardDeliveryKernel.ps1",
    "tooling/runtime-host/cerebro_runtime.ps1",
    "tooling/builder/cerebro_runtime_release.ps1",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(text.split())


def source_state_fingerprint(root: Path, paths: list[str] | None = None) -> str:
    rows: list[str] = []
    for relative in sorted(paths or ACTIVATION_BASIS_FILES):
        path = root / relative
        if not path.is_file():
            raise ValueError(f"activation-basis-file-missing:{relative}")
        rows.append(f"{relative}|{sha256_file(path)}")
    return sha256_bytes("\n".join(rows).encode("utf-8"))


def load_relevance_engine(root: Path):
    path = root / "tooling/context/relevance_engine.py"
    spec = importlib.util.spec_from_file_location("cerebro_relevance_engine", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("relevance-engine-load-failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def authoritative_source_identity(root: Path, request: dict[str, Any]) -> str:
    supplied = str(request.get("authoritative_source_commit") or "").strip().lower()
    try:
        result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], text=True, capture_output=True, check=False)
        if result.returncode == 0 and len(result.stdout.strip()) == 40:
            observed = result.stdout.strip().lower()
            if supplied and supplied != observed:
                raise ValueError(f"authoritative-source-commit-mismatch:expected={supplied}:observed={observed}")
            return observed
    except OSError:
        pass
    if supplied:
        return supplied
    return "source-fingerprint:" + source_state_fingerprint(root)


def context_identity(root: Path) -> str:
    return sha256_file(root / "engines/context/working-context.yaml")


def semantic_resolution(request: dict[str, Any]) -> dict[str, str]:
    return {
        "state": str(request.get("semantic_resolution_state") or "UNRESOLVED").upper(),
        "objective": normalize(request.get("resolved_objective") or request.get("current_objective")),
        "scope": normalize(request.get("resolved_scope") or request.get("current_scope")),
        "intent": normalize(request.get("resolved_intent") or request.get("intent") or request.get("commitment_target")),
    }


def deterministic_context_conflicts(root: Path) -> list[str]:
    try:
        doc = yaml.safe_load((root / "engines/context/working-context.yaml").read_text(encoding="utf-8"))
        working = doc.get("working_context", {}) if isinstance(doc, dict) else {}
        records = [item for item in working.get("records", []) if isinstance(item, dict)]
        index = working.get("current_index", {}) if isinstance(working.get("current_index"), dict) else {}
        current_refs: set[str] = set()
        for key in ("decision_refs", "current_basis_refs", "override_refs", "current_wisdom_refs"):
            values = index.get(key, [])
            if isinstance(values, list):
                current_refs.update(str(value) for value in values)
        by_id = {str(item.get("id")): item for item in records if item.get("id")}
        findings: list[str] = []
        for ref in sorted(current_refs):
            if ref not in by_id:
                findings.append("CURRENT_INDEX_REF_MISSING:" + ref)
                continue
            relations = by_id[ref].get("relations", {}) if isinstance(by_id[ref].get("relations"), dict) else {}
            if relations.get("superseded_by_ref"):
                findings.append("CURRENT_RECORD_SUPERSEDED:" + ref)
            if relations.get("revoked_by_ref"):
                findings.append("CURRENT_RECORD_REVOKED:" + ref)
        return findings
    except Exception as exc:
        return ["CURRENT_CONTEXT_CONFLICT_CHECK_FAILED:" + type(exc).__name__]


def conflict_resolution(request: dict[str, Any], root: Path) -> dict[str, Any]:
    value = request.get("conflict_assessment")
    if isinstance(value, dict):
        state = str(value.get("state") or "NOT_ASSESSED").upper()
        refs = sorted(str(item) for item in value.get("refs", []))
    else:
        state = "NOT_ASSESSED"
        refs = []
    if state not in CONFLICT_STATES:
        state = "ASSESSMENT_FAILED"
    deterministic = deterministic_context_conflicts(root)
    if deterministic:
        state = "CONFIRMED" if not any(item.startswith("CURRENT_CONTEXT_CONFLICT_CHECK_FAILED:") for item in deterministic) else "ASSESSMENT_FAILED"
        refs = sorted(set(refs + deterministic))
    return {"state": state, "refs": refs, "deterministic_findings": deterministic}


def build_control_state(request: dict[str, Any], retrieval: dict[str, Any], semantics: dict[str, str], conflict: dict[str, Any], source_identity: str, current_context_identity: str) -> dict[str, Any]:
    material = {
        "source_identity": source_identity,
        "context_identity": current_context_identity,
        "objective": semantics["objective"],
        "scope": semantics["scope"],
        "intent": semantics["intent"],
        "commitment_target": str(request.get("commitment_target") or ""),
        "stage": str(request.get("stage") or "UNDERSTAND_FRAME").upper(),
        "semantic_resolution_state": semantics["state"],
        "coverage_state": retrieval.get("coverage_state"),
        "conflict_state": conflict["state"],
        "conflict_refs": conflict["refs"],
        "relevance_basis_fingerprint": retrieval.get("basis_fingerprint"),
        "applicable_knowledge_refs": retrieval.get("applicable_knowledge_refs", []),
        "applicable_wisdom_refs": retrieval.get("applicable_wisdom_refs", []),
        "applicable_history_refs": retrieval.get("applicable_history_refs", []),
    }
    fingerprint = sha256_bytes(json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return {
        "schema": "cerebro-control-state/material-commitment-v1",
        "control_state_id": "CTRL-" + fingerprint[:16].upper(),
        "domain": str(request.get("domain") or "CEREBRO"),
        "objective_ref": str(request.get("objective_ref") or semantics["objective"]),
        "governing_basis_refs": sorted(str(item) for item in request.get("governing_basis_refs", [])),
        "effective_user_configuration": str(request.get("effective_user_configuration") or "CURRENT_EXPLICIT_USER_INSTRUCTION"),
        "execution_profile_ref": str(request.get("execution_profile_ref") or "UNRESOLVED"),
        "applicable_wisdom_refs": list(retrieval.get("applicable_wisdom_refs", [])),
        "applicable_knowledge_refs": list(retrieval.get("applicable_knowledge_refs", [])),
        "applicable_history_refs": list(retrieval.get("applicable_history_refs", [])),
        "coverage_state": str(retrieval.get("coverage_state") or "FAILED"),
        "conflict_state": conflict["state"],
        "semantic_resolution_state": semantics["state"],
        "progress_state": str(request.get("progress_state") or "PREFLIGHT"),
        "failure_state": str(request.get("current_failure_state") or "NONE"),
        "verification_state": "PREFLIGHT_EVALUATED",
        "capability_state": str(request.get("capability_state") or "UNRESOLVED"),
        "human_boundary": str(request.get("human_boundary") or "NONE"),
        "source_identity": source_identity,
        "current_context_identity": current_context_identity,
        "relevance_basis_fingerprint": retrieval.get("basis_fingerprint"),
        "basis_fingerprint": fingerprint,
    }


def mcp_decide(request: dict[str, Any], control_state: dict[str, Any], retrieval: dict[str, Any]) -> dict[str, Any]:
    stage = str(request.get("stage") or "UNDERSTAND_FRAME").upper()
    material = bool(request.get("material")) or stage in MATERIAL_STAGES
    blockers: list[str] = []
    if material:
        if control_state["semantic_resolution_state"] != "RESOLVED":
            blockers.append("SEMANTIC_RESOLUTION_NOT_RESOLVED")
        if retrieval.get("retrieval_state") != "COMPLETE":
            blockers.append("RETRIEVAL_NOT_COMPLETE")
        if control_state["coverage_state"] != "COMPLETE":
            blockers.append("COVERAGE_NOT_COMPLETE")
        if control_state["conflict_state"] != "NONE_FOUND":
            blockers.append("CONFLICT_NOT_CLEARED")
        if not str(request.get("commitment_target") or "").strip():
            blockers.append("COMMITMENT_TARGET_MISSING")
    if retrieval.get("human_insight_checkpoint") == "CRITIQUE_AND_REASSESS":
        blockers.append("MATERIAL_USER_INSIGHT_REQUIRES_REASSESSMENT")

    if blockers:
        outcome = "BLOCK"
        verification = "RE_RESOLVE_CONTROL_BEFORE_COMMITMENT"
    else:
        outcome = "CONTINUE"
        verification = "FRESH_PREFLIGHT_RECEIPT_REQUIRED" if material else "STAGE_AWARE_CONTINUE"

    decision_material = {
        "control_state_ref": control_state["control_state_id"],
        "basis_fingerprint": control_state["basis_fingerprint"],
        "stage": stage,
        "outcome": outcome,
        "blockers": blockers,
    }
    digest = sha256_bytes(json.dumps(decision_material, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return {
        "schema": "cerebro-mcp-control-decision/material-commitment-v1",
        "control_decision_id": "MCPD-" + digest[:16].upper(),
        "control_state_ref": control_state["control_state_id"],
        "objective_ref": control_state["objective_ref"],
        "basis_refs": sorted(control_state["governing_basis_refs"] + control_state["applicable_knowledge_refs"] + control_state["applicable_wisdom_refs"] + control_state["applicable_history_refs"]),
        "basis_fingerprint": control_state["basis_fingerprint"],
        "effective_user_config_ref": control_state["effective_user_configuration"],
        "execution_profile_ref": control_state["execution_profile_ref"],
        "applicable_control_refs": ["CEREBRO-MATERIAL-COMMITMENT-PREFLIGHT-001", "CEREBRO-RELEVANCE-RETRIEVAL-001", "CEREBRO-WISDOM-CONTROL-BINDING-001"],
        "outcome": outcome,
        "invalidates": blockers,
        "verification_requirement": verification,
        "human_boundary": control_state["human_boundary"],
        "evidence_scope": "CURRENT_RELEVANCE_AND_PRIOR_LEARNING",
        "resolved_at": utc_now(),
    }


def resolve(request: dict[str, Any], root: Path = SOURCE_ROOT) -> dict[str, Any]:
    stage = str(request.get("stage") or "UNDERSTAND_FRAME").upper()
    material = bool(request.get("material")) or stage in MATERIAL_STAGES
    if stage not in MATERIAL_STAGES | EXPLORATORY_STAGES:
        raise ValueError(f"unsupported-stage:{stage}")
    semantics = semantic_resolution(request)
    conflict = conflict_resolution(request, root)
    source_identity = authoritative_source_identity(root, request)
    current_context_identity = context_identity(root)
    engine = load_relevance_engine(root)
    retrieval_request = {
        "current_objective": semantics["objective"] or request.get("current_objective"),
        "current_scope": semantics["scope"] or request.get("current_scope"),
        "objective_ref": request.get("objective_ref"),
        "current_failure_state": request.get("current_failure_state"),
        "current_decision_state": request.get("current_decision_state"),
        "tags": request.get("tags", []),
        "material_user_insight": request.get("material_user_insight"),
        "expected_prior_learning": request.get("expected_prior_learning", material and str(request.get("domain") or "CEREBRO").upper() == "CEREBRO"),
        "coverage_audit_complete": request.get("coverage_audit_complete", False),
        "coverage_audit_refs": request.get("coverage_audit_refs", []),
    }
    retrieval = engine.retrieve(retrieval_request, root)
    control_state = build_control_state(request, retrieval, semantics, conflict, source_identity, current_context_identity)
    decision = mcp_decide(request, control_state, retrieval)
    receipt = {
        "schema": "cerebro-material-commitment-preflight-receipt/v1",
        "result": "PASS" if decision["outcome"] == "CONTINUE" else "BLOCKED",
        "material": material,
        "stage": stage,
        "commitment_target": str(request.get("commitment_target") or ""),
        "source_identity": source_identity,
        "current_context_identity": current_context_identity,
        "resolved_objective": semantics["objective"],
        "resolved_scope": semantics["scope"],
        "resolved_intent": semantics["intent"],
        "semantic_resolution_state": semantics["state"],
        "coverage_state": control_state["coverage_state"],
        "conflict_state": control_state["conflict_state"],
        "applicable_knowledge_refs": control_state["applicable_knowledge_refs"],
        "applicable_wisdom_refs": control_state["applicable_wisdom_refs"],
        "applicable_history_refs": control_state["applicable_history_refs"],
        "relevance_source_fingerprints": retrieval.get("source_fingerprints", {}),
        "basis_fingerprint": control_state["basis_fingerprint"],
        "control_state_ref": control_state["control_state_id"],
        "control_decision_ref": decision["control_decision_id"],
        "issued_at": utc_now(),
        "authority": "DERIVED_CONTROL_EVIDENCE",
    }
    return {
        "schema": "cerebro-material-commitment-preflight-result/v1",
        "result": receipt["result"],
        "context_invoked": True,
        "mcp_consumed": True,
        "retrieval": retrieval,
        "control_state": control_state,
        "mcp_control_decision": decision,
        "receipt": receipt,
    }


def _freshness_equal(left: Any, right: Any) -> bool:
    return json.dumps(left, sort_keys=True, separators=(",", ":"), ensure_ascii=False) == json.dumps(right, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def consume(request: dict[str, Any], receipt: dict[str, Any], root: Path = SOURCE_ROOT) -> dict[str, Any]:
    current = resolve(request, root)
    reasons: list[str] = []
    if receipt.get("schema") != "cerebro-material-commitment-preflight-receipt/v1":
        reasons.append("RECEIPT_SCHEMA_INVALID")
    if receipt.get("result") != "PASS":
        reasons.append("RECEIPT_NOT_PASS")
    if current["result"] != "PASS":
        reasons.append("CURRENT_PREFLIGHT_NOT_PASS")

    freshness_fields = (
        "basis_fingerprint",
        "commitment_target",
        "stage",
        "source_identity",
        "current_context_identity",
        "resolved_objective",
        "resolved_scope",
        "resolved_intent",
        "semantic_resolution_state",
        "coverage_state",
        "conflict_state",
        "applicable_knowledge_refs",
        "applicable_wisdom_refs",
        "applicable_history_refs",
        "relevance_source_fingerprints",
    )
    for field in freshness_fields:
        if not _freshness_equal(receipt.get(field), current["receipt"].get(field)):
            reasons.append("STALE_OR_MISMATCHED_" + field.upper())

    passed = not reasons
    return {
        "schema": "cerebro-material-commitment-consumption/v1",
        "result": "PASS" if passed else "BLOCK",
        "binding_id": "STANDARD_DELIVERY_MATERIAL_PREFLIGHT_CALL_PATH",
        "proves_bindings": ["STANDARD_DELIVERY_MATERIAL_PREFLIGHT_CALL_PATH"],
        "normal_call_path_exercised": True,
        "context_invoked": current.get("context_invoked") is True,
        "mcp_consumed": current.get("mcp_consumed") is True,
        "receipt_consumed": passed,
        "freshness_verified": passed,
        "resolved_intent_verified": not any(reason.endswith("RESOLVED_INTENT") for reason in reasons),
        "reasons": reasons,
        "current_basis_fingerprint": current["receipt"]["basis_fingerprint"],
        "receipt_basis_fingerprint": receipt.get("basis_fingerprint"),
        "control_decision_ref": current["mcp_control_decision"]["control_decision_id"],
        "source_state_fingerprint": source_state_fingerprint(root) if all((root / relative).is_file() for relative in ACTIVATION_BASIS_FILES) else "",
        "basis_files": ACTIVATION_BASIS_FILES,
    }


def _write_fixture(root: Path) -> None:
    (root / "engines/context").mkdir(parents=True, exist_ok=True)
    (root / "tooling/runtime-host").mkdir(parents=True, exist_ok=True)
    (root / "tooling/builder").mkdir(parents=True, exist_ok=True)
    (root / "cerebro.yaml").write_text("schema: cerebro-manifest/v1\n", encoding="utf-8")
    docs = {
        "knowledge.yaml": {"knowledge": {"records": [
            {"id": "K1", "claim": "PowerShell delivery requires exact hash verification", "scope": "delivery", "status": "ACTIVE", "verification": {"state": "VERIFIED"}, "contradiction": {"state": "NONE"}, "validity": {"state": "CURRENT"}, "tags": ["powershell"]}
        ]}},
        "working-context.yaml": {"working_context": {"records": [
            {"id": "W1", "type": "WISDOM_RECORD", "scope": "delivery", "statement": "Use exact hash verification before human handoff", "payload": {}, "relations": {}},
            {"id": "WOLD", "type": "WISDOM_RECORD", "scope": "delivery", "statement": "Old delivery hash guidance", "payload": {}, "relations": {}},
            {"id": "WREV", "type": "WISDOM_RECORD", "scope": "delivery", "statement": "Revoked delivery guidance", "payload": {}, "relations": {"revoked_by_ref": "W1"}},
        ], "current_index": {"current_wisdom_refs": ["W1"]}}},
        "wisdom-evidence.yaml": {"wisdom_evidence": {"profiles": []}},
        "development-history.yaml": {"development_history": {"records": [
            {"id": "H1", "role": "EVENT", "event_class": "LEARNING_EVENT", "significance": "MAJOR", "title": "Delivery hash learning", "fact": "Exact hash verification prevented repeat failure", "impact": {}, "relations": {}, "provenance": {}}
        ]}},
    }
    for name, doc in docs.items():
        (root / "engines/context" / name).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    (root / "tooling/runtime-host/cerebro_runtime.ps1").write_text("# runtime transition only\n", encoding="utf-8")
    (root / "tooling/builder/cerebro_runtime_release.ps1").write_text("# runtime release builder without reasoning policy\n", encoding="utf-8")


def selftest(root: Path = SOURCE_ROOT) -> dict[str, Any]:
    tests: list[dict[str, Any]] = []
    def check(name: str, ok: bool, detail: str = "") -> None:
        tests.append({"name": name, "result": "PASS" if ok else "FAIL", "detail": detail})
    engine = load_relevance_engine(root)
    engine_result = engine.selftest()
    check("context-relevance-engine-selftest", engine_result.get("result") == "PASS")
    with tempfile.TemporaryDirectory() as tmp:
        fixture = Path(tmp); _write_fixture(fixture)
        # Use candidate relevance engine implementation while fixture supplies state.
        fixture_engine_target = fixture / "tooling/context/relevance_engine.py"
        fixture_engine_target.parent.mkdir(parents=True, exist_ok=True)
        fixture_engine_target.write_bytes((root / "tooling/context/relevance_engine.py").read_bytes())
        fixture_mcp = fixture / "mcp"; fixture_mcp.mkdir(parents=True, exist_ok=True)
        request = {
            "stage": "MATERIAL_AUTHORIZE", "material": True,
            "current_objective": "implement PowerShell delivery hash verification",
            "current_scope": "delivery", "resolved_objective": "implement powershell delivery hash verification",
            "resolved_scope": "delivery", "resolved_intent": "authorize implementation",
            "semantic_resolution_state": "RESOLVED", "commitment_target": "PATCH-X",
            "conflict_assessment": {"state": "NONE_FOUND", "refs": []},
            "expected_prior_learning": True, "authoritative_source_commit": "a" * 40,
        }
        first = resolve(request, fixture)
        check("material-preflight-pass", first["result"] == "PASS")
        check("context-invoked", first.get("context_invoked") is True)
        check("mcp-consumed-retrieval", first.get("mcp_consumed") is True and first["mcp_control_decision"]["basis_fingerprint"] == first["control_state"]["basis_fingerprint"])
        check("current-wisdom-only", first["retrieval"]["applicable_wisdom_refs"] == ["W1"])
        # Deterministic current-context integrity must override a caller's NONE_FOUND claim.
        conflict_doc = yaml.safe_load((fixture / "engines/context/working-context.yaml").read_text(encoding="utf-8"))
        conflict_doc["working_context"]["current_index"]["current_wisdom_refs"] = ["W1", "WREV"]
        (fixture / "engines/context/working-context.yaml").write_text(yaml.safe_dump(conflict_doc, sort_keys=False), encoding="utf-8")
        conflict_result = resolve(request, fixture)
        check("deterministic-current-context-conflict-overrides-none-found", conflict_result["result"] == "BLOCKED" and conflict_result["control_state"]["conflict_state"] == "CONFIRMED")
        _write_fixture(fixture)
        fixture_engine_target.parent.mkdir(parents=True, exist_ok=True)
        fixture_engine_target.write_bytes((root / "tooling/context/relevance_engine.py").read_bytes())
        consumed = consume(request, first["receipt"], fixture)
        check("fresh-receipt-consumed", consumed["result"] == "PASS" and consumed["receipt_consumed"])
        changed_intent = dict(request)
        changed_intent["resolved_intent"] = "authorize a materially different implementation"
        intent_stale = consume(changed_intent, first["receipt"], fixture)
        check("changed-intent-stales-receipt", intent_stale["result"] == "BLOCK" and "STALE_OR_MISMATCHED_RESOLVED_INTENT" in intent_stale["reasons"])
        changed_objective = dict(request)
        changed_objective["resolved_objective"] = "implement a materially different delivery objective"
        objective_stale = consume(changed_objective, first["receipt"], fixture)
        check("changed-objective-stales-receipt", objective_stale["result"] == "BLOCK")
        # Change current context -> prior receipt must become stale.
        context_path = fixture / "engines/context/working-context.yaml"
        context_doc = yaml.safe_load(context_path.read_text(encoding="utf-8"))
        context_doc["working_context"]["current_index"]["current_wisdom_refs"] = []
        context_path.write_text(yaml.safe_dump(context_doc, sort_keys=False), encoding="utf-8")
        stale = consume(request, first["receipt"], fixture)
        check("changed-context-stales-receipt", stale["result"] == "BLOCK" and not stale["freshness_verified"])
        # Restore state and prove unresolved semantic/coverage paths block material commitment.
        _write_fixture(fixture)
        fixture_engine_target.parent.mkdir(parents=True, exist_ok=True)
        fixture_engine_target.write_bytes((root / "tooling/context/relevance_engine.py").read_bytes())
        unresolved = dict(request); unresolved["semantic_resolution_state"] = "UNRESOLVED"
        check("unresolved-semantics-block-material", resolve(unresolved, fixture)["result"] == "BLOCKED")
        missing = dict(request); missing["current_objective"] = "unmatched topic"; missing["resolved_objective"] = "unmatched topic"; missing["current_scope"] = "unmatched"; missing["resolved_scope"] = "unmatched"
        check("expected-prior-learning-without-coverage-blocks", resolve(missing, fixture)["control_state"]["coverage_state"] == "INCOMPLETE" and resolve(missing, fixture)["result"] == "BLOCKED")
        exploratory = dict(unresolved); exploratory["stage"] = "EXPLORE_RESEARCH"; exploratory["material"] = False; exploratory["commitment_target"] = ""
        check("non-material-stage-does-not-require-resolved-semantics", resolve(exploratory, fixture)["result"] == "PASS")
    return {"schema": "cerebro-material-commitment-preflight-selftest/v1", "result": "PASS" if all(t["result"] == "PASS" for t in tests) else "FAIL", "tests": tests}


def activation_probe(root: Path, output: Path) -> dict[str, Any]:
    result = selftest(root)
    runtime_text = (root / "tooling/runtime-host/cerebro_runtime.ps1").read_text(encoding="utf-8", errors="replace")
    release_text = (root / "tooling/builder/cerebro_runtime_release.ps1").read_text(encoding="utf-8", errors="replace")
    forbidden = ("relevance_engine.py", "material_commitment_preflight.py", "MATERIAL_COMMITMENT_PREFLIGHT")
    runtime_clean = not any(token in runtime_text for token in forbidden) and not any(token in release_text for token in forbidden)
    test_map = {item.get("name"): item.get("result") == "PASS" for item in result.get("tests", [])}
    tests_pass = result.get("result") == "PASS"
    proof = {
        "schema": "cerebro-relevance-mcp-activation-proof/v1",
        "result": "PASS" if tests_pass and runtime_clean else "FAIL",
        "binding_id": "RELEVANCE_MATERIAL_COMMITMENT_PREFLIGHT",
        "proves_bindings": [
            "RELEVANCE_MATERIAL_COMMITMENT_PREFLIGHT",
            "WISDOM_MCP_CONTROL_CONSUMPTION",
            "MATERIAL_COMMITMENT_PREFLIGHT",
        ],
        "context_invoked": bool(test_map.get("context-invoked")),
        "mcp_consumed": bool(test_map.get("mcp-consumed-retrieval")),
        "commitment_gate_exercised": bool(test_map.get("material-preflight-pass")),
        "stale_basis_rejected": bool(test_map.get("changed-context-stales-receipt")),
        "intent_freshness_enforced": bool(test_map.get("changed-intent-stales-receipt")),
        "objective_freshness_enforced": bool(test_map.get("changed-objective-stales-receipt")),
        "current_wisdom_enforced": bool(test_map.get("current-wisdom-only")),
        "coverage_gate_exercised": bool(test_map.get("expected-prior-learning-without-coverage-blocks")),
        "semantic_resolution_gate_exercised": bool(test_map.get("unresolved-semantics-block-material")),
        "deterministic_conflict_guard_exercised": bool(test_map.get("deterministic-current-context-conflict-overrides-none-found")),
        "freshness_consumption_verified": bool(test_map.get("fresh-receipt-consumed")),
        "runtime_reasoning_policy_absent": runtime_clean,
        "source_state_fingerprint": source_state_fingerprint(root) if all((root / relative).is_file() for relative in ACTIVATION_BASIS_FILES) else "",
        "basis_files": ACTIVATION_BASIS_FILES,
        "generated_at_utc": utc_now(),
        "selftest": result,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8")
    return proof


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"json-object-required:{path}")
    return value


def emit(value: dict[str, Any], output: str | None = None) -> int:
    text = json.dumps(value, indent=2) + "\n"
    if output:
        Path(output).write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if value.get("result") == "PASS" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Cerebro MCP Material Commitment Preflight")
    sub = parser.add_subparsers(dest="command", required=True)
    resolve_cmd = sub.add_parser("resolve")
    resolve_cmd.add_argument("--request", required=True); resolve_cmd.add_argument("--source-root"); resolve_cmd.add_argument("--output")
    consume_cmd = sub.add_parser("consume")
    consume_cmd.add_argument("--request", required=True); consume_cmd.add_argument("--receipt", required=True); consume_cmd.add_argument("--source-root"); consume_cmd.add_argument("--output")
    selftest_cmd = sub.add_parser("selftest"); selftest_cmd.add_argument("--source-root")
    probe_cmd = sub.add_parser("activation-probe"); probe_cmd.add_argument("--source-root", required=True); probe_cmd.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.command == "resolve":
        return emit(resolve(read_json(Path(args.request)), Path(args.source_root) if args.source_root else SOURCE_ROOT), args.output)
    if args.command == "consume":
        return emit(consume(read_json(Path(args.request)), read_json(Path(args.receipt)), Path(args.source_root) if args.source_root else SOURCE_ROOT), args.output)
    if args.command == "selftest":
        return emit(selftest(Path(args.source_root) if args.source_root else SOURCE_ROOT))
    return emit(activation_probe(Path(args.source_root), Path(args.output)))


if __name__ == "__main__":
    raise SystemExit(main())
