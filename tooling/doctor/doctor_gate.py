#!/usr/bin/env python3
from __future__ import annotations
import argparse, hashlib, json, re
from pathlib import Path
from typing import Any

REQUEST_SCHEMA = "cerebro-doctor-gate-request/v1"
RECEIPT_SCHEMA = "cerebro-doctor-assurance-receipt/v1"
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
GATE_FAMILIES = [
    "01_CONTRACT_MANIFEST_INTEGRITY",
    "02_RUNTIME_VISIBILITY",
    "03_TERMINAL_PERSISTENCE",
    "04_LOCATION_INDEPENDENT_EXECUTION",
    "05_CODE_EXECUTION_READINESS",
    "06_SYNTAX_CORRECTNESS",
    "07_FILESYSTEM_DEPENDENCY_INTEGRITY",
    "08_FAILURE_EVIDENCE_INTEGRITY",
    "09_PASTE_EXECUTION_INTEGRITY",
    "10_TARGET_BINDING_INTEGRITY",
    "11_OUTCOME_TRUTH",
    "12_DIAGNOSTIC_SUFFICIENCY",
    "13_EXECUTION_SEQUENCE_INTEGRITY",
    "14_PARTIAL_FAILURE_INTEGRITY",
    "15_RERUN_SAFETY",
    "16_SEMANTIC_CORRECTNESS",
    "17_KNOWN_FAILURE_PREVENTION_INTEGRITY",
    "18_MUTATION_RECOVERY_READINESS",
    "19_CODE_ARTIFACT_PROMOTION_INTEGRITY",
]

class DoctorGateError(RuntimeError):
    pass

def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def _hex40(value: Any, field: str) -> str:
    value = str(value or "")
    if not HEX40.fullmatch(value):
        raise DoctorGateError(f"INVALID_HEX40:{field}")
    return value

def _hex64(value: Any, field: str) -> str:
    value = str(value or "")
    if not HEX64.fullmatch(value):
        raise DoctorGateError(f"INVALID_HEX64:{field}")
    return value

def _nonempty(value: Any, field: str) -> str:
    value = str(value or "")
    if not value:
        raise DoctorGateError(f"MISSING:{field}")
    return value

def _receipt_fingerprint(receipt: dict[str, Any]) -> str:
    payload = dict(receipt)
    payload.pop("receipt_fingerprint", None)
    return sha256_hex(canonical(payload))

def validate_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    if receipt.get("schema") != RECEIPT_SCHEMA:
        raise DoctorGateError("RECEIPT_SCHEMA_INVALID")
    fp = _hex64(receipt.get("receipt_fingerprint"), "receipt_fingerprint")
    if fp != _receipt_fingerprint(receipt):
        raise DoctorGateError("RECEIPT_FINGERPRINT_MISMATCH")
    return receipt

def evaluate(request: dict[str, Any]) -> dict[str, Any]:
    if request.get("schema") != REQUEST_SCHEMA:
        raise DoctorGateError("REQUEST_SCHEMA_INVALID")
    subject = request.get("subject")
    basis = request.get("basis")
    instances = request.get("gate_instances")
    if not isinstance(subject, dict) or not isinstance(basis, dict) or not isinstance(instances, list):
        raise DoctorGateError("REQUEST_STRUCTURE_INVALID")

    subject_id = _nonempty(subject.get("subject_id"), "subject.subject_id")
    source_head = _hex40(subject.get("source_pre_head"), "subject.source_pre_head")
    package_sha = _hex64(subject.get("package_sha256"), "subject.package_sha256")
    paths_sha = _hex64(subject.get("touched_paths_sha256"), "subject.touched_paths_sha256")
    manifest_sha = _hex64(subject.get("manifest_sha256"), "subject.manifest_sha256")
    operation = _nonempty(subject.get("operation"), "subject.operation")
    claim_scope = subject.get("claim_scope")
    if not isinstance(claim_scope, list) or not claim_scope or any(not str(x) for x in claim_scope):
        raise DoctorGateError("CLAIM_SCOPE_INVALID")

    required_basis = [
        "doctor_implementation_sha256",
        "runtime_baseline_sha256",
        "knowledge_basis_sha256",
        "failure_index_sha256",
        "gate_plan_sha256",
    ]
    normalized_basis = {k: _hex64(basis.get(k), f"basis.{k}") for k in required_basis}

    seen_ids: set[str] = set()
    family_rows: dict[str, list[dict[str, Any]]] = {f: [] for f in GATE_FAMILIES}
    normalized_instances: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    for raw in instances:
        if not isinstance(raw, dict):
            raise DoctorGateError("GATE_INSTANCE_INVALID")
        iid = _nonempty(raw.get("instance_id"), "gate.instance_id")
        if iid in seen_ids:
            raise DoctorGateError("GATE_INSTANCE_DUPLICATE:" + iid)
        seen_ids.add(iid)
        fam = _nonempty(raw.get("gate_family"), "gate.gate_family")
        if fam not in family_rows:
            raise DoctorGateError("UNKNOWN_GATE_FAMILY:" + fam)
        applicability = str(raw.get("applicability") or "")
        status = str(raw.get("status") or "")
        evidence_sha = _hex64(raw.get("evidence_sha256"), f"gate.{iid}.evidence_sha256")
        if applicability not in {"REQUIRED", "NOT_APPLICABLE"}:
            raise DoctorGateError("APPLICABILITY_INVALID:" + iid)
        if applicability == "REQUIRED" and status not in {"PASS", "FAIL", "UNKNOWN", "UNAVAILABLE", "ERROR"}:
            raise DoctorGateError("REQUIRED_STATUS_INVALID:" + iid)
        if applicability == "NOT_APPLICABLE" and status != "NOT_APPLICABLE":
            raise DoctorGateError("N_A_STATUS_INVALID:" + iid)
        row = {
            "instance_id": iid,
            "gate_family": fam,
            "applicability": applicability,
            "status": status,
            "evidence_sha256": evidence_sha,
        }
        family_rows[fam].append(row)
        normalized_instances.append(row)
        if applicability == "REQUIRED" and status != "PASS":
            findings.append({"instance_id": iid, "gate_family": fam, "status": status})

    missing = [fam for fam, rows in family_rows.items() if not rows]
    if missing:
        findings.extend({"instance_id": "MISSING", "gate_family": fam, "status": "UNACCOUNTED"} for fam in missing)

    result = "PASS" if not findings else "BLOCK"
    receipt: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "result": result,
        "subject": {
            "subject_id": subject_id,
            "source_pre_head": source_head,
            "package_sha256": package_sha,
            "touched_paths_sha256": paths_sha,
            "manifest_sha256": manifest_sha,
            "operation": operation,
            "claim_scope": [str(x) for x in claim_scope],
        },
        "basis": normalized_basis,
        "gate_family_cardinality": len(GATE_FAMILIES),
        "gate_instance_cardinality": len(normalized_instances),
        "gate_instances": normalized_instances,
        "findings": findings,
        "unresolved_required_count": len(findings),
        "completion_semantics": "FINITE_MANIFEST_CARDINALITY",
        "authority": "ASSURANCE_EVIDENCE_ONLY",
    }
    receipt["receipt_fingerprint"] = _receipt_fingerprint(receipt)
    return receipt

def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("evaluate")
    e.add_argument("--request", required=True)
    e.add_argument("--out")
    v = sub.add_parser("validate-receipt")
    v.add_argument("--receipt", required=True)
    args = p.parse_args()
    try:
        if args.cmd == "evaluate":
            req = json.loads(Path(args.request).read_text(encoding="utf-8"))
            out = evaluate(req)
            if args.out:
                Path(args.out).parent.mkdir(parents=True, exist_ok=True)
                Path(args.out).write_text(json.dumps(out, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8", newline="\n")
            print(json.dumps(out, sort_keys=True))
            return 0 if out["result"] == "PASS" else 3
        receipt = json.loads(Path(args.receipt).read_text(encoding="utf-8"))
        validate_receipt(receipt)
        print(json.dumps({"result":"PASS","receipt_fingerprint":receipt["receipt_fingerprint"]}, sort_keys=True))
        return 0
    except (OSError, ValueError, DoctorGateError) as exc:
        print(json.dumps({"result":"BLOCK","reason":str(exc)}, sort_keys=True))
        return 3

if __name__ == "__main__":
    raise SystemExit(main())
