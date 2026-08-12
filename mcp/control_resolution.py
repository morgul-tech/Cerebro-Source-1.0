#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

SOURCE_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "cerebro-mcp-control-resolution/live-v1"
DECISION_SCHEMA = "cerebro-mcp-control-decision/adaptive-live-v1"
PROFILE_SCHEMA = "cerebro-execution-profile/adaptive-live-v1"
ACTIVATION_SCHEMA = "cerebro-aa004-mcp-control-activation-proof/v1"
BINDING_ID = "ADAPTIVE_MCP_CONTROL_RESOLUTION"
CONTROL_SURFACE_ID = "CEREBRO-MCP-CONTROL-RESOLUTION-001"
PROMOTION_SOURCE_COMMIT = "810a010d3a3395217b8263b3701b3e7d1c31ff33"
EXPECTED_CANDIDATE_BLOB = "c375639e0a37141e96d90da7bf00fb36d61651cf"
EXPECTED_CANDIDATE_CONTRACT_BLOB = "a8a2b9d55a9f4af2f7439ded5c66425818b384be"
EXPECTED_SHADOW_ORACLE_BLOB = "831f7edb0545c66e92a6cfe10d376af3e2fb278e"
MATERIAL_STAGES = {"DECIDE", "LOCK", "MATERIAL_EXECUTE", "MATERIAL_AUTHORIZE", "GOVERNING_PUBLISH"}
CONTROL_OUTCOMES = {"CONTINUE", "REMEDIATE", "RETRY", "REORIENT", "USER_DECISION_REQUIRED", "BLOCK"}
EVIDENCE_BASIS_FILES = [
    "mcp/control-resolution.yaml",
    "mcp/control_resolution.py",
    "mcp/adaptive-control-resolver.yaml",
    "mcp/adaptive_control_resolver.py",
    "tooling/change/adaptive-control-shadow-scenarios.json",
    "tooling/change/adaptive_control_shadow.py",
    "mcp/manifest.yaml",
    "standards/mcp.yaml",
    "standards/control-architecture.yaml",
    "standards/change-delivery.yaml",
    "standards/change-delivery-convergence.yaml",
    "tooling/delivery/cerebro_delivery.ps1",
    "tooling/delivery/selection-state-schema.json",
    "tooling/delivery/component.yaml",
    "tooling/delivery/Cerebro.StandardDeliveryKernel.ps1",
    "standards/delivery-kernel.yaml",
    "tooling/validator/target-runtime/Invoke-CerebroWindowsPowerShellValidation.ps1",
    "tooling/validator/target_runtime_validation.py",
    "standards/development/delivery-failure-regression.yaml",
    "tooling/validator/contract-activation-bindings.json",
    "engines/project/roadmap.yaml",
    "tooling/runtime-host/component.yaml",
    "tooling/runtime-host/cerebro_runtime.ps1",
]

DELIVERY_PROFILES = {"LIMITED", "STANDARD", "FULL"}
DELIVERY_PROFILE_ALIASES = {
    "STANDARD_A": "LIMITED",
    "STANDARD_B": "STANDARD",
    "STANDARD_C": "FULL",
}
DELIVERY_OPERATIONS = {"replace", "create", "delete"}

sys.dont_write_bytecode = True


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git_blob_sha(path: Path) -> str:
    data = path.read_bytes()
    return hashlib.sha1(f"blob {len(data)}".encode("ascii") + b"\x00" + data).hexdigest()


def source_state_fingerprint(root: Path) -> str:
    rows: list[str] = []
    for relative in sorted(EVIDENCE_BASIS_FILES):
        path = root / relative
        if not path.is_file():
            raise ValueError(f"activation-basis-file-missing:{relative}")
        rows.append(f"{relative}|{sha256_bytes(path.read_bytes())}")
    return sha256_bytes("\n".join(rows).encode("utf-8"))


def load_module(path: Path, name: str):
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"module-load-failed:{path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.dont_write_bytecode = previous


def git_head(root: Path) -> str | None:
    try:
        cp = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], text=True, capture_output=True, check=False)
    except OSError:
        return None
    value = cp.stdout.strip()
    return value if cp.returncode == 0 and len(value) == 40 else None


def git_blob_at(root: Path, ref: str, relative: str) -> str | None:
    try:
        cp = subprocess.run(
            ["git", "-C", str(root), "rev-parse", f"{ref}:{relative}"],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    value = cp.stdout.strip().lower()
    return value if cp.returncode == 0 and len(value) == 40 else None


def git_is_ancestor(root: Path, ancestor: str, head: str) -> bool:
    try:
        cp = subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", ancestor, head], text=True, capture_output=True, check=False)
    except OSError:
        return False
    return cp.returncode == 0


def _load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def _delivery_profile_controls(profile: str) -> dict[str, Any]:
    controls = {
        "LIMITED": {
            "execution_owner": "USER",
            "agent_local_access": "PROHIBITED",
            "access_request_budget": 0,
            "artifact_format": "FILES",
        },
        "STANDARD": {
            "execution_owner": "USER_LOCAL_RUNNER",
            "agent_local_access": "PROHIBITED",
            "access_request_budget": 0,
            "artifact_format": "PAYLOAD_PLUS_INSTALLER",
        },
        "FULL": {
            "execution_owner": "AGENT_CONTROLLED",
            "agent_local_access": "EXPLICIT_GRANT_REQUIRED",
            "access_request_budget": 1,
            "artifact_format": "CONTROLLED_TRANSACTION",
        },
    }
    if profile not in controls:
        raise ValueError(f"unknown-delivery-profile:{profile}")
    return dict(controls[profile])


def resolve_delivery_profile(request: dict[str, Any]) -> dict[str, Any]:
    """Resolve delivery capability inside the canonical MCP control owner."""
    requested_input = str(request.get("requested_delivery_profile") or "").upper()
    requested = DELIVERY_PROFILE_ALIASES.get(requested_input, requested_input)
    operations = [str(value).lower() for value in request.get("delivery_operations", [])]
    unknown_operations = sorted(set(operations).difference(DELIVERY_OPERATIONS))
    direct_access = bool(request.get("direct_workspace_access_declared"))
    classification = "DELIVERY_PROFILE_RESOLVED"
    reason = ""
    resolved: str | None = None

    if unknown_operations:
        classification = "UNKNOWN_PATCH_OPERATION"
        reason = ",".join(unknown_operations)
    elif requested == "AUTO":
        if direct_access:
            resolved = "FULL"
            reason = "direct-workspace-access-declared"
        elif not operations:
            classification = "INSUFFICIENT_CAPABILITY_EVIDENCE"
            reason = "AUTO requires patch operations or declared direct workspace access"
        elif all(operation == "replace" for operation in operations):
            resolved = "LIMITED"
            reason = "existing-file-replacements-only"
        else:
            resolved = "STANDARD"
            reason = "structured-file-operations-required"
    elif requested not in DELIVERY_PROFILES:
        classification = "UNKNOWN_DELIVERY_PROFILE"
        reason = "allowed=LIMITED,STANDARD,FULL,AUTO; aliases=STANDARD_A,STANDARD_B,STANDARD_C"
    elif requested == "LIMITED" and any(operation != "replace" for operation in operations):
        classification = "DELIVERY_PROFILE_NOT_APPLICABLE"
        reason = "LIMITED permits replacement of existing files only"
    else:
        resolved = requested
        reason = (
            "explicit-user-terminal-selection"
            if requested_input == requested
            else "legacy-alias-resolved-to-canonical-profile"
        )

    basis_material = {
        "requested_input": requested_input,
        "requested_profile": requested,
        "operations": operations,
        "direct_workspace_access_declared": direct_access,
        "resolved_profile": resolved,
        "classification": classification,
        "reason": reason,
        "authoritative_source_commit": str(request.get("authoritative_source_commit") or "UNKNOWN"),
        "contract": "STD-CHANGE-DELIVERY@0.11.0",
    }
    basis_fingerprint = sha256_bytes(
        json.dumps(basis_material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    return {
        "schema": "cerebro-mcp-delivery-profile-resolution/v1",
        "authority": "MCP",
        "result": "PASS" if resolved else "BLOCKED",
        "classification": classification,
        "requested_input": requested_input,
        "requested_profile": requested,
        "resolved_profile": resolved,
        "reason": reason,
        "operations": operations,
        "direct_workspace_access_declared": direct_access,
        "controls": _delivery_profile_controls(resolved) if resolved else None,
        "basis_fingerprint": basis_fingerprint,
        "source_commit": str(request.get("authoritative_source_commit") or "UNKNOWN"),
        "decision_owner": "MCP",
        "adapter_may_recompute": False,
    }


def _roadmap_patch_status(root: Path, patch_id: str) -> str:
    doc = _load_yaml(root / "engines/project/roadmap.yaml")
    roadmap = doc.get("roadmap", {}) if isinstance(doc, dict) else {}
    for patch in roadmap.get("patches", []) if isinstance(roadmap, dict) else []:
        if isinstance(patch, dict) and str(patch.get("id")) == patch_id:
            return str(patch.get("status") or "")
    return ""


def verify_promotion_basis(root: Path, require_git_ancestry: bool = True) -> dict[str, Any]:
    candidate = root / "mcp/adaptive_control_resolver.py"
    contract = root / "mcp/adaptive-control-resolver.yaml"
    oracle = root / "tooling/change/adaptive-control-shadow-scenarios.json"
    manifest_path = root / "mcp/manifest.yaml"
    control_contract_path = root / "mcp/control-resolution.yaml"
    checks: dict[str, Any] = {}
    observed_head = git_head(root)
    candidate_blob = git_blob_at(root, observed_head, "mcp/adaptive_control_resolver.py") if observed_head else None
    contract_blob = git_blob_at(root, observed_head, "mcp/adaptive-control-resolver.yaml") if observed_head else None
    oracle_blob = git_blob_at(root, observed_head, "tooling/change/adaptive-control-shadow-scenarios.json") if observed_head else None
    promotion_candidate_blob = git_blob_at(root, PROMOTION_SOURCE_COMMIT, "mcp/adaptive_control_resolver.py")
    promotion_contract_blob = git_blob_at(root, PROMOTION_SOURCE_COMMIT, "mcp/adaptive-control-resolver.yaml")
    promotion_oracle_blob = git_blob_at(root, PROMOTION_SOURCE_COMMIT, "tooling/change/adaptive-control-shadow-scenarios.json")
    checks["candidate_identity_verified"] = (
        candidate.is_file()
        and candidate_blob == EXPECTED_CANDIDATE_BLOB
        and promotion_candidate_blob == EXPECTED_CANDIDATE_BLOB
    )
    checks["candidate_contract_identity_verified"] = (
        contract.is_file()
        and promotion_contract_blob == EXPECTED_CANDIDATE_CONTRACT_BLOB
    )
    current_contract_semantics_verified = False
    try:
        current_contract = _load_yaml(contract)
        adaptive = current_contract.get("adaptive_control_resolver", {}) if isinstance(current_contract, dict) else {}
        history = adaptive.get("promotion_history", {}) if isinstance(adaptive, dict) else {}
        current_contract_semantics_verified = (
            str(adaptive.get("id")) == "CEREBRO-ADAPTIVE-CONTROL-RESOLVER-001"
            and str(adaptive.get("status")) == "VALIDATED_LOGIC"
            and str(adaptive.get("implementation_ref")) == "mcp/adaptive_control_resolver.py"
            and adaptive.get("live_control_authority") is False
            and adaptive.get("direct_live_authority") is False
            and str(adaptive.get("canonical_live_owner")) == "mcp/control-resolution.yaml"
            and str(history.get("shadow_validation")) == "PATCH-AA-003_VERIFIED"
            and str(history.get("controlled_promotion")) == "PATCH-AA-004_VERIFIED"
            and str(history.get("outcome")) == "RETAIN_AS_VALIDATED_LOGIC_BEHIND_CANONICAL_LIVE_WRAPPER"
        )
    except Exception:
        current_contract_semantics_verified = False
    checks["current_contract_semantics_verified"] = current_contract_semantics_verified
    checks["shadow_oracle_identity_verified"] = (
        oracle.is_file()
        and oracle_blob == EXPECTED_SHADOW_ORACLE_BLOB
        and promotion_oracle_blob == EXPECTED_SHADOW_ORACLE_BLOB
    )
    checks["git_object_identity_verified"] = (
        checks["candidate_identity_verified"]
        and checks["candidate_contract_identity_verified"]
        and checks["shadow_oracle_identity_verified"]
    )

    oracle_subject_ok = False
    if checks["shadow_oracle_identity_verified"]:
        try:
            suite = json.loads(oracle.read_text(encoding="utf-8"))
            subject = suite.get("subject", {}) if isinstance(suite, dict) else {}
            oracle_subject_ok = (
                str(subject.get("resolver_git_blob_sha")) == EXPECTED_CANDIDATE_BLOB
                and str(subject.get("contract_git_blob_sha")) == EXPECTED_CANDIDATE_CONTRACT_BLOB
                and subject.get("live_control_authority_expected") is False
            )
        except Exception:
            oracle_subject_ok = False
    checks["shadow_oracle_subject_matches_candidate"] = oracle_subject_ok
    checks["aa003_roadmap_verified"] = _roadmap_patch_status(root, "PATCH-AA-003") == "VERIFIED"

    manifest_ok = False
    try:
        m = _load_yaml(manifest_path)
        adapters = m.get("control_adapters", {}) if isinstance(m, dict) else {}
        live = adapters.get("control_resolution", {}) if isinstance(adapters, dict) else {}
        logic = adapters.get("adaptive_control_resolver", {}) if isinstance(adapters, dict) else {}
        manifest_ok = (
            isinstance(live, dict)
            and str(live.get("implementation_ref")) == "mcp/control_resolution.py"
            and str(live.get("status")) == "ACTIVE"
            and live.get("canonical") is True
            and live.get("live_control_authority") is True
            and isinstance(logic, dict)
            and str(logic.get("status")) == "VALIDATED_LOGIC"
            and logic.get("direct_live_authority") is False
        )
    except Exception:
        manifest_ok = False
    checks["mcp_registration_verified"] = manifest_ok

    control_contract_ok = False
    try:
        c = _load_yaml(control_contract_path)
        cr = c.get("control_resolution", {}) if isinstance(c, dict) else {}
        basis = cr.get("promotion_basis", {}) if isinstance(cr, dict) else {}
        control_contract_ok = (
            str(cr.get("id")) == CONTROL_SURFACE_ID
            and str(cr.get("status")) == "ACTIVE"
            and cr.get("live_control_authority") is True
            and str(basis.get("source_commit")) == PROMOTION_SOURCE_COMMIT
            and str(basis.get("resolver_git_blob_sha")) == EXPECTED_CANDIDATE_BLOB
            and str(basis.get("resolver_contract_git_blob_sha")) == EXPECTED_CANDIDATE_CONTRACT_BLOB
            and str(basis.get("shadow_oracle_git_blob_sha")) == EXPECTED_SHADOW_ORACLE_BLOB
        )
    except Exception:
        control_contract_ok = False
    checks["promotion_contract_verified"] = control_contract_ok

    checks["observed_source_head"] = observed_head
    checks["candidate_git_blob_at_head"] = candidate_blob
    checks["candidate_contract_git_blob_at_head"] = contract_blob
    checks["shadow_oracle_git_blob_at_head"] = oracle_blob
    checks["identity_authority"] = "GIT_OBJECT_AT_SOURCE_COMMIT"
    ancestry = bool(observed_head and git_is_ancestor(root, PROMOTION_SOURCE_COMMIT, observed_head))
    checks["aa003_basis_ancestry_verified"] = ancestry if require_git_ancestry else True
    required_boolean_checks = (
        "candidate_identity_verified",
        "candidate_contract_identity_verified",
        "current_contract_semantics_verified",
        "shadow_oracle_identity_verified",
        "git_object_identity_verified",
        "shadow_oracle_subject_matches_candidate",
        "aa003_roadmap_verified",
        "mcp_registration_verified",
        "promotion_contract_verified",
        "aa003_basis_ancestry_verified",
    )
    checks["promotion_basis_verified"] = all(checks.get(name) is True for name in required_boolean_checks)
    return checks


def _block_due_to_basis(request: dict[str, Any], basis: dict[str, Any]) -> dict[str, Any]:
    objective = str(request.get("objective_ref") or "UNSPECIFIED")
    digest = sha256_bytes(json.dumps({"objective": objective, "basis": basis}, sort_keys=True, default=str).encode("utf-8"))
    decision = {
        "schema": DECISION_SCHEMA,
        "control_decision_id": "MCPD-AA4-BLOCK-" + digest[:12].upper(),
        "control_state_ref": "CTRL-AA4-PROMOTION-BASIS",
        "objective_ref": objective,
        "basis_refs": ["PATCH-AA-003", CONTROL_SURFACE_ID],
        "basis_fingerprint": digest,
        "effective_user_config_ref": str(request.get("effective_user_configuration") or "CURRENT_EFFECTIVE_CONFIGURATION"),
        "execution_profile_ref": "NONE",
        "applicable_control_refs": [CONTROL_SURFACE_ID],
        "outcome": "BLOCK",
        "invalidates": ["PROMOTION_BASIS_INVALID"],
        "verification_requirement": "REESTABLISH_EXACT_SHADOW_VALIDATED_BASIS",
        "human_boundary": "NONE",
        "evidence_scope": "PROMOTION_BASIS",
        "authority": "MCP",
        "resolved_at": utc_now(),
    }
    return {
        "schema": SCHEMA,
        "result": "PASS",
        "authority": "DERIVED_MCP_CONTROL_DECISION",
        "live_control_authority": True,
        "normal_control_path_exercised": True,
        "promotion_basis_verified": False,
        "promotion_basis": basis,
        "material_preflight_exercised": False,
        "material_preflight_precedes_adaptive": True,
        "adaptive_invoked": False,
        "mcp_control_decision": decision,
        "execution_profile": None,
        "continuation_effect": "NONE",
    }



def _block_due_to_preflight_error(
    request: dict[str, Any],
    basis: dict[str, Any],
    error: Exception,
) -> dict[str, Any]:
    objective = str(request.get("objective_ref") or request.get("current_objective") or "UNSPECIFIED")
    error_type = type(error).__name__
    digest = sha256_bytes(json.dumps(
        {"objective": objective, "error_type": error_type, "basis": basis},
        sort_keys=True,
        default=str,
    ).encode("utf-8"))
    decision = {
        "schema": DECISION_SCHEMA,
        "control_decision_id": "MCPD-AA4-PREFLIGHT-BLOCK-" + digest[:12].upper(),
        "control_state_ref": "CTRL-AA4-MATERIAL-PREFLIGHT",
        "objective_ref": objective,
        "basis_refs": ["CEREBRO-MATERIAL-COMMITMENT-PREFLIGHT-001", CONTROL_SURFACE_ID],
        "basis_fingerprint": digest,
        "effective_user_config_ref": str(request.get("effective_user_configuration") or "CURRENT_EFFECTIVE_CONFIGURATION"),
        "execution_profile_ref": "NONE",
        "applicable_control_refs": ["CEREBRO-MATERIAL-COMMITMENT-PREFLIGHT-001", CONTROL_SURFACE_ID],
        "outcome": "BLOCK",
        "invalidates": ["MATERIAL_PREFLIGHT_INPUT_OR_EXECUTION_ERROR"],
        "verification_requirement": "RE_RESOLVE_MATERIAL_PREFLIGHT_INPUT_OR_EXECUTION",
        "human_boundary": "NONE",
        "evidence_scope": "MATERIAL_PREFLIGHT_FAIL_CLOSED",
        "authority": "MCP",
        "resolved_at": utc_now(),
    }
    return {
        "schema": SCHEMA,
        "result": "PASS",
        "authority": "DERIVED_MCP_CONTROL_DECISION",
        "live_control_authority": True,
        "direct_resolver_live_authority": False,
        "normal_control_path_exercised": True,
        "promotion_basis_verified": True,
        "promotion_basis": basis,
        "material_preflight_exercised": True,
        "material_preflight_passed": False,
        "material_preflight_precedes_adaptive": True,
        "adaptive_invoked": False,
        "mcp_control_decision": decision,
        "execution_profile": None,
        "continuation_effect": "NONE",
        "preflight_result": {
            "result": "ERROR",
            "error_class": error_type,
            "authority": "DERIVED_CONTROL_EVIDENCE",
        },
        "preflight_error_type": error_type,
    }


def _block_due_to_delivery_profile(
    request: dict[str, Any],
    basis: dict[str, Any],
    delivery: dict[str, Any],
) -> dict[str, Any]:
    objective = str(request.get("objective_ref") or "UNSPECIFIED")
    digest = sha256_bytes(json.dumps(
        {"objective": objective, "delivery": delivery, "basis": basis},
        sort_keys=True,
        default=str,
    ).encode("utf-8"))
    decision = {
        "schema": DECISION_SCHEMA,
        "control_decision_id": "MCPD-DELIVERY-BLOCK-" + digest[:12].upper(),
        "control_state_ref": "CTRL-MCP-DELIVERY-PROFILE",
        "objective_ref": objective,
        "basis_refs": ["STD-CHANGE-DELIVERY", CONTROL_SURFACE_ID],
        "basis_fingerprint": digest,
        "effective_user_config_ref": str(request.get("effective_user_configuration") or "CURRENT_EFFECTIVE_CONFIGURATION"),
        "execution_profile_ref": "NONE",
        "applicable_control_refs": ["STD-CHANGE-DELIVERY", CONTROL_SURFACE_ID],
        "outcome": "BLOCK",
        "invalidates": [str(delivery.get("classification") or "DELIVERY_PROFILE_RESOLUTION_FAILED")],
        "verification_requirement": "RE_RESOLVE_DELIVERY_CAPABILITY_EVIDENCE",
        "human_boundary": "NONE",
        "evidence_scope": "MCP_DELIVERY_PROFILE_RESOLUTION",
        "authority": "MCP",
        "control_resolution_surface": CONTROL_SURFACE_ID,
        "resolved_at": utc_now(),
    }
    return {
        "schema": SCHEMA,
        "result": "PASS",
        "authority": "DERIVED_MCP_CONTROL_DECISION",
        "live_control_authority": True,
        "direct_resolver_live_authority": False,
        "normal_control_path_exercised": True,
        "promotion_basis_verified": True,
        "promotion_basis": basis,
        "material_preflight_exercised": False,
        "material_preflight_passed": None,
        "material_preflight_precedes_adaptive": True,
        "adaptive_invoked": False,
        "mcp_control_decision": decision,
        "execution_profile": None,
        "mcp_delivery_profile_resolution": delivery,
        "continuation_effect": "NONE",
    }



def resolve(request: dict[str, Any], root: Path = SOURCE_ROOT, require_git_ancestry: bool = True) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise ValueError("request-must-be-object")
    basis = verify_promotion_basis(root, require_git_ancestry=require_git_ancestry)
    if not basis.get("promotion_basis_verified"):
        return _block_due_to_basis(request, basis)

    adaptive = load_module(root / "mcp/adaptive_control_resolver.py", "cerebro_adaptive_control_validated_logic")
    stage = str(request.get("stage") or "UNDERSTAND_FRAME").upper()
    material = bool(request.get("material")) or stage in MATERIAL_STAGES
    preflight_result = None
    adaptive_request = dict(request)
    delivery_resolution = None

    if "requested_delivery_profile" in request:
        delivery_resolution = resolve_delivery_profile(request)
        if delivery_resolution.get("result") != "PASS":
            return _block_due_to_delivery_profile(request, basis, delivery_resolution)
        adaptive_request["delivery_mode"] = delivery_resolution["resolved_profile"]
        adaptive_request["governing_basis_refs"] = sorted(set(
            [str(x) for x in adaptive_request.get("governing_basis_refs", [])]
            + [
                "STD-CHANGE-DELIVERY",
                "MCP-DELIVERY-PROFILE:" + str(delivery_resolution["basis_fingerprint"]),
            ]
        ))

    if material:
        preflight = load_module(root / "mcp/material_commitment_preflight.py", "cerebro_material_commitment_preflight_live")
        try:
            preflight_result = preflight.resolve(request, root)
        except Exception as exc:
            return _block_due_to_preflight_error(request, basis, exc)
        preflight_decision = preflight_result.get("mcp_control_decision", {}) if isinstance(preflight_result, dict) else {}
        if preflight_result.get("result") != "PASS" or preflight_decision.get("outcome") != "CONTINUE":
            decision = dict(preflight_decision) if isinstance(preflight_decision, dict) else {}
            decision["authority"] = "MCP"
            decision["control_resolution_surface"] = CONTROL_SURFACE_ID
            return {
                "schema": SCHEMA,
                "result": "PASS",
                "authority": "DERIVED_MCP_CONTROL_DECISION",
                "live_control_authority": True,
                "normal_control_path_exercised": True,
                "promotion_basis_verified": True,
                "promotion_basis": basis,
                "material_preflight_exercised": True,
                "material_preflight_passed": False,
                "material_preflight_precedes_adaptive": True,
                "adaptive_invoked": False,
                "mcp_control_decision": decision,
                "execution_profile": None,
                "continuation_effect": "NONE",
                "preflight_result": preflight_result,
            }
        control_state = preflight_result.get("control_state", {}) if isinstance(preflight_result, dict) else {}
        receipt = preflight_result.get("receipt", {}) if isinstance(preflight_result, dict) else {}
        adaptive_request["governing_basis_refs"] = sorted(set(
            [str(x) for x in adaptive_request.get("governing_basis_refs", [])]
            + [str(x) for x in control_state.get("governing_basis_refs", [])]
            + [str(x) for x in control_state.get("applicable_knowledge_refs", [])]
            + [str(x) for x in control_state.get("applicable_wisdom_refs", [])]
            + [str(x) for x in control_state.get("applicable_history_refs", [])]
        ))
        adaptive_request["coverage_state"] = control_state.get("coverage_state", adaptive_request.get("coverage_state"))
        adaptive_request["conflict_state"] = control_state.get("conflict_state", adaptive_request.get("conflict_state"))
        adaptive_request["semantic_resolution_state"] = control_state.get("semantic_resolution_state", adaptive_request.get("semantic_resolution_state"))
        adaptive_request["authoritative_source_commit"] = receipt.get("source_identity", adaptive_request.get("authoritative_source_commit"))

    candidate = adaptive.resolve(adaptive_request)
    candidate_decision = candidate.get("mcp_control_decision", {})
    if not isinstance(candidate_decision, dict) or candidate_decision.get("outcome") not in CONTROL_OUTCOMES:
        raise RuntimeError("candidate-produced-noncanonical-control-decision")
    decision = dict(candidate_decision)
    decision["candidate_schema"] = decision.get("schema")
    decision["schema"] = DECISION_SCHEMA
    decision["authority"] = "MCP"
    decision["control_resolution_surface"] = CONTROL_SURFACE_ID
    profile = candidate.get("execution_profile")
    if isinstance(profile, dict):
        profile = dict(profile)
        profile["candidate_schema"] = profile.get("schema")
        profile["schema"] = PROFILE_SCHEMA
        profile["authority"] = "MCP"
        if delivery_resolution is not None:
            profile["mcp_delivery_profile_resolution"] = delivery_resolution
    return {
        "schema": SCHEMA,
        "result": "PASS",
        "authority": "DERIVED_MCP_CONTROL_DECISION",
        "live_control_authority": True,
        "direct_resolver_live_authority": False,
        "normal_control_path_exercised": True,
        "promotion_basis_verified": True,
        "promotion_basis": basis,
        "material_preflight_exercised": material,
        "material_preflight_passed": True if material else None,
        "material_preflight_precedes_adaptive": True,
        "adaptive_invoked": True,
        "mcp_control_decision": decision,
        "execution_profile": profile,
        "mcp_delivery_profile_resolution": delivery_resolution,
        "continuation_effect": candidate.get("continuation_effect"),
        "capability_resolution": candidate.get("capability_resolution", {}),
        "preflight_result": preflight_result,
    }


def runtime_control_policy_absent(root: Path) -> bool:
    tokens = ("mcp/control_resolution.py", "adaptive_control_resolver.py", CONTROL_SURFACE_ID)
    paths = [root / "tooling/runtime-host/component.yaml", root / "tooling/runtime-host/cerebro_runtime.ps1"]
    for path in paths:
        if not path.is_file():
            return False
        text = path.read_text(encoding="utf-8", errors="replace")
        if any(token in text for token in tokens):
            return False
    return True


def selftest(root: Path = SOURCE_ROOT, require_git_ancestry: bool = True) -> dict[str, Any]:
    tests: list[dict[str, Any]] = []
    def check(name: str, ok: bool, detail: str = "") -> None:
        tests.append({"name": name, "result": "PASS" if ok else "FAIL", "detail": detail})

    basis = verify_promotion_basis(root, require_git_ancestry=require_git_ancestry)
    check("promotion-basis-exact", bool(basis.get("promotion_basis_verified")), json.dumps(basis, sort_keys=True))
    check(
        "promotion-identity-uses-git-object-not-worktree-representation",
        basis.get("git_object_identity_verified") is True and basis.get("identity_authority") == "GIT_OBJECT_AT_SOURCE_COMMIT",
    )
    check(
        "promoted-contract-lifecycle-semantics-verified",
        basis.get("current_contract_semantics_verified") is True,
    )

    adaptive = load_module(root / "mcp/adaptive_control_resolver.py", "cerebro_adaptive_control_direct_canary")
    direct = adaptive.resolve({"objective_ref": "AA004-DIRECT-CANARY", "consequence": "LOW", "uncertainty": "LOW"})
    check("direct-resolver-remains-non-live", direct.get("live_control_authority") is False)

    simple = resolve({"objective_ref": "AA004-SIMPLE", "consequence": "LOW", "uncertainty": "LOW"}, root, require_git_ancestry=require_git_ancestry)
    check("canonical-nonmaterial-path-live", simple.get("live_control_authority") is True and simple.get("adaptive_invoked") is True and simple.get("mcp_control_decision", {}).get("outcome") == "CONTINUE")

    delivery_cases = [
        (
            "delivery-auto-without-evidence-fails-closed",
            {"requested_delivery_profile": "AUTO"},
            None,
            "BLOCK",
        ),
        (
            "delivery-auto-replace-resolves-limited",
            {"requested_delivery_profile": "AUTO", "delivery_operations": ["replace", "replace"]},
            "LIMITED",
            "CONTINUE",
        ),
        (
            "delivery-auto-create-resolves-standard",
            {"requested_delivery_profile": "AUTO", "delivery_operations": ["replace", "create"]},
            "STANDARD",
            "CONTINUE",
        ),
        (
            "delivery-auto-direct-resolves-full",
            {"requested_delivery_profile": "AUTO", "delivery_operations": ["create"], "direct_workspace_access_declared": True},
            "FULL",
            "CONTINUE",
        ),
        (
            "delivery-limited-rejects-create",
            {"requested_delivery_profile": "LIMITED", "delivery_operations": ["create"]},
            None,
            "BLOCK",
        ),
    ]
    delivery_results: dict[str, dict[str, Any]] = {}
    for name, fields, expected_profile, expected_outcome in delivery_cases:
        candidate = resolve(
            {"objective_ref": name.upper(), "authoritative_source_commit": git_head(root), **fields},
            root,
            require_git_ancestry=require_git_ancestry,
        )
        delivery_results[name] = candidate
        resolution = candidate.get("mcp_delivery_profile_resolution") or {}
        profile = candidate.get("execution_profile") or {}
        check(
            name,
            candidate.get("mcp_control_decision", {}).get("outcome") == expected_outcome
            and resolution.get("resolved_profile") == expected_profile
            and resolution.get("authority") == "MCP"
            and resolution.get("adapter_may_recompute") is False
            and (expected_profile is None or profile.get("delivery_mode") == expected_profile),
        )

    standard_delivery = delivery_results["delivery-auto-create-resolves-standard"]
    standard_resolution = standard_delivery.get("mcp_delivery_profile_resolution") or {}
    standard_profile = standard_delivery.get("execution_profile") or {}
    check(
        "delivery-profile-resolution-is-bound-into-execution-profile",
        standard_profile.get("mcp_delivery_profile_resolution", {}).get("basis_fingerprint")
        == standard_resolution.get("basis_fingerprint")
        and str(standard_delivery.get("mcp_control_decision", {}).get("basis_fingerprint") or "")
        == str(standard_profile.get("basis_fingerprint") or ""),
    )
    check(
        "delivery-profile-namespaces-remain-distinct",
        standard_profile.get("delivery_mode") == "STANDARD"
        and standard_resolution.get("controls", {}).get("artifact_format") == "PAYLOAD_PLUS_INSTALLER"
        and standard_profile.get("verification_depth") in {"LIGHT", "STANDARD", "DEEP"},
    )

    human = resolve({"objective_ref": "AA004-HUMAN", "human_decision_value": "HIGH"}, root, require_git_ancestry=require_git_ancestry)
    check("human-boundary-preserved", human.get("mcp_control_decision", {}).get("outcome") == "USER_DECISION_REQUIRED")

    reorient = resolve({
        "objective_ref": "AA004-REORIENT-SAME",
        "materially_different_path_required": True,
        "current_execution_mechanism": "same",
        "proposed_execution_mechanism": "same",
    }, root, require_git_ancestry=require_git_ancestry)
    check("bounded-reorientation-remediation-preserved", reorient.get("mcp_control_decision", {}).get("outcome") == "BLOCK")

    material_block = resolve({
        "objective_ref": "AA004-MATERIAL-BLOCK",
        "current_objective": "verify material preflight block precedence",
        "current_scope": "AA004 controlled promotion selftest",
        "resolved_objective": "verify material preflight block precedence",
        "resolved_scope": "AA004 controlled promotion selftest",
        "stage": "MATERIAL_AUTHORIZE",
        "material": True,
        "semantic_resolution_state": "UNRESOLVED",
        "commitment_target": "",
        "expected_prior_learning": False,
        "coverage_audit_complete": False,
    }, root, require_git_ancestry=require_git_ancestry)
    check(
        "material-preflight-precedes-adaptive",
        material_block.get("material_preflight_exercised") is True
        and material_block.get("adaptive_invoked") is False
        and material_block.get("preflight_error_type") is None
        and isinstance(material_block.get("preflight_result"), dict)
        and material_block.get("preflight_result", {}).get("result") == "BLOCKED",
    )
    check(
        "preflight-block-not-overridden",
        material_block.get("mcp_control_decision", {}).get("outcome") == "BLOCK"
        and material_block.get("preflight_error_type") is None,
    )

    incomplete_material = resolve({
        "objective_ref": "AA004-MATERIAL-INCOMPLETE",
        "stage": "MATERIAL_AUTHORIZE",
        "material": True,
        "semantic_resolution_state": "UNRESOLVED",
        "commitment_target": "",
        "expected_prior_learning": False,
        "coverage_audit_complete": False,
    }, root, require_git_ancestry=require_git_ancestry)
    check(
        "material-preflight-input-failure-fails-closed",
        incomplete_material.get("material_preflight_exercised") is True
        and incomplete_material.get("adaptive_invoked") is False
        and incomplete_material.get("mcp_control_decision", {}).get("outcome") == "BLOCK"
        and "MATERIAL_PREFLIGHT_INPUT_OR_EXECUTION_ERROR" in incomplete_material.get("mcp_control_decision", {}).get("invalidates", [])
        and incomplete_material.get("preflight_error_type") == "ValueError",
    )
    check("runtime-control-policy-absent", runtime_control_policy_absent(root))

    passed = all(item["result"] == "PASS" for item in tests)
    return {"schema": "cerebro-aa004-control-resolution-selftest/v1", "result": "PASS" if passed else "FAIL", "tests": tests}


def activation_probe(root: Path = SOURCE_ROOT, require_git_ancestry: bool = True) -> dict[str, Any]:
    basis = verify_promotion_basis(root, require_git_ancestry=require_git_ancestry)
    tests = selftest(root, require_git_ancestry=require_git_ancestry)
    test_map = {item["name"]: item["result"] == "PASS" for item in tests.get("tests", [])}
    registry_text = (root / "tooling/validator/contract-activation-bindings.json").read_text(encoding="utf-8")
    binding_registered = BINDING_ID in registry_text
    result_ok = tests.get("result") == "PASS" and binding_registered and basis.get("promotion_basis_verified") is True
    return {
        "schema": ACTIVATION_SCHEMA,
        "result": "PASS" if result_ok else "FAIL",
        "binding_id": BINDING_ID,
        "proves_bindings": [BINDING_ID],
        "authority": "DERIVED_OPERATIONAL_EVIDENCE",
        "promotion_basis_verified": basis.get("promotion_basis_verified") is True,
        "candidate_identity_verified": basis.get("candidate_identity_verified") is True,
        "candidate_contract_identity_verified": basis.get("candidate_contract_identity_verified") is True,
        "current_contract_semantics_verified": basis.get("current_contract_semantics_verified") is True,
        "shadow_oracle_identity_verified": basis.get("shadow_oracle_identity_verified") is True,
        "git_object_identity_verified": basis.get("git_object_identity_verified") is True,
        "identity_authority": basis.get("identity_authority"),
        "aa003_basis_ancestry_verified": basis.get("aa003_basis_ancestry_verified") is True,
        "aa003_roadmap_verified": basis.get("aa003_roadmap_verified") is True,
        "mcp_registration_verified": basis.get("mcp_registration_verified") is True,
        "promotion_contract_verified": basis.get("promotion_contract_verified") is True,
        "direct_resolver_non_live": test_map.get("direct-resolver-remains-non-live", False),
        "canonical_control_surface_live": test_map.get("canonical-nonmaterial-path-live", False),
        "delivery_profile_resolution_owned_by_mcp": all(
            test_map.get(name, False)
            for name in (
                "delivery-auto-without-evidence-fails-closed",
                "delivery-auto-replace-resolves-limited",
                "delivery-auto-create-resolves-standard",
                "delivery-auto-direct-resolves-full",
                "delivery-limited-rejects-create",
            )
        ),
        "delivery_profile_adapter_non_deciding": test_map.get("delivery-profile-resolution-is-bound-into-execution-profile", False),
        "delivery_resolution_fail_closed": test_map.get("delivery-auto-without-evidence-fails-closed", False) and test_map.get("delivery-limited-rejects-create", False),
        "delivery_profile_namespace_preserved": test_map.get("delivery-profile-namespaces-remain-distinct", False),
        "material_preflight_precedes_adaptive": test_map.get("material-preflight-precedes-adaptive", False),
        "preflight_block_not_overridden": test_map.get("preflight-block-not-overridden", False),
        "preflight_input_failure_fails_closed": test_map.get("material-preflight-input-failure-fails-closed", False),
        "non_material_path_exercised": test_map.get("canonical-nonmaterial-path-live", False),
        "human_boundary_preserved": test_map.get("human-boundary-preserved", False),
        "reorientation_guard_preserved": test_map.get("bounded-reorientation-remediation-preserved", False),
        "runtime_control_policy_absent": test_map.get("runtime-control-policy-absent", False),
        "normal_control_path_exercised": test_map.get("canonical-nonmaterial-path-live", False),
        "selftest_result": tests.get("result"),
        "selftest_count": len(tests.get("tests", [])),
        "source_state_fingerprint": source_state_fingerprint(root),
        "basis_files": EVIDENCE_BASIS_FILES,
        "observed_source_head": basis.get("observed_source_head"),
        "observed_at": utc_now(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Cerebro canonical MCP control resolution surface")
    parser.add_argument("command", nargs="?", choices=["resolve", "activation-probe", "selftest"], default="resolve")
    parser.add_argument("--request")
    parser.add_argument("--output")
    parser.add_argument("--source-root", default=str(SOURCE_ROOT))
    parser.add_argument("--allow-no-git-ancestry", action="store_true")
    args = parser.parse_args()
    root = Path(args.source_root).resolve()
    require_git = not args.allow_no_git_ancestry
    if args.command == "activation-probe":
        result = activation_probe(root, require_git_ancestry=require_git)
    elif args.command == "selftest":
        result = selftest(root, require_git_ancestry=require_git)
    else:
        if not args.request:
            parser.error("resolve requires --request")
        request = json.loads(Path(args.request).read_text(encoding="utf-8"))
        result = resolve(request, root, require_git_ancestry=require_git)
    text = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        out = Path(args.output); out.parent.mkdir(parents=True, exist_ok=True); out.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0 if result.get("result") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
