#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SOURCE_ROOT = Path(__file__).resolve().parents[2]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from tooling.change.change_engine import (
    ChangeError,
    changed_paths,
    contained,
    git,
    load_json,
    safe_relative,
    sha256_file,
    validate_manifest,
    verify_baseline,
    verify_payloads,
    verify_repo_identity,
)

RECEIPT_SCHEMA = "cerebro-delivery-receipt/v0.1"
CONTROLLER_VERSION = "0.1.0"


class DeliveryError(RuntimeError):
    def __init__(self, classification: str, detail: str):
        super().__init__(detail)
        self.classification = classification
        self.detail = detail


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def default_run_root(source: Path) -> Path:
    for parent in (source, *source.parents):
        candidate = parent / "Run"
        if candidate.is_dir():
            return candidate / "delivery"
    return source.parent / "Run" / "delivery"


def verify_working_source(source: Path, manifest: dict[str, Any]) -> None:
    verify_repo_identity(source, manifest)
    if git(source, "branch", "--show-current") != "main":
        raise DeliveryError("BASELINE_MISMATCH", "Working Source branch must be main")
    expected = str(manifest["authority"]["base_commit"]).lower()
    actual = git(source, "rev-parse", "HEAD").lower()
    if actual != expected:
        raise DeliveryError("BASELINE_MISMATCH", f"HEAD expected={expected} actual={actual}")
    verify_baseline(source, manifest)


def declared_paths(manifest: dict[str, Any]) -> list[str]:
    return [safe_relative(str(item["path"])) for item in manifest["files"]]


def create_prestate(
    source: Path,
    manifest: dict[str, Any],
    transaction: Path,
) -> dict[str, Any]:
    backup_root = transaction / "backup"
    records: list[dict[str, Any]] = []
    for item in manifest["files"]:
        relative = safe_relative(str(item["path"]))
        target = contained(source, relative)
        record: dict[str, Any] = {
            "path": relative,
            "operation": item["operation"],
            "baseline_state": item["baseline"]["state"],
            "mutated": False,
        }
        if target.is_file():
            backup = contained(backup_root, relative)
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup)
            digest = sha256_file(target)
            if sha256_file(backup) != digest:
                raise DeliveryError("BACKUP_VERIFICATION_FAILED", relative)
            record.update({"physical_sha256": digest, "backup": relative})
        elif target.exists():
            raise DeliveryError("UNSUPPORTED_TARGET_TYPE", relative)
        else:
            record.update({"physical_sha256": None, "backup": None})
        records.append(record)
    return {
        "schema": "cerebro-delivery-journal/v0.1",
        "created_at_utc": now(),
        "state": "PREPARED",
        "records": records,
    }


def post_state_matches(target: Path, item: dict[str, Any]) -> bool:
    if item["operation"] == "delete":
        return not target.exists()
    return target.is_file() and sha256_file(target).lower() == str(item["sha256"]).lower()


def baseline_state_matches(target: Path, record: dict[str, Any]) -> bool:
    if record["baseline_state"] == "absent":
        return not target.exists()
    return target.is_file() and sha256_file(target).lower() == str(record["physical_sha256"]).lower()


def rollback(
    source: Path,
    manifest: dict[str, Any],
    transaction: Path,
    journal: dict[str, Any],
) -> dict[str, Any]:
    unknown: list[str] = []
    restored: list[str] = []
    by_path = {safe_relative(str(item["path"])): item for item in manifest["files"]}
    for record in reversed(journal["records"]):
        relative = record["path"]
        target = contained(source, relative)
        item = by_path[relative]
        if not (baseline_state_matches(target, record) or post_state_matches(target, item)):
            unknown.append(relative)
            continue
        if record["baseline_state"] == "present":
            backup = contained(transaction / "backup", str(record["backup"]))
            if not backup.is_file() or sha256_file(backup) != record["physical_sha256"]:
                unknown.append(relative)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, target)
            if sha256_file(target) != record["physical_sha256"]:
                unknown.append(relative)
                continue
        elif target.exists():
            if target.is_file():
                target.unlink()
            else:
                unknown.append(relative)
                continue
        restored.append(relative)
    clean = not changed_paths(source)
    return {
        "result": "FAILED_RECOVERY_REQUIRED" if unknown or not clean else "ROLLED_BACK",
        "restored_paths": sorted(restored),
        "unknown_paths": sorted(unknown),
        "worktree_clean": clean,
    }


def apply_transaction(
    capsule_root: Path,
    source: Path,
    run_root: Path | None = None,
    *,
    fault_after: int | None = None,
) -> dict[str, Any]:
    capsule_root = capsule_root.resolve()
    source = source.resolve()
    manifest = load_json(capsule_root / "capsule.json")
    validate_manifest(manifest)
    verify_payloads(capsule_root, manifest)
    verify_working_source(source, manifest)

    transaction_id = f"{manifest['change']['id']}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:12]}"
    transaction = (run_root or default_run_root(source)).resolve() / transaction_id
    transaction.mkdir(parents=True, exist_ok=False)
    receipt_path = transaction / "DELIVERY_RECEIPT.json"
    journal_path = transaction / "MUTATION_JOURNAL.json"
    journal = create_prestate(source, manifest, transaction)
    atomic_json(journal_path, journal)

    receipt: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "controller_version": CONTROLLER_VERSION,
        "transaction_id": transaction_id,
        "change_id": manifest["change"]["id"],
        "created_at_utc": now(),
        "result": "FAIL",
        "classification": "DELIVERY_NOT_COMPLETED",
        "detail": "",
        "source": {
            "path": str(source),
            "base_commit": manifest["authority"]["base_commit"],
        },
        "paths": declared_paths(manifest),
        "journal": str(journal_path),
        "receipt_path": str(receipt_path),
        "recovery": {"result": "NOT_REQUIRED"},
        "source_mutation": False,
        "publication_performed": False,
    }

    try:
        for index, item in enumerate(manifest["files"], start=1):
            relative = safe_relative(str(item["path"]))
            record = next(value for value in journal["records"] if value["path"] == relative)
            record["mutated"] = True
            journal["state"] = "MUTATING"
            atomic_json(journal_path, journal)
            target = contained(source, relative)
            if item["operation"] == "delete":
                target.unlink(missing_ok=True)
            else:
                payload = contained(capsule_root, str(item["payload"]))
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
                shutil.copy2(payload, temporary)
                if sha256_file(temporary).lower() != str(item["sha256"]).lower():
                    temporary.unlink(missing_ok=True)
                    raise DeliveryError("PAYLOAD_WRITE_FAILED", relative)
                os.replace(temporary, target)
            if not post_state_matches(target, item):
                raise DeliveryError("POST_STATE_MISMATCH", relative)
            if fault_after == index:
                raise DeliveryError("INJECTED_SELFTEST_FAULT", relative)

        actual = set(changed_paths(source))
        expected = set(declared_paths(manifest))
        if actual != expected:
            raise DeliveryError(
                "SCOPE_VIOLATION",
                f"expected={sorted(expected)} actual={sorted(actual)}",
            )
        journal["state"] = "DELIVERED"
        atomic_json(journal_path, journal)
        receipt.update({
            "result": "PASS",
            "classification": "READY_FOR_LOCAL_VALIDATION",
            "source_mutation": True,
            "completed_at_utc": now(),
        })
        atomic_json(receipt_path, receipt)
    except Exception as exc:
        recovery = rollback(source, manifest, transaction, journal)
        classification = getattr(exc, "classification", "UNEXPECTED_DELIVERY_FAILURE")
        detail = getattr(exc, "detail", repr(exc))
        receipt.update({
            "classification": classification,
            "detail": detail,
            "recovery": recovery,
            "source_mutation": not recovery.get("worktree_clean", False),
            "completed_at_utc": now(),
        })
        try:
            atomic_json(receipt_path, receipt)
        except Exception as receipt_exc:
            raise DeliveryError(
                "RECEIPT_PERSISTENCE_FAILED",
                f"original={classification}:{detail}; receipt={receipt_exc!r}; recovery={recovery['result']}",
            ) from receipt_exc
    return receipt


def _run_git(repo: Path, *arguments: str) -> str:
    process = subprocess.run(["git", *arguments], cwd=repo, text=True, capture_output=True, check=False)
    if process.returncode != 0:
        raise RuntimeError((process.stdout + process.stderr).strip())
    return process.stdout.strip()


def selftest() -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="cerebro-delivery-selftest-") as temporary:
        root = Path(temporary)
        source = root / "source"
        capsule = root / "capsule"
        payload = capsule / "payload"
        source.mkdir()
        payload.mkdir(parents=True)
        _run_git(source, "init", "-b", "main")
        _run_git(source, "config", "user.email", "selftest@cerebro.local")
        _run_git(source, "config", "user.name", "Cerebro Selftest")
        _run_git(source, "remote", "add", "origin", "https://github.com/morgul-tech/Cerebro-Source-1.0.git")
        (source / "a.txt").write_text("baseline\n", encoding="utf-8")
        _run_git(source, "add", "a.txt")
        _run_git(source, "commit", "-m", "baseline")
        base = _run_git(source, "rev-parse", "HEAD")
        blob = _run_git(source, "rev-parse", f"{base}:a.txt")
        replacement = b"replacement\n"
        (payload / "a.txt").write_bytes(replacement)
        manifest = {
            "schema": "cerebro-change-capsule/v0.2",
            "change": {"id": "DELIVERY-SELFTEST", "title": "delivery selftest"},
            "authority": {
                "repository": "morgul-tech/Cerebro-Source-1.0",
                "branch": "main",
                "base_commit": base,
            },
            "assurance": {"profile": "FAST"},
            "files": [{
                "path": "a.txt",
                "operation": "replace",
                "payload": "payload/a.txt",
                "sha256": hashlib.sha256(replacement).hexdigest(),
                "baseline": {"state": "present", "git_blob_sha": blob},
            }],
        }
        atomic_json(capsule / "capsule.json", manifest)

        failed = apply_transaction(capsule, source, root / "failed", fault_after=1)
        results.append({
            "name": "fault_rolls_back_to_clean_baseline",
            "result": "PASS" if failed["recovery"]["result"] == "ROLLED_BACK" and not changed_paths(source) else "FAIL",
        })
        passed = apply_transaction(capsule, source, root / "passed")
        results.append({
            "name": "bounded_delivery_matches_declared_scope",
            "result": "PASS" if passed["result"] == "PASS" and changed_paths(source) == ["a.txt"] else "FAIL",
        })
        receipt = load_json(Path(passed["receipt_path"]))
        results.append({
            "name": "persistent_receipt_blocks_implicit_publication",
            "result": "PASS" if receipt["publication_performed"] is False else "FAIL",
        })
    overall = "PASS" if all(item["result"] == "PASS" for item in results) else "FAIL"
    return {
        "schema": "cerebro-delivery-selftest/v0.1",
        "result": overall,
        "controller_version": CONTROLLER_VERSION,
        "tests": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Cerebro Delivery Controller")
    sub = parser.add_subparsers(dest="command", required=True)
    apply_parser = sub.add_parser("apply")
    apply_parser.add_argument("--capsule-root", required=True)
    apply_parser.add_argument("--source-root", required=True)
    apply_parser.add_argument("--run-root")
    sub.add_parser("selftest")
    args = parser.parse_args()
    try:
        if args.command == "selftest":
            report = selftest()
        else:
            report = apply_transaction(
                Path(args.capsule_root),
                Path(args.source_root),
                Path(args.run_root) if args.run_root else None,
            )
        print(json.dumps(report, indent=2))
        return 0 if report["result"] == "PASS" else 2
    except (ChangeError, DeliveryError) as exc:
        print(json.dumps({"result": "FAIL", "classification": exc.classification, "detail": exc.detail}, indent=2))
        return 2
    except Exception as exc:
        print(json.dumps({"result": "FAIL", "classification": "UNEXPECTED_EXCEPTION", "detail": repr(exc)}, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
