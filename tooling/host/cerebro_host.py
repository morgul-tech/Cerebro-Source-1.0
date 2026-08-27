#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib.util
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

HOST_VERSION = "0.8.0"
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
        snapshot / "tooling" / "runtime-host" / "first_light_runtime.py",
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
        shutil.rmtree(target)
    capture(source, "worktree", "prune")
    capture(source, "worktree", "add", "--detach", "--force", str(target), commit)
    required_engines = [
        target / "tooling" / "change" / "change_engine.py",
        target / "tooling" / "delivery" / "delivery_controller.py",
        target / "tooling" / "closure" / "closure_engine.py",
        target / "tooling" / "host" / "diagnostic_capsule.py",
        target / "tooling" / "runtime-host" / "first_light_runtime.py",
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


def new_operation_id(component: str, cwd: Path) -> str:
    return hashlib.sha256(
        f"{component}|{cwd}|{utc_now()}|{os.getpid()}".encode("utf-8")
    ).hexdigest()[:16]


def supervise_native_process(
    cmd: list[str],
    cwd: Path,
    component: str,
    heartbeat_seconds: float = 2.0,
    env: dict[str, str] | None = None,
    operation_id: str | None = None,
) -> dict:
    operation_id = operation_id or new_operation_id(component, cwd)
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


def _load_snapshot_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise HostError("FIRST_LIGHT_MODULE_LOAD_FAILURE", str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _argument_value(arguments: list[str], flag: str) -> str | None:
    try:
        index = arguments.index(flag)
    except ValueError:
        return None
    if index + 1 >= len(arguments):
        raise HostError("FIRST_LIGHT_ARGUMENT_INVALID", flag)
    return arguments[index + 1]


def _replace_argument(arguments: list[str], flag: str, value: str) -> list[str]:
    output = list(arguments)
    try:
        index = output.index(flag)
    except ValueError:
        output.extend([flag, value])
        return output
    if index + 1 >= len(output):
        raise HostError("FIRST_LIGHT_ARGUMENT_INVALID", flag)
    output[index + 1] = value
    return output


def prepare_first_light_precommit(
    snapshot: Path,
    arguments: list[str],
    *,
    source_commit: str,
    operation_id: str,
) -> tuple[list[str], dict[str, str]]:
    if "--invocation-envelope" in arguments:
        raise HostError("FIRST_LIGHT_PRECOMMIT_ENVELOPE_EXTERNAL_OVERRIDE_PROHIBITED", "--invocation-envelope")
    event_value = _argument_value(arguments, "--event-file")
    db_value = _argument_value(arguments, "--db")
    mode = _argument_value(arguments, "--mode") or "REAL_FIRST_LIGHT"
    if not event_value:
        raise HostError("FIRST_LIGHT_PRECOMMIT_EVENT_FILE_REQUIRED", "host dispatch requires --event-file")
    if not db_value:
        raise HostError("FIRST_LIGHT_PRECOMMIT_DB_REQUIRED", "host dispatch requires --db")

    event_path = Path(event_value)
    if not event_path.is_absolute():
        event_path = snapshot / event_path
    event_path = event_path.resolve()
    db_path = Path(db_value)
    if not db_path.is_absolute():
        db_path = snapshot / db_path
    db_path = db_path.resolve()

    try:
        raw_event = event_path.read_bytes()
        event = json.loads(raw_event.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HostError("FIRST_LIGHT_PRECOMMIT_EVENT_READ_FAILURE", str(exc)) from exc
    if not isinstance(event, dict):
        raise HostError("FIRST_LIGHT_PRECOMMIT_EVENT_INVALID", "event must be object")

    runtime_path = snapshot / "tooling" / "runtime-host" / "first_light_runtime.py"
    try:
        runtime = _load_snapshot_module(runtime_path, f"cerebro_first_light_precommit_{operation_id}")
        envelope = runtime.build_precommit_identity(
            event=event,
            event_source_bytes=raw_event,
            event_source_path=event_path,
            db_path=db_path,
            mode=mode,
            source_commit=source_commit,
            host_operation_id=operation_id,
        )
        frozen_event_bytes = runtime.canonical_event_bytes(event)
    except Exception as exc:
        classification = getattr(exc, "classification", "FIRST_LIGHT_PRECOMMIT_BUILD_FAILURE")
        detail = getattr(exc, "detail", repr(exc))
        raise HostError(str(classification), str(detail)) from exc

    precommit_root = operation_root() / "first-light-precommit"
    precommit_root.mkdir(parents=True, exist_ok=True)
    frozen_event_path = precommit_root / f"{operation_id}.event.json"
    envelope_path = precommit_root / f"{operation_id}.envelope.json"
    frozen_temp = frozen_event_path.with_suffix(".event.json.tmp")
    frozen_temp.write_bytes(frozen_event_bytes)
    os.replace(frozen_temp, frozen_event_path)
    write_operation_journal(envelope_path, envelope)

    rewritten = _replace_argument(arguments, "--event-file", str(frozen_event_path))
    rewritten.extend(["--invocation-envelope", str(envelope_path)])
    return rewritten, {
        "CEREBRO_SOURCE_COMMIT": source_commit,
        "CEREBRO_HOST_OPERATION_ID": operation_id,
        "CEREBRO_FIRST_LIGHT_CORRELATION_ID": str(envelope["correlation_id"]),
        "CEREBRO_FIRST_LIGHT_PRECOMMIT_FINGERPRINT": str(envelope["precommit_fingerprint"]),
    }


def delegate(snapshot: Path, component: str, arguments: list[str], source_commit: str) -> int:
    engines = {
        "change": snapshot / "tooling" / "change" / "change_engine.py",
        "delivery": snapshot / "tooling" / "delivery" / "delivery_controller.py",
        "closure": snapshot / "tooling" / "closure" / "closure_engine.py",
        "diagnostics": snapshot / "tooling" / "host" / "diagnostic_capsule.py",
        "runtime-first-light": snapshot / "tooling" / "runtime-host" / "first_light_runtime.py",
    }
    engine = engines[component]
    operation_id = new_operation_id(component, snapshot)
    component_arguments = list(arguments)

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

    if component == "runtime-first-light":
        component_arguments, precommit_env = prepare_first_light_precommit(
            snapshot,
            component_arguments,
            source_commit=source_commit,
            operation_id=operation_id,
        )
        env.update(precommit_env)

    cmd = [sys.executable, str(engine), *component_arguments]
    observation = supervise_native_process(
        cmd,
        snapshot,
        component,
        env=env,
        operation_id=operation_id,
    )
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
    material = f"{HOST_VERSION}|{SOURCE_REPOSITORY}|{SNAPSHOT_ROOT}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return {
        "schema": "cerebro-host-selftest/v0.1",
        "result": "PASS",
        "host_version": HOST_VERSION,
        "dispatch_contract": "snapshot_rehydrate_diagnostics_precommit_first_light_then_delegate",
        "source_mutation": False,
        "fingerprint": digest,
    }


DELEGATE_COMMANDS = ("change", "delivery", "closure", "diagnostics", "runtime-first-light")


def parse_host_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cerebro Host")
    parser.add_argument("--source-root")
    parser.add_argument("--source-commit")
    parser.add_argument("command", choices=("selftest", *DELEGATE_COMMANDS))
    parser.add_argument("delegate_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command == "selftest" and args.delegate_args:
        parser.error("selftest does not accept delegated arguments")
    return args


def main() -> int:
    args = parse_host_arguments()

    try:
        if args.command == "selftest":
            print(json.dumps(selftest(), indent=2))
            return 0
        source = locate_source(args.source_root)
        commit = verify_source(source, args.source_commit)
        snapshot = create_snapshot(source, commit)
        component_args = list(args.delegate_args)
        if not component_args:
            raise HostError("MISSING_DELEGATE_COMMAND", f"pass engine arguments after '{args.command}'")
        return delegate(snapshot, args.command, component_args, commit)
    except HostError as exc:
        print(json.dumps({"result": "FAIL", "classification": exc.classification, "detail": exc.detail}, indent=2))
        return 1
    except Exception as exc:
        print(json.dumps({"result": "FAIL", "classification": "UNEXPECTED_EXCEPTION", "detail": repr(exc)}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
