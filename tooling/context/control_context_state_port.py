#!/usr/bin/env python3
"""Provider-neutral state-port contract plus an in-memory verification adapter.

The in-memory adapter is a deterministic test implementation. It is not a durable
or universal runtime backend and must not be used for an operational activation
claim.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
from typing import Any

try:
    from .control_context_registry import (
        DIRECTIVE_SCHEMA,
        ControlContextError,
        ancestor_chain,
        apply_transition,
        bind_control_session,
        bootstrap_project_state,
        continuation_fingerprint,
        validate_actor_generation_shadow,
        validate_work_claim_shadow,
        refresh_session_fingerprint,
        validate_project_state,
        validate_session_state,
    )
except ImportError:
    from control_context_registry import (
        DIRECTIVE_SCHEMA,
        ControlContextError,
        ancestor_chain,
        apply_transition,
        bind_control_session,
        bootstrap_project_state,
        continuation_fingerprint,
        validate_actor_generation_shadow,
        validate_work_claim_shadow,
        refresh_session_fingerprint,
        validate_project_state,
        validate_session_state,
    )


BEGIN_SCHEMA = "cerebro-control-context-event-begin/v1"
BEGIN_RESULT_SCHEMA = "cerebro-control-context-event-binding/v1"
COMPLETE_RESULT_SCHEMA = "cerebro-control-context-event-completion/v1"


class StatePortError(RuntimeError):
    pass


class StateServiceUnavailable(StatePortError):
    pass


class StateAuthorizationError(StatePortError):
    pass


class StateConflict(StatePortError):
    pass


class StateBindingError(StatePortError):
    pass


def _require(condition: bool, message: str, error=StatePortError) -> None:
    if not condition:
        raise error(message)


def _sha256(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def nearest_open_ancestor(project: dict[str, Any], context_ref: str) -> str | None:
    """Resolve the nearest legal session focus after a shared-tree revision."""

    mapping = {item["context_id"]: item for item in project["contexts"]}
    if context_ref not in mapping:
        return None
    for candidate in ancestor_chain(project, context_ref):
        if mapping[candidate].get("lifecycle") == "OPEN":
            return candidate
    default_ref = project.get("default_context_ref")
    if isinstance(default_ref, str) and mapping.get(default_ref, {}).get("lifecycle") == "OPEN":
        return default_ref
    return None


def rehydrate_control_session(session: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
    """Rebase one session onto current shared project state without moving others."""

    if session.get("project_revision") == project.get("revision"):
        validate_session_state(session, project)
        return session
    target = nearest_open_ancestor(project, str(session.get("active_context_ref") or ""))
    _require(target is not None, "control-session-rehydration-failed", StateBindingError)
    prior_target = session.get("active_context_ref")
    session["project_revision"] = project["revision"]
    session["session_revision"] += 1
    session["active_context_ref"] = target
    binding = session.get("active_continuation_binding")
    if isinstance(binding, dict) and binding.get("context_ref") == target and prior_target == target:
        binding["basis_project_revision"] = project["revision"]
        binding["basis_session_revision"] = session["session_revision"]
        binding["binding_revision"] += 1
        binding["binding_fingerprint"] = continuation_fingerprint(binding)
    else:
        session["active_continuation_binding"] = None
    session = refresh_session_fingerprint(session)
    validate_session_state(session, project)
    return session


def validate_context_owner_candidate_binding(
    candidate: dict[str, Any],
    *,
    directive: dict[str, Any],
    project_before: dict[str, Any],
    project_after: dict[str, Any],
    transition_receipt: dict[str, Any],
) -> dict[str, Any]:
    """Fail before persistence unless a Context candidate predicts this transition."""

    _require(isinstance(candidate, dict), "context-owner-candidate-object-required")
    _require(candidate.get("schema") == "cerebro-owner-effect-receipt/v1", "context-owner-candidate-schema-mismatch")
    _require(candidate.get("owner") == "context" and candidate.get("producer_ref") == "context", "context-owner-candidate-owner-mismatch")
    _require(candidate.get("effect") == "REFRESH_GOVERNING_REFS", "context-owner-candidate-effect-mismatch")
    _require(candidate.get("result") == "CANDIDATE" and candidate.get("current") is False, "context-owner-candidate-precommit-required")
    _require(candidate.get("persistence_evidence_ref") is None, "context-owner-candidate-cannot-have-persistence-evidence")
    subject = copy.deepcopy(candidate)
    subject.pop("receipt_ref", None)
    subject.pop("receipt_fingerprint", None)
    fingerprint = _sha256(subject)
    _require(candidate.get("receipt_fingerprint") == fingerprint, "context-owner-candidate-fingerprint-mismatch")
    _require(candidate.get("receipt_ref") == "OER-" + fingerprint[:24].upper(), "context-owner-candidate-ref-mismatch")
    _require(candidate.get("control_decision_ref") == directive.get("decision_ref"), "context-owner-candidate-decision-mismatch")
    _require(candidate.get("input_state_ref") == project_before.get("project_ref"), "context-owner-candidate-input-ref-mismatch")
    _require(candidate.get("output_state_ref") == project_after.get("project_ref"), "context-owner-candidate-output-ref-mismatch")
    _require(candidate.get("input_state_fingerprint") == project_before.get("fingerprint"), "context-owner-candidate-input-fingerprint-mismatch")
    _require(candidate.get("output_state_fingerprint") == project_after.get("fingerprint"), "context-owner-candidate-output-fingerprint-mismatch")
    operations = directive.get("project_operations")
    _require(isinstance(operations, list) and bool(operations), "context-owner-refresh-operations-required")
    affected: list[str] = []
    for operation in operations:
        _require(
            isinstance(operation, dict) and operation.get("operation") == "REFRESH_GOVERNING_REFS",
            "context-owner-candidate-allows-only-refresh-operations",
        )
        context_ref = operation.get("context_ref")
        _require(isinstance(context_ref, str) and bool(context_ref), "context-owner-refresh-context-ref-required")
        affected.append(context_ref)
    _require(len(affected) == len(set(affected)), "context-owner-refresh-context-ref-duplicate")
    _require(directive.get("session_operations") == [], "context-owner-refresh-cannot-change-session")
    _require(candidate.get("affected_refs") == sorted(affected), "context-owner-candidate-affected-refs-mismatch")
    evidence = candidate.get("evidence_refs")
    _require(isinstance(evidence, list) and transition_receipt.get("receipt_id") in evidence, "context-owner-candidate-transition-evidence-required")
    _require(candidate.get("state_mutated") is True and transition_receipt.get("mutated") is True, "context-owner-candidate-material-mutation-required")
    _require(candidate.get("unaffected_state_preserved") is True, "context-owner-candidate-unaffected-state-preservation-required")
    return {
        "result": "PASS",
        "candidate_ref": candidate["receipt_ref"],
        "candidate_fingerprint": candidate["receipt_fingerprint"],
        "affected_refs": sorted(affected),
    }


HUMAN_T3_IDENTITY_FIELDS = ("tenant_ref", "workspace_ref", "principal_ref", "consumer_ref", "session_ref")


def validate_human_t3_record(record: dict[str, Any]) -> None:
    """Custody integrity only; this port does not decide Human admission."""
    _require(isinstance(record, dict), "T3-record-object-required", StateBindingError)
    fields = {"schema", "identity", "override_id", "candidate", "human_ref", "arm_event_id", "arm_turn_id", "revision", "state", "authority", "current", "override_consumed", "confirm_event_id", "effect_attempt_id", "effect_truth", "context_override_record", "invalidation_reason", "fingerprint"}
    _require(set(record) == fields, "T3-exact-record-fields-required", StateBindingError)
    subject = copy.deepcopy(record)
    supplied = subject.pop("fingerprint", None)
    _require(supplied == _sha256(subject), "T3-record-fingerprint-mismatch", StateBindingError)
    _require(record.get("schema") == "cerebro-human-t3-break-glass/v1", "T3-schema-mismatch", StateBindingError)
    identity = record.get("identity")
    _require(isinstance(identity, dict) and set(identity) == set(HUMAN_T3_IDENTITY_FIELDS)
             and all(isinstance(v, str) and bool(v.strip()) for v in identity.values()), "T3-identity-invalid", StateBindingError)
    _require(record.get("current") is False and record.get("authority") == "NONAUTHORIZING_CUSTODY", "T3-custody-cannot-authorize", StateBindingError)
    _require(type(record.get("revision")) is int and record["revision"] >= 1, "T3-revision-invalid", StateBindingError)
    _require(isinstance(record.get("override_id"), str) and bool(record["override_id"].strip()), "T3-override-id-invalid", StateBindingError)
    for key in ("human_ref", "arm_event_id", "arm_turn_id"):
        _require(isinstance(record[key], str) and bool(record[key].strip()), "T3-Human-binding-invalid", StateBindingError)
    candidate = record.get("candidate")
    candidate_fields = {"candidate_id", "candidate_digest", "generation_id", "target_binding", "material_fingerprint", "supersession_ref", "project_revision", "session_revision", "frontier_revision", "normal_admission", "override_dimensions"}
    _require(isinstance(candidate, dict) and set(candidate) == candidate_fields, "T3-candidate-shape-invalid", StateBindingError)
    _require(all(isinstance(candidate[k], str) and bool(candidate[k].strip()) for k in candidate_fields - {"override_dimensions"}), "T3-candidate-field-invalid", StateBindingError)
    for key in ("candidate_digest", "material_fingerprint"):
        _require(len(candidate[key]) == 64 and all(c in "0123456789abcdef" for c in candidate[key]), "T3-candidate-fingerprint-invalid", StateBindingError)
    dims = candidate["override_dimensions"]
    _require(isinstance(dims, list) and bool(dims) and all(isinstance(d, str) for d in dims)
             and len(dims) == len(set(dims)) and set(dims) <= {"AUTHORITY", "FENCE", "CURRENTNESS"}, "T3-candidate-dimensions-invalid", StateBindingError)
    _require(candidate["normal_admission"] in {"BLOCK", "UNKNOWN"}, "T3-candidate-admission-shape-invalid", StateBindingError)
    states = {"ARMED_PENDING_CONFIRM", "INVALIDATED", "OVERRIDE_CONSUMED", "CONTEXT_OVERRIDE_COMMITTED", "EFFECT_SUCCESS", "EFFECT_NO_EFFECT", "EFFECT_UNKNOWN"}
    _require(record.get("state") in states, "T3-state-invalid", StateBindingError)
    consumed = record["state"] not in {"ARMED_PENDING_CONFIRM", "INVALIDATED"}
    _require(record.get("override_consumed") is consumed, "T3-consumption-state-mismatch", StateBindingError)
    _require((record.get("effect_attempt_id") is not None) is consumed
             and (record.get("confirm_event_id") is not None) is consumed, "T3-attempt-binding-mismatch", StateBindingError)
    if consumed:
        _require(all(isinstance(record[k], str) and bool(record[k].strip()) for k in ("confirm_event_id", "effect_attempt_id")), "T3-consumed-binding-invalid", StateBindingError)
        _require(record["confirm_event_id"] != record["arm_event_id"], "T3-distinct-event-required", StateBindingError)
    _require((record["invalidation_reason"] is not None) is (record["state"] == "INVALIDATED"), "T3-invalidation-reason-mismatch", StateBindingError)
    context = record["context_override_record"]
    needs_context = record["state"] in {"CONTEXT_OVERRIDE_COMMITTED", "EFFECT_SUCCESS", "EFFECT_NO_EFFECT", "EFFECT_UNKNOWN"}
    _require((context is not None) is needs_context, "T3-Context-evidence-state-mismatch", StateBindingError)
    if context is not None:
        _require(isinstance(context, dict) and set(context) == {"record_type", "authority", "mcp_decision", "override_id", "effect_attempt_id", "candidate_fingerprint", "state_service_receipt", "readback_verified"}, "T3-Context-evidence-shape-invalid", StateBindingError)
        _require(context["record_type"] == "OVERRIDE" and context["authority"] == "EVIDENCE_ONLY"
                 and context["mcp_decision"] == "CONFIRM_ADMITTED" and context["readback_verified"] is True,
                 "T3-Context-evidence-cannot-authorize", StateBindingError)
        _require(context["override_id"] == record["override_id"] and context["effect_attempt_id"] == record["effect_attempt_id"]
                 and context["candidate_fingerprint"] == _sha256(candidate), "T3-Context-evidence-binding-mismatch", StateBindingError)
        receipt = context["state_service_receipt"]
        _require(isinstance(receipt, dict), "T3-Context-custody-receipt-required", StateBindingError)
        subject = copy.deepcopy(receipt)
        fp = subject.pop("fingerprint", None)
        _require(fp == _sha256(subject) and receipt.get("authority") == "CUSTODY_ONLY"
                 and receipt.get("schema") == "cerebro-human-t3-custody-receipt/v1", "T3-Context-custody-receipt-invalid", StateBindingError)
    _require(record.get("effect_truth") == (record["state"] if record["state"].startswith("EFFECT_") else "EFFECT_UNKNOWN" if consumed else "NOT_ATTEMPTED"), "T3-effect-truth-mismatch", StateBindingError)


def validate_human_t3_custody_write(record: dict[str, Any], prior: dict[str, Any] | None, expected_revision: int) -> dict[str, Any]:
    validate_human_t3_record(record)
    _require(type(expected_revision) is int and expected_revision >= 0, "T3-expected-revision-invalid", StateBindingError)
    _require((prior["revision"] if prior else 0) == expected_revision, "T3-custody-CAS-conflict", StateConflict)
    _require(record["revision"] == expected_revision + 1, "T3-revision-sequence-invalid", StateBindingError)
    if prior is None:
        _require(record["state"] == "ARMED_PENDING_CONFIRM", "T3-first-revision-must-be-ARM", StateBindingError)
    else:
        validate_human_t3_record(prior)
        for key in ("schema", "identity", "override_id", "candidate", "human_ref", "arm_event_id", "arm_turn_id", "authority", "current"):
            _require(record.get(key) == prior.get(key), "T3-immutable-binding-changed:" + key, StateBindingError)
        next_states = {"ARMED_PENDING_CONFIRM": {"INVALIDATED", "OVERRIDE_CONSUMED"},
                       "OVERRIDE_CONSUMED": {"CONTEXT_OVERRIDE_COMMITTED"},
                       "CONTEXT_OVERRIDE_COMMITTED": {"EFFECT_SUCCESS", "EFFECT_NO_EFFECT", "EFFECT_UNKNOWN"}}
        _require(record["state"] in next_states.get(prior["state"], set()), "T3-terminal-or-consumed-no-retry", StateConflict)
        if prior["override_consumed"]:
            for key in ("confirm_event_id", "effect_attempt_id"):
                _require(record[key] == prior[key], "T3-consumed-binding-changed", StateBindingError)
        if prior.get("context_override_record") is not None:
            _require(record.get("context_override_record") == prior["context_override_record"], "T3-Context-evidence-immutable", StateBindingError)
        if record["state"] == "CONTEXT_OVERRIDE_COMMITTED":
            embedded = record["context_override_record"]["state_service_receipt"]
            _require(embedded.get("record_fingerprint") == prior["fingerprint"] and embedded.get("revision") == prior["revision"]
                     and embedded.get("override_id") == prior["override_id"], "T3-Context-consumption-receipt-binding-mismatch", StateBindingError)
    receipt = {"schema": "cerebro-human-t3-custody-receipt/v1", "authority": "CUSTODY_ONLY",
               "override_id": record["override_id"], "revision": record["revision"],
               "previous_revision": expected_revision, "record_fingerprint": record["fingerprint"]}
    receipt["fingerprint"] = _sha256(receipt)
    return receipt


SUCCESSION_IDENTITY_FIELDS = ("tenant_ref", "workspace_ref", "principal_ref")
SUCCESSION_RECORD_SCHEMA = "cerebro-principal-succession-custody-record/v1"


def validate_principal_succession_custody_record(record: dict[str, Any]) -> None:
    """Integrity/custody only; full permit semantics remain MCP-owned."""
    _require(isinstance(record, dict) and set(record) == {
        "schema", "identity", "revision", "request_ref", "permit", "fingerprint",
    }, "succession-custody-record-fields", StateBindingError)
    _require(record["schema"] == SUCCESSION_RECORD_SCHEMA, "succession-custody-schema", StateBindingError)
    identity = record["identity"]
    _require(isinstance(identity, dict) and set(identity) == set(SUCCESSION_IDENTITY_FIELDS),
             "succession-custody-identity", StateBindingError)
    for value in [*identity.values(), record["request_ref"]]:
        _require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", value) is not None,
                 "succession-custody-opaque-binding", StateBindingError)
    _require(type(record["revision"]) is int and record["revision"] >= 1,
             "succession-custody-revision", StateBindingError)
    permit = record["permit"]
    _require(isinstance(permit, dict), "succession-custody-permit-object", StateBindingError)
    subject = copy.deepcopy(permit)
    fingerprint = subject.pop("permit_fingerprint", None)
    _require(fingerprint == _sha256(subject), "succession-custody-permit-fingerprint", StateBindingError)
    _require(permit.get("schema") == "cerebro-principal-succession-permit/v1",
             "succession-custody-permit-schema", StateBindingError)
    _require(isinstance(permit.get("permit_id"), str) and
             re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,255}", permit["permit_id"]) is not None and
             not any(token in permit["permit_id"].lower() for token in ("diary", "stambok")),
             "succession-custody-permit-ref", StateBindingError)
    subject = copy.deepcopy(record)
    supplied = subject.pop("fingerprint")
    _require(supplied == _sha256(subject), "succession-custody-record-fingerprint", StateBindingError)


def principal_succession_custody_receipt(record: dict[str, Any]) -> dict[str, Any]:
    validate_principal_succession_custody_record(record)
    receipt = {
        "schema": "cerebro-principal-succession-custody-receipt/v1",
        "authority": "CUSTODY_ONLY", "identity": copy.deepcopy(record["identity"]),
        "revision": record["revision"], "request_ref": record["request_ref"],
        "record_fingerprint": record["fingerprint"], "permit_ref": record["permit"]["permit_id"],
        "permit_fingerprint": record["permit"]["permit_fingerprint"],
    }
    receipt["fingerprint"] = _sha256(receipt)
    return receipt


def validate_principal_succession_custody_write(record, prior, expected_revision):
    validate_principal_succession_custody_record(record)
    _require(type(expected_revision) is int and expected_revision >= 0,
             "succession-custody-expected-revision", StateBindingError)
    if prior is not None:
        validate_principal_succession_custody_record(prior)
        _require(prior["identity"] == record["identity"], "succession-custody-scope", StateBindingError)
        for field in ("provider_revision", "covered_through_frontier"):
            _require(type(record["permit"].get(field)) is int and
                     type(prior["permit"].get(field)) is int and
                     record["permit"][field] >= prior["permit"][field],
                     "succession-custody-currentness-regression", StateConflict)
    _require((prior["revision"] if prior else 0) == expected_revision,
             "succession-custody-CAS-conflict", StateConflict)
    _require(record["revision"] == expected_revision + 1,
             "succession-custody-revision-sequence", StateBindingError)
    return principal_succession_custody_receipt(record)


class InMemoryControlContextStatePort:
    """Thread-safe test adapter implementing session-scoped focus and CAS."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._available = True
        self._projects: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._sessions: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        self._active_projects: dict[tuple[str, str, str], str] = {}
        self._events: dict[tuple[str, str, str, str, str, str], dict[str, Any]] = {}
        self._idempotency: dict[tuple[str, str, str, str, str, str], str] = {}
        self._bootstraps: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        self._actor_generation_shadows: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self._work_claim_shadows: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._human_t3_arms: dict[tuple[str, ...], dict[str, Any]] = {}
        self._human_t3_revisions: dict[tuple[str, ...], list[dict[str, Any]]] = {}
        self._human_t3_receipts: dict[tuple[str, ...], list[dict[str, Any]]] = {}

    def read_human_t3_arm(self, *, override_id: str, scopes: set[str], **identity: str) -> dict[str, Any] | None:
        with self._lock:
            self._require_available()
            self._require_scope(scopes, "project_state:transition")
            _require(set(identity) == set(HUMAN_T3_IDENTITY_FIELDS), "T3-exact-identity-required", StateBindingError)
            key = tuple(identity[k] for k in HUMAN_T3_IDENTITY_FIELDS) + (override_id,)
            record = self._human_t3_arms.get(key)
            if record is not None:
                validate_human_t3_record(record)
            return copy.deepcopy(record)

    def commit_human_t3_arm(self, *, record: dict[str, Any], expected_revision: int, scopes: set[str]) -> dict[str, Any]:
        with self._lock:
            self._require_available()
            self._require_scope(scopes, "project_state:transition")
            validate_human_t3_record(record)
            key = tuple(record["identity"][k] for k in HUMAN_T3_IDENTITY_FIELDS) + (record["override_id"],)
            receipt = validate_human_t3_custody_write(record, self._human_t3_arms.get(key), expected_revision)
            self._human_t3_arms[key] = copy.deepcopy(record)
            self._human_t3_revisions.setdefault(key, []).append(copy.deepcopy(record))
            self._human_t3_receipts.setdefault(key, []).append(copy.deepcopy(receipt))
            return copy.deepcopy(receipt)

    def set_available(self, available: bool) -> None:
        with self._lock:
            self._available = bool(available)

    def _require_available(self) -> None:
        _require(self._available, "control-context-state-service-unavailable", StateServiceUnavailable)

    @staticmethod
    def _require_scope(scopes: set[str], required: str) -> None:
        _require(required in scopes, f"required-scope-missing:{required}", StateAuthorizationError)

    @staticmethod
    def _project_key(tenant_ref: str, workspace_ref: str, project_ref: str) -> tuple[str, str, str]:
        return tenant_ref, workspace_ref, project_ref

    @staticmethod
    def _session_key(
        tenant_ref: str,
        workspace_ref: str,
        principal_ref: str,
        consumer_ref: str,
        session_ref: str,
    ) -> tuple[str, str, str, str, str]:
        return tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref

    def bootstrap_project(
        self,
        *,
        tenant_ref: str,
        workspace_ref: str,
        principal_ref: str,
        project_ref: str,
        aggregate_id: str,
        source_revision: str,
        event_id: str,
        decision_ref: str,
        root: dict[str, Any],
        scopes: set[str],
        make_default: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            self._require_available()
            self._require_scope(scopes, "project_state:transition")
            key = self._project_key(tenant_ref, workspace_ref, project_ref)
            bootstrap_key = (tenant_ref, workspace_ref, principal_ref, project_ref, event_id)
            request_subject = {
                "operation": "BOOTSTRAP_PROJECT",
                "tenant_ref": tenant_ref,
                "workspace_ref": workspace_ref,
                "principal_ref": principal_ref,
                "project_ref": project_ref,
                "aggregate_id": aggregate_id,
                "source_revision": source_revision,
                "event_id": event_id,
                "decision_ref": decision_ref,
                "root": copy.deepcopy(root),
                "make_default": make_default,
            }
            if bootstrap_key in self._bootstraps:
                existing = self._bootstraps[bootstrap_key]
                _require(
                    existing["request_fingerprint"] == _sha256(request_subject),
                    "project-bootstrap-replayed-with-different-request",
                    StateConflict,
                )
                return copy.deepcopy(existing["result"])
            _require(key not in self._projects, "project-instance-already-exists", StateConflict)
            project, receipt = bootstrap_project_state(
                aggregate_id=aggregate_id,
                tenant_ref=tenant_ref,
                workspace_ref=workspace_ref,
                project_ref=project_ref,
                source_revision=source_revision,
                event_id=event_id,
                decision_ref=decision_ref,
                root=root,
            )
            self._projects[key] = copy.deepcopy(project)
            if make_default:
                self._active_projects[(tenant_ref, workspace_ref, principal_ref)] = project_ref
            result = {"project": copy.deepcopy(project), "receipt": copy.deepcopy(receipt)}
            self._bootstraps[bootstrap_key] = {
                "request_fingerprint": _sha256(request_subject),
                "result": copy.deepcopy(result),
            }
            return result

    def set_default_project(
        self,
        *,
        tenant_ref: str,
        workspace_ref: str,
        principal_ref: str,
        project_ref: str,
        scopes: set[str],
    ) -> None:
        with self._lock:
            self._require_available()
            self._require_scope(scopes, "project_state:transition")
            _require(self._project_key(tenant_ref, workspace_ref, project_ref) in self._projects, "project-instance-not-found", StateBindingError)
            self._active_projects[(tenant_ref, workspace_ref, principal_ref)] = project_ref

    def bind_session(
        self,
        *,
        tenant_ref: str,
        workspace_ref: str,
        principal_ref: str,
        consumer_ref: str,
        session_ref: str,
        session_binding_id: str,
        scopes: set[str],
        project_ref: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._require_available()
            self._require_scope(scopes, "project_state:transition")
            session_key = self._session_key(tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref)
            if session_key in self._sessions:
                if project_ref is not None:
                    _require(
                        self._sessions[session_key].get("project_ref") == project_ref,
                        "control-session-already-bound-to-different-project",
                        StateBindingError,
                    )
                return copy.deepcopy(self._sessions[session_key])
            resolved_project = project_ref or self._active_projects.get((tenant_ref, workspace_ref, principal_ref))
            _require(isinstance(resolved_project, str) and bool(resolved_project), "active-project-binding-required", StateBindingError)
            project_key = self._project_key(tenant_ref, workspace_ref, resolved_project)
            _require(project_key in self._projects, "project-instance-not-found", StateBindingError)
            session = bind_control_session(
                self._projects[project_key],
                session_binding_id=session_binding_id,
                principal_ref=principal_ref,
                consumer_ref=consumer_ref,
                session_ref=session_ref,
            )
            self._sessions[session_key] = copy.deepcopy(session)
            return session

    def read_project(
        self,
        *,
        tenant_ref: str,
        workspace_ref: str,
        project_ref: str,
        scopes: set[str],
        principal_ref: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._require_available()
            self._require_scope(scopes, "project_state:read")
            key = self._project_key(tenant_ref, workspace_ref, project_ref)
            _require(key in self._projects, "project-instance-not-found", StateBindingError)
            validate_project_state(self._projects[key])
            return copy.deepcopy(self._projects[key])

    def read_session(
        self,
        *,
        tenant_ref: str,
        workspace_ref: str,
        principal_ref: str,
        consumer_ref: str,
        session_ref: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        with self._lock:
            self._require_available()
            self._require_scope(scopes, "project_state:read")
            key = self._session_key(tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref)
            _require(key in self._sessions, "control-session-not-bound", StateBindingError)
            session = self._sessions[key]
            project_key = self._project_key(tenant_ref, workspace_ref, session["project_ref"])
            _require(project_key in self._projects, "project-instance-not-found", StateBindingError)
            _require(
                session.get("project_revision") == self._projects[project_key].get("revision"),
                "control-session-stale-requires-begin-event-rehydrate",
                StateBindingError,
            )
            validate_session_state(session, self._projects[project_key])
            return copy.deepcopy(session)

    @staticmethod
    def _nearest_open_ancestor(project: dict[str, Any], context_ref: str) -> str | None:
        return nearest_open_ancestor(project, context_ref)

    def _rehydrate_session(self, session: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
        return rehydrate_control_session(session, project)

    def begin_event(self, request: dict[str, Any], *, scopes: set[str]) -> dict[str, Any]:
        with self._lock:
            self._require_available()
            self._require_scope(scopes, "project_state:transition")
            _require(request.get("schema") == BEGIN_SCHEMA, "begin-event-schema-mismatch")
            for field in (
                "tenant_ref", "workspace_ref", "principal_ref", "consumer_ref", "session_ref",
                "event_id", "idempotency_key",
            ):
                _require(isinstance(request.get(field), str) and bool(request[field].strip()), f"begin-{field}-required")
            session_key = self._session_key(
                request["tenant_ref"], request["workspace_ref"], request["principal_ref"],
                request["consumer_ref"], request["session_ref"],
            )
            _require(session_key in self._sessions, "control-session-not-bound", StateBindingError)
            idem_key = session_key + (request["idempotency_key"],)
            existing_event_id = self._idempotency.get(idem_key)
            if existing_event_id is not None:
                event = self._events[session_key + (existing_event_id,)]
                _require(
                    event.get("begin_request_fingerprint") == _sha256(request),
                    "idempotency-key-reused-with-different-begin-request",
                    StateConflict,
                )
                if event["state"] == "COMPLETED":
                    return copy.deepcopy(event["completion"])
                return copy.deepcopy(event["binding"])

            session = copy.deepcopy(self._sessions[session_key])
            project_key = self._project_key(request["tenant_ref"], request["workspace_ref"], session["project_ref"])
            _require(project_key in self._projects, "project-instance-not-found", StateBindingError)
            project = copy.deepcopy(self._projects[project_key])
            session_before_rehydrate = copy.deepcopy(session)
            session = self._rehydrate_session(session, project)
            self._sessions[session_key] = copy.deepcopy(session)
            rehydration_receipt = None
            if session != session_before_rehydrate:
                rehydration_receipt = {
                    "schema": "cerebro-control-session-rehydration-receipt/v1",
                    "project_ref": project["project_ref"],
                    "project_revision": project["revision"],
                    "session_revision_before": session_before_rehydrate["session_revision"],
                    "session_revision_after": session["session_revision"],
                    "session_fingerprint_before": session_before_rehydrate["fingerprint"],
                    "session_fingerprint_after": session["fingerprint"],
                    "active_context_before": session_before_rehydrate["active_context_ref"],
                    "active_context_after": session["active_context_ref"],
                    "continuation_preserved": (
                        session_before_rehydrate.get("active_continuation_binding") is not None
                        and session.get("active_continuation_binding") is not None
                    ),
                }
                rehydration_receipt["receipt_id"] = "CSR-" + _sha256(rehydration_receipt)[:24].upper()
            binding = {
                "schema": BEGIN_RESULT_SCHEMA,
                "event_id": request["event_id"],
                "idempotency_key": request["idempotency_key"],
                "project": copy.deepcopy(project),
                "session": copy.deepcopy(session),
                "expected_project_revision": project["revision"],
                "expected_project_fingerprint": project["fingerprint"],
                "expected_session_revision": session["session_revision"],
                "expected_session_fingerprint": session["fingerprint"],
                "repository_permission_required": False,
                "rehydration_receipt": rehydration_receipt,
            }
            event_key = session_key + (request["event_id"],)
            _require(event_key not in self._events, "event-id-already-used", StateConflict)
            self._events[event_key] = {
                "state": "OPEN",
                "project_key": project_key,
                "binding": copy.deepcopy(binding),
                "completion": None,
                "begin_request_fingerprint": _sha256(request),
                "completion_request_fingerprint": None,
            }
            self._idempotency[idem_key] = request["event_id"]
            return binding

    def _assert_no_other_session_invalidated(
        self,
        calling_session_key: tuple[str, str, str, str, str],
        project: dict[str, Any],
        project_operations: list[dict[str, Any]],
    ) -> None:
        ending_targets = {
            operation.get("context_ref")
            for operation in project_operations
            if operation.get("operation") in {"RETURN_CONTEXT", "CANCEL_CONTEXT"}
        }
        ending_targets.discard(None)
        if not ending_targets:
            return
        for session_key, session in self._sessions.items():
            if (
                session_key == calling_session_key
                or session_key[:3] != calling_session_key[:3]
                or session.get("project_ref") != project["project_ref"]
            ):
                continue
            if session.get("active_context_ref") in ending_targets:
                raise StateConflict("context-active-in-another-control-session")

    def complete_event(self, request: dict[str, Any], *, scopes: set[str]) -> dict[str, Any]:
        with self._lock:
            self._require_available()
            self._require_scope(scopes, "project_state:transition")
            for field in ("tenant_ref", "workspace_ref", "principal_ref", "consumer_ref", "session_ref", "event_id"):
                _require(isinstance(request.get(field), str) and bool(request[field].strip()), f"complete-{field}-required")
            directive = request.get("directive")
            _require(isinstance(directive, dict) and directive.get("schema") == DIRECTIVE_SCHEMA, "complete-directive-required")
            _require(directive.get("event_id") == request["event_id"], "complete-event-id-mismatch")
            session_key = self._session_key(
                request["tenant_ref"], request["workspace_ref"], request["principal_ref"],
                request["consumer_ref"], request["session_ref"],
            )
            event_key = session_key + (request["event_id"],)
            _require(event_key in self._events, "event-not-open", StateBindingError)
            event = self._events[event_key]
            if event["state"] == "COMPLETED":
                _require(
                    event.get("completion_request_fingerprint") == _sha256(request),
                    "completed-event-replayed-with-different-directive",
                    StateConflict,
                )
                return copy.deepcopy(event["completion"])
            project = self._projects[event["project_key"]]
            session = self._sessions[session_key]
            begin = event["binding"]
            if project["revision"] != begin["expected_project_revision"] or project["fingerprint"] != begin["expected_project_fingerprint"]:
                raise StateConflict("project-state-conflict-reload-and-reresolve")
            if session["session_revision"] != begin["expected_session_revision"] or session["fingerprint"] != begin["expected_session_fingerprint"]:
                raise StateConflict("session-state-conflict-reload-and-reresolve")
            self._assert_no_other_session_invalidated(session_key, project, directive.get("project_operations", []))
            try:
                project_after, session_after, receipt = apply_transition(project, session, directive)
            except ControlContextError as exc:
                raise StateConflict(str(exc)) from exc
            owner_effect_candidate = request.get("owner_effect_candidate")
            if owner_effect_candidate is not None:
                validate_context_owner_candidate_binding(
                    owner_effect_candidate,
                    directive=directive,
                    project_before=project,
                    project_after=project_after,
                    transition_receipt=receipt,
                )
            self._projects[event["project_key"]] = copy.deepcopy(project_after)
            self._sessions[session_key] = copy.deepcopy(session_after)
            completion = {
                "schema": COMPLETE_RESULT_SCHEMA,
                "event_id": request["event_id"],
                "result": "PASS",
                "receipt": copy.deepcopy(receipt),
                "project": copy.deepcopy(project_after),
                "session": copy.deepcopy(session_after),
                "repository_permission_required": False,
            }
            event["state"] = "COMPLETED"
            event["completion"] = copy.deepcopy(completion)
            event["completion_request_fingerprint"] = _sha256(request)
            event["completion_fingerprint"] = _sha256(completion)
            return completion

    def write_actor_generation_shadow(
        self,
        state: dict[str, Any],
        *,
        expected_revision: int,
        scopes: set[str],
    ) -> dict[str, Any]:
        """CAS-write an internal shadow projection; it grants no live authority."""

        with self._lock:
            self._require_available()
            self._require_scope(scopes, "project_state:transition")
            validate_actor_generation_shadow(state)
            key = (state["tenant_ref"], state["workspace_ref"], state["role"], state["generation_ref"])
            current = self._actor_generation_shadows.get(key)
            actual_revision = 0 if current is None else current["revision"]
            _require(actual_revision == expected_revision, "actor-generation-shadow-revision-conflict", StateConflict)
            _require(state["revision"] == expected_revision + 1, "actor-generation-shadow-revision-step-invalid", StateConflict)
            if current is not None:
                _require(current["actor_ref"] == state["actor_ref"], "actor-generation-shadow-identity-immutable", StateConflict)
                _require(current["lifecycle"] != "RETIRED", "actor-generation-shadow-retired-terminal", StateConflict)
            if state["role"] == "PRINCIPAL":
                baseline = state.get("principal_continuity_baseline")
                _require(
                    state["lifecycle"] not in {"READY", "ACTIVE"} or baseline is not None,
                    "principal-continuity-baseline-required-before-ready",
                    StateConflict,
                )
                prior = None if current is None else current.get("principal_continuity_baseline")
                if prior is not None:
                    _require(baseline is not None, "principal-continuity-baseline-removal-prohibited", StateConflict)
                if baseline is not None:
                    if prior is None:
                        _require(
                            baseline["ready_epoch"] == state["revision"],
                            "principal-continuity-baseline-ready-epoch-must-be-prospective",
                            StateConflict,
                        )
                    elif baseline["baseline_fingerprint"] == prior["baseline_fingerprint"]:
                        _require(
                            baseline["ready_epoch"] == prior["ready_epoch"],
                            "principal-continuity-baseline-ready-epoch-changed-without-new-baseline",
                            StateConflict,
                        )
                    else:
                        _require(
                            baseline["ready_epoch"] == state["revision"]
                            and baseline["ready_epoch"] > prior["ready_epoch"],
                            "principal-continuity-baseline-ready-epoch-must-advance",
                            StateConflict,
                        )
                        _require(
                            baseline["provider_revision"] >= prior["provider_revision"],
                            "principal-continuity-baseline-provider-revision-regression",
                            StateConflict,
                        )
                        _require(
                            baseline["covered_through_frontier"] >= prior["covered_through_frontier"],
                            "principal-continuity-baseline-frontier-regression",
                            StateConflict,
                        )
            self._actor_generation_shadows[key] = copy.deepcopy(state)
            return copy.deepcopy(state)

    def read_actor_generation_shadow(
        self,
        *,
        tenant_ref: str,
        workspace_ref: str,
        role: str,
        generation_ref: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        with self._lock:
            self._require_available()
            self._require_scope(scopes, "project_state:read")
            key = (tenant_ref, workspace_ref, role, generation_ref)
            _require(key in self._actor_generation_shadows, "actor-generation-shadow-not-found", StateBindingError)
            state = self._actor_generation_shadows[key]
            validate_actor_generation_shadow(state)
            return copy.deepcopy(state)

    def write_work_claim_shadow(
        self,
        state: dict[str, Any],
        *,
        expected_revision: int,
        scopes: set[str],
    ) -> dict[str, Any]:
        """CAS-write a non-authoritative claim shadow without creating a live claim."""

        with self._lock:
            self._require_available()
            self._require_scope(scopes, "project_state:transition")
            validate_work_claim_shadow(state)
            actor_key = (
                state["tenant_ref"], state["workspace_ref"],
                state["actor_role"], state["actor_generation_ref"],
            )
            _require(actor_key in self._actor_generation_shadows, "work-claim-shadow-actor-generation-not-found", StateBindingError)
            validate_work_claim_shadow(state, self._actor_generation_shadows[actor_key])
            key = (state["tenant_ref"], state["workspace_ref"], state["claim_ref"])
            current = self._work_claim_shadows.get(key)
            actual_revision = 0 if current is None else current["revision"]
            _require(actual_revision == expected_revision, "work-claim-shadow-revision-conflict", StateConflict)
            _require(state["revision"] == expected_revision + 1, "work-claim-shadow-revision-step-invalid", StateConflict)
            if current is not None:
                for field in ("project_ref", "actor_ref", "actor_role", "actor_generation_ref", "scope_ref", "mode"):
                    _require(current[field] == state[field], f"work-claim-shadow-{field}-immutable", StateConflict)
            self._work_claim_shadows[key] = copy.deepcopy(state)
            return copy.deepcopy(state)

    def read_work_claim_shadow(
        self,
        *,
        tenant_ref: str,
        workspace_ref: str,
        claim_ref: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        with self._lock:
            self._require_available()
            self._require_scope(scopes, "project_state:read")
            key = (tenant_ref, workspace_ref, claim_ref)
            _require(key in self._work_claim_shadows, "work-claim-shadow-not-found", StateBindingError)
            state = self._work_claim_shadows[key]
            validate_work_claim_shadow(state)
            return copy.deepcopy(state)
