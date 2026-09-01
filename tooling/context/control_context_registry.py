#!/usr/bin/env python3
"""Pure domain logic for Cerebro hierarchical project-control state.

The shared project tree and each consumer session are separate versioned aggregates.
This module performs no persistence, network, repository or authorization action.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Iterable


PROJECT_STATE_SCHEMA = "cerebro-control-context-project-state/v1"
SESSION_STATE_SCHEMA = "cerebro-control-session-state/v1"
CONTEXT_SCHEMA = "cerebro-control-context/v1"
BINDING_SCHEMA = "cerebro-control-continuation-binding/v1"
DIRECTIVE_SCHEMA = "cerebro-control-context-transition-directive/v1"
RECEIPT_SCHEMA = "cerebro-control-context-transition-receipt/v1"
ACTOR_GENERATION_SHADOW_SCHEMA = "cerebro-actor-generation-shadow/v1"
WORK_CLAIM_SHADOW_SCHEMA = "cerebro-work-claim-shadow/v1"

PROJECT_STATUSES = {"ACTIVE", "PAUSED", "BLOCKED", "COMPLETED", "CANCELLED"}
LIFECYCLES = {"OPEN", "RETURNED", "CLOSED", "CANCELLED"}
CONTROL_CONDITIONS = {"READY", "PAUSED_BY_USER", "WAITING_HUMAN", "STALLED", "SAFE_HOLD", None}
DISPOSITIONS = {"NONE", "PENDING_JOIN", "INCORPORATED", "PRESERVED", "SUPERSEDED", None}
CLOSED_DISPOSITIONS = {"INCORPORATED", "PRESERVED", "SUPERSEDED"}
PROJECT_OPERATIONS = {
    "CREATE_CHILD",
    "SET_CONTROL_CONDITION",
    "RETURN_CONTEXT",
    "APPLY_JOIN_DISPOSITION",
    "CLOSE_CONTEXT",
    "CANCEL_CONTEXT",
    "CREATE_DERIVED_CONTEXT",
    "REFRESH_GOVERNING_REFS",
    "SET_DEFAULT_CONTEXT",
}
SESSION_OPERATIONS = {"SET_ACTIVE", "SET_CONTINUATION_BINDING", "CLEAR_CONTINUATION_BINDING"}
ACTOR_ROLES = {"PRINCIPAL", "ASSISTANT", "PROJECT_MANAGER", "IMPLEMENTER", "WORKER", "RESEARCHER"}
ACTOR_GENERATION_LIFECYCLES = {"READY", "ACTIVE", "RETIRED"}
WORK_CLAIM_LIFECYCLES = {
    "BOUND_ACTIVE_PRESTART", "ACTIVE", "TERMINAL_PASS", "TERMINAL_FAIL", "RELEASED"
}

SHA256 = re.compile(r"^[0-9a-f]{64}$")
MACHINE_PAYLOAD_PATTERNS = (
    re.compile(r"[;=]"),
    re.compile(r"(?:^|\s)[&|>$](?:\s|$)"),
    re.compile(r"(?:[A-Za-z]:\\|/[-A-Za-z0-9_.]+/)"),
    re.compile(r"https?://", re.IGNORECASE),
    re.compile(r"\b[0-9a-f]{40}(?:[0-9a-f]{24})?\b", re.IGNORECASE),
)


class ControlContextError(ValueError):
    """A state or transition violates the control-context contract."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ControlContextError(message)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def actor_generation_shadow_fingerprint(state: dict[str, Any]) -> str:
    subject = copy.deepcopy(state)
    subject.pop("fingerprint", None)
    return _sha256(subject)


def work_claim_shadow_fingerprint(state: dict[str, Any]) -> str:
    subject = copy.deepcopy(state)
    subject.pop("fingerprint", None)
    return _sha256(subject)


def validate_actor_generation_shadow(state: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema", "tenant_ref", "workspace_ref", "actor_ref", "role", "generation_ref",
        "lifecycle", "source_revision", "revision", "authority", "fingerprint",
    }
    _require(isinstance(state, dict), "actor-generation-shadow-object-required")
    _require(set(state) == required, "actor-generation-shadow-fields-mismatch")
    _require(state.get("schema") == ACTOR_GENERATION_SHADOW_SCHEMA, "actor-generation-shadow-schema-mismatch")
    for field in ("tenant_ref", "workspace_ref", "actor_ref", "generation_ref", "source_revision"):
        _require(isinstance(state.get(field), str) and bool(state[field].strip()), f"actor-generation-shadow-{field}-required")
    _require(state.get("role") in ACTOR_ROLES, "actor-generation-shadow-role-invalid")
    _require(state.get("lifecycle") in ACTOR_GENERATION_LIFECYCLES, "actor-generation-shadow-lifecycle-invalid")
    _require(state.get("authority") == "SHADOW_ONLY", "actor-generation-shadow-authority-must-be-shadow-only")
    _require(isinstance(state.get("revision"), int) and state["revision"] >= 1, "actor-generation-shadow-revision-invalid")
    _require(state.get("fingerprint") == actor_generation_shadow_fingerprint(state), "actor-generation-shadow-fingerprint-mismatch")
    return {"result": "PASS", "role": state["role"], "generation_ref": state["generation_ref"], "revision": state["revision"]}


def bootstrap_actor_generation_shadow(
    *, tenant_ref: str, workspace_ref: str, actor_ref: str, role: str,
    generation_ref: str, source_revision: str, lifecycle: str = "READY",
) -> dict[str, Any]:
    state = {
        "schema": ACTOR_GENERATION_SHADOW_SCHEMA,
        "tenant_ref": tenant_ref,
        "workspace_ref": workspace_ref,
        "actor_ref": actor_ref,
        "role": role,
        "generation_ref": generation_ref,
        "lifecycle": lifecycle,
        "source_revision": source_revision,
        "revision": 1,
        "authority": "SHADOW_ONLY",
    }
    state["fingerprint"] = actor_generation_shadow_fingerprint(state)
    validate_actor_generation_shadow(state)
    return state


def transition_actor_generation_shadow(
    state: dict[str, Any], *, lifecycle: str, source_revision: str,
) -> dict[str, Any]:
    validate_actor_generation_shadow(state)
    allowed = {"READY": {"ACTIVE", "RETIRED"}, "ACTIVE": {"RETIRED"}, "RETIRED": set()}
    _require(lifecycle in allowed[state["lifecycle"]], "actor-generation-shadow-transition-invalid")
    _require(isinstance(source_revision, str) and bool(source_revision.strip()), "actor-generation-shadow-source-revision-required")
    candidate = copy.deepcopy(state)
    candidate.update(lifecycle=lifecycle, source_revision=source_revision, revision=state["revision"] + 1)
    candidate["fingerprint"] = actor_generation_shadow_fingerprint(candidate)
    validate_actor_generation_shadow(candidate)
    return candidate


def validate_work_claim_shadow(
    state: dict[str, Any], actor_generation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    required = {
        "schema", "tenant_ref", "workspace_ref", "claim_ref", "project_ref", "actor_ref",
        "actor_role", "actor_generation_ref", "scope_ref", "mode", "lifecycle",
        "source_revision", "revision", "authority", "live_claim", "fingerprint",
    }
    _require(isinstance(state, dict), "work-claim-shadow-object-required")
    _require(set(state) == required, "work-claim-shadow-fields-mismatch")
    _require(state.get("schema") == WORK_CLAIM_SHADOW_SCHEMA, "work-claim-shadow-schema-mismatch")
    for field in (
        "tenant_ref", "workspace_ref", "claim_ref", "project_ref", "actor_ref",
        "actor_generation_ref", "scope_ref", "mode", "source_revision",
    ):
        _require(isinstance(state.get(field), str) and bool(state[field].strip()), f"work-claim-shadow-{field}-required")
    _require(state.get("actor_role") in ACTOR_ROLES, "work-claim-shadow-role-invalid")
    _require(state.get("lifecycle") in WORK_CLAIM_LIFECYCLES, "work-claim-shadow-lifecycle-invalid")
    _require(state.get("authority") == "SHADOW_ONLY" and state.get("live_claim") is False, "work-claim-shadow-cannot-be-live-authority")
    _require(isinstance(state.get("revision"), int) and state["revision"] >= 1, "work-claim-shadow-revision-invalid")
    _require(state.get("fingerprint") == work_claim_shadow_fingerprint(state), "work-claim-shadow-fingerprint-mismatch")
    if actor_generation is not None:
        validate_actor_generation_shadow(actor_generation)
        _require(actor_generation["tenant_ref"] == state["tenant_ref"] and actor_generation["workspace_ref"] == state["workspace_ref"], "work-claim-shadow-actor-scope-mismatch")
        _require(actor_generation["actor_ref"] == state["actor_ref"], "work-claim-shadow-actor-ref-mismatch")
        _require(actor_generation["role"] == state["actor_role"], "work-claim-shadow-actor-role-mismatch")
        _require(actor_generation["generation_ref"] == state["actor_generation_ref"], "work-claim-shadow-actor-generation-mismatch")
        _require(actor_generation["lifecycle"] != "RETIRED", "work-claim-shadow-retired-generation-prohibited")
        if state["lifecycle"] == "ACTIVE":
            _require(actor_generation["lifecycle"] == "ACTIVE", "work-claim-shadow-active-requires-active-generation")
    return {"result": "PASS", "claim_ref": state["claim_ref"], "revision": state["revision"]}


def bootstrap_work_claim_shadow(
    *, tenant_ref: str, workspace_ref: str, claim_ref: str, project_ref: str,
    actor_generation: dict[str, Any], scope_ref: str, mode: str, source_revision: str,
) -> dict[str, Any]:
    validate_actor_generation_shadow(actor_generation)
    state = {
        "schema": WORK_CLAIM_SHADOW_SCHEMA,
        "tenant_ref": tenant_ref,
        "workspace_ref": workspace_ref,
        "claim_ref": claim_ref,
        "project_ref": project_ref,
        "actor_ref": actor_generation["actor_ref"],
        "actor_role": actor_generation["role"],
        "actor_generation_ref": actor_generation["generation_ref"],
        "scope_ref": scope_ref,
        "mode": mode,
        "lifecycle": "BOUND_ACTIVE_PRESTART",
        "source_revision": source_revision,
        "revision": 1,
        "authority": "SHADOW_ONLY",
        "live_claim": False,
    }
    state["fingerprint"] = work_claim_shadow_fingerprint(state)
    validate_work_claim_shadow(state, actor_generation)
    return state


def transition_work_claim_shadow(
    state: dict[str, Any], actor_generation: dict[str, Any], *, lifecycle: str,
    source_revision: str,
) -> dict[str, Any]:
    validate_work_claim_shadow(state, actor_generation)
    allowed = {
        "BOUND_ACTIVE_PRESTART": {"ACTIVE", "RELEASED"},
        "ACTIVE": {"TERMINAL_PASS", "TERMINAL_FAIL", "RELEASED"},
        "TERMINAL_PASS": set(), "TERMINAL_FAIL": set(), "RELEASED": set(),
    }
    _require(lifecycle in allowed[state["lifecycle"]], "work-claim-shadow-transition-invalid")
    _require(isinstance(source_revision, str) and bool(source_revision.strip()), "work-claim-shadow-source-revision-required")
    candidate = copy.deepcopy(state)
    candidate.update(lifecycle=lifecycle, source_revision=source_revision, revision=state["revision"] + 1)
    candidate["fingerprint"] = work_claim_shadow_fingerprint(candidate)
    validate_work_claim_shadow(candidate, actor_generation)
    return candidate


def validate_trusted_role_generation_binding(
    actor_generation: dict[str, Any], *, required_role: str, generation_ref: str,
) -> dict[str, Any]:
    validate_actor_generation_shadow(actor_generation)
    _require(actor_generation["role"] == required_role, "trusted-role-generation-role-mismatch")
    _require(actor_generation["generation_ref"] == generation_ref, "trusted-role-generation-ref-mismatch")
    _require(actor_generation["lifecycle"] == "ACTIVE", "trusted-role-generation-not-active")
    return {
        "result": "PASS",
        "authority": "SHADOW_ONLY",
        "actor_ref": actor_generation["actor_ref"],
        "role": actor_generation["role"],
        "generation_ref": actor_generation["generation_ref"],
        "fingerprint": actor_generation["fingerprint"],
    }


def _finalize_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    """Seal a deterministic transition receipt without circular identity fields."""

    candidate = copy.deepcopy(receipt)
    candidate.setdefault("message_kind", "CONTROL_CONTEXT_TRANSITION_RECEIPT")
    candidate.setdefault("producer_ref", "context")
    candidate.setdefault("subject_ref", candidate.get("project_ref"))
    candidate.setdefault("correlation_ref", candidate.get("event_id"))
    candidate.setdefault("control_decision_ref", candidate.get("decision_ref"))
    candidate.setdefault("result", "PASS")
    candidate.setdefault(
        "applied_operations",
        list(candidate.get("project_operations", [])) + list(candidate.get("session_operations", [])),
    )
    candidate.pop("receipt_id", None)
    candidate.pop("receipt_fingerprint", None)
    candidate["receipt_fingerprint"] = _sha256(candidate)
    candidate["receipt_id"] = "CTR-" + candidate["receipt_fingerprint"][:24].upper()
    return candidate


def validate_transition_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema", "message_kind", "producer_ref", "subject_ref", "correlation_ref",
        "control_decision_ref", "event_id", "decision_ref", "project_ref", "result",
        "mutated", "project_revision_before", "project_revision_after",
        "session_revision_before", "session_revision_after", "project_fingerprint_before",
        "project_fingerprint_after", "session_fingerprint_before", "session_fingerprint_after",
        "project_operations", "session_operations", "applied_operations",
        "active_context_ref_after", "receipt_fingerprint", "receipt_id",
    }
    missing = sorted(required.difference(receipt))
    _require(not missing, "receipt-fields-missing:" + ",".join(missing))
    _require(receipt.get("schema") == RECEIPT_SCHEMA, "receipt-schema-mismatch")
    _require(receipt.get("message_kind") == "CONTROL_CONTEXT_TRANSITION_RECEIPT", "receipt-message-kind-mismatch")
    _require(receipt.get("producer_ref") == "context", "receipt-producer-mismatch")
    _require(receipt.get("subject_ref") == receipt.get("project_ref"), "receipt-subject-mismatch")
    _require(receipt.get("correlation_ref") == receipt.get("event_id"), "receipt-correlation-mismatch")
    _require(receipt.get("control_decision_ref") == receipt.get("decision_ref"), "receipt-decision-mismatch")
    _require(receipt.get("result") in {"PASS", "BLOCKED"}, "receipt-result-invalid")
    expected_operations = list(receipt.get("project_operations", [])) + list(receipt.get("session_operations", []))
    _require(receipt.get("applied_operations") == expected_operations, "receipt-operation-list-mismatch")
    if receipt.get("result") == "BLOCKED":
        _require(receipt.get("mutated") is False, "blocked-receipt-cannot-mutate")
        _require(
            receipt.get("project_fingerprint_after") == receipt.get("project_fingerprint_before")
            and receipt.get("session_fingerprint_after") == receipt.get("session_fingerprint_before"),
            "blocked-receipt-must-preserve-fingerprints",
        )
    subject = copy.deepcopy(receipt)
    subject.pop("receipt_id", None)
    subject.pop("receipt_fingerprint", None)
    expected_fingerprint = _sha256(subject)
    _require(receipt.get("receipt_fingerprint") == expected_fingerprint, "receipt-fingerprint-mismatch")
    _require(receipt.get("receipt_id") == "CTR-" + expected_fingerprint[:24].upper(), "receipt-id-mismatch")
    return {
        "result": "PASS",
        "receipt_id": receipt["receipt_id"],
        "event_id": receipt["event_id"],
        "mutated": receipt["mutated"],
        "project_revision_after": receipt["project_revision_after"],
        "session_revision_after": receipt["session_revision_after"],
    }


def _normalized_refs(value: Any, field: str) -> list[str]:
    _require(isinstance(value, list), f"{field}-array-required")
    refs = [str(item) for item in value]
    _require(all(refs), f"{field}-empty-ref")
    _require(len(refs) == len(set(refs)), f"{field}-duplicate-ref")
    return sorted(refs)


def _word_count(alias: str) -> int:
    return len([part for part in re.split(r"\s+", alias.strip()) if part])


def validate_alias(alias: Any) -> str:
    _require(isinstance(alias, str), "alias-string-required")
    normalized = " ".join(alias.strip().split())
    _require(normalized == alias, "alias-must-be-normalized")
    count = _word_count(alias)
    _require(2 <= count <= 5, f"alias-word-budget-2-to-5-required:{count}")
    _require(not any(pattern.search(alias) for pattern in MACHINE_PAYLOAD_PATTERNS), "alias-machine-payload-prohibited")
    _require(not alias.endswith((".", ":", ";", ",")), "alias-must-be-action-trigger-not-sentence")
    return alias


def context_fingerprint(context: dict[str, Any]) -> str:
    subject = copy.deepcopy(context)
    subject.pop("context_fingerprint", None)
    subject["basis_refs"] = sorted(subject.get("basis_refs", []))
    subject["completion_criteria_refs"] = sorted(subject.get("completion_criteria_refs", []))
    return _sha256(subject)


def continuation_fingerprint(binding: dict[str, Any]) -> str:
    subject = copy.deepcopy(binding)
    subject.pop("binding_fingerprint", None)
    return _sha256(subject)


def project_fingerprint(project: dict[str, Any]) -> str:
    subject = copy.deepcopy(project)
    subject.pop("fingerprint", None)
    subject["contexts"] = sorted(subject.get("contexts", []), key=lambda item: (item.get("sequence", 0), item.get("context_id", "")))
    return _sha256(subject)


def session_fingerprint(session: dict[str, Any]) -> str:
    subject = copy.deepcopy(session)
    subject.pop("fingerprint", None)
    return _sha256(subject)


def _basis_fingerprint(payload: dict[str, Any]) -> str:
    return _sha256(
        {
            "objective_ref": payload.get("objective_ref"),
            "scope_ref": payload.get("scope_ref"),
            "basis_refs": sorted(payload.get("basis_refs", [])),
            "project_basis_ref": payload.get("project_basis_ref"),
            "quality_trace_ref": payload.get("quality_trace_ref"),
        }
    )


def refresh_project_fingerprints(project: dict[str, Any]) -> dict[str, Any]:
    candidate = copy.deepcopy(project)
    candidate["contexts"] = sorted(
        candidate.get("contexts", []), key=lambda item: (item.get("sequence", 0), item.get("context_id", ""))
    )
    for context in candidate["contexts"]:
        context["basis_refs"] = sorted(context.get("basis_refs", []))
        context["completion_criteria_refs"] = sorted(context.get("completion_criteria_refs", []))
        context["context_fingerprint"] = context_fingerprint(context)
    candidate["fingerprint"] = project_fingerprint(candidate)
    return candidate


def refresh_session_fingerprint(session: dict[str, Any]) -> dict[str, Any]:
    candidate = copy.deepcopy(session)
    binding = candidate.get("active_continuation_binding")
    if isinstance(binding, dict):
        binding["binding_fingerprint"] = continuation_fingerprint(binding)
    candidate["fingerprint"] = session_fingerprint(candidate)
    return candidate


def _context_map(project: dict[str, Any]) -> dict[str, dict[str, Any]]:
    contexts = project.get("contexts")
    _require(isinstance(contexts, list), "contexts-array-required")
    result: dict[str, dict[str, Any]] = {}
    for item in contexts:
        _require(isinstance(item, dict), "context-object-required")
        context_id = item.get("context_id")
        _require(isinstance(context_id, str) and bool(context_id), "context-id-required")
        _require(context_id not in result, f"duplicate-context-id:{context_id}")
        result[context_id] = item
    return result


def descendant_ids(project: dict[str, Any], context_id: str) -> set[str]:
    mapping = _context_map(project)
    _require(context_id in mapping, f"context-not-found:{context_id}")
    descendants: set[str] = set()
    frontier = [context_id]
    while frontier:
        parent = frontier.pop()
        for child in [item_id for item_id, item in mapping.items() if item.get("parent_context_ref") == parent]:
            _require(child not in descendants, "context-cycle-detected")
            descendants.add(child)
            frontier.append(child)
    return descendants


def ancestor_chain(project: dict[str, Any], context_id: str, include_self: bool = True) -> list[str]:
    mapping = _context_map(project)
    _require(context_id in mapping, f"context-not-found:{context_id}")
    chain = [context_id] if include_self else []
    seen = {context_id}
    cursor = mapping[context_id].get("parent_context_ref")
    while cursor is not None:
        _require(cursor in mapping, f"parent-context-not-found:{cursor}")
        _require(cursor not in seen, "context-cycle-detected")
        chain.append(cursor)
        seen.add(cursor)
        cursor = mapping[cursor].get("parent_context_ref")
    return chain


def lowest_common_ancestor(project: dict[str, Any], context_ids: Iterable[str]) -> str:
    ids = list(context_ids)
    _require(bool(ids), "lca-contexts-required")
    chains = [ancestor_chain(project, context_id) for context_id in ids]
    sets = [set(chain) for chain in chains]
    for candidate in chains[0]:
        if all(candidate in values for values in sets[1:]):
            return candidate
    raise ControlContextError("lca-not-found")


def validate_context(context: dict[str, Any], project_ref: str) -> None:
    required = {
        "schema", "context_id", "project_ref", "parent_context_ref", "derived_from_context_ref",
        "human_label", "objective_ref", "scope_ref", "basis_refs", "project_basis_ref",
        "quality_trace_ref", "lifecycle", "control_condition", "disposition",
        "completion_criteria_refs", "result_ref", "return_target_ref", "created_from_event_ref",
        "last_transition_ref", "basis_fingerprint", "sequence", "context_fingerprint",
    }
    missing = sorted(required.difference(context))
    _require(not missing, "context-fields-missing:" + ",".join(missing))
    _require(context.get("schema") == CONTEXT_SCHEMA, "context-schema-mismatch")
    _require(context.get("project_ref") == project_ref, "context-project-ref-mismatch")
    for field in ("context_id", "human_label", "objective_ref", "scope_ref", "created_from_event_ref"):
        _require(isinstance(context.get(field), str) and bool(context[field].strip()), f"context-{field}-required")
    _normalized_refs(context.get("basis_refs"), "basis-refs")
    _normalized_refs(context.get("completion_criteria_refs"), "completion-criteria-refs")
    _require(context.get("lifecycle") in LIFECYCLES, "context-lifecycle-invalid")
    _require(context.get("control_condition") in CONTROL_CONDITIONS, "context-control-condition-invalid")
    _require(context.get("disposition") in DISPOSITIONS, "context-disposition-invalid")
    _require(isinstance(context.get("sequence"), int) and context["sequence"] >= 1, "context-sequence-invalid")
    _require(isinstance(context.get("basis_fingerprint"), str) and SHA256.fullmatch(context["basis_fingerprint"]) is not None, "context-basis-fingerprint-invalid")
    _require(context.get("context_fingerprint") == context_fingerprint(context), "context-fingerprint-mismatch")

    lifecycle = context["lifecycle"]
    condition = context["control_condition"]
    disposition = context["disposition"]
    if lifecycle == "OPEN":
        _require(disposition == "NONE", "open-context-disposition-must-be-NONE")
    elif lifecycle == "RETURNED":
        _require(condition is None, "returned-context-condition-must-be-null")
        _require(disposition == "PENDING_JOIN", "returned-context-disposition-must-be-PENDING_JOIN")
    elif lifecycle == "CLOSED":
        _require(condition is None, "closed-context-condition-must-be-null")
        _require(disposition in CLOSED_DISPOSITIONS, "closed-context-disposition-invalid")
    else:
        _require(condition is None, "cancelled-context-condition-must-be-null")
        _require(disposition is None, "cancelled-context-disposition-must-be-null")


def validate_project_state(project: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema", "aggregate_id", "tenant_ref", "workspace_ref", "project_ref", "source_revision",
        "project_status", "revision", "default_context_ref", "contexts", "next_sequence", "fingerprint",
    }
    missing = sorted(required.difference(project))
    _require(not missing, "project-fields-missing:" + ",".join(missing))
    _require(project.get("schema") == PROJECT_STATE_SCHEMA, "project-state-schema-mismatch")
    for field in ("aggregate_id", "tenant_ref", "workspace_ref", "project_ref", "source_revision"):
        _require(isinstance(project.get(field), str) and bool(project[field].strip()), f"project-{field}-required")
    _require(project.get("project_status") in PROJECT_STATUSES, "project-status-invalid")
    _require(isinstance(project.get("revision"), int) and project["revision"] >= 1, "project-revision-invalid")
    _require(isinstance(project.get("next_sequence"), int) and project["next_sequence"] >= 2, "next-sequence-invalid")
    mapping = _context_map(project)
    _require(bool(mapping), "project-root-required")
    sequences = [item.get("sequence") for item in mapping.values()]
    _require(all(isinstance(value, int) for value in sequences), "context-sequence-invalid")
    _require(len(sequences) == len(set(sequences)), "duplicate-context-sequence")
    _require(project["next_sequence"] > max(sequences), "next-sequence-must-exceed-context-sequences")
    for context in mapping.values():
        validate_context(context, project["project_ref"])
    roots = [item for item in mapping.values() if item.get("parent_context_ref") is None]
    _require(len(roots) == 1, "exactly-one-root-required")
    for context_id, context in mapping.items():
        parent = context.get("parent_context_ref")
        if parent is not None:
            _require(parent in mapping, f"parent-context-not-found:{parent}")
            _require(mapping[parent].get("project_ref") == project["project_ref"], "cross-project-parent-prohibited")
        derived = context.get("derived_from_context_ref")
        if derived is not None:
            _require(derived in mapping, f"derived-context-not-found:{derived}")
            _require(derived != context_id, "context-cannot-derive-from-self")
            _require(mapping[derived].get("lifecycle") != "OPEN", "derived-source-must-be-historical")
        ancestor_chain(project, context_id)
    default_ref = project.get("default_context_ref")
    if project["project_status"] in {"ACTIVE", "PAUSED", "BLOCKED"}:
        _require(isinstance(default_ref, str) and default_ref in mapping, "default-context-required")
        _require(mapping[default_ref].get("lifecycle") == "OPEN", "default-context-must-be-open")
    else:
        _require(default_ref is None, "terminal-project-default-context-must-be-null")
    _require(isinstance(project.get("fingerprint"), str) and SHA256.fullmatch(project["fingerprint"]) is not None, "project-fingerprint-invalid")
    _require(project.get("fingerprint") == project_fingerprint(project), "project-fingerprint-mismatch")
    return {
        "result": "PASS",
        "project_ref": project["project_ref"],
        "revision": project["revision"],
        "context_count": len(mapping),
        "root_context_ref": roots[0]["context_id"],
        "default_context_ref": default_ref,
        "fingerprint": project["fingerprint"],
    }


def validate_continuation_binding(binding: dict[str, Any], project: dict[str, Any], session: dict[str, Any]) -> None:
    required = {
        "schema", "binding_id", "surface_kind", "alias", "operation", "target_ref", "context_ref",
        "basis_project_revision", "basis_session_revision", "binding_revision", "binding_fingerprint",
    }
    missing = sorted(required.difference(binding))
    _require(not missing, "binding-fields-missing:" + ",".join(missing))
    _require(binding.get("schema") == BINDING_SCHEMA, "binding-schema-mismatch")
    _require(binding.get("surface_kind") in {"HNS", "HCS"}, "binding-surface-kind-invalid")
    validate_alias(binding.get("alias"))
    for field in ("binding_id", "operation", "target_ref", "context_ref"):
        _require(isinstance(binding.get(field), str) and bool(binding[field].strip()), f"binding-{field}-required")
    _require(binding.get("basis_project_revision") == project["revision"], "binding-project-revision-stale")
    _require(binding.get("basis_session_revision") == session["session_revision"], "binding-session-revision-stale")
    _require(isinstance(binding.get("binding_revision"), int) and binding["binding_revision"] >= 1, "binding-revision-invalid")
    mapping = _context_map(project)
    context_ref = binding["context_ref"]
    _require(context_ref in mapping, "binding-context-not-found")
    _require(context_ref == session.get("active_context_ref"), "binding-context-must-be-session-active")
    _require(mapping[context_ref].get("lifecycle") == "OPEN", "binding-context-must-be-open")
    _require(binding.get("binding_fingerprint") == continuation_fingerprint(binding), "binding-fingerprint-mismatch")


def validate_session_state(session: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
    validate_project_state(project)
    required = {
        "schema", "session_binding_id", "tenant_ref", "workspace_ref", "principal_ref", "consumer_ref",
        "session_ref", "project_ref", "project_revision", "session_revision", "active_context_ref",
        "active_continuation_binding", "fingerprint",
    }
    missing = sorted(required.difference(session))
    _require(not missing, "session-fields-missing:" + ",".join(missing))
    _require(session.get("schema") == SESSION_STATE_SCHEMA, "session-state-schema-mismatch")
    for field in ("session_binding_id", "tenant_ref", "workspace_ref", "principal_ref", "consumer_ref", "session_ref", "project_ref"):
        _require(isinstance(session.get(field), str) and bool(session[field].strip()), f"session-{field}-required")
    for field in ("tenant_ref", "workspace_ref", "project_ref"):
        _require(session[field] == project[field], f"session-{field}-mismatch")
    _require(session.get("project_revision") == project["revision"], "session-project-revision-stale")
    _require(isinstance(session.get("session_revision"), int) and session["session_revision"] >= 1, "session-revision-invalid")
    mapping = _context_map(project)
    active_ref = session.get("active_context_ref")
    if project["project_status"] in {"ACTIVE", "PAUSED", "BLOCKED"}:
        _require(isinstance(active_ref, str) and active_ref in mapping, "session-active-context-required")
        _require(mapping[active_ref].get("lifecycle") == "OPEN", "session-active-context-must-be-open")
    else:
        _require(active_ref is None, "terminal-project-session-context-must-be-null")
    binding = session.get("active_continuation_binding")
    _require(binding is None or isinstance(binding, dict), "session-binding-object-or-null")
    if isinstance(binding, dict):
        validate_continuation_binding(binding, project, session)
    _require(isinstance(session.get("fingerprint"), str) and SHA256.fullmatch(session["fingerprint"]) is not None, "session-fingerprint-invalid")
    _require(session.get("fingerprint") == session_fingerprint(session), "session-fingerprint-mismatch")
    return {
        "result": "PASS",
        "session_ref": session["session_ref"],
        "project_ref": session["project_ref"],
        "project_revision": session["project_revision"],
        "session_revision": session["session_revision"],
        "active_context_ref": active_ref,
        "active_binding_id": binding.get("binding_id") if isinstance(binding, dict) else None,
        "fingerprint": session["fingerprint"],
    }


def _make_context(project: dict[str, Any], value: dict[str, Any], event_id: str, parent_ref: str | None, derived_ref: str | None = None) -> dict[str, Any]:
    for field in ("context_id", "human_label", "objective_ref", "scope_ref"):
        _require(isinstance(value.get(field), str) and bool(value[field].strip()), f"{field}-required")
    context: dict[str, Any] = {
        "schema": CONTEXT_SCHEMA,
        "context_id": value["context_id"],
        "project_ref": project["project_ref"],
        "parent_context_ref": parent_ref,
        "derived_from_context_ref": derived_ref,
        "human_label": value["human_label"],
        "objective_ref": value["objective_ref"],
        "scope_ref": value["scope_ref"],
        "basis_refs": _normalized_refs(value.get("basis_refs", []), "basis-refs"),
        "project_basis_ref": value.get("project_basis_ref"),
        "quality_trace_ref": value.get("quality_trace_ref"),
        "lifecycle": "OPEN",
        "control_condition": value.get("control_condition", "READY"),
        "disposition": "NONE",
        "completion_criteria_refs": _normalized_refs(value.get("completion_criteria_refs", []), "completion-criteria-refs"),
        "result_ref": None,
        "return_target_ref": None,
        "created_from_event_ref": event_id,
        "last_transition_ref": event_id,
        "basis_fingerprint": "",
        "sequence": project["next_sequence"],
        "context_fingerprint": "",
    }
    _require(context["control_condition"] in CONTROL_CONDITIONS - {None}, "new-context-control-condition-invalid")
    context["basis_fingerprint"] = value.get("basis_fingerprint") or _basis_fingerprint(context)
    _require(SHA256.fullmatch(context["basis_fingerprint"]) is not None, "new-context-basis-fingerprint-invalid")
    context["context_fingerprint"] = context_fingerprint(context)
    return context


def bootstrap_project_state(
    *,
    aggregate_id: str,
    tenant_ref: str,
    workspace_ref: str,
    project_ref: str,
    source_revision: str,
    event_id: str,
    decision_ref: str,
    root: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create a project instance and its single root in one valid commit."""

    for field, value in {
        "aggregate-id": aggregate_id,
        "tenant-ref": tenant_ref,
        "workspace-ref": workspace_ref,
        "project-ref": project_ref,
        "source-revision": source_revision,
        "event-id": event_id,
        "decision-ref": decision_ref,
    }.items():
        _require(isinstance(value, str) and bool(value.strip()), f"bootstrap-{field}-required")
    project: dict[str, Any] = {
        "schema": PROJECT_STATE_SCHEMA,
        "aggregate_id": aggregate_id,
        "tenant_ref": tenant_ref,
        "workspace_ref": workspace_ref,
        "project_ref": project_ref,
        "source_revision": source_revision,
        "project_status": "ACTIVE",
        "revision": 1,
        "default_context_ref": None,
        "contexts": [],
        "next_sequence": 1,
        "fingerprint": "",
    }
    root_context = _make_context(project, root, event_id, None)
    project["contexts"].append(root_context)
    project["default_context_ref"] = root_context["context_id"]
    project["next_sequence"] = 2
    project = refresh_project_fingerprints(project)
    validate_project_state(project)
    receipt: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "event_id": event_id,
        "decision_ref": decision_ref,
        "project_ref": project_ref,
        "mutated": True,
        "project_revision_before": 0,
        "project_revision_after": 1,
        "session_revision_before": 0,
        "session_revision_after": 0,
        "project_fingerprint_before": None,
        "project_fingerprint_after": project["fingerprint"],
        "session_fingerprint_before": None,
        "session_fingerprint_after": None,
        "project_operations": ["CREATE_PROJECT_INSTANCE", "CREATE_ROOT"],
        "session_operations": [],
        "active_context_ref_after": None,
    }
    return project, _finalize_receipt(receipt)


def bind_control_session(
    project: dict[str, Any],
    *,
    session_binding_id: str,
    principal_ref: str,
    consumer_ref: str,
    session_ref: str,
) -> dict[str, Any]:
    validate_project_state(project)
    session: dict[str, Any] = {
        "schema": SESSION_STATE_SCHEMA,
        "session_binding_id": session_binding_id,
        "tenant_ref": project["tenant_ref"],
        "workspace_ref": project["workspace_ref"],
        "principal_ref": principal_ref,
        "consumer_ref": consumer_ref,
        "session_ref": session_ref,
        "project_ref": project["project_ref"],
        "project_revision": project["revision"],
        "session_revision": 1,
        "active_context_ref": project["default_context_ref"],
        "active_continuation_binding": None,
        "fingerprint": "",
    }
    session = refresh_session_fingerprint(session)
    validate_session_state(session, project)
    return session


def _equivalent_open_context_exists(project: dict[str, Any], candidate: dict[str, Any]) -> bool:
    """Reject duplicate siblings and material no-delta recursion into an ancestor."""

    semantic_identity = (
        candidate["objective_ref"],
        candidate["scope_ref"],
        tuple(candidate["basis_refs"]),
        candidate["project_basis_ref"],
        candidate["quality_trace_ref"],
    )
    parent_ref = candidate["parent_context_ref"]
    lineage = set(ancestor_chain(project, parent_ref))
    for item in project["contexts"]:
        if item.get("lifecycle") != "OPEN":
            continue
        other_identity = (
            item.get("objective_ref"),
            item.get("scope_ref"),
            tuple(sorted(item.get("basis_refs", []))),
            item.get("project_basis_ref"),
            item.get("quality_trace_ref"),
        )
        equivalent_sibling = item.get("parent_context_ref") == parent_ref and other_identity == semantic_identity
        equivalent_ancestor = item.get("context_id") in lineage and other_identity == semantic_identity
        if equivalent_sibling or equivalent_ancestor:
            return True
    return False


def _touch_context(context: dict[str, Any], event_id: str) -> None:
    context["last_transition_ref"] = event_id
    context["context_fingerprint"] = context_fingerprint(context)


def _apply_project_operation(project: dict[str, Any], operation: dict[str, Any], event_id: str) -> None:
    name = operation.get("operation")
    _require(name in PROJECT_OPERATIONS, f"unknown-project-operation:{name}")
    mapping = _context_map(project)

    if name == "CREATE_CHILD":
        parent_ref = operation.get("parent_context_ref")
        _require(isinstance(parent_ref, str) and parent_ref in mapping, "child-parent-not-found")
        _require(mapping[parent_ref].get("lifecycle") == "OPEN", "child-parent-must-be-open")
        context = _make_context(project, operation, event_id, parent_ref)
        _require(context["context_id"] not in mapping, "duplicate-context-id")
        _require(not _equivalent_open_context_exists(project, context), "equivalent-open-context-without-material-delta")
        project["contexts"].append(context)
        project["next_sequence"] += 1
        return

    target = operation.get("context_ref")
    if name != "CREATE_DERIVED_CONTEXT":
        _require(isinstance(target, str) and target in mapping, f"{name.lower()}-target-not-found")

    if name == "SET_CONTROL_CONDITION":
        condition = operation.get("control_condition")
        _require(mapping[target].get("lifecycle") == "OPEN", "condition-target-must-be-open")
        _require(condition in CONTROL_CONDITIONS - {None}, "control-condition-invalid")
        _require(mapping[target].get("control_condition") != condition, "control-condition-no-delta")
        mapping[target]["control_condition"] = condition
        _touch_context(mapping[target], event_id)
        return

    if name == "RETURN_CONTEXT":
        context = mapping[target]
        _require(context.get("lifecycle") == "OPEN", "return-target-must-be-open")
        _require(context.get("parent_context_ref") is not None, "root-context-cannot-return")
        unresolved = [
            mapping[item_id]
            for item_id in descendant_ids(project, target)
            if mapping[item_id].get("lifecycle") == "OPEN"
            or (mapping[item_id].get("lifecycle") == "RETURNED" and mapping[item_id].get("disposition") == "PENDING_JOIN")
        ]
        _require(not unresolved, "return-requires-clean-descendant-state")
        result_ref = operation.get("result_ref")
        _require(isinstance(result_ref, str) and bool(result_ref.strip()), "return-result-ref-required")
        evidence_refs = _normalized_refs(operation.get("evidence_refs"), "return-evidence-refs")
        unresolved_refs = _normalized_refs(operation.get("unresolved_refs"), "return-unresolved-refs")
        completion_proof_ref = operation.get("completion_proof_ref")
        _require(
            isinstance(completion_proof_ref, str) and bool(completion_proof_ref.strip()),
            "return-completion-proof-ref-required",
        )
        _require(
            operation.get("child_basis_fingerprint") == context.get("basis_fingerprint"),
            "return-child-basis-fingerprint-stale",
        )
        _require(
            operation.get("completion_criteria_satisfied") is True,
            "return-completion-criteria-proof-required",
        )
        unresolved_disposition_ref = operation.get("unresolved_disposition_ref")
        _require(
            not unresolved_refs
            or (isinstance(unresolved_disposition_ref, str) and bool(unresolved_disposition_ref.strip())),
            "return-unresolved-disposition-ref-required",
        )
        _require(isinstance(evidence_refs, list), "return-evidence-refs-required")
        return_target = operation.get("return_target_ref") or context["parent_context_ref"]
        _require(return_target in mapping and mapping[return_target].get("lifecycle") == "OPEN", "return-target-context-must-be-open")
        context.update(
            lifecycle="RETURNED",
            control_condition=None,
            disposition="PENDING_JOIN",
            result_ref=result_ref,
            return_target_ref=return_target,
        )
        _touch_context(context, event_id)
        return

    if name == "APPLY_JOIN_DISPOSITION":
        context = mapping[target]
        disposition = operation.get("disposition")
        _require(context.get("lifecycle") == "RETURNED", "join-disposition-target-must-be-returned")
        _require(context.get("disposition") == "PENDING_JOIN", "join-disposition-target-must-be-pending")
        _require(disposition in CLOSED_DISPOSITIONS, "join-disposition-invalid")
        _require(operation.get("dependent_owner_state_current") is True, "join-dependent-owner-state-must-be-current")
        owner_receipt_ref = operation.get("owner_effect_receipt_ref")
        _require(
            isinstance(owner_receipt_ref, str) and bool(owner_receipt_ref.strip()),
            "join-owner-effect-receipt-ref-required",
        )
        context["disposition"] = disposition
        _touch_context(context, event_id)
        return

    if name == "CLOSE_CONTEXT":
        _require(mapping[target].get("lifecycle") == "RETURNED", "close-target-must-be-returned")
        disposition = mapping[target].get("disposition")
        if disposition == "PENDING_JOIN" and operation.get("compatibility_direct_disposition") is True:
            disposition = operation.get("disposition")
            _require(disposition in CLOSED_DISPOSITIONS, "close-disposition-invalid")
        _require(disposition in CLOSED_DISPOSITIONS, "close-requires-applied-join-disposition")
        mapping[target].update(lifecycle="CLOSED", disposition=disposition, control_condition=None)
        _touch_context(mapping[target], event_id)
        return

    if name == "CANCEL_CONTEXT":
        context = mapping[target]
        _require(context.get("lifecycle") == "OPEN", "cancel-target-must-be-open")
        _require(context.get("parent_context_ref") is not None, "root-context-cannot-cancel")
        unresolved = [
            mapping[item_id]
            for item_id in descendant_ids(project, target)
            if mapping[item_id].get("lifecycle") == "OPEN"
            or (mapping[item_id].get("lifecycle") == "RETURNED" and mapping[item_id].get("disposition") == "PENDING_JOIN")
        ]
        _require(not unresolved, "cancel-requires-clean-descendant-state")
        context.update(lifecycle="CANCELLED", control_condition=None, disposition=None)
        _touch_context(context, event_id)
        return

    if name == "CREATE_DERIVED_CONTEXT":
        source_ref = operation.get("derived_from_context_ref")
        _require(isinstance(source_ref, str) and source_ref in mapping, "derived-source-not-found")
        _require(mapping[source_ref].get("lifecycle") != "OPEN", "derived-source-must-be-historical")
        parent_ref = operation.get("parent_context_ref", mapping[source_ref].get("parent_context_ref"))
        _require(parent_ref is not None, "historical-root-reactivation-requires-new-project-instance")
        _require(parent_ref in mapping and mapping[parent_ref].get("lifecycle") == "OPEN", "derived-parent-must-be-open")
        context = _make_context(project, operation, event_id, parent_ref, source_ref)
        _require(context["context_id"] not in mapping, "duplicate-context-id")
        project["contexts"].append(context)
        project["next_sequence"] += 1
        return

    if name == "REFRESH_GOVERNING_REFS":
        context = mapping[target]
        _require(context.get("lifecycle") == "OPEN", "refresh-target-must-be-open")
        changed = False
        for field in ("project_basis_ref", "quality_trace_ref"):
            if field in operation and context.get(field) != operation.get(field):
                context[field] = operation.get(field)
                changed = True
        if "basis_refs" in operation:
            refs = _normalized_refs(operation["basis_refs"], "basis-refs")
            if refs != context.get("basis_refs"):
                context["basis_refs"] = refs
                changed = True
        _require(changed, "refresh-governing-refs-no-delta")
        context["basis_fingerprint"] = operation.get("basis_fingerprint") or _basis_fingerprint(context)
        _touch_context(context, event_id)
        return

    if name == "SET_DEFAULT_CONTEXT":
        _require(mapping[target].get("lifecycle") == "OPEN", "default-target-must-be-open")
        _require(project.get("default_context_ref") != target, "default-context-no-delta")
        project["default_context_ref"] = target
        return

    raise ControlContextError(f"unhandled-project-operation:{name}")


def _apply_session_operation(session: dict[str, Any], project: dict[str, Any], operation: dict[str, Any]) -> None:
    name = operation.get("operation")
    _require(name in SESSION_OPERATIONS, f"unknown-session-operation:{name}")
    mapping = _context_map(project)
    if name == "SET_ACTIVE":
        target = operation.get("context_ref")
        _require(isinstance(target, str) and target in mapping, "active-target-not-found")
        _require(mapping[target].get("lifecycle") == "OPEN", "active-target-must-be-open")
        _require(session.get("active_context_ref") != target, "set-active-no-delta")
        session["active_context_ref"] = target
        return
    if name == "SET_CONTINUATION_BINDING":
        value = operation.get("binding")
        _require(isinstance(value, dict), "continuation-binding-required")
        prior = session.get("active_continuation_binding")
        binding = copy.deepcopy(value)
        binding["schema"] = BINDING_SCHEMA
        if isinstance(prior, dict) and prior.get("binding_id") == binding.get("binding_id"):
            binding["binding_revision"] = prior["binding_revision"] + 1
        else:
            binding["binding_revision"] = 1
        session["active_continuation_binding"] = binding
        return
    if name == "CLEAR_CONTINUATION_BINDING":
        _require(session.get("active_continuation_binding") is not None, "continuation-binding-already-clear")
        session["active_continuation_binding"] = None
        return
    raise ControlContextError(f"unhandled-session-operation:{name}")


def _rebase_binding(session: dict[str, Any], project: dict[str, Any], prior: dict[str, Any] | None) -> None:
    binding = session.get("active_continuation_binding")
    if not isinstance(binding, dict):
        return
    if isinstance(prior, dict) and binding == prior:
        binding["binding_revision"] = prior["binding_revision"] + 1
    binding["basis_project_revision"] = project["revision"]
    binding["basis_session_revision"] = session["session_revision"]
    binding["binding_fingerprint"] = continuation_fingerprint(binding)


def apply_transition(
    project: dict[str, Any],
    session: dict[str, Any],
    directive: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Return validated project/session candidates and one atomic receipt."""

    validate_session_state(session, project)
    _require(directive.get("schema") == DIRECTIVE_SCHEMA, "transition-directive-schema-mismatch")
    for field in ("event_id", "decision_ref"):
        _require(isinstance(directive.get(field), str) and bool(directive[field].strip()), f"transition-{field}-required")
    _require(directive.get("expected_project_revision") == project["revision"], "project-revision-conflict")
    _require(directive.get("expected_project_fingerprint") == project["fingerprint"], "project-fingerprint-conflict")
    _require(directive.get("expected_session_revision") == session["session_revision"], "session-revision-conflict")
    _require(directive.get("expected_session_fingerprint") == session["fingerprint"], "session-fingerprint-conflict")
    project_ops = directive.get("project_operations")
    session_ops = directive.get("session_operations")
    _require(isinstance(project_ops, list) and all(isinstance(item, dict) for item in project_ops), "project-operations-array-required")
    _require(isinstance(session_ops, list) and all(isinstance(item, dict) for item in session_ops), "session-operations-array-required")

    before_project = copy.deepcopy(project)
    before_session = copy.deepcopy(session)
    if not project_ops and not session_ops:
        receipt: dict[str, Any] = {
            "schema": RECEIPT_SCHEMA,
            "event_id": directive["event_id"],
            "decision_ref": directive["decision_ref"],
            "project_ref": project["project_ref"],
            "mutated": False,
            "project_revision_before": project["revision"],
            "project_revision_after": project["revision"],
            "session_revision_before": session["session_revision"],
            "session_revision_after": session["session_revision"],
            "project_fingerprint_before": project["fingerprint"],
            "project_fingerprint_after": project["fingerprint"],
            "session_fingerprint_before": session["fingerprint"],
            "session_fingerprint_after": session["fingerprint"],
            "project_operations": [],
            "session_operations": [],
            "active_context_ref_after": session.get("active_context_ref"),
        }
        return copy.deepcopy(project), copy.deepcopy(session), _finalize_receipt(receipt)

    event_id = directive["event_id"]
    candidate_project = copy.deepcopy(project)
    candidate_session = copy.deepcopy(session)
    prior_binding = copy.deepcopy(candidate_session.get("active_continuation_binding"))
    for operation in project_ops:
        _apply_project_operation(candidate_project, operation, event_id)
    if project_ops:
        candidate_project["revision"] += 1
        candidate_project = refresh_project_fingerprints(candidate_project)
        validate_project_state(candidate_project)

    for operation in session_ops:
        _apply_session_operation(candidate_session, candidate_project, operation)
    candidate_session["project_revision"] = candidate_project["revision"]
    candidate_session["session_revision"] += 1
    _rebase_binding(candidate_session, candidate_project, prior_binding)
    candidate_session = refresh_session_fingerprint(candidate_session)
    validate_session_state(candidate_session, candidate_project)

    _require(candidate_project != before_project or candidate_session != before_session, "transition-produced-no-delta")
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "event_id": event_id,
        "decision_ref": directive["decision_ref"],
        "project_ref": candidate_project["project_ref"],
        "mutated": True,
        "project_revision_before": before_project["revision"],
        "project_revision_after": candidate_project["revision"],
        "session_revision_before": before_session["session_revision"],
        "session_revision_after": candidate_session["session_revision"],
        "project_fingerprint_before": before_project["fingerprint"],
        "project_fingerprint_after": candidate_project["fingerprint"],
        "session_fingerprint_before": before_session["fingerprint"],
        "session_fingerprint_after": candidate_session["fingerprint"],
        "project_operations": [item["operation"] for item in project_ops],
        "session_operations": [item["operation"] for item in session_ops],
        "active_context_ref_after": candidate_session.get("active_context_ref"),
    }
    return candidate_project, candidate_session, _finalize_receipt(receipt)
