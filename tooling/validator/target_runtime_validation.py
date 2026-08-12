#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

SCHEMA_PLAN = "cerebro-target-runtime-validation-plan/v1"
SCHEMA_RECEIPT = "cerebro-target-runtime-validation-receipt/v1"
VALIDATOR_ID = "CEREBRO-TARGET-RUNTIME-VALIDATION-001"


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"json-object-required:{path}")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git-failed:{' '.join(args)}:{proc.stderr.strip()}")
    return proc.stdout.rstrip("\r\n")


def candidate_identity(manifest: dict[str, Any]) -> str:
    rows: list[str] = []
    for item in sorted(manifest.get("files", []), key=lambda x: str(x.get("path", ""))):
        path = str(item.get("path", ""))
        operation = str(item.get("operation", ""))
        expected = str(item.get("expected_git_blob_sha", ""))
        sha256 = str(item.get("sha256", ""))
        blob = str(item.get("final_git_blob_sha", ""))
        if operation not in {"create", "replace", "delete"}:
            raise ValueError(f"candidate-identity-operation-invalid:{path}:{operation}")
        if operation in {"replace", "delete"} and not expected:
            raise ValueError(f"candidate-identity-baseline-missing:{path}")
        if operation == "delete":
            if sha256 or blob:
                raise ValueError(f"candidate-identity-delete-payload-forbidden:{path}")
        elif not sha256 or not blob:
            raise ValueError(f"candidate-identity-field-unresolved:{path}")
        if "GENERATED_AT_LAUNCH" in (expected, sha256, blob):
            raise ValueError(f"candidate-identity-field-unresolved:{path}")
        rows.append(f"{path}|{operation}|{expected}|{blob}|{sha256}")
    payload = "\n".join(rows).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def source_file_sha256(root: Path, relative: str) -> str:
    p = root / relative
    if not p.is_file():
        raise FileNotFoundError(f"candidate-file-missing:{relative}")
    return hashlib.sha256(p.read_bytes()).hexdigest()


def basis_fingerprint(root: Path, basis_files: list[str]) -> str:
    rows = [
        f"{p}|{source_file_sha256(root, p) if (root / p).is_file() else 'ABSENT'}"
        for p in sorted(basis_files)
    ]
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def build_plan(source_root: Path, manifest_path: Path, profile_id: str) -> dict[str, Any]:
    manifest = read_json(manifest_path)
    expected_base = str(manifest.get("expected_base_commit", ""))
    observed_head = git(source_root, "rev-parse", "HEAD")
    if observed_head != expected_base:
        raise RuntimeError(f"candidate-base-mismatch:expected={expected_base}:actual={observed_head}")

    changed_paths = sorted(str(x["path"]) for x in manifest.get("files", []))
    actual_changed = sorted(
        line[3:].replace("\\", "/")
        for line in git(source_root, "status", "--porcelain", "--untracked-files=all").splitlines()
        if len(line) >= 4
    )
    # Candidate may contain validator scratch only after planning; at plan time it must be exact.
    if actual_changed != changed_paths:
        raise RuntimeError(
            "candidate-changed-path-scope-mismatch:"
            f"expected={changed_paths}:actual={actual_changed}"
        )

    for item in manifest.get("files", []):
        relative = str(item["path"])
        operation = str(item.get("operation", ""))
        if operation == "delete":
            if (source_root / relative).exists():
                raise RuntimeError(f"candidate-delete-not-effective:{relative}")
            continue
        actual = source_file_sha256(source_root, relative)
        expected = str(item.get("sha256", ""))
        if actual != expected:
            raise RuntimeError(f"candidate-sha256-mismatch:{relative}:expected={expected}:actual={actual}")

    registry_path = source_root / "tooling/validator/contract-activation-bindings.json"
    registry = read_json(registry_path)
    runtime_bindings: list[dict[str, Any]] = []
    impacted: list[dict[str, Any]] = []
    changed = set(changed_paths)
    for binding in registry.get("bindings", []):
        if str(binding.get("wiring_proof_kind", "")) != "RUNTIME_EVIDENCE":
            continue
        spec = binding.get("runtime_evidence") or {}
        basis = [str(x) for x in spec.get("basis_files", [])]
        hits = sorted(changed.intersection(basis))
        row = {
            "binding_id": str(binding.get("id", "")),
            "evidence_path": str(spec.get("path", "")),
            "required_schema": str(spec.get("schema", "")),
            "required_proves_binding": str(spec.get("required_proves_binding", "")),
            "required_true_fields": [str(x) for x in spec.get("required_true_fields", [])],
            "basis_files": basis,
            "expected_source_state_fingerprint": basis_fingerprint(source_root, basis) if basis else "",
            "impacted": bool(hits),
            "changed_basis_files": hits,
        }
        runtime_bindings.append(row)
        if hits:
            impacted.append(row)

    declared_producers = []
    for probe in manifest.get("activation_probes", []):
        declared_producers.append({
            "kind": "ACTIVATION_PROBE",
            "id": str(probe.get("id", "")),
            "implementation_path": str(probe.get("implementation_path", "")),
            "required_schema": str(probe.get("required_schema", "")),
        })
    if manifest.get("material_commitment_preflight"):
        declared_producers.append({
            "kind": "STANDARD_MATERIAL_PREFLIGHT_CALL_PATH",
            "id": "STANDARD_DELIVERY_MATERIAL_PREFLIGHT_CALL_PATH",
            "implementation_path": "mcp/material_commitment_preflight.py",
            "required_schema": "cerebro-material-commitment-consumption/v1",
        })

    return {
        "schema": SCHEMA_PLAN,
        "validator_id": VALIDATOR_ID,
        "result": "PASS",
        "source_base_commit": expected_base,
        "candidate_identity": candidate_identity(manifest),
        "target_profile": profile_id,
        "changed_paths": changed_paths,
        "runtime_evidence_bindings": runtime_bindings,
        "impacted_runtime_evidence_bindings": impacted,
        "declared_producers": declared_producers,
        "contract_activation_registry": "tooling/validator/contract-activation-bindings.json",
        "contract_activation_closure": "tooling/validator/cerebro_contract_activation_closure.ps1",
        "deep_change_engine": "tooling/change/change_engine.py",
        "authority": "PRE_HANDOFF_ASSURANCE_ONLY",
        "authoritative_source_publish_allowed": False,
    }


def verify_receipt(manifest_path: Path, receipt_path: Path, profile_id: str | None) -> dict[str, Any]:
    manifest = read_json(manifest_path)
    receipt = read_json(receipt_path)
    reasons: list[str] = []
    if receipt.get("schema") != SCHEMA_RECEIPT:
        reasons.append("RECEIPT_SCHEMA_MISMATCH")
    if receipt.get("result") != "PASS":
        reasons.append("RECEIPT_NOT_PASS")
    if receipt.get("validator_id") != VALIDATOR_ID:
        reasons.append("VALIDATOR_ID_MISMATCH")
    if receipt.get("patch_id") != manifest.get("patch_id"):
        reasons.append("PATCH_ID_MISMATCH")
    if receipt.get("source_base_commit") != manifest.get("expected_base_commit"):
        reasons.append("SOURCE_BASE_COMMIT_MISMATCH")
    try:
        expected_identity = candidate_identity(manifest)
    except Exception as exc:
        reasons.append(f"CANDIDATE_IDENTITY_UNRESOLVED:{exc}")
        expected_identity = ""
    if receipt.get("candidate_identity") != expected_identity:
        reasons.append("CANDIDATE_IDENTITY_MISMATCH")
    if profile_id and receipt.get("target_profile") != profile_id:
        reasons.append("TARGET_PROFILE_MISMATCH")
    if receipt.get("target_runtime_execution") is not True:
        reasons.append("TARGET_RUNTIME_NOT_EXECUTED")
    if receipt.get("authoritative_source_mutated") is not False:
        reasons.append("AUTHORITATIVE_SOURCE_MUTATION_NOT_FALSE")
    cac = receipt.get("cac") or {}
    if cac.get("result") != "PASS":
        reasons.append("CAC_NOT_PASS")
    deep = receipt.get("deep_assurance") or {}
    if deep.get("result") != "PASS" or int(deep.get("required_runs", 0)) < 3:
        reasons.append("DEEP_ASSURANCE_NOT_PASS")
    if receipt.get("producer_consumer_compatibility") != "PASS":
        reasons.append("PRODUCER_CONSUMER_COMPATIBILITY_NOT_PASS")
    expected_paths = sorted(str(x["path"]) for x in manifest.get("files", []))
    if sorted(str(x) for x in receipt.get("changed_paths", [])) != expected_paths:
        reasons.append("CHANGED_PATHS_MISMATCH")
    return {
        "schema": "cerebro-target-runtime-validation-receipt-verification/v1",
        "result": "PASS" if not reasons else "BLOCK",
        "reasons": reasons,
        "candidate_identity": expected_identity,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan")
    p_plan.add_argument("--source-root", required=True)
    p_plan.add_argument("--manifest", required=True)
    p_plan.add_argument("--profile", required=True)
    p_plan.add_argument("--output", required=True)

    p_verify = sub.add_parser("verify-receipt")
    p_verify.add_argument("--manifest", required=True)
    p_verify.add_argument("--receipt", required=True)
    p_verify.add_argument("--profile")
    p_verify.add_argument("--output")

    args = parser.parse_args()
    if args.command == "plan":
        result = build_plan(Path(args.source_root), Path(args.manifest), args.profile)
        write_json(Path(args.output), result)
    else:
        result = verify_receipt(Path(args.manifest), Path(args.receipt), args.profile)
        if args.output:
            write_json(Path(args.output), result)
        else:
            print(json.dumps(result, indent=2))
    return 0 if result.get("result") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
