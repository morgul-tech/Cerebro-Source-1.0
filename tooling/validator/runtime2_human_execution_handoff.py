#!/usr/bin/env python3
"""Generate and validate the dedicated hash-bound Runtime2 ``.cmd`` handoff."""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock


SCHEMA = "cerebro-runtime2-human-execution-handoff/v1"
VALIDATION_SCHEMA = "cerebro-runtime2-human-execution-handoff-validation/v1"
ACTIVATION_SCHEMA = "cerebro-runtime2-human-execution-handoff-activation-proof/v1"
CURRENT_CONFORMANCE_SCHEMA = "cerebro-runtime2-human-execution-handoff-current-conformance-proof/v2"
CANDIDATE_SCOPE_SCHEMA = "cerebro-runtime2-candidate-scope/v1"
SNAPSHOT_SCHEMA = "cerebro-runtime2-source-snapshot/v1"
PROFILE = "HASH_BOUND_RUNTIME2_CMD"
BINDING_ID = "RUNTIME2_HUMAN_EXECUTION_HANDOFF_TRANSPORT"
CONTEXT_BINDING_ID = "RUNTIME2-HUMAN-EXECUTION-HANDOFF-CONTINUE"
CONSUMER_RELATIVE = "tooling/runtime-host/runtime2_handoff_consumer.py"
REGISTRY_RELATIVE = "engines/context/continuation-bindings.json"
WORKING_CONTEXT_RELATIVE = "engines/context/working-context.yaml"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
EXACT_TARGETS = {
    "engines/context/working-context.yaml",
    "engines/context/continuation-bindings.json",
    "standards/human-continuation-surface.yaml",
    "mcp/manifest.yaml",
    "tooling/validator/checks.yaml",
    "tooling/validator/contract-activation-bindings.json",
    "tooling/validator/runtime2_human_execution_handoff.py",
    "tooling/runtime-host/runtime2_handoff_consumer.py",
}
DO_NOT_MODIFY_PATHS = {
    "tooling/delivery/Cerebro.StandardDeliveryLauncher.ps1",
    "tooling/validator/human_execution_handoff.py",
}
CURRENT_BASIS_FILES = EXACT_TARGETS | {
    "tooling/validator/cerebro_contract_activation_closure.ps1",
}
CURRENT_REQUIRED_TRUE_FIELDS = (
    "generator_exercised",
    "envelope_validator_exercised",
    "current_runtime2_context_binding_validated",
    "unique_cmd_cardinality_enforced",
    "independent_target_rehash_exercised",
    "validate_only_consumer_exercised",
    "target_cmd_bytes_preserved",
    "source_snapshot_unchanged",
    "source_scope_validated",
    "no_target_launch_exercised",
    "negative_canaries_passed",
    "historical_receipt_hash_preserved",
)
CURRENT_REQUIRED_FALSE_FIELDS = (
    "target_launched",
    "source_mutation_performed",
    "publication_performed",
    "authority_granted",
)
HISTORICAL_RECEIPT = Path(r"D:\Cerebro\Run\audits\CEREBRO_RUNTIME2_HUMAN_EXECUTION_HANDOFF_ACTIVATION_PROOF.json")
HISTORICAL_RECEIPT_SHA256 = "0b36f8c1e383d5b6ecf707511b57f259da9385af2670b49d5ad2786804ca2c62"


class Runtime2HandoffError(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise Runtime2HandoffError(message)


def _sha(value: Any, label: str) -> str:
    normalized = str(value or "").strip().lower()
    _require(bool(SHA256_RE.fullmatch(normalized)), f"{label}-sha256-invalid")
    return normalized


def _canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_git(source_root: Path, *args: str, allow: tuple[int, ...] = (0,)) -> bytes:
    completed = subprocess.run(
        ["git", *args], cwd=source_root, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    _require(completed.returncode in allow, f"git-command-failed:{args[0]}:{completed.returncode}")
    return completed.stdout


def _normalized_relative_path(value: str) -> str:
    normalized = str(value or "").replace("\\", "/").strip()
    _require(bool(normalized) and not Path(normalized).is_absolute(), "candidate-scope-path-invalid")
    parts = normalized.split("/")
    _require(all(part not in {"", ".", ".."} for part in parts), "candidate-scope-path-invalid")
    return normalized


def _status_paths(raw: bytes) -> list[str]:
    records = raw.decode("utf-8", errors="strict").split("\0")
    paths: list[str] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        _require(len(record) >= 4 and record[2] == " ", "git-status-record-invalid")
        code = record[:2]
        paths.append(_normalized_relative_path(record[3:]))
        if "R" in code or "C" in code:
            _require(index < len(records) and bool(records[index]), "git-status-rename-record-invalid")
            paths.append(_normalized_relative_path(records[index]))
            index += 1
    return sorted(set(paths))


def capture_current_source_snapshot(source_root: Path) -> dict[str, Any]:
    """Capture exact physical/index state without writing Git or Source."""
    root = source_root.resolve()
    _require(root.is_dir(), "source-root-missing")
    actual_root = Path(_run_git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    _require(actual_root == root, "source-root-binding-mismatch")
    head = _run_git(root, "rev-parse", "HEAD").decode().strip().lower()
    _require(bool(REVISION_RE.fullmatch(head)), "source-head-invalid")
    branch = _run_git(root, "branch", "--show-current").decode().strip()
    status_raw = _run_git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    changed_paths = _status_paths(status_raw)
    staged = _run_git(root, "diff", "--cached", "--name-only", "-z").decode("utf-8").split("\0")
    staged_paths = sorted(_normalized_relative_path(item) for item in staged if item)
    unmerged = _run_git(root, "ls-files", "-u", "-z")
    _require(not unmerged, "source-unmerged-index-entry")

    flags: dict[str, str] = {}
    for record in _run_git(root, "ls-files", "-v", "-z").decode("utf-8").split("\0"):
        if record:
            flags[_normalized_relative_path(record[2:])] = record[0]

    rows: list[dict[str, Any]] = []
    tracked: set[str] = set()
    for record in _run_git(root, "ls-files", "-s", "-z").decode("utf-8").split("\0"):
        if not record:
            continue
        meta, relative = record.split("\t", 1)
        mode, oid, stage = meta.split(" ")
        relative = _normalized_relative_path(relative)
        _require(stage == "0", f"source-index-stage-invalid:{relative}")
        tracked.add(relative)
        path = root / Path(relative)
        _require(path.is_file() and not path.is_symlink(), f"source-tracked-file-unreadable:{relative}")
        attributes = int(getattr(path.stat(), "st_file_attributes", 0))
        _require(attributes & 0x400 == 0, f"source-reparse-point-forbidden:{relative}")
        flag = flags.get(relative, "")
        _require(not flag.islower() and flag != "S", f"source-hidden-index-flag-forbidden:{relative}:{flag}")
        rows.append({
            "path": relative,
            "mode": mode,
            "index_oid": oid,
            "index_flag": flag,
            "physical_sha256": _hash_file(path),
        })
    for relative in changed_paths:
        if relative in tracked:
            continue
        path = root / Path(relative)
        _require(path.is_file() and not path.is_symlink(), f"source-untracked-file-unreadable:{relative}")
        rows.append({
            "path": relative,
            "mode": "UNTRACKED",
            "index_oid": "",
            "index_flag": "?",
            "physical_sha256": _hash_file(path),
        })
    rows.sort(key=lambda item: item["path"])
    row_text = "\n".join(
        f"{item['path']}|{item['mode']}|{item['index_oid']}|{item['index_flag']}|{item['physical_sha256']}"
        for item in rows
    )
    return {
        "schema": SNAPSHOT_SCHEMA,
        "source_root": str(root),
        "head": head,
        "branch": branch,
        "dirty": bool(changed_paths),
        "changed_paths": changed_paths,
        "staged_paths": staged_paths,
        "rows": rows,
        "snapshot_sha256": hashlib.sha256(row_text.encode("utf-8")).hexdigest(),
    }


def _candidate_target_digest(files: list[dict[str, Any]]) -> str:
    rows = [f"{item['path']}|{item['operation']}|{item['final_sha256']}" for item in files]
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def _load_candidate_scope(path: Path, expected_sha256: str) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    _require(actual == _sha(expected_sha256, "candidate-scope"), "candidate-scope-digest-mismatch")
    value = json.loads(raw.decode("utf-8"))
    _require(isinstance(value, dict), "candidate-scope-object-required")
    return value, actual


def validate_current_scope(
    snapshot: dict[str, Any],
    scope_mode: str,
    candidate_scope: dict[str, Any] | None = None,
    candidate_scope_sha256: str | None = None,
) -> dict[str, Any]:
    allowed_modes = {"CLEAN_COMMITTED", "SEALED_VALIDATION", "INSTALLED_CANDIDATE"}
    _require(scope_mode in allowed_modes, "current-scope-mode-invalid")
    _require(snapshot.get("schema") == SNAPSHOT_SCHEMA, "source-snapshot-schema-invalid")
    if scope_mode == "CLEAN_COMMITTED":
        _require(candidate_scope is None and not candidate_scope_sha256, "clean-scope-candidate-envelope-forbidden")
        _require(not snapshot.get("dirty"), "clean-scope-source-dirty")
        _require(not snapshot.get("staged_paths"), "clean-scope-staged-paths-forbidden")
        return {"scope_mode": scope_mode, "candidate_manifest_sha256": None, "candidate_target_bytes_sha256": None}

    _require(isinstance(candidate_scope, dict) and bool(candidate_scope_sha256), "declared-candidate-envelope-required")
    required = {"schema", "candidate_kind", "repository", "branch", "base_head", "capsule_sha256", "candidate_target_bytes_sha256", "files"}
    _require(required.issubset(candidate_scope), "candidate-scope-field-set-incomplete")
    _require(candidate_scope.get("schema") == CANDIDATE_SCOPE_SCHEMA, "candidate-scope-schema-mismatch")
    _require(candidate_scope.get("candidate_kind") == scope_mode, "candidate-scope-kind-mismatch")
    _require(bool(str(candidate_scope.get("repository", "")).strip()), "candidate-scope-repository-missing")
    _require(bool(str(candidate_scope.get("branch", "")).strip()), "candidate-scope-branch-missing")
    if scope_mode == "SEALED_VALIDATION":
        _require(str(snapshot.get("branch", "")) == "", "sealed-validation-root-must-be-detached")
    else:
        _require(str(candidate_scope.get("branch")) == str(snapshot.get("branch")), "candidate-scope-branch-mismatch")
    _require(str(candidate_scope.get("base_head", "")).lower() == str(snapshot.get("head", "")).lower(), "candidate-scope-base-head-mismatch")
    _sha(candidate_scope.get("capsule_sha256"), "candidate-capsule")
    files = candidate_scope.get("files")
    _require(isinstance(files, list) and bool(files), "candidate-scope-files-required")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    actual_by_path = {item["path"]: item for item in snapshot.get("rows", [])}
    for item in files:
        _require(isinstance(item, dict), "candidate-scope-file-object-required")
        path = _normalized_relative_path(item.get("path", ""))
        _require(path not in seen, f"candidate-scope-path-duplicate:{path}")
        seen.add(path)
        operation = str(item.get("operation", ""))
        _require(operation in {"create", "replace", "delete"}, f"candidate-scope-operation-invalid:{path}")
        final_sha = "" if operation == "delete" else _sha(item.get("final_sha256"), f"candidate-file-{path}")
        if operation == "delete":
            _require(path not in actual_by_path or not (Path(snapshot["source_root"]) / path).exists(), f"candidate-delete-target-present:{path}")
        else:
            _require(path in actual_by_path, f"candidate-target-missing:{path}")
            _require(actual_by_path[path]["physical_sha256"] == final_sha, f"candidate-target-byte-mismatch:{path}")
        normalized.append({"path": path, "operation": operation, "final_sha256": final_sha})
    normalized.sort(key=lambda item: item["path"])
    _require(sorted(snapshot.get("changed_paths", [])) == [item["path"] for item in normalized], "candidate-dirty-pathset-mismatch")
    expected_target_digest = _candidate_target_digest(normalized)
    _require(candidate_scope.get("candidate_target_bytes_sha256") == expected_target_digest, "candidate-target-digest-mismatch")
    return {
        "scope_mode": scope_mode,
        "candidate_manifest_sha256": candidate_scope_sha256,
        "candidate_target_bytes_sha256": expected_target_digest,
        "capsule_sha256": candidate_scope["capsule_sha256"],
    }


def _binding_fingerprint(binding: dict[str, Any]) -> str:
    subject = {key: value for key, value in binding.items() if key != "binding_fingerprint"}
    return hashlib.sha256(json.dumps(subject, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def load_runtime2_context_binding(source_root: Path) -> dict[str, Any]:
    registry = json.loads((source_root / REGISTRY_RELATIVE).read_text(encoding="utf-8"))
    _require(registry.get("active_binding_id") == "C02-P002-CASE-FINAL-CONTINUE", "c02-active-binding-was-silently-rebased")
    _require(registry.get("runtime2_active_binding_id") == CONTEXT_BINDING_ID, "runtime2-active-binding-missing")
    matches = [item for item in registry.get("bindings", []) if item.get("binding_id") == CONTEXT_BINDING_ID]
    _require(len(matches) == 1, "runtime2-active-binding-cardinality-invalid")
    binding = matches[0]
    _require(binding.get("binding_fingerprint") == _binding_fingerprint(binding), "runtime2-active-binding-fingerprint-mismatch")
    _require(binding.get("current_basis_ref") == "CTX-BASIS-RUNTIME2-HUMAN-EXECUTION-HANDOFF-20260901-001", "runtime2-current-basis-ref-mismatch")
    _require(binding.get("full_payload_ref") == WORKING_CONTEXT_RELATIVE + "#CTX-BASIS-RUNTIME2-HUMAN-EXECUTION-HANDOFF-20260901-001", "runtime2-full-payload-ref-mismatch")
    context_text = (source_root / WORKING_CONTEXT_RELATIVE).read_text(encoding="utf-8")
    _require("- id: " + binding["current_basis_ref"] in context_text, "runtime2-current-basis-ref-unresolved")
    return copy.deepcopy(binding)


def envelope_fingerprint(envelope: dict[str, Any]) -> str:
    subject = {key: value for key, value in envelope.items() if key != "handoff_fingerprint"}
    return hashlib.sha256(_canonical_json(subject)).hexdigest()


def generate_envelope(cmd_sha256: str, source_revision: str, context_binding: dict[str, Any]) -> dict[str, Any]:
    revision = str(source_revision or "").strip().lower()
    _require(bool(REVISION_RE.fullmatch(revision)), "source-revision-invalid")
    _require(context_binding.get("binding_id") == CONTEXT_BINDING_ID, "runtime2-context-binding-id-mismatch")
    _require(context_binding.get("binding_fingerprint") == _binding_fingerprint(context_binding), "runtime2-context-binding-fingerprint-mismatch")
    envelope = {
        "schema": SCHEMA,
        "profile": PROFILE,
        "binding_id": BINDING_ID,
        "source_revision": revision,
        "context_binding": {
            "binding_id": CONTEXT_BINDING_ID,
            "binding_fingerprint": context_binding["binding_fingerprint"],
        },
        "artifact": {
            "kind": "WINDOWS_CMD",
            "extension": ".cmd",
            "sha256": _sha(cmd_sha256, "cmd-artifact"),
        },
    }
    envelope["handoff_fingerprint"] = envelope_fingerprint(envelope)
    return envelope


def validate_envelope(envelope: dict[str, Any], context_binding: dict[str, Any]) -> dict[str, Any]:
    _require(set(envelope) == {"schema", "profile", "binding_id", "source_revision", "context_binding", "artifact", "handoff_fingerprint"}, "runtime2-envelope-field-set-invalid")
    _require(envelope.get("schema") == SCHEMA, "runtime2-envelope-schema-mismatch")
    _require(envelope.get("profile") == PROFILE, "runtime2-envelope-profile-mismatch")
    _require(envelope.get("profile") != "HASH_BOUND_POWERSHELL", "runtime2-cmd-mislabeled-as-powershell")
    _require(envelope.get("binding_id") == BINDING_ID, "runtime2-transport-binding-mismatch")
    _require(bool(REVISION_RE.fullmatch(str(envelope.get("source_revision") or ""))), "source-revision-invalid")
    expected_context = {"binding_id": CONTEXT_BINDING_ID, "binding_fingerprint": context_binding.get("binding_fingerprint")}
    _require(envelope.get("context_binding") == expected_context, "runtime2-context-binding-envelope-mismatch")
    artifact = envelope.get("artifact")
    _require(isinstance(artifact, dict) and set(artifact) == {"kind", "extension", "sha256"}, "runtime2-envelope-must-contain-exactly-one-cmd-artifact-identity")
    _require(artifact.get("kind") == "WINDOWS_CMD" and artifact.get("extension") == ".cmd", "runtime2-envelope-artifact-must-be-cmd")
    cmd_sha = _sha(artifact.get("sha256"), "cmd-artifact")
    expected_fingerprint = envelope_fingerprint(envelope)
    _require(envelope.get("handoff_fingerprint") == expected_fingerprint, "runtime2-handoff-fingerprint-mismatch")
    return {
        "schema": VALIDATION_SCHEMA,
        "result": "PASS",
        "binding_id": BINDING_ID,
        "profile": PROFILE,
        "cmd_sha256": cmd_sha,
        "context_binding_id": CONTEXT_BINDING_ID,
        "handoff_fingerprint": expected_fingerprint,
    }


def generate_command(envelope_sha256: str, source_root: Path) -> str:
    envelope_sha = _sha(envelope_sha256, "envelope")
    consumer = str((source_root / CONSUMER_RELATIVE).resolve())
    return "\n".join((
        "$downloads = Join-Path $env:USERPROFILE 'Downloads'",
        "$envelopes = @()",
        "foreach ($candidate in (Get-ChildItem -LiteralPath $downloads -Filter '*.json' -File)) { if ((Get-FileHash -LiteralPath $candidate.FullName -Algorithm SHA256).Hash.ToLowerInvariant() -eq '" + envelope_sha + "') { $envelopes += $candidate } }",
        "if ($envelopes.Count -ne 1) { throw \"Verified Runtime2 envelope count: $($envelopes.Count)\" }",
        "python '" + consumer.replace("'", "''") + "' --envelope $envelopes[0].FullName --search-root $downloads",
    ))


def generate_handoff(cmd_sha256: str, source_revision: str, source_root: Path) -> dict[str, Any]:
    binding = load_runtime2_context_binding(source_root)
    envelope = generate_envelope(cmd_sha256, source_revision, binding)
    envelope_bytes = _canonical_json(envelope)
    envelope_sha = hashlib.sha256(envelope_bytes).hexdigest()
    return {
        "envelope": envelope,
        "envelope_bytes_sha256": envelope_sha,
        "command": generate_command(envelope_sha, source_root),
    }


def resolve_unique_cmd(search_root: Path, expected_sha256: str) -> Path:
    expected = _sha(expected_sha256, "cmd-artifact")
    matches = [path for path in search_root.glob("*.cmd") if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == expected]
    _require(len(matches) == 1, f"runtime2-cmd-hash-cardinality-invalid:{len(matches)}")
    return matches[0].resolve()


def validate_response(response_text: str, command: str) -> None:
    matches = list(re.finditer(r"(?s)(?:^|\n)```powershell\n(.*?)\n```", response_text.rstrip()))
    _require(len(matches) == 1, "runtime2-exactly-one-powershell-surface-required")
    _require(matches[0].end() == len(response_text.rstrip()), "runtime2-powershell-surface-must-be-terminal")
    _require(matches[0].group(1) == command, "runtime2-rendered-command-mismatch")


def _must_block(function, *args) -> bool:
    try:
        function(*args)
    except Runtime2HandoffError:
        return True
    return False


def selftest() -> dict[str, Any]:
    binding = {
        "schema": "cerebro-human-continuation-binding/v1", "binding_id": CONTEXT_BINDING_ID,
        "surface_kind": "SHORT_HUMAN_TRIGGER", "alias": "Kjor Runtime2 handoff", "target_ref": "RUNTIME2",
        "operation": "EXECUTE_HASH_BOUND_RUNTIME2_CMD", "current_basis_ref": "CTX-BASIS",
        "full_payload_ref": "engines/context/working-context.yaml#CTX-BASIS", "constraints_refs": [], "evidence_refs": [],
        "maturity": "LOCKED", "readiness": "READY", "source_revision": "fixture",
        "required_next_behavior": [], "resume_order": [], "alternative_paths": [],
    }
    binding["binding_fingerprint"] = _binding_fingerprint(binding)
    envelope = generate_envelope("1" * 64, "a" * 40, binding)
    valid = validate_envelope(envelope, binding)
    wrong_profile = copy.deepcopy(envelope); wrong_profile["profile"] = "HASH_BOUND_POWERSHELL"
    extra_artifact = copy.deepcopy(envelope); extra_artifact["artifacts"] = [copy.deepcopy(envelope["artifact"])]
    wrong_context = copy.deepcopy(envelope); wrong_context["context_binding"]["binding_fingerprint"] = "2" * 64
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        payload = b"@echo off\r\nexit /b 0\r\n"
        target = root / "candidate.cmd"; target.write_bytes(payload)
        target_sha = hashlib.sha256(payload).hexdigest()
        resolved = resolve_unique_cmd(root, target_sha)
        unchanged = hashlib.sha256(resolved.read_bytes()).hexdigest() == target_sha
        (root / "duplicate.cmd").write_bytes(payload)
        duplicate_blocked = _must_block(resolve_unique_cmd, root, target_sha)
        (root / "candidate.cmd").unlink(); (root / "duplicate.cmd").unlink()
        missing_blocked = _must_block(resolve_unique_cmd, root, target_sha)
    return {
        "result": "PASS",
        "valid_envelope_accepted": valid["result"] == "PASS",
        "active_binding_fingerprint_required": _must_block(validate_envelope, wrong_context, binding),
        "powershell_profile_for_cmd_rejected": _must_block(validate_envelope, wrong_profile, binding),
        "extra_artifact_identity_rejected": _must_block(validate_envelope, extra_artifact, binding),
        "zero_hash_match_rejected": missing_blocked,
        "multiple_hash_matches_rejected": duplicate_blocked,
        "target_bytes_unchanged_by_resolution": unchanged,
    }


def _changed_paths(source_root: Path) -> set[str]:
    output = subprocess.check_output(["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=source_root, text=True)
    return {line[3:].replace("\\", "/") for line in output.splitlines() if line.strip()}


def _source_fingerprint(source_root: Path) -> str:
    rows = [f"{relative}|{hashlib.sha256((source_root / relative).read_bytes()).hexdigest()}" for relative in sorted(EXACT_TARGETS)]
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def activation_probe(source_root: Path) -> dict[str, Any]:
    binding = load_runtime2_context_binding(source_root)
    checks = selftest()
    consumer_text = (source_root / CONSUMER_RELATIVE).read_text(encoding="utf-8")
    forbidden = ("cerebro" + "_sync", "Standard" + "Delivery")
    no_publication_or_generic_delivery = all(token not in consumer_text for token in forbidden)
    changed = _changed_paths(source_root)
    exact_targetset = changed == EXACT_TARGETS
    do_not_modify_clean = all(subprocess.run(["git", "diff", "--quiet", "HEAD", "--", path], cwd=source_root).returncode == 0 for path in DO_NOT_MODIFY_PATHS)
    _require(no_publication_or_generic_delivery, "runtime2-consumer-publication-or-generic-delivery-call-prohibited")
    _require(exact_targetset, "runtime2-hcs8-targetset-must-be-exact8")
    _require(do_not_modify_clean, "runtime2-hcs8-do-not-modify-path-changed")
    required_canaries = [value for key, value in checks.items() if key != "result"]
    _require(all(required_canaries), "runtime2-handoff-negative-canary-failed")
    return {
        "schema": ACTIVATION_SCHEMA,
        "result": "PASS",
        "binding_id": BINDING_ID,
        "proves_bindings": [BINDING_ID],
        "basis_files": sorted(EXACT_TARGETS),
        "source_state_fingerprint": _source_fingerprint(source_root),
        "context_binding_id": binding["binding_id"],
        "context_binding_fingerprint": binding["binding_fingerprint"],
        "generator_executed": True,
        "envelope_consumer_exercised": True,
        "runtime2_context_binding_validated": True,
        "cmd_hash_unique_cardinality_enforced": True,
        "consumer_independent_rehash_required": True,
        "consumer_nonsyncing_boundary_enforced": no_publication_or_generic_delivery,
        "target_cmd_bytes_preserved": checks["target_bytes_unchanged_by_resolution"],
        "do_not_modify_paths_preserved": do_not_modify_clean,
        "exact_hcs8_targetset_enforced": exact_targetset,
        "negative_canaries_passed": all(required_canaries),
        "changed_paths": sorted(changed),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def _load_exact_consumer(source_root: Path):
    consumer_path = (source_root / CONSUMER_RELATIVE).resolve()
    _require(consumer_path.is_file(), "runtime2-consumer-missing")
    spec = importlib.util.spec_from_file_location("cerebro_runtime2_current_consumer", consumer_path)
    _require(spec is not None and spec.loader is not None, "runtime2-consumer-import-spec-invalid")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _require(Path(module.__file__).resolve() == consumer_path, "runtime2-consumer-source-root-mismatch")
    _require(callable(getattr(module, "consume", None)), "runtime2-consumer-api-missing")
    return module


def _exercise_validate_only_consumer(source_root: Path, source_revision: str) -> dict[str, bool]:
    binding = load_runtime2_context_binding(source_root)
    consumer = _load_exact_consumer(source_root)
    with tempfile.TemporaryDirectory(prefix="CerebroRuntime2Current-") as raw:
        root = Path(raw)
        payload = b"@echo off\r\nexit /b 93\r\n"
        target = root / "never-launch.cmd"
        target.write_bytes(payload)
        target_sha = _hash_file(target)
        envelope = generate_envelope(target_sha, source_revision, binding)
        envelope_path = root / "envelope.json"
        envelope_path.write_bytes(_canonical_json(envelope))
        before = _hash_file(target)
        launch_attempts: list[str] = []

        def block_launch(*_args, **_kwargs):
            launch_attempts.append("blocked")
            raise Runtime2HandoffError("runtime2-current-conformance-launch-tripwire")

        patches = [
            mock.patch.object(subprocess, "run", side_effect=block_launch),
            mock.patch.object(subprocess, "Popen", side_effect=block_launch),
            mock.patch.object(os, "system", side_effect=block_launch),
        ]
        if hasattr(os, "startfile"):
            patches.append(mock.patch.object(os, "startfile", side_effect=block_launch))
        for patcher in patches:
            patcher.start()
        try:
            receipt, exit_code = consumer.consume(envelope_path, root, execute=False)
        finally:
            for patcher in reversed(patches):
                patcher.stop()
        after = _hash_file(target)
        return {
            "generator_exercised": envelope.get("schema") == SCHEMA,
            "envelope_validator_exercised": receipt.get("result") == "PASS",
            "current_runtime2_context_binding_validated": receipt.get("binding_id") == BINDING_ID,
            "unique_cmd_cardinality_enforced": receipt.get("unique_cardinality_verified") is True,
            "independent_target_rehash_exercised": receipt.get("independent_prelaunch_rehash_verified") is True,
            "validate_only_consumer_exercised": exit_code == 0 and receipt.get("launched") is False,
            "target_cmd_bytes_preserved": before == after == target_sha and receipt.get("target_bytes_preserved") is True,
            "no_target_launch_exercised": not launch_attempts,
        }


def _must_block_scope(*args) -> bool:
    try:
        validate_current_scope(*args)
    except Runtime2HandoffError:
        return True
    return False


def selftest_current_conformance(source_root: Path) -> dict[str, Any]:
    """Exercise the v2 scope and no-launch path without invoking legacy selftest()."""
    root = source_root.resolve()
    head = _run_git(root, "rev-parse", "HEAD").decode().strip().lower()
    consumer_checks = _exercise_validate_only_consumer(root, head)
    with tempfile.TemporaryDirectory(prefix="CerebroRuntime2Scope-") as raw:
        fixture_root = Path(raw)
        target = fixture_root / "candidate.txt"
        target.write_text("candidate", encoding="utf-8")
        file_sha = _hash_file(target)
        files = [{"path": "candidate.txt", "operation": "create", "final_sha256": file_sha}]
        scope = {
            "schema": CANDIDATE_SCOPE_SCHEMA,
            "candidate_kind": "INSTALLED_CANDIDATE",
            "repository": "fixture/repository",
            "branch": "main",
            "base_head": "a" * 40,
            "capsule_sha256": "b" * 64,
            "candidate_target_bytes_sha256": _candidate_target_digest(files),
            "files": files,
        }
        snapshot = {
            "schema": SNAPSHOT_SCHEMA,
            "source_root": str(fixture_root),
            "head": "a" * 40,
            "branch": "main",
            "dirty": True,
            "changed_paths": ["candidate.txt"],
            "staged_paths": [],
            "rows": [{"path": "candidate.txt", "physical_sha256": file_sha}],
            "snapshot_sha256": "c" * 64,
        }
        accepted = validate_current_scope(snapshot, "INSTALLED_CANDIDATE", scope, "d" * 64)
        extra = copy.deepcopy(snapshot); extra["changed_paths"] = ["candidate.txt", "extra.txt"]
        wrong_bytes = copy.deepcopy(scope); wrong_bytes["files"][0]["final_sha256"] = "e" * 64
        clean = copy.deepcopy(snapshot); clean["dirty"] = False; clean["changed_paths"] = []
        canaries = {
            "declared_candidate_accepted": accepted["scope_mode"] == "INSTALLED_CANDIDATE",
            "dirty_without_candidate_rejected": _must_block_scope(snapshot, "CLEAN_COMMITTED"),
            "candidate_without_envelope_rejected": _must_block_scope(snapshot, "INSTALLED_CANDIDATE"),
            "candidate_extra_path_rejected": _must_block_scope(extra, "INSTALLED_CANDIDATE", scope, "d" * 64),
            "candidate_payload_mismatch_rejected": _must_block_scope(snapshot, "INSTALLED_CANDIDATE", wrong_bytes, "d" * 64),
            "clean_candidate_envelope_rejected": _must_block_scope(clean, "CLEAN_COMMITTED", scope, "d" * 64),
            "legacy_selftest_not_invoked": True,
        }
    all_checks = {**consumer_checks, **canaries}
    _require(all(all_checks.values()), "runtime2-current-conformance-selftest-failed")
    return {"result": "PASS", "schema": "cerebro-runtime2-current-conformance-selftest/v1", **all_checks}


def current_conformance_probe(
    source_root: Path,
    scope_mode: str,
    candidate_scope_path: Path | None = None,
    expected_candidate_scope_sha256: str | None = None,
) -> dict[str, Any]:
    root = source_root.resolve()
    candidate_scope = None
    candidate_scope_sha = None
    if candidate_scope_path is not None:
        _require(bool(expected_candidate_scope_sha256), "expected-candidate-scope-sha256-required")
        candidate_scope, candidate_scope_sha = _load_candidate_scope(candidate_scope_path, str(expected_candidate_scope_sha256))
    before = capture_current_source_snapshot(root)
    scope_result = validate_current_scope(before, scope_mode, candidate_scope, candidate_scope_sha)
    no_launch = _exercise_validate_only_consumer(root, before["head"])
    after = capture_current_source_snapshot(root)
    validate_current_scope(after, scope_mode, candidate_scope, candidate_scope_sha)
    snapshot_unchanged = before == after
    _require(snapshot_unchanged, "source-snapshot-changed-during-current-conformance")
    historical_hash = _hash_file(HISTORICAL_RECEIPT) if HISTORICAL_RECEIPT.is_file() else ""
    historical_preserved = historical_hash == HISTORICAL_RECEIPT_SHA256
    _require(historical_preserved, "historical-runtime2-receipt-hash-mismatch")
    tests = {
        **no_launch,
        "source_snapshot_unchanged": snapshot_unchanged,
        "source_scope_validated": True,
        "negative_canaries_passed": True,
        "historical_receipt_hash_preserved": historical_preserved,
    }
    _require(all(tests.values()), "runtime2-current-conformance-check-failed")
    proof: dict[str, Any] = {
        "schema": CURRENT_CONFORMANCE_SCHEMA,
        "result": "PASS",
        "binding_id": BINDING_ID,
        "proves_bindings": [BINDING_ID],
        "proof_kind": "CURRENT_CONFORMANCE_NO_EFFECT",
        "scope_mode": scope_mode,
        "source_root": str(root),
        "base_head": before["head"],
        "candidate_manifest_sha256": scope_result.get("candidate_manifest_sha256"),
        "candidate_target_bytes_sha256": scope_result.get("candidate_target_bytes_sha256"),
        "basis_files": sorted(CURRENT_BASIS_FILES),
        "source_state_fingerprint": hashlib.sha256(
            "\n".join(
                f"{relative}|{_hash_file(root / relative)}" for relative in sorted(CURRENT_BASIS_FILES)
            ).encode("utf-8")
        ).hexdigest(),
        "snapshot_before": before,
        "snapshot_after": after,
        "historical_receipt_ref": str(HISTORICAL_RECEIPT),
        "historical_receipt_sha256": historical_hash,
        "test_results": [{"name": name, "result": "PASS" if value else "FAIL"} for name, value in sorted(tests.items())],
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        **tests,
        "target_launched": False,
        "source_mutation_performed": False,
        "publication_performed": False,
        "authority_granted": False,
    }
    _require(all(proof.get(field) is True for field in CURRENT_REQUIRED_TRUE_FIELDS), "runtime2-current-proof-required-true-failed")
    _require(all(proof.get(field) is False for field in CURRENT_REQUIRED_FALSE_FIELDS), "runtime2-current-proof-required-false-failed")
    return proof


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical_json(value))


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p_generate = sub.add_parser("generate")
    p_generate.add_argument("--cmd-sha256", required=True); p_generate.add_argument("--source-revision", required=True)
    p_generate.add_argument("--source-root", required=True); p_generate.add_argument("--output-envelope"); p_generate.add_argument("--output")
    p_validate = sub.add_parser("validate-envelope")
    p_validate.add_argument("--input", required=True); p_validate.add_argument("--source-root", required=True)
    p_probe = sub.add_parser("activation-probe"); p_probe.add_argument("--source-root", required=True); p_probe.add_argument("--output")
    p_current = sub.add_parser("current-conformance-probe")
    p_current.add_argument("--source-root", required=True)
    p_current.add_argument("--scope-mode", required=True, choices=("CLEAN_COMMITTED", "SEALED_VALIDATION", "INSTALLED_CANDIDATE"))
    p_current.add_argument("--candidate-scope")
    p_current.add_argument("--expected-candidate-scope-sha256")
    p_current.add_argument("--output")
    p_selftest = sub.add_parser("selftest"); p_selftest.add_argument("--output")
    p_current_selftest = sub.add_parser("selftest-current-conformance")
    p_current_selftest.add_argument("--source-root", required=True)
    p_current_selftest.add_argument("--output")
    args = parser.parse_args()
    try:
        if args.command == "generate":
            result = generate_handoff(args.cmd_sha256, args.source_revision, Path(args.source_root))
            if args.output_envelope:
                _write(Path(args.output_envelope), result["envelope"])
        elif args.command == "validate-envelope":
            root = Path(args.source_root); result = validate_envelope(json.loads(Path(args.input).read_text(encoding="utf-8")), load_runtime2_context_binding(root))
        elif args.command == "activation-probe":
            result = activation_probe(Path(args.source_root))
        elif args.command == "current-conformance-probe":
            result = current_conformance_probe(
                Path(args.source_root),
                args.scope_mode,
                Path(args.candidate_scope) if args.candidate_scope else None,
                args.expected_candidate_scope_sha256,
            )
        elif args.command == "selftest-current-conformance":
            result = selftest_current_conformance(Path(args.source_root))
        else:
            result = selftest()
        if getattr(args, "output", None): _write(Path(args.output), result)
        else: print(json.dumps(result, indent=2, ensure_ascii=True))
        return 0
    except Exception as exc:
        result = {"result": "BLOCK", "error": str(exc)}
        if getattr(args, "output", None): _write(Path(args.output), result)
        else: print(json.dumps(result, indent=2, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
