#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BINDING_SCHEMA = "cerebro-human-continuation-binding/v1"
RESPONSE_SCHEMA = "cerebro-human-continuation-response/v1"
ACTIVATION_SCHEMA = "cerebro-human-continuation-activation-proof/v1"
REGISTRY_SCHEMA = "cerebro-human-continuation-binding-registry/v1"
ACTION_OWNER_SCHEMA = "cerebro-action-owner-resolution/v1"
HMI_BOUNDARY_SCHEMA = "cerebro-hmi-boundary-resolution/v1"
ATTENTION_OBJECT_SCHEMA = "cerebro-hmi-attention-object/v1"
BINDING_ID = "HUMAN_CONTINUATION_SURFACE_ENFORCEMENT"
ROADMAP_RECEIPT_SCHEMA = "cerebro-project-terminal-roadmap-projection-receipt/v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ACTIVE_BINDING_REGISTRY = "engines/context/continuation-bindings.json"
REQUIRED_BINDING_FIELDS = (
    "alias",
    "target_ref",
    "operation",
    "current_basis_ref",
    "full_payload_ref",
    "constraints_refs",
    "evidence_refs",
    "maturity",
    "readiness",
    "source_revision",
    "required_next_behavior",
    "resume_order",
    "alternative_paths",
)
EVIDENCE_BASIS_FILES = (
    "standards/human-continuation-surface.yaml",
    "standards/continuation-surface-system-policy.yaml",
    "standards/change-delivery.yaml",
    "engines/interaction/rules.yaml",
    "engines/presentation/rules.yaml",
    ACTIVE_BINDING_REGISTRY,
    "mcp/manifest.yaml",
    "tooling/validator/continuation_surface_validation.py",
    "tooling/validator/human_execution_handoff.py",
    "tooling/builder/builder.yaml",
    "tooling/validator/checks.yaml",
    "tooling/validator/contract-activation-bindings.json",
    "standards/project-terminal-roadmap-projection.yaml",
    "engines/presentation/roadmap-official.yaml",
    "engines/presentation/roadmap_official.py",
)
MACHINE_PAYLOAD_PATTERNS = (
    re.compile(r"[;=]"),
    re.compile(r"(?:^|\s)[&|>$](?:\s|$)"),
    re.compile(r"(?:[A-Za-z]:\\|/[-A-Za-z0-9_.]+/)"),
    re.compile(r"https?://", re.IGNORECASE),
    re.compile(r"\b[0-9a-f]{40}(?:[0-9a-f]{24})?\b", re.IGNORECASE),
)
ATTENTION_MATERIAL_FIELDS = (
    "authority_or_consent",
    "irreversible_or_external_effect",
    "material_scope",
    "tradeoff_or_branch",
    "safety_privacy_or_legal",
    "material_premise",
    "final_governing_promotion",
)
ATTENTION_LIFECYCLES = {"OPEN", "PRESENTED", "ANSWERED", "SUPERSEDED"}
ATTENTION_MACHINE_LOCAL_STEPS = {
    "REFINE",
    "PROMOTE_INTERMEDIATE",
    "CROSSREAD",
    "VALIDATE",
}
ATTENTION_CONTINUATION_STEPS = ATTENTION_MACHINE_LOCAL_STEPS | {
    "GOVERNING_GATE",
    "SAME_STATE_REVISION",
}
ATTENTION_INVALID_MATERIAL_TOKENS = {"", "UNKNOWN", "STALE", "UNRESOLVED", "N/A"}


class ContinuationSurfaceError(ValueError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ContinuationSurfaceError("json-object-required")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def attention_material_fingerprint(material_state: dict[str, Any]) -> str:
    if not isinstance(material_state, dict):
        raise ContinuationSurfaceError("attention-material-state-required")
    if set(material_state) != set(ATTENTION_MATERIAL_FIELDS):
        raise ContinuationSurfaceError("attention-material-state-fields-mismatch")
    normalized: dict[str, str] = {}
    for field in ATTENTION_MATERIAL_FIELDS:
        value = material_state.get(field)
        if not isinstance(value, str):
            raise ContinuationSurfaceError(f"attention-material-{field.replace('_', '-')}-string-required")
        value = value.strip()
        if value.upper() in ATTENTION_INVALID_MATERIAL_TOKENS:
            raise ContinuationSurfaceError(f"attention-material-{field.replace('_', '-')}-unknown-or-stale")
        normalized[field] = value
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _attention_object_id(material_fingerprint: str) -> str:
    return f"ATTENTION-{material_fingerprint[:20].upper()}"


def _validate_attention_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContinuationSurfaceError(f"attention-{label}-object-required")
    if value.get("schema") != ATTENTION_OBJECT_SCHEMA:
        raise ContinuationSurfaceError(f"attention-{label}-schema-mismatch")
    if value.get("provider_ownership") != "CURRENT":
        raise ContinuationSurfaceError(f"attention-{label}-provider-current-ownership-required")
    lifecycle = str(value.get("lifecycle") or "").strip().upper()
    if lifecycle not in ATTENTION_LIFECYCLES:
        raise ContinuationSurfaceError(f"attention-{label}-lifecycle-unknown-or-stale")
    fingerprint = str(value.get("material_state_fingerprint") or "").strip().lower()
    recomputed = attention_material_fingerprint(value.get("material_state"))
    if not SHA256_RE.fullmatch(fingerprint) or fingerprint != recomputed:
        raise ContinuationSurfaceError(f"attention-{label}-material-fingerprint-mismatch")
    if value.get("attention_object_id") != _attention_object_id(recomputed):
        raise ContinuationSurfaceError(f"attention-{label}-object-id-mismatch")
    interrupt_count = value.get("interrupt_count")
    if isinstance(interrupt_count, bool) or not isinstance(interrupt_count, int) or interrupt_count not in {0, 1}:
        raise ContinuationSurfaceError(f"attention-{label}-interrupt-count-invalid")
    if lifecycle == "OPEN" and interrupt_count != 0:
        raise ContinuationSurfaceError(f"attention-{label}-open-cannot-have-interrupt")
    if lifecycle in {"PRESENTED", "ANSWERED"} and interrupt_count != 1:
        raise ContinuationSurfaceError(f"attention-{label}-{lifecycle.lower()}-requires-one-interrupt")
    display_context = value.get("display_context", {})
    if not isinstance(display_context, dict):
        raise ContinuationSurfaceError(f"attention-{label}-display-context-must-be-object")
    return {
        "object": value,
        "lifecycle": lifecycle,
        "fingerprint": recomputed,
        "material_state": value["material_state"],
        "interrupt_count": interrupt_count,
    }


def _validate_attention_resolution(
    value: Any,
    *,
    boundary_kind: str,
    governing_gate: bool,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContinuationSurfaceError("attention-object-resolution-required")
    current = _validate_attention_object(value.get("current"), "current")
    previous_value = value.get("previous")
    previous = None if previous_value is None else _validate_attention_object(previous_value, "previous")
    step = str(value.get("continuation_step") or "").strip().upper()
    if step not in ATTENTION_CONTINUATION_STEPS:
        raise ContinuationSurfaceError("attention-continuation-step-invalid")
    supplied_change_classes = value.get("material_change_classes")
    if not isinstance(supplied_change_classes, list) or not all(isinstance(item, str) for item in supplied_change_classes):
        raise ContinuationSurfaceError("attention-material-change-classes-required")
    change_classes = [item.strip().lower() for item in supplied_change_classes]
    if len(change_classes) != len(set(change_classes)) or any(item not in ATTENTION_MATERIAL_FIELDS for item in change_classes):
        raise ContinuationSurfaceError("attention-material-change-class-invalid")

    if previous is None:
        relation = "NEW_MATERIAL_STATE"
        changed_fields = [
            field for field in ATTENTION_MATERIAL_FIELDS
            if str(current["material_state"][field]).strip().upper() != "NONE"
        ]
    else:
        relation = "SAME_MATERIAL_STATE" if current["fingerprint"] == previous["fingerprint"] else "NEW_MATERIAL_STATE"
        changed_fields = [
            field for field in ATTENTION_MATERIAL_FIELDS
            if current["material_state"][field] != previous["material_state"][field]
        ]
        if relation == "SAME_MATERIAL_STATE" and current["object"].get("attention_object_id") != previous["object"].get("attention_object_id"):
            raise ContinuationSurfaceError("attention-same-material-state-object-id-mismatch")

    if sorted(change_classes) != sorted(changed_fields):
        raise ContinuationSurfaceError("attention-material-change-classes-mismatch")
    if relation == "SAME_MATERIAL_STATE" and change_classes:
        raise ContinuationSurfaceError("attention-same-state-cannot-declare-material-change")
    if relation == "NEW_MATERIAL_STATE" and not change_classes:
        raise ContinuationSurfaceError("attention-new-state-requires-material-change")

    if step in ATTENTION_MACHINE_LOCAL_STEPS:
        if previous is None or relation != "SAME_MATERIAL_STATE":
            raise ContinuationSurfaceError("attention-microiteration-requires-existing-same-material-state")
        if boundary_kind != "MACHINE_CONTINUATION" or governing_gate:
            raise ContinuationSurfaceError("attention-microiteration-must-drain-machine-locally")
        if current["lifecycle"] != previous["lifecycle"] or current["interrupt_count"] != previous["interrupt_count"]:
            raise ContinuationSurfaceError("attention-microiteration-cannot-change-lifecycle-or-interrupt-count")

    if governing_gate:
        if step != "GOVERNING_GATE":
            raise ContinuationSurfaceError("attention-governing-gate-step-required")
        if current["lifecycle"] != "PRESENTED" or current["interrupt_count"] != 1:
            raise ContinuationSurfaceError("attention-governing-gate-must-present-exactly-once")
        if previous is not None and relation == "SAME_MATERIAL_STATE":
            if previous["lifecycle"] == "ANSWERED":
                raise ContinuationSurfaceError("attention-answered-same-state-must-carry-forward-without-new-gate")
            if previous["lifecycle"] == "PRESENTED":
                raise ContinuationSurfaceError("attention-presented-same-state-cannot-reinterrupt")
            if previous["lifecycle"] != "OPEN":
                raise ContinuationSurfaceError("attention-same-state-governing-gate-invalid-predecessor")
        if previous is not None and relation == "NEW_MATERIAL_STATE" and previous["lifecycle"] != "SUPERSEDED":
            raise ContinuationSurfaceError("attention-material-change-must-supersede-predecessor")
    else:
        if step == "GOVERNING_GATE":
            raise ContinuationSurfaceError("attention-governing-step-requires-governing-human-gate")
        if previous is not None and relation == "SAME_MATERIAL_STATE":
            if previous["lifecycle"] in {"ANSWERED", "PRESENTED"} and current["lifecycle"] != previous["lifecycle"]:
                raise ContinuationSurfaceError("attention-same-state-carry-forward-lifecycle-mismatch")
        elif relation == "NEW_MATERIAL_STATE" and current["lifecycle"] != "OPEN":
            raise ContinuationSurfaceError("attention-new-unpresented-state-must-remain-open")

    return {
        "attention_object_validated": True,
        "attention_object_id": current["object"]["attention_object_id"],
        "material_state_fingerprint": current["fingerprint"],
        "material_state_relation": relation,
        "material_change_classes": change_classes,
        "new_attention_object_count": 1 if relation == "NEW_MATERIAL_STATE" else 0,
        "same_answered_state_carried_forward": (
            previous is not None and relation == "SAME_MATERIAL_STATE" and previous["lifecycle"] == "ANSWERED"
        ),
        "same_presented_state_reinterrupt_suppressed": (
            previous is not None and relation == "SAME_MATERIAL_STATE" and previous["lifecycle"] == "PRESENTED"
        ),
        "machine_local_microiteration_drained": step in ATTENTION_MACHINE_LOCAL_STEPS,
        "continuation_step": step,
    }


def _canonical_json(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def binding_fingerprint(binding: dict[str, Any]) -> str:
    subject = {key: value for key, value in binding.items() if key != "binding_fingerprint"}
    return hashlib.sha256(_canonical_json(subject)).hexdigest()


def _word_count(alias: str) -> int:
    return len([part for part in re.split(r"\s+", alias.strip()) if part])


def validate_alias(alias: Any) -> str:
    if not isinstance(alias, str):
        raise ContinuationSurfaceError("alias-string-required")
    normalized = " ".join(alias.strip().split())
    if normalized != alias:
        raise ContinuationSurfaceError("alias-must-be-normalized")
    count = _word_count(alias)
    if count < 2 or count > 5:
        raise ContinuationSurfaceError(f"alias-word-budget-2-to-5-required:{count}")
    if any(pattern.search(alias) for pattern in MACHINE_PAYLOAD_PATTERNS):
        raise ContinuationSurfaceError("alias-machine-payload-prohibited")
    if alias.endswith(('.', ':', ';', ',')):
        raise ContinuationSurfaceError("alias-must-be-action-trigger-not-sentence")
    return alias


def validate_binding(binding: dict[str, Any]) -> dict[str, Any]:
    if binding.get("schema") != BINDING_SCHEMA:
        raise ContinuationSurfaceError("binding-schema-mismatch")
    if binding.get("surface_kind") != "SHORT_HUMAN_TRIGGER":
        raise ContinuationSurfaceError("normal-short-trigger-surface-required")
    for field in REQUIRED_BINDING_FIELDS:
        if field not in binding:
            raise ContinuationSurfaceError(f"binding-field-missing:{field}")
    alias = validate_alias(binding.get("alias"))
    for field in ("target_ref", "operation", "current_basis_ref", "full_payload_ref", "maturity", "readiness", "source_revision"):
        if not isinstance(binding.get(field), str) or not str(binding[field]).strip():
            raise ContinuationSurfaceError(f"binding-field-empty:{field}")
    for field in ("constraints_refs", "evidence_refs", "required_next_behavior", "resume_order", "alternative_paths"):
        if not isinstance(binding.get(field), list):
            raise ContinuationSurfaceError(f"binding-array-required:{field}")
    if "full_payload" in binding or "canonical_command" in binding:
        raise ContinuationSurfaceError("authoritative-payload-must-remain-referenced-not-visible")
    expected = binding_fingerprint(binding)
    if binding.get("binding_fingerprint") != expected:
        raise ContinuationSurfaceError("binding-fingerprint-mismatch")
    return {"result": "PASS", "alias": alias, "word_count": _word_count(alias), "binding_fingerprint": expected}


def validate_registry(registry: dict[str, Any]) -> dict[str, Any]:
    if registry.get("schema") != REGISTRY_SCHEMA:
        raise ContinuationSurfaceError("binding-registry-schema-mismatch")
    if registry.get("status") != "ACTIVE":
        raise ContinuationSurfaceError("binding-registry-not-active")
    active_id = registry.get("active_binding_id")
    if not isinstance(active_id, str) or not active_id:
        raise ContinuationSurfaceError("active-binding-id-required")
    bindings = registry.get("bindings")
    if not isinstance(bindings, list) or not bindings:
        raise ContinuationSurfaceError("binding-registry-bindings-required")
    ids = [item.get("binding_id") for item in bindings if isinstance(item, dict)]
    if len(ids) != len(bindings) or len(ids) != len(set(ids)):
        raise ContinuationSurfaceError("binding-registry-ids-invalid")
    matches = [item for item in bindings if item.get("binding_id") == active_id]
    if len(matches) != 1:
        raise ContinuationSurfaceError("active-binding-cardinality-invalid")
    validation = validate_binding(matches[0])
    return {"result": "PASS", "active_binding_id": active_id, "binding": matches[0], "validation": validation}



def validate_terminal_roadmap_projection_receipt(receipt: Any, binding: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(receipt, dict):
        raise ContinuationSurfaceError("terminal-roadmap-projection-receipt-required")
    if receipt.get("schema") != ROADMAP_RECEIPT_SCHEMA:
        raise ContinuationSurfaceError("terminal-roadmap-projection-receipt-schema-mismatch")
    if receipt.get("result") != "PASS":
        raise ContinuationSurfaceError("terminal-roadmap-projection-receipt-not-pass")
    if receipt.get("authority") != "DERIVED_NON_AUTHORITATIVE_PROJECTION_EVIDENCE":
        raise ContinuationSurfaceError("terminal-roadmap-projection-authority-invalid")
    project_ref = str(candidate.get("terminal_project_ref") or "")
    if not project_ref or receipt.get("terminal_project_ref") != project_ref:
        raise ContinuationSurfaceError("terminal-roadmap-project-ref-mismatch")
    if receipt.get("projection_basis_ref") != binding.get("current_basis_ref"):
        raise ContinuationSurfaceError("terminal-roadmap-projection-basis-mismatch")
    if receipt.get("source_revision") != binding.get("source_revision"):
        raise ContinuationSurfaceError("terminal-roadmap-source-revision-mismatch")
    for field in ("projection_fingerprint", "pdf_sha256"):
        value = str(receipt.get(field) or "").lower()
        if not SHA256_RE.fullmatch(value):
            raise ContinuationSurfaceError("terminal-roadmap-" + field.replace("_", "-") + "-invalid")
    if receipt.get("provider_readback_verified") is not True:
        raise ContinuationSurfaceError("terminal-roadmap-provider-readback-not-verified")
    if receipt.get("stable_identity_verified") is not True:
        raise ContinuationSurfaceError("terminal-roadmap-stable-identity-not-verified")
    if not str(receipt.get("stable_drive_file_id") or "").strip():
        raise ContinuationSurfaceError("terminal-roadmap-stable-drive-file-id-missing")
    return {"result":"PASS","terminal_project_ref":project_ref,"projection_fingerprint":receipt["projection_fingerprint"],"stable_drive_file_id":receipt["stable_drive_file_id"],"pdf_authority":"NONE"}

def _terminal_code_block(text: str) -> tuple[str, str]:
    stripped = text.rstrip()
    match = re.search(r"(?s)(?:^|\n)```(?:text)?\s*\n([^`\r\n]+)\r?\n```$", stripped)
    if not match:
        raise ContinuationSurfaceError("terminal-copyable-trigger-block-required")
    return stripped, match.group(1).strip()


def validate_response(candidate: dict[str, Any]) -> dict[str, Any]:
    if candidate.get("schema") != RESPONSE_SCHEMA:
        raise ContinuationSurfaceError("response-schema-mismatch")
    if candidate.get("human_action_is_next") is not True:
        raise ContinuationSurfaceError("validator-only-accepts-real-human-boundary-candidates")
    binding = candidate.get("binding")
    if not isinstance(binding, dict):
        raise ContinuationSurfaceError("response-binding-required")
    binding_result = validate_binding(binding)
    roadmap_validation = None
    if candidate.get("project_terminal_boundary") is True:
        roadmap_validation = validate_terminal_roadmap_projection_receipt(candidate.get("roadmap_projection_receipt"), binding, candidate)
    response_text = candidate.get("response_text")
    if not isinstance(response_text, str) or not response_text.strip():
        raise ContinuationSurfaceError("response-text-required")
    stripped, visible_alias = _terminal_code_block(response_text)
    if visible_alias != binding_result["alias"]:
        raise ContinuationSurfaceError("visible-trigger-does-not-match-active-binding")
    if stripped.count(visible_alias) != 1:
        raise ContinuationSurfaceError("exactly-one-visible-trigger-required")
    return {
        "schema": "cerebro-human-continuation-response-validation/v1",
        "result": "PASS",
        "alias": visible_alias,
        "word_count": binding_result["word_count"],
        "absolute_response_end": True,
        "machine_payload_separated": True,
        "binding_fingerprint": binding_result["binding_fingerprint"],
        "terminal_roadmap_projection_validated": roadmap_validation is not None,
        "terminal_roadmap_projection": roadmap_validation,
    }


def validate_action_owner_resolution(candidate: dict[str, Any]) -> dict[str, Any]:
    if candidate.get("schema") != ACTION_OWNER_SCHEMA:
        raise ContinuationSurfaceError("action-owner-schema-mismatch")
    if candidate.get("next_step_bounded") is not True:
        raise ContinuationSurfaceError("bounded-next-step-required")
    if candidate.get("authorized") is not True:
        raise ContinuationSurfaceError("authorized-next-step-required")

    actor = candidate.get("actor")
    if not isinstance(actor, dict):
        raise ContinuationSurfaceError("action-actor-required")
    actor_role = str(actor.get("role") or "").strip().upper()
    actor_id = str(actor.get("actor_id") or "").strip()
    if not actor_role or not actor_id:
        raise ContinuationSurfaceError("action-actor-identity-required")
    if actor_role == "PROJECT_MANAGER":
        raise ContinuationSurfaceError("project-manager-bind-admit-only-not-action-owner")

    work_mode = candidate.get("work_mode")
    if not isinstance(work_mode, dict):
        raise ContinuationSurfaceError("work-mode-classification-required")
    work_mode_enabled = work_mode.get("enabled") is True
    capability_proven = work_mode.get("capability_proven") is True
    receipt_observable = work_mode.get("local_receipt_observable") is True
    internally_executable = candidate.get("internally_executable") is True
    owner = str(candidate.get("resolved_owner") or "").strip().upper()
    human_transport = candidate.get("human_transport_requested") is True
    physical_human_action = candidate.get("physical_human_action_required") is True
    exact_invalidator = str(candidate.get("exact_capability_or_access_invalidator") or "").strip()

    if work_mode_enabled and actor_role != "IMPLEMENTER":
        raise ContinuationSurfaceError("work-mode-local-execution-role-must-be-implementer")

    machine_owned = work_mode_enabled and capability_proven and receipt_observable
    if machine_owned:
        if owner != "CEREBRO":
            raise ContinuationSurfaceError("machine-observable-work-mode-action-must-remain-cerebro-owned")
        if human_transport:
            raise ContinuationSurfaceError("human-receipt-transport-prohibited-when-machine-observable")
        if physical_human_action and internally_executable:
            raise ContinuationSurfaceError("internally-executable-step-cannot-require-human-action")
    elif internally_executable and owner != "CEREBRO":
        raise ContinuationSurfaceError("internally-executable-action-must-remain-cerebro-owned")

    if owner == "HUMAN":
        if exact_invalidator.upper() in {"", "NONE", "UNKNOWN", "UNRESOLVED", "N/A"}:
            raise ContinuationSurfaceError("human-fallback-requires-exact-capability-or-access-invalidator")
        if not physical_human_action:
            raise ContinuationSurfaceError("human-owner-requires-genuine-physical-action")
    elif owner != "CEREBRO":
        raise ContinuationSurfaceError("resolved-owner-must-be-cerebro-or-human")

    return {
        "schema": "cerebro-action-owner-resolution-validation/v1",
        "result": "PASS",
        "resolved_owner": owner,
        "actor_role": actor_role,
        "actor_id": actor_id,
        "work_mode_machine_observable": machine_owned,
        "implementer_self_consumption_required": machine_owned,
        "human_transport_prohibited": machine_owned,
        "exact_capability_invalidator_fallback": owner == "HUMAN",
        "genuine_human_physical_boundary_preserved": owner == "HUMAN" and physical_human_action,
    }


def validate_hmi_boundary_resolution(candidate: dict[str, Any]) -> dict[str, Any]:
    if candidate.get("schema") != HMI_BOUNDARY_SCHEMA:
        raise ContinuationSurfaceError("hmi-boundary-schema-mismatch")
    if candidate.get("surface_first") is not True:
        raise ContinuationSurfaceError("hmi-surface-first-required")
    for field in ("role_assessment","responsibility_assessment","human_boundary_assessment","presentation_request"):
        if not isinstance(candidate.get(field),dict):
            raise ContinuationSurfaceError(f"hmi-{field.replace('_','-')}-required")

    role=candidate["role_assessment"]
    responsibility=candidate["responsibility_assessment"]
    boundary=candidate["human_boundary_assessment"]
    presentation=candidate["presentation_request"]
    role_name=str(role.get("role") or "").strip().upper()
    owner=str(responsibility.get("owner") or "").strip().upper()
    action_kind=str(responsibility.get("action_kind") or "").strip().upper()
    boundary_kind=str(boundary.get("boundary_kind") or "").strip().upper()
    governing_gate=boundary.get("governing_human_gate_is_next") is True
    real_human=boundary.get("real_human_action_is_next") is True
    governance_effect=str(boundary.get("governance_effect") or "").strip().upper()
    machine_route=candidate.get("machine_route_available") is True
    human_relay=candidate.get("human_relay_requested") is True
    allowed_semantics = {
        "MACHINE_CONTINUATION": ("MACHINE_CONTINUATION", False, False, "NONE"),
        "FOREGROUND_TRANSPORT": ("FOREGROUND_TRANSPORT", True, False, "NONE"),
        "GOVERNING_HUMAN_GATE": ("GOVERNING_CONSENT", True, True, "APPROVAL_OR_CONSENT"),
    }
    if boundary_kind not in allowed_semantics:
        raise ContinuationSurfaceError("hmi-boundary-kind-required")
    expected_action, expected_real_human, expected_governing, expected_effect = allowed_semantics[boundary_kind]
    if (action_kind, real_human, governing_gate, governance_effect) != (
        expected_action, expected_real_human, expected_governing, expected_effect
    ):
        raise ContinuationSurfaceError("hmi-boundary-action-semantics-mismatch")
    if role_name=="HA" and any(key in role for key in ("actor_id","generation_id","identity_fingerprint")):
        raise ContinuationSurfaceError("HA-presentation-shorthand-cannot-carry-actor-identity")
    if candidate.get("actor_identity_mutation_requested") is True:
        raise ContinuationSurfaceError("hmi-actor-identity-mutation-prohibited")
    if machine_route and boundary_kind != "GOVERNING_HUMAN_GATE" and human_relay:
        raise ContinuationSurfaceError("machine-route-required-before-human-relay")
    gate=str(boundary.get("next_human_gate") or "").strip()
    gate_present=gate.upper() not in {"","NONE"}
    terminal_textbox=False
    if boundary_kind == "GOVERNING_HUMAN_GATE":
        if owner != "HUMAN" or not gate_present:
            raise ContinuationSurfaceError("genuine-human-gate-must-remain-visible")
        if presentation.get("surface_kind") != "TERMINAL_COPYABLE_TEXTBOX":
            raise ContinuationSurfaceError("governing-human-gate-terminal-textbox-required")
        if presentation.get("gate_source") != "human_boundary_assessment.next_human_gate":
            raise ContinuationSurfaceError("governing-human-gate-current-source-required")
        response_text=presentation.get("response_text")
        if not isinstance(response_text,str) or not response_text.strip():
            raise ContinuationSurfaceError("governing-human-gate-response-text-required")
        stripped, visible_gate=_terminal_code_block(response_text)
        if stripped.count("```") != 2:
            raise ContinuationSurfaceError("exactly-one-governing-human-gate-textbox-required")
        if visible_gate != gate or stripped.count(gate) != 1:
            raise ContinuationSurfaceError("governing-human-gate-must-match-current-next-human-gate")
        terminal_textbox=True
    elif gate_present:
        if boundary_kind == "FOREGROUND_TRANSPORT":
            raise ContinuationSurfaceError("foreground-transport-cannot-be-approval-gate")
        raise ContinuationSurfaceError("machine-owned-next-cannot-be-presented-as-human-duty")
    if boundary_kind == "FOREGROUND_TRANSPORT":
        if owner != "HUMAN" or presentation.get("surface_kind") != "FOREGROUND_TRANSPORT_INSTRUCTION":
            raise ContinuationSurfaceError("foreground-transport-semantics-required")
    if boundary_kind == "MACHINE_CONTINUATION":
        if owner not in {"MACHINE","CEREBRO","IMPLEMENTER"} or presentation.get("surface_kind") != "NO_HUMAN_SURFACE":
            raise ContinuationSurfaceError("machine-continuation-semantics-required")
    attention_value = candidate.get("attention_object_resolution")
    if governing_gate and attention_value is None:
        raise ContinuationSurfaceError("governing-human-gate-attention-object-required")
    attention_result = {
        "attention_object_validated": False,
        "new_attention_object_count": 0,
        "same_answered_state_carried_forward": False,
        "same_presented_state_reinterrupt_suppressed": False,
        "machine_local_microiteration_drained": False,
    }
    if attention_value is not None:
        attention_result = _validate_attention_resolution(
            attention_value,
            boundary_kind=boundary_kind,
            governing_gate=governing_gate,
        )
    return {
        "schema":"cerebro-hmi-boundary-resolution-validation/v1","result":"PASS",
        "surface_first":True,"typed_signals_consumed":True,
        "boundary_kind":boundary_kind,
        "action_kind":action_kind,
        "machine_route_before_human_relay":machine_route and boundary_kind == "MACHINE_CONTINUATION",
        "genuine_human_gate_visible":governing_gate and gate_present and terminal_textbox,
        "governing_human_gate_terminal_textbox":terminal_textbox,
        "governing_human_gate_textbox_count":1 if terminal_textbox else 0,
        "gate_source_is_current_next_human_gate":terminal_textbox,
        "absolute_response_end":terminal_textbox,
        "foreground_transport_not_governing":boundary_kind == "FOREGROUND_TRANSPORT" and not governing_gate,
        "ha_expansion":"HUMAN_ADMIN" if role_name=="HA" else None,
        "ha_presentation_shorthand_only":role_name=="HA",
        "actor_identity_mutation_allowed":False,
        "carrier_change_identity_effect":"NONE",
        **attention_result,
    }


def _fixture_binding(alias: str = "Fortsett DualityArc Wave 02") -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema": BINDING_SCHEMA,
        "surface_kind": "SHORT_HUMAN_TRIGGER",
        "alias": alias,
        "target_ref": "DUALITYARC-WAVE-02",
        "operation": "CONTINUE",
        "current_basis_ref": "CTX-BASIS-DUALITY-ARC-WAVE-02",
        "full_payload_ref": "CEREBRO-CONTINUATION/DUALITYARC-WAVE-02",
        "constraints_refs": ["CEREBRO-HUMAN-CONTINUATION-SURFACE-001"],
        "evidence_refs": ["CURRENT-SOURCE"],
        "maturity": "LOCKED",
        "readiness": "READY",
        "source_revision": "fixture",
        "required_next_behavior": ["LOAD_FULL_STATE", "CONTINUE"],
        "resume_order": ["VERIFY_SOURCE", "LOAD_BINDING", "CONTINUE"],
        "alternative_paths": [],
    }
    value["binding_fingerprint"] = binding_fingerprint(value)
    return value


def _must_reject(label: str, function, value: dict[str, Any]) -> bool:
    try:
        function(value)
    except ContinuationSurfaceError:
        return True
    raise ContinuationSurfaceError(f"negative-canary-not-rejected:{label}")


def _fixture_attention_material(**overrides: str) -> dict[str, str]:
    material = {field: "NONE" for field in ATTENTION_MATERIAL_FIELDS}
    material["authority_or_consent"] = "AUTHORITY-CANARY-V1"
    material.update(overrides)
    return material


def _fixture_attention_object(
    material_state: dict[str, str],
    lifecycle: str,
    interrupt_count: int,
    *,
    display_context: dict[str, str] | None = None,
) -> dict[str, Any]:
    fingerprint = attention_material_fingerprint(material_state)
    return {
        "schema": ATTENTION_OBJECT_SCHEMA,
        "provider_ownership": "CURRENT",
        "attention_object_id": _attention_object_id(fingerprint),
        "material_state": dict(material_state),
        "material_state_fingerprint": fingerprint,
        "lifecycle": lifecycle,
        "interrupt_count": interrupt_count,
        "display_context": dict(display_context or {}),
    }


def selftest() -> dict[str, Any]:
    binding = _fixture_binding()
    response = {
        "schema": RESPONSE_SCHEMA,
        "human_action_is_next": True,
        "binding": binding,
        "response_text": "Wave 02 er klar.\n\n```text\nFortsett DualityArc Wave 02\n```",
    }
    validate_response(response)

    one_word = _fixture_binding("Fortsett")
    one_word["binding_fingerprint"] = binding_fingerprint(one_word)
    six_words = _fixture_binding("Lagre og fortsett DualityArc Wave 02")
    six_words["binding_fingerprint"] = binding_fingerprint(six_words)
    payload = _fixture_binding("Fortsett; commit=0123456789012345678901234567890123456789")
    payload["binding_fingerprint"] = binding_fingerprint(payload)
    nonterminal = dict(response)
    nonterminal["response_text"] = response["response_text"] + "\nMer tekst"
    mismatched = dict(response)
    mismatched["response_text"] = "```text\nFortsett noe annet\n```"
    terminal = dict(response)
    terminal["project_terminal_boundary"] = True
    terminal["terminal_project_ref"] = "PROJECT-TERMINAL-1"
    terminal["roadmap_projection_receipt"] = {
        "schema": ROADMAP_RECEIPT_SCHEMA, "result": "PASS",
        "authority": "DERIVED_NON_AUTHORITATIVE_PROJECTION_EVIDENCE",
        "terminal_project_ref": "PROJECT-TERMINAL-1",
        "projection_basis_ref": binding["current_basis_ref"],
        "source_revision": binding["source_revision"],
        "projection_fingerprint": "1" * 64, "pdf_sha256": "2" * 64,
        "provider_readback_verified": True, "stable_identity_verified": True,
        "stable_drive_file_id": "DRIVE-STABLE-ROADMAP-ID",
    }
    terminal_result = validate_response(terminal)
    missing_terminal_receipt = dict(terminal); missing_terminal_receipt.pop("roadmap_projection_receipt")
    stale_terminal_receipt = dict(terminal); stale_terminal_receipt["roadmap_projection_receipt"] = dict(terminal["roadmap_projection_receipt"]); stale_terminal_receipt["roadmap_projection_receipt"]["projection_basis_ref"] = "STALE"

    machine_action = {
        "schema": ACTION_OWNER_SCHEMA,
        "next_step_bounded": True,
        "authorized": True,
        "internally_executable": True,
        "resolved_owner": "CEREBRO",
        "actor": {"role": "IMPLEMENTER", "actor_id": "IMPLEMENTER_GENERIC_TEST"},
        "work_mode": {"enabled": True, "capability_proven": True, "local_receipt_observable": True},
        "human_transport_requested": False,
        "physical_human_action_required": False,
    }
    machine_result = validate_action_owner_resolution(machine_action)
    human_courier = json.loads(json.dumps(machine_action))
    human_courier["resolved_owner"] = "HUMAN"
    human_courier["human_transport_requested"] = True
    human_courier["physical_human_action_required"] = True
    human_courier["exact_capability_or_access_invalidator"] = "NONE"
    worker_work_mode = json.loads(json.dumps(machine_action))
    worker_work_mode["actor"] = {"role": "WORKER", "actor_id": "WORKER_TEST"}
    human_fallback = json.loads(json.dumps(machine_action))
    human_fallback["internally_executable"] = False
    human_fallback["resolved_owner"] = "HUMAN"
    human_fallback["work_mode"]["capability_proven"] = False
    human_fallback["work_mode"]["local_receipt_observable"] = False
    human_fallback["physical_human_action_required"] = True
    human_fallback["exact_capability_or_access_invalidator"] = "LOCAL_RECEIPT_NOT_MACHINE_OBSERVABLE"
    fallback_result = validate_action_owner_resolution(human_fallback)

    hmi_machine = {
        "schema": HMI_BOUNDARY_SCHEMA, "surface_first": True,
        "role_assessment": {"role":"HA"},
        "responsibility_assessment": {"owner":"MACHINE","action_kind":"MACHINE_CONTINUATION"},
        "human_boundary_assessment": {"boundary_kind":"MACHINE_CONTINUATION","real_human_action_is_next":False,"governing_human_gate_is_next":False,"governance_effect":"NONE","next_human_gate":"NONE"},
        "presentation_request": {"dialect":"IMPLEMENTER","surface_kind":"NO_HUMAN_SURFACE"},
        "machine_route_available": True, "human_relay_requested": False,
        "actor_identity_mutation_requested": False, "carrier_changed": True,
    }
    hmi_machine_result=validate_hmi_boundary_resolution(hmi_machine)
    hmi_human=json.loads(json.dumps(hmi_machine))
    hmi_human["responsibility_assessment"]={"owner":"HUMAN","action_kind":"GOVERNING_CONSENT"}
    hmi_human["human_boundary_assessment"]={"boundary_kind":"GOVERNING_HUMAN_GATE","real_human_action_is_next":True,"governing_human_gate_is_next":True,"governance_effect":"APPROVAL_OR_CONSENT","next_human_gate":"GODKJENN CANARY V2"}
    hmi_human["presentation_request"]={"dialect":"IMPLEMENTER","surface_kind":"TERMINAL_COPYABLE_TEXTBOX","gate_source":"human_boundary_assessment.next_human_gate","response_text":"Godkjenning kreves.\n\n```text\nGODKJENN CANARY V2\n```"}
    attention_material=_fixture_attention_material()
    attention_presented=_fixture_attention_object(attention_material,"PRESENTED",1)
    hmi_human["attention_object_resolution"]={
        "current":attention_presented,
        "previous":None,
        "continuation_step":"GOVERNING_GATE",
        "material_change_classes":["authority_or_consent"],
    }
    hmi_human_result=validate_hmi_boundary_resolution(hmi_human)
    hmi_dynamic_gate=json.loads(json.dumps(hmi_human))
    hmi_dynamic_gate["human_boundary_assessment"]["next_human_gate"]="GODKJENN CANARY V3"
    hmi_dynamic_gate["presentation_request"]["response_text"]="Godkjenning kreves.\n\n```text\nGODKJENN CANARY V3\n```"
    dynamic_material=_fixture_attention_material(authority_or_consent="AUTHORITY-CANARY-V2")
    hmi_dynamic_gate["attention_object_resolution"]={
        "current":_fixture_attention_object(dynamic_material,"PRESENTED",1),
        "previous":None,
        "continuation_step":"GOVERNING_GATE",
        "material_change_classes":["authority_or_consent"],
    }
    hmi_dynamic_gate_result=validate_hmi_boundary_resolution(hmi_dynamic_gate)
    hmi_foreground=json.loads(json.dumps(hmi_human))
    hmi_foreground["responsibility_assessment"]={"owner":"HUMAN","action_kind":"FOREGROUND_TRANSPORT"}
    hmi_foreground["human_boundary_assessment"]={"boundary_kind":"FOREGROUND_TRANSPORT","real_human_action_is_next":True,"governing_human_gate_is_next":False,"governance_effect":"NONE","next_human_gate":"NONE"}
    hmi_foreground["presentation_request"]={"dialect":"IMPLEMENTER","surface_kind":"FOREGROUND_TRANSPORT_INSTRUCTION"}
    hmi_foreground["machine_route_available"]=False
    hmi_foreground["human_relay_requested"]=True
    hmi_foreground.pop("attention_object_resolution",None)
    hmi_foreground_result=validate_hmi_boundary_resolution(hmi_foreground)
    hmi_manual_actor=json.loads(json.dumps(hmi_machine)); hmi_manual_actor["role_assessment"]["actor_id"]="MANUAL"
    hmi_relay=json.loads(json.dumps(hmi_machine)); hmi_relay["human_relay_requested"]=True
    hmi_hidden_gate=json.loads(json.dumps(hmi_human)); hmi_hidden_gate["human_boundary_assessment"].pop("next_human_gate")
    hmi_prose_only=json.loads(json.dumps(hmi_human)); hmi_prose_only["presentation_request"]["response_text"]="GODKJENN CANARY V2"
    hmi_content_after=json.loads(json.dumps(hmi_human)); hmi_content_after["presentation_request"]["response_text"] += "\nIkke-terminalt innhold"
    hmi_multiple_textboxes=json.loads(json.dumps(hmi_human)); hmi_multiple_textboxes["presentation_request"]["response_text"]="```text\nIKKE EN PORT\n```\n\n" + hmi_multiple_textboxes["presentation_request"]["response_text"]
    hmi_foreground_approval=json.loads(json.dumps(hmi_foreground)); hmi_foreground_approval["human_boundary_assessment"]["next_human_gate"]="GODKJENN CANARY V2"
    hmi_identity_mutation=json.loads(json.dumps(hmi_machine)); hmi_identity_mutation["actor_identity_mutation_requested"]=True

    attention_answered_previous=_fixture_attention_object(
        attention_material,"ANSWERED",1,
        display_context={"display":"CARD","version":"RC1","status":"WAITING","room":"ROOM-A"},
    )
    attention_answered_current=_fixture_attention_object(
        attention_material,"ANSWERED",1,
        display_context={"display":"PANEL","version":"RC9","status":"CURRENT","room":"ROOM-Z"},
    )
    hmi_answered_carry=json.loads(json.dumps(hmi_machine))
    hmi_answered_carry["attention_object_resolution"]={
        "current":attention_answered_current,
        "previous":attention_answered_previous,
        "continuation_step":"SAME_STATE_REVISION",
        "material_change_classes":[],
    }
    hmi_answered_carry_result=validate_hmi_boundary_resolution(hmi_answered_carry)

    hmi_answered_regate=json.loads(json.dumps(hmi_human))
    hmi_answered_regate["attention_object_resolution"]={
        "current":attention_presented,
        "previous":attention_answered_previous,
        "continuation_step":"GOVERNING_GATE",
        "material_change_classes":[],
    }
    hmi_presented_reinterrupt=json.loads(json.dumps(hmi_human))
    hmi_presented_reinterrupt["attention_object_resolution"]={
        "current":attention_presented,
        "previous":attention_presented,
        "continuation_step":"GOVERNING_GATE",
        "material_change_classes":[],
    }
    hmi_forged_fingerprint=json.loads(json.dumps(hmi_human))
    hmi_forged_fingerprint["attention_object_resolution"]["current"]["material_state_fingerprint"]="0"*64
    hmi_unknown_material=json.loads(json.dumps(hmi_human))
    hmi_unknown_material["attention_object_resolution"]["current"]["material_state"]["material_scope"]="UNKNOWN"
    hmi_stale_lifecycle=json.loads(json.dumps(hmi_human))
    hmi_stale_lifecycle["attention_object_resolution"]["current"]["lifecycle"]="STALE"

    microiteration_results: dict[str, dict[str, Any]] = {}
    for micro_step in sorted(ATTENTION_MACHINE_LOCAL_STEPS):
        micro_candidate=json.loads(json.dumps(hmi_machine))
        micro_candidate["attention_object_resolution"]={
            "current":attention_answered_current,
            "previous":attention_answered_previous,
            "continuation_step":micro_step,
            "material_change_classes":[],
        }
        microiteration_results[micro_step]=validate_hmi_boundary_resolution(micro_candidate)

    material_change_results: dict[str, dict[str, Any]] = {}
    for material_field in ATTENTION_MATERIAL_FIELDS:
        changed_material=dict(attention_material)
        changed_material[material_field]=f"{material_field.upper()}-V2"
        material_candidate=json.loads(json.dumps(hmi_human))
        material_candidate["attention_object_resolution"]={
            "current":_fixture_attention_object(changed_material,"PRESENTED",1),
            "previous":_fixture_attention_object(attention_material,"SUPERSEDED",1),
            "continuation_step":"GOVERNING_GATE",
            "material_change_classes":[material_field],
        }
        material_change_results[material_field]=validate_hmi_boundary_resolution(material_candidate)

    reversed_attention_material=dict(reversed(list(attention_material.items())))
    deterministic_attention_fingerprint=(
        attention_material_fingerprint(attention_material)
        == attention_material_fingerprint(reversed_attention_material)
    )

    policy_text = (Path(__file__).resolve().parents[2] / "standards/continuation-surface-system-policy.yaml").read_text(encoding="utf-8")
    fixed_point_tokens = (
        "machine_fixed_point_handoff:",
        "execute-authorized-command",
        "verify-provider-readback",
        "canonical-control-reresolve",
        "REAL_HUMAN_GATE",
        "NONPROGRESS_CYCLE",
        "user-pulse-as-machine-loop-clock: PROHIBITED",
    )
    if not all(token in policy_text for token in fixed_point_tokens):
        raise ContinuationSurfaceError("machine-fixed-point-handoff-contract-missing")

    return {
        "result": "PASS",
        "valid_short_trigger_accepted": True,
        "machine_fixed_point_handoff_contract_bound": all(token in policy_text for token in fixed_point_tokens),
        "one_word_rejected": _must_reject("one-word", validate_binding, one_word),
        "six_word_rejected": _must_reject("six-word", validate_binding, six_words),
        "machine_payload_rejected": _must_reject("machine-payload", validate_binding, payload),
        "nonterminal_surface_rejected": _must_reject("nonterminal", validate_response, nonterminal),
        "mismatched_binding_rejected": _must_reject("mismatched-binding", validate_response, mismatched),
        "terminal_roadmap_projection_accepted": terminal_result.get("terminal_roadmap_projection_validated") is True,
        "terminal_missing_projection_rejected": _must_reject("terminal-missing-roadmap-projection", validate_response, missing_terminal_receipt),
        "terminal_stale_projection_rejected": _must_reject("terminal-stale-roadmap-projection", validate_response, stale_terminal_receipt),
        "workmode_machine_observable_action_accepted": machine_result.get("implementer_self_consumption_required") is True,
        "workmode_human_courier_rejected": _must_reject("workmode-human-courier", validate_action_owner_resolution, human_courier),
        "exact_capability_invalidator_fallback_accepted": fallback_result.get("genuine_human_physical_boundary_preserved") is True,
        "non_implementer_workmode_rejected": _must_reject("non-implementer-workmode", validate_action_owner_resolution, worker_work_mode),
        "typed_hmi_machine_route_accepted": hmi_machine_result.get("machine_route_before_human_relay") is True,
        "typed_hmi_genuine_human_gate_accepted": hmi_human_result.get("genuine_human_gate_visible") is True,
        "governing_gate_GODKJENN_CANARY_V2_terminal_textbox_accepted": hmi_human_result.get("governing_human_gate_terminal_textbox") is True,
        "dynamic_gate_command_without_source_mutation_accepted": hmi_dynamic_gate_result.get("gate_source_is_current_next_human_gate") is True,
        "foreground_transport_not_approval_gate_accepted": hmi_foreground_result.get("foreground_transport_not_governing") is True,
        "attention_material_fingerprint_deterministic_accepted": deterministic_attention_fingerprint,
        "attention_forged_fingerprint_rejected": _must_reject("attention-forged-fingerprint",validate_hmi_boundary_resolution,hmi_forged_fingerprint),
        "attention_answered_same_state_carry_forward_accepted": hmi_answered_carry_result.get("same_answered_state_carried_forward") is True,
        "attention_answered_same_state_regate_rejected": _must_reject("attention-answered-same-state-regate",validate_hmi_boundary_resolution,hmi_answered_regate),
        "attention_presented_same_state_reinterrupt_rejected": _must_reject("attention-presented-same-state-reinterrupt",validate_hmi_boundary_resolution,hmi_presented_reinterrupt),
        "attention_machine_local_REFINE_accepted": microiteration_results["REFINE"].get("machine_local_microiteration_drained") is True,
        "attention_machine_local_PROMOTE_INTERMEDIATE_accepted": microiteration_results["PROMOTE_INTERMEDIATE"].get("machine_local_microiteration_drained") is True,
        "attention_machine_local_CROSSREAD_accepted": microiteration_results["CROSSREAD"].get("machine_local_microiteration_drained") is True,
        "attention_machine_local_VALIDATE_accepted": microiteration_results["VALIDATE"].get("machine_local_microiteration_drained") is True,
        "attention_material_change_classes_single_object_accepted": all(
            result.get("new_attention_object_count") == 1
            for result in material_change_results.values()
        ),
        "attention_nonmaterial_display_version_status_room_reuse_accepted": (
            hmi_answered_carry_result.get("new_attention_object_count") == 0
            and hmi_answered_carry_result.get("material_state_relation") == "SAME_MATERIAL_STATE"
        ),
        "attention_UNKNOWN_material_state_rejected": _must_reject("attention-unknown-material",validate_hmi_boundary_resolution,hmi_unknown_material),
        "attention_STALE_lifecycle_rejected": _must_reject("attention-stale-lifecycle",validate_hmi_boundary_resolution,hmi_stale_lifecycle),
        "HA_manual_actor_identity_rejected": _must_reject("HA-manual-actor",validate_hmi_boundary_resolution,hmi_manual_actor),
        "machine_route_human_relay_rejected": _must_reject("machine-route-human-relay",validate_hmi_boundary_resolution,hmi_relay),
        "hidden_genuine_human_gate_rejected": _must_reject("hidden-genuine-human-gate",validate_hmi_boundary_resolution,hmi_hidden_gate),
        "prose_only_governing_gate_rejected": _must_reject("prose-only-governing-gate",validate_hmi_boundary_resolution,hmi_prose_only),
        "content_after_governing_gate_rejected": _must_reject("content-after-governing-gate",validate_hmi_boundary_resolution,hmi_content_after),
        "multiple_governing_gate_textboxes_rejected": _must_reject("multiple-governing-gate-textboxes",validate_hmi_boundary_resolution,hmi_multiple_textboxes),
        "foreground_transport_approval_gate_rejected": _must_reject("foreground-transport-approval-gate",validate_hmi_boundary_resolution,hmi_foreground_approval),
        "carrier_identity_mutation_rejected": _must_reject("carrier-identity-mutation",validate_hmi_boundary_resolution,hmi_identity_mutation),
    }


def _source_fingerprint(root: Path) -> str:
    rows: list[str] = []
    for relative in sorted(EVIDENCE_BASIS_FILES):
        path = root / relative
        if not path.is_file():
            raise ContinuationSurfaceError(f"activation-basis-file-missing:{relative}")
        rows.append(f"{relative}|{hashlib.sha256(path.read_bytes()).hexdigest()}")
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def activation_probe(root: Path) -> dict[str, Any]:
    checks = selftest()
    registry_result = validate_registry(_read_json(root / ACTIVE_BINDING_REGISTRY))
    renderer_path = root / "engines/presentation/roadmap_official.py"
    spec = importlib.util.spec_from_file_location("cerebro_roadmap_official_activation", renderer_path)
    if spec is None or spec.loader is None:
        raise ContinuationSurfaceError("roadmap-renderer-load-failed")
    renderer = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(renderer)
        renderer_checks = renderer.selftest()
    finally:
        sys.dont_write_bytecode = previous
    if renderer_checks.get("result") != "PASS":
        raise ContinuationSurfaceError("roadmap-renderer-selftest-failed")
    continuation_policy = (
        root / "standards" / "continuation-surface-system-policy.yaml"
    ).read_text(encoding="utf-8")
    operational_pulse_tokens = (
        "consume-CURRENT_CONTACT-correction-and-report-debt-through-constructor-bound-provider-reader",
        "persist-operational-pulse-through-existing-REFRESH_GOVERNING_REFS-effect-and-readback",
    )
    if not all(token in continuation_policy for token in operational_pulse_tokens):
        raise ContinuationSurfaceError("continuation-operational-pulse-binding-missing")
    return {
        "schema": ACTIVATION_SCHEMA,
        "result": "PASS",
        "binding_id": BINDING_ID,
        "proves_bindings": [BINDING_ID],
        "basis_files": list(EVIDENCE_BASIS_FILES),
        "source_state_fingerprint": _source_fingerprint(root),
        "binding_validation_executed": True,
        "active_binding_registry_validated": True,
        "active_binding_id": registry_result["active_binding_id"],
        "active_alias": registry_result["validation"]["alias"],
        "response_candidate_consumer_exercised": True,
        "normal_word_budget_enforced": True,
        "machine_payload_separation_enforced": True,
        "absolute_response_end_enforced": True,
        "full_state_reference_required": True,
        "visible_alias_is_trigger_not_state": True,
        "action_owner_resolution_exercised": checks.get("workmode_machine_observable_action_accepted") is True,
        "typed_hmi_boundary_resolution_exercised": checks.get("typed_hmi_machine_route_accepted") is True,
        "typed_interaction_signals_consumed": checks.get("typed_hmi_machine_route_accepted") is True,
        "machine_route_before_human_relay_enforced": checks.get("machine_route_human_relay_rejected") is True,
        "genuine_human_gate_visibility_enforced": checks.get("typed_hmi_genuine_human_gate_accepted") is True,
        "attention_object_material_fingerprint_enforced": checks.get("attention_material_fingerprint_deterministic_accepted") is True and checks.get("attention_forged_fingerprint_rejected") is True,
        "attention_object_answered_carry_forward_enforced": checks.get("attention_answered_same_state_carry_forward_accepted") is True and checks.get("attention_answered_same_state_regate_rejected") is True,
        "attention_object_single_interrupt_enforced": checks.get("attention_presented_same_state_reinterrupt_rejected") is True,
        "attention_object_machine_local_microiteration_drain_enforced": all(
            checks.get(f"attention_machine_local_{step}_accepted") is True
            for step in ATTENTION_MACHINE_LOCAL_STEPS
        ),
        "attention_object_material_change_singleton_enforced": checks.get("attention_material_change_classes_single_object_accepted") is True,
        "attention_object_nonmaterial_reuse_enforced": checks.get("attention_nonmaterial_display_version_status_room_reuse_accepted") is True,
        "attention_object_unknown_stale_fail_closed": checks.get("attention_UNKNOWN_material_state_rejected") is True and checks.get("attention_STALE_lifecycle_rejected") is True,
        "HA_presentation_shorthand_only_enforced": checks.get("HA_manual_actor_identity_rejected") is True,
        "carrier_change_identity_invariance_enforced": checks.get("carrier_identity_mutation_rejected") is True,
        "current_contact_operational_debt_consumer_bound": True,
        "operational_pulse_existing_effect_readback_bound": True,
        "workmode_machine_observable_self_consumption_enforced": checks.get("workmode_human_courier_rejected") is True,
        "exact_capability_invalidator_fallback_preserved": checks.get("exact_capability_invalidator_fallback_accepted") is True,
        "non_implementer_workmode_scope_preserved": checks.get("non_implementer_workmode_rejected") is True,
        "terminal_roadmap_projection_gate_exercised": checks.get("terminal_roadmap_projection_accepted") is True,
        "terminal_missing_projection_blocked": checks.get("terminal_missing_projection_rejected") is True,
        "terminal_stale_projection_blocked": checks.get("terminal_stale_projection_rejected") is True,
        "roadmap_renderer_selftest_passed": renderer_checks.get("result") == "PASS",
        "negative_canaries_passed": all(value is True for key, value in checks.items() if key.endswith(("_rejected", "_accepted"))),
        "checks": checks,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p_binding = sub.add_parser("validate-binding")
    p_binding.add_argument("--input", required=True)
    p_binding.add_argument("--output")
    p_response = sub.add_parser("validate-response")
    p_response.add_argument("--input", required=True)
    p_response.add_argument("--output")
    p_action_owner = sub.add_parser("validate-action-owner")
    p_action_owner.add_argument("--input", required=True)
    p_action_owner.add_argument("--output")
    p_hmi = sub.add_parser("validate-hmi-boundary")
    p_hmi.add_argument("--input", required=True)
    p_hmi.add_argument("--output")
    p_registry = sub.add_parser("validate-registry")
    p_registry.add_argument("--input", required=True)
    p_registry.add_argument("--output")
    p_resolve = sub.add_parser("resolve-active-binding")
    p_resolve.add_argument("--source-root", required=True)
    p_resolve.add_argument("--output")
    p_probe = sub.add_parser("activation-probe")
    p_probe.add_argument("--source-root", required=True)
    p_probe.add_argument("--output", required=True)
    p_selftest = sub.add_parser("selftest")
    p_selftest.add_argument("--output")
    args = parser.parse_args()
    try:
        if args.command == "validate-binding":
            result = validate_binding(_read_json(Path(args.input)))
        elif args.command == "validate-response":
            result = validate_response(_read_json(Path(args.input)))
        elif args.command == "validate-action-owner":
            result = validate_action_owner_resolution(_read_json(Path(args.input)))
        elif args.command == "validate-hmi-boundary":
            result = validate_hmi_boundary_resolution(_read_json(Path(args.input)))
        elif args.command == "validate-registry":
            result = validate_registry(_read_json(Path(args.input)))
        elif args.command == "resolve-active-binding":
            result = validate_registry(_read_json(Path(args.source_root) / ACTIVE_BINDING_REGISTRY))
        elif args.command == "activation-probe":
            result = activation_probe(Path(args.source_root))
        else:
            result = selftest()
        output = getattr(args, "output", None)
        if output:
            _write_json(Path(output), result)
        else:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except Exception as exc:
        result = {"result": "BLOCK", "error": str(exc)}
        output = getattr(args, "output", None)
        if output:
            _write_json(Path(output), result)
        else:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
