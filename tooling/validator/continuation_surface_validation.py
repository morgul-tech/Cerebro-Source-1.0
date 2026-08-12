#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
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
BINDING_ID = "HUMAN_CONTINUATION_SURFACE_ENFORCEMENT"
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
    "engines/interaction/rules.yaml",
    "engines/presentation/rules.yaml",
    ACTIVE_BINDING_REGISTRY,
    "mcp/manifest.yaml",
    "tooling/validator/continuation_surface_validation.py",
    "tooling/validator/checks.yaml",
    "tooling/validator/contract-activation-bindings.json",
)
MACHINE_PAYLOAD_PATTERNS = (
    re.compile(r"[;=]"),
    re.compile(r"(?:^|\s)[&|>$](?:\s|$)"),
    re.compile(r"(?:[A-Za-z]:\\|/[-A-Za-z0-9_.]+/)"),
    re.compile(r"https?://", re.IGNORECASE),
    re.compile(r"\b[0-9a-f]{40}(?:[0-9a-f]{24})?\b", re.IGNORECASE),
)


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

    return {
        "result": "PASS",
        "valid_short_trigger_accepted": True,
        "one_word_rejected": _must_reject("one-word", validate_binding, one_word),
        "six_word_rejected": _must_reject("six-word", validate_binding, six_words),
        "machine_payload_rejected": _must_reject("machine-payload", validate_binding, payload),
        "nonterminal_surface_rejected": _must_reject("nonterminal", validate_response, nonterminal),
        "mismatched_binding_rejected": _must_reject("mismatched-binding", validate_response, mismatched),
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
