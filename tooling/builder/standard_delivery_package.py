#!/usr/bin/env python3
"""Deterministic, non-authoritative Standard Delivery package builder.

This builder emits bytes only under a separately bound Run-only package-build
authorization. Validation does not grant publication, Source delivery, or S1a.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable
from package_build_verifier import PackageBuildVerifier, VerifierError


INPUT_SCHEMA = "cerebro-standard-delivery-package-input/v1"
MANIFEST_SCHEMA = "cerebro-standard-delivery-manifest/v1"
CAPSULE_SCHEMA = "cerebro-change-capsule/v0.2"
IDENTITY_SCHEMA = "cerebro-exact15-candidate-identity-manifest/v1"
IDENTITY_CAPSULE_SCHEMA = "cerebro-exact15-candidate-capsule/v1"
REPOSITORY = "morgul-tech/Cerebro-Source-1.0"
BRANCH = "main"
FILE_COUNT = 15
EXACT35_PROFILE = "EXACT35_COMMIT_1886"
EXACT35_BASE = "7321e4592eac0940a8b075c1932765a9c4d046d8"
EXACT35_CANDIDATE = "1886c0c1aa58e5734fc4149a11781cf0f0588375"
EXACT35_TREE = "9493601ab89c4025a5e794cb21cf9b31d862ad74"
EXACT35_AUDIT_SHA256 = "359b11809a5e7a138e6e6b6ac4989185ee3c962c7f8acd9c7a5a195061e8f308"
EXACT35_REPLACE = "tooling/validator/test_component_inventory.py"
BUILDER_EXTENSION_PATHS = (
    "tooling/builder/component.yaml", "tooling/builder/standard_delivery_package.py")
P813_COMPONENT_SHA256 = "b6c5a24883ae14690d4d074cda99b4545a3bb39ad6b689bddc765e45fa6e8ff6"
DIRTY17_PROFILE = "P813_DIRTY17_CANDIDATE"
EXTENSION_RECEIPT_SCHEMA = "cerebro-builder-extension-currentness-receipt/v1"
HEX40 = re.compile(r"^[0-9a-fA-F]{40}$")
HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
MANIFEST_REQUIRED = (
    "patch_id", "campaign_id", "package_class", "intended_consequence_class",
    "delivery_profile", "delivery_execution_contract", "kernel_sha256",
    "commit_message", "assurance_kernel", "delivery_control_binding",
    "human_execution_handoff", "campaign_closeout", "material_commitment_preflight",
    "target_runtime_validation", "activation_probes", "required_text_assertions",
    "qualification_basis",
)


class BuilderError(ValueError):
    """A fail-closed input, currentness, or output violation."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise BuilderError(message)


def nonempty(value: Any, name: str) -> str:
    require(isinstance(value, str) and bool(value.strip()), f"{name} required")
    return value


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git_blob(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()


def json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def read_json(path: Path) -> dict[str, Any]:
    try:
        result = json.loads(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise BuilderError(f"invalid JSON: {path}: {exc}") from exc
    require(isinstance(result, dict), f"JSON object required: {path}")
    return result


def relative(value: Any, name: str) -> str:
    raw = nonempty(value, name)
    require("\\" not in raw and ":" not in raw and not raw.startswith("/"), f"unsafe {name}")
    parts = PurePosixPath(raw).parts
    require(parts and all(part not in ("", ".", "..") for part in parts), f"unsafe {name}")
    normalized = "/".join(parts)
    require(normalized == raw, f"noncanonical {name}")
    return normalized


def beneath(root: Path, path: str) -> Path:
    target = (root / Path(path)).resolve()
    require(target.is_relative_to(root.resolve()), f"path escapes root: {path}")
    return target


def command(cwd: Path, *args: str) -> str:
    completed = subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False)
    require(completed.returncode == 0, f"command failed: {args[0]} {args[1:]}: {completed.stderr.strip()}")
    return completed.stdout.strip()


def git_bytes(cwd: Path, revision: str, path: str) -> bytes:
    completed = subprocess.run(["git", "show", f"{revision}:{path}"], cwd=cwd,
                               capture_output=True, check=False)
    require(completed.returncode == 0, f"Git object unreadable: {revision}:{path}")
    return completed.stdout


def exact35_candidate(declared: dict[str, Any]) -> tuple[Path, list[dict[str, Any]], str]:
    """Pin current main separately from the historical exact35 artifact parent."""
    source, identity = declared["source"], declared["identity"]
    require(identity.get("profile") == EXACT35_PROFILE, "exact35 profile required")
    root = Path(nonempty(source.get("root"), "source.root")).resolve()
    require(root.is_dir() and source.get("repository") == REPOSITORY and source.get("branch") == BRANCH,
            "exact35 Source binding mismatch")
    require(Path(command(root, "git", "rev-parse", "--show-toplevel")).resolve() == root,
            "exact35 Source root is not Git top level")
    origin_url = command(root, "git", "remote", "get-url", "origin").lower().replace("\\", "/").rstrip("/")
    require(origin_url.removesuffix(".git").endswith("morgul-tech/cerebro-source-1.0"),
            "exact35 origin repository mismatch")
    require(source.get("base_commit") == EXACT35_BASE and source.get("candidate_commit") == EXACT35_CANDIDATE
            and source.get("candidate_tree") == EXACT35_TREE, "exact35 pinned identity mismatch")
    require(command(root, "git", "symbolic-ref", "--short", "HEAD") == BRANCH,
            "exact35 requires current main checkout")
    remote = command(root, "git", "ls-remote", "origin", "refs/heads/main").split()
    current_main = str(source.get("current_main_commit", "")).lower()
    require(bool(HEX40.fullmatch(current_main)), "exact35 current main commit required")
    require(bool(remote) and command(root, "git", "rev-parse", "HEAD").lower()
            == command(root, "git", "rev-parse", "origin/main").lower()
            == remote[0].lower() == current_main, "exact35 main currentness mismatch")
    require(not command(root, "git", "status", "--porcelain=v1", "--untracked-files=all"),
            "exact35 Source checkout must be clean")
    parents = command(root, "git", "rev-list", "--parents", "-n", "1", EXACT35_CANDIDATE).split()
    require(parents == [EXACT35_CANDIDATE, EXACT35_BASE], "exact35 candidate parent mismatch")
    require(command(root, "git", "rev-parse", f"{EXACT35_CANDIDATE}^{{tree}}").lower() == EXACT35_TREE,
            "exact35 candidate tree mismatch")
    audit_arg = Path(nonempty(identity.get("audit_path"), "identity.audit_path"))
    require(not audit_arg.is_symlink(), "exact35 audit symlink unsafe")
    audit_path = audit_arg.resolve()
    require(audit_path.is_file() and not audit_path.is_relative_to(root), "exact35 audit missing/unsafe")
    require(identity.get("audit_sha256") == EXACT35_AUDIT_SHA256
            and digest(audit_path.read_bytes()) == EXACT35_AUDIT_SHA256, "exact35 audit hash mismatch")
    audit = read_json(audit_path)
    require(audit.get("schema") == "c1043-p1651-exact-35-path-manifest/v1"
            and audit.get("base_head") == EXACT35_BASE
            and audit.get("candidate_head") == EXACT35_CANDIDATE
            and audit.get("candidate_tree") == EXACT35_TREE
            and audit.get("sole_parent") == EXACT35_BASE
            and audit.get("changed_paths") == 35, "exact35 audit identity mismatch")
    audit_rows = audit.get("rows")
    require(isinstance(audit_rows, list) and len(audit_rows) == 35, "exact35 audit rows required")
    audit_by_path: dict[str, dict[str, Any]] = {}
    for row in audit_rows:
        require(isinstance(row, dict), "exact35 audit row malformed")
        path = relative(row.get("path"), "audit.path")
        require(path not in audit_by_path, f"duplicate exact35 audit path: {path}")
        audit_by_path[path] = row
    raw_diff = subprocess.run(["git", "diff-tree", "--no-commit-id", "--name-status", "--no-renames",
                               "-r", "-z", EXACT35_BASE, EXACT35_CANDIDATE], cwd=root,
                              capture_output=True, check=False)
    require(raw_diff.returncode == 0 and raw_diff.stdout.endswith(b"\0"), "exact35 Git diff unavailable")
    tokens = raw_diff.stdout[:-1].split(b"\0")
    require(len(tokens) == 70, "exact35 Git diff count mismatch")
    files: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index in range(0, len(tokens), 2):
        try:
            status, raw_path = tokens[index].decode("ascii"), tokens[index + 1].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BuilderError("exact35 Git diff encoding invalid") from exc
        path = relative(raw_path, "diff.path")
        require(path not in seen, f"duplicate exact35 Git path: {path}")
        seen.add(path)
        operation = "replace" if path == EXACT35_REPLACE else "create"
        require(status == ("M" if operation == "replace" else "A"), f"exact35 operation mismatch: {path}")
        entry = command(root, "git", "ls-tree", EXACT35_CANDIDATE, "--", path).split()
        require(len(entry) >= 3 and entry[0] in {"100644", "100755"} and entry[1] == "blob",
                f"exact35 nonregular Git object: {path}")
        blob = entry[2].lower()
        data = git_bytes(root, EXACT35_CANDIDATE, path)
        require(git_blob(data) == blob, f"exact35 candidate blob mismatch: {path}")
        row = audit_by_path.get(path)
        require(row is not None and str(row.get("git_blob", "")).lower() == blob
                and str(row.get("physical_sha256", "")).lower() == digest(data),
                f"exact35 audit differs from Git: {path}")
        record = {"path": path, "operation": operation, "sha256": digest(data),
                  "final_git_blob_sha": blob}
        baseline = command(root, "git", "ls-tree", EXACT35_BASE, "--", path).split()
        if operation == "create":
            require(not baseline, f"exact35 create baseline exists: {path}")
        else:
            require(len(baseline) >= 3 and baseline[0] in {"100644", "100755"}
                    and baseline[1] == "blob", "exact35 replacement baseline invalid")
            record["expected_git_blob_sha"] = baseline[2].lower()
        files.append(record)
    files.sort(key=lambda item: item["path"])
    require(set(audit_by_path) == seen and len(files) == 35
            and sum(item["operation"] == "create" for item in files) == 34
            and sum(item["operation"] == "replace" for item in files) == 1,
            "exact35 pathset/operation count mismatch")
    declared_files = identity.get("files")
    if declared_files is not None:
        require(declared_files == files, "exact35 declarative files differ from Git")
    rendered = "\n".join(f'{item["path"]}|{item["operation"]}|{item["sha256"]}' for item in files)
    target_digest = digest(rendered.encode("utf-8"))
    require(identity.get("target_bytes_sha256") == target_digest, "exact35 target digest mismatch")
    return root, files, target_digest


def source_currentness(source: dict[str, Any], files: list[dict[str, Any]]) -> Path:
    root = Path(nonempty(source.get("root"), "source.root")).resolve()
    require(root.is_dir(), "source root missing")
    require(source.get("repository") == REPOSITORY and source.get("branch") == BRANCH,
            "authoritative Source binding mismatch")
    base = nonempty(source.get("base_commit"), "source.base_commit").lower()
    require(bool(HEX40.fullmatch(base)), "base commit must be 40 hex")
    require(Path(command(root, "git", "rev-parse", "--show-toplevel")).resolve() == root,
            "Source root is not Git top level")
    origin_url = command(root, "git", "remote", "get-url", "origin").lower().replace("\\", "/").rstrip("/")
    require(origin_url.removesuffix(".git").endswith("morgul-tech/cerebro-source-1.0"),
            "origin repository mismatch")
    head = command(root, "git", "rev-parse", "HEAD").lower()
    origin = command(root, "git", "rev-parse", "origin/main").lower()
    remote_rows = command(root, "git", "ls-remote", "origin", "refs/heads/main").split()
    require(bool(remote_rows), "remote main not resolved")
    remote = remote_rows[0].lower()
    require(head == origin == remote == base, "HEAD/origin/remote/base drift")
    require(command(root, "git", "symbolic-ref", "--short", "HEAD") == BRANCH,
            "actual Source branch mismatch")
    expected_status = {item["path"]: " M" for item in files}
    require(len(expected_status) == FILE_COUNT, "exact15 Source paths required")
    currentness = source.get("currentness_contract")
    if currentness is not None:
        require(isinstance(currentness, dict) and currentness.get("profile") == DIRTY17_PROFILE,
                "unsupported Source currentness profile")
        receipt_argument = Path(nonempty(currentness.get("extension_receipt_path"),
                                         "currentness.extension_receipt_path"))
        require(not receipt_argument.is_symlink(), "extension receipt symlink unsafe")
        receipt_path = receipt_argument.resolve()
        receipt_hash = str(currentness.get("extension_receipt_sha256", "")).lower()
        require(bool(HEX64.fullmatch(receipt_hash)), "extension receipt hash invalid")
        require(receipt_path.is_file() and not receipt_path.is_symlink()
                and not receipt_path.is_relative_to(root), "extension receipt missing/unsafe")
        require(digest(receipt_path.read_bytes()) == receipt_hash, "extension receipt hash mismatch")
        receipt = read_json(receipt_path)
        require(receipt.get("schema") == EXTENSION_RECEIPT_SCHEMA
                and receipt.get("base_commit") == base, "extension receipt binding mismatch")
        extension = receipt.get("files")
        require(isinstance(extension, list) and len(extension) == 2,
                "exact2 Builder extension receipt required")
        declared_extension = currentness.get("extension_files")
        require(isinstance(declared_extension, list) and declared_extension == extension,
                "declared exact2 extension differs from pinned receipt")
        extension_by_path: dict[str, dict[str, Any]] = {}
        for item in extension:
            require(isinstance(item, dict), "extension file record must be object")
            path = relative(item.get("path"), "extension.path")
            require(path not in extension_by_path, f"duplicate extension path: {path}")
            extension_by_path[path] = item
        require(tuple(sorted(extension_by_path)) == BUILDER_EXTENSION_PATHS
                and not set(extension_by_path).intersection(expected_status),
                "Builder extension pathset not exact2/disjoint")
        for path in BUILDER_EXTENSION_PATHS:
            item = extension_by_path[path]
            status = " M" if path.endswith("component.yaml") else "??"
            require(item.get("git_status") == status, f"extension status binding mismatch: {path}")
            expected_hash = str(item.get("sha256", "")).lower()
            require(bool(HEX64.fullmatch(expected_hash)), f"extension SHA256 invalid: {path}")
            if path == BUILDER_EXTENSION_PATHS[0]:
                require(expected_hash == P813_COMPONENT_SHA256, "P813 component lock drift")
            target = beneath(root, path)
            require(target.is_file() and not target.is_symlink(),
                    f"physical Builder extension missing/unsafe: {path}")
            require(digest(target.read_bytes()) == expected_hash,
                    f"physical Builder extension drift: {path}")
            expected_status[path] = status
    status_result = subprocess.run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                                   cwd=root, capture_output=True, check=False)
    require(status_result.returncode == 0, "Source status failed")
    raw = status_result.stdout.split(b"\0")
    require(raw[-1] == b"", "Source porcelain status malformed")
    observed_status: dict[str, str] = {}
    for entry in raw[:-1]:
        require(len(entry) >= 4 and entry[2:3] == b" ", "Source status ambiguous")
        try:
            status, path = entry[:2].decode("ascii"), entry[3:].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BuilderError("Source status path/code not UTF-8/ASCII") from exc
        require(path not in observed_status, f"duplicate Source status path: {path}")
        observed_status[path] = status
    require(observed_status == expected_status,
            f"Source dirty status not exact{len(expected_status)}: "
            f"observed={sorted(observed_status.items())} expected={sorted(expected_status.items())}")
    require(not command(root, "git", "diff", "--cached", "--name-only"), "staged Source changes")
    return root


def load_declared_input(path: Path) -> dict[str, Any]:
    declared = read_json(path)
    require(declared.get("schema") == INPUT_SCHEMA, "declarative input schema mismatch")
    for key in ("source", "identity", "standard_manifest", "change", "output", "authorization"):
        require(isinstance(declared.get(key), dict), f"{key} object required")
    auth = declared["authorization"]
    require(auth.get("effect") == "RUN_ONLY_PACKAGE_BUILD" and auth.get("pm_bound") is True,
            "separate PM-bound Run-only package-build authorization required")
    nonempty(auth.get("packet"), "authorization.packet")
    nonempty(auth.get("claim"), "authorization.claim")
    require(declared["output"].get("authority") == "NON_AUTHORITATIVE_GENERATED_EVIDENCE",
            "output must remain non-authoritative")
    if declared["identity"].get("profile") == EXACT35_PROFILE:
        exact35_evidence(declared)
    return declared


def exact35_evidence(declared: dict[str, Any]) -> None:
    """Check candidate-bound evidence consistency; hashes alone confer no PM authority."""
    auth = declared["authorization"]
    require(auth.get("source_base") == EXACT35_BASE
            and auth.get("candidate_commit") == EXACT35_CANDIDATE
            and auth.get("candidate_tree") == EXACT35_TREE,
            "exact35 authorization identity missing/mismatched")
    root = Path(nonempty(declared["source"].get("root"), "source.root")).resolve()
    proof_arg = Path(nonempty(auth.get("qualification_report_path"),
                              "authorization.qualification_report_path"))
    require(not proof_arg.is_symlink(), "exact35 qualification report symlink unsafe")
    proof_path = proof_arg.resolve()
    proof_sha = str(auth.get("qualification_report_sha256", "")).lower()
    require(bool(HEX64.fullmatch(proof_sha)) and proof_path.is_file()
            and not proof_path.is_relative_to(root)
            and digest(proof_path.read_bytes()) == proof_sha,
            "exact35 qualification report missing/hash mismatch")
    proof = read_json(proof_path)
    require(proof.get("result") == "PASS"
            and proof.get("source_base") == EXACT35_BASE
            and proof.get("candidate_commit") == EXACT35_CANDIDATE
            and proof.get("candidate_tree") == EXACT35_TREE,
            "exact35 qualification report not candidate-bound PASS")
    basis = declared["standard_manifest"].get("qualification_basis", {})
    require(isinstance(basis, dict) and basis.get("result") == "PASS"
            and basis.get("report_sha256") == proof_sha,
            "exact35 manifest qualification basis not bound to report")
    control = declared["standard_manifest"].get("delivery_control_binding", {})
    require(isinstance(control, dict) and control.get("source_commit") == EXACT35_BASE
            and control.get("candidate_commit") == EXACT35_CANDIDATE
            and control.get("candidate_tree") == EXACT35_TREE,
            "exact35 control binding not candidate-bound")


def exact35_authority(declared: dict[str, Any],
                      trusted_check: Callable[[dict[str, Any]], dict[str, Any]] | None) -> None:
    """A JSON field or caller-supplied hash cannot authenticate a PM decision.

    The CLI binds an owner-configured authenticated HTTPS verifier. Process
    configuration is outside declarative input; absent configuration holds.
    """
    require(trusted_check is not None, "EXACT35_AUTHORITY_VERIFIER_UNBOUND")
    receipt = trusted_check(declared)
    auth = declared["authorization"]
    require(isinstance(receipt, dict) and receipt.get("result") == "PASS"
            and receipt.get("effect") == "RUN_ONLY_PACKAGE_BUILD"
            and receipt.get("packet") == auth["packet"]
            and receipt.get("claim") == auth["claim"]
            and receipt.get("source_base") == EXACT35_BASE
            and receipt.get("candidate_commit") == EXACT35_CANDIDATE
            and receipt.get("candidate_tree") == EXACT35_TREE
            and receipt.get("qualification_report_sha256") == auth["qualification_report_sha256"],
            "EXACT35_AUTHORITY_VERIFIER_REJECTED")


def verify_source_and_p801_lock(declared: dict[str, Any]) -> tuple[Path, list[dict[str, Any]], str]:
    identity = declared["identity"]
    rows = identity.get("files")
    require(isinstance(rows, list) and len(rows) == FILE_COUNT, "exact15 records required")
    verified: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in rows:
        require(isinstance(item, dict), "file record must be object")
        path = relative(item.get("path"), "file.path")
        require(path not in seen, f"duplicate path: {path}")
        seen.add(path)
        require(item.get("operation") == "replace", f"replace required: {path}")
        for field, pattern in (("sha256", HEX64), ("expected_git_blob_sha", HEX40),
                               ("final_git_blob_sha", HEX40)):
            require(bool(pattern.fullmatch(str(item.get(field, "")))), f"{field} invalid: {path}")
        verified.append({"path": path, "operation": "replace",
                         "sha256": item["sha256"].lower(),
                         "expected_git_blob_sha": item["expected_git_blob_sha"].lower(),
                         "final_git_blob_sha": item["final_git_blob_sha"].lower()})
    verified.sort(key=lambda item: item["path"])
    target_material = "\n".join(f'{item["path"]}|replace|{item["sha256"]}' for item in verified)
    target_digest = digest(target_material.encode("utf-8"))
    require(target_digest == str(identity.get("target_bytes_sha256", "")).lower(),
            "exact15 target-bytes digest mismatch")
    root = source_currentness(declared["source"], verified)
    for item in verified:
        path = beneath(root, item["path"])
        require(path.is_file() and not path.is_symlink(), f"physical Source file missing/unsafe: {item['path']}")
        data = path.read_bytes()
        require(digest(data) == item["sha256"] and git_blob(data) == item["final_git_blob_sha"],
                f"final physical identity mismatch: {item['path']}")
        baseline = command(root, "git", "rev-parse", f'{declared["source"]["base_commit"]}:{item["path"]}').lower()
        require(baseline == item["expected_git_blob_sha"], f"baseline Git blob mismatch: {item['path']}")
    identity_path = Path(nonempty(identity.get("manifest_path"), "identity.manifest_path")).resolve()
    capsule_path = Path(nonempty(identity.get("candidate_capsule_path"), "identity.candidate_capsule_path")).resolve()
    for artifact, field, schema in ((identity_path, "manifest_sha256", IDENTITY_SCHEMA),
                                    (capsule_path, "candidate_capsule_sha256", IDENTITY_CAPSULE_SCHEMA)):
        require(artifact.is_file() and not artifact.is_symlink(), f"identity anchor missing: {field}")
        expected_hash = str(identity.get(field, "")).lower()
        require(bool(HEX64.fullmatch(expected_hash)) and digest(artifact.read_bytes()) == expected_hash,
                f"identity anchor hash mismatch: {field}")
        require(read_json(artifact).get("schema") == schema, f"identity anchor schema mismatch: {field}")
    pinned_manifest = read_json(identity_path)
    require(pinned_manifest.get("source", {}).get("head", "").lower() == declared["source"]["base_commit"].lower(),
            "identity manifest base mismatch")
    candidate = pinned_manifest.get("prepared_candidate")
    require(isinstance(candidate, dict), "identity prepared_candidate missing")
    pinned_files = candidate.get("prestate_files", [])
    require(isinstance(pinned_files, list) and all(isinstance(item, dict) for item in pinned_files) and
            sorted((item.get("path"), str(item.get("sha256", "")).lower()) for item in pinned_files)
            == [(item["path"], item["sha256"]) for item in verified],
            "declarative exact15 differs from P801 identity manifest")
    require(str(pinned_manifest.get("identity", {}).get("candidate_capsule_sha256", "")).lower()
            == str(identity["candidate_capsule_sha256"]).lower(), "identity/capsule anchor mismatch")
    require(str(pinned_manifest.get("identity", {}).get("candidate_target_bytes_sha256", "")).lower()
            == target_digest, "identity target digest mismatch")
    return root, verified, target_digest


def build_standard_manifest(declared: dict[str, Any], files: list[dict[str, Any]]) -> dict[str, Any]:
    exact35 = declared["identity"].get("profile") == EXACT35_PROFILE
    if exact35:
        exact35_evidence(declared)
    envelope = declared["standard_manifest"]
    for field in MANIFEST_REQUIRED:
        require(field in envelope and envelope[field] not in (None, "", [], {}), f"manifest {field} required")
    for field in ("assurance_kernel", "delivery_control_binding", "human_execution_handoff",
                  "campaign_closeout", "material_commitment_preflight", "target_runtime_validation",
                  "qualification_basis"):
        require(isinstance(envelope[field], dict), f"manifest {field} object required")
    for field in ("activation_probes", "required_text_assertions"):
        require(isinstance(envelope[field], list) and bool(envelope[field]),
                f"manifest {field} nonempty list required")
    require(envelope.get("package_class") == "STANDARD_MATERIAL_SOURCE" and
            envelope.get("intended_consequence_class") == "SOURCE_EFFECT" and
            envelope.get("delivery_profile") == "STANDARD" and
            envelope.get("delivery_execution_contract") == "CEREBRO-STANDARD-DELIVERY-KERNEL-001",
            "standard delivery contract mismatch")
    require(bool(HEX64.fullmatch(str(envelope["kernel_sha256"]))), "kernel_sha256 invalid")
    if exact35:
        root = Path(declared["source"]["root"]).resolve()
        kernel_bytes = git_bytes(root, EXACT35_BASE, "tooling/delivery/Cerebro.StandardDeliveryKernel.ps1")
        require(envelope["kernel_sha256"].lower() == digest(kernel_bytes),
                "kernel binding differs from exact35 base")
    else:
        kernel = next((item for item in files if item["path"] == "tooling/delivery/Cerebro.StandardDeliveryKernel.ps1"), None)
        require(kernel is not None and envelope["kernel_sha256"].lower() == kernel["sha256"],
                "kernel binding differs from dirty15 candidate")
    require(envelope.get("assurance_kernel", {}).get("campaign_id") == envelope["campaign_id"],
            "assurance campaign mismatch")
    require(envelope.get("assurance_kernel", {}).get("package_class") == envelope["package_class"],
            "assurance package-class mismatch")
    require(envelope.get("campaign_closeout", {}).get("campaign_id") == envelope["campaign_id"],
            "closeout campaign mismatch")
    require(envelope.get("qualification_basis", {}).get("result") == "PASS", "qualification not PASS")
    require(envelope.get("human_execution_handoff", {}).get("required") is True,
            "human execution handoff required")
    require(envelope.get("target_runtime_validation", {}).get("required") is True,
            "target runtime validation required")
    require(envelope.get("delivery_control_binding", {}).get("schema") ==
            "cerebro-sealed-delivery-control-binding/v1", "control binding schema mismatch")
    require(envelope.get("delivery_control_binding", {}).get("source_commit", "").lower() ==
            declared["source"]["base_commit"].lower(), "control binding base mismatch")
    require(set(envelope).isdisjoint({"schema", "files", "branch", "expected_base_commit"}),
            "manifest derived fields must not be caller supplied")
    manifest = {"schema": MANIFEST_SCHEMA, "branch": BRANCH,
                "expected_base_commit": declared["source"]["base_commit"].lower(), **envelope}
    manifest["files"] = [{"path": item["path"], "operation": item["operation"],
                          "payload_path": f'payload/{item["path"]}', "sha256": item["sha256"],
                          "final_git_blob_sha": item["final_git_blob_sha"],
                          **({"expected_git_blob_sha": item["expected_git_blob_sha"]}
                             if item["operation"] == "replace" else {})} for item in files]
    return manifest


def build_change_capsule(declared: dict[str, Any], files: list[dict[str, Any]]) -> dict[str, Any]:
    change = declared["change"]
    change_id = nonempty(change.get("id"), "change.id")
    title = nonempty(change.get("title"), "change.title")
    require(change.get("assurance_profile") == "DEEP", "DEEP change assurance required")
    return {"schema": CAPSULE_SCHEMA, "change": {"id": change_id, "title": title},
            "authority": {"repository": REPOSITORY, "branch": BRANCH,
                          "base_commit": declared["source"]["base_commit"].lower()},
            "assurance": {"profile": "DEEP"},
            "files": [{"path": item["path"], "operation": item["operation"],
                       "payload": f'payload/{item["path"]}', "sha256": item["sha256"],
                       "baseline": ({"state": "present", "git_blob_sha": item["expected_git_blob_sha"]}
                                    if item["operation"] == "replace" else {"state": "absent"})}
                      for item in files]}


def output_path(declared: dict[str, Any], source_root: Path, disposable: bool = False) -> Path:
    root = Path(nonempty(declared["output"].get("root"), "output.root")).resolve()
    require(not root.is_relative_to(source_root), "output cannot be inside Source")
    if not disposable:
        run_root = Path(nonempty(declared["output"].get("run_root"), "output.run_root")).resolve()
        require(root.is_relative_to(run_root) and root != run_root, "output must be beneath declared Run root")
        require(run_root.is_dir() and not run_root.is_symlink(), "Run root missing/unsafe")
    require(root.parent.is_dir() and not root.exists(), "output must be a new child of an existing directory")
    return root


def verify_bundle_cross_binding(bundle: Path, declared: dict[str, Any], files: list[dict[str, Any]],
                                target_digest: str) -> dict[str, Any]:
    exact35 = declared["identity"].get("profile") == EXACT35_PROFILE
    manifest_path = bundle / "manifest.json"
    capsule_path = bundle / "capsule" / "capsule.json"
    manifest_bytes = manifest_path.read_bytes()
    capsule_bytes = capsule_path.read_bytes()
    manifest = read_json(manifest_path)
    capsule = read_json(capsule_path)
    require(manifest_bytes == json_bytes(build_standard_manifest(declared, files)), "manifest bytes not deterministic")
    require(capsule_bytes == json_bytes(build_change_capsule(declared, files)), "capsule bytes not deterministic")
    require(manifest.get("schema") == MANIFEST_SCHEMA and capsule.get("schema") == CAPSULE_SCHEMA,
            "generated schema mismatch")
    mf = manifest["files"]
    cf = capsule["files"]
    require(len(mf) == len(cf) == (35 if exact35 else FILE_COUNT), "generated file-count mismatch")
    for source_item, standard_item, change_item in zip(files, mf, cf, strict=True):
        path = source_item["path"]
        require(standard_item["path"] == change_item["path"] == path and
                standard_item["operation"] == change_item["operation"] == source_item["operation"] and
                standard_item["sha256"] == change_item["sha256"] == source_item["sha256"],
                f"cross-binding mismatch: {path}")
        if source_item["operation"] == "replace":
            require(standard_item["expected_git_blob_sha"] ==
                    change_item["baseline"]["git_blob_sha"] == source_item["expected_git_blob_sha"],
                    f"baseline mismatch: {path}")
        else:
            require("expected_git_blob_sha" not in standard_item
                    and change_item["baseline"] == {"state": "absent"},
                    f"create baseline mismatch: {path}")
        first = beneath(bundle, standard_item["payload_path"])
        second = beneath(bundle / "capsule", change_item["payload"])
        require(first.is_file() and second.is_file() and not first.is_symlink() and not second.is_symlink(),
                f"payload missing/unsafe: {path}")
        data = first.read_bytes()
        require(data == second.read_bytes() and digest(data) == source_item["sha256"] and
                git_blob(data) == source_item["final_git_blob_sha"], f"payload identity mismatch: {path}")
    rendered = "\n".join(f'{item["path"]}|{item["operation"]}|{item["sha256"]}' for item in mf)
    require(digest(rendered.encode("utf-8")) == target_digest, "emitted target digest mismatch")
    change_sha = digest(capsule_bytes)
    identity_sha = str(declared["identity"].get("candidate_capsule_sha256", "")).lower()
    if not exact35:
        require(change_sha != identity_sha, "P801 identity capsule cannot substitute for change capsule")
    engine = Path(__file__).resolve().parents[1] / "change" / "change_engine.py"
    verified = subprocess.run([sys.executable, str(engine), "verify-capsule", "--capsule-root",
                               str(bundle / "capsule")], text=True, capture_output=True, check=False)
    require(verified.returncode == 0, f"existing change validator rejected capsule: {verified.stdout} {verified.stderr}")
    return {"result": "PASS_NON_AUTHORITATIVE_BYTES_ONLY", "standard_manifest_sha256": digest(manifest_bytes),
            "change_capsule_sha256": change_sha,
            **({"p801_identity_capsule_sha256": identity_sha} if not exact35 else
               {"exact35_audit_sha256": EXACT35_AUDIT_SHA256, "candidate_commit": EXACT35_CANDIDATE}),
            "target_bytes_sha256": target_digest, "file_count": len(files),
            "publication": "NONE", "s1a": "NONE"}


def write_bundle_atomically_outside_source(declared: dict[str, Any], source_root: Path,
                                           files: list[dict[str, Any]], target_digest: str,
                                           disposable: bool = False,
                                           trusted_authority_check: Callable[[dict[str, Any]], dict[str, Any]] | None = None
                                           ) -> dict[str, Any]:
    if declared["identity"].get("profile") == EXACT35_PROFILE:
        exact35_evidence(declared)
        exact35_authority(declared, trusted_authority_check)
    root = output_path(declared, source_root, disposable)
    temporary = Path(tempfile.mkdtemp(prefix=".standard-delivery-builder-", dir=root.parent))
    try:
        bundle = temporary / "bundle"
        (bundle / "capsule").mkdir(parents=True)
        for item in files:
            source_bytes = (git_bytes(source_root, EXACT35_CANDIDATE, item["path"])
                            if declared["identity"].get("profile") == EXACT35_PROFILE
                            else beneath(source_root, item["path"]).read_bytes())
            for payload_root in (bundle / "payload", bundle / "capsule" / "payload"):
                destination = beneath(payload_root, item["path"])
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(source_bytes)
        (bundle / "manifest.json").write_bytes(json_bytes(build_standard_manifest(declared, files)))
        (bundle / "capsule" / "capsule.json").write_bytes(json_bytes(build_change_capsule(declared, files)))
        result = verify_bundle_cross_binding(bundle, declared, files, target_digest)
        if declared["identity"].get("profile") == EXACT35_PROFILE:
            exact35_authority(declared, trusted_authority_check)
        require(not root.exists(), "output raced with another writer")
        os.replace(temporary, root)
        temporary = root
        readback = verify_bundle_cross_binding(root / "bundle", declared, files, target_digest)
        require(result == readback, "post-move byte readback mismatch")
        return readback
    finally:
        if temporary.exists() and temporary != root:
            shutil.rmtree(temporary)


def verify(declared: dict[str, Any]) -> dict[str, Any]:
    source_root, files, target_digest = (exact35_candidate(declared)
                                         if declared["identity"].get("profile") == EXACT35_PROFILE
                                         else verify_source_and_p801_lock(declared))
    root = Path(nonempty(declared["output"].get("root"), "output.root")).resolve()
    require(not root.is_relative_to(source_root) and root.is_dir(), "bundle root missing/unsafe")
    return verify_bundle_cross_binding(root / "bundle", declared, files, target_digest)


def selftest() -> dict[str, Any]:
    """Exercise exact15 payload plus exact2 Builder extension in disposable Git/Run."""
    with tempfile.TemporaryDirectory(prefix="cerebro-builder-selftest-") as scratch:
        temp = Path(scratch)
        bare = temp / "morgul-tech" / "Cerebro-Source-1.0.git"
        bare.parent.mkdir(parents=True)
        command(temp, "git", "init", "--bare", "--initial-branch=main", str(bare))
        repo = temp / "source"
        command(temp, "git", "init", "--initial-branch=main", str(repo))
        command(repo, "git", "config", "user.email", "fixture@example.invalid")
        command(repo, "git", "config", "user.name", "Builder Fixture")
        paths = [f"fixture/file-{number:02d}.txt" for number in range(14)] + [
            "tooling/delivery/Cerebro.StandardDeliveryKernel.ps1"]
        baseline: dict[str, str] = {}
        for path in paths:
            target = beneath(repo, path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(f"baseline:{path}\n".encode())
            baseline[path] = git_blob(target.read_bytes())
        component = beneath(repo, BUILDER_EXTENSION_PATHS[0])
        component.parent.mkdir(parents=True, exist_ok=True)
        component.write_bytes(b"baseline:component\n")
        command(repo, "git", "add", ".")
        command(repo, "git", "commit", "-m", "fixture baseline")
        base = command(repo, "git", "rev-parse", "HEAD")
        command(repo, "git", "remote", "add", "origin", str(bare))
        command(repo, "git", "push", "-u", "origin", "main")
        records: list[dict[str, Any]] = []
        for path in sorted(paths):
            final = f"candidate:{path}\n".encode()
            beneath(repo, path).write_bytes(final)
            records.append({"path": path, "operation": "replace", "sha256": digest(final),
                            "expected_git_blob_sha": baseline[path], "final_git_blob_sha": git_blob(final)})
        component_bytes = Path(__file__).with_name("component.yaml").read_bytes()
        require(digest(component_bytes) == P813_COMPONENT_SHA256, "live P813 component fixture lock drift")
        component.write_bytes(component_bytes)
        builder = beneath(repo, BUILDER_EXTENSION_PATHS[1])
        builder.write_bytes(b"candidate:builder\n")
        extension_receipt_path = temp / "builder-extension-receipt.json"
        extension_receipt_path.write_bytes(json_bytes({
            "schema": EXTENSION_RECEIPT_SCHEMA, "base_commit": base,
            "files": [
                {"path": BUILDER_EXTENSION_PATHS[0], "git_status": " M",
                 "sha256": digest(component.read_bytes())},
                {"path": BUILDER_EXTENSION_PATHS[1], "git_status": "??",
                 "sha256": digest(builder.read_bytes())}]}))
        extension_records = read_json(extension_receipt_path)["files"]
        target_digest = digest("\n".join(f'{r["path"]}|replace|{r["sha256"]}' for r in records).encode())
        candidate_path = temp / "candidate-capsule.json"
        candidate_path.write_bytes(json_bytes({"schema": IDENTITY_CAPSULE_SCHEMA, "fixture": True}))
        identity_path = temp / "identity-manifest.json"
        identity_path.write_bytes(json_bytes({"schema": IDENTITY_SCHEMA, "source": {"head": base},
                                               "prepared_candidate": {"prestate_files": records},
                                               "identity": {"candidate_capsule_sha256": digest(candidate_path.read_bytes()),
                                                            "candidate_target_bytes_sha256": target_digest}}))
        control = {"schema": "cerebro-sealed-delivery-control-binding/v1", "source_commit": base,
                   "binding_fingerprint": "fixture-pinned"}
        campaign = "FIXTURE_BUILDER_CONFORMANCE"
        envelope: dict[str, Any] = {"patch_id": "FIXTURE-EXACT15", "campaign_id": campaign,
            "package_class": "STANDARD_MATERIAL_SOURCE", "intended_consequence_class": "SOURCE_EFFECT",
            "delivery_profile": "STANDARD", "delivery_execution_contract": "CEREBRO-STANDARD-DELIVERY-KERNEL-001",
            "kernel_sha256": next(r["sha256"] for r in records if r["path"].endswith("Kernel.ps1")),
            "commit_message": "fixture only", "assurance_kernel": {"campaign_id": campaign,
            "package_class": "STANDARD_MATERIAL_SOURCE"}, "delivery_control_binding": control,
            "human_execution_handoff": {"required": True}, "campaign_closeout": {"campaign_id": campaign},
            "material_commitment_preflight": {"fixture": True}, "target_runtime_validation": {"required": True},
            "activation_probes": [{"id": "fixture"}], "required_text_assertions": [{"path": paths[0]}],
            "qualification_basis": {"result": "PASS"}}
        output = temp / "package-a"
        declared = {"schema": INPUT_SCHEMA,
            "source": {"root": str(repo), "repository": REPOSITORY, "branch": BRANCH,
                       "base_commit": base, "currentness_contract": {
                           "profile": DIRTY17_PROFILE,
                           "extension_receipt_path": str(extension_receipt_path),
                           "extension_receipt_sha256": digest(extension_receipt_path.read_bytes()),
                           "extension_files": extension_records}},
            "identity": {"manifest_path": str(identity_path), "manifest_sha256": digest(identity_path.read_bytes()),
                         "candidate_capsule_path": str(candidate_path),
                         "candidate_capsule_sha256": digest(candidate_path.read_bytes()),
                         "target_bytes_sha256": target_digest, "files": records},
            "standard_manifest": envelope, "change": {"id": "FIXTURE-EXACT15", "title": "fixture only",
                                                    "assurance_profile": "DEEP"},
            "output": {"root": str(output), "run_root": str(temp),
                       "authority": "NON_AUTHORITATIVE_GENERATED_EVIDENCE"},
            "authorization": {"effect": "RUN_ONLY_PACKAGE_BUILD", "pm_bound": True,
                              "packet": "SELFTEST_ONLY", "claim": "SELFTEST_ONLY"}}
        input_path = temp / "declared-input.json"
        input_path.write_bytes(json_bytes(declared))
        declared = load_declared_input(input_path)
        source_root, files, target_digest = verify_source_and_p801_lock(declared)
        first = write_bundle_atomically_outside_source(declared, source_root, files, target_digest, disposable=True)
        second_output = temp / "package-b"
        declared["output"]["root"] = str(second_output)
        second = write_bundle_atomically_outside_source(declared, source_root, files, target_digest, disposable=True)
        require(first == second, "repeat build not byte-identical")
        negatives: list[str] = []

        def expect_reject(label: str, candidate: dict[str, Any]) -> None:
            try:
                verify_source_and_p801_lock(candidate)
            except BuilderError:
                negatives.append(label)
            else:
                raise BuilderError(f"negative canary not rejected: {label}")

        candidate = copy.deepcopy(declared)
        candidate["identity"]["candidate_capsule_sha256"] = first["change_capsule_sha256"]
        expect_reject("dual_digest_substitution", candidate)
        candidate = copy.deepcopy(declared)
        candidate["identity"]["files"][1]["path"] = candidate["identity"]["files"][0]["path"]
        expect_reject("duplicate_path", candidate)
        candidate = copy.deepcopy(declared)
        candidate["identity"]["files"][0]["path"] = "../escape.txt"
        expect_reject("path_traversal", candidate)
        candidate = copy.deepcopy(declared)
        candidate["identity"]["files"][0]["sha256"] = "0" * 64
        expect_reject("final_hash_mismatch", candidate)
        candidate = copy.deepcopy(declared)
        candidate["source"]["base_commit"] = "0" * 40
        expect_reject("base_commit_drift", candidate)
        candidate = copy.deepcopy(declared)
        candidate["identity"]["files"].pop()
        expect_reject("missing_path", candidate)
        candidate = copy.deepcopy(declared)
        candidate["source"].pop("currentness_contract")
        expect_reject("exact15_profile_rejects_dirty17", candidate)
        candidate = copy.deepcopy(declared)
        candidate["source"]["currentness_contract"]["extension_receipt_sha256"] = "0" * 64
        expect_reject("extension_receipt_hash_drift", candidate)
        candidate = copy.deepcopy(declared)
        candidate["source"]["currentness_contract"]["extension_files"][1]["sha256"] = "0" * 64
        expect_reject("declared_extension_receipt_divergence", candidate)
        candidate = copy.deepcopy(declared)
        candidate["source"]["currentness_contract"]["profile"] = "ALLOW_EXTRA_DIRTY"
        expect_reject("permissive_profile_rejected", candidate)

        extra = beneath(repo, "tooling/builder/unowned-extra.txt")
        extra.write_bytes(b"eighteenth path\n")
        try:
            expect_reject("eighteenth_path", declared)
        finally:
            extra.unlink()
        original_builder = builder.read_bytes()
        builder.write_bytes(original_builder + b"drift")
        try:
            expect_reject("builder_executable_hash_drift", declared)
        finally:
            builder.write_bytes(original_builder)
        builder.unlink()
        try:
            expect_reject("missing_extension_path", declared)
        finally:
            builder.write_bytes(original_builder)
        builder.unlink()
        substitute = beneath(repo, "tooling/builder/substitute.py")
        substitute.write_bytes(b"replacement path\n")
        try:
            expect_reject("seventeen_count_substitution", declared)
        finally:
            substitute.unlink()
            builder.write_bytes(original_builder)
        original_component = component.read_bytes()
        component.write_bytes(original_component + b"drift")
        try:
            expect_reject("extension_hash_drift", declared)
        finally:
            component.write_bytes(original_component)
        command(repo, "git", "add", BUILDER_EXTENSION_PATHS[0])
        try:
            expect_reject("staged_wrong_status_kind", declared)
        finally:
            command(repo, "git", "reset", "-q", "HEAD", "--", BUILDER_EXTENSION_PATHS[0])
        baseline_file = beneath(repo, paths[0])
        original_baseline_file = baseline_file.read_bytes()
        baseline_file.write_bytes(original_baseline_file + b"drift")
        try:
            expect_reject("physical_baseline_hash_drift", declared)
        finally:
            baseline_file.write_bytes(original_baseline_file)
        baseline_file.unlink()
        try:
            expect_reject("missing_baseline_path", declared)
        finally:
            baseline_file.write_bytes(original_baseline_file)

        capsule_file = second_output / "bundle" / "capsule" / "capsule.json"
        original_capsule = capsule_file.read_bytes()
        capsule_file.write_bytes(json_bytes({"schema": IDENTITY_CAPSULE_SCHEMA}))
        try:
            verify_bundle_cross_binding(second_output / "bundle", declared, files, target_digest)
        except BuilderError:
            negatives.append("capsule_schema_mismatch")
        else:
            raise BuilderError("capsule schema negative not rejected")
        finally:
            capsule_file.write_bytes(original_capsule)

        payload_file = second_output / "bundle" / "payload" / records[0]["path"]
        original_payload = payload_file.read_bytes()
        payload_file.write_bytes(original_payload + b"tampered")
        try:
            verify_bundle_cross_binding(second_output / "bundle", declared, files, target_digest)
        except BuilderError:
            negatives.append("payload_byte_mismatch")
        else:
            raise BuilderError("payload negative not rejected")
        finally:
            payload_file.write_bytes(original_payload)
        return {"result": "PASS", "schemas": [MANIFEST_SCHEMA, CAPSULE_SCHEMA],
                "deterministic_repeat": True, "negative_canaries": negatives,
                "payload_file_count": FILE_COUNT, "currentness_path_count": 17,
                "currentness_profile": DIRTY17_PROFILE,
                "source_effect": "NONE", "s1a_effect": "NONE"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("build", "verify", "selftest"))
    parser.add_argument("--input", type=Path, help="fully pinned PM-bound declarative JSON input")
    args = parser.parse_args()
    try:
        if args.mode == "selftest":
            result = selftest()
        else:
            require(args.input is not None, "--input required")
            declared = load_declared_input(args.input)
            if args.mode == "verify":
                result = verify(declared)
            else:
                verifier = PackageBuildVerifier.from_environment()
                source_root, files, target_digest = (exact35_candidate(declared)
                                                     if declared["identity"].get("profile") == EXACT35_PROFILE
                                                     else verify_source_and_p801_lock(declared))
                if declared["identity"].get("profile") != EXACT35_PROFILE:
                    verifier(declared)
                result = write_bundle_atomically_outside_source(
                    declared, source_root, files, target_digest, trusted_authority_check=verifier)
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except (BuilderError, VerifierError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"result": "HOLD_NO_OUTPUT_NO_EFFECT", "detail": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
