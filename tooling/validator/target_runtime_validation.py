#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

SCHEMA_PLAN = "cerebro-target-runtime-validation-plan/v1"
SCHEMA_RECEIPT = "cerebro-target-runtime-validation-receipt/v1"
SCHEMA_RECEIPT_VERIFICATION = "cerebro-target-runtime-validation-receipt-verification/v1"
SCHEMA_EVIDENCE_BINDING = "cerebro-target-runtime-evidence-binding/v1"
SCHEMA_EVIDENCE_CUSTODY = "cerebro-target-runtime-evidence-custody/v1"
VALIDATOR_ID = "CEREBRO-TARGET-RUNTIME-VALIDATION-001"
CUSTODY_ALGORITHM = "SHA256_CONTENT_ADDRESSED_FILESYSTEM"


class TargetRuntimeValidationError(RuntimeError):
    pass


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"json-object-required:{path}")
    return value


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(_json_bytes(value))
    os.replace(temporary, path)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git(root: Path, *args: str) -> str:
    import subprocess

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

    declared_producers: list[dict[str, str]] = []
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

    context_bindings = _source_context_bindings(source_root, profile_id)
    plan_material = _target_plan_identity_material(
        source_base_commit=expected_base,
        candidate_identity_value=candidate_identity(manifest),
        changed_paths=changed_paths,
        impacted_runtime_evidence_bindings=impacted,
        target_profile_binding=context_bindings["target_profile_binding"],
    )
    plan_fp = _target_plan_fingerprint(plan_material)

    return {
        "schema": SCHEMA_PLAN,
        "validator_id": VALIDATOR_ID,
        "result": "PASS",
        "source_base_commit": expected_base,
        "candidate_identity": candidate_identity(manifest),
        "target_profile": profile_id,
        "target_profile_binding": context_bindings["target_profile_binding"],
        "target_runtime_adapter_binding": context_bindings["target_runtime_adapter_binding"],
        "delivery_adapter_binding": context_bindings["delivery_adapter_binding"],
        "target_plan_fingerprint": plan_fp,
        "changed_paths": changed_paths,
        "runtime_evidence_bindings": runtime_bindings,
        "impacted_runtime_evidence_bindings": impacted,
        "declared_producers": declared_producers,
        "evidence_custody_contract": {
            "schema": SCHEMA_EVIDENCE_CUSTODY,
            "algorithm": CUSTODY_ALGORITHM,
            "cleanup_requires_custody_and_consumer_validation": True,
            "post_cleanup_digest_and_resolvability_required": True,
        },
        "contract_activation_registry": "tooling/validator/contract-activation-bindings.json",
        "contract_activation_closure": "tooling/validator/cerebro_contract_activation_closure.ps1",
        "deep_change_engine": "tooling/change/change_engine.py",
        "authority": "PRE_HANDOFF_ASSURANCE_ONLY",
        "authoritative_source_publish_allowed": False,
    }


def _binding_source_root(explicit: Path | None = None) -> Path:
    return explicit.resolve() if explicit is not None else Path(__file__).resolve().parents[2]


def _source_context_bindings(source_root: Path, profile_id: str) -> dict[str, Any]:
    profile_path = source_root / "tooling/validator/target-runtime" / f"{profile_id}.json"
    target_adapter_path = source_root / "tooling/validator/target-runtime/Invoke-CerebroWindowsPowerShellValidation.ps1"
    delivery_adapter_path = source_root / "tooling/delivery/cerebro_delivery.ps1"
    for label, path in (
        ("target-profile", profile_path),
        ("target-runtime-adapter", target_adapter_path),
        ("delivery-adapter", delivery_adapter_path),
    ):
        if not path.is_file():
            raise TargetRuntimeValidationError(f"{label}-binding-file-missing:{path}")
    profile = read_json(profile_path)
    spec = profile.get("profile") or {}
    if not isinstance(spec, Mapping):
        raise TargetRuntimeValidationError("target-profile-object-invalid")
    if str(spec.get("id", "")) != profile_id:
        raise TargetRuntimeValidationError("target-profile-id-mismatch")
    if str(spec.get("receipt_schema", "")) != SCHEMA_RECEIPT:
        raise TargetRuntimeValidationError("target-profile-receipt-schema-mismatch")
    return {
        "target_profile_binding": {
            "id": profile_id,
            "version": str(spec.get("version", "")),
            "schema": str(profile.get("schema", "")),
            "path": "tooling/validator/target-runtime/" + f"{profile_id}.json",
            "sha256": sha256_file(profile_path),
        },
        "target_runtime_adapter_binding": {
            "path": "tooling/validator/target-runtime/Invoke-CerebroWindowsPowerShellValidation.ps1",
            "sha256": sha256_file(target_adapter_path),
        },
        "delivery_adapter_binding": {
            "path": "tooling/delivery/cerebro_delivery.ps1",
            "sha256": sha256_file(delivery_adapter_path),
        },
    }


def _normalized_impacted_bindings(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise TargetRuntimeValidationError("impacted-runtime-evidence-bindings-list-required")
    rows = [dict(item) if isinstance(item, Mapping) else item for item in value]
    return sorted(rows, key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"), default=str))


def _target_plan_identity_material(
    *,
    source_base_commit: str,
    candidate_identity_value: str,
    changed_paths: Iterable[str],
    impacted_runtime_evidence_bindings: Any,
    target_profile_binding: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "source_base_commit": source_base_commit,
        "candidate_identity": candidate_identity_value,
        "changed_paths": sorted(str(x) for x in changed_paths),
        "impacted_runtime_evidence_bindings": _normalized_impacted_bindings(impacted_runtime_evidence_bindings),
        "target_profile_binding": {
            "id": str(target_profile_binding.get("id", "")),
            "version": str(target_profile_binding.get("version", "")),
            "sha256": str(target_profile_binding.get("sha256", "")),
        },
        "evidence_custody_contract": {
            "schema": SCHEMA_EVIDENCE_CUSTODY,
            "algorithm": CUSTODY_ALGORITHM,
            "cleanup_requires_custody_and_consumer_validation": True,
            "post_cleanup_digest_and_resolvability_required": True,
        },
    }


def _target_plan_fingerprint(material: Mapping[str, Any]) -> str:
    raw = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256_bytes(raw)


def _finalize_receipt_context_bindings(
    receipt: dict[str, Any],
    *,
    source_root: Path,
    profile_id: str,
) -> dict[str, Any]:
    bindings = _source_context_bindings(source_root, profile_id)
    receipt.update(bindings)
    material = _target_plan_identity_material(
        source_base_commit=str(receipt.get("source_base_commit", "")),
        candidate_identity_value=str(receipt.get("candidate_identity", "")),
        changed_paths=receipt.get("changed_paths") or [],
        impacted_runtime_evidence_bindings=receipt.get("impacted_runtime_evidence_bindings") or [],
        target_profile_binding=bindings["target_profile_binding"],
    )
    receipt["target_plan_fingerprint"] = _target_plan_fingerprint(material)
    return receipt


def _verify_receipt_context_bindings(
    receipt: Mapping[str, Any],
    *,
    source_root: Path,
    profile_id: str,
) -> list[str]:
    reasons: list[str] = []
    try:
        expected = _source_context_bindings(source_root, profile_id)
    except Exception as exc:
        return [f"TARGET_CONTEXT_BINDING_UNAVAILABLE:{exc}"]
    for key in ("target_profile_binding", "target_runtime_adapter_binding", "delivery_adapter_binding"):
        actual = receipt.get(key)
        if not isinstance(actual, Mapping):
            reasons.append(f"{key.upper()}_MISSING")
        elif dict(actual) != expected[key]:
            reasons.append(f"{key.upper()}_MISMATCH")
    actual_profile = receipt.get("target_profile_binding")
    if isinstance(actual_profile, Mapping):
        try:
            material = _target_plan_identity_material(
                source_base_commit=str(receipt.get("source_base_commit", "")),
                candidate_identity_value=str(receipt.get("candidate_identity", "")),
                changed_paths=receipt.get("changed_paths") or [],
                impacted_runtime_evidence_bindings=receipt.get("impacted_runtime_evidence_bindings") or [],
                target_profile_binding=actual_profile,
            )
            expected_plan_fp = _target_plan_fingerprint(material)
            if receipt.get("target_plan_fingerprint") != expected_plan_fp:
                reasons.append("TARGET_PLAN_FINGERPRINT_MISMATCH")
        except Exception as exc:
            reasons.append(f"TARGET_PLAN_BINDING_INVALID:{exc}")
    return reasons


def _default_custody_root(receipt_path: Path) -> Path:
    parent = receipt_path.resolve().parent
    run_root = parent.parent if parent.name.lower() == "receipts" else parent
    return run_root / "Evidence" / "TargetRuntime"


def _assert_durable_root(custody_root: Path, receipt_path: Path) -> Path:
    root = custody_root.expanduser().resolve()
    receipt_parent = receipt_path.resolve().parent
    if root == receipt_parent:
        raise TargetRuntimeValidationError("custody-root-must-not-equal-receipt-directory")
    # The known failure is scratch cleanup. The default system temp root is never a valid
    # durable target-runtime evidence endpoint.
    temp_root = Path(tempfile.gettempdir()).resolve()
    try:
        root.relative_to(temp_root)
    except ValueError:
        pass
    else:
        raise TargetRuntimeValidationError(f"custody-root-under-system-temp:{root}")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _content_addressed_destination(custody_root: Path, digest: str) -> Path:
    return custody_root / "sha256" / digest[:2] / f"{digest}.json"


def _atomic_copy_verified(source: Path, destination: Path, expected_sha256: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        if sha256_file(destination) != expected_sha256:
            raise TargetRuntimeValidationError(f"custody-existing-digest-mismatch:{destination}")
        return
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    shutil.copyfile(source, temporary)
    if sha256_file(temporary) != expected_sha256:
        temporary.unlink(missing_ok=True)
        raise TargetRuntimeValidationError(f"custody-copy-digest-mismatch:{source}")
    os.replace(temporary, destination)
    if sha256_file(destination) != expected_sha256:
        raise TargetRuntimeValidationError(f"custody-postwrite-digest-mismatch:{destination}")


def _evidence_binding_from_file(proof: Mapping[str, Any], source: Path, destination: Path) -> dict[str, Any]:
    raw = source.read_bytes()
    digest = sha256_bytes(raw)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise TargetRuntimeValidationError(f"evidence-json-invalid:{source}:{exc}") from exc
    if not isinstance(parsed, dict):
        raise TargetRuntimeValidationError(f"evidence-json-object-required:{source}")
    return {
        "schema": SCHEMA_EVIDENCE_BINDING,
        "producer": str(proof.get("producer", "")),
        "artifact_schema": str(parsed.get("schema", "")),
        "artifact_result": str(parsed.get("result", "")),
        "sha256": digest,
        "byte_count": len(raw),
        "durable_ref": str(destination.resolve()),
        "content_address": f"sha256:{digest}",
        "proves_bindings": sorted(str(x) for x in proof.get("proves_bindings", []) if str(x)),
        "custody": {
            "class": "CONTENT_ADDRESSED_FILESYSTEM",
            "algorithm": "SHA256",
            "state": "PRESENT_VERIFIED",
            "lifecycle": "DOWNSTREAM_RECEIPT_VALIDATION",
            "scratch_cleanup_excluded": True,
        },
    }


def finalize_receipt_evidence_custody(
    receipt_path: Path,
    *,
    custody_root: Path | None = None,
    binding_source_root: Path | None = None,
    profile_id: str | None = None,
) -> dict[str, Any]:
    receipt = read_json(receipt_path)
    effective_profile = profile_id or str(receipt.get("target_profile", ""))
    if not effective_profile:
        raise TargetRuntimeValidationError("target-profile-required-for-context-binding")
    receipt = _finalize_receipt_context_bindings(
        receipt, source_root=_binding_source_root(binding_source_root), profile_id=effective_profile
    )
    root = _assert_durable_root(custody_root or _default_custody_root(receipt_path), receipt_path)
    proofs = receipt.get("activation_proofs")
    if not isinstance(proofs, list):
        raise TargetRuntimeValidationError("activation_proofs:list-required")

    finalized_proofs: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = []
    seen_digest: dict[str, str] = {}
    for index, raw_proof in enumerate(proofs):
        if not isinstance(raw_proof, Mapping):
            raise TargetRuntimeValidationError(f"activation_proofs[{index}]:object-required")
        proof = dict(raw_proof)
        existing = proof.get("evidence_binding")
        if isinstance(existing, Mapping):
            binding = dict(existing)
            bindings.append(binding)
            finalized_proofs.append({
                "producer": str(proof.get("producer", "")),
                "result": str(proof.get("result", "")),
                "proves_bindings": sorted(str(x) for x in proof.get("proves_bindings", []) if str(x)),
                "evidence_binding": binding,
            })
            continue
        source_text = str(proof.get("path", "")).strip()
        if not source_text:
            raise TargetRuntimeValidationError(f"activation_proofs[{index}].path:required-before-custody")
        source = Path(source_text)
        if not source.is_file():
            raise TargetRuntimeValidationError(f"activation-proof-source-missing:{source}")
        digest = sha256_file(source)
        destination = _content_addressed_destination(root, digest)
        _atomic_copy_verified(source, destination, digest)
        binding = _evidence_binding_from_file(proof, source, destination)
        previous = seen_digest.get(digest)
        if previous is not None and previous != binding["durable_ref"]:
            raise TargetRuntimeValidationError(f"content-address-collision:{digest}")
        seen_digest[digest] = binding["durable_ref"]
        bindings.append(binding)
        finalized_proofs.append({
            "producer": str(proof.get("producer", "")),
            "result": str(proof.get("result", "")),
            "proves_bindings": sorted(str(x) for x in proof.get("proves_bindings", []) if str(x)),
            "evidence_binding": binding,
        })

    custody_material = [
        f"{item.get('producer','')}|{item.get('sha256','')}|{item.get('byte_count','')}|{item.get('durable_ref','')}"
        for item in sorted(bindings, key=lambda x: (str(x.get("producer", "")), str(x.get("sha256", ""))))
    ]
    custody_fp = sha256_bytes("\n".join(custody_material).encode("utf-8"))
    receipt["activation_proofs"] = finalized_proofs
    receipt["evidence_custody"] = {
        "schema": SCHEMA_EVIDENCE_CUSTODY,
        "result": "PASS",
        "algorithm": CUSTODY_ALGORITHM,
        "binding_count": len(bindings),
        "custody_fingerprint": custody_fp,
        "bindings": bindings,
        "cleanup_gate": "CUSTODY_TRANSFER_AND_CONSUMER_VALIDATION_REQUIRED",
    }
    write_json(receipt_path, receipt)
    return read_json(receipt_path)


def _validate_one_binding(binding: Mapping[str, Any], index: int) -> list[str]:
    reasons: list[str] = []
    if binding.get("schema") != SCHEMA_EVIDENCE_BINDING:
        reasons.append(f"EVIDENCE_BINDING_SCHEMA_MISMATCH:{index}")
    digest = str(binding.get("sha256", ""))
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest.lower()):
        reasons.append(f"EVIDENCE_BINDING_SHA256_INVALID:{index}")
        return reasons
    ref = str(binding.get("durable_ref", "")).strip()
    if not ref:
        reasons.append(f"EVIDENCE_DURABLE_REF_MISSING:{index}")
        return reasons
    path = Path(ref)
    if not path.is_absolute():
        reasons.append(f"EVIDENCE_DURABLE_REF_NOT_ABSOLUTE:{index}")
        return reasons
    if not path.is_file():
        reasons.append(f"EVIDENCE_DURABLE_REF_UNRESOLVABLE:{index}")
        return reasons
    resolved = path.resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    try:
        resolved.relative_to(temp_root)
    except ValueError:
        pass
    else:
        reasons.append(f"EVIDENCE_DURABLE_REF_UNDER_SYSTEM_TEMP:{index}")
    if resolved.name != f"{digest}.json" or resolved.parent.name != digest[:2]:
        reasons.append(f"EVIDENCE_CONTENT_ADDRESS_PATH_MISMATCH:{index}")
    try:
        observed_size = path.stat().st_size
        expected_size = int(binding.get("byte_count", -1))
    except Exception:
        reasons.append(f"EVIDENCE_BYTE_COUNT_INVALID:{index}")
        return reasons
    if observed_size != expected_size:
        reasons.append(f"EVIDENCE_BYTE_COUNT_MISMATCH:{index}")
    if sha256_file(path) != digest.lower():
        reasons.append(f"EVIDENCE_DIGEST_MISMATCH:{index}")
    if str(binding.get("content_address", "")) != f"sha256:{digest}":
        reasons.append(f"EVIDENCE_CONTENT_ADDRESS_MISMATCH:{index}")
    custody = binding.get("custody") or {}
    if not isinstance(custody, Mapping):
        reasons.append(f"EVIDENCE_CUSTODY_BINDING_INVALID:{index}")
    else:
        if custody.get("class") != "CONTENT_ADDRESSED_FILESYSTEM":
            reasons.append(f"EVIDENCE_CUSTODY_CLASS_INVALID:{index}")
        if custody.get("state") != "PRESENT_VERIFIED":
            reasons.append(f"EVIDENCE_CUSTODY_STATE_INVALID:{index}")
        if custody.get("scratch_cleanup_excluded") is not True:
            reasons.append(f"EVIDENCE_CUSTODY_SCRATCH_EXCLUSION_MISSING:{index}")
    return reasons


def verify_evidence_custody(receipt: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    custody = receipt.get("evidence_custody")
    if not isinstance(custody, Mapping):
        return ["EVIDENCE_CUSTODY_MISSING"]
    if custody.get("schema") != SCHEMA_EVIDENCE_CUSTODY:
        reasons.append("EVIDENCE_CUSTODY_SCHEMA_MISMATCH")
    if custody.get("result") != "PASS":
        reasons.append("EVIDENCE_CUSTODY_NOT_PASS")
    if custody.get("algorithm") != CUSTODY_ALGORITHM:
        reasons.append("EVIDENCE_CUSTODY_ALGORITHM_MISMATCH")
    bindings = custody.get("bindings")
    if not isinstance(bindings, list):
        return reasons + ["EVIDENCE_CUSTODY_BINDINGS_LIST_REQUIRED"]
    try:
        declared_count = int(custody.get("binding_count", -1))
    except Exception:
        declared_count = -1
    if declared_count != len(bindings):
        reasons.append("EVIDENCE_CUSTODY_BINDING_COUNT_MISMATCH")
    for index, binding in enumerate(bindings):
        if not isinstance(binding, Mapping):
            reasons.append(f"EVIDENCE_BINDING_OBJECT_REQUIRED:{index}")
            continue
        reasons.extend(_validate_one_binding(binding, index))

    proofs = receipt.get("activation_proofs")
    if not isinstance(proofs, list):
        reasons.append("ACTIVATION_PROOFS_LIST_REQUIRED")
    else:
        if len(proofs) != len(bindings):
            reasons.append("ACTIVATION_PROOF_CUSTODY_CARDINALITY_MISMATCH")
        for index, proof in enumerate(proofs):
            if not isinstance(proof, Mapping) or not isinstance(proof.get("evidence_binding"), Mapping):
                reasons.append(f"ACTIVATION_PROOF_DURABLE_BINDING_MISSING:{index}")
                continue
            if "path" in proof:
                reasons.append(f"ACTIVATION_PROOF_EPHEMERAL_PATH_RETAINED:{index}")
            if str(proof.get("result", "")) != "PASS":
                reasons.append(f"ACTIVATION_PROOF_NOT_PASS:{index}")
            binding = proof.get("evidence_binding") or {}
            if isinstance(binding, Mapping) and str(binding.get("artifact_result", "")) != "PASS":
                reasons.append(f"EVIDENCE_ARTIFACT_RESULT_NOT_PASS:{index}")

    material = [
        f"{item.get('producer','')}|{item.get('sha256','')}|{item.get('byte_count','')}|{item.get('durable_ref','')}"
        for item in sorted(
            [dict(x) for x in bindings if isinstance(x, Mapping)],
            key=lambda x: (str(x.get("producer", "")), str(x.get("sha256", ""))),
        )
    ]
    expected_fp = sha256_bytes("\n".join(material).encode("utf-8"))
    if custody.get("custody_fingerprint") != expected_fp:
        reasons.append("EVIDENCE_CUSTODY_FINGERPRINT_MISMATCH")
    if custody.get("cleanup_gate") != "CUSTODY_TRANSFER_AND_CONSUMER_VALIDATION_REQUIRED":
        reasons.append("EVIDENCE_CUSTODY_CLEANUP_GATE_MISMATCH")
    return reasons


def _verify_receipt_summary(
    manifest: Mapping[str, Any],
    receipt: Mapping[str, Any],
    profile_id: str | None,
) -> tuple[list[str], str]:
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
        expected_identity = candidate_identity(dict(manifest))
    except Exception as exc:
        reasons.append(f"CANDIDATE_IDENTITY_UNRESOLVED:{exc}")
        expected_identity = ""
    if receipt.get("candidate_identity") != expected_identity:
        reasons.append("CANDIDATE_IDENTITY_MISMATCH")
    if profile_id and receipt.get("target_profile") != profile_id:
        reasons.append("TARGET_PROFILE_MISMATCH")
    if receipt.get("target_runtime_execution") is not True:
        reasons.append("TARGET_RUNTIME_NOT_EXECUTED")
    runtime_identity = receipt.get("target_runtime_identity")
    if not isinstance(runtime_identity, Mapping) or any(
        not str(runtime_identity.get(key, "")).strip() for key in ("os", "powershell_version", "powershell_edition")
    ):
        reasons.append("TARGET_RUNTIME_IDENTITY_INCOMPLETE")
    if receipt.get("authoritative_source_mutated") is not False:
        reasons.append("AUTHORITATIVE_SOURCE_MUTATION_NOT_FALSE")
    cac = receipt.get("cac") or {}
    if not isinstance(cac, Mapping) or cac.get("result") != "PASS":
        reasons.append("CAC_NOT_PASS")
    deep = receipt.get("deep_assurance") or {}
    if not isinstance(deep, Mapping):
        reasons.append("DEEP_ASSURANCE_NOT_PASS")
    else:
        try:
            required_runs = int(deep.get("required_runs", 0))
        except Exception:
            required_runs = 0
        if deep.get("result") != "PASS" or required_runs < 3:
            reasons.append("DEEP_ASSURANCE_NOT_PASS")
    if receipt.get("producer_consumer_compatibility") != "PASS":
        reasons.append("PRODUCER_CONSUMER_COMPATIBILITY_NOT_PASS")
    adapter = receipt.get("delivery_adapter_selftest") or {}
    if not isinstance(adapter, Mapping) or (
        adapter.get("result") != "PASS"
        or adapter.get("schema") != "cerebro-delivery-adapter-selftest/v0.3"
        or adapter.get("decision_owner") != "MCP"
        or adapter.get("adapter_recomputed") is not False
    ):
        reasons.append("DELIVERY_ADAPTER_SELFTEST_NOT_PASS")
    expected_paths = sorted(str(x["path"]) for x in manifest.get("files", []))
    actual_paths = receipt.get("changed_paths")
    if not isinstance(actual_paths, list) or sorted(str(x) for x in actual_paths) != expected_paths:
        reasons.append("CHANGED_PATHS_MISMATCH")
    return reasons, expected_identity


def verify_receipt(
    manifest_path: Path,
    receipt_path: Path,
    profile_id: str | None,
    *,
    custody_root: Path | None = None,
    binding_source_root: Path | None = None,
    finalize_custody: bool = True,
) -> dict[str, Any]:
    manifest = read_json(manifest_path)
    receipt = read_json(receipt_path)
    reasons, expected_identity = _verify_receipt_summary(manifest, receipt, profile_id)

    if not reasons and finalize_custody and not isinstance(receipt.get("evidence_custody"), Mapping):
        try:
            receipt = finalize_receipt_evidence_custody(
                receipt_path, custody_root=custody_root, binding_source_root=binding_source_root, profile_id=profile_id
            )
        except Exception as exc:
            reasons.append(f"EVIDENCE_CUSTODY_FINALIZATION_FAILED:{exc}")
    elif finalize_custody and isinstance(receipt.get("evidence_custody"), Mapping):
        # Already-finalized receipts are never rewritten merely to obtain another PASS.
        receipt = read_json(receipt_path)

    if not reasons:
        receipt = read_json(receipt_path)
        reasons.extend(_verify_receipt_context_bindings(
            receipt, source_root=_binding_source_root(binding_source_root), profile_id=profile_id or str(receipt.get("target_profile", ""))
        ))
        reasons.extend(verify_evidence_custody(receipt))

    receipt_sha = sha256_file(receipt_path) if receipt_path.is_file() else ""
    return {
        "schema": SCHEMA_RECEIPT_VERIFICATION,
        "result": "PASS" if not reasons else "BLOCK",
        "reasons": reasons,
        "candidate_identity": expected_identity,
        "receipt_sha256": receipt_sha,
        "evidence_custody_verified": not reasons,
        "finalize_custody_requested": finalize_custody,
    }


def verify_custody_only(receipt_path: Path) -> dict[str, Any]:
    receipt = read_json(receipt_path)
    reasons = verify_evidence_custody(receipt)
    return {
        "schema": "cerebro-target-runtime-evidence-custody-verification/v1",
        "result": "PASS" if not reasons else "BLOCK",
        "reasons": reasons,
        "receipt_sha256": sha256_file(receipt_path),
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
    p_verify.add_argument("--custody-root")
    p_verify.add_argument("--binding-source-root")
    p_verify.add_argument("--no-finalize-custody", action="store_true")

    p_custody = sub.add_parser("verify-custody")
    p_custody.add_argument("--receipt", required=True)
    p_custody.add_argument("--output")

    args = parser.parse_args()
    if args.command == "plan":
        result = build_plan(Path(args.source_root), Path(args.manifest), args.profile)
        write_json(Path(args.output), result)
    elif args.command == "verify-receipt":
        result = verify_receipt(
            Path(args.manifest),
            Path(args.receipt),
            args.profile,
            custody_root=Path(args.custody_root) if args.custody_root else None,
            binding_source_root=Path(args.binding_source_root) if args.binding_source_root else None,
            finalize_custody=not args.no_finalize_custody,
        )
        if args.output:
            write_json(Path(args.output), result)
        else:
            print(json.dumps(result, indent=2))
    else:
        result = verify_custody_only(Path(args.receipt))
        if args.output:
            write_json(Path(args.output), result)
        else:
            print(json.dumps(result, indent=2))
    return 0 if result.get("result") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
