#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable

HOST_VERSION = "0.1.0"
SOURCE_REPOSITORY = "morgul-tech/Cerebro-Source-1.0"
DEFAULT_SOURCE_CANDIDATES = [
    Path(r"D:\Cerebro\Source\Cerebro_Source_v1.0"),
    Path(r"C:\Cerebro\Source\Cerebro_Source_v1.0"),
]
LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share"))
SNAPSHOT_ROOT = LOCALAPPDATA / "Cerebro" / "tooling-snapshots"


class HostError(RuntimeError):
    def __init__(self, classification: str, detail: str):
        super().__init__(detail)
        self.classification = classification
        self.detail = detail


def run(cmd: list[str], cwd: Path, allowed: Iterable[int] = (0,)) -> subprocess.CompletedProcess[str]:
    try:
        process = subprocess.run(cmd, cwd=cwd, text=True, check=False)
    except OSError as exc:
        raise HostError("PROCESS_START_FAILURE", f"{cmd[0]}:{exc}") from exc
    if process.returncode not in set(allowed):
        raise HostError("DELEGATE_FAILURE", f"exit={process.returncode}: {' '.join(cmd)}")
    return process


def capture(repo: Path, *args: str, allowed: Iterable[int] = (0,)) -> str:
    try:
        process = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=False)
    except OSError as exc:
        raise HostError("GIT_UNAVAILABLE", str(exc)) from exc
    if process.returncode not in set(allowed):
        detail = (process.stdout + "\n" + process.stderr).strip()
        raise HostError("GIT_FAILURE", f"git {' '.join(args)} exit={process.returncode}: {detail}")
    return process.stdout.strip()


def locate_source(explicit: str | None) -> Path:
    candidates = [Path(explicit)] if explicit else []
    candidates.extend(DEFAULT_SOURCE_CANDIDATES)
    for candidate in candidates:
        try:
            path = candidate.resolve()
        except Exception:
            continue
        if path.is_dir() and (path / "cerebro.yaml").is_file():
            try:
                if capture(path, "rev-parse", "--is-inside-work-tree") == "true":
                    return path
            except HostError:
                continue
    raise HostError("SOURCE_NOT_FOUND", "Cerebro Working Source was not found")


def verify_source(source: Path, commit: str | None) -> str:
    root = Path(capture(source, "rev-parse", "--show-toplevel")).resolve()
    if root != source.resolve():
        raise HostError("SOURCE_BINDING_MISMATCH", f"expected={source}; actual={root}")
    origin = capture(source, "remote", "get-url", "origin").lower().rstrip("/").removesuffix(".git")
    if not origin.endswith(SOURCE_REPOSITORY.lower()):
        raise HostError("SOURCE_REMOTE_MISMATCH", origin)
    selected = commit.lower() if commit else capture(source, "rev-parse", "HEAD").lower()
    if len(selected) != 40 or any(c not in "0123456789abcdef" for c in selected):
        raise HostError("SOURCE_COMMIT_INVALID", selected)
    capture(source, "cat-file", "-e", f"{selected}^{{commit}}")
    return selected


def snapshot_path(commit: str) -> Path:
    return SNAPSHOT_ROOT / commit


def snapshot_is_valid(snapshot: Path, commit: str) -> bool:
    marker = snapshot / ".cerebro-tooling-snapshot.json"
    engine = snapshot / "tooling" / "change" / "change_engine.py"
    if not marker.is_file() or not engine.is_file():
        return False
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        return data.get("commit") == commit and data.get("host_snapshot_schema") == "cerebro-tooling-snapshot/v0.1"
    except Exception:
        return False


def create_snapshot(source: Path, commit: str) -> Path:
    SNAPSHOT_ROOT.mkdir(parents=True, exist_ok=True)
    target = snapshot_path(commit)
    if snapshot_is_valid(target, commit):
        return target
    if target.exists():
        # Only host-owned snapshot paths are removed; Working Source is never touched.
        shutil.rmtree(target)
    capture(source, "worktree", "prune")
    capture(source, "worktree", "add", "--detach", "--force", str(target), commit)
    engine = target / "tooling" / "change" / "change_engine.py"
    if not engine.is_file():
        try:
            capture(source, "worktree", "remove", "--force", str(target))
        finally:
            raise HostError("CHANGE_ENGINE_MISSING", f"{commit}:tooling/change/change_engine.py")
    marker = {
        "host_snapshot_schema": "cerebro-tooling-snapshot/v0.1",
        "commit": commit,
        "source_repository": SOURCE_REPOSITORY,
    }
    (target / ".cerebro-tooling-snapshot.json").write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
    return target


def delegate_change(snapshot: Path, arguments: list[str]) -> int:
    engine = snapshot / "tooling" / "change" / "change_engine.py"
    cmd = [sys.executable, str(engine), *arguments]
    # Deliberately inherit stdout/stderr for live visibility.
    try:
        process = subprocess.run(cmd, cwd=snapshot, check=False)
    except OSError as exc:
        raise HostError("PROCESS_START_FAILURE", str(exc)) from exc
    return int(process.returncode)


def selftest() -> dict:
    # Host selftest is intentionally narrow: it proves parsing/dispatch shape,
    # not repository semantics that require a real Cerebro Source checkout.
    material = f"{HOST_VERSION}|{SOURCE_REPOSITORY}|{SNAPSHOT_ROOT}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return {
        "schema": "cerebro-host-selftest/v0.1",
        "result": "PASS",
        "host_version": HOST_VERSION,
        "dispatch_contract": "snapshot_then_delegate",
        "source_mutation": False,
        "fingerprint": digest,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Cerebro Host")
    parser.add_argument("--source-root")
    parser.add_argument("--source-commit")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("selftest")
    change = sub.add_parser("change")
    change.add_argument("change_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    try:
        if args.command == "selftest":
            print(json.dumps(selftest(), indent=2))
            return 0
        source = locate_source(args.source_root)
        commit = verify_source(source, args.source_commit)
        snapshot = create_snapshot(source, commit)
        if not args.change_args:
            raise HostError("MISSING_CHANGE_COMMAND", "pass Change Engine arguments after 'change'")
        return delegate_change(snapshot, args.change_args)
    except HostError as exc:
        print(json.dumps({"result": "FAIL", "classification": exc.classification, "detail": exc.detail}, indent=2))
        return 1
    except Exception as exc:
        print(json.dumps({"result": "FAIL", "classification": "UNEXPECTED_EXCEPTION", "detail": repr(exc)}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
