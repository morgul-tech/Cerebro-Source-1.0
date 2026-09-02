#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from decimal import Decimal, InvalidOperation
import shutil
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Iterator, Mapping

from diagnostic_capsule import capture_capsule, latest_unresolved_context

HOST_VERSION = "0.9.0"
SOURCE_REPOSITORY = "morgul-tech/Cerebro-Source-1.0"
DEFAULT_SOURCE_CANDIDATES = [
    Path(r"D:\Cerebro\Source\Cerebro_Source_v1.0"),
    Path(r"C:\Cerebro\Source\Cerebro_Source_v1.0"),
]
LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share"))
SNAPSHOT_ROOT = LOCALAPPDATA / "Cerebro" / "tooling-snapshots"
SNAPSHOT_LOCK_TIMEOUT_SECONDS = 60.0

RUNTIME2_REQUEST_SCHEMA = "cerebro-runtime2-supervision-request/v1"
RUNTIME2_RESULT_SCHEMA = "cerebro-runtime2-supervision-result/v1"
RUNTIME2_JOURNAL_SCHEMA = "cerebro-runtime2-operation-journal/v1"
RUNTIME2_PROCESS_OBSERVATION_SCHEMA = "cerebro-operational-evidence/v0.1"
RUNTIME2_HEARTBEAT_SCHEMA = "cerebro-runtime2-heartbeat-summary/v1"
RUNTIME2_EVIDENCE_BINDING_SCHEMA = "cerebro-runtime2-evidence-binding/v1"
OPERATIONAL_EVIDENCE_SCHEMA = "cerebro-operational-evidence/v0.1"
SENSITIVITY_VALUES = {"PUBLIC", "INTERNAL", "SENSITIVE", "SECRET"}
REDACTION_VALUES = {"NONE", "MASK", "HASH", "SUMMARY", "OMIT"}
RUNTIME2_PROCESS_MODES = {"SUPERVISED_PROCESS", "ISOLATED_CAPABILITY_WORKER"}
RUNTIME2_TIMEOUT_ACTIONS = {
    "OBSERVE_ONLY",
    "REQUEST_COOPERATIVE_STOP",
    "FORCE_TERMINATE_IF_EXPLICITLY_SAFE",
}
RUNTIME2_IO_MODES = {"INHERIT"}
RUNTIME2_TERMINAL_JOURNAL_STATES = {
    "PROCESS_OBSERVED",
    "START_FAILURE",
    "SUPERVISOR_INTERRUPTED",
    "SUPERVISOR_FAILURE_POST_START",
}


class HostError(RuntimeError):
    def __init__(self, classification: str, detail: str):
        super().__init__(detail)
        self.classification = classification
        self.detail = detail


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _jcs_sort_key(value: str) -> bytes:
    # RFC 8785 sorts object property names as arrays of UTF-16 code units.
    return value.encode("utf-16-be", errors="surrogatepass")


def jcs_canonical_bytes(value: Any) -> bytes:
    """RFC 8785 JCS for the Runtime2 identity subset.

    Runtime2 pre-normalizes numeric policy values to decimal strings before
    identity construction. The canonical identity surface therefore contains
    only JSON strings, booleans, null, arrays and objects; this keeps the JCS
    implementation exact without depending on platform float serialization.
    """
    if value is None:
        return b"null"
    if value is True:
        return b"true"
    if value is False:
        return b"false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if isinstance(value, (int, float, Decimal)):
        raise HostError(
            "RUNTIME2_JCS_NUMERIC_PRENORMALIZATION_REQUIRED",
            repr(value),
        )
    if isinstance(value, (list, tuple)):
        return b"[" + b",".join(jcs_canonical_bytes(item) for item in value) + b"]"
    if isinstance(value, Mapping):
        items: list[bytes] = []
        for key in sorted(value.keys(), key=lambda item: _jcs_sort_key(str(item))):
            if not isinstance(key, str):
                raise HostError("RUNTIME2_JCS_OBJECT_KEY_INVALID", repr(key))
            items.append(jcs_canonical_bytes(key) + b":" + jcs_canonical_bytes(value[key]))
        return b"{" + b",".join(items) + b"}"
    raise HostError("RUNTIME2_JCS_TYPE_UNSUPPORTED", type(value).__name__)


def canonical_fingerprint(value: Any) -> str:
    return sha256_bytes(jcs_canonical_bytes(value))


def _decimal_identity(value: Any, name: str) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise HostError("RUNTIME2_REQUEST_INVALID", f"{name}:finite-number-required") from exc
    if not number.is_finite():
        raise HostError("RUNTIME2_REQUEST_INVALID", f"{name}:finite-number-required")
    if number == 0:
        return "0"
    normalized = number.normalize()
    rendered = format(normalized, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise HostError("RUNTIME2_REQUEST_INVALID", f"{name}:mapping-required")
    return dict(value)


def _require_text(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise HostError("RUNTIME2_REQUEST_INVALID", f"{name}:non-empty-string-required")
    return text


def _require_sha256(value: Any, name: str) -> str:
    text = _require_text(value, name).lower()
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise HostError("RUNTIME2_REQUEST_INVALID", f"{name}:sha256-required")
    return text


def _stable_binding_ref(value: Any, name: str) -> dict[str, str]:
    if isinstance(value, str):
        return {"ref": _require_text(value, name)}
    data = _require_mapping(value, name)
    ref = _require_text(data.get("ref"), f"{name}.ref")
    result = {"ref": ref}
    if data.get("fingerprint") is not None:
        result["fingerprint"] = _require_sha256(data.get("fingerprint"), f"{name}.fingerprint")
    return result


def _require_fingerprinted_ref(value: Any, name: str) -> dict[str, str]:
    data = _require_mapping(value, name)
    return {
        "ref": _require_text(data.get("ref"), f"{name}.ref"),
        "fingerprint": _require_sha256(data.get("fingerprint"), f"{name}.fingerprint"),
    }


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


def _lock_file(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def snapshot_creation_lock(commit: str) -> Iterator[None]:
    lock_path = SNAPSHOT_ROOT / ".locks" / f"{commit}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    locked = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + SNAPSHOT_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                _lock_file(handle)
                locked = True
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise HostError("SNAPSHOT_LOCK_TIMEOUT", str(lock_path)) from exc
                time.sleep(0.025)
        yield
    finally:
        if locked:
            _unlock_file(handle)
        handle.close()


def snapshot_is_valid(snapshot: Path, commit: str) -> bool:
    marker = snapshot / ".cerebro-tooling-snapshot.json"
    engines = [
        snapshot / "tooling" / "change" / "change_engine.py",
        snapshot / "tooling" / "delivery" / "delivery_controller.py",
        snapshot / "tooling" / "closure" / "closure_engine.py",
        snapshot / "tooling" / "host" / "diagnostic_capsule.py",
        snapshot / "tooling" / "runtime-host" / "first_light_runtime.py",
        snapshot / "tooling" / "runtime-host" / "cerebro_runtime.py",
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
    with snapshot_creation_lock(commit):
        if snapshot_is_valid(target, commit):
            return target
        if target.exists():
            try:
                capture(source, "worktree", "remove", "--force", str(target))
            except HostError:
                shutil.rmtree(target, ignore_errors=True)
        capture(source, "worktree", "prune")
        try:
            capture(source, "worktree", "add", "--detach", "--force", str(target), commit)
            required_engines = [
                target / "tooling" / "change" / "change_engine.py",
                target / "tooling" / "delivery" / "delivery_controller.py",
                target / "tooling" / "closure" / "closure_engine.py",
                target / "tooling" / "host" / "diagnostic_capsule.py",
                target / "tooling" / "runtime-host" / "first_light_runtime.py",
                target / "tooling" / "runtime-host" / "cerebro_runtime.py",
            ]
            if not all(engine.is_file() for engine in required_engines):
                raise HostError("TOOLING_ENGINE_MISSING", commit)
            marker = {
                "host_snapshot_schema": "cerebro-tooling-snapshot/v0.2",
                "commit": commit,
                "source_repository": SOURCE_REPOSITORY,
            }
            marker_path = target / ".cerebro-tooling-snapshot.json"
            marker_temp = marker_path.with_name(f"{marker_path.name}.{os.getpid()}.tmp")
            marker_temp.write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
            os.replace(marker_temp, marker_path)
            if not snapshot_is_valid(target, commit):
                raise HostError("TOOLING_SNAPSHOT_VALIDATION_FAILED", commit)
        except Exception:
            try:
                capture(source, "worktree", "remove", "--force", str(target))
            except HostError:
                shutil.rmtree(target, ignore_errors=True)
            capture(source, "worktree", "prune")
            raise
    return target


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
    """Legacy host supervision retained for current non-Runtime2 dispatch.

    Runtime2 must use supervise_runtime2_process(). The legacy return shape is
    preserved to avoid silently breaking current change/delivery/closure paths.
    """
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
        # Legacy compatibility only. Runtime2 never consumes this semanticized
        # classification; its typed path returns neutral process evidence.
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


def runtime2_operation_root() -> Path:
    root = LOCALAPPDATA / "Cerebro" / "runtime2-operations"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(raw)
    os.replace(temporary, path)
    return sha256_bytes(raw)


def _file_evidence_binding(path: Path, kind: str, schema: str) -> dict[str, Any]:
    raw = path.read_bytes()
    fingerprint = sha256_bytes(raw)
    return {
        "schema": RUNTIME2_EVIDENCE_BINDING_SCHEMA,
        "kind": kind,
        "artifact_schema": schema,
        "ref": str(path),
        "fingerprint": fingerprint,
        "sha256": fingerprint,
        "byte_count": len(raw),
        "producer_ref": "tooling.host",
        "custody": {
            "class": "DURABLE_LOCAL_HOST_EVIDENCE",
            "liveness": "PRESENT_VERIFIED_AT_BINDING",
        },
    }


def _operational_evidence(
    compiled: dict[str, Any],
    *,
    evidence_kind: str,
    probe_ref: str,
    status: str,
    result: str,
    value: Any,
    error: Any = None,
) -> dict[str, Any]:
    basis_refs = [
        compiled["execution_basis_ref"]["ref"],
        "receipt-subject:" + compiled["receipt_subject_fingerprint"],
        "event:" + compiled["event_fingerprint"],
        "plan:" + compiled["plan_fingerprint"],
        "supervision-subject:" + compiled["supervision_subject_fingerprint"],
    ]
    id_material = {
        "evidence_kind": evidence_kind,
        "subject_ref": compiled["supervision_subject_fingerprint"],
        "probe_ref": probe_ref,
        "status": status,
        "result": result,
        "value": value,
        "basis_refs": basis_refs,
        "producer_ref": "tooling.host",
    }
    evidence_id = "EVID-" + sha256_bytes(
        json.dumps(
            id_material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    )[:20].upper()
    return {
        "schema": OPERATIONAL_EVIDENCE_SCHEMA,
        "evidence_id": evidence_id,
        "evidence_kind": evidence_kind,
        "subject_ref": compiled["supervision_subject_fingerprint"],
        "probe_ref": probe_ref,
        "status": status,
        "result": result,
        "value": value,
        "source": "tooling.host/runtime2-supervision",
        "observed_at": utc_now(),
        "freshness": {
            "freshness_kind": "STATE_BOUND",
            "basis_fingerprint": compiled["supervision_subject_fingerprint"],
            "invalidation_triggers": [
                "SUPERVISION_SUBJECT_CHANGE",
                "EXECUTION_BASIS_CHANGE",
            ],
        },
        "confidence": "VERIFIED",
        "sensitivity": compiled["sensitivity"],
        "redaction": compiled["redaction"],
        "error": error,
        "basis_refs": basis_refs,
        "producer_ref": "tooling.host",
    }


def normalize_exit_observation(exit_code: int | None) -> dict[str, Any]:
    if exit_code is None:
        return {
            "exit_status": "UNAVAILABLE",
            "exit_code": None,
            "semantic_result": "UNRESOLVED_BY_SUPERVISOR",
        }
    return {
        "exit_status": "AVAILABLE",
        "exit_code": int(exit_code),
        "semantic_result": "UNRESOLVED_BY_SUPERVISOR",
    }


def _environment_values(binding: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any]]:
    for required in ("values", "declared_keys", "secret_keys"):
        if required not in binding:
            raise HostError(
                "RUNTIME2_REQUEST_INVALID",
                f"environment_binding.{required}:required",
            )
    values = binding.get("values")
    if not isinstance(values, Mapping):
        raise HostError("RUNTIME2_REQUEST_INVALID", "environment_binding.values:mapping-required")
    env: dict[str, str] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not key:
            raise HostError("RUNTIME2_REQUEST_INVALID", "environment-binding-key-invalid")
        if not isinstance(value, str):
            raise HostError("RUNTIME2_REQUEST_INVALID", f"environment-binding-value-not-string:{key}")
        env[key] = value

    declared = binding.get("declared_keys")
    if not isinstance(declared, list) or any(not isinstance(item, str) for item in declared):
        raise HostError("RUNTIME2_REQUEST_INVALID", "environment_binding.declared_keys:string-list-required")
    if sorted(declared) != sorted(env):
        raise HostError("RUNTIME2_ENVIRONMENT_BINDING_MISMATCH", "declared_keys-do-not-equal-values-keys")

    secret_keys = binding.get("secret_keys")
    if not isinstance(secret_keys, list) or any(not isinstance(item, str) for item in secret_keys):
        raise HostError("RUNTIME2_REQUEST_INVALID", "environment_binding.secret_keys:string-list-required")
    if not set(secret_keys).issubset(env):
        raise HostError("RUNTIME2_REQUEST_INVALID", "environment_binding.secret_keys:not-subset")

    identity = {
        "keys": sorted(env),
        "secret_keys": sorted(secret_keys),
        "values_fingerprint": canonical_fingerprint(env),
    }
    return env, identity


def _compile_runtime2_request(request: dict[str, Any]) -> dict[str, Any]:
    if request.get("schema") != RUNTIME2_REQUEST_SCHEMA:
        raise HostError("RUNTIME2_REQUEST_INVALID", "schema")

    invocation_id = _require_text(request.get("invocation_id"), "invocation_id")
    receipt_subject = _require_sha256(request.get("receipt_subject_fingerprint"), "receipt_subject_fingerprint")
    event_fp = _require_sha256(request.get("event_fingerprint"), "event_fingerprint")
    plan_fp = _require_sha256(request.get("plan_fingerprint"), "plan_fingerprint")
    node_id = _require_text(request.get("node_id"), "node_id")
    execution_basis_ref = _require_fingerprinted_ref(request.get("execution_basis_ref"), "execution_basis_ref")
    capability_ref = _require_text(request.get("capability_binding_ref"), "capability_binding_ref")
    mode = _require_text(request.get("execution_mode_ref"), "execution_mode_ref").upper()
    if mode not in RUNTIME2_PROCESS_MODES:
        raise HostError("RUNTIME2_EXECUTION_MODE_NOT_PROCESS_SUPERVISED", mode)

    executable = _require_mapping(request.get("executable_binding"), "executable_binding")
    resolved_path = Path(_require_text(executable.get("resolved_path"), "executable_binding.resolved_path"))
    if not resolved_path.is_absolute():
        raise HostError("RUNTIME2_EXECUTABLE_BINDING_INVALID", "resolved_path-must-be-absolute")
    logical_role = _require_text(executable.get("logical_role"), "executable_binding.logical_role")
    executable_sha = _require_sha256(executable.get("content_sha256"), "executable_binding.content_sha256")
    executable_version = _require_text(executable.get("version"), "executable_binding.version")

    argv_binding = _require_mapping(request.get("argv_binding"), "argv_binding")
    argv = argv_binding.get("argv")
    if not isinstance(argv, list) or not argv or any(not isinstance(item, str) for item in argv):
        raise HostError("RUNTIME2_REQUEST_INVALID", "argv_binding.argv:non-empty-string-list-required")
    if Path(argv[0]) != resolved_path:
        raise HostError("RUNTIME2_ARGV_EXECUTABLE_BINDING_MISMATCH", f"argv0={argv[0]} resolved={resolved_path}")

    environment_binding = _require_mapping(request.get("environment_binding"), "environment_binding")
    env, environment_identity = _environment_values(environment_binding)

    cwd_policy_ref = _require_text(request.get("cwd_policy_ref"), "cwd_policy_ref")
    cwd_binding = _require_mapping(request.get("cwd_binding"), "cwd_binding")
    cwd_role = _require_text(cwd_binding.get("role"), "cwd_binding.role")
    cwd_locator = Path(_require_text(cwd_binding.get("resolved_cwd_locator"), "cwd_binding.resolved_cwd_locator"))
    if not cwd_locator.is_absolute():
        raise HostError("RUNTIME2_CWD_BINDING_INVALID", "resolved_cwd_locator-must-be-absolute")

    io_policy_ref = _require_text(request.get("io_policy_ref"), "io_policy_ref")
    io_policy = _require_mapping(request.get("io_policy"), "io_policy")
    io_identity = {}
    for stream in ("stdin", "stdout", "stderr"):
        if stream not in io_policy:
            raise HostError("RUNTIME2_REQUEST_INVALID", f"io_policy.{stream}:required")
        mode_value = _require_text(io_policy.get(stream), f"io_policy.{stream}").upper()
        if mode_value not in RUNTIME2_IO_MODES:
            raise HostError("RUNTIME2_IO_POLICY_UNSUPPORTED", f"{stream}:{mode_value}")
        io_identity[stream] = mode_value

    heartbeat_policy_ref = _require_text(request.get("heartbeat_policy_ref"), "heartbeat_policy_ref")
    heartbeat_policy = _require_mapping(request.get("heartbeat_policy"), "heartbeat_policy")
    try:
        heartbeat_seconds = float(heartbeat_policy.get("interval_seconds"))
    except (TypeError, ValueError) as exc:
        raise HostError("RUNTIME2_REQUEST_INVALID", "heartbeat_policy.interval_seconds:number-required") from exc
    if not (0.005 <= heartbeat_seconds <= 300.0):
        raise HostError("RUNTIME2_REQUEST_INVALID", "heartbeat_policy.interval_seconds:out-of-range")

    timeout_policy_ref = _require_text(request.get("timeout_policy_ref"), "timeout_policy_ref")
    timeout_policy = _require_mapping(request.get("timeout_policy"), "timeout_policy")
    timeout_action = _require_text(timeout_policy.get("action"), "timeout_policy.action").upper()
    if timeout_action not in RUNTIME2_TIMEOUT_ACTIONS:
        raise HostError("RUNTIME2_REQUEST_INVALID", f"timeout_policy.action:{timeout_action}")
    timeout_raw = timeout_policy.get("timeout_seconds")
    timeout_seconds: float | None
    if timeout_raw is None:
        timeout_seconds = None
    else:
        try:
            timeout_seconds = float(timeout_raw)
        except (TypeError, ValueError) as exc:
            raise HostError("RUNTIME2_REQUEST_INVALID", "timeout_policy.timeout_seconds:number-or-null-required") from exc
        if timeout_seconds <= 0:
            raise HostError("RUNTIME2_REQUEST_INVALID", "timeout_policy.timeout_seconds:must-be-positive")
    if timeout_seconds is None and timeout_action != "OBSERVE_ONLY":
        raise HostError("RUNTIME2_UNDECLARED_TIMEOUT_TERMINATION_BLOCKED", timeout_action)

    termination_policy_ref = _require_text(request.get("termination_policy_ref"), "termination_policy_ref")
    termination_policy = _require_mapping(request.get("termination_policy"), "termination_policy")
    for required in ("force_terminate_explicitly_safe", "force_grace_seconds", "cooperative_signal"):
        if required not in termination_policy:
            raise HostError("RUNTIME2_REQUEST_INVALID", f"termination_policy.{required}:required")
    if not isinstance(termination_policy.get("force_terminate_explicitly_safe"), bool):
        raise HostError(
            "RUNTIME2_REQUEST_INVALID",
            "termination_policy.force_terminate_explicitly_safe:boolean-required",
        )
    force_safe = termination_policy.get("force_terminate_explicitly_safe") is True
    if timeout_action == "FORCE_TERMINATE_IF_EXPLICITLY_SAFE" and not force_safe:
        raise HostError("RUNTIME2_FORCE_TERMINATION_NOT_EXPLICITLY_SAFE", "force_terminate_explicitly_safe!=true")
    cooperative_signal = str(termination_policy.get("cooperative_signal") or "").strip().upper() or None
    if timeout_action == "REQUEST_COOPERATIVE_STOP" and cooperative_signal not in {"SIGTERM", "SIGINT"}:
        raise HostError("RUNTIME2_COOPERATIVE_STOP_BINDING_MISSING", "cooperative_signal must be SIGTERM or SIGINT")

    if "progress_policy_ref" not in request:
        raise HostError("RUNTIME2_REQUEST_INVALID", "progress_policy_ref:required-nullable")
    progress_policy_ref = request.get("progress_policy_ref")
    progress_identity: dict[str, Any] | None = None
    progress_marker_path: Path | None = None
    if progress_policy_ref is not None:
        progress_policy_ref = _require_text(progress_policy_ref, "progress_policy_ref")
        progress_policy = _require_mapping(request.get("progress_policy"), "progress_policy")
        observation_kind = _require_text(
            progress_policy.get("observation_kind"), "progress_policy.observation_kind"
        ).upper()
        if observation_kind != "FILE_FINGERPRINT_CHANGE":
            raise HostError("RUNTIME2_PROGRESS_OBSERVER_UNSUPPORTED", observation_kind)
        marker = _require_mapping(progress_policy.get("marker_binding"), "progress_policy.marker_binding")
        marker_role = _require_text(marker.get("role"), "progress_policy.marker_binding.role")
        progress_marker_path = Path(
            _require_text(
                marker.get("resolved_path"),
                "progress_policy.marker_binding.resolved_path",
            )
        )
        if not progress_marker_path.is_absolute():
            raise HostError(
                "RUNTIME2_PROGRESS_BINDING_INVALID",
                "marker resolved_path must be absolute",
            )
        progress_identity = {
            "observation_kind": observation_kind,
            "marker_role": marker_role,
        }
    elif request.get("progress_policy") is not None:
        raise HostError(
            "RUNTIME2_REQUEST_INVALID",
            "progress_policy must be null when progress_policy_ref is null",
        )

    stall_policy_ref = _require_text(request.get("stall_policy_ref"), "stall_policy_ref")
    stall_policy = _require_mapping(request.get("stall_policy"), "stall_policy")
    if "stall_threshold_seconds" not in stall_policy or "action" not in stall_policy:
        raise HostError(
            "RUNTIME2_REQUEST_INVALID",
            "stall_policy requires stall_threshold_seconds and action",
        )
    stall_action = _require_text(stall_policy.get("action"), "stall_policy.action").upper()
    if stall_action != "OBSERVE_DO_NOT_FORCE_KILL":
        raise HostError("RUNTIME2_STALL_DESTRUCTIVE_ACTION_BLOCKED", stall_action)
    stall_threshold_raw = stall_policy.get("stall_threshold_seconds")
    stall_threshold_seconds: float | None = None
    if stall_threshold_raw is not None:
        try:
            stall_threshold_seconds = float(stall_threshold_raw)
        except (TypeError, ValueError) as exc:
            raise HostError(
                "RUNTIME2_REQUEST_INVALID",
                "stall_policy.stall_threshold_seconds:number-or-null-required",
            ) from exc
        if stall_threshold_seconds <= 0:
            raise HostError(
                "RUNTIME2_REQUEST_INVALID",
                "stall_policy.stall_threshold_seconds:must-be-positive",
            )
    if progress_policy_ref is None and stall_threshold_seconds is not None:
        raise HostError(
            "RUNTIME2_UNDECLARED_PROGRESS_INFERENCE_BLOCKED",
            "stall threshold requires progress_policy_ref",
        )
    if progress_policy_ref is not None and stall_threshold_seconds is None:
        raise HostError(
            "RUNTIME2_REQUEST_INVALID",
            "declared progress policy requires explicit stall threshold or explicit null-policy ref",
        )
    stall_identity = {
        "action": stall_action,
        "stall_threshold_seconds": (
            _decimal_identity(stall_threshold_seconds, "stall_policy.stall_threshold_seconds")
            if stall_threshold_seconds is not None
            else None
        ),
    }

    sensitivity_rules = _require_mapping(request.get("sensitivity_rules"), "sensitivity_rules")
    for required in ("sensitivity", "redaction", "secret_binding_refs"):
        if required not in sensitivity_rules:
            raise HostError(
                "RUNTIME2_REQUEST_INVALID",
                f"sensitivity_rules.{required}:required",
            )
    sensitivity = _require_text(
        sensitivity_rules.get("sensitivity"), "sensitivity_rules.sensitivity"
    ).upper()
    redaction = _require_text(
        sensitivity_rules.get("redaction"), "sensitivity_rules.redaction"
    ).upper()
    if sensitivity not in SENSITIVITY_VALUES:
        raise HostError("RUNTIME2_REQUEST_INVALID", f"sensitivity:{sensitivity}")
    if redaction not in REDACTION_VALUES:
        raise HostError("RUNTIME2_REQUEST_INVALID", f"redaction:{redaction}")
    secret_binding_refs = sensitivity_rules.get("secret_binding_refs")
    if not isinstance(secret_binding_refs, list) or any(
        not isinstance(item, str) or not item for item in secret_binding_refs
    ):
        raise HostError(
            "RUNTIME2_REQUEST_INVALID",
            "sensitivity_rules.secret_binding_refs:string-list-required",
        )
    sensitivity_identity = {
        "sensitivity": sensitivity,
        "redaction": redaction,
        "secret_binding_refs": sorted(secret_binding_refs),
    }

    failure_policy_ref = _require_text(request.get("failure_policy_ref"), "failure_policy_ref")
    owner_defined_retry_rule = request.get("owner_defined_retry_rule")
    if owner_defined_retry_rule is not None:
        owner_defined_retry_rule = _require_text(
            owner_defined_retry_rule, "owner_defined_retry_rule"
        )

    if "diagnostic_policy_ref" not in request:
        raise HostError("RUNTIME2_REQUEST_INVALID", "diagnostic_policy_ref:required-nullable")
    diagnostic_policy_ref = request.get("diagnostic_policy_ref")
    if diagnostic_policy_ref is not None:
        diagnostic_policy_ref = _require_text(
            diagnostic_policy_ref, "diagnostic_policy_ref"
        )
    diagnostic_context_binding = request.get("diagnostic_context_binding")
    diagnostic_context_identity: dict[str, str] | None = None
    if diagnostic_context_binding is not None:
        diagnostic_context_identity = _stable_binding_ref(
            diagnostic_context_binding, "diagnostic_context_binding"
        )
        if diagnostic_policy_ref is None:
            raise HostError(
                "RUNTIME2_REQUEST_INVALID",
                "diagnostic_context_binding requires diagnostic_policy_ref",
            )

    worker_identity: dict[str, Any] | None = None
    isolation_identity: dict[str, Any] | None = None
    if mode == "ISOLATED_CAPABILITY_WORKER":
        worker_runtime = _stable_binding_ref(
            request.get("worker_runtime_binding"), "worker_runtime_binding"
        )
        worker_request = _require_mapping(
            request.get("worker_request_binding"), "worker_request_binding"
        )
        if worker_request.get("immutable") is not True:
            raise HostError(
                "RUNTIME2_WORKER_REQUEST_NOT_IMMUTABLE",
                "worker_request_binding.immutable!=true",
            )
        worker_request_identity = {
            "ref": _require_text(worker_request.get("ref"), "worker_request_binding.ref"),
            "fingerprint": _require_sha256(
                worker_request.get("fingerprint"), "worker_request_binding.fingerprint"
            ),
            "schema": _require_text(
                worker_request.get("schema"), "worker_request_binding.schema"
            ),
            "immutable": True,
        }
        worker_result_contract_ref = _require_text(
            request.get("worker_result_contract_ref"), "worker_result_contract_ref"
        )
        resources_raw = request.get("allowed_resource_bindings")
        if not isinstance(resources_raw, list):
            raise HostError(
                "RUNTIME2_REQUEST_INVALID",
                "allowed_resource_bindings:list-required",
            )
        allowed_resources: list[dict[str, str]] = []
        prohibited_kinds = {"MUTABLE_SOURCE", "CONVERSATION_STATE", "AMBIENT_CONFIG"}
        for index, item in enumerate(resources_raw):
            resource = _require_mapping(item, f"allowed_resource_bindings[{index}]")
            kind = _require_text(
                resource.get("kind"), f"allowed_resource_bindings[{index}].kind"
            ).upper()
            if kind in prohibited_kinds:
                raise HostError(
                    "RUNTIME2_WORKER_HIDDEN_INPUT_BLOCKED",
                    f"prohibited-resource-kind:{kind}",
                )
            access = _require_text(
                resource.get("access"), f"allowed_resource_bindings[{index}].access"
            ).upper()
            if access not in {"READ_ONLY", "WRITE_OUTPUT", "READ_WRITE_EXPLICIT"}:
                raise HostError(
                    "RUNTIME2_REQUEST_INVALID",
                    f"allowed_resource_bindings[{index}].access:{access}",
                )
            allowed_resources.append(
                {
                    "ref": _require_text(
                        resource.get("ref"), f"allowed_resource_bindings[{index}].ref"
                    ),
                    "fingerprint": _require_sha256(
                        resource.get("fingerprint"),
                        f"allowed_resource_bindings[{index}].fingerprint",
                    ),
                    "kind": kind,
                    "access": access,
                }
            )
        # Process isolation is a fresh-process/state boundary, not an implicit
        # security sandbox. Stronger sandboxing is consumed only when an exact
        # capability contract explicitly supplies a verified sandbox binding.
        security_sandbox = request.get("security_sandbox_binding")
        if security_sandbox is not None:
            sandbox = _require_mapping(security_sandbox, "security_sandbox_binding")
            if str(sandbox.get("verification_state") or "").upper() != "VERIFIED":
                raise HostError(
                    "RUNTIME2_SECURITY_SANDBOX_NOT_VERIFIED",
                    "verification_state!=VERIFIED",
                )
            isolation_identity = {
                "security_sandbox_ref": _require_text(
                    sandbox.get("ref"), "security_sandbox_binding.ref"
                ),
                "fingerprint": _require_sha256(
                    sandbox.get("fingerprint"), "security_sandbox_binding.fingerprint"
                ),
                "verification_state": "VERIFIED",
            }
        worker_identity = {
            "worker_runtime_binding": worker_runtime,
            "worker_request_binding": worker_request_identity,
            "worker_result_contract_ref": worker_result_contract_ref,
            "allowed_resource_bindings": sorted(
                allowed_resources,
                key=lambda row: (row["ref"], row["kind"], row["access"]),
            ),
            "one_worker_per_node": True,
            "worker_pool": "PROHIBITED",
        }
    elif any(
        request.get(name) is not None
        for name in (
            "worker_runtime_binding",
            "worker_request_binding",
            "worker_result_contract_ref",
            "allowed_resource_bindings",
            "security_sandbox_binding",
        )
    ):
        raise HostError(
            "RUNTIME2_REQUEST_INVALID",
            "worker-boundary-fields-only-valid-for-isolated-worker",
        )

    argv_identity = {
        "argv_fingerprint": canonical_fingerprint(argv),
        "argc": str(len(argv)),
    }
    timeout_identity = {
        "timeout_seconds": (
            _decimal_identity(timeout_seconds, "timeout_policy.timeout_seconds")
            if timeout_seconds is not None
            else None
        ),
        "action": timeout_action,
    }
    force_grace_seconds = float(termination_policy.get("force_grace_seconds"))
    if force_grace_seconds <= 0:
        raise HostError("RUNTIME2_REQUEST_INVALID", "termination_policy.force_grace_seconds:must-be-positive")
    termination_identity = {
        "force_terminate_explicitly_safe": force_safe,
        "cooperative_signal": cooperative_signal,
        "force_grace_seconds": _decimal_identity(
            force_grace_seconds, "termination_policy.force_grace_seconds"
        ),
    }

    semantic_material = {
        "canonicalization_algorithm": "RFC8785_JCS",
        "receipt_subject_fingerprint": receipt_subject,
        "event_fingerprint": event_fp,
        "plan_fingerprint": plan_fp,
        "node_id": node_id,
        "execution_basis_ref": execution_basis_ref,
        "capability_binding_ref": capability_ref,
        "execution_mode_ref": mode,
        "executable_identity": {
            "logical_role": logical_role,
            "resolved_path": str(resolved_path),
            "content_sha256": executable_sha,
            "version": executable_version,
        },
        "argv_identity": argv_identity,
        "environment_identity": environment_identity,
        "cwd_policy_ref": cwd_policy_ref,
        "cwd_role": cwd_role,
        "io_policy_ref": io_policy_ref,
        "io_identity": io_identity,
        "heartbeat_policy_ref": heartbeat_policy_ref,
        "heartbeat_interval_seconds": _decimal_identity(
            heartbeat_seconds, "heartbeat_policy.interval_seconds"
        ),
        "timeout_policy_ref": timeout_policy_ref,
        "timeout_identity": timeout_identity,
        "termination_policy_ref": termination_policy_ref,
        "termination_identity": termination_identity,
        "progress_policy_ref": progress_policy_ref,
        "progress_identity": progress_identity,
        "stall_policy_ref": stall_policy_ref,
        "stall_identity": stall_identity,
        "sensitivity_identity": sensitivity_identity,
        "failure_policy_ref": failure_policy_ref,
        "owner_defined_retry_rule": owner_defined_retry_rule,
        "diagnostic_policy_ref": diagnostic_policy_ref,
        "diagnostic_context_identity": diagnostic_context_identity,
        "worker_identity": worker_identity,
        "isolation_identity": isolation_identity,
    }
    subject_fingerprint = canonical_fingerprint(semantic_material)

    return {
        "invocation_id": invocation_id,
        "receipt_subject_fingerprint": receipt_subject,
        "event_fingerprint": event_fp,
        "plan_fingerprint": plan_fp,
        "node_id": node_id,
        "execution_basis_ref": execution_basis_ref,
        "capability_binding_ref": capability_ref,
        "execution_mode_ref": mode,
        "executable_path": resolved_path,
        "executable_sha256": executable_sha,
        "argv": list(argv),
        "argv_fingerprint": argv_identity["argv_fingerprint"],
        "env": env,
        "environment_identity": environment_identity,
        "cwd_policy_ref": cwd_policy_ref,
        "cwd_role": cwd_role,
        "cwd": cwd_locator,
        "io_policy_ref": io_policy_ref,
        "io_identity": io_identity,
        "heartbeat_policy_ref": heartbeat_policy_ref,
        "heartbeat_seconds": heartbeat_seconds,
        "timeout_policy_ref": timeout_policy_ref,
        "timeout_seconds": timeout_seconds,
        "timeout_action": timeout_action,
        "termination_policy_ref": termination_policy_ref,
        "termination_identity": termination_identity,
        "force_grace_seconds": force_grace_seconds,
        "progress_policy_ref": progress_policy_ref,
        "progress_identity": progress_identity,
        "progress_marker_path": progress_marker_path,
        "stall_policy_ref": stall_policy_ref,
        "stall_threshold_seconds": stall_threshold_seconds,
        "stall_action": stall_action,
        "sensitivity": sensitivity,
        "redaction": redaction,
        "secret_binding_refs": sorted(secret_binding_refs),
        "failure_policy_ref": failure_policy_ref,
        "owner_defined_retry_rule": owner_defined_retry_rule,
        "diagnostic_policy_ref": diagnostic_policy_ref,
        "diagnostic_context_identity": diagnostic_context_identity,
        "worker_identity": worker_identity,
        "isolation_identity": isolation_identity,
        "semantic_material": semantic_material,
        "supervision_subject_fingerprint": subject_fingerprint,
        "canonicalization_algorithm": "RFC8785_JCS",
    }


def _freshness_recheck(compiled: dict[str, Any]) -> None:
    executable = compiled["executable_path"]
    if not executable.is_absolute() or not executable.is_file():
        raise HostError("RUNTIME2_EXECUTABLE_UNAVAILABLE", str(executable))
    observed_hash = sha256_file(executable)
    if observed_hash != compiled["executable_sha256"]:
        raise HostError(
            "RUNTIME2_EXECUTABLE_IDENTITY_MISMATCH",
            f"expected={compiled['executable_sha256']};observed={observed_hash}",
        )
    if Path(compiled["argv"][0]) != executable:
        raise HostError("RUNTIME2_ARGV_EXECUTABLE_BINDING_MISMATCH", compiled["argv"][0])
    if canonical_fingerprint(compiled["argv"]) != compiled["argv_fingerprint"]:
        raise HostError("RUNTIME2_ARGV_BINDING_STALE", "argv-fingerprint-changed")
    if canonical_fingerprint(compiled["env"]) != compiled["environment_identity"]["values_fingerprint"]:
        raise HostError("RUNTIME2_ENVIRONMENT_BINDING_STALE", "environment-fingerprint-changed")
    cwd = compiled["cwd"]
    if not cwd.is_absolute() or not cwd.is_dir():
        raise HostError("RUNTIME2_CWD_BINDING_STALE", str(cwd))


def _progress_marker_signature(path: Path | None) -> tuple[str, str | None]:
    if path is None:
        return "NOT_APPLICABLE", None
    try:
        if not path.exists():
            return "COMPLETE", "ABSENT"
        if not path.is_file():
            return "ERROR", None
        return "COMPLETE", sha256_file(path)
    except OSError:
        return "ERROR", None


def _runtime2_invocation_dir(invocation_id: str, root: Path | None = None) -> Path:
    base = root if root is not None else runtime2_operation_root()
    base.mkdir(parents=True, exist_ok=True)
    return base / sha256_bytes(invocation_id.encode("utf-8"))[:32]


def _journal_base(compiled: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": RUNTIME2_JOURNAL_SCHEMA,
        "journal_revision": 1,
        "invocation_id": compiled["invocation_id"],
        "receipt_subject_fingerprint": compiled["receipt_subject_fingerprint"],
        "supervision_subject_fingerprint": compiled["supervision_subject_fingerprint"],
        "event_fingerprint": compiled["event_fingerprint"],
        "plan_fingerprint": compiled["plan_fingerprint"],
        "node_id": compiled["node_id"],
        "execution_basis_ref": compiled["execution_basis_ref"],
        "capability_binding_ref": compiled["capability_binding_ref"],
        "execution_mode_ref": compiled["execution_mode_ref"],
        "binding_identities": {
            "executable_sha256": compiled["executable_sha256"],
            "argv_fingerprint": compiled["argv_fingerprint"],
            "environment_identity": compiled["environment_identity"],
            "cwd_policy_ref": compiled["cwd_policy_ref"],
            "cwd_role": compiled["cwd_role"],
            "io_policy_ref": compiled["io_policy_ref"],
            "heartbeat_policy_ref": compiled["heartbeat_policy_ref"],
            "progress_policy_ref": compiled["progress_policy_ref"],
            "progress_identity": compiled["progress_identity"],
            "stall_policy_ref": compiled["stall_policy_ref"],
            "timeout_policy_ref": compiled["timeout_policy_ref"],
            "termination_policy_ref": compiled["termination_policy_ref"],
            "sensitivity": compiled["sensitivity"],
            "redaction": compiled["redaction"],
            "failure_policy_ref": compiled["failure_policy_ref"],
            "worker_identity": compiled["worker_identity"],
            "isolation_identity": compiled["isolation_identity"],
        },
        "state": "PREPARED",
        "prepared_at": utc_now(),
        "heartbeat_count": 0,
        "heartbeat_at": None,
        "timeout_observed": False,
        "progress_state": (
            "NOT_DECLARED"
            if compiled["progress_policy_ref"] is None
            else "DECLARED_NO_PROGRESS_OBSERVED"
        ),
        "last_progress_observation": None,
        "progress_probe_status": (
            "NOT_APPLICABLE"
            if compiled["progress_policy_ref"] is None
            else "PENDING"
        ),
        "stall_state": (
            "NOT_APPLICABLE_NO_PROGRESS_POLICY"
            if compiled["progress_policy_ref"] is None
            else "NOT_OBSERVED"
        ),
        "retry_or_replay": "PROHIBITED",
        "process_observation": {
            "start_status": "NOT_ATTEMPTED",
            "lifecycle_status": "NOT_STARTED",
            "pid": None,
            "exit_status": "UNAVAILABLE",
            "exit_code": None,
            "termination_reason": None,
            "semantic_result": "UNRESOLVED_BY_SUPERVISOR",
            "material_poststate": "UNRESOLVED_BY_CAPABILITY_OWNER",
        },
    }


def _persist_process_observation(
    path: Path,
    compiled: dict[str, Any],
    journal: dict[str, Any],
    supplemental_bindings: dict[str, Any],
) -> dict[str, Any]:
    process = dict(journal["process_observation"])
    if process["start_status"] == "START_FAILURE":
        status = "COMPLETE"
        result = "FAIL"
    elif process["lifecycle_status"] == "EXITED":
        status = "COMPLETE"
        # PASS answers only the operational question "was the declared process
        # lifecycle observed to completion?" It is never capability PASS.
        result = "PASS"
    else:
        status = "PARTIAL"
        result = "UNKNOWN"

    value = {
        "invocation_id": compiled["invocation_id"],
        "node_id": compiled["node_id"],
        "supervision_subject_fingerprint": compiled["supervision_subject_fingerprint"],
        "pid": process.get("pid"),
        "start_status": process.get("start_status"),
        "lifecycle_status": process.get("lifecycle_status"),
        "exit_status": process.get("exit_status"),
        "exit_code": process.get("exit_code"),
        "termination_reason": process.get("termination_reason"),
        "semantic_result": "UNRESOLVED_BY_SUPERVISOR",
        "material_poststate": process.get("material_poststate"),
        "heartbeat_observation_binding": supplemental_bindings.get("heartbeat_summary"),
        "progress_observation_binding": supplemental_bindings.get("progress_observation"),
        "stall_observation_binding": supplemental_bindings.get("stall_observation"),
        "timeout_observation_binding": supplemental_bindings.get("timeout_observation"),
        "termination_observation_binding": supplemental_bindings.get("termination_observation"),
        "progress_inference": "NONE",
        "control_effect": "NONE",
        "retry_or_replay": "NONE",
    }
    observation = _operational_evidence(
        compiled,
        evidence_kind="PROCESS_OBSERVATION",
        probe_ref="RT2SUP-PROCESS-LIFECYCLE-OBSERVATION",
        status=status,
        result=result,
        value=value,
        error=None,
    )
    _write_json_atomic(path, observation)
    return observation


def _persist_heartbeat_summary(
    path: Path,
    compiled: dict[str, Any],
    journal: dict[str, Any],
) -> dict[str, Any]:
    summary = {
        "schema": RUNTIME2_HEARTBEAT_SCHEMA,
        "authority": "LIVENESS_EVIDENCE_ONLY",
        "invocation_id": compiled["invocation_id"],
        "supervision_subject_fingerprint": compiled["supervision_subject_fingerprint"],
        "node_id": compiled["node_id"],
        "heartbeat_count": int(journal.get("heartbeat_count", 0)),
        "last_heartbeat_at": journal.get("heartbeat_at"),
        "last_progress_observation": journal.get("last_progress_observation"),
        "proves_progress": False,
        "proves_success": False,
        "proves_semantic_result": False,
    }
    _write_json_atomic(path, summary)
    return summary


def _persist_aux_observation(
    path: Path,
    compiled: dict[str, Any],
    *,
    probe_ref: str,
    value: dict[str, Any],
    result: str = "PASS",
) -> dict[str, Any]:
    observation = _operational_evidence(
        compiled,
        evidence_kind="STATE_OBSERVATION",
        probe_ref=probe_ref,
        status="COMPLETE",
        result=result,
        value=value,
        error=None,
    )
    _write_json_atomic(path, observation)
    return observation


def _attempt_runtime2_diagnostic_capture(
    compiled: dict[str, Any],
    journal: dict[str, Any],
    supervision_status: str,
    *,
    capture_func: Any | None = None,
    store_root: Path | None = None,
) -> dict[str, Any] | None:
    """Capture bounded diagnostics without changing primary supervision truth.

    Runtime2 never rehydrates diagnostics from ambient state. Capture is opt-in
    through the frozen diagnostic_policy_ref and remains EVIDENCE_ONLY.
    """
    if compiled["diagnostic_policy_ref"] is None:
        journal["diagnostic_capture_status"] = "NOT_REQUESTED"
        return None
    if supervision_status not in {
        "START_FAILURE",
        "SUPERVISOR_INTERRUPTED",
        "SUPERVISOR_FAILURE_POST_START",
    }:
        journal["diagnostic_capture_status"] = "NOT_APPLICABLE_PRIMARY_PROCESS_OBSERVED"
        return None

    capture = capture_func or capture_capsule
    root = (store_root or (LOCALAPPDATA / "Cerebro" / "diagnostics")).resolve()
    event = {
        "subject": {
            "ref": compiled["supervision_subject_fingerprint"],
            "invocation_id": compiled["invocation_id"],
            "node_id": compiled["node_id"],
            "receipt_subject_fingerprint": compiled["receipt_subject_fingerprint"],
            "execution_basis_ref": compiled["execution_basis_ref"],
            "capability_binding_ref": compiled["capability_binding_ref"],
            "diagnostic_policy_ref": compiled["diagnostic_policy_ref"],
            "diagnostic_context_binding": compiled["diagnostic_context_identity"],
        },
        "failure": {
            "stage": "RUNTIME2_TOOLING_HOST_SUPERVISION",
            "detection": supervision_status,
            "exception_type": journal.get("supervisor_error_class"),
            "message": supervision_status,
            "exit_code": journal["process_observation"].get("exit_code"),
            "probe_status": "COMPLETE",
            "subject_result": "UNKNOWN",
            "raw_error_bounded": supervision_status,
        },
        "execution": {
            "execution_mode_ref": compiled["execution_mode_ref"],
            "process_start_status": journal["process_observation"].get("start_status"),
            "process_lifecycle_status": journal["process_observation"].get("lifecycle_status"),
            "termination_reason": journal["process_observation"].get("termination_reason"),
        },
        "evidence_refs": [],
    }
    try:
        captured = capture(store_root=root, event=event, repo=None)
        full_path = Path(str(captured["full_path"]))
        if not full_path.is_file():
            raise RuntimeError("diagnostic-full-capsule-missing-after-capture")
        observed = sha256_file(full_path)
        expected = str(captured.get("capsule_fingerprint") or "").lower()
        if observed != expected:
            raise RuntimeError("diagnostic-capsule-fingerprint-mismatch")
        journal["diagnostic_capture_status"] = "COMPLETE"
        journal["diagnostic_capsule_id"] = str(captured.get("capsule_id") or "")
        return _file_evidence_binding(
            full_path,
            "diagnostic_capsule",
            "cerebro-diagnostic-capsule/v0.1",
        )
    except Exception as exc:
        # RT2SUP-031 / STD-OP-EVID: diagnostics are subordinate evidence.
        # Their failure cannot replace or rewrite the primary supervision fact.
        journal["diagnostic_capture_status"] = "ERROR"
        journal["diagnostic_capture_error_class"] = type(exc).__name__
        return None


def _final_result(
    compiled: dict[str, Any],
    operation_dir: Path,
    journal_path: Path,
    process_path: Path,
    heartbeat_path: Path,
    journal: dict[str, Any],
    supervision_status: str,
) -> dict[str, Any]:
    # Bind subordinate observations first so the PROCESS_OBSERVATION can refer
    # to exact immutable fingerprints rather than paths/names alone.
    _persist_heartbeat_summary(heartbeat_path, compiled, journal)
    evidence_bindings: dict[str, Any] = {
        "heartbeat_summary": _file_evidence_binding(
            heartbeat_path, "heartbeat_summary", RUNTIME2_HEARTBEAT_SCHEMA
        )
    }

    if journal.get("last_progress_observation") is not None:
        progress_path = operation_dir / "progress-observation.json"
        _persist_aux_observation(
            progress_path,
            compiled,
            probe_ref="RT2SUP-PROGRESS-OBSERVATION",
            value={
                "progress_policy_ref": compiled["progress_policy_ref"],
                "progress_identity": compiled["progress_identity"],
                "last_progress_observation": journal.get("last_progress_observation"),
                "heartbeat_is_progress": False,
            },
        )
        evidence_bindings["progress_observation"] = _file_evidence_binding(
            progress_path, "progress_observation", OPERATIONAL_EVIDENCE_SCHEMA
        )

    if journal.get("stall_state") in {"STALL_SUSPECTED", "STALL_OBSERVED"}:
        stall_path = operation_dir / "stall-observation.json"
        _persist_aux_observation(
            stall_path,
            compiled,
            probe_ref="RT2SUP-STALL-OBSERVATION",
            value={
                "stall_state": journal.get("stall_state"),
                "stall_policy_ref": compiled["stall_policy_ref"],
                "stall_threshold_seconds": compiled["stall_threshold_seconds"],
                "last_progress_observation": journal.get("last_progress_observation"),
                "action": compiled["stall_action"],
                "process_terminated_by_stall": False,
            },
        )
        evidence_bindings["stall_observation"] = _file_evidence_binding(
            stall_path, "stall_observation", OPERATIONAL_EVIDENCE_SCHEMA
        )

    if journal.get("timeout_observed"):
        timeout_path = operation_dir / "timeout-observation.json"
        _persist_aux_observation(
            timeout_path,
            compiled,
            probe_ref="RT2SUP-TIMEOUT-OBSERVATION",
            value={
                "timeout_policy_ref": compiled["timeout_policy_ref"],
                "timeout_seconds": compiled["timeout_seconds"],
                "timeout_action": compiled["timeout_action"],
                "timeout_action_effect": journal.get("timeout_action_effect"),
                "material_poststate": journal["process_observation"].get("material_poststate"),
            },
        )
        evidence_bindings["timeout_observation"] = _file_evidence_binding(
            timeout_path, "timeout_observation", OPERATIONAL_EVIDENCE_SCHEMA
        )

    if journal["process_observation"].get("termination_reason"):
        termination_path = operation_dir / "termination-observation.json"
        _persist_aux_observation(
            termination_path,
            compiled,
            probe_ref="RT2SUP-TERMINATION-OBSERVATION",
            value={
                "termination_policy_ref": compiled["termination_policy_ref"],
                "termination_reason": journal["process_observation"].get("termination_reason"),
                "material_poststate": journal["process_observation"].get("material_poststate"),
                "rollback_inferred": False,
            },
        )
        evidence_bindings["termination_observation"] = _file_evidence_binding(
            termination_path, "termination_observation", OPERATIONAL_EVIDENCE_SCHEMA
        )

    diagnostic_binding = _attempt_runtime2_diagnostic_capture(
        compiled,
        journal,
        supervision_status,
    )
    if diagnostic_binding is not None:
        evidence_bindings["diagnostic_capsule"] = diagnostic_binding

    _persist_process_observation(process_path, compiled, journal, evidence_bindings)
    evidence_bindings["process_observation"] = _file_evidence_binding(
        process_path, "process_observation", OPERATIONAL_EVIDENCE_SCHEMA
    )

    # Finalize canonical journal bytes before creating its external storage
    # binding. The journal never includes its own path/write-status/fingerprint.
    _write_json_atomic(journal_path, journal)
    evidence_bindings["operation_journal"] = _file_evidence_binding(
        journal_path, "operation_journal", RUNTIME2_JOURNAL_SCHEMA
    )

    return {
        "schema": RUNTIME2_RESULT_SCHEMA,
        "authority": "EVIDENCE_ONLY",
        "supervision_status": supervision_status,
        "semantic_result": "UNRESOLVED_BY_SUPERVISOR",
        "invocation_id": compiled["invocation_id"],
        "receipt_subject_fingerprint": compiled["receipt_subject_fingerprint"],
        "supervision_subject_fingerprint": compiled["supervision_subject_fingerprint"],
        "canonicalization_algorithm": compiled["canonicalization_algorithm"],
        "node_id": compiled["node_id"],
        "execution_mode_ref": compiled["execution_mode_ref"],
        "process_observation": dict(journal["process_observation"]),
        "process_observation_binding": evidence_bindings["process_observation"],
        "operation_journal_binding": evidence_bindings["operation_journal"],
        "supervision_evidence_bindings": evidence_bindings,
        "material_poststate_required": journal["process_observation"]["start_status"] == "STARTED",
        "capability_owner_semantic_verification_required": True,
        "diagnostic_capture_status": journal.get("diagnostic_capture_status", "NOT_REQUESTED"),
        "mcp_control_effect": "NONE",
        "work_selection_effect": "NONE",
        "retry_scheduled": False,
        "replay_performed": False,
        "operation_ref": str(operation_dir),
    }


def _signal_from_name(name: str) -> int:
    if name == "SIGTERM":
        return signal.SIGTERM
    if name == "SIGINT":
        return signal.SIGINT
    raise HostError("RUNTIME2_COOPERATIVE_STOP_BINDING_INVALID", name)


def supervise_runtime2_process(
    request: dict[str, Any],
    evidence_root: Path | None = None,
) -> dict[str, Any]:
    compiled = _compile_runtime2_request(request)
    operation_dir = _runtime2_invocation_dir(compiled["invocation_id"], evidence_root)
    if operation_dir.exists():
        raise HostError(
            "RUNTIME2_INVOCATION_ID_REUSE_BLOCKED",
            f"existing-operation-evidence:{operation_dir}",
        )
    operation_dir.mkdir(parents=False, exist_ok=False)

    journal_path = operation_dir / "operation-journal.json"
    process_path = operation_dir / "process-observation.json"
    heartbeat_path = operation_dir / "heartbeat-summary.json"
    journal = _journal_base(compiled)

    # RT2SUP-007: the PREPARED journal is durable before process start.
    _write_json_atomic(journal_path, journal)

    # RT2SUP-042: immediately-before-spawn freshness recheck.
    try:
        _freshness_recheck(compiled)
    except Exception:
        journal["journal_revision"] += 1
        journal["state"] = "PRESTART_FRESHNESS_BLOCK"
        journal["completed_at"] = utc_now()
        _write_json_atomic(journal_path, journal)
        raise

    start_monotonic = time.monotonic()
    timeout_triggered = False
    last_progress_monotonic = start_monotonic
    progress_probe_status, progress_signature = _progress_marker_signature(
        compiled["progress_marker_path"]
    )
    if compiled["progress_policy_ref"] is not None:
        journal["progress_probe_status"] = progress_probe_status
        journal["progress_marker_initial_signature"] = progress_signature
        if progress_probe_status == "ERROR":
            journal["journal_revision"] += 1
            journal["state"] = "PRESTART_PROGRESS_PROBE_BLOCK"
            journal["completed_at"] = utc_now()
            _write_json_atomic(journal_path, journal)
            raise HostError(
                "RUNTIME2_PROGRESS_PROBE_UNAVAILABLE",
                str(compiled["progress_marker_path"]),
            )
    try:
        process = subprocess.Popen(
            compiled["argv"],
            cwd=compiled["cwd"],
            env=compiled["env"],
            shell=False,
            stdin=None,
            stdout=None,
            stderr=None,
        )
    except OSError as exc:
        journal["journal_revision"] += 1
        journal["state"] = "START_FAILURE"
        journal["completed_at"] = utc_now()
        journal["process_observation"].update(
            {
                "start_status": "START_FAILURE",
                "lifecycle_status": "NOT_STARTED",
                "pid": None,
                "exit_status": "UNAVAILABLE",
                "exit_code": None,
                "termination_reason": "PROCESS_START_FAILURE",
                "semantic_result": "UNRESOLVED_BY_SUPERVISOR",
                "material_poststate": "NOT_ATTEMPTED",
            }
        )
        return _final_result(
            compiled,
            operation_dir,
            journal_path,
            process_path,
            heartbeat_path,
            journal,
            "START_FAILURE",
        )

    journal["journal_revision"] += 1
    journal["state"] = "RUNNING"
    journal["started_at"] = utc_now()
    journal["process_observation"].update(
        {
            "start_status": "STARTED",
            "lifecycle_status": "RUNNING",
            "pid": process.pid,
            "material_poststate": "UNKNOWN_UNTIL_CAPABILITY_OWNER_VERIFIES",
        }
    )
    journal["heartbeat_count"] = 1
    journal["heartbeat_at"] = utc_now()
    _write_json_atomic(journal_path, journal)

    try:
        while True:
            exit_code = process.poll()
            if exit_code is not None:
                break

            elapsed = time.monotonic() - start_monotonic
            if (
                compiled["timeout_seconds"] is not None
                and elapsed >= compiled["timeout_seconds"]
                and not timeout_triggered
            ):
                timeout_triggered = True
                journal["journal_revision"] += 1
                journal["timeout_observed"] = True
                journal["timeout_observed_at"] = utc_now()
                action = compiled["timeout_action"]
                if action == "OBSERVE_ONLY":
                    journal["timeout_action_effect"] = "OBSERVED_NO_TERMINATION"
                elif action == "REQUEST_COOPERATIVE_STOP":
                    signal_name = compiled["termination_identity"]["cooperative_signal"]
                    process.send_signal(_signal_from_name(signal_name))
                    journal["timeout_action_effect"] = "COOPERATIVE_STOP_SIGNAL_SENT"
                    journal["process_observation"]["termination_reason"] = "COOPERATIVE_STOP_REQUESTED"
                    journal["process_observation"]["material_poststate"] = (
                        "UNKNOWN_UNTIL_CAPABILITY_OWNER_VERIFIES"
                    )
                elif action == "FORCE_TERMINATE_IF_EXPLICITLY_SAFE":
                    process.terminate()
                    grace = max(
                        0.01,
                        float(compiled["force_grace_seconds"]),
                    )
                    try:
                        process.wait(timeout=grace)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    journal["timeout_action_effect"] = "FORCE_TERMINATION_APPLIED"
                    journal["process_observation"]["termination_reason"] = (
                        "FORCE_TERMINATE_EXPLICITLY_SAFE"
                    )
                    journal["process_observation"]["material_poststate"] = (
                        "UNKNOWN_UNTIL_CAPABILITY_OWNER_VERIFIES"
                    )
                _write_json_atomic(journal_path, journal)

            time.sleep(compiled["heartbeat_seconds"])
            now_monotonic = time.monotonic()
            journal["journal_revision"] += 1
            journal["heartbeat_count"] += 1
            journal["heartbeat_at"] = utc_now()
            journal["state"] = "RUNNING"

            if compiled["progress_policy_ref"] is None:
                journal["progress_state"] = "NOT_DECLARED"
                journal["progress_probe_status"] = "NOT_APPLICABLE"
                journal["stall_state"] = "NOT_APPLICABLE_NO_PROGRESS_POLICY"
            else:
                current_probe_status, current_signature = _progress_marker_signature(
                    compiled["progress_marker_path"]
                )
                journal["progress_probe_status"] = current_probe_status
                if current_probe_status == "COMPLETE":
                    if current_signature != progress_signature:
                        progress_signature = current_signature
                        last_progress_monotonic = now_monotonic
                        journal["progress_state"] = "PROGRESS_OBSERVED"
                        journal["last_progress_observation"] = {
                            "observed_at": utc_now(),
                            "progress_policy_ref": compiled["progress_policy_ref"],
                            "marker_fingerprint": current_signature,
                        }
                    threshold = compiled["stall_threshold_seconds"]
                    if (
                        threshold is not None
                        and now_monotonic - last_progress_monotonic >= threshold
                        and journal["stall_state"] != "STALL_OBSERVED"
                    ):
                        journal["stall_state"] = "STALL_OBSERVED"
                        journal["stall_observed_at"] = utc_now()
                        journal["stall_action_effect"] = "OBSERVED_NO_TERMINATION"
                else:
                    journal["progress_state"] = "PROGRESS_PROBE_ERROR"
                    journal["stall_state"] = "UNKNOWN_PROGRESS_PROBE_ERROR"

            _write_json_atomic(journal_path, journal)

        exit_code = process.wait()
        journal["journal_revision"] += 1
        journal["state"] = "PROCESS_OBSERVED"
        journal["completed_at"] = utc_now()
        neutral_exit = normalize_exit_observation(exit_code)
        journal["process_observation"].update(
            {
                "lifecycle_status": "EXITED",
                "exit_status": neutral_exit["exit_status"],
                "exit_code": neutral_exit["exit_code"],
                "semantic_result": neutral_exit["semantic_result"],
            }
        )
        return _final_result(
            compiled,
            operation_dir,
            journal_path,
            process_path,
            heartbeat_path,
            journal,
            "PROCESS_EXIT_OBSERVED",
        )

    except KeyboardInterrupt:
        journal["journal_revision"] += 1
        journal["state"] = "SUPERVISOR_INTERRUPTED"
        journal["completed_at"] = utc_now()
        journal["process_observation"]["lifecycle_status"] = "SUPERVISOR_INTERRUPTED_CHILD_STATE_UNKNOWN"
        journal["process_observation"]["termination_reason"] = "SUPERVISOR_INTERRUPT"
        journal["process_observation"]["material_poststate"] = "UNKNOWN_UNTIL_CAPABILITY_OWNER_VERIFIES"
        return _final_result(
            compiled,
            operation_dir,
            journal_path,
            process_path,
            heartbeat_path,
            journal,
            "SUPERVISOR_INTERRUPTED",
        )
    except Exception as exc:
        journal["journal_revision"] += 1
        journal["state"] = "SUPERVISOR_FAILURE_POST_START"
        journal["completed_at"] = utc_now()
        journal["supervisor_error_class"] = type(exc).__name__
        journal["process_observation"]["lifecycle_status"] = "SUPERVISOR_FAILURE_CHILD_STATE_UNKNOWN"
        journal["process_observation"]["material_poststate"] = "UNKNOWN_UNTIL_CAPABILITY_OWNER_VERIFIES"
        _write_json_atomic(journal_path, journal)
        raise


def _test_request(
    executable: Path,
    cwd: Path,
    argv: list[str],
    *,
    invocation_id: str,
    env: dict[str, str] | None = None,
    heartbeat: float = 0.01,
    timeout_seconds: float | None = None,
    timeout_action: str = "OBSERVE_ONLY",
    force_safe: bool = False,
    mode: str = "SUPERVISED_PROCESS",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Selftests bind the parent environment explicitly so Windows child process
    # startup remains valid. Production requests still receive only their frozen
    # environment_binding through _environment_values().
    test_env = dict(env) if env is not None else dict(os.environ)
    request: dict[str, Any] = {
        "schema": RUNTIME2_REQUEST_SCHEMA,
        "invocation_id": invocation_id,
        "receipt_subject_fingerprint": "1" * 64,
        "event_fingerprint": "2" * 64,
        "plan_fingerprint": "3" * 64,
        "node_id": "NODE-1",
        "execution_basis_ref": {"ref": "BASIS-1", "fingerprint": "4" * 64},
        "capability_binding_ref": "CAP-1",
        "execution_mode_ref": mode,
        "executable_binding": {
            "logical_role": "test-python",
            "resolved_path": str(executable),
            "content_sha256": sha256_file(executable),
            "version": sys.version.split()[0],
        },
        "argv_binding": {"argv": argv},
        "environment_binding": {
            "values": test_env,
            "declared_keys": sorted(test_env),
            "secret_keys": [],
        },
        "cwd_policy_ref": "CWD-TEST",
        "cwd_binding": {
            "role": "test-work-root",
            "resolved_cwd_locator": str(cwd),
        },
        "io_policy_ref": "IO-INHERIT",
        "io_policy": {"stdin": "INHERIT", "stdout": "INHERIT", "stderr": "INHERIT"},
        "heartbeat_policy_ref": "HB-TEST",
        "heartbeat_policy": {"interval_seconds": heartbeat},
        "timeout_policy_ref": "TIMEOUT-TEST",
        "timeout_policy": {
            "timeout_seconds": timeout_seconds,
            "action": timeout_action,
        },
        "termination_policy_ref": "TERM-TEST",
        "termination_policy": {
            "force_terminate_explicitly_safe": force_safe,
            "force_grace_seconds": 0.05,
            "cooperative_signal": None,
        },
        "progress_policy_ref": None,
        "progress_policy": None,
        "stall_policy_ref": "STALL-TEST",
        "stall_policy": {
            "stall_threshold_seconds": None,
            "action": "OBSERVE_DO_NOT_FORCE_KILL",
        },
        "sensitivity_rules": {
            "sensitivity": "INTERNAL",
            "redaction": "OMIT",
            "secret_binding_refs": [],
        },
        "failure_policy_ref": "FAILURE-POLICY-TEST",
        "owner_defined_retry_rule": None,
        "diagnostic_policy_ref": None,
        "diagnostic_context_binding": None,
    }
    if extra:
        request.update(extra)
    return request


def runtime2_selftest() -> dict[str, Any]:
    import tempfile

    tests: list[dict[str, Any]] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        tests.append(
            {
                "name": name,
                "result": "PASS" if condition else "FAIL",
                "detail": detail,
            }
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        work = root / "work"
        evidence = root / "evidence"
        work.mkdir()
        evidence.mkdir()
        python = Path(sys.executable).resolve()

        # Deterministic subject identity and CWD relocation policy semantics.
        base = _test_request(
            python,
            work,
            [str(python), "-c", "pass"],
            invocation_id="SELFTEST-DETERMINISM",
        )
        compiled1 = _compile_runtime2_request(base)
        compiled2 = _compile_runtime2_request(dict(base))
        check(
            "canonical-subject-deterministic",
            compiled1["supervision_subject_fingerprint"]
            == compiled2["supervision_subject_fingerprint"],
        )

        relocated = root / "relocated"
        relocated.mkdir()
        relocated_req = json.loads(json.dumps(base))
        relocated_req["cwd_binding"]["resolved_cwd_locator"] = str(relocated)
        compiled_relocated = _compile_runtime2_request(relocated_req)
        check(
            "cwd-relocation-preserves-logical-subject-identity",
            compiled1["supervision_subject_fingerprint"]
            == compiled_relocated["supervision_subject_fingerprint"],
        )

        # Strong ordering canary: PREPARED journal must exist at the exact
        # moment subprocess.Popen is invoked, not merely in final poststate.
        prestart_req = _test_request(
            python,
            work,
            [str(python), "-c", "pass"],
            invocation_id="SELFTEST-PRESTART-ORDER",
        )
        prestart_dir = _runtime2_invocation_dir(
            prestart_req["invocation_id"], evidence
        )
        original_popen = subprocess.Popen
        prestart_observed = {"prepared": False}

        def checking_popen(*args: Any, **kwargs: Any):
            journal_at_spawn = prestart_dir / "operation-journal.json"
            if journal_at_spawn.is_file():
                payload = json.loads(journal_at_spawn.read_text(encoding="utf-8"))
                prestart_observed["prepared"] = (
                    payload.get("state") == "PREPARED"
                    and payload.get("process_observation", {}).get("start_status")
                    == "NOT_ATTEMPTED"
                )
            return original_popen(*args, **kwargs)

        subprocess.Popen = checking_popen  # type: ignore[assignment]
        try:
            supervise_runtime2_process(prestart_req, evidence)
        finally:
            subprocess.Popen = original_popen  # type: ignore[assignment]
        check("pre-start-journal-before-popen-exact-order", prestart_observed["prepared"])

        # Pre-start journal, silent heartbeat, neutral zero exit.
        silent_req = _test_request(
            python,
            work,
            [str(python), "-c", "import time; time.sleep(0.04)"],
            invocation_id="SELFTEST-SILENT",
            heartbeat=0.01,
        )
        silent = supervise_runtime2_process(silent_req, evidence)
        silent_journal = json.loads(
            Path(silent["supervision_evidence_bindings"]["operation_journal"]["ref"]).read_text(encoding="utf-8")
        )
        check("journal-remains-durable-through-completion", silent_journal["journal_revision"] >= 2)
        check("heartbeat-with-silent-child", silent_journal["heartbeat_count"] >= 2)
        check(
            "heartbeat-is-not-progress",
            silent["supervision_evidence_bindings"]["heartbeat_summary"]["sha256"]
            and silent["semantic_result"] == "UNRESOLVED_BY_SUPERVISOR",
        )
        check(
            "zero-exit-not-semantic-pass",
            silent["process_observation"]["exit_code"] == 0
            and silent["semantic_result"] == "UNRESOLVED_BY_SUPERVISOR",
        )

        # Process observation uses the Source-owned operational evidence envelope.
        process_envelope = json.loads(
            Path(
                silent["supervision_evidence_bindings"]["process_observation"]["ref"]
            ).read_text(encoding="utf-8")
        )
        required_evidence_fields = {
            "evidence_id", "evidence_kind", "subject_ref", "probe_ref", "status",
            "result", "value", "source", "observed_at", "freshness", "confidence",
            "sensitivity", "redaction", "error", "basis_refs", "producer_ref",
        }
        check(
            "process-observation-operational-evidence-envelope",
            process_envelope.get("schema") == OPERATIONAL_EVIDENCE_SCHEMA
            and process_envelope.get("evidence_kind") == "PROCESS_OBSERVATION"
            and required_evidence_fields.issubset(process_envelope),
        )

        # Declared progress is a separate explicit marker signal; heartbeat never
        # resets the stall clock. A marker change creates progress evidence.
        progress_marker = work / "progress.marker"
        progress_req = _test_request(
            python,
            work,
            [
                str(python),
                "-c",
                "import pathlib,time; time.sleep(0.05); pathlib.Path('progress.marker').write_text('1'); time.sleep(0.10); pathlib.Path('progress.marker').write_text('2'); time.sleep(0.20)",
            ],
            invocation_id="SELFTEST-PROGRESS",
            heartbeat=0.005,
            extra={
                "progress_policy_ref": "PROGRESS-FILE-1",
                "progress_policy": {
                    "observation_kind": "FILE_FINGERPRINT_CHANGE",
                    "marker_binding": {
                        "role": "capability-progress-marker",
                        "resolved_path": str(progress_marker),
                    },
                },
                "stall_policy_ref": "STALL-PROGRESS-1",
                "stall_policy": {
                    "stall_threshold_seconds": 0.50,
                    "action": "OBSERVE_DO_NOT_FORCE_KILL",
                },
            },
        )
        progress_result = supervise_runtime2_process(progress_req, evidence)
        progress_journal = json.loads(
            Path(
                progress_result["supervision_evidence_bindings"]["operation_journal"]["ref"]
            ).read_text(encoding="utf-8")
        )
        check(
            "declared-progress-marker-observed",
            progress_journal["last_progress_observation"] is not None
            and "progress_observation"
            in progress_result["supervision_evidence_bindings"],
        )

        # A declared progress threshold may emit stall evidence, but the default
        # stall action remains observation-only and never kills the child.
        stall_marker = work / "stall.marker"
        stall_req = _test_request(
            python,
            work,
            [str(python), "-c", "import time; time.sleep(0.06)"],
            invocation_id="SELFTEST-STALL",
            heartbeat=0.005,
            extra={
                "progress_policy_ref": "PROGRESS-FILE-STALL",
                "progress_policy": {
                    "observation_kind": "FILE_FINGERPRINT_CHANGE",
                    "marker_binding": {
                        "role": "capability-progress-marker",
                        "resolved_path": str(stall_marker),
                    },
                },
                "stall_policy_ref": "STALL-DECLARED",
                "stall_policy": {
                    "stall_threshold_seconds": 0.02,
                    "action": "OBSERVE_DO_NOT_FORCE_KILL",
                },
            },
        )
        stall_result = supervise_runtime2_process(stall_req, evidence)
        stall_journal = json.loads(
            Path(
                stall_result["supervision_evidence_bindings"]["operation_journal"]["ref"]
            ).read_text(encoding="utf-8")
        )
        check(
            "declared-stall-observed-non-destructive",
            stall_journal["stall_state"] == "STALL_OBSERVED"
            and stall_result["process_observation"]["termination_reason"] is None
            and "stall_observation" in stall_result["supervision_evidence_bindings"],
        )

        # Ambient environment never leaks into typed child environment.
        ambient_key = "CEREBRO_RUNTIME2_AMBIENT_SHOULD_NOT_LEAK"
        old = os.environ.get(ambient_key)
        os.environ[ambient_key] = "AMBIENT_SECRET"
        env_out = work / "env.txt"
        env_req = _test_request(
            python,
            work,
            [
                str(python),
                "-c",
                "import os,pathlib; pathlib.Path('env.txt').write_text(os.environ.get('CEREBRO_RUNTIME2_AMBIENT_SHOULD_NOT_LEAK','ABSENT'))",
            ],
            invocation_id="SELFTEST-ENV",
            env={key: value for key, value in os.environ.items() if key != ambient_key},
        )
        supervise_runtime2_process(env_req, evidence)
        if old is None:
            os.environ.pop(ambient_key, None)
        else:
            os.environ[ambient_key] = old
        check("undeclared-ambient-env-absent", env_out.read_text(encoding="utf-8") == "ABSENT")

        # Executable identity mismatch blocks before start.
        mismatch = _test_request(
            python,
            work,
            [str(python), "-c", "pass"],
            invocation_id="SELFTEST-HASH-MISMATCH",
        )
        mismatch["executable_binding"]["content_sha256"] = "0" * 64
        try:
            supervise_runtime2_process(mismatch, evidence)
            mismatch_blocked = False
        except HostError as exc:
            mismatch_blocked = exc.classification == "RUNTIME2_EXECUTABLE_IDENTITY_MISMATCH"
        check("executable-hash-mismatch-blocks", mismatch_blocked)

        # Command shadowing cannot replace exact absolute argv[0].
        shadow = _test_request(
            python,
            work,
            ["python", "-c", "pass"],
            invocation_id="SELFTEST-SHADOW",
        )
        try:
            _compile_runtime2_request(shadow)
            shadow_blocked = False
        except HostError as exc:
            shadow_blocked = exc.classification == "RUNTIME2_ARGV_EXECUTABLE_BINDING_MISMATCH"
        check("command-shadowing-cannot-win", shadow_blocked)

        # Environment changes change the canonical subject/basis identity.
        env_a = _test_request(
            python, work, [str(python), "-c", "pass"], invocation_id="SELFTEST-ENV-A", env={"X": "1"}
        )
        env_b = json.loads(json.dumps(env_a))
        env_b["invocation_id"] = "SELFTEST-ENV-B"
        env_b["environment_binding"]["values"]["X"] = "2"
        check(
            "declared-environment-change-requires-new-basis",
            _compile_runtime2_request(env_a)["supervision_subject_fingerprint"]
            != _compile_runtime2_request(env_b)["supervision_subject_fingerprint"],
        )

        # Null/unavailable exit is UNKNOWN, not FAIL.
        check(
            "unavailable-exit-is-unknown-not-fail",
            normalize_exit_observation(None)
            == {
                "exit_status": "UNAVAILABLE",
                "exit_code": None,
                "semantic_result": "UNRESOLVED_BY_SUPERVISOR",
            },
        )

        # Start failure is distinct and material poststate is NOT_ATTEMPTED.
        bad_exec = root / "not-executable.txt"
        bad_exec.write_text("not an executable", encoding="utf-8")
        bad_req = _test_request(
            bad_exec,
            work,
            [str(bad_exec)],
            invocation_id="SELFTEST-START-FAILURE",
        )
        bad = supervise_runtime2_process(bad_req, evidence)
        check(
            "start-failure-distinct",
            bad["supervision_status"] == "START_FAILURE"
            and bad["process_observation"]["material_poststate"] == "NOT_ATTEMPTED",
        )

        # Default silence/stall behavior is non-destructive.
        check(
            "stall-default-non-destructive",
            silent_journal["stall_state"] == "NOT_APPLICABLE_NO_PROGRESS_POLICY"
            and silent_journal["process_observation"]["termination_reason"] is None,
        )

        # Undeclared force timeout is blocked before start.
        undeclared = _test_request(
            python,
            work,
            [str(python), "-c", "pass"],
            invocation_id="SELFTEST-UNDECLARED-KILL",
            timeout_action="FORCE_TERMINATE_IF_EXPLICITLY_SAFE",
            force_safe=True,
            timeout_seconds=None,
        )
        try:
            _compile_runtime2_request(undeclared)
            undeclared_blocked = False
        except HostError as exc:
            undeclared_blocked = exc.classification == "RUNTIME2_UNDECLARED_TIMEOUT_TERMINATION_BLOCKED"
        check("undeclared-timeout-kill-blocks", undeclared_blocked)

        # Explicit safe forced termination remains semantic/poststate UNKNOWN.
        forced = _test_request(
            python,
            work,
            [str(python), "-c", "import time; time.sleep(3)"],
            invocation_id="SELFTEST-FORCE",
            heartbeat=0.01,
            timeout_seconds=0.04,
            timeout_action="FORCE_TERMINATE_IF_EXPLICITLY_SAFE",
            force_safe=True,
        )
        forced_result = supervise_runtime2_process(forced, evidence)
        check(
            "forced-kill-poststate-unknown",
            forced_result["process_observation"]["termination_reason"]
            == "FORCE_TERMINATE_EXPLICITLY_SAFE"
            and forced_result["process_observation"]["material_poststate"]
            == "UNKNOWN_UNTIL_CAPABILITY_OWNER_VERIFIES",
        )

        # Operation replay is blocked by existing invocation evidence.
        try:
            supervise_runtime2_process(silent_req, evidence)
            replay_blocked = False
        except HostError as exc:
            replay_blocked = exc.classification == "RUNTIME2_INVOCATION_ID_REUSE_BLOCKED"
        check("abnormal-or-complete-evidence-blocks-replay", replay_blocked)

        # Evidence bindings are content-fingerprint bound.
        bindings = silent["supervision_evidence_bindings"]
        bindings_ok = True
        for binding in bindings.values():
            p = Path(binding["ref"])
            bindings_ok = bindings_ok and p.is_file() and sha256_file(p) == binding["sha256"]
        check("evidence-refs-fingerprint-bound", bindings_ok)

        # No semantic/control authority is returned by the supervisor.
        check(
            "supervisor-cannot-change-mcp-or-capability-owner",
            silent["mcp_control_effect"] == "NONE"
            and silent["work_selection_effect"] == "NONE"
            and silent["capability_owner_semantic_verification_required"] is True,
        )

        # Isolated workers are fresh process/state boundaries with exact frozen
        # request/resource bindings; mutable Source/ambient config cannot be
        # introduced as an allowed hidden input.
        isolated_base_extra = {
            "worker_runtime_binding": {"ref": "PY-WORKER-RUNTIME", "fingerprint": "5" * 64},
            "worker_request_binding": {
                "ref": "WORKER-REQUEST-1",
                "fingerprint": "6" * 64,
                "schema": "worker-request/v1",
                "immutable": True,
            },
            "worker_result_contract_ref": "worker-result/v1",
            "allowed_resource_bindings": [],
        }
        isolated = _test_request(
            python,
            work,
            [str(python), "-c", "pass"],
            invocation_id="SELFTEST-ISOLATED",
            mode="ISOLATED_CAPABILITY_WORKER",
            extra=isolated_base_extra,
        )
        isolated_result = supervise_runtime2_process(isolated, evidence)
        check(
            "isolated-worker-one-fresh-process-boundary",
            isolated_result["execution_mode_ref"] == "ISOLATED_CAPABILITY_WORKER"
            and isolated_result["retry_scheduled"] is False
            and isolated_result["replay_performed"] is False,
        )

        hidden = json.loads(json.dumps(isolated))
        hidden["invocation_id"] = "SELFTEST-ISOLATED-HIDDEN"
        hidden["allowed_resource_bindings"] = [
            {
                "ref": "LOCAL-WORKING-SOURCE",
                "fingerprint": "7" * 64,
                "kind": "MUTABLE_SOURCE",
                "access": "READ_ONLY",
            }
        ]
        try:
            _compile_runtime2_request(hidden)
            hidden_blocked = False
        except HostError as exc:
            hidden_blocked = exc.classification == "RUNTIME2_WORKER_HIDDEN_INPUT_BLOCKED"
        check("worker-mutable-source-hidden-input-blocked", hidden_blocked)

        # Operator interrupt preserves already-created effects and does not
        # infer rollback. The same invocation cannot be replayed afterward.
        interrupt_effect = work / "interrupt-effect.txt"
        interrupt_req = _test_request(
            python,
            work,
            [
                str(python),
                "-c",
                (
                    "import pathlib,time;"
                    f"pathlib.Path({str(interrupt_effect)!r}).write_text('EFFECT');"
                    "time.sleep(0.2)"
                ),
            ],
            invocation_id="SELFTEST-INTERRUPT",
            heartbeat=0.01,
        )
        original_sleep = time.sleep
        sleep_calls = {"count": 0}

        def interrupting_sleep(seconds: float) -> None:
            sleep_calls["count"] += 1
            # Only inject loss after the child effect is observably established.
            # Until then behave exactly like the requested heartbeat wait.
            if interrupt_effect.is_file():
                raise KeyboardInterrupt()
            original_sleep(seconds)

        time.sleep = interrupting_sleep  # type: ignore[assignment]
        try:
            interrupted = supervise_runtime2_process(interrupt_req, evidence)
        finally:
            time.sleep = original_sleep  # type: ignore[assignment]
        # Give the child enough time to finish naturally; supervisor interruption
        # must not fabricate rollback or silently replay/terminate it.
        original_sleep(0.25)
        interrupted_journal = json.loads(
            Path(
                interrupted["supervision_evidence_bindings"]["operation_journal"]["ref"]
            ).read_text(encoding="utf-8")
        )
        check(
            "operator-interrupt-preserves-prior-effect",
            interrupted["supervision_status"] == "SUPERVISOR_INTERRUPTED"
            and interrupt_effect.read_text(encoding="utf-8") == "EFFECT"
            and interrupted["process_observation"]["material_poststate"]
            == "UNKNOWN_UNTIL_CAPABILITY_OWNER_VERIFIES"
            and interrupted["replay_performed"] is False,
        )
        check(
            "abnormal-loss-journal-preserved",
            interrupted_journal["state"] == "SUPERVISOR_INTERRUPTED"
            and Path(
                interrupted["supervision_evidence_bindings"]["operation_journal"]["ref"]
            ).is_file(),
        )
        try:
            supervise_runtime2_process(interrupt_req, evidence)
            interrupted_replay_blocked = False
        except HostError as exc:
            interrupted_replay_blocked = (
                exc.classification == "RUNTIME2_INVOCATION_ID_REUSE_BLOCKED"
            )
        check("abnormal-loss-does-not-replay", interrupted_replay_blocked)

        # Diagnostic capture is subordinate EVIDENCE_ONLY. Whether capture
        # completes or fails, it must never replace the primary supervision
        # failure classification.
        diagnostic_exec = root / "diagnostic-start-failure.txt"
        diagnostic_exec.write_text("not executable", encoding="utf-8")
        diagnostic_req = _test_request(
            diagnostic_exec,
            work,
            [str(diagnostic_exec)],
            invocation_id="SELFTEST-DIAGNOSTIC-FAILURE",
            extra={"diagnostic_policy_ref": "DIAG-SELFTEST"},
        )
        diagnostic_result = supervise_runtime2_process(diagnostic_req, evidence)
        check(
            "diagnostic-capture-preserves-primary-failure",
            diagnostic_result["supervision_status"] == "START_FAILURE"
            and diagnostic_result["diagnostic_capture_status"] in {"COMPLETE", "ERROR"}
            and diagnostic_result["process_observation"]["termination_reason"]
            == "PROCESS_START_FAILURE"
            and diagnostic_result["process_observation"]["material_poststate"]
            == "NOT_ATTEMPTED",
        )

        # Secret plaintext is not written to evidence.
        secret_value = "TOP-SECRET-SELFTEST-VALUE"
        secret_req = _test_request(
            python,
            work,
            [str(python), "-c", "pass"],
            invocation_id="SELFTEST-SECRET",
            env={**os.environ, "SECRET_X": secret_value},
        )
        secret_req["environment_binding"]["secret_keys"] = ["SECRET_X"]
        secret = supervise_runtime2_process(secret_req, evidence)
        secret_leak = any(
            secret_value in Path(binding["ref"]).read_text(encoding="utf-8")
            for binding in secret["supervision_evidence_bindings"].values()
        )
        check("secret-plaintext-not-persisted", not secret_leak)

        # Process result is evidence, not capability success.
        nonzero_req = _test_request(
            python,
            work,
            [str(python), "-c", "raise SystemExit(7)"],
            invocation_id="SELFTEST-NONZERO",
        )
        nonzero = supervise_runtime2_process(nonzero_req, evidence)
        check(
            "nonzero-exit-remains-neutral-process-evidence",
            nonzero["process_observation"]["exit_code"] == 7
            and nonzero["semantic_result"] == "UNRESOLVED_BY_SUPERVISOR",
        )

        # One invocation directory per worker/node means no resident pool or reuse.
        check(
            "one-worker-one-node-no-pool",
            "worker_pool" not in compiled1["semantic_material"]
            and silent["retry_scheduled"] is False
            and silent["replay_performed"] is False,
        )

        # Diagnostic/evidence storage outcome is external to canonical artifact bytes:
        # canonical artifacts contain no self write-status or own storage path fields.
        journal_text = Path(bindings["operation_journal"]["ref"]).read_text(encoding="utf-8")
        check(
            "no-self-persistence-circularity",
            '"journal_path"' not in journal_text
            and '"storage_status"' not in journal_text
            and '"artifact_sha256"' not in journal_text,
        )

        # Child output policy remains inherited/live, while heartbeat is independent.
        check(
            "io-live-and-heartbeat-independent",
            compiled1["io_identity"] == {"stdin": "INHERIT", "stdout": "INHERIT", "stderr": "INHERIT"}
            and compiled1["heartbeat_seconds"] > 0,
        )

    result = "PASS" if all(item["result"] == "PASS" for item in tests) else "FAIL"
    return {
        "schema": "cerebro-runtime2-host-supervision-selftest/v1",
        "result": result,
        "test_count": len(tests),
        "tests": tests,
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
        raise HostError(
            "FIRST_LIGHT_PRECOMMIT_ENVELOPE_EXTERNAL_OVERRIDE_PROHIBITED",
            "--invocation-envelope",
        )
    event_value = _argument_value(arguments, "--event-file")
    db_value = _argument_value(arguments, "--db")
    mode = _argument_value(arguments, "--mode") or "REAL_FIRST_LIGHT"
    if not event_value:
        raise HostError(
            "FIRST_LIGHT_PRECOMMIT_EVENT_FILE_REQUIRED",
            "host dispatch requires --event-file",
        )
    if not db_value:
        raise HostError(
            "FIRST_LIGHT_PRECOMMIT_DB_REQUIRED",
            "host dispatch requires --db",
        )

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
        runtime = _load_snapshot_module(
            runtime_path,
            f"cerebro_first_light_precommit_{operation_id}",
        )
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
        "CEREBRO_FIRST_LIGHT_PRECOMMIT_FINGERPRINT": str(
            envelope["precommit_fingerprint"]
        ),
    }


def delegate(
    snapshot: Path,
    component: str,
    arguments: list[str],
    source_commit: str,
) -> int:
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

    # Legacy compatibility only. Runtime2 uses an exact environment_binding and
    # never calls this ambient-environment path.
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
        print(
            json.dumps(
                {
                    "result": "UNKNOWN",
                    "classification": "PROCESS_EXIT_STATUS_UNAVAILABLE",
                    "process_observation": observation,
                    "material_poststate_required": True,
                },
                indent=2,
            )
        )
        return 2
    return int(observation["exit_code"])


def selftest() -> dict:
    # Legacy host compatibility plus the Runtime2 typed-supervision canaries.
    material = f"{HOST_VERSION}|{SOURCE_REPOSITORY}|{SNAPSHOT_ROOT}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    runtime2 = runtime2_selftest()
    return {
        "schema": "cerebro-host-selftest/v0.2",
        "result": "PASS" if runtime2["result"] == "PASS" else "FAIL",
        "host_version": HOST_VERSION,
        "dispatch_contract": "snapshot_rehydrate_diagnostics_precommit_first_light_then_delegate",
        "runtime2_supervision_contract": "typed_basis_bound_neutral_process_evidence",
        "runtime2_test_count": runtime2["test_count"],
        "runtime2": runtime2,
        "source_mutation": False,
        "fingerprint": digest,
    }


def _write_cli_result(path: str | None, result: dict[str, Any]) -> None:
    text = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, target)
    else:
        print(text, end="")


DELEGATE_COMMANDS = (
    "change",
    "delivery",
    "closure",
    "diagnostics",
    "runtime-first-light",
)
HOST_COMMANDS = ("selftest", "runtime2-supervise", *DELEGATE_COMMANDS)


def parse_host_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cerebro Host")
    parser.add_argument("--source-root")
    parser.add_argument("--source-commit")
    parser.add_argument("command", choices=HOST_COMMANDS)
    parser.add_argument("command_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    args.request = None
    args.output = None
    args.delegate_args = []
    if args.command == "selftest":
        if args.command_args:
            parser.error("selftest does not accept delegated arguments")
        return args
    if args.command == "runtime2-supervise":
        runtime2 = argparse.ArgumentParser(prog=f"{parser.prog} runtime2-supervise")
        runtime2.add_argument("--request", required=True)
        runtime2.add_argument("--output")
        parsed = runtime2.parse_args(args.command_args)
        args.request = parsed.request
        args.output = parsed.output
        return args
    args.delegate_args = list(args.command_args)
    return args


def main() -> int:
    args = parse_host_arguments()

    try:
        if args.command == "selftest":
            result = selftest()
            print(json.dumps(result, indent=2))
            return 0 if result["result"] == "PASS" else 1

        if args.command == "runtime2-supervise":
            request_path = Path(args.request)
            request = json.loads(request_path.read_text(encoding="utf-8"))
            if not isinstance(request, dict):
                raise HostError("RUNTIME2_REQUEST_INVALID", "top-level-object-required")
            result = supervise_runtime2_process(request)
            _write_cli_result(args.output, result)
            # CLI exit reports host invocation health, not child semantic truth.
            return 0 if result["supervision_status"] != "START_FAILURE" else 2

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
