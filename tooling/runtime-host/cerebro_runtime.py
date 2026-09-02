#!/usr/bin/env python3
"""Cerebro Runtime2 owner-preserving event kernel.

This module implements the bounded Runtime2 semantic core.  It deliberately
contains no resident scheduler, no policy resolver, no Source mutation path and
no autonomous retry.  It consumes already-resolved MCP decisions and immutable
execution-basis material, compiles one deterministic event plan, records
ordered outcomes, and composes evidence-only terminal receipts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

RUNTIME_VERSION = "2.0.0-m4"
EVENT_SCHEMA = "cerebro-runtime2-event/v1"
BASIS_SCHEMA = "cerebro-runtime2-execution-basis/v1"
PLAN_SCHEMA = "cerebro-runtime2-execution-plan/v1"
RECEIPT_SCHEMA = "cerebro-runtime2-terminal-receipt/v1"
NODE_OUTCOME_SCHEMA = "cerebro-runtime2-node-outcome/v1"
MCP_DECISION_CONTRACT = "MCP_CONTROL_DECISION"

CONTROL_OUTCOMES = {
    "CONTINUE", "REMEDIATE", "RETRY", "REORIENT", "USER_DECISION_REQUIRED", "BLOCK"
}
EXECUTION_MODES = {"IN_PROCESS_TRUSTED_PURE", "SUPERVISED_PROCESS", "ISOLATED_CAPABILITY_WORKER"}
EXECUTION_STATES = {"NOT_RUN", "SUCCESS", "FAIL", "CONTROL_STOP", "UNKNOWN"}
VERIFICATION_STATES = {"NOT_RUN", "PASS", "FAIL", "UNKNOWN", "NOT_APPLICABLE"}
SIDE_EFFECT_STATES = {"NOT_ATTEMPTED", "NONE", "CONFIRMED", "OWNER_COMMITTED", "UNKNOWN"}
TERMINAL_STATES = {"COMPLETED", "WAITING_USER", "CONTROL_STOPPED", "FAILED_CLOSED", "RECOVERY_REQUIRED"}
TERMINAL_DISPOSITIONS = {
    "EFFECTS_VERIFIED", "NO_EFFECT_CONTROL_OUTCOME", "HUMAN_BOUNDARY",
    "MCP_RERESOLUTION_BOUNDARY", "DECLARED_CONTROL_STOP", "KNOWN_FAILURE",
    "EXECUTION_TRUTH_UNKNOWN",
}
HEX64 = re.compile(r"^[0-9a-f]{64}$")
MAX_SAFE_INTEGER = 9007199254740991


class Runtime2Error(RuntimeError):
    def __init__(self, classification: str, detail: str):
        super().__init__(f"{classification}:{detail}")
        self.classification = classification
        self.detail = detail


def _fail(classification: str, detail: str) -> None:
    raise Runtime2Error(classification, detail)


def _utf16_sort_key(value: str) -> bytes:
    return value.encode("utf-16-be", errors="strict")


def jcs_canonical_bytes(value: Any) -> bytes:
    """RFC8785-compatible canonical JSON for the Runtime2 supported domain.

    Runtime2 identity material intentionally forbids floating point and integers
    outside the interoperable IEEE-754 exact integer domain.  This avoids
    cross-runtime numeric ambiguity while remaining compatible with JCS.
    """
    def emit(v: Any) -> str:
        if v is None:
            return "null"
        if v is True:
            return "true"
        if v is False:
            return "false"
        if isinstance(v, int) and not isinstance(v, bool):
            if abs(v) > MAX_SAFE_INTEGER:
                _fail("CANONICAL_NUMBER_OUT_OF_RANGE", str(v))
            return str(v)
        if isinstance(v, float):
            _fail("CANONICAL_FLOAT_FORBIDDEN", repr(v))
        if isinstance(v, str):
            return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
        if isinstance(v, (list, tuple)):
            return "[" + ",".join(emit(x) for x in v) + "]"
        if isinstance(v, Mapping):
            if any(not isinstance(k, str) for k in v):
                _fail("CANONICAL_OBJECT_KEY_INVALID", "non-string key")
            items = sorted(v.items(), key=lambda item: _utf16_sort_key(item[0]))
            return "{" + ",".join(emit(k) + ":" + emit(x) for k, x in items) + "}"
        _fail("CANONICAL_TYPE_UNSUPPORTED", type(v).__name__)
        raise AssertionError

    return emit(value).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def fingerprint(value: Any) -> str:
    return sha256_bytes(jcs_canonical_bytes(value))


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        _fail("JSON_ROOT_NOT_OBJECT", str(path))
    return value


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.write_text(payload, encoding="utf-8", newline="\n")


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _fail("SCHEMA_FIELD_TYPE", f"{name}:mapping-required")
    return dict(value)


def _list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        _fail("SCHEMA_FIELD_TYPE", f"{name}:list-required")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail("SCHEMA_FIELD_REQUIRED", name)
    return value.strip()


def _sha(value: Any, name: str) -> str:
    text = _text(value, name).lower()
    if not HEX64.fullmatch(text):
        _fail("FINGERPRINT_INVALID", name)
    return text


def _bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        _fail("SCHEMA_FIELD_TYPE", f"{name}:bool-required")
    return value


def exact_binding(value: Any, name: str, *, require_kind: bool = False) -> dict[str, Any]:
    b = _mapping(value, name)
    ref = _text(b.get("ref"), f"{name}.ref")
    fp = _sha(b.get("fingerprint"), f"{name}.fingerprint")
    out: dict[str, Any] = {"ref": ref, "fingerprint": fp}
    kind = b.get("kind_or_schema") or b.get("schema") or b.get("kind")
    if require_kind and not isinstance(kind, str):
        _fail("EVIDENCE_REF_WITHOUT_EXACT_IDENTITY_BLOCK", f"{name}:kind_or_schema")
    if isinstance(kind, str) and kind.strip():
        out["kind_or_schema"] = kind.strip()
    producer = b.get("producer_ref")
    if isinstance(producer, str) and producer.strip():
        out["producer_ref"] = producer.strip()
    return out


def _assert_self_fingerprint(obj: Mapping[str, Any], fp_field: str, content: Mapping[str, Any], classification: str) -> str:
    observed = _sha(obj.get(fp_field), fp_field)
    expected = fingerprint(content)
    if observed != expected:
        _fail(classification, f"expected={expected}:actual={observed}")
    return observed


def normalize_event(event: Mapping[str, Any]) -> dict[str, Any]:
    e = deepcopy(dict(event))
    if e.get("schema") != EVENT_SCHEMA:
        _fail("EVENT_SCHEMA_INVALID", str(e.get("schema")))
    event_id = _text(e.get("event_id"), "event_id")
    event_type = _text(e.get("event_type"), "event_type")
    _text(e.get("issued_at"), "issued_at")  # evidence field, included in event identity by contract
    _text(e.get("source"), "source")
    if "authority_claim" not in e:
        e["authority_claim"] = "NONE"
    if not isinstance(e.get("payload"), Mapping):
        _fail("UNSUPPORTED_EVENT_OR_PAYLOAD_CONTRACT_FAIL", "payload-not-object")
    correlation = e.get("correlation_ref")
    if correlation is not None and not isinstance(correlation, str):
        _fail("EVENT_SCHEMA_INVALID", "correlation_ref")
    project_bound = _bool(e.get("project_bound", False), "project_bound")
    if project_bound:
        for key in ("project_ref", "control_session_ref", "control_context_state_ref"):
            _text(e.get(key), key)
    # Event contract resolution is explicit, never generic fallthrough.
    _text(e.get("event_contract_ref"), "event_contract_ref")
    _text(e.get("payload_contract_ref"), "payload_contract_ref")
    e["event_id"] = event_id
    e["event_type"] = event_type
    return e


def event_identity(event: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    normalized = normalize_event(event)
    return normalized, fingerprint(normalized)


def normalize_basis(basis: Mapping[str, Any]) -> dict[str, Any]:
    b = deepcopy(dict(basis))
    if b.get("schema") != BASIS_SCHEMA:
        _fail("BASIS_INCOMPLETE_BLOCK", "schema")
    required_maps = (
        "source", "release", "runtime_distribution", "dependencies", "capability_registry",
        "effective_configuration", "platform",
    )
    for key in required_maps:
        _mapping(b.get(key), key)
    src = _mapping(b["source"], "source")
    for key in ("provider_repository_id", "repository_locator", "branch_ref", "commit_sha", "root_tree_sha"):
        _text(src.get(key), f"source.{key}")
    _sha(src.get("commit_sha"), "source.commit_sha")
    _sha(src.get("root_tree_sha"), "source.root_tree_sha")
    canonical = {
        "source": b["source"],
        "release": b["release"],
        "runtime_distribution": b["runtime_distribution"],
        "dependencies": b["dependencies"],
        "capability_registry": b["capability_registry"],
        "effective_configuration": b["effective_configuration"],
        "platform": b["platform"],
    }
    fp = fingerprint(canonical)
    declared_fp = _sha(b.get("execution_basis_fingerprint"), "execution_basis_fingerprint")
    if fp != declared_fp:
        _fail("BASIS_FINGERPRINT_NONDETERMINISM", f"expected={fp}:actual={declared_fp}")
    expected_id = "R2BASIS-" + fp[:24].upper()
    if b.get("execution_basis_id") != expected_id:
        _fail("BASIS_ID_DERIVATION_MISMATCH", str(b.get("execution_basis_id")))
    return b


def _decision_schema_ok(value: Any) -> bool:
    return isinstance(value, str) and (
        value == MCP_DECISION_CONTRACT or value.startswith("cerebro-mcp-control-decision/")
    )


def normalize_decision(decision: Mapping[str, Any], event_fp: str, basis: Mapping[str, Any]) -> dict[str, Any]:
    d = deepcopy(dict(decision))
    if not _decision_schema_ok(d.get("schema")) and d.get("contract") != MCP_DECISION_CONTRACT:
        _fail("CONTROL_DECISION_MISSING_STALE_OR_MISMATCHED", "contract")
    decision_ref = _text(d.get("control_decision_ref"), "control_decision_ref")
    outcome = _text(d.get("outcome") or d.get("control_outcome"), "control_outcome")
    if outcome not in CONTROL_OUTCOMES:
        _fail("CONTROL_DECISION_MISSING_STALE_OR_MISMATCHED", f"outcome={outcome}")
    if _sha(d.get("event_fingerprint"), "decision.event_fingerprint") != event_fp:
        _fail("CONTROL_DECISION_MISSING_STALE_OR_MISMATCHED", "event-fingerprint")
    if _sha(d.get("execution_basis_fingerprint"), "decision.execution_basis_fingerprint") != basis["execution_basis_fingerprint"]:
        _fail("CONTROL_DECISION_MISSING_STALE_OR_MISMATCHED", "basis-fingerprint")
    # Fingerprint excludes the fingerprint field itself.
    material = {k: v for k, v in d.items() if k != "control_decision_fingerprint"}
    declared = _sha(d.get("control_decision_fingerprint"), "control_decision_fingerprint")
    observed = fingerprint(material)
    if declared != observed:
        _fail("CONTROL_DECISION_MISSING_STALE_OR_MISMATCHED", "decision-fingerprint")
    d["control_decision_ref"] = decision_ref
    d["control_outcome"] = outcome
    return d


def _registry_entries(basis: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    registry = _mapping(basis["capability_registry"], "capability_registry")
    entries_raw = registry.get("capabilities") or registry.get("entries")
    entries = _list(entries_raw, "capability_registry.capabilities")
    out: dict[str, dict[str, Any]] = {}
    for item in entries:
        row = _mapping(item, "capability_registry.entry")
        cid = _text(row.get("capability_id"), "capability_id")
        if cid in out:
            _fail("CAPABILITY_BINDING_INVALID", f"duplicate:{cid}")
        out[cid] = row
    return out


def _normalize_node(raw: Mapping[str, Any], ordinal: int, registry: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    n = deepcopy(dict(raw))
    cid = _text(n.get("capability_id"), "node.capability_id")
    entry = registry.get(cid)
    if entry is None:
        _fail("FAILED_CLOSED_BEFORE_NODE_DISPATCH", f"unknown-capability:{cid}")
    owner = _text(n.get("owner"), "node.owner")
    if owner != _text(entry.get("owner"), f"registry.{cid}.owner"):
        _fail("OWNER_CAPABILITY_BINDING_BLOCK", cid)
    binding_ref = _text(n.get("capability_binding_ref"), "node.capability_binding_ref")
    if binding_ref != _text(entry.get("capability_binding_ref"), f"registry.{cid}.capability_binding_ref"):
        _fail("CAPABILITY_BINDING_INVALID", cid)
    mode = _text(n.get("execution_mode_ref"), "node.execution_mode_ref")
    if mode not in EXECUTION_MODES:
        _fail("INVALID_EXECUTION_MODE_BINDING", mode)
    allowed_modes = entry.get("execution_modes") or [entry.get("execution_mode_ref")]
    if mode not in [x for x in allowed_modes if isinstance(x, str)]:
        _fail("INVALID_EXECUTION_MODE_BINDING", f"{cid}:{mode}")
    for key in ("output_contract_ref", "allowed_side_effects_ref", "verification_policy_ref", "failure_policy_ref"):
        _text(n.get(key), f"node.{key}")
    inputs = n.get("input_binding_refs", [])
    if not isinstance(inputs, list) or any(not isinstance(x, str) or not x for x in inputs):
        _fail("NODE_BINDING_INVALID", f"{cid}:input_binding_refs")
    rer = n.get("requires_mcp_reresolution_after", False)
    if not isinstance(rer, bool):
        _fail("NODE_BINDING_INVALID", f"{cid}:requires_mcp_reresolution_after")
    return {
        "node_id": f"N{ordinal:04d}",
        "ordinal": ordinal,
        "owner": owner,
        "capability_id": cid,
        "capability_binding_ref": binding_ref,
        "input_binding_refs": list(inputs),
        "output_contract_ref": n["output_contract_ref"],
        "allowed_side_effects_ref": n["allowed_side_effects_ref"],
        "verification_policy_ref": n["verification_policy_ref"],
        "failure_policy_ref": n["failure_policy_ref"],
        "execution_mode_ref": mode,
        "requires_mcp_reresolution_after": rer,
    }


def compile_plan(event: Mapping[str, Any], decision: Mapping[str, Any], basis: Mapping[str, Any]) -> dict[str, Any]:
    normalized_event, event_fp = event_identity(event)
    normalized_basis = normalize_basis(basis)
    normalized_decision = normalize_decision(decision, event_fp, normalized_basis)
    registry = _registry_entries(normalized_basis)
    raw_nodes = normalized_decision.get("dispatch_nodes")
    if raw_nodes is None:
        raw_nodes = normalized_decision.get("ordered_nodes", [])
    nodes_raw = _list(raw_nodes, "decision.dispatch_nodes")
    nodes: list[dict[str, Any]] = []
    rer_seen = False
    for idx, raw in enumerate(nodes_raw, start=1):
        if rer_seen:
            _fail("PLAN_INVALID_REQUIRES_NEW_MCP_DECISION_EVENT", f"node-after-reresolution:{idx}")
        node = _normalize_node(_mapping(raw, f"node[{idx}]"), idx, registry)
        nodes.append(node)
        rer_seen = bool(node["requires_mcp_reresolution_after"])

    outcome = normalized_decision["control_outcome"]
    if outcome in {"BLOCK", "USER_DECISION_REQUIRED"}:
        # Only exact non-mutating nodes are legal under these outcomes.
        for node in nodes:
            entry = registry[node["capability_id"]]
            if bool(entry.get("mutation_capable", True)):
                _fail("CONTROL_NOT_DISPATCHABLE", f"{outcome}:{node['capability_id']}")
    dispatchable = normalized_decision.get("dispatchable", bool(nodes) or outcome in {"BLOCK", "USER_DECISION_REQUIRED"})
    if not isinstance(dispatchable, bool) or not dispatchable:
        _fail("CONTROL_NOT_DISPATCHABLE", "decision-dispatchable-false")

    canonical_content = {
        "schema": PLAN_SCHEMA,
        "event_id": normalized_event["event_id"],
        "event_fingerprint": event_fp,
        "correlation_ref": normalized_event.get("correlation_ref"),
        "control_decision_ref": normalized_decision["control_decision_ref"],
        "control_decision_fingerprint": normalized_decision["control_decision_fingerprint"],
        "execution_basis_ref": normalized_basis["execution_basis_id"],
        "execution_basis_fingerprint": normalized_basis["execution_basis_fingerprint"],
        "control_outcome": outcome,
        "ordered_nodes": nodes,
    }
    plan_fp = fingerprint(canonical_content)
    return {
        **canonical_content,
        "plan_fingerprint": plan_fp,
        "plan_id": "R2PLAN-" + plan_fp[:24].upper(),
        "authority": "DERIVED_DISPATCH_ARTIFACT",
        "runtime_version": RUNTIME_VERSION,
    }


def verify_frozen_plan(plan: Mapping[str, Any]) -> None:
    p = dict(plan)
    if p.get("schema") != PLAN_SCHEMA:
        _fail("PLAN_BINDING_INVALID", "schema")
    declared = _sha(p.get("plan_fingerprint"), "plan_fingerprint")
    material = {k: v for k, v in p.items() if k not in {"plan_fingerprint", "plan_id", "authority", "runtime_version"}}
    observed = fingerprint(material)
    if declared != observed:
        _fail("PLAN_FINGERPRINT_NONDETERMINISM_OR_MUTATION_ATTEMPT", f"expected={observed}:actual={declared}")
    if p.get("plan_id") != "R2PLAN-" + declared[:24].upper():
        _fail("PLAN_ID_DERIVATION_MISMATCH", str(p.get("plan_id")))
    nodes = _list(p.get("ordered_nodes"), "ordered_nodes")
    for idx, node in enumerate(nodes, start=1):
        n = _mapping(node, f"node[{idx}]")
        if n.get("ordinal") != idx or n.get("node_id") != f"N{idx:04d}":
            _fail("NODE_ID_NONDETERMINISTIC_OR_DUPLICATE", str(idx))
        if idx < len(nodes) and n.get("requires_mcp_reresolution_after") is True:
            _fail("PLAN_INVALID_REQUIRES_NEW_MCP_DECISION_EVENT", str(idx))


def _empty_outcome(node: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": NODE_OUTCOME_SCHEMA,
        "node_id": node["node_id"],
        "ordinal": node["ordinal"],
        "owner": node["owner"],
        "capability_id": node["capability_id"],
        "capability_binding_ref": node["capability_binding_ref"],
        "execution_status": "NOT_RUN",
        "execution_result_binding": None,
        "process_observation_binding": None,
        "verification_status": "NOT_RUN",
        "verification_binding": None,
        "side_effect_state": "NOT_ATTEMPTED",
        "owner_effect_receipt_binding": None,
        "owner_state_commit_binding": None,
        "owner_persistence_verification_binding": None,
        "state_service_commit_binding": None,
        "evidence_bindings": [],
        "diagnostic_bindings": [],
        "failure_bindings": [],
    }


def validate_node_outcome(node: Mapping[str, Any], outcome: Mapping[str, Any]) -> dict[str, Any]:
    o = deepcopy(dict(outcome))
    if o.get("schema") != NODE_OUTCOME_SCHEMA:
        _fail("NODE_OUTCOME_CARDINALITY_ORDER_OR_EVIDENCE_BINDING_MISMATCH", "schema")
    for key in ("node_id", "ordinal", "owner", "capability_id", "capability_binding_ref"):
        if o.get(key) != node.get(key):
            _fail("NODE_OUTCOME_CARDINALITY_ORDER_OR_EVIDENCE_BINDING_MISMATCH", key)
    if o.get("execution_status") not in EXECUTION_STATES:
        _fail("EXECUTION_UNKNOWN_OR_RESULT_IDENTITY_COLLAPSE", str(o.get("execution_status")))
    if o.get("verification_status") not in VERIFICATION_STATES:
        _fail("EXECUTION_VERIFICATION_PROBE_OR_IDENTITY_COLLAPSE", str(o.get("verification_status")))
    if o.get("side_effect_state") not in SIDE_EFFECT_STATES:
        _fail("SIDE_EFFECT_POSTSTATE_UNKNOWN_COLLAPSE", str(o.get("side_effect_state")))
    # Any non-null external evidence binding must have exact immutable identity.
    for key in (
        "execution_result_binding", "process_observation_binding", "verification_binding",
        "owner_effect_receipt_binding", "owner_state_commit_binding",
        "owner_persistence_verification_binding", "state_service_commit_binding",
    ):
        if o.get(key) is not None:
            o[key] = exact_binding(o[key], key, require_kind=True)
    for key in ("evidence_bindings", "diagnostic_bindings", "failure_bindings"):
        vals = _list(o.get(key, []), key)
        o[key] = [exact_binding(v, f"{key}[{i}]", require_kind=True) for i, v in enumerate(vals)]
    return o


def derive_terminal(plan: Mapping[str, Any], outcomes: Sequence[Mapping[str, Any]]) -> tuple[str, str, bool]:
    control = str(plan["control_outcome"])
    partial = any(o.get("side_effect_state") in {"CONFIRMED", "OWNER_COMMITTED"} for o in outcomes)
    unknown = any(
        o.get("execution_status") == "UNKNOWN" or o.get("side_effect_state") == "UNKNOWN" or o.get("verification_status") == "UNKNOWN"
        for o in outcomes
    )
    known_fail = any(o.get("execution_status") == "FAIL" or o.get("verification_status") == "FAIL" for o in outcomes)
    control_stop = any(o.get("execution_status") == "CONTROL_STOP" for o in outcomes)
    if unknown:
        return "RECOVERY_REQUIRED", "EXECUTION_TRUTH_UNKNOWN", partial
    if known_fail:
        return "FAILED_CLOSED", "KNOWN_FAILURE", partial
    if control == "USER_DECISION_REQUIRED":
        return "WAITING_USER", "HUMAN_BOUNDARY", partial
    if control_stop or any(
        bool(node.get("requires_mcp_reresolution_after")) and outcomes[i].get("execution_status") == "SUCCESS"
        for i, node in enumerate(plan["ordered_nodes"])
        if i < len(outcomes)
    ):
        return "CONTROL_STOPPED", "MCP_RERESOLUTION_BOUNDARY", partial
    if not plan["ordered_nodes"]:
        return "COMPLETED", "NO_EFFECT_CONTROL_OUTCOME", partial
    if all(o.get("execution_status") == "SUCCESS" and o.get("verification_status") in {"PASS", "NOT_APPLICABLE"} for o in outcomes):
        return "COMPLETED", "EFFECTS_VERIFIED", partial
    return "FAILED_CLOSED", "KNOWN_FAILURE", partial


def receipt_subject_fingerprint(plan: Mapping[str, Any]) -> str:
    return fingerprint({
        "event_fingerprint": plan["event_fingerprint"],
        "control_decision_fingerprint": plan["control_decision_fingerprint"],
        "execution_basis_fingerprint": plan["execution_basis_fingerprint"],
        "plan_fingerprint": plan["plan_fingerprint"],
    })


def compose_receipt(
    plan: Mapping[str, Any], *, invocation_id: str, outcomes: Sequence[Mapping[str, Any]],
    started_at: str, completed_at: str, producer_ref: str = "tooling/runtime-host/cerebro_runtime.py",
    evidence_bindings: Sequence[Mapping[str, Any]] = (), diagnostic_bindings: Sequence[Mapping[str, Any]] = (),
    failure_bindings: Sequence[Mapping[str, Any]] = (), recovery: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    verify_frozen_plan(plan)
    invocation_id = _text(invocation_id, "invocation_id")
    if len(outcomes) != len(plan["ordered_nodes"]):
        _fail("NODE_OUTCOME_CARDINALITY_ORDER_OR_EVIDENCE_BINDING_MISMATCH", "count")
    normalized = [validate_node_outcome(node, outcome) for node, outcome in zip(plan["ordered_nodes"], outcomes)]
    state, disposition, partial = derive_terminal(plan, normalized)
    event_state = {
        "invocation_id": invocation_id,
        "event_id": plan["event_id"],
        "plan_fingerprint": plan["plan_fingerprint"],
        "last_started_node_id": next((o["node_id"] for o in reversed(normalized) if o["execution_status"] != "NOT_RUN"), None),
        "last_completed_node_id": next((o["node_id"] for o in reversed(normalized) if o["execution_status"] in {"SUCCESS", "FAIL", "CONTROL_STOP"}), None),
        "node_state_summary": [{"node_id": o["node_id"], "execution_status": o["execution_status"], "verification_status": o["verification_status"], "side_effect_state": o["side_effect_state"]} for o in normalized],
        "recovery_state": dict(recovery or {"required": state == "RECOVERY_REQUIRED"}),
        "runtime_terminal_state": state,
    }
    event_state_fp = fingerprint(event_state)
    content = {
        "schema": RECEIPT_SCHEMA,
        "receipt_subject_fingerprint": receipt_subject_fingerprint(plan),
        "invocation_id": invocation_id,
        "event_id": plan["event_id"],
        "event_fingerprint": plan["event_fingerprint"],
        "control_decision_ref": plan["control_decision_ref"],
        "control_decision_fingerprint": plan["control_decision_fingerprint"],
        "execution_basis_ref": plan["execution_basis_ref"],
        "execution_basis_fingerprint": plan["execution_basis_fingerprint"],
        "plan_id": plan["plan_id"],
        "plan_fingerprint": plan["plan_fingerprint"],
        "control_outcome": plan["control_outcome"],
        "runtime_terminal_state": state,
        "terminal_disposition": disposition,
        "node_outcomes": normalized,
        "event_state": event_state,
        "event_state_fingerprint": event_state_fp,
        "partial_effects_present": partial,
        "evidence_bindings": [exact_binding(v, "evidence_binding", require_kind=True) for v in evidence_bindings],
        "diagnostic_bindings": [exact_binding(v, "diagnostic_binding", require_kind=True) for v in diagnostic_bindings],
        "failure_bindings": [exact_binding(v, "failure_binding", require_kind=True) for v in failure_bindings],
        "recovery": dict(recovery or {"required": state == "RECOVERY_REQUIRED"}),
        "started_at": _text(started_at, "started_at"),
        "completed_at": _text(completed_at, "completed_at"),
        "authority": "EVIDENCE_ONLY",
        "producer_ref": producer_ref,
    }
    receipt_fp = fingerprint(content)
    return {**content, "receipt_fingerprint": receipt_fp, "receipt_id": "R2REC-" + receipt_fp[:24].upper()}


def no_op_receipt(plan: Mapping[str, Any], *, invocation_id: str, started_at: str, completed_at: str) -> dict[str, Any]:
    if plan.get("ordered_nodes"):
        _fail("NOOP_RECEIPT_REQUIRED", "plan-has-nodes")
    return compose_receipt(plan, invocation_id=invocation_id, outcomes=[], started_at=started_at, completed_at=completed_at)


def runtime2_selftest() -> dict[str, Any]:
    tests: list[dict[str, Any]] = []
    def check(name: str, condition: bool) -> None:
        tests.append({"name": name, "result": "PASS" if condition else "FAIL"})
    def blocked(name: str, fn: Callable[[], Any], classification: str) -> None:
        try:
            fn(); ok = False
        except Runtime2Error as exc:
            ok = exc.classification == classification
        tests.append({"name": name, "result": "PASS" if ok else "FAIL"})

    source = {"provider_repository_id":"github:1322106707","repository_locator":"morgul-tech/Cerebro-Source-1.0","branch_ref":"main","commit_sha":"1"*64,"root_tree_sha":"2"*64}
    registry = {"capabilities":[{"capability_id":"cap.echo","owner":"example-owner","capability_binding_ref":"cap.echo/v1","execution_mode_ref":"IN_PROCESS_TRUSTED_PURE","execution_modes":["IN_PROCESS_TRUSTED_PURE"],"mutation_capable":False}]}
    basis_material = {"source":source,"release":{"fingerprint":"3"*64},"runtime_distribution":{"fingerprint":"4"*64},"dependencies":{"fingerprint":"5"*64},"capability_registry":registry,"effective_configuration":{"fingerprint":"6"*64},"platform":{"fingerprint":"7"*64}}
    bfp = fingerprint(basis_material)
    basis = {"schema":BASIS_SCHEMA,**basis_material,"execution_basis_fingerprint":bfp,"execution_basis_id":"R2BASIS-"+bfp[:24].upper()}
    event = {"schema":EVENT_SCHEMA,"event_id":"EV-1","event_type":"TEST","issued_at":"2026-01-01T00:00:00Z","source":"selftest","authority_claim":"NONE","payload":{},"correlation_ref":"C1","project_bound":False,"event_contract_ref":"test-event/v1","payload_contract_ref":"test-payload/v1"}
    _, efp = event_identity(event)
    decision_base = {"schema":"cerebro-mcp-control-decision/v1","control_decision_ref":"MCPD-1","outcome":"CONTINUE","event_fingerprint":efp,"execution_basis_fingerprint":bfp,"dispatchable":True,"dispatch_nodes":[{"owner":"example-owner","capability_id":"cap.echo","capability_binding_ref":"cap.echo/v1","input_binding_refs":[],"output_contract_ref":"out/v1","allowed_side_effects_ref":"none","verification_policy_ref":"pure/v1","failure_policy_ref":"fail-closed/v1","execution_mode_ref":"IN_PROCESS_TRUSTED_PURE","requires_mcp_reresolution_after":False}]}
    decision = {**decision_base,"control_decision_fingerprint":fingerprint(decision_base)}
    p1 = compile_plan(event, decision, basis); p2 = compile_plan(deepcopy(event), deepcopy(decision), deepcopy(basis))
    check("deterministic_same_input_plan_fingerprint", p1["plan_fingerprint"] == p2["plan_fingerprint"] and jcs_canonical_bytes({k:v for k,v in p1.items() if k not in {"authority","runtime_version"}}) == jcs_canonical_bytes({k:v for k,v in p2.items() if k not in {"authority","runtime_version"}}))
    mutated_event = deepcopy(event); mutated_event["payload"]={"x":1}
    blocked("stale_mismatched_mcp_decision_blocks", lambda: compile_plan(mutated_event, decision, basis), "CONTROL_DECISION_MISSING_STALE_OR_MISMATCHED")
    unknown = deepcopy(decision_base); unknown["dispatch_nodes"][0]["capability_id"]="missing"; unknown["control_decision_fingerprint"]=fingerprint({k:v for k,v in unknown.items() if k!="control_decision_fingerprint"})
    blocked("undeclared_capability_blocks", lambda: compile_plan(event, unknown, basis), "FAILED_CLOSED_BEFORE_NODE_DISPATCH")
    block_base = deepcopy(decision_base); block_base["outcome"]="BLOCK"; block_base["dispatch_nodes"]=[]; block_base["control_decision_fingerprint"]=fingerprint({k:v for k,v in block_base.items() if k!="control_decision_fingerprint"})
    block_plan = compile_plan(event, block_base, basis)
    check("block_zero_node_legal", block_plan["ordered_nodes"] == [])
    rec = no_op_receipt(block_plan, invocation_id="INV-1", started_at="2026-01-01T00:00:00Z", completed_at="2026-01-01T00:00:01Z")
    check("completed_block_remains_no_effect_not_continue", rec["runtime_terminal_state"]=="COMPLETED" and rec["terminal_disposition"]=="NO_EFFECT_CONTROL_OUTCOME" and rec["control_outcome"]=="BLOCK")
    rer_base = deepcopy(decision_base); rer_base["dispatch_nodes"] = [deepcopy(decision_base["dispatch_nodes"][0]), deepcopy(decision_base["dispatch_nodes"][0])]; rer_base["dispatch_nodes"][0]["requires_mcp_reresolution_after"]=True; rer_base["control_decision_fingerprint"]=fingerprint({k:v for k,v in rer_base.items() if k!="control_decision_fingerprint"})
    blocked("mandatory_reresolution_boundary_cannot_have_following_node", lambda: compile_plan(event, rer_base, basis), "PLAN_INVALID_REQUIRES_NEW_MCP_DECISION_EVENT")
    frozen = deepcopy(p1); frozen["ordered_nodes"][0]["owner"]="tampered"
    blocked("plan_mutation_after_freeze_blocks", lambda: verify_frozen_plan(frozen), "PLAN_FINGERPRINT_NONDETERMINISM_OR_MUTATION_ATTEMPT")
    check("authority_claim_does_not_enter_control_owner", event["authority_claim"] == "NONE" and p1["authority"]=="DERIVED_DISPATCH_ARTIFACT")
    blocked("float_identity_input_blocks", lambda: jcs_canonical_bytes({"x":1.25}), "CANONICAL_FLOAT_FORBIDDEN")
    check("no_runtime_retry_or_scheduler_surface", not any(x in globals() for x in ("retry_loop","scheduler","resident_worker_pool")))
    result = "PASS" if all(t["result"]=="PASS" for t in tests) else "FAIL"
    return {"schema":"cerebro-runtime2-kernel-selftest/v1","runtime_version":RUNTIME_VERSION,"result":result,"tests":tests,"decision_owner":"MCP","resident_state":False,"autonomous_retry":False,"source_mutation":False}


def main() -> int:
    parser = argparse.ArgumentParser(description="Cerebro Runtime2 bounded event kernel")
    sub = parser.add_subparsers(dest="command", required=True)
    p_compile = sub.add_parser("compile-plan")
    p_compile.add_argument("--event", required=True); p_compile.add_argument("--decision", required=True); p_compile.add_argument("--basis", required=True); p_compile.add_argument("--output", required=True)
    p_verify = sub.add_parser("verify-plan")
    p_verify.add_argument("--plan", required=True); p_verify.add_argument("--output")
    p_noop = sub.add_parser("finalize-noop")
    p_noop.add_argument("--plan", required=True); p_noop.add_argument("--invocation-id", required=True); p_noop.add_argument("--started-at", required=True); p_noop.add_argument("--completed-at", required=True); p_noop.add_argument("--output", required=True)
    p_self = sub.add_parser("selftest"); p_self.add_argument("--output")
    args = parser.parse_args()
    try:
        if args.command == "compile-plan":
            out = compile_plan(read_json(Path(args.event)), read_json(Path(args.decision)), read_json(Path(args.basis))); write_json(Path(args.output), out)
        elif args.command == "verify-plan":
            plan=read_json(Path(args.plan)); verify_frozen_plan(plan); out={"schema":"cerebro-runtime2-plan-verification/v1","result":"PASS","plan_fingerprint":plan["plan_fingerprint"]}; write_json(Path(args.output),out) if args.output else print(json.dumps(out,indent=2))
        elif args.command == "finalize-noop":
            out=no_op_receipt(read_json(Path(args.plan)),invocation_id=args.invocation_id,started_at=args.started_at,completed_at=args.completed_at); write_json(Path(args.output),out)
        else:
            out=runtime2_selftest(); write_json(Path(args.output),out) if args.output else print(json.dumps(out,indent=2)); return 0 if out["result"]=="PASS" else 2
        return 0
    except Runtime2Error as exc:
        print(json.dumps({"schema":"cerebro-runtime2-error/v1","result":"BLOCK","classification":exc.classification,"detail":exc.detail},ensure_ascii=False), file=__import__('sys').stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


