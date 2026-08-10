#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CAPSULE_SCHEMA = "cerebro-diagnostic-capsule/v0.1"
REGISTRY_SCHEMA = "cerebro-diagnostic-registry/v0.1"
ACTIVE_SCHEMA = "cerebro-active-diagnostic/v0.1"
TRANSPORT_SCHEMA = "cerebro-diagnostic-transport/v0.1"
TOOL_VERSION = "0.1.0"

SECRET_KEY_PATTERN = re.compile(
    r"(password|passwd|secret|token|authorization|credential|private[_-]?key|api[_-]?key)",
    re.IGNORECASE,
)
SECRET_TEXT_PATTERNS = [
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}\b"),
]
MAX_TEXT = 65536
MAX_TRANSPORT_TEXT = 12000


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_store_root() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "Cerebro" / "diagnostics"
    return Path.home() / ".cerebro" / "diagnostics"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bounded_text(value: Any, limit: int = MAX_TEXT) -> str:
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[TRUNCATED {len(text) - limit} chars]"


def redact_text(value: Any, limit: int = MAX_TEXT) -> str:
    text = bounded_text(value, limit)
    for pattern in SECRET_TEXT_PATTERNS:
        text = pattern.sub("[REDACTED_SECRET]", text)
    return text


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if SECRET_KEY_PATTERN.search(key_text):
                out[key_text] = "[REDACTED_SECRET]"
            else:
                out[key_text] = redact(item)
        return out
    if isinstance(value, list):
        return [redact(item) for item in value[:256]]
    if isinstance(value, tuple):
        return [redact(item) for item in value[:256]]
    if isinstance(value, str):
        return redact_text(value)
    return value


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"JSON object required: {path}")
    return data


def run_probe(cmd: list[str], cwd: Path | None = None) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            cmd,
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
        )
        return {
            "status": "COMPLETE",
            "exit_code": completed.returncode,
            "stdout": redact_text(completed.stdout),
            "stderr": redact_text(completed.stderr),
        }
    except OSError as exc:
        return {
            "status": "ERROR",
            "exit_code": None,
            "stdout": "",
            "stderr": redact_text(repr(exc)),
        }


def parse_porcelain_z(raw: str) -> list[str]:
    items = raw.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(items):
        entry = items[index]
        if not entry:
            index += 1
            continue
        if len(entry) < 4:
            paths.append(f"[MALFORMED:{entry!r}]")
            index += 1
            continue
        status = entry[:2]
        path = entry[3:].replace("\\", "/")
        if status[0] in {"R", "C"}:
            index += 1
            if index < len(items) and items[index]:
                path = items[index].replace("\\", "/")
        paths.append(path)
        index += 1
    return sorted(set(paths))



def explicit_artifacts(event: dict[str, Any]) -> dict[str, Any]:
    declared = event.get("artifacts", {})
    if not isinstance(declared, dict):
        return {}
    out: dict[str, Any] = {}
    transcript_value = declared.get("transcript_path")
    if transcript_value:
        path = Path(str(transcript_value))
        if path.is_file():
            try:
                raw = path.read_text(encoding="utf-8-sig", errors="replace")
                out["transcript"] = {
                    "source_path": str(path),
                    "sha256": sha256_file(path),
                    "bytes": path.stat().st_size,
                    "content_bounded": redact_text(raw),
                    "status": "COMPLETE",
                }
            except Exception as exc:
                out["transcript"] = {
                    "source_path": str(path),
                    "status": "ERROR",
                    "error": redact_text(repr(exc)),
                }
        else:
            out["transcript"] = {
                "source_path": str(path),
                "status": "UNAVAILABLE",
            }
    return out


def repo_observations(repo: Path | None) -> dict[str, Any]:
    if repo is None:
        return {
            "status": "UNAVAILABLE",
            "error": "repository_not_supplied",
            "probes": {},
        }
    repo = repo.resolve()
    probes: dict[str, Any] = {}
    probes["root"] = run_probe(["git", "rev-parse", "--show-toplevel"], repo)
    probes["branch"] = run_probe(["git", "branch", "--show-current"], repo)
    probes["head"] = run_probe(["git", "rev-parse", "HEAD"], repo)
    probes["remote_head"] = run_probe(
        ["git", "ls-remote", "origin", "refs/heads/main"],
        repo,
    )
    probes["status"] = run_probe(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        repo,
    )
    probes["diff_check"] = run_probe(["git", "diff", "--check"], repo)
    probes["diff_stat"] = run_probe(["git", "diff", "--stat"], repo)

    status_probe = probes["status"]
    changed_paths: list[str] = []
    if status_probe["status"] == "COMPLETE" and status_probe["exit_code"] == 0:
        changed_paths = parse_porcelain_z(status_probe["stdout"])

    complete = all(
        probe.get("status") == "COMPLETE"
        for probe in probes.values()
    )
    return {
        "status": "COMPLETE" if complete else "PARTIAL",
        "repository": str(repo),
        "changed_paths": changed_paths,
        "probes": probes,
    }


def store_paths(store_root: Path, capsule_id: str) -> dict[str, Path]:
    capsule_root = store_root / "capsules" / capsule_id
    return {
        "capsule_root": capsule_root,
        "full": capsule_root / "full.json",
        "transport": capsule_root / "transport.json",
        "registry": store_root / "registry.json",
        "active": store_root / "active.json",
        "inbox": store_root / "inbox",
    }


def load_registry(store_root: Path) -> dict[str, Any]:
    path = store_root / "registry.json"
    if not path.is_file():
        return {
            "schema": REGISTRY_SCHEMA,
            "generation": 0,
            "entries": {},
            "updated_at": utc_now(),
        }
    data = load_json(path)
    if data.get("schema") != REGISTRY_SCHEMA:
        raise ValueError("diagnostic registry schema mismatch")
    data.setdefault("entries", {})
    return data


def select_latest_unresolved(registry: dict[str, Any]) -> dict[str, Any] | None:
    entries = []
    for capsule_id, entry in registry.get("entries", {}).items():
        if entry.get("state") == "UNRESOLVED":
            entries.append((str(entry.get("created_at", "")), capsule_id, entry))
    if not entries:
        return None
    entries.sort()
    _, capsule_id, entry = entries[-1]
    return {"capsule_id": capsule_id, **entry}


def refresh_active_pointer(store_root: Path, registry: dict[str, Any]) -> dict[str, Any]:
    selected = select_latest_unresolved(registry)
    active_path = store_root / "active.json"
    if selected is None:
        payload = {
            "schema": ACTIVE_SCHEMA,
            "state": "NONE",
            "active_capsule_id": None,
            "updated_at": utc_now(),
        }
    else:
        payload = {
            "schema": ACTIVE_SCHEMA,
            "state": "UNRESOLVED",
            "active_capsule_id": selected["capsule_id"],
            "capsule_path": selected["full_path"],
            "transport_path": selected["transport_path"],
            "capsule_fingerprint": selected["capsule_fingerprint"],
            "subject": selected.get("subject", {}),
            "updated_at": utc_now(),
        }
    atomic_write_json(active_path, payload)
    return payload


def make_transport(full: dict[str, Any], full_path: Path) -> dict[str, Any]:
    failure = full.get("failure", {})
    repo = full.get("repository_observation", {})
    transport = {
        "schema": TRANSPORT_SCHEMA,
        "capsule_id": full["capsule_id"],
        "state": full["state"],
        "authority": "EVIDENCE_ONLY",
        "created_at": full["created_at"],
        "subject": full.get("subject", {}),
        "failure": {
            "stage": failure.get("stage"),
            "detection": failure.get("detection"),
            "exception_type": failure.get("exception_type"),
            "message": redact_text(failure.get("message", ""), MAX_TRANSPORT_TEXT),
            "script_stack_trace": redact_text(
                failure.get("script_stack_trace", ""),
                MAX_TRANSPORT_TEXT,
            ),
            "exit_code": failure.get("exit_code"),
            "probe_status": failure.get("probe_status"),
            "subject_result": failure.get("subject_result"),
            "mismatch_domain": failure.get("mismatch_domain"),
            "failure_family": failure.get("failure_family"),
            "root_cause": failure.get("root_cause"),
            "prevention_gap": failure.get("prevention_gap"),
            "candidate_regressions": failure.get("candidate_regressions", []),
        },
        "execution": full.get("execution", {}),
        "repository": {
            "status": repo.get("status"),
            "repository": repo.get("repository"),
            "changed_paths": repo.get("changed_paths", []),
            "branch": repo.get("probes", {}).get("branch", {}),
            "head": repo.get("probes", {}).get("head", {}),
            "remote_head": repo.get("probes", {}).get("remote_head", {}),
            "diff_check": repo.get("probes", {}).get("diff_check", {}),
        },
        "acquisition": full.get("acquisition", {}),
        "artifact_refs": {
            name: {
                "source_path": item.get("source_path"),
                "sha256": item.get("sha256"),
                "bytes": item.get("bytes"),
                "status": item.get("status"),
            }
            for name, item in full.get("artifacts", {}).items()
            if isinstance(item, dict)
        },
        "evidence_refs": full.get("evidence_refs", []),
        "full_capsule_local_ref": str(full_path),
        "privacy": {
            "transport_redaction": "BOUNDED_REDACTED_MACHINE_RELEVANT",
            "environment_dump_collected": False,
        },
    }
    return redact(transport)


def capture_capsule(
    *,
    store_root: Path,
    event: dict[str, Any],
    repo: Path | None,
) -> dict[str, Any]:
    safe_event = redact(event)
    subject = safe_event.get("subject", {})
    if not isinstance(subject, dict):
        subject = {"ref": str(subject)}
    failure = safe_event.get("failure", {})
    if not isinstance(failure, dict):
        failure = {"message": str(failure)}
    execution = safe_event.get("execution", {})
    if not isinstance(execution, dict):
        execution = {"value": execution}

    repo_evidence = repo_observations(repo)
    material = json.dumps(
        {
            "subject": subject,
            "failure": failure,
            "created_at": utc_now(),
            "nonce": uuid.uuid4().hex,
        },
        sort_keys=True,
    ).encode("utf-8")
    capsule_id = "DCAP-" + sha256_bytes(material)[:20]
    paths = store_paths(store_root, capsule_id)

    probe_errors: list[str] = []
    for name, probe in repo_evidence.get("probes", {}).items():
        if probe.get("status") == "ERROR":
            probe_errors.append(f"{name}:{probe.get('stderr', '')}")

    full = {
        "schema": CAPSULE_SCHEMA,
        "tool_version": TOOL_VERSION,
        "capsule_id": capsule_id,
        "state": "UNRESOLVED",
        "authority": "EVIDENCE_ONLY",
        "created_at": utc_now(),
        "subject": subject,
        "failure": {
            "stage": failure.get("stage"),
            "detection": failure.get("detection"),
            "exception_type": failure.get("exception_type"),
            "message": redact_text(failure.get("message", "")),
            "fully_qualified_error_id": failure.get("fully_qualified_error_id"),
            "script_stack_trace": redact_text(
                failure.get("script_stack_trace", "")
            ),
            "exit_code": failure.get("exit_code"),
            "probe_status": failure.get("probe_status", "COMPLETE"),
            "subject_result": failure.get("subject_result", "UNKNOWN"),
            "raw_error_bounded": redact_text(
                failure.get("raw_error_bounded", failure.get("message", ""))
            ),
            "mismatch_domain": failure.get("mismatch_domain"),
            "failure_family": failure.get("failure_family"),
            "root_cause": failure.get("root_cause"),
            "prevention_gap": failure.get("prevention_gap"),
            "candidate_regressions": failure.get("candidate_regressions", []),
        },
        "execution": execution,
        "repository_observation": repo_evidence,
        "artifacts": explicit_artifacts(safe_event),
        "evidence_refs": safe_event.get("evidence_refs", []),
        "acquisition": {
            "mode": "PROGRESSIVE",
            "stage": "CORE",
            "status": "PARTIAL" if probe_errors else "COMPLETE",
            "probe_errors": probe_errors,
            "full_environment_dump_collected": False,
        },
        "privacy": {
            "local_artifact": "BOUNDED_MINIMIZED",
            "transport": "BOUNDED_REDACTED_MACHINE_RELEVANT",
            "secrets_redacted": True,
        },
        "resolution": None,
    }
    full = redact(full)
    atomic_write_json(paths["full"], full)
    transport = make_transport(full, paths["full"])
    atomic_write_json(paths["transport"], transport)

    fingerprint = sha256_file(paths["full"])
    registry = load_registry(store_root)
    registry["generation"] = int(registry.get("generation", 0)) + 1
    registry["updated_at"] = utc_now()
    registry["entries"][capsule_id] = {
        "state": "UNRESOLVED",
        "created_at": full["created_at"],
        "full_path": str(paths["full"]),
        "transport_path": str(paths["transport"]),
        "capsule_fingerprint": fingerprint,
        "subject": subject,
    }
    atomic_write_json(paths["registry"], registry)
    active = refresh_active_pointer(store_root, registry)
    return {
        "capsule_id": capsule_id,
        "full_path": str(paths["full"]),
        "transport_path": str(paths["transport"]),
        "capsule_fingerprint": fingerprint,
        "active": active,
    }


def latest_unresolved_context(store_root: Path | None = None) -> dict[str, Any] | None:
    root = (store_root or default_store_root()).resolve()
    try:
        registry = load_registry(root)
        selected = select_latest_unresolved(registry)
        if selected is None:
            return None
        full_path = Path(selected["full_path"])
        if not full_path.is_file():
            return {
                "status": "ERROR",
                "capsule_id": selected["capsule_id"],
                "path": str(full_path),
                "error": "active_capsule_file_missing",
            }
        full = load_json(full_path)
        return {
            "status": "COMPLETE",
            "capsule_id": full.get("capsule_id"),
            "state": full.get("state"),
            "path": str(full_path),
            "fingerprint": selected.get("capsule_fingerprint"),
            "subject": full.get("subject", {}),
            "failure": {
                "stage": full.get("failure", {}).get("stage"),
                "detection": full.get("failure", {}).get("detection"),
                "message": bounded_text(
                    full.get("failure", {}).get("message", ""),
                    2048,
                ),
                "mismatch_domain": full.get("failure", {}).get("mismatch_domain"),
                "failure_family": full.get("failure", {}).get("failure_family"),
                "root_cause": full.get("failure", {}).get("root_cause"),
                "candidate_regressions": full.get("failure", {}).get("candidate_regressions", []),
            },
        }
    except Exception as exc:
        return {
            "status": "ERROR",
            "capsule_id": None,
            "path": None,
            "error": repr(exc),
        }


def resolve_capsules(
    *,
    store_root: Path,
    patch_id: str,
    resulting_commit: str,
    repair_revision: str | None = None,
    root_cause: str | None = None,
    resolution_summary: str | None = None,
    prevention_refs: list[str] | None = None,
) -> dict[str, Any]:
    registry = load_registry(store_root)
    resolved: list[str] = []
    for capsule_id, entry in registry.get("entries", {}).items():
        if entry.get("state") != "UNRESOLVED":
            continue
        subject = entry.get("subject", {})
        if str(subject.get("patch_id", "")) != patch_id:
            continue
        full_path = Path(entry["full_path"])
        if full_path.is_file():
            full = load_json(full_path)
            full["state"] = "RESOLVED"
            full["resolution"] = {
                "resolved_at": utc_now(),
                "patch_id": patch_id,
                "resulting_commit": resulting_commit,
                "repair_revision": repair_revision,
                "root_cause": root_cause,
                "resolution_summary": resolution_summary,
                "prevention_refs": list(prevention_refs or []),
            }
            atomic_write_json(full_path, full)
            transport_path = Path(entry["transport_path"])
            atomic_write_json(transport_path, make_transport(full, full_path))
            entry["capsule_fingerprint"] = sha256_file(full_path)
        entry["state"] = "RESOLVED"
        entry["resolved_at"] = utc_now()
        entry["resulting_commit"] = resulting_commit
        entry["repair_revision"] = repair_revision
        entry["root_cause"] = root_cause
        entry["resolution_summary"] = resolution_summary
        entry["prevention_refs"] = list(prevention_refs or [])
        resolved.append(capsule_id)

    registry["generation"] = int(registry.get("generation", 0)) + 1
    registry["updated_at"] = utc_now()
    atomic_write_json(store_root / "registry.json", registry)
    active = refresh_active_pointer(store_root, registry)
    return {
        "state": "RESOLVED" if resolved else "NO_MATCH",
        "patch_id": patch_id,
        "resulting_commit": resulting_commit,
        "resolved_capsules": resolved,
        "active": active,
    }


def failure_handoff(store_root: Path, capsule_id: str | None = None) -> dict[str, Any]:
    registry = load_registry(store_root)
    if capsule_id is None:
        selected = select_latest_unresolved(registry)
        if selected is None:
            return {"schema": "cerebro-failure-handoff/v0.1", "state": "NONE"}
        capsule_id = selected["capsule_id"]
    entry = registry.get("entries", {}).get(capsule_id)
    if not entry:
        return {"schema": "cerebro-failure-handoff/v0.1", "state": "NOT_FOUND", "capsule_id": capsule_id}
    full_path = Path(entry["full_path"])
    full = load_json(full_path)
    failure = full.get("failure", {})
    subject = full.get("subject", {})
    return redact({
        "schema": "cerebro-failure-handoff/v0.1",
        "state": "PATCH_FAIL" if entry.get("state") == "UNRESOLVED" else entry.get("state"),
        "authority": "EVIDENCE_ONLY",
        "capsule_id": capsule_id,
        "capsule_fingerprint": entry.get("capsule_fingerprint"),
        "patch_id": subject.get("patch_id"),
        "revision": subject.get("revision"),
        "baseline_commit": subject.get("baseline_commit"),
        "stage": failure.get("stage"),
        "detection": bounded_text(failure.get("detection", failure.get("message", "")), 1600),
        "failure_family": failure.get("failure_family"),
        "root_cause": bounded_text(failure.get("root_cause", ""), 800) if failure.get("root_cause") else None,
        "candidate_regressions": failure.get("candidate_regressions", [])[:12],
        "acquisition_status": full.get("acquisition", {}).get("status"),
        "local_evidence_ref": str(full_path),
        "transport_required_only_if_targeted_detail_needed": True,
    })


def print_failure_handoff(store_root: Path, capsule_id: str | None = None) -> int:
    payload = failure_handoff(store_root, capsule_id)
    print("CEREBRO_FAILURE_HANDOFF")
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    print("CEREBRO_FAILURE_HANDOFF_END")
    return 0 if payload.get("state") not in {"NOT_FOUND"} else 2


def print_transport(store_root: Path, capsule_id: str | None) -> int:
    registry = load_registry(store_root)
    if capsule_id is None:
        selected = select_latest_unresolved(registry)
        if selected is None:
            print("CEREBRO_DIAGNOSTIC_TRANSPORT STATE=NONE")
            return 0
        capsule_id = selected["capsule_id"]
    entry = registry.get("entries", {}).get(capsule_id)
    if not entry:
        print(f"CEREBRO_DIAGNOSTIC_TRANSPORT STATE=NOT_FOUND CAPSULE={capsule_id}")
        return 2
    path = Path(entry["transport_path"])
    payload = load_json(path)
    print("CEREBRO_DIAGNOSTIC_CAPSULE")
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    print("CEREBRO_DIAGNOSTIC_CAPSULE_END")
    return 0


def selftest() -> dict[str, Any]:
    results: list[dict[str, Any]] = []

    def record(name: str, passed: bool, detail: str = "") -> None:
        results.append(
            {
                "name": name,
                "result": "PASS" if passed else "FAIL",
                "detail": detail,
            }
        )

    with tempfile.TemporaryDirectory(prefix="cerebro-diagnostic-selftest-") as temp:
        store = Path(temp) / "store"
        transcript = Path(temp) / "runner-transcript.txt"
        transcript.write_text("primary failure output\n", encoding="utf-8")
        event = {
            "subject": {
                "patch_id": "SELFTEST.PATCH",
                "revision": "R1",
                "baseline_commit": "0" * 40,
            },
            "failure": {
                "stage": "TEST_STAGE",
                "detection": "SELFTEST_FAILURE",
                "message": "token=ghp_123456789012345678901234567890",
                "exception_type": "RuntimeError",
                "exit_code": 17,
                "subject_result": "UNKNOWN",
            },
            "execution": {
                "mutation_started": False,
                "sync_started": False,
            },
            "artifacts": {
                "transcript_path": str(transcript),
            },
        }
        captured = capture_capsule(store_root=store, event=event, repo=None)
        full = load_json(Path(captured["full_path"]))
        record("capture", full.get("state") == "UNRESOLVED")
        record(
            "redaction",
            "ghp_" not in json.dumps(full),
            json.dumps(full.get("failure", {})),
        )
        active = latest_unresolved_context(store)
        compact = failure_handoff(store, captured["capsule_id"])
        record(
            "minimal_failure_handoff",
            compact.get("state") == "PATCH_FAIL"
            and compact.get("patch_id") == "SELFTEST.PATCH"
            and "transcript" not in json.dumps(compact).lower(),
            json.dumps(compact),
        )
        record(
            "artifact_capture",
            full.get("artifacts", {}).get("transcript", {}).get("status") == "COMPLETE",
        )
        record(
            "auto_rehydration",
            bool(active and active.get("capsule_id") == captured["capsule_id"]),
        )
        resolved = resolve_capsules(
            store_root=store,
            patch_id="SELFTEST.PATCH",
            resulting_commit="1" * 40,
            repair_revision="R2",
            root_cause="SELFTEST_ROOT_CAUSE",
            resolution_summary="selftest repair",
            prevention_refs=["REG-SELFTEST"],
        )
        record(
            "resolution_traceability",
            captured["capsule_id"] in resolved.get("resolved_capsules", []),
        )
        resolved_full = load_json(Path(captured["full_path"]))
        record(
            "resolution_lineage",
            resolved_full.get("resolution", {}).get("repair_revision") == "R2"
            and resolved_full.get("resolution", {}).get("root_cause") == "SELFTEST_ROOT_CAUSE"
            and "REG-SELFTEST" in resolved_full.get("resolution", {}).get("prevention_refs", []),
            json.dumps(resolved_full.get("resolution", {})),
        )
        record(
            "active_cleared_after_resolution",
            latest_unresolved_context(store) is None,
        )

    passed = all(item["result"] == "PASS" for item in results)
    return {
        "schema": "cerebro-diagnostic-selftest/v0.1",
        "result": "PASS" if passed else "FAIL",
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Cerebro Diagnostic Capsule")
    parser.add_argument("--store-root")
    sub = parser.add_subparsers(dest="command", required=True)

    capture_parser = sub.add_parser("capture")
    capture_parser.add_argument("--event-file", required=True)
    capture_parser.add_argument("--repo")

    sub.add_parser("latest")

    transport_parser = sub.add_parser("transport")
    transport_parser.add_argument("--capsule-id")

    handoff_parser = sub.add_parser("handoff")
    handoff_parser.add_argument("--capsule-id")

    resolve_parser = sub.add_parser("resolve")
    resolve_parser.add_argument("--patch-id", required=True)
    resolve_parser.add_argument("--resulting-commit", required=True)
    resolve_parser.add_argument("--repair-revision")
    resolve_parser.add_argument("--root-cause")
    resolve_parser.add_argument("--resolution-summary")
    resolve_parser.add_argument("--prevention-ref", action="append", default=[])

    sub.add_parser("selftest")
    args = parser.parse_args()
    store = (
        Path(args.store_root).resolve()
        if args.store_root
        else default_store_root().resolve()
    )

    try:
        if args.command == "capture":
            event = load_json(Path(args.event_file))
            repo = Path(args.repo).resolve() if args.repo else None
            result = capture_capsule(store_root=store, event=event, repo=repo)
            print(
                "CEREBRO_DIAGNOSTIC_CAPTURE "
                f"STATE=UNRESOLVED CAPSULE={result['capsule_id']} "
                f"PATH={result['full_path']}"
            )
            return 0
        if args.command == "latest":
            context = latest_unresolved_context(store)
            print(json.dumps(context or {"state": "NONE"}, indent=2))
            return 0
        if args.command == "transport":
            return print_transport(store, args.capsule_id)
        if args.command == "handoff":
            return print_failure_handoff(store, args.capsule_id)
        if args.command == "resolve":
            result = resolve_capsules(
                store_root=store,
                patch_id=args.patch_id,
                resulting_commit=args.resulting_commit,
                repair_revision=args.repair_revision,
                root_cause=args.root_cause,
                resolution_summary=args.resolution_summary,
                prevention_refs=args.prevention_ref,
            )
            print(json.dumps(result, indent=2))
            return 0
        if args.command == "selftest":
            result = selftest()
            print(json.dumps(result, indent=2))
            return 0 if result["result"] == "PASS" else 1
    except Exception as exc:
        print(
            json.dumps(
                {
                    "result": "FAIL",
                    "classification": "DIAGNOSTIC_TOOL_FAILURE",
                    "detail": repr(exc),
                },
                indent=2,
            )
        )
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
