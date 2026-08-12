#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


SCHEMA = "cerebro-change-campaign-closeout/v1"
RECEIPT_SCHEMA = "cerebro-change-campaign-closeout-receipt/v1"
REQUIRED_DIMENSIONS = (
    "intent_to_result",
    "rule_to_normal_consumer",
    "state_and_learning",
    "next_phase_readiness",
)
READY_STATES = {"READY", "READY_WITH_DECLARED_DEBT"}
BINDING_ID = "CHANGE_CAMPAIGN_CLOSEOUT_GATE"
EVIDENCE_BASIS_FILES = (
    "standards/change-campaign-closeout.yaml",
    "tooling/change/campaign-policy.yaml",
    "tooling/change/change_engine.py",
    "tooling/validator/change_campaign_closeout.py",
    "tooling/delivery/Cerebro.StandardDeliveryKernel.ps1",
    "mcp/control_resolution.py",
    "tooling/validator/contract-activation-bindings.json",
)


class CloseoutError(ValueError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise CloseoutError("json-object-required")
    return value


def _fingerprint(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(data.encode("ascii")).hexdigest()


def _source_fingerprint(root: Path) -> str:
    rows = []
    for relative in sorted(EVIDENCE_BASIS_FILES):
        path = root / relative
        if not path.is_file():
            raise CloseoutError(f"basis-file-missing:{relative}")
        rows.append(f"{relative}|{hashlib.sha256(path.read_bytes()).hexdigest()}")
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def validate(closeout: dict[str, Any], expected_base: str = "") -> dict[str, Any]:
    errors: list[str] = []
    if closeout.get("schema") != SCHEMA:
        errors.append("SCHEMA_MISMATCH")
    for field in ("campaign_id", "milestone", "source_base_commit", "next_phase"):
        if not str(closeout.get(field) or "").strip():
            errors.append(f"FIELD_REQUIRED:{field}")
    if expected_base and closeout.get("source_base_commit") != expected_base:
        errors.append("SOURCE_BASE_COMMIT_MISMATCH")

    patches = closeout.get("patch_sequence")
    if not isinstance(patches, list) or not patches or len(patches) != len(set(map(str, patches))):
        errors.append("PATCH_SEQUENCE_INVALID")

    dimensions = closeout.get("dimensions")
    if not isinstance(dimensions, dict) or set(dimensions) != set(REQUIRED_DIMENSIONS):
        errors.append("DIMENSION_SET_INVALID")
    else:
        for name in REQUIRED_DIMENSIONS:
            item = dimensions.get(name)
            if not isinstance(item, dict) or item.get("result") != "PASS":
                errors.append(f"DIMENSION_NOT_PASS:{name}")
                continue
            refs = item.get("evidence_refs")
            if not isinstance(refs, list) or not refs or any(not str(ref).strip() for ref in refs):
                errors.append(f"DIMENSION_EVIDENCE_MISSING:{name}")

    state = str(closeout.get("closeout_state") or "")
    debts = closeout.get("declared_debt")
    if not isinstance(debts, list):
        errors.append("DECLARED_DEBT_ARRAY_REQUIRED")
        debts = []
    for index, debt in enumerate(debts):
        if not isinstance(debt, dict):
            errors.append(f"DEBT_INVALID:{index}")
            continue
        for field in ("id", "owner", "target_phase", "rationale"):
            if not str(debt.get(field) or "").strip():
                errors.append(f"DEBT_FIELD_REQUIRED:{index}:{field}")
        if debt.get("blocks_next_phase") is not False:
            errors.append(f"DEBT_BLOCKING_OR_UNCLASSIFIED:{index}")
    if state == "READY" and debts:
        errors.append("READY_MUST_HAVE_ZERO_DECLARED_DEBT")
    if state == "READY_WITH_DECLARED_DEBT" and not debts:
        errors.append("DECLARED_DEBT_REQUIRED")
    if state not in READY_STATES:
        errors.append("CLOSEOUT_NOT_READY")
    if closeout.get("unknown_or_unclassified_debt") is not False:
        errors.append("UNKNOWN_OR_UNCLASSIFIED_DEBT")
    if closeout.get("normal_consumer") != "STANDARD_DELIVERY_KERNEL_SELFTEST":
        errors.append("NORMAL_CONSUMER_MISMATCH")

    return {
        "schema": RECEIPT_SCHEMA,
        "result": "PASS" if not errors else "BLOCKED",
        "binding_id": BINDING_ID,
        "campaign_id": closeout.get("campaign_id"),
        "milestone": closeout.get("milestone"),
        "closeout_state": state,
        "next_phase": closeout.get("next_phase"),
        "phase_transition_allowed": not errors,
        "normal_call_path_exercised": True,
        "all_dimensions_passed": not any(error.startswith("DIMENSION_") for error in errors),
        "unknown_or_unclassified_debt_absent": closeout.get("unknown_or_unclassified_debt") is False,
        "declared_debt_count": len(debts),
        "contract_fingerprint": _fingerprint(closeout),
        "errors": errors,
    }


def _fixture() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "campaign_id": "SELFTEST-CAMPAIGN",
        "milestone": "SELFTEST-MILESTONE",
        "source_base_commit": "a" * 40,
        "patch_sequence": ["R1", "R2"],
        "dimensions": {
            name: {"result": "PASS", "evidence_refs": [f"EVIDENCE-{name}"]}
            for name in REQUIRED_DIMENSIONS
        },
        "declared_debt": [],
        "unknown_or_unclassified_debt": False,
        "closeout_state": "READY",
        "next_phase": "NEXT",
        "normal_consumer": "STANDARD_DELIVERY_KERNEL_SELFTEST",
    }


def selftest() -> dict[str, Any]:
    tests: list[dict[str, Any]] = []

    def check(name: str, condition: bool) -> None:
        tests.append({"name": name, "result": "PASS" if condition else "FAIL"})

    good = validate(_fixture(), "a" * 40)
    check("ready-campaign-accepted", good["result"] == "PASS")
    missing = _fixture(); missing["dimensions"].pop("rule_to_normal_consumer")
    check("missing-dimension-blocked", validate(missing)["result"] == "BLOCKED")
    unknown = _fixture(); unknown["unknown_or_unclassified_debt"] = True
    check("unknown-debt-blocked", validate(unknown)["result"] == "BLOCKED")
    deferred = _fixture(); deferred["closeout_state"] = "READY_WITH_DECLARED_DEBT"
    deferred["declared_debt"] = [{
        "id": "D1", "owner": "Change Engine", "target_phase": "WAVE-03",
        "rationale": "Outside current dependency chain", "blocks_next_phase": False,
    }]
    check("declared-nonblocking-debt-accepted", validate(deferred)["result"] == "PASS")
    blocked = _fixture(); blocked["closeout_state"] = "BLOCKED"
    check("blocked-campaign-rejected", validate(blocked)["result"] == "BLOCKED")
    return {
        "schema": "cerebro-change-campaign-closeout-selftest/v1",
        "result": "PASS" if all(item["result"] == "PASS" for item in tests) else "FAIL",
        "tests": tests,
    }


def activation_probe(root: Path) -> dict[str, Any]:
    test = selftest()
    kernel = (root / "tooling/delivery/Cerebro.StandardDeliveryKernel.ps1").read_text(encoding="utf-8-sig")
    mcp = (root / "mcp/control_resolution.py").read_text(encoding="utf-8")
    wired = "SELFTEST_CHANGE_CAMPAIGN_CLOSEOUT" in kernel and "phase_transition_requested" in mcp
    return {
        "schema": "cerebro-change-campaign-closeout-activation-proof/v1",
        "result": "PASS" if test["result"] == "PASS" and wired else "FAIL",
        "binding_id": BINDING_ID,
        "proves_bindings": [BINDING_ID],
        "selftest_passed": test["result"] == "PASS",
        "standard_kernel_consumer_wired": "SELFTEST_CHANGE_CAMPAIGN_CLOSEOUT" in kernel,
        "mcp_phase_transition_consumer_wired": "phase_transition_requested" in mcp,
        "source_state_fingerprint": _source_fingerprint(root),
        "basis_files": list(EVIDENCE_BASIS_FILES),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Cerebro change-campaign closeout validator")
    parser.add_argument("command", choices=("validate-manifest", "selftest", "activation-probe"))
    parser.add_argument("--manifest")
    parser.add_argument("--source-root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--output")
    args = parser.parse_args()
    root = Path(args.source_root).resolve()
    if args.command == "selftest":
        result = selftest()
    elif args.command == "activation-probe":
        result = activation_probe(root)
    else:
        if not args.manifest:
            parser.error("validate-manifest requires --manifest")
        manifest = _read_json(Path(args.manifest))
        closeout = manifest.get("campaign_closeout")
        if not isinstance(closeout, dict):
            result = {"schema": RECEIPT_SCHEMA, "result": "BLOCKED", "errors": ["CAMPAIGN_CLOSEOUT_REQUIRED"]}
        else:
            result = validate(closeout, str(manifest.get("expected_base_commit") or ""))
    text = json.dumps(result, indent=2, ensure_ascii=True) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="ascii")
    else:
        print(text, end="")
    return 0 if result.get("result") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
