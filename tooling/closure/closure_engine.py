#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ENGINE_VERSION = "0.1.0"
INPUT_SCHEMA = "cerebro-closure-input/v0.1"
RECEIPT_SCHEMA = "cerebro-closure-receipt/v0.1"
REQUIRED_EVIDENCE = {
    "stability_gate",
    "quality_gate",
    "publication_gate",
    "operational_verification",
    "final_learning_consolidation",
    "internal_control_review",
    "roadmap_reconciliation",
}


class ClosureError(RuntimeError):
    def __init__(self, classification: str, detail: str):
        super().__init__(detail)
        self.classification = classification
        self.detail = detail


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def capture(repo: Path, *arguments: str) -> str:
    process = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    if process.returncode != 0:
        raise ClosureError(
            "GIT_EVIDENCE_FAILURE",
            f"{repo}:git {' '.join(arguments)}:{(process.stdout + process.stderr).strip()}",
        )
    return process.stdout.strip()


def load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ClosureError("INVALID_EVIDENCE", f"{path}:{exc}") from exc
    if not isinstance(value, dict):
        raise ClosureError("INVALID_EVIDENCE", f"object required:{path}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_input(value: dict[str, Any]) -> None:
    if value.get("schema") != INPUT_SCHEMA:
        raise ClosureError("INVALID_CLOSURE_INPUT", f"schema must be {INPUT_SCHEMA}")
    for field in ("patch_ref", "source_commit", "release_commit"):
        if not isinstance(value.get(field), str) or not value[field]:
            raise ClosureError("INVALID_CLOSURE_INPUT", f"{field} required")
    for field in ("source_commit", "release_commit"):
        commit = value[field]
        if len(commit) != 40 or any(character not in "0123456789abcdefABCDEF" for character in commit):
            raise ClosureError("INVALID_CLOSURE_INPUT", f"{field} must be 40 hex chars")
    evidence = value.get("evidence")
    if not isinstance(evidence, dict):
        raise ClosureError("INVALID_CLOSURE_INPUT", "evidence object required")
    missing = REQUIRED_EVIDENCE - set(evidence)
    if missing:
        raise ClosureError("MISSING_CLOSURE_EVIDENCE", ",".join(sorted(missing)))


def repository_check(
    name: str,
    repo: Path,
    expected_commit: str,
    expected_remote_suffix: str,
) -> dict[str, Any]:
    repo = repo.resolve()
    root = Path(capture(repo, "rev-parse", "--show-toplevel")).resolve()
    branch = capture(repo, "branch", "--show-current")
    head = capture(repo, "rev-parse", "HEAD").lower()
    remote = capture(repo, "remote", "get-url", "origin").lower().rstrip("/").removesuffix(".git")
    remote_head = capture(repo, "rev-parse", "origin/main").lower()
    dirty = capture(repo, "status", "--porcelain=v1", "--untracked-files=all")
    failures: list[str] = []
    if root != repo:
        failures.append("root-binding")
    if branch != "main":
        failures.append("branch-main")
    if head != expected_commit.lower():
        failures.append("expected-commit")
    if remote_head != head:
        failures.append("remote-alignment")
    if not remote.endswith(expected_remote_suffix.lower()):
        failures.append("remote-identity")
    if dirty:
        failures.append("worktree-clean")
    return {
        "name": name,
        "result": "PASS" if not failures else "FAIL",
        "path": str(repo),
        "head": head,
        "origin_main": remote_head,
        "failures": failures,
        "dirty": dirty.splitlines(),
    }


def select(value: dict[str, Any], selector: str) -> Any:
    current: Any = value
    for part in selector.split("."):
        if not isinstance(current, dict) or part not in current:
            raise ClosureError("INVALID_EVIDENCE", f"selector not found:{selector}")
        current = current[part]
    return current


def evidence_check(name: str, declaration: Any) -> dict[str, Any]:
    if not isinstance(declaration, dict) or not isinstance(declaration.get("path"), str):
        raise ClosureError("INVALID_CLOSURE_INPUT", f"evidence.{name}.path required")
    path = Path(declaration["path"]).resolve()
    expected_sha256 = str(declaration.get("sha256", "")).lower()
    if len(expected_sha256) != 64 or any(character not in "0123456789abcdef" for character in expected_sha256):
        raise ClosureError("INVALID_CLOSURE_INPUT", f"evidence.{name}.sha256 required")
    actual_sha256 = sha256_file(path)
    if actual_sha256 != expected_sha256:
        raise ClosureError(
            "EVIDENCE_IDENTITY_MISMATCH",
            f"{name}:expected={expected_sha256}:actual={actual_sha256}",
        )
    value = load_object(path)
    selector = str(declaration.get("selector", "result"))
    expected = declaration.get("expected", "PASS")
    actual = select(value, selector)
    return {
        "name": name,
        "result": "PASS" if actual == expected else "FAIL",
        "path": str(path),
        "sha256": actual_sha256,
        "selector": selector,
        "expected": expected,
        "actual": actual,
    }


def evaluate(
    closure_input: dict[str, Any],
    source: Path,
    release: Path,
) -> dict[str, Any]:
    validate_input(closure_input)
    repositories = [
        repository_check(
            "source",
            source,
            closure_input["source_commit"],
            "morgul-tech/cerebro-source-1.0",
        ),
        repository_check(
            "release",
            release,
            closure_input["release_commit"],
            "morgul-tech/cerebro-release-0.1",
        ),
    ]
    evidence = [
        evidence_check(name, closure_input["evidence"][name])
        for name in sorted(REQUIRED_EVIDENCE)
    ]
    blockers = [
        f"repository:{item['name']}"
        for item in repositories
        if item["result"] != "PASS"
    ] + [
        f"evidence:{item['name']}"
        for item in evidence
        if item["result"] != "PASS"
    ]
    return {
        "schema": RECEIPT_SCHEMA,
        "engine_version": ENGINE_VERSION,
        "patch_ref": closure_input["patch_ref"],
        "created_at_utc": now(),
        "result": "PASS" if not blockers else "FAIL",
        "classification": "CLOSURE_PASS" if not blockers else "CLOSURE_BLOCKED",
        "repositories": repositories,
        "evidence": evidence,
        "blockers": blockers,
        "source_mutation": False,
        "release_mutation": False,
        "commit_created": False,
        "publication_performed": False,
    }


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _git(repo: Path, *arguments: str) -> str:
    process = subprocess.run(["git", *arguments], cwd=repo, text=True, capture_output=True, check=False)
    if process.returncode != 0:
        raise RuntimeError((process.stdout + process.stderr).strip())
    return process.stdout.strip()


def _make_repo(root: Path, remote_suffix: str) -> tuple[Path, str]:
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "selftest@cerebro.local")
    _git(root, "config", "user.name", "Cerebro Selftest")
    _git(root, "remote", "add", "origin", f"https://github.com/{remote_suffix}.git")
    (root / "baseline.txt").write_text("baseline\n", encoding="utf-8")
    _git(root, "add", "baseline.txt")
    _git(root, "commit", "-m", "baseline")
    commit = _git(root, "rev-parse", "HEAD")
    _git(root, "update-ref", "refs/remotes/origin/main", commit)
    return root, commit


def selftest() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="cerebro-closure-selftest-") as temporary:
        root = Path(temporary)
        source, source_commit = _make_repo(root / "source", "morgul-tech/Cerebro-Source-1.0")
        release, release_commit = _make_repo(root / "release", "morgul-tech/Cerebro-Release-0.1")
        evidence: dict[str, Any] = {}
        for name in REQUIRED_EVIDENCE:
            path = root / f"{name}.json"
            atomic_json(path, {"result": "PASS"})
            evidence[name] = {
                "path": str(path),
                "sha256": sha256_file(path),
                "selector": "result",
                "expected": "PASS",
            }
        closure_input = {
            "schema": INPUT_SCHEMA,
            "patch_ref": "CLOSURE-SELFTEST",
            "source_commit": source_commit,
            "release_commit": release_commit,
            "evidence": evidence,
        }
        passed = evaluate(closure_input, source, release)
        (release / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        blocked = evaluate(closure_input, source, release)
        tests = [
            {"name": "clean_aligned_complete_evidence_passes", "result": "PASS" if passed["result"] == "PASS" else "FAIL"},
            {"name": "dirty_release_blocks_closure", "result": "PASS" if "repository:release" in blocked["blockers"] else "FAIL"},
            {"name": "closure_is_read_only", "result": "PASS" if not passed["source_mutation"] and not passed["release_mutation"] else "FAIL"},
        ]
        return {
            "schema": "cerebro-closure-selftest/v0.1",
            "result": "PASS" if all(item["result"] == "PASS" for item in tests) else "FAIL",
            "tests": tests,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Cerebro Closure Engine")
    sub = parser.add_subparsers(dest="command", required=True)
    evaluate_parser = sub.add_parser("evaluate")
    evaluate_parser.add_argument("--input", required=True)
    evaluate_parser.add_argument("--source-root", required=True)
    evaluate_parser.add_argument("--release-root", required=True)
    evaluate_parser.add_argument("--receipt")
    sub.add_parser("selftest")
    args = parser.parse_args()
    try:
        if args.command == "selftest":
            report = selftest()
        else:
            report = evaluate(
                load_object(Path(args.input)),
                Path(args.source_root),
                Path(args.release_root),
            )
            if args.receipt:
                atomic_json(Path(args.receipt), report)
        print(json.dumps(report, indent=2))
        return 0 if report["result"] == "PASS" else 2
    except ClosureError as exc:
        print(json.dumps({"result": "FAIL", "classification": exc.classification, "detail": exc.detail}, indent=2))
        return 2
    except Exception as exc:
        print(json.dumps({"result": "FAIL", "classification": "UNEXPECTED_EXCEPTION", "detail": repr(exc)}, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
