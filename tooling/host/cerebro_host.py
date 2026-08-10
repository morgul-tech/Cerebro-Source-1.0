#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from diagnostic_capsule import latest_unresolved_context

HOST_VERSION = "0.5.0"
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
    engines = [
        snapshot / "tooling" / "change" / "change_engine.py",
        snapshot / "tooling" / "delivery" / "delivery_controller.py",
        snapshot / "tooling" / "closure" / "closure_engine.py",
        snapshot / "tooling" / "host" / "diagnostic_capsule.py",
    ]
    if not marker.is_file() or not all(engine.is_file() for engine in engines):
        return False
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        return data.get("commit") == commit and data.get("host_snapshot_schema") == "cerebro-tooling-snapshot/v0.2"
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
    required_engines = [
        target / "tooling" / "change" / "change_engine.py",
        target / "tooling" / "delivery" / "delivery_controller.py",
        target / "tooling" / "closure" / "closure_engine.py",
        target / "tooling" / "host" / "diagnostic_capsule.py",
    ]
    if not all(engine.is_file() for engine in required_engines):
        try:
            capture(source, "worktree", "remove", "--force", str(target))
        finally:
            raise HostError("TOOLING_ENGINE_MISSING", commit)
    marker = {
        "host_snapshot_schema": "cerebro-tooling-snapshot/v0.2",
        "commit": commit,
        "source_repository": SOURCE_REPOSITORY,
    }
    (target / ".cerebro-tooling-snapshot.json").write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
    return target


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def operation_root() -> Path:
    root = LOCALAPPDATA / "Cerebro" / "operations"
    root.mkdir(parents=True, exist_ok=True)
    return root


def write_operation_journal(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def supervise_native_process(
    cmd: list[str],
    cwd: Path,
    component: str,
    heartbeat_seconds: float = 2.0,
    env: dict[str, str] | None = None,
) -> dict:
    operation_id = hashlib.sha256(
        f"{component}|{cwd}|{utc_now()}|{os.getpid()}".encode("utf-8")
    ).hexdigest()[:16]
    journal_path = operation_root() / f"{operation_id}.json"
    journal = {
        "schema": "cerebro-operation-journal/v0.1",
        "operation_id": operation_id,
        "component": component,
        "command": cmd,
        "cwd": str(cwd),
        "state": "STARTING",
        "started_at": utc_now(),
        "heartbeat_at": None,
        "process_observation": {
            "pid": None,
            "exit_status": "UNKNOWN",
            "exit_code": None,
            "stdout_mode": "INHERITED",
            "stderr_mode": "INHERITED",
        },
        "interruptibility": "STAGE_SPECIFIC",
        "stall_policy": "OBSERVE_DO_NOT_FORCE_KILL",
        "diagnostic_context": {
            "status": (env or {}).get("CEREBRO_DIAGNOSTIC_CONTEXT_STATUS", "NONE"),
            "capsule_id": (env or {}).get("CEREBRO_ACTIVE_DIAGNOSTIC_ID"),
            "capsule_path": (env or {}).get("CEREBRO_ACTIVE_DIAGNOSTIC_CAPSULE"),
            "capsule_fingerprint": (env or {}).get("CEREBRO_ACTIVE_DIAGNOSTIC_FINGERPRINT"),
        },
    }
    write_operation_journal(journal_path, journal)

    try:
        process = subprocess.Popen(cmd, cwd=cwd, env=env)
    except OSError as exc:
        journal["state"] = "PROCESS_START_FAILURE"
        journal["completed_at"] = utc_now()
        journal["error"] = repr(exc)
        write_operation_journal(journal_path, journal)
        raise HostError("PROCESS_START_FAILURE", str(exc)) from exc

    journal["state"] = "RUNNING"
    journal["process_observation"]["pid"] = process.pid
    journal["heartbeat_at"] = utc_now()
    write_operation_journal(journal_path, journal)

    while True:
        observed = process.poll()
        if observed is not None:
            break
        time.sleep(heartbeat_seconds)
        journal["heartbeat_at"] = utc_now()
        journal["state"] = "RUNNING"
        write_operation_journal(journal_path, journal)

    exit_code = process.wait()
    journal["completed_at"] = utc_now()
    journal["heartbeat_at"] = journal.get("heartbeat_at") or utc_now()
    if exit_code is None:
        journal["state"] = "PROCESS_COMPLETED_EXIT_UNKNOWN"
        journal["process_observation"]["exit_status"] = "UNKNOWN"
        journal["process_observation"]["exit_code"] = None
        classification = "UNKNOWN"
    else:
        journal["state"] = "PROCESS_COMPLETED"
        journal["process_observation"]["exit_status"] = "AVAILABLE"
        journal["process_observation"]["exit_code"] = int(exit_code)
        classification = "PASS" if int(exit_code) == 0 else "FAIL"
    journal["process_observation"]["classification"] = classification
    write_operation_journal(journal_path, journal)
    return {
        "operation_id": operation_id,
        "journal_path": str(journal_path),
        "exit_status": journal["process_observation"]["exit_status"],
        "exit_code": journal["process_observation"]["exit_code"],
        "classification": classification,
    }


def delegate(snapshot: Path, component: str, arguments: list[str]) -> int:
    engines = {
        "change": snapshot / "tooling" / "change" / "change_engine.py",
        "delivery": snapshot / "tooling" / "delivery" / "delivery_controller.py",
        "closure": snapshot / "tooling" / "closure" / "closure_engine.py",
        "diagnostics": snapshot / "tooling" / "host" / "diagnostic_capsule.py",
    }
    engine = engines[component]
    cmd = [sys.executable, str(engine), *arguments]

    env = os.environ.copy()
    diagnostic = latest_unresolved_context()
    if diagnostic:
        env["CEREBRO_DIAGNOSTIC_CONTEXT_STATUS"] = str(diagnostic.get("status", "UNKNOWN"))
        if diagnostic.get("path"):
            env["CEREBRO_ACTIVE_DIAGNOSTIC_CAPSULE"] = str(diagnostic["path"])
        if diagnostic.get("capsule_id"):
            env["CEREBRO_ACTIVE_DIAGNOSTIC_ID"] = str(diagnostic["capsule_id"])
        if diagnostic.get("fingerprint"):
            env["CEREBRO_ACTIVE_DIAGNOSTIC_FINGERPRINT"] = str(diagnostic["fingerprint"])
    else:
        env["CEREBRO_DIAGNOSTIC_CONTEXT_STATUS"] = "NONE"

    observation = supervise_native_process(cmd, snapshot, component, env=env)
    if observation["exit_status"] == "UNKNOWN":
        print(json.dumps({
            "result": "UNKNOWN",
            "classification": "PROCESS_EXIT_STATUS_UNAVAILABLE",
            "process_observation": observation,
            "material_poststate_required": True,
        }, indent=2))
        return 2
    return int(observation["exit_code"])


def selftest() -> dict:
    # Host selftest is intentionally narrow: it proves parsing/dispatch shape,
    # not repository semantics that require a real Cerebro Source checkout.
    material = f"{HOST_VERSION}|{SOURCE_REPOSITORY}|{SNAPSHOT_ROOT}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return {
        "schema": "cerebro-host-selftest/v0.1",
        "result": "PASS",
        "host_version": HOST_VERSION,
        "dispatch_contract": "snapshot_rehydrate_diagnostics_then_delegate",
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
    delivery = sub.add_parser("delivery")
    delivery.add_argument("delivery_args", nargs=argparse.REMAINDER)
    closure = sub.add_parser("closure")
    closure.add_argument("closure_args", nargs=argparse.REMAINDER)
    diagnostics = sub.add_parser("diagnostics")
    diagnostics.add_argument("diagnostic_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    try:
        if args.command == "selftest":
            print(json.dumps(selftest(), indent=2))
            return 0
        source = locate_source(args.source_root)
        commit = verify_source(source, args.source_commit)
        snapshot = create_snapshot(source, commit)
        component_args = {
            "change": getattr(args, "change_args", None),
            "delivery": getattr(args, "delivery_args", None),
            "closure": getattr(args, "closure_args", None),
            "diagnostics": getattr(args, "diagnostic_args", None),
        }[args.command]
        if not component_args:
            raise HostError("MISSING_DELEGATE_COMMAND", f"pass engine arguments after '{args.command}'")
        return delegate(snapshot, args.command, component_args)
    except HostError as exc:
        print(json.dumps({"result": "FAIL", "classification": exc.classification, "detail": exc.detail}, indent=2))
        return 1
    except Exception as exc:
        print(json.dumps({"result": "FAIL", "classification": "UNEXPECTED_EXCEPTION", "detail": repr(exc)}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
