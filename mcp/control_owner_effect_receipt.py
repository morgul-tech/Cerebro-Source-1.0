#!/usr/bin/env python3
"""Shared envelope integrity for receipts emitted by existing semantic owners.

This module defines and validates the receipt shape.  It does not execute owner
semantics and cannot make Project, Quality, Convergence or Context state current.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any


RECEIPT_SCHEMA = "cerebro-owner-effect-receipt/v1"
OWNERS = {"project", "quality", "convergence", "context"}
OWNER_EFFECTS = {
    "project": {"REVISION_REQUIRED"},
    "quality": {"INVALIDATE_AFFECTED"},
    "convergence": {"REVALIDATE_AFFECTED"},
    "context": {"REFRESH_GOVERNING_REFS"},
}
SHA256 = re.compile(r"^[0-9a-f]{64}$")
CONSOLIDATION_REF = re.compile(r"^CCR-[0-9A-F]{24}$")


class OwnerEffectReceiptError(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise OwnerEffectReceiptError(message)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _fingerprint(value: dict[str, Any]) -> str:
    subject = copy.deepcopy(value)
    subject.pop("receipt_ref", None)
    subject.pop("receipt_fingerprint", None)
    return hashlib.sha256(_canonical(subject)).hexdigest()


def build_owner_effect_receipt(
    *,
    owner: str,
    control_decision_ref: str,
    consolidation_result_ref: str,
    effect: str,
    input_state_ref: str,
    input_state_fingerprint: str,
    output_state_ref: str,
    output_state_fingerprint: str,
    affected_refs: list[str],
    evidence_refs: list[str],
    unaffected_state_preserved: bool,
    state_mutated: bool,
    persistence_evidence_ref: str | None = None,
) -> dict[str, Any]:
    """Build an owner receipt envelope.

    Semantic consumers call this without ``persistence_evidence_ref`` and get a
    non-current CANDIDATE.  Only an owner persistence adapter may supply that
    reference, and the MCP router still requires an independently injected
    persistence verifier before treating the resulting PASS as current.
    """
    _require(owner in OWNERS, "owner-effect-receipt-owner-invalid")
    _require(effect in OWNER_EFFECTS[owner], "owner-effect-receipt-effect-invalid")
    for field, value in (
        ("control-decision-ref", control_decision_ref),
        ("consolidation-result-ref", consolidation_result_ref),
        ("input-state-ref", input_state_ref),
        ("output-state-ref", output_state_ref),
    ):
        _require(isinstance(value, str) and bool(value.strip()), f"owner-effect-{field}-required")
    for field, value in (
        ("input-state-fingerprint", input_state_fingerprint),
        ("output-state-fingerprint", output_state_fingerprint),
    ):
        _require(isinstance(value, str) and SHA256.fullmatch(value) is not None, f"owner-effect-{field}-invalid")
    _require(isinstance(affected_refs, list) and bool(affected_refs) and all(isinstance(item, str) and item for item in affected_refs), "owner-effect-affected-refs-invalid")
    _require(len(affected_refs) == len(set(affected_refs)), "owner-effect-affected-refs-duplicate")
    _require(isinstance(evidence_refs, list) and bool(evidence_refs) and all(isinstance(item, str) and item for item in evidence_refs), "owner-effect-evidence-refs-invalid")
    _require(len(evidence_refs) == len(set(evidence_refs)), "owner-effect-evidence-refs-duplicate")
    _require(isinstance(unaffected_state_preserved, bool), "owner-effect-unaffected-state-preserved-boolean-required")
    _require(isinstance(state_mutated, bool), "owner-effect-state-mutated-boolean-required")
    _require(
        persistence_evidence_ref is None
        or (isinstance(persistence_evidence_ref, str) and bool(persistence_evidence_ref.strip())),
        "owner-effect-persistence-evidence-ref-invalid",
    )
    persistence_evidence_ref = persistence_evidence_ref.strip() if isinstance(persistence_evidence_ref, str) else None
    persisted = persistence_evidence_ref is not None
    receipt: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "message_kind": "OWNER_EFFECT_RECEIPT",
        "producer_ref": owner,
        "owner": owner,
        "control_decision_ref": control_decision_ref,
        "consolidation_result_ref": consolidation_result_ref,
        "effect": effect,
        "result": "PASS" if persisted else "CANDIDATE",
        "current": persisted,
        "input_state_ref": input_state_ref,
        "input_state_fingerprint": input_state_fingerprint,
        "output_state_ref": output_state_ref,
        "output_state_fingerprint": output_state_fingerprint,
        "affected_refs": sorted(affected_refs),
        "evidence_refs": sorted(evidence_refs),
        "persistence_evidence_ref": persistence_evidence_ref,
        "unaffected_state_preserved": unaffected_state_preserved,
        "state_mutated": state_mutated,
        "receipt_fingerprint": "",
        "receipt_ref": "",
    }
    receipt["receipt_fingerprint"] = _fingerprint(receipt)
    receipt["receipt_ref"] = "OER-" + receipt["receipt_fingerprint"][:24].upper()
    validate_owner_effect_receipt(receipt)
    return receipt


def validate_owner_effect_receipt(
    receipt: dict[str, Any],
    *,
    expected_owner: str | None = None,
    expected_control_decision_ref: str | None = None,
    expected_consolidation_result_ref: str | None = None,
    expected_effect: str | None = None,
) -> dict[str, Any]:
    _require(isinstance(receipt, dict), "owner-effect-receipt-object-required")
    required = {
        "schema", "message_kind", "producer_ref", "owner", "control_decision_ref",
        "consolidation_result_ref", "effect", "result", "current", "input_state_ref",
        "input_state_fingerprint", "output_state_ref", "output_state_fingerprint",
        "affected_refs", "evidence_refs", "persistence_evidence_ref",
        "unaffected_state_preserved", "state_mutated",
        "receipt_fingerprint", "receipt_ref",
    }
    _require(not required.difference(receipt), "owner-effect-receipt-fields-missing")
    _require(receipt.get("schema") == RECEIPT_SCHEMA, "owner-effect-receipt-schema-mismatch")
    _require(receipt.get("message_kind") == "OWNER_EFFECT_RECEIPT", "owner-effect-receipt-message-kind-mismatch")
    owner = receipt.get("owner")
    _require(owner in OWNERS and receipt.get("producer_ref") == owner, "owner-effect-receipt-producer-mismatch")
    _require(receipt.get("effect") in OWNER_EFFECTS[owner], "owner-effect-receipt-effect-invalid")
    for field in ("control_decision_ref", "input_state_ref", "output_state_ref"):
        _require(isinstance(receipt.get(field), str) and bool(receipt[field].strip()), f"owner-effect-receipt-{field}-required")
    _require(
        isinstance(receipt.get("consolidation_result_ref"), str)
        and CONSOLIDATION_REF.fullmatch(receipt["consolidation_result_ref"]) is not None,
        "owner-effect-receipt-consolidation-result-ref-invalid",
    )
    for field in ("affected_refs", "evidence_refs"):
        values = receipt.get(field)
        _require(
            isinstance(values, list) and bool(values)
            and all(isinstance(item, str) and bool(item.strip()) for item in values),
            f"owner-effect-receipt-{field}-invalid",
        )
        _require(len(values) == len(set(values)), f"owner-effect-receipt-{field}-duplicate")
        _require(values == sorted(values), f"owner-effect-receipt-{field}-must-be-canonical-sorted")
    result = receipt.get("result")
    current = receipt.get("current")
    persistence_evidence_ref = receipt.get("persistence_evidence_ref")
    _require(result in {"CANDIDATE", "PASS"}, "owner-effect-receipt-result-invalid")
    _require(isinstance(current, bool), "owner-effect-receipt-current-boolean-required")
    _require(
        persistence_evidence_ref is None
        or (isinstance(persistence_evidence_ref, str) and bool(persistence_evidence_ref.strip())),
        "owner-effect-receipt-persistence-evidence-ref-invalid",
    )
    if result == "PASS":
        _require(current is True and persistence_evidence_ref is not None, "owner-effect-PASS-requires-persistence-evidence")
        _require(receipt.get("unaffected_state_preserved") is True, "owner-effect-PASS-requires-unaffected-state-preservation")
        _require(receipt.get("state_mutated") is True, "owner-effect-PASS-requires-material-state-mutation")
    else:
        _require(current is False and persistence_evidence_ref is None, "owner-effect-candidate-cannot-be-current-or-persisted")
    _require(isinstance(receipt.get("unaffected_state_preserved"), bool), "owner-effect-receipt-unaffected-boolean-required")
    _require(isinstance(receipt.get("state_mutated"), bool), "owner-effect-receipt-mutated-boolean-required")
    for field in ("input_state_fingerprint", "output_state_fingerprint", "receipt_fingerprint"):
        _require(isinstance(receipt.get(field), str) and SHA256.fullmatch(receipt[field]) is not None, f"owner-effect-receipt-{field}-invalid")
    _require(receipt.get("receipt_fingerprint") == _fingerprint(receipt), "owner-effect-receipt-fingerprint-mismatch")
    _require(receipt.get("receipt_ref") == "OER-" + receipt["receipt_fingerprint"][:24].upper(), "owner-effect-receipt-ref-mismatch")
    for field, expected in (
        ("owner", expected_owner),
        ("control_decision_ref", expected_control_decision_ref),
        ("consolidation_result_ref", expected_consolidation_result_ref),
        ("effect", expected_effect),
    ):
        if expected is not None:
            _require(receipt.get(field) == expected, f"owner-effect-receipt-{field}-mismatch")
    return {
        "result": "PASS",
        "receipt_result": result,
        "current": current,
        "owner": owner,
        "effect": receipt["effect"],
        "receipt_ref": receipt["receipt_ref"],
        "output_state_ref": receipt["output_state_ref"],
        "output_state_fingerprint": receipt["output_state_fingerprint"],
    }
