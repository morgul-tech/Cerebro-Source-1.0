#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
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
CONTROL_CONTEXT_BINDING_SCHEMA = "cerebro-control-context-event-binding/v1"
HNS_CANDIDATE_ACTIVATION_PRECONDITION = "ACTUAL_TRANSITION_RECEIPT_AND_COMMITTED_STATE_EXACTLY_MATCH_PREDICTION"
EVIDENCE_BASIS_FILES = [
    "mcp/control-resolution.yaml",
    "mcp/control_resolution.py",
    "mcp/integrity-control.yaml",
    "mcp/integrity_resolution.py",
    "mcp/integrity_control_adapter.py",
    "mcp/integrity_cli.py",
    "engines/interaction/integrity_intent.py",
    "engines/presentation/integrity_presentation.py",
    "tooling/validator/integrity_validation.py",
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
    "standards/control-context-state-service.yaml",
    "standards/control-context-hierarchy.yaml",
    "standards/human-navigation-surface.yaml",
    "standards/development/consolidate.yaml",
    "engines/context/control-context-state.schema.json",
    "engines/interaction/control-context-intent-assessment.schema.json",
    "engines/interaction/control_context_intent.py",
    "engines/interaction/context-consolidation-result.schema.json",
    "engines/interaction/context_consolidation.py",
    "mcp/context-navigation-options.schema.json",
    "mcp/context-navigation-options-candidate.schema.json",
    "mcp/control-resolution-attestation.schema.json",
    "mcp/control-owner-effect-plan.schema.json",
    "mcp/owner-effect-receipt.schema.json",
    "mcp/owner-state-persistence-verification.schema.json",
    "mcp/state-service-commit-receipt.schema.json",
    "mcp/owner-state-commit-receipt.schema.json",
    "mcp/control_owner_effect_receipt.py",
    "mcp/control_owner_routing.py",
    "mcp/project-manager-control-governor.yaml",
    "mcp/project-manager-control-governor-decision.schema.json",
    "mcp/project_manager_control_governor.py",
    "tooling/validator/project_manager_control_governor_validation.py",
    "mcp/control_resolution_host.py",
    "engines/project/project-basis-state.schema.json",
    "engines/project/project_owner_effect.py",
    "engines/quality/quality_owner_effect.py",
    "engines/convergence/convergence-owner-state.schema.json",
    "engines/convergence/convergence_owner_effect.py",
    "tooling/context/control_context_registry.py",
    "tooling/context/control_context_state_postgres.py",
    "tooling/context/control_context_state_postgres.sql",
    "tooling/context/control_context_postgres_migrations.json",
    "tooling/context/control_owner_effect.py",
    "tooling/context/control_context_owner_persistence.py",
    "tooling/owner_state/owner_state_persistence.py",
    "tooling/owner_state/component.yaml",
    "tooling/validator/control_context_postgres_validation.py",
    "tooling/validator/control_context_owner_persistence_validation.py",
    "tooling/validator/owner_state_persistence_validation.py",
    "tooling/validator/control_resolution_host_validation.py",
    "tooling/validator/human_navigation_surface_validation.py",
]

DELIVERY_PROFILES = {"LIMITED", "STANDARD", "FULL"}
DELIVERY_PROFILE_ALIASES = {
    "STANDARD_A": "LIMITED",
    "STANDARD_B": "STANDARD",
    "STANDARD_C": "FULL",
}
DELIVERY_OPERATIONS = {"replace", "create", "delete"}
CONSTITUTIONAL_STATES = {"CLEAR", "SUSPECTED", "VERIFIED_MATERIAL_BREACH", "VERIFIED_NONMATERIAL_BREACH"}

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


def resolve_phase_transition(request: dict[str, Any]) -> dict[str, Any]:
    """Consume a validated campaign closeout before dependent work starts."""
    receipt = request.get("campaign_closeout_receipt")
    reasons: list[str] = []
    if not isinstance(receipt, dict):
        reasons.append("CLOSEOUT_RECEIPT_REQUIRED")
        receipt = {}
    if receipt.get("schema") != "cerebro-change-campaign-closeout-receipt/v1":
        reasons.append("CLOSEOUT_RECEIPT_SCHEMA_MISMATCH")
    if receipt.get("result") != "PASS":
        reasons.append("CLOSEOUT_NOT_PASS")
    if receipt.get("phase_transition_allowed") is not True:
        reasons.append("PHASE_TRANSITION_NOT_ALLOWED")
    if receipt.get("closeout_state") not in {"READY", "READY_WITH_DECLARED_DEBT"}:
        reasons.append("CLOSEOUT_STATE_NOT_READY")
    if receipt.get("unknown_or_unclassified_debt_absent") is not True:
        reasons.append("UNKNOWN_OR_UNCLASSIFIED_DEBT")
    return {
        "schema": "cerebro-mcp-phase-transition-decision/v1",
        "authority": "MCP",
        "outcome": "CONTINUE" if not reasons else "BLOCK",
        "classification": "CAMPAIGN_CLOSEOUT_ACCEPTED" if not reasons else "CAMPAIGN_CLOSEOUT_BLOCKED",
        "campaign_id": receipt.get("campaign_id"),
        "next_phase": receipt.get("next_phase"),
        "closeout_contract_fingerprint": receipt.get("contract_fingerprint"),
        "reasons": reasons,
    }


def evaluate_constitutional_compliance(request: dict[str, Any]) -> dict[str, Any]:
    raw = request.get("constitutional_breach_candidates", [])
    candidates = raw if isinstance(raw, list) else []
    normalized: list[dict[str, Any]] = []
    verified_material: list[str] = []
    suspected: list[str] = []
    verified_nonmaterial: list[str] = []
    for index, item in enumerate(candidates):
        if not isinstance(item, dict):
            continue
        article = str(item.get("article_id") or "").strip()
        state = str(item.get("state") or "SUSPECTED").upper()
        material = item.get("material") is True
        evidence_ref = str(item.get("evidence_ref") or "").strip()
        candidate_id = str(item.get("candidate_id") or f"CBR-{index + 1}").strip()
        if article not in {f"C-{value:02d}" for value in range(1, 9)}:
            state = "SUSPECTED"
        if state not in {"SUSPECTED", "VERIFIED", "DISPROVED", "RESOLVED"}:
            state = "SUSPECTED"
        row = {
            "candidate_id": candidate_id,
            "article_id": article or "UNRESOLVED",
            "state": state,
            "material": material,
            "evidence_ref": evidence_ref or None,
        }
        normalized.append(row)
        if state == "VERIFIED" and material and evidence_ref:
            verified_material.append(candidate_id)
        elif state == "VERIFIED" and evidence_ref:
            verified_nonmaterial.append(candidate_id)
        elif state == "SUSPECTED":
            suspected.append(candidate_id)

    if verified_material:
        state = "VERIFIED_MATERIAL_BREACH"
        effect = "BLOCK_AFFECTED_ACTION"
    elif verified_nonmaterial:
        state = "VERIFIED_NONMATERIAL_BREACH"
        effect = "REMEDIATE_WITH_TRACEABILITY"
    elif suspected:
        state = "SUSPECTED"
        effect = "RECORD_AND_INVESTIGATE"
    else:
        state = "CLEAR"
        effect = "NONE"
    if state not in CONSTITUTIONAL_STATES:
        raise AssertionError("constitutional-state-noncanonical")
    return {
        "schema": "cerebro-constitutional-compliance-decision/v1",
        "authority": "MCP",
        "constitution_ref": "mcp/constitution.yaml",
        "state": state,
        "effect": effect,
        "blockers": verified_material,
        "candidates": normalized,
        "automatic_constitution_amendment": False,
    }


def build_delivery_control_binding(result: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    decision = result.get("mcp_control_decision") or {}
    profile = result.get("execution_profile") or {}
    delivery = result.get("mcp_delivery_profile_resolution") or {}
    subject = {
        "schema": "cerebro-sealed-delivery-control-binding/v1",
        "decision_owner": "MCP",
        "control_resolution_surface": CONTROL_SURFACE_ID,
        "control_decision_id": decision.get("control_decision_id"),
        "control_decision_basis_fingerprint": decision.get("basis_fingerprint"),
        "execution_profile_id": profile.get("execution_profile_id"),
        "execution_profile_basis_fingerprint": profile.get("basis_fingerprint"),
        "delivery_profile_resolution_fingerprint": delivery.get("basis_fingerprint"),
        "requested_profile": str(request.get("requested_delivery_profile") or "").upper(),
        "resolved_profile": delivery.get("resolved_profile"),
        "operations": [str(value).lower() for value in request.get("delivery_operations", [])],
        "direct_workspace_access_declared": bool(request.get("direct_workspace_access_declared")),
        "source_commit": str(request.get("authoritative_source_commit") or ""),
        "adapter_recomputed": False,
    }
    subject["binding_fingerprint"] = sha256_bytes(
        json.dumps(subject, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    return subject


def validate_delivery_control_binding(manifest: dict[str, Any], root: Path) -> dict[str, Any]:
    binding = manifest.get("delivery_control_binding")
    reasons: list[str] = []
    if not isinstance(binding, dict):
        binding = {}
        reasons.append("SEALED_DELIVERY_CONTROL_BINDING_REQUIRED")
    request = {
        "objective_ref": "SEALED-STANDARD-DELIVERY-BINDING",
        "requested_delivery_profile": binding.get("requested_profile"),
        "delivery_operations": binding.get("operations") or [],
        "direct_workspace_access_declared": binding.get("direct_workspace_access_declared") is True,
        "authoritative_source_commit": binding.get("source_commit"),
        "consequence": "LOW",
        "uncertainty": "LOW",
    }
    result = resolve(request, root)
    expected = build_delivery_control_binding(result, request)
    if binding != expected:
        reasons.append("SEALED_DELIVERY_CONTROL_BINDING_MISMATCH")
    if not str(binding.get("control_decision_id") or "").startswith("MCPD-"):
        reasons.append("DELIVERY_CONTROL_DECISION_ID_INVALID")
    if not str(binding.get("execution_profile_id") or "").startswith("EXECP-"):
        reasons.append("DELIVERY_EXECUTION_PROFILE_ID_INVALID")
    if binding.get("source_commit") != manifest.get("expected_base_commit"):
        reasons.append("DELIVERY_CONTROL_SOURCE_COMMIT_MISMATCH")
    if binding.get("resolved_profile") != manifest.get("delivery_profile"):
        reasons.append("DELIVERY_CONTROL_PROFILE_MISMATCH")
    declared_operations = sorted(str(item.get("operation") or "") for item in manifest.get("files", []))
    if sorted(str(value) for value in binding.get("operations", [])) != declared_operations:
        reasons.append("DELIVERY_CONTROL_OPERATIONS_MISMATCH")
    if result.get("mcp_control_decision", {}).get("outcome") != "CONTINUE":
        reasons.append("MCP_DELIVERY_CONTROL_NOT_CONTINUE")
    return {
        "schema": "cerebro-sealed-delivery-control-binding-validation/v1",
        "result": "PASS" if not reasons else "BLOCKED",
        "normal_call_path_exercised": True,
        "decision_owner": binding.get("decision_owner"),
        "binding_fingerprint": binding.get("binding_fingerprint"),
        "control_decision_id": binding.get("control_decision_id"),
        "execution_profile_id": binding.get("execution_profile_id"),
        "errors": reasons,
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


class ControlContextBindingError(ValueError):
    def __init__(self, family: str, detail: str):
        super().__init__(detail)
        self.family = family
        self.detail = detail


def validate_control_context_binding(request: dict[str, Any], root: Path) -> dict[str, Any] | None:
    """Validate a State Service begin-event envelope before project reasoning."""

    project_bound = request.get("project_bound") is True or isinstance(request.get("control_context_binding"), dict)
    if not project_bound:
        return None
    binding = request.get("control_context_binding")
    if not isinstance(binding, dict):
        raise ControlContextBindingError("CONTROL_CONTEXT_BINDING_MISSING", "project-bound-event-requires-control-context-binding")
    if binding.get("schema") != CONTROL_CONTEXT_BINDING_SCHEMA:
        raise ControlContextBindingError("CONTROL_CONTEXT_REGISTRY_INVALID", "control-context-binding-schema-mismatch")
    if binding.get("repository_permission_required") is not False:
        raise ControlContextBindingError("CONTROL_CONTEXT_REGISTRY_INVALID", "project-control-binding-must-not-require-repository-permission")
    project = binding.get("project")
    session = binding.get("session")
    if not isinstance(project, dict) or not isinstance(session, dict):
        raise ControlContextBindingError("CONTROL_CONTEXT_BINDING_MISSING", "project-and-session-state-required")
    try:
        domain = load_module(root / "tooling/context/control_context_registry.py", "cerebro_control_context_domain")
        domain.validate_session_state(session, project)
    except Exception as exc:
        detail = str(exc)
        family = "CONTROL_CONTEXT_BINDING_STALE" if "stale" in detail or "revision" in detail or "fingerprint" in detail else "CONTROL_CONTEXT_REGISTRY_INVALID"
        raise ControlContextBindingError(family, detail) from exc

    expected = {
        "expected_project_revision": project.get("revision"),
        "expected_project_fingerprint": project.get("fingerprint"),
        "expected_session_revision": session.get("session_revision"),
        "expected_session_fingerprint": session.get("fingerprint"),
    }
    for field, value in expected.items():
        if binding.get(field) != value:
            raise ControlContextBindingError("CONTROL_CONTEXT_BINDING_STALE", f"{field}-mismatch")
    request_project = request.get("project_ref")
    if request_project is not None and str(request_project) != str(project.get("project_ref")):
        raise ControlContextBindingError("CONTROL_CONTEXT_BINDING_STALE", "request-project-ref-mismatch")
    request_session = request.get("session_ref")
    if request_session is not None and str(request_session) != str(session.get("session_ref")):
        raise ControlContextBindingError("CONTROL_CONTEXT_BINDING_STALE", "request-session-ref-mismatch")
    source_commit = str(request.get("authoritative_source_commit") or "")
    if source_commit and str(project.get("source_revision")) != source_commit:
        raise ControlContextBindingError("CONTROL_CONTEXT_BINDING_STALE", "project-source-revision-mismatch")

    summary_subject = {
        "event_id": binding.get("event_id"),
        "project_ref": project.get("project_ref"),
        "project_revision": project.get("revision"),
        "project_fingerprint": project.get("fingerprint"),
        "session_ref": session.get("session_ref"),
        "session_revision": session.get("session_revision"),
        "session_fingerprint": session.get("fingerprint"),
        "active_context_ref": session.get("active_context_ref"),
    }
    return {
        "schema": "cerebro-mcp-control-context-binding-validation/v1",
        "result": "PASS",
        **summary_subject,
        "binding_fingerprint": sha256_bytes(
            json.dumps(summary_subject, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ),
        "repository_permission_required": False,
        "session_scoped_focus": True,
    }


def _block_due_to_context_binding(
    request: dict[str, Any],
    basis: dict[str, Any],
    error: ControlContextBindingError,
) -> dict[str, Any]:
    objective = str(request.get("objective_ref") or request.get("current_objective") or "UNSPECIFIED")
    digest = sha256_bytes(
        json.dumps(
            {"objective": objective, "family": error.family, "detail": error.detail},
            sort_keys=True,
        ).encode("utf-8")
    )
    decision = {
        "schema": DECISION_SCHEMA,
        "control_decision_id": "MCPD-CONTEXT-BLOCK-" + digest[:12].upper(),
        "control_state_ref": "CTRL-CONTEXT-BINDING",
        "objective_ref": objective,
        "basis_refs": ["CEREBRO-CONTROL-CONTEXT-HIERARCHY-001", CONTROL_SURFACE_ID],
        "basis_fingerprint": digest,
        "effective_user_config_ref": str(request.get("effective_user_configuration") or "CURRENT_EFFECTIVE_CONFIGURATION"),
        "execution_profile_ref": "NONE",
        "applicable_control_refs": ["CEREBRO-CONTROL-CONTEXT-HIERARCHY-001", CONTROL_SURFACE_ID],
        "outcome": "BLOCK",
        "invalidates": [error.family],
        "verification_requirement": "REHYDRATE_VALIDATE_AND_RERESOLVE_CONTROL_CONTEXT_BINDING",
        "human_boundary": "NONE",
        "evidence_scope": "CONTROL_CONTEXT_BINDING_GATE",
        "authority": "MCP",
        "control_resolution_surface": CONTROL_SURFACE_ID,
        "resolved_at": utc_now(),
    }
    result = {
        "schema": SCHEMA,
        "result": "PASS",
        "authority": "DERIVED_MCP_CONTROL_DECISION",
        "live_control_authority": True,
        "direct_resolver_live_authority": False,
        "normal_control_path_exercised": True,
        "promotion_basis_verified": True,
        "promotion_basis": basis,
        "control_context_binding_required": True,
        "control_context_binding_validated": False,
        "control_context_binding_error": {"family": error.family, "detail": error.detail},
        "material_preflight_exercised": False,
        "material_preflight_passed": None,
        "material_preflight_precedes_adaptive": True,
        "adaptive_invoked": False,
        "mcp_control_decision": decision,
        "execution_profile": None,
        "continuation_effect": "PRESERVE_OR_REHYDRATE",
    }
    return result


def _block_due_to_context_transition(
    result: dict[str, Any],
    request: dict[str, Any],
    detail: str,
) -> dict[str, Any]:
    objective = str(request.get("objective_ref") or request.get("current_objective") or "UNSPECIFIED")
    candidate_decision = result.get("mcp_control_decision") if isinstance(result.get("mcp_control_decision"), dict) else {}
    digest = sha256_bytes(
        json.dumps(
            {"objective": objective, "detail": detail, "candidate_decision": candidate_decision},
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    )
    blocked = dict(result)
    blocked["candidate_mcp_control_decision"] = candidate_decision
    blocked["mcp_control_decision"] = {
        "schema": DECISION_SCHEMA,
        "control_decision_id": "MCPD-CONTEXT-TRANSITION-BLOCK-" + digest[:12].upper(),
        "control_state_ref": "CTRL-CONTEXT-TRANSITION",
        "objective_ref": objective,
        "basis_refs": ["CEREBRO-CONTROL-CONTEXT-HIERARCHY-001", CONTROL_SURFACE_ID],
        "basis_fingerprint": digest,
        "effective_user_config_ref": str(request.get("effective_user_configuration") or "CURRENT_EFFECTIVE_CONFIGURATION"),
        "execution_profile_ref": "NONE",
        "applicable_control_refs": ["CEREBRO-CONTROL-CONTEXT-HIERARCHY-001", CONTROL_SURFACE_ID],
        "outcome": "BLOCK",
        "invalidates": ["CONTROL_CONTEXT_TRANSITION_INVALID"],
        "verification_requirement": "RE_RESOLVE_CONTEXT_TRANSITION_FROM_CURRENT_BINDING",
        "human_boundary": "NONE",
        "evidence_scope": "CONTROL_CONTEXT_TRANSITION_VALIDATION",
        "authority": "MCP",
        "control_resolution_surface": CONTROL_SURFACE_ID,
        "resolved_at": utc_now(),
    }
    blocked["execution_profile"] = None
    blocked["context_transition"] = None
    blocked["context_transition_error"] = detail
    blocked["continuation_effect"] = "PRESERVE"
    return blocked


def build_context_navigation_options(
    project: dict[str, Any],
    session: dict[str, Any],
    decision: dict[str, Any],
    proposal: Any,
    predicted_receipt: dict[str, Any],
    root: Path,
) -> dict[str, Any] | None:
    """Create a non-renderable precommit candidate bound to the predicted receipt."""

    if proposal is None:
        return None
    if not isinstance(proposal, dict):
        raise ValueError("context-navigation-candidate-object-required")
    if proposal.get("human_action_is_next") is not True or proposal.get("machine_action_pending") is True:
        return None
    if decision.get("outcome") != "CONTINUE" or decision.get("human_boundary", "NONE") != "NONE":
        return None
    binding = session.get("active_continuation_binding")
    if not isinstance(binding, dict) or binding.get("surface_kind") != "HNS":
        raise ValueError("HNS-navigation-requires-committed-HNS-continuation-binding")
    optional_proposals = proposal.get("optional", [])
    if not isinstance(optional_proposals, list) or not all(isinstance(item, dict) for item in optional_proposals):
        raise ValueError("context-navigation-optional-array-required")
    if len(optional_proposals) > 3:
        raise ValueError("context-navigation-optional-maximum-three")

    def action_id(operation: str, target_ref: str, alias: str) -> str:
        subject = {
            "decision_ref": decision.get("control_decision_id"),
            "operation": operation,
            "target_ref": target_ref,
            "alias": alias,
            "project_fingerprint": project.get("fingerprint"),
            "session_fingerprint": session.get("fingerprint"),
        }
        return "HNSA-" + sha256_bytes(
            json.dumps(subject, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )[:20].upper()

    primary = {
        "action_id": action_id(binding["operation"], binding["target_ref"], binding["alias"]),
        "surface_kind": "HNS",
        "binding_id": binding["binding_id"],
        "alias": binding["alias"],
        "operation": binding["operation"],
        "target_ref": binding["target_ref"],
        "approved_by_mcp": True,
    }
    optional: list[dict[str, Any]] = []
    for item in optional_proposals:
        alias = item.get("alias")
        operation = item.get("operation")
        target_ref = item.get("target_ref")
        if not all(isinstance(value, str) and bool(value.strip()) for value in (alias, operation, target_ref)):
            raise ValueError("context-navigation-optional-action-fields-required")
        optional.append(
            {
                "action_id": action_id(operation, target_ref, alias),
                "surface_kind": "HNS",
                "alias": alias,
                "operation": operation,
                "target_ref": target_ref,
                "approved_by_mcp": True,
            }
        )
    options = {
        "schema": "cerebro-mcp-context-navigation-options-candidate/v1",
        "authority": "MCP",
        "state_basis": "PREDICTED_POST_COMMIT_STATE",
        "render_authorized": False,
        "activation_precondition": HNS_CANDIDATE_ACTIVATION_PRECONDITION,
        "expected_transition_receipt_ref": predicted_receipt["receipt_id"],
        "expected_transition_receipt_fingerprint": predicted_receipt["receipt_fingerprint"],
        "control_decision_ref": decision.get("control_decision_id"),
        "project_ref": project["project_ref"],
        "session_ref": session["session_ref"],
        "source_context_ref": session["active_context_ref"],
        "project_revision": project["revision"],
        "session_revision": session["session_revision"],
        "project_fingerprint": project["fingerprint"],
        "session_fingerprint": session["fingerprint"],
        "primary": primary,
        "optional": optional,
        "candidate_fingerprint": "",
    }
    fingerprint_subject = dict(options)
    fingerprint_subject.pop("candidate_fingerprint", None)
    options["candidate_fingerprint"] = sha256_bytes(
        json.dumps(fingerprint_subject, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )
    validator = load_module(
        root / "tooling/validator/human_navigation_surface_validation.py",
        "cerebro_hns_options_validator",
    )
    validator.validate_navigation_options_candidate(options, project, session, predicted_receipt)
    return options


def _none_owner_effects() -> dict[str, dict[str, Any]]:
    return {
        owner: {"owner": owner, "effect": "NONE", "candidate_ref": None, "state_mutation_by_MCP": False}
        for owner in ("project", "quality", "convergence", "context", "human")
    }


def _default_next_action(decision: dict[str, Any], navigation_options: dict[str, Any] | None) -> dict[str, Any]:
    if navigation_options is not None:
        return {
            "action_ref": navigation_options["primary"]["action_id"],
            "action_class": "CONTROL",
            "owner": "HUMAN",
            "internally_executable": False,
            "required_before_event_closure": False,
            "basis_fingerprint": str(
                navigation_options.get("options_fingerprint") or navigation_options.get("candidate_fingerprint") or ""
            ),
        }
    if decision.get("outcome") == "USER_DECISION_REQUIRED":
        return {
            "action_ref": str(decision.get("human_boundary") or "HUMAN_DECISION_REQUIRED"),
            "action_class": "HUMAN_DECISION",
            "owner": "HUMAN",
            "internally_executable": False,
            "required_before_event_closure": False,
            "basis_fingerprint": str(decision.get("basis_fingerprint") or ""),
        }
    return {
        "action_ref": "NONE",
        "action_class": "CONTROL",
        "owner": "NONE",
        "internally_executable": False,
        "required_before_event_closure": False,
        "basis_fingerprint": str(decision.get("basis_fingerprint") or ""),
    }


def _validate_consolidation_against_bound_project(
    consolidation: dict[str, Any], project: dict[str, Any]
) -> None:
    selected = consolidation.get("selected_contexts")
    if not isinstance(selected, list) or not selected:
        raise ValueError("context-consolidation-selected-contexts-required")
    mapping = {item["context_id"]: item for item in project["contexts"]}
    for snapshot in selected:
        if not isinstance(snapshot, dict):
            raise ValueError("context-consolidation-selected-snapshot-object-required")
        if snapshot.get("project_ref") != project["project_ref"]:
            raise ValueError("cross-project-owner-routing-requires-separately-validated-project-bindings")
        if snapshot.get("project_revision") != project["revision"] or snapshot.get("project_fingerprint") != project["fingerprint"]:
            raise ValueError("context-consolidation-project-snapshot-stale")
        context_ref = snapshot.get("context_ref")
        if context_ref not in mapping or snapshot.get("context_fingerprint") != mapping[context_ref]["context_fingerprint"]:
            raise ValueError("context-consolidation-context-snapshot-stale")


def _bind_shared_pm_governance_candidate(
    request: dict[str, Any],
    candidate: Any,
) -> dict[str, Any]:
    """Bind provider readback and one PM-owned transition into the governor input.

    The governance candidate may describe intent, but it may not smuggle a second
    provider snapshot or control consumer.  START and PROCESS identities are
    supplied once by the canonical control request and consumed once by the PM
    governor decision.
    """

    if not isinstance(candidate, dict):
        raise ValueError("project-manager-governance-candidate-object-required")
    bound = copy.deepcopy(candidate)
    transaction = bound.get("shared_write_transaction")
    transition_binding = request.get("shared_control_transition")
    provider_readback = request.get("shared_provider_readback")
    if transaction is None:
        if transition_binding is not None or provider_readback is not None:
            raise ValueError("shared-provider-or-transition-evidence-requires-governed-transaction")
        return bound
    if not isinstance(transaction, dict):
        raise ValueError("shared-write-transaction-object-required")

    normalized = copy.deepcopy(transaction)
    transition_kind = str(normalized.get("transition_kind") or "GENERIC").upper()
    controlled = (
        transition_kind in {"START", "PROCESS"}
        or normalized.get("global_scalar_projection_intent") is True
        or normalized.get("h3_safe_publication") is True
    )
    if controlled:
        if not isinstance(provider_readback, dict):
            raise ValueError("shared-provider-readback-binding-required")
        supplied_provider = normalized.get("provider_state")
        if supplied_provider is not None and supplied_provider != provider_readback:
            raise ValueError("shared-candidate-provider-state-injection-prohibited")
        normalized["provider_state"] = copy.deepcopy(provider_readback)

    if transition_kind in {"START", "PROCESS"}:
        if not isinstance(transition_binding, dict):
            raise ValueError("shared-control-transition-binding-required")
        if str(transition_binding.get("transition_kind") or "").upper() != transition_kind:
            raise ValueError("shared-control-transition-kind-mismatch")
        if transition_binding.get("canonical_consumer") != "PROJECT_MANAGER":
            raise ValueError("shared-control-transition-must-use-canonical-PM-consumer")
        identity_fields = ["start_receipt_id", "target_generation"]
        if transition_kind == "START":
            identity_fields.append("packet_id")
        else:
            identity_fields.append("canonical_claim_id")
        for field in identity_fields:
            value = str(transition_binding.get(field) or "").strip()
            if not value:
                raise ValueError(f"shared-control-transition-{field}-required")
            supplied = normalized.get(field)
            if supplied is not None and str(supplied) != value:
                raise ValueError(f"shared-control-transition-{field}-mismatch")
            normalized[field] = value
        actor_generation = request.get("actor_generation")
        if actor_generation is not None and str(actor_generation) != normalized["target_generation"]:
            raise ValueError("shared-control-transition-actor-generation-mismatch")
        normalized["canonical_consumer"] = "PROJECT_MANAGER"
    elif transition_binding is not None:
        raise ValueError("shared-control-transition-without-START-or-PROCESS-prohibited")

    bound["shared_write_transaction"] = normalized
    return bound


def attach_context_transition(
    result: dict[str, Any],
    request: dict[str, Any],
    context_binding: dict[str, Any] | None,
    root: Path,
    owner_persistence_verifier: Any | None = None,
    runtime_capability_resolver: Any | None = None,
    pm_profile_verifier: Any | None = None,
) -> dict[str, Any]:
    """Bind an Interaction proposal to the final MCP decision without persisting it."""

    if context_binding is None:
        return result
    envelope = request.get("control_context_binding")
    if not isinstance(envelope, dict):
        return _block_due_to_context_transition(result, request, "validated-binding-envelope-missing")
    decision = result.get("mcp_control_decision")
    if not isinstance(decision, dict):
        return _block_due_to_context_transition(result, request, "mcp-control-decision-required")
    outcome = decision.get("outcome")
    proposal = request.get("context_transition_candidate")
    if proposal is None:
        proposal = {}
    if not isinstance(proposal, dict):
        return _block_due_to_context_transition(result, request, "context-transition-candidate-object-required")
    project_operations = proposal.get("project_operations", [])
    session_operations = proposal.get("session_operations", [])
    if not isinstance(project_operations, list) or not isinstance(session_operations, list):
        return _block_due_to_context_transition(result, request, "context-transition-operation-arrays-required")
    if outcome != "CONTINUE" and (project_operations or session_operations):
        return _block_due_to_context_transition(result, request, "state-mutation-prohibited-for-non-CONTINUE-outcome")
    if outcome != "CONTINUE":
        project_operations = []
        session_operations = []
    intent_validation: dict[str, Any] | None = None
    if outcome == "CONTINUE":
        assessment = request.get("control_context_intent_assessment")
        if not isinstance(assessment, dict):
            return _block_due_to_context_transition(result, request, "project-CONTINUE-requires-Interaction-intent-assessment")
        try:
            interaction = load_module(
                root / "engines/interaction/control_context_intent.py",
                "cerebro_control_context_intent_validation",
            )
            intent_validation = interaction.validate_control_context_intent_assessment(
                assessment, envelope["project"], envelope["session"], envelope.get("event_id")
            )
            operation_names = {
                item.get("operation") for item in [*project_operations, *session_operations] if isinstance(item, dict)
            }
            if operation_names:
                expected_route = "CREATE_CHILD" if "CREATE_CHILD" in operation_names else "CONTROL_TRANSITION"
                if assessment.get("route_candidate") != expected_route:
                    raise ValueError("context-transition-does-not-match-Interaction-route-candidate")
        except Exception as exc:
            return _block_due_to_context_transition(result, request, str(exc))
    directive = {
        "schema": "cerebro-control-context-transition-directive/v1",
        "event_id": envelope.get("event_id"),
        "decision_ref": decision.get("control_decision_id") or "MCPD-UNIDENTIFIED",
        "expected_project_revision": envelope.get("expected_project_revision"),
        "expected_project_fingerprint": envelope.get("expected_project_fingerprint"),
        "expected_session_revision": envelope.get("expected_session_revision"),
        "expected_session_fingerprint": envelope.get("expected_session_fingerprint"),
        "project_operations": project_operations,
        "session_operations": session_operations,
    }
    try:
        domain = load_module(root / "tooling/context/control_context_registry.py", "cerebro_control_context_transition_domain")
        project_after, session_after, receipt = domain.apply_transition(
            envelope["project"], envelope["session"], directive
        )
        owner_effect_plan = None
        consolidation = request.get("context_consolidation_result_candidate")
        if consolidation is not None:
            if not isinstance(consolidation, dict):
                raise ValueError("context-consolidation-result-candidate-object-required")
            _validate_consolidation_against_bound_project(consolidation, envelope["project"])
            router = load_module(root / "mcp/control_owner_routing.py", "cerebro_control_owner_effect_routing")
            owner_effect_plan = router.build_owner_effect_plan(
                decision,
                consolidation,
                request.get("owner_effect_state") if isinstance(request.get("owner_effect_state"), dict) else None,
                persistence_evidence_verifier=owner_persistence_verifier,
                capability_resolver=runtime_capability_resolver,
            )
        navigation_candidate = request.get("context_navigation_candidate")
        if (
            isinstance(owner_effect_plan, dict)
            and owner_effect_plan.get("next_action", {}).get("owner") == "MACHINE"
            and owner_effect_plan.get("next_action", {}).get("required_before_event_closure") is True
        ):
            navigation_candidate = {"human_action_is_next": False, "machine_action_pending": True, "optional": []}
        navigation_options = build_context_navigation_options(
            project_after,
            session_after,
            decision,
            navigation_candidate,
            receipt,
            root,
        )
    except Exception as exc:
        return _block_due_to_context_transition(result, request, str(exc))
    transition_subject = {
        "event_id": directive["event_id"],
        "decision_ref": directive["decision_ref"],
        "project_revision_after": project_after["revision"],
        "project_fingerprint_after": project_after["fingerprint"],
        "session_revision_after": session_after["session_revision"],
        "session_fingerprint_after": session_after["fingerprint"],
        "receipt_id": receipt["receipt_id"],
    }
    attached = dict(result)
    attached["context_transition"] = {
        "schema": "cerebro-mcp-control-context-transition/v1",
        "transition_id": "MCPT-" + sha256_bytes(
            json.dumps(transition_subject, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )[:20].upper(),
        "directive": directive,
        "predicted_receipt": receipt,
        "predicted_project_revision_after": project_after["revision"],
        "predicted_project_fingerprint_after": project_after["fingerprint"],
        "predicted_session_revision_after": session_after["session_revision"],
        "predicted_session_fingerprint_after": session_after["fingerprint"],
        "state_service_must_revalidate_and_compare_and_swap": True,
        "state_service_mutation_attestation_required": True,
        "navigation_options_activation_precondition": HNS_CANDIDATE_ACTIVATION_PRECONDITION,
        "repository_permission_required": False,
    }
    attached["mcp_context_navigation_options_candidate"] = navigation_options
    attached["mcp_context_navigation_options"] = None
    attached["human_navigation_surface_required"] = False
    attached["human_navigation_surface_render_authorized_before_commit"] = False
    attached["human_navigation_surface_required_after_committed_state_match"] = navigation_options is not None
    attached["control_context_intent_assessment_validation"] = intent_validation
    attached["owner_effect_plan"] = owner_effect_plan
    attached_decision = dict(decision)
    attached_decision["context_transition_ref"] = attached["context_transition"]["transition_id"]
    attached_decision["context_directive"] = copy.deepcopy(directive)
    attached_decision["owner_effects"] = (
        copy.deepcopy(owner_effect_plan["owner_effects"]) if isinstance(owner_effect_plan, dict) else _none_owner_effects()
    )
    attached_decision["next_action"] = (
        copy.deepcopy(owner_effect_plan["next_action"])
        if isinstance(owner_effect_plan, dict)
        else _default_next_action(attached_decision, navigation_options)
    )
    governance_candidate = request.get("project_manager_governance_candidate")
    if governance_candidate is None and (
        request.get("shared_control_transition") is not None
        or request.get("shared_provider_readback") is not None
    ):
        return _block_due_to_context_transition(
            result,
            request,
            "shared-provider-or-transition-evidence-requires-project-manager-governance",
        )
    if governance_candidate is not None:
        try:
            governance_candidate = _bind_shared_pm_governance_candidate(request, governance_candidate)
            governor = load_module(
                root / "mcp/project_manager_control_governor.py",
                "cerebro_project_manager_control_governor_live",
            )
            governance = governor.govern_project_manager_event(
                candidate=governance_candidate,
                canonical_next_action=attached_decision["next_action"],
                session=envelope["session"],
                profile_binding=request.get("project_manager_profile_binding"),
                profile_verifier=pm_profile_verifier,
            )
        except Exception as exc:
            return _block_due_to_context_transition(result, request, "project-manager-control-governor:" + str(exc))
        attached_decision["next_action"] = copy.deepcopy(governance["next_action"])
        attached_decision["shared_control_disposition"] = copy.deepcopy(
            governance.get("shared_write_gate", {}).get("transition_disposition")
        )
        attached["project_manager_control_governance"] = governance
        attached["shared_control_disposition"] = attached_decision["shared_control_disposition"]
        if attached_decision["next_action"].get("owner") == "MACHINE":
            attached["mcp_context_navigation_options_candidate"] = None
            attached["mcp_context_navigation_options"] = None
            attached["human_navigation_surface_required"] = False
            attached["human_navigation_surface_render_authorized_before_commit"] = False
            attached["human_navigation_surface_required_after_committed_state_match"] = False
    else:
        attached["project_manager_control_governance"] = None
        attached["shared_control_disposition"] = None
    attached["mcp_control_decision"] = attached_decision
    next_action = attached_decision["next_action"]
    attached["current_event_machine_action_required"] = (
        next_action.get("owner") == "MACHINE" and next_action.get("required_before_event_closure") is True
    )
    attached["event_closure_allowed_before_required_machine_action"] = not attached["current_event_machine_action_required"]
    return attached


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




def apply_integrity_subresolution(
    request: dict[str, Any],
    candidate: dict[str, Any],
    context_binding: dict[str, Any] | None,
    promotion_basis: dict[str, Any],
    preflight_result: dict[str, Any] | None,
    delivery_resolution: dict[str, Any] | None,
    phase_transition: dict[str, Any] | None,
    root: Path,
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any]]:
    """Run the non-authoritative Integrity subresolution before final MCP decision projection."""
    integrity = load_module(root / "mcp/integrity_resolution.py", "cerebro_integrity_subresolution")
    intent = request.get("integrity_intent") if isinstance(request.get("integrity_intent"), dict) else None
    invocation = integrity.resolve_invocation(request, intent)
    if invocation.get("required") is not True:
        return candidate, None, invocation
    adapter = load_module(root / "mcp/integrity_control_adapter.py", "cerebro_integrity_control_adapter")
    payload = adapter.build_integrity_request(
        request,
        candidate,
        context_binding,
        promotion_basis,
        preflight_result,
        delivery_resolution,
        phase_transition,
        invocation,
        root,
    )
    assessment = integrity.resolve(payload)
    decision = candidate.get("mcp_control_decision") if isinstance(candidate.get("mcp_control_decision"), dict) else {}
    current_outcome = str(decision.get("outcome") or "")
    final_outcome, integrity_reasons = adapter.apply_recommendation(current_outcome, assessment)
    updated_decision = dict(decision)
    updated_decision["applicable_control_refs"] = sorted(set(
        [str(x) for x in updated_decision.get("applicable_control_refs", [])]
        + ["CEREBRO-MCP-INTEGRITY-CONTROL-001"]
    ))
    updated_decision["integrity_assessment_ref"] = assessment.get("assessment_id")
    updated_decision["integrity_basis_fingerprint"] = assessment.get("basis_fingerprint")
    updated_decision["integrity_result"] = assessment.get("result")
    updated_decision["integrity_coverage_mode"] = assessment.get("coverage_mode")
    if final_outcome != current_outcome:
        identity = {
            "candidate_control_decision_id": decision.get("control_decision_id"),
            "candidate_basis_fingerprint": decision.get("basis_fingerprint"),
            "integrity_assessment_id": assessment.get("assessment_id"),
            "integrity_basis_fingerprint": assessment.get("basis_fingerprint"),
            "outcome": final_outcome,
        }
        digest = sha256_bytes(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        updated_decision["candidate_control_decision_id"] = decision.get("control_decision_id")
        updated_decision["control_decision_id"] = "MCPD-INTG-" + digest[:16].upper()
        updated_decision["outcome"] = final_outcome
        updated_decision["invalidates"] = sorted(set(
            [str(x) for x in updated_decision.get("invalidates", [])] + integrity_reasons
        ))
        updated_decision["verification_requirement"] = "INTEGRITY_REMEDIATION_AND_RERESOLUTION"
    updated_candidate = dict(candidate)
    updated_candidate["mcp_control_decision"] = updated_decision
    return updated_candidate, assessment, invocation

def resolve(
    request: dict[str, Any],
    root: Path = SOURCE_ROOT,
    require_git_ancestry: bool = True,
    owner_persistence_verifier: Any | None = None,
    runtime_capability_resolver: Any | None = None,
    pm_profile_verifier: Any | None = None,
) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise ValueError("request-must-be-object")
    basis = verify_promotion_basis(root, require_git_ancestry=require_git_ancestry)
    if not basis.get("promotion_basis_verified"):
        return _block_due_to_basis(request, basis)

    try:
        context_binding = validate_control_context_binding(request, root)
    except ControlContextBindingError as exc:
        return _block_due_to_context_binding(request, basis, exc)

    def finalize(bound_result: dict[str, Any]) -> dict[str, Any]:
        """Close every valid project-bound event through one transition directive.

        A BLOCK or human-boundary decision still needs a no-op completion receipt so
        that an event lease cannot make the previously committed continuation vanish.
        """

        return attach_context_transition(
            bound_result,
            request,
            context_binding,
            root,
            owner_persistence_verifier=owner_persistence_verifier,
            runtime_capability_resolver=runtime_capability_resolver,
            pm_profile_verifier=pm_profile_verifier,
        )

    constitutional = evaluate_constitutional_compliance(request)
    if constitutional["state"] == "VERIFIED_MATERIAL_BREACH":
        objective = str(request.get("objective_ref") or "UNSPECIFIED")
        digest = sha256_bytes(json.dumps({"objective": objective, "constitutional": constitutional}, sort_keys=True).encode("utf-8"))
        return finalize({
            "schema": SCHEMA,
            "result": "PASS",
            "authority": "DERIVED_MCP_CONTROL_DECISION",
            "live_control_authority": True,
            "normal_control_path_exercised": True,
            "promotion_basis_verified": True,
            "promotion_basis": basis,
            "constitutional_compliance": constitutional,
            "material_preflight_exercised": False,
            "material_preflight_passed": None,
            "material_preflight_precedes_adaptive": True,
            "adaptive_invoked": False,
            "mcp_control_decision": {
                "schema": DECISION_SCHEMA,
                "control_decision_id": "MCPD-CONSTITUTION-BLOCK-" + digest[:12].upper(),
                "control_state_ref": "CTRL-CONSTITUTIONAL-COMPLIANCE",
                "objective_ref": objective,
                "basis_refs": ["CEREBRO-CONSTITUTION-001"],
                "basis_fingerprint": digest,
                "effective_user_config_ref": "CURRENT_EFFECTIVE_CONFIGURATION",
                "execution_profile_ref": "NONE",
                "applicable_control_refs": ["CEREBRO-CONSTITUTION-001"],
                "outcome": "BLOCK",
                "invalidates": constitutional["blockers"],
                "verification_requirement": "RESOLVE_VERIFIED_MATERIAL_CONSTITUTIONAL_BREACH",
                "human_boundary": "NONE",
                "evidence_scope": "VERIFIED_MATERIAL_CONSTITUTIONAL_BREACH",
                "authority": "MCP",
                "control_resolution_surface": CONTROL_SURFACE_ID,
                "resolved_at": utc_now(),
            },
            "execution_profile": None,
            "continuation_effect": "NONE",
        })

    adaptive = load_module(root / "mcp/adaptive_control_resolver.py", "cerebro_adaptive_control_validated_logic")
    stage = str(request.get("stage") or "UNDERSTAND_FRAME").upper()
    material = bool(request.get("material")) or stage in MATERIAL_STAGES
    preflight_result = None
    adaptive_request = dict(request)
    if context_binding is not None:
        adaptive_request["governing_basis_refs"] = sorted(set(
            [str(x) for x in adaptive_request.get("governing_basis_refs", [])]
            + [
                "CONTROL-CONTEXT:" + str(context_binding["active_context_ref"]),
                "CONTROL-CONTEXT-BINDING:" + str(context_binding["binding_fingerprint"]),
            ]
        ))
    delivery_resolution = None
    phase_transition = None

    if bool(request.get("phase_transition_requested")):
        phase_transition = resolve_phase_transition(request)
        if phase_transition.get("outcome") != "CONTINUE":
            return finalize({
                "schema": SCHEMA,
                "result": "PASS",
                "authority": "DERIVED_MCP_CONTROL_DECISION",
                "live_control_authority": True,
                "normal_control_path_exercised": True,
                "promotion_basis_verified": True,
                "promotion_basis": basis,
                "material_preflight_exercised": False,
                "material_preflight_passed": None,
                "material_preflight_precedes_adaptive": True,
                "adaptive_invoked": False,
                "mcp_control_decision": {
                    "schema": DECISION_SCHEMA,
                    "authority": "MCP",
                    "control_resolution_surface": CONTROL_SURFACE_ID,
                    "outcome": "BLOCK",
                    "classification": "CAMPAIGN_CLOSEOUT_BLOCKED",
                    "invalidates": ["DEPENDENT_PHASE_TRANSITION"],
                },
                "execution_profile": None,
                "campaign_phase_transition": phase_transition,
                "continuation_effect": "BLOCK_DEPENDENT_PHASE",
            })
        adaptive_request["governing_basis_refs"] = sorted(set(
            [str(x) for x in adaptive_request.get("governing_basis_refs", [])]
            + ["CAMPAIGN-CLOSEOUT:" + str(phase_transition.get("closeout_contract_fingerprint") or "")]
        ))

    if "requested_delivery_profile" in request:
        delivery_resolution = resolve_delivery_profile(request)
        if delivery_resolution.get("result") != "PASS":
            return finalize(_block_due_to_delivery_profile(request, basis, delivery_resolution))
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
            return finalize(_block_due_to_preflight_error(request, basis, exc))
        preflight_decision = preflight_result.get("mcp_control_decision", {}) if isinstance(preflight_result, dict) else {}
        if preflight_result.get("result") != "PASS" or preflight_decision.get("outcome") != "CONTINUE":
            decision = dict(preflight_decision) if isinstance(preflight_decision, dict) else {}
            decision["authority"] = "MCP"
            decision["control_resolution_surface"] = CONTROL_SURFACE_ID
            return finalize({
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
            })
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
    candidate, integrity_assessment, integrity_invocation = apply_integrity_subresolution(
        request, candidate, context_binding, basis, preflight_result, delivery_resolution, phase_transition, root
    )
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
    result = {
        "schema": SCHEMA,
        "result": "PASS",
        "authority": "DERIVED_MCP_CONTROL_DECISION",
        "live_control_authority": True,
        "direct_resolver_live_authority": False,
        "normal_control_path_exercised": True,
        "promotion_basis_verified": True,
        "promotion_basis": basis,
        "constitutional_compliance": constitutional,
        "material_preflight_exercised": material,
        "material_preflight_passed": True if material else None,
        "material_preflight_precedes_adaptive": True,
        "adaptive_invoked": True,
        "mcp_control_decision": decision,
        "execution_profile": profile,
        "mcp_delivery_profile_resolution": delivery_resolution,
        "campaign_phase_transition": phase_transition,
        "control_context_binding_required": context_binding is not None,
        "control_context_binding_validated": context_binding is not None,
        "control_context_binding": context_binding,
        "continuation_effect": candidate.get("continuation_effect"),
        "capability_resolution": candidate.get("capability_resolution", {}),
        "integrity_invocation": integrity_invocation,
        "integrity_assessment": integrity_assessment,
        "preflight_result": preflight_result,
    }
    return finalize(result)


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


def _fixture_control_context_binding(root: Path) -> dict[str, Any]:
    domain = load_module(root / "tooling/context/control_context_registry.py", "cerebro_control_context_fixture_domain")
    source_revision = git_head(root) or "fixture-source"
    project, _ = domain.bootstrap_project_state(
        aggregate_id="AGG-MCP-CONTEXT-SELFTEST",
        tenant_ref="TENANT-SELFTEST",
        workspace_ref="WORKSPACE-SELFTEST",
        project_ref="PROJECT-SELFTEST",
        source_revision=source_revision,
        event_id="EVENT-BOOTSTRAP",
        decision_ref="DECISION-BOOTSTRAP",
        root={
            "context_id": "CTX-ROOT",
            "human_label": "Hovedspor",
            "objective_ref": "OBJECTIVE-ROOT",
            "scope_ref": "SCOPE-ROOT",
            "basis_refs": ["BASIS-ROOT"],
            "project_basis_ref": "PROJECT-BASIS-SELFTEST",
            "quality_trace_ref": "QUALITY-TRACE-SELFTEST",
            "completion_criteria_refs": ["ROOT-COMPLETE"],
        },
    )
    session = domain.bind_control_session(
        project,
        session_binding_id="SESSION-BINDING-SELFTEST",
        principal_ref="PRINCIPAL-SELFTEST",
        consumer_ref="CHATGPT",
        session_ref="SESSION-SELFTEST",
    )
    return {
        "schema": CONTROL_CONTEXT_BINDING_SCHEMA,
        "event_id": "EVENT-PROJECT-BOUND",
        "idempotency_key": "IDEMPOTENCY-PROJECT-BOUND",
        "project": project,
        "session": session,
        "expected_project_revision": project["revision"],
        "expected_project_fingerprint": project["fingerprint"],
        "expected_session_revision": session["session_revision"],
        "expected_session_fingerprint": session["fingerprint"],
        "repository_permission_required": False,
        "rehydration_receipt": None,
    }


def _fixture_intent_assessment(
    root: Path,
    binding: dict[str, Any],
    *,
    intent_candidate: str = "CONTINUE_CURRENT",
    materiality_hint: str = "NONMATERIAL",
    explicitness: str = "INFERRED",
    fork_justifications: list[str] | None = None,
) -> dict[str, Any]:
    interaction = load_module(
        root / "engines/interaction/control_context_intent.py",
        "cerebro_control_context_intent_selftest_fixture",
    )
    return interaction.assess_control_context_intent(
        {
            "event_ref": binding["event_id"],
            "project_relation": "ACTIVE_PROJECT",
            "intent_candidate": intent_candidate,
            "target_selectors": [],
            "materiality_hint": materiality_hint,
            "explicitness": explicitness,
            "fork_justifications": fork_justifications or [],
            "human_meaning": {"speech_to_text_observed": False, "material_objective_delta": False},
        },
        binding["project"],
        binding["session"],
    )


def selftest(root: Path = SOURCE_ROOT, require_git_ancestry: bool = True) -> dict[str, Any]:
    tests: list[dict[str, Any]] = []
    def check(name: str, ok: bool, detail: str = "") -> None:
        tests.append({"name": name, "result": "PASS" if ok else "FAIL", "detail": detail})

    check(
        "fixed-MCP-control-outcome-vocabulary-unchanged",
        CONTROL_OUTCOMES == {"CONTINUE", "REMEDIATE", "RETRY", "REORIENT", "USER_DECISION_REQUIRED", "BLOCK"},
    )

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
    check("constitutional-normal-consumer-clear", simple.get("constitutional_compliance", {}).get("state") == "CLEAR")

    missing_context = resolve(
        {"objective_ref": "PROJECT-MISSING-CONTEXT", "project_bound": True},
        root,
        require_git_ancestry=require_git_ancestry,
    )
    check(
        "project-bound-event-missing-context-binding-blocks-before-adaptive",
        missing_context.get("mcp_control_decision", {}).get("outcome") == "BLOCK"
        and "CONTROL_CONTEXT_BINDING_MISSING" in missing_context.get("mcp_control_decision", {}).get("invalidates", [])
        and missing_context.get("adaptive_invoked") is False,
    )

    context_binding = _fixture_control_context_binding(root)
    continue_intent = _fixture_intent_assessment(root, context_binding)
    valid_context = resolve(
        {
            "objective_ref": "PROJECT-VALID-CONTEXT",
            "project_bound": True,
            "project_ref": "PROJECT-SELFTEST",
            "session_ref": "SESSION-SELFTEST",
            "authoritative_source_commit": git_head(root) or "fixture-source",
            "control_context_binding": context_binding,
            "control_context_intent_assessment": continue_intent,
            "consequence": "LOW",
            "uncertainty": "LOW",
        },
        root,
        require_git_ancestry=require_git_ancestry,
    )
    check(
        "valid-project-context-binding-precedes-adaptive",
        valid_context.get("control_context_binding_validated") is True
        and valid_context.get("control_context_binding", {}).get("session_scoped_focus") is True
        and valid_context.get("adaptive_invoked") is True
        and valid_context.get("mcp_control_decision", {}).get("outcome") == "CONTINUE"
        and valid_context.get("context_transition", {}).get("predicted_receipt", {}).get("mutated") is False
        and valid_context.get("control_context_intent_assessment_validation", {}).get("result") == "PASS"
        and valid_context.get("mcp_control_decision", {}).get("context_directive", {}).get("event_id") == context_binding["event_id"],
    )
    missing_intent = resolve(
        {
            "objective_ref": "PROJECT-MISSING-INTERACTION-ASSESSMENT",
            "project_bound": True,
            "control_context_binding": context_binding,
            "consequence": "LOW",
            "uncertainty": "LOW",
        },
        root,
        require_git_ancestry=require_git_ancestry,
    )
    check(
        "project-CONTINUE-requires-state-bound-Interaction-assessment",
        missing_intent.get("mcp_control_decision", {}).get("outcome") == "BLOCK"
        and "CONTROL_CONTEXT_TRANSITION_INVALID" in missing_intent.get("mcp_control_decision", {}).get("invalidates", []),
    )
    transition_context = resolve(
        {
            "objective_ref": "PROJECT-CONTEXT-TRANSITION",
            "project_bound": True,
            "control_context_binding": context_binding,
            "control_context_intent_assessment": _fixture_intent_assessment(
                root,
                context_binding,
                intent_candidate="FORK_CANDIDATE",
                materiality_hint="MATERIAL",
                explicitness="EXPLICIT",
                fork_justifications=["MULTISTEP_CONTINUITY_USEFUL"],
            ),
            "context_transition_candidate": {
                "project_operations": [
                    {
                        "operation": "CREATE_CHILD",
                        "parent_context_ref": "CTX-ROOT",
                        "context_id": "CTX-CHILD",
                        "human_label": "State service",
                        "objective_ref": "OBJECTIVE-CHILD",
                        "scope_ref": "SCOPE-CHILD",
                        "basis_refs": ["BASIS-CHILD"],
                        "project_basis_ref": "PROJECT-BASIS-SELFTEST",
                        "quality_trace_ref": "QUALITY-TRACE-SELFTEST",
                        "completion_criteria_refs": ["CHILD-COMPLETE"],
                    }
                ],
                "session_operations": [
                    {"operation": "SET_ACTIVE", "context_ref": "CTX-CHILD"},
                    {
                        "operation": "SET_CONTINUATION_BINDING",
                        "binding": {
                            "binding_id": "BIND-CTX-CHILD",
                            "surface_kind": "HNS",
                            "alias": "Fortsett denne grenen",
                            "operation": "CONTINUE_CURRENT",
                            "target_ref": "CTX-CHILD",
                            "context_ref": "CTX-CHILD",
                        },
                    },
                ],
            },
            "context_navigation_candidate": {
                "human_action_is_next": True,
                "machine_action_pending": False,
                "optional": [
                    {
                        "alias": "Tilbake til hovedsporet",
                        "operation": "RETURN_ROOT",
                        "target_ref": "CTX-ROOT",
                    }
                ],
            },
            "consequence": "LOW",
            "uncertainty": "LOW",
        },
        root,
        require_git_ancestry=require_git_ancestry,
    )
    check(
        "mcp-binds-and-validates-context-transition-without-repository-permission",
        transition_context.get("mcp_control_decision", {}).get("outcome") == "CONTINUE"
        and transition_context.get("context_transition", {}).get("predicted_receipt", {}).get("mutated") is True
        and transition_context.get("context_transition", {}).get("repository_permission_required") is False
        and transition_context.get("context_transition", {}).get("state_service_mutation_attestation_required") is True
        and transition_context.get("mcp_context_navigation_options_candidate", {}).get("authority") == "MCP"
        and len(transition_context.get("mcp_context_navigation_options_candidate", {}).get("optional", [])) == 1
        and transition_context.get("mcp_context_navigation_options_candidate", {}).get("activation_precondition") == HNS_CANDIDATE_ACTIVATION_PRECONDITION
        and transition_context.get("mcp_context_navigation_options_candidate", {}).get("render_authorized") is False
        and transition_context.get("mcp_context_navigation_options") is None
        and transition_context.get("human_navigation_surface_required") is False
        and transition_context.get("human_navigation_surface_render_authorized_before_commit") is False
        and transition_context.get("human_navigation_surface_required_after_committed_state_match") is True
        and transition_context.get("mcp_control_decision", {}).get("owner_effects", {}).get("project", {}).get("effect") == "NONE"
        and transition_context.get("mcp_control_decision", {}).get("next_action", {}).get("owner") == "HUMAN",
    )
    consolidation_module = load_module(
        root / "engines/interaction/context_consolidation.py",
        "cerebro_context_consolidation_mcp_fixture",
    )
    consolidation_result = consolidation_module.build_context_consolidation_result(
        {
            "schema": consolidation_module.REQUEST_SCHEMA,
            "event_ref": context_binding["event_id"],
            "target_kind": "CONTROL_CONTEXTS",
            "selectors": [{"kind": "CONTEXT_REF", "project_ref": "PROJECT-SELFTEST", "value": "CTX-ROOT"}],
            "structural_join_requested": False,
        },
        [context_binding["project"]],
        {
            "synthesis_ref": "SYNTH-MCP-OWNER-ROUTING",
            "evidence_refs": ["EVIDENCE-MCP-OWNER-ROUTING"],
            "material_conflicts": [],
            "branch_disposition_candidates": [],
            "effect_candidates": ["PROJECT_REVISION_REQUIRED"],
        },
    )
    owner_routed = resolve(
        {
            "objective_ref": "PROJECT-OWNER-ROUTING",
            "project_bound": True,
            "control_context_binding": context_binding,
            "control_context_intent_assessment": continue_intent,
            "context_consolidation_result_candidate": consolidation_result,
            "context_navigation_candidate": {
                "human_action_is_next": True,
                "machine_action_pending": False,
                "optional": [],
            },
            "consequence": "LOW",
            "uncertainty": "LOW",
        },
        root,
        require_git_ancestry=require_git_ancestry,
    )
    check(
        "canonical-MCP-decision-contains-owner-effects-and-machine-next-action",
        owner_routed.get("mcp_control_decision", {}).get("outcome") == "CONTINUE"
        and owner_routed.get("mcp_control_decision", {}).get("owner_effects", {}).get("project", {}).get("effect") == "REVISION_REQUIRED"
        and owner_routed.get("mcp_control_decision", {}).get("next_action", {}).get("owner") == "MACHINE"
        and owner_routed.get("current_event_machine_action_required") is True
        and owner_routed.get("event_closure_allowed_before_required_machine_action") is False
        and owner_routed.get("human_navigation_surface_required") is False,
    )
    bound_human_boundary = resolve(
        {
            "objective_ref": "PROJECT-HCS-PRECEDENCE",
            "project_bound": True,
            "control_context_binding": context_binding,
            "authorization_required": True,
            "context_navigation_candidate": {
                "human_action_is_next": True,
                "machine_action_pending": False,
                "optional": [],
            },
        },
        root,
        require_git_ancestry=require_git_ancestry,
    )
    check(
        "project-human-boundary-suppresses-HNS-and-closes-event-noop",
        bound_human_boundary.get("mcp_control_decision", {}).get("outcome") == "USER_DECISION_REQUIRED"
        and bound_human_boundary.get("mcp_context_navigation_options") is None
        and bound_human_boundary.get("human_navigation_surface_required") is False
        and bound_human_boundary.get("context_transition", {}).get("predicted_receipt", {}).get("mutated") is False,
    )
    invalid_transition_context = resolve(
        {
            "objective_ref": "PROJECT-CONTEXT-TRANSITION-INVALID",
            "project_bound": True,
            "control_context_binding": context_binding,
            "control_context_intent_assessment": _fixture_intent_assessment(
                root,
                context_binding,
                intent_candidate="PAUSE_REQUEST",
                materiality_hint="MATERIAL",
                explicitness="EXPLICIT",
            ),
            "context_transition_candidate": {
                "project_operations": [{"operation": "UNKNOWN"}],
                "session_operations": [],
            },
        },
        root,
        require_git_ancestry=require_git_ancestry,
    )
    check(
        "invalid-context-transition-becomes-canonical-MCP-BLOCK",
        invalid_transition_context.get("mcp_control_decision", {}).get("outcome") == "BLOCK"
        and "CONTROL_CONTEXT_TRANSITION_INVALID" in invalid_transition_context.get("mcp_control_decision", {}).get("invalidates", [])
        and invalid_transition_context.get("context_transition") is None,
    )
    stale_context_binding = copy.deepcopy(context_binding)
    stale_context_binding["expected_session_revision"] += 1
    stale_context = resolve(
        {
            "objective_ref": "PROJECT-STALE-CONTEXT",
            "project_bound": True,
            "control_context_binding": stale_context_binding,
        },
        root,
        require_git_ancestry=require_git_ancestry,
    )
    check(
        "stale-project-context-binding-blocks-zero-adaptive",
        stale_context.get("mcp_control_decision", {}).get("outcome") == "BLOCK"
        and "CONTROL_CONTEXT_BINDING_STALE" in stale_context.get("mcp_control_decision", {}).get("invalidates", [])
        and stale_context.get("adaptive_invoked") is False,
    )

    suspected_constitutional = resolve({
        "objective_ref": "CONSTITUTION-SUSPECTED",
        "constitutional_breach_candidates": [{"candidate_id": "CBR-S", "article_id": "C-03", "state": "SUSPECTED", "material": True}],
    }, root, require_git_ancestry=require_git_ancestry)
    check(
        "suspected-constitutional-breach-does-not-auto-block",
        suspected_constitutional.get("constitutional_compliance", {}).get("state") == "SUSPECTED"
        and suspected_constitutional.get("mcp_control_decision", {}).get("outcome") == "CONTINUE",
    )

    verified_constitutional = resolve({
        "objective_ref": "CONSTITUTION-VERIFIED",
        "constitutional_breach_candidates": [{"candidate_id": "CBR-V", "article_id": "C-05", "state": "VERIFIED", "material": True, "evidence_ref": "EVIDENCE-1"}],
    }, root, require_git_ancestry=require_git_ancestry)
    check(
        "verified-material-constitutional-breach-blocks-before-adaptive",
        verified_constitutional.get("constitutional_compliance", {}).get("state") == "VERIFIED_MATERIAL_BREACH"
        and verified_constitutional.get("mcp_control_decision", {}).get("outcome") == "BLOCK"
        and verified_constitutional.get("adaptive_invoked") is False,
    )
    bound_verified_constitutional = resolve({
        "objective_ref": "CONSTITUTION-VERIFIED-PROJECT-BOUND",
        "project_bound": True,
        "project_ref": "PROJECT-SELFTEST",
        "session_ref": "SESSION-SELFTEST",
        "authoritative_source_commit": git_head(root) or "fixture-source",
        "control_context_binding": context_binding,
        "constitutional_breach_candidates": [
            {
                "candidate_id": "CBR-V-BOUND",
                "article_id": "C-05",
                "state": "VERIFIED",
                "material": True,
                "evidence_ref": "EVIDENCE-BOUND",
            }
        ],
    }, root, require_git_ancestry=require_git_ancestry)
    check(
        "project-bound-block-still-produces-noop-event-completion-directive",
        bound_verified_constitutional.get("mcp_control_decision", {}).get("outcome") == "BLOCK"
        and bound_verified_constitutional.get("context_transition", {}).get("predicted_receipt", {}).get("mutated") is False
        and bound_verified_constitutional.get("context_transition", {}).get("predicted_session_fingerprint_after")
        == context_binding["session"]["fingerprint"],
    )

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
    binding_request = {
        "objective_ref": "SEALED-STANDARD-DELIVERY-BINDING",
        "requested_delivery_profile": "STANDARD",
        "delivery_operations": ["replace", "create"],
        "direct_workspace_access_declared": False,
        "authoritative_source_commit": git_head(root),
        "consequence": "LOW",
        "uncertainty": "LOW",
    }
    binding_result = resolve(binding_request, root, require_git_ancestry=require_git_ancestry)
    sealed_binding = build_delivery_control_binding(binding_result, binding_request)
    binding_manifest = {
        "expected_base_commit": git_head(root),
        "delivery_profile": "STANDARD",
        "delivery_control_binding": sealed_binding,
        "files": [{"operation": "replace"}, {"operation": "create"}],
    }
    binding_validation = validate_delivery_control_binding(binding_manifest, root)
    check(
        "sealed-delivery-binding-normal-consumer",
        binding_validation.get("result") == "PASS"
        and binding_validation.get("normal_call_path_exercised") is True,
    )
    tampered_manifest = json.loads(json.dumps(binding_manifest))
    tampered_manifest["delivery_control_binding"]["control_decision_id"] = "MCPD-TAMPERED"
    check(
        "sealed-delivery-binding-tamper-blocked",
        validate_delivery_control_binding(tampered_manifest, root).get("result") == "BLOCKED",
    )

    ready_closeout = {
        "schema": "cerebro-change-campaign-closeout-receipt/v1",
        "result": "PASS",
        "campaign_id": "SELFTEST-CAMPAIGN",
        "closeout_state": "READY",
        "next_phase": "SELFTEST-NEXT",
        "phase_transition_allowed": True,
        "unknown_or_unclassified_debt_absent": True,
        "contract_fingerprint": "a" * 64,
    }
    ready_transition = resolve({
        "objective_ref": "CAMPAIGN-CLOSEOUT-READY",
        "phase_transition_requested": True,
        "campaign_closeout_receipt": ready_closeout,
        "consequence": "LOW",
        "uncertainty": "LOW",
    }, root, require_git_ancestry=require_git_ancestry)
    check(
        "campaign-closeout-allows-ready-phase-transition",
        ready_transition.get("campaign_phase_transition", {}).get("outcome") == "CONTINUE"
        and ready_transition.get("mcp_control_decision", {}).get("outcome") == "CONTINUE",
    )
    blocked_transition = resolve({
        "objective_ref": "CAMPAIGN-CLOSEOUT-MISSING",
        "phase_transition_requested": True,
        "consequence": "LOW",
        "uncertainty": "LOW",
    }, root, require_git_ancestry=require_git_ancestry)
    check(
        "campaign-closeout-blocks-unproven-phase-transition",
        blocked_transition.get("campaign_phase_transition", {}).get("outcome") == "BLOCK"
        and blocked_transition.get("mcp_control_decision", {}).get("outcome") == "BLOCK"
        and blocked_transition.get("adaptive_invoked") is False,
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
        "constitutional_normal_consumer_clear": test_map.get("constitutional-normal-consumer-clear", False),
        "suspected_constitutional_breach_nonblocking": test_map.get("suspected-constitutional-breach-does-not-auto-block", False),
        "verified_material_constitutional_breach_blocking": test_map.get("verified-material-constitutional-breach-blocks-before-adaptive", False),
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
        "sealed_delivery_binding_normal_consumer": test_map.get("sealed-delivery-binding-normal-consumer", False),
        "sealed_delivery_binding_tamper_blocked": test_map.get("sealed-delivery-binding-tamper-blocked", False),
        "campaign_closeout_allows_ready_transition": test_map.get("campaign-closeout-allows-ready-phase-transition", False),
        "campaign_closeout_blocks_unproven_transition": test_map.get("campaign-closeout-blocks-unproven-phase-transition", False),
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
    parser.add_argument("command", nargs="?", choices=["resolve", "validate-delivery-binding", "activation-probe", "selftest"], default="resolve")
    parser.add_argument("--request")
    parser.add_argument("--manifest")
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
    elif args.command == "validate-delivery-binding":
        if not args.manifest:
            parser.error("validate-delivery-binding requires --manifest")
        result = validate_delivery_control_binding(
            json.loads(Path(args.manifest).read_text(encoding="utf-8")), root
        )
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
