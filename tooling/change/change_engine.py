#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    import yaml
except Exception as exc:  # pragma: no cover
    raise SystemExit(f"BLOCKED_PREREQUISITE:PyYAML:{exc}")

SCHEMA_ID = "cerebro-change-capsule/v0.2"
REPORT_SCHEMA = "cerebro-change-campaign-report/v0.1"
KNOWLEDGE_SCHEMA = "cerebro-failure-knowledge/v0.1"
ENGINE_VERSION = "0.3.1"

PROFILE_RUNS = {"FAST": 1, "STANDARD": 2, "DEEP": 3}


class ChangeError(RuntimeError):
    def __init__(self, classification: str, detail: str):
        super().__init__(detail)
        self.classification = classification
        self.detail = detail


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(cmd: list[str], cwd: Path, *, capture: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, cwd=cwd, text=True, capture_output=capture, check=False)
    except OSError as exc:
        raise ChangeError("PROCESS_START_FAILURE", f"{cmd[0]}:{exc}") from exc


def git(repo: Path, *args: str, allowed: Iterable[int] = (0,)) -> str:
    result = run(["git", *args], repo, capture=True)
    if result.returncode not in set(allowed):
        detail = (result.stdout + "\n" + result.stderr).strip()
        raise ChangeError("GIT_OPERATION_FAILURE", f"git {' '.join(args)} exit={result.returncode}: {detail}")
    return result.stdout.strip()


def safe_relative(value: str) -> str:
    candidate = value.replace("\\", "/").strip("/")
    p = Path(candidate)
    if not candidate or p.is_absolute() or ".." in p.parts or ":" in candidate:
        raise ChangeError("INVALID_CAPSULE", f"unsafe relative path: {value}")
    return candidate


def contained(root: Path, relative: str) -> Path:
    target = (root / safe_relative(relative)).resolve(strict=False)
    resolved_root = root.resolve()
    try:
        target.relative_to(resolved_root)
    except ValueError as exc:
        raise ChangeError("INVALID_CAPSULE", f"path escapes root: {relative}") from exc
    return target


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ChangeError("INVALID_CAPSULE", f"cannot parse JSON {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ChangeError("INVALID_CAPSULE", f"expected JSON object: {path}")
    return data


def validate_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schema") != SCHEMA_ID:
        raise ChangeError("INVALID_CAPSULE", f"schema must be {SCHEMA_ID}")
    change = manifest.get("change")
    authority = manifest.get("authority")
    assurance = manifest.get("assurance")
    files = manifest.get("files")
    if not isinstance(change, dict) or not str(change.get("id", "")).strip():
        raise ChangeError("INVALID_CAPSULE", "change.id required")
    if not isinstance(authority, dict):
        raise ChangeError("INVALID_CAPSULE", "authority required")
    if authority.get("repository") != "morgul-tech/Cerebro-Source-1.0":
        raise ChangeError("INVALID_CAPSULE", "unexpected authoritative repository")
    if authority.get("branch") != "main":
        raise ChangeError("INVALID_CAPSULE", "unexpected authoritative branch")
    base = str(authority.get("base_commit", ""))
    if len(base) != 40 or any(ch not in "0123456789abcdefABCDEF" for ch in base):
        raise ChangeError("INVALID_CAPSULE", "authority.base_commit must be 40 hex chars")
    if not isinstance(assurance, dict) or assurance.get("profile") not in PROFILE_RUNS:
        raise ChangeError("INVALID_CAPSULE", "assurance.profile must be FAST, STANDARD, or DEEP")
    if not isinstance(files, list) or not files:
        raise ChangeError("INVALID_CAPSULE", "files must be non-empty")
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            raise ChangeError("INVALID_CAPSULE", "file entry must be object")
        path = safe_relative(str(item.get("path", "")))
        if path in seen:
            raise ChangeError("INVALID_CAPSULE", f"duplicate path: {path}")
        seen.add(path)
        if item.get("operation") not in {"create", "replace", "delete"}:
            raise ChangeError("INVALID_CAPSULE", f"invalid operation for {path}")
        baseline = item.get("baseline")
        if not isinstance(baseline, dict) or baseline.get("state") not in {"present", "absent"}:
            raise ChangeError("INVALID_CAPSULE", f"baseline required for {path}")
        if baseline["state"] == "present":
            blob = str(baseline.get("git_blob_sha", ""))
            if len(blob) != 40 or any(ch not in "0123456789abcdefABCDEF" for ch in blob):
                raise ChangeError("INVALID_CAPSULE", f"baseline git_blob_sha required for {path}")
        elif "git_blob_sha" in baseline:
            raise ChangeError("INVALID_CAPSULE", f"absent baseline must not declare git_blob_sha for {path}")
        if "sha256" in baseline:
            raise ChangeError("INVALID_CAPSULE", f"tracked baseline sha256 is prohibited for {path}")
        if item["operation"] != "delete":
            payload = safe_relative(str(item.get("payload", "")))
            digest = str(item.get("sha256", ""))
            if len(digest) != 64:
                raise ChangeError("INVALID_CAPSULE", f"payload sha256 required for {path}")
            if payload.startswith("../"):
                raise ChangeError("INVALID_CAPSULE", f"unsafe payload: {payload}")


def verify_payloads(capsule_root: Path, manifest: dict[str, Any]) -> None:
    for item in manifest["files"]:
        if item["operation"] == "delete":
            continue
        source = contained(capsule_root, item["payload"])
        if not source.is_file():
            raise ChangeError("INVALID_CAPSULE", f"missing payload: {item['payload']}")
        actual = sha256_file(source)
        if actual.lower() != str(item["sha256"]).lower():
            raise ChangeError("INVALID_CAPSULE", f"payload hash mismatch: {item['payload']}")


def verify_repo_identity(repo: Path, manifest: dict[str, Any]) -> None:
    if not (repo / ".git").exists() and not git(repo, "rev-parse", "--git-dir"):
        raise ChangeError("UNKNOWN_AUTHORITATIVE_STATE", "repository is not Git")
    root = Path(git(repo, "rev-parse", "--show-toplevel")).resolve()
    if root != repo.resolve():
        raise ChangeError("UNKNOWN_AUTHORITATIVE_STATE", f"repository binding mismatch: {root}")
    origin = git(repo, "remote", "get-url", "origin").lower().rstrip("/")
    normalized = origin.removesuffix(".git")
    if not normalized.endswith("morgul-tech/cerebro-source-1.0"):
        raise ChangeError("UNKNOWN_AUTHORITATIVE_STATE", f"unexpected origin: {origin}")
    base = manifest["authority"]["base_commit"].lower()
    try:
        git(repo, "cat-file", "-e", f"{base}^{{commit}}")
    except ChangeError as exc:
        raise ChangeError("UNKNOWN_AUTHORITATIVE_STATE", f"base commit not present: {base}") from exc


def verify_baseline(repo: Path, manifest: dict[str, Any]) -> None:
    base = manifest["authority"]["base_commit"]
    if changed_paths(repo):
        raise ChangeError("BASELINE_MISMATCH", "candidate worktree must be clean before apply")
    for item in manifest["files"]:
        path = safe_relative(item["path"])
        target = contained(repo, path)
        baseline = item["baseline"]
        if baseline["state"] == "absent":
            tracked = git(repo, "ls-tree", "--name-only", base, "--", path)
            if tracked.strip():
                raise ChangeError("BASELINE_MISMATCH", f"expected Git-absent path: {path}")
            if target.exists():
                raise ChangeError("BASELINE_MISMATCH", f"expected physical absent path: {path}")
        else:
            if not target.is_file():
                raise ChangeError("BASELINE_MISMATCH", f"expected physical file: {path}")
            actual_blob = git(repo, "rev-parse", f"{base}:{path}").lower()
            expected_blob = str(baseline["git_blob_sha"]).lower()
            if actual_blob != expected_blob:
                raise ChangeError(
                    "BASELINE_MISMATCH",
                    f"Git blob mismatch: {path} expected={expected_blob} actual={actual_blob}",
                )


def apply_payload(capsule_root: Path, repo: Path, manifest: dict[str, Any]) -> None:
    verify_baseline(repo, manifest)
    for item in manifest["files"]:
        target = contained(repo, item["path"])
        if item["operation"] == "delete":
            target.unlink(missing_ok=True)
            continue
        source = contained(capsule_root, item["payload"])
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.cerebro-{uuid.uuid4().hex}.tmp")
        shutil.copy2(source, tmp)
        if sha256_file(tmp).lower() != str(item["sha256"]).lower():
            tmp.unlink(missing_ok=True)
            raise ChangeError("PAYLOAD_WRITE_FAILURE", f"temporary hash mismatch: {item['path']}")
        os.replace(tmp, target)
        if sha256_file(target).lower() != str(item["sha256"]).lower():
            raise ChangeError("PAYLOAD_WRITE_FAILURE", f"installed hash mismatch: {item['path']}")


def changed_paths(repo: Path) -> list[str]:
    result = run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"], repo, capture=True)
    if result.returncode != 0:
        raise ChangeError("GIT_OPERATION_FAILURE", result.stderr.strip())
    raw = result.stdout
    out: list[str] = []
    parts = raw.split("\0")
    i = 0
    while i < len(parts):
        entry = parts[i]
        if not entry:
            i += 1
            continue
        if len(entry) < 4:
            raise ChangeError("MACHINE_PROTOCOL_FAILURE", f"malformed porcelain entry: {entry!r}")
        status = entry[:2]
        path = entry[3:].replace("\\", "/")
        if status[0] in {"R", "C"}:
            i += 1
            if i >= len(parts) or not parts[i]:
                raise ChangeError("MACHINE_PROTOCOL_FAILURE", "rename/copy path missing")
            path = parts[i].replace("\\", "/")
        out.append(path)
        i += 1
    return sorted(set(out))


POWERSHELL_SCOPE_PREFIXES = {"env", "global", "script", "local", "private", "using"}


def powershell_ambiguous_colon_interpolations(text: str) -> list[tuple[int, str]]:
    """Conservative producer guard; target PowerShell parsing remains mandatory."""
    findings: list[tuple[int, str]] = []
    pattern = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)\:")
    for lineno, line in enumerate(text.splitlines(), 1):
        for match in pattern.finditer(line):
            if match.group(1).lower() in POWERSHELL_SCOPE_PREFIXES:
                continue
            findings.append((lineno, match.group(0)))
    return findings


def parse_structured(path: Path) -> None:
    suffix = path.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        try:
            list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        except Exception as exc:
            raise ChangeError("STRUCTURED_SERIALIZATION", f"YAML parse failed {path}: {exc}") from exc
    elif suffix == ".json":
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ChangeError("STRUCTURED_SERIALIZATION", f"JSON parse failed {path}: {exc}") from exc
    elif suffix == ".py":
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        except Exception as exc:
            raise ChangeError("CODE_SYNTAX", f"Python compile failed {path}: {exc}") from exc


def semantic_candidate_checks(repo: Path, manifest: dict[str, Any]) -> None:
    expected = sorted(item["path"].replace("\\", "/") for item in manifest["files"])
    actual = changed_paths(repo)
    if actual != expected:
        raise ChangeError("UNEXPECTED_CHANGE_SCOPE", f"expected={expected}; actual={actual}")
    for item in manifest["files"]:
        target = contained(repo, item["path"])
        if item["operation"] == "delete":
            if target.exists():
                raise ChangeError("PAYLOAD_WRITE_FAILURE", f"delete not effective: {item['path']}")
        else:
            if sha256_file(target).lower() != str(item["sha256"]).lower():
                raise ChangeError("PAYLOAD_WRITE_FAILURE", f"candidate hash mismatch: {item['path']}")
            parse_structured(target)
    result = run(["git", "diff", "--check"], repo, capture=True)
    if result.returncode != 0:
        raise ChangeError("DIFF_CHECK_FAILURE", (result.stdout + result.stderr).strip())

    # Architecture-level invariants for this change family.
    required = {
        "standards/change-architecture.yaml",
        "standards/change-delivery.yaml",
        "tooling/host/component.yaml",
        "tooling/host/cerebro_host.py",
        "tooling/change/component.yaml",
        "tooling/change/change_engine.py",
        "tooling/change/capsule-schema.json",
        "tooling/change/campaign-policy.yaml",
        "tooling/change/failure-record-schema.json",
    }
    declared = {item["path"] for item in manifest["files"]}
    if manifest["change"]["id"] == "PATCH-005.POST-001" and not required.issubset(declared):
        missing = sorted(required - declared)
        raise ChangeError("ARCHITECTURE_CONTRACT_INCOMPLETE", f"missing required change paths: {missing}")


def family_for(classification: str) -> str:
    mapping = {
        "STRUCTURED_SERIALIZATION": "REPRESENTATION_AND_SERIALIZATION",
        "CODE_SYNTAX": "REPRESENTATION_AND_SERIALIZATION",
        "INVALID_CAPSULE": "CHANGE_CAPSULE_INTEGRITY",
        "BASELINE_MISMATCH": "AUTHORITY_AND_BASELINE",
        "UNKNOWN_AUTHORITATIVE_STATE": "AUTHORITY_AND_BASELINE",
        "GIT_OPERATION_FAILURE": "GIT_EXECUTION",
        "UNEXPECTED_CHANGE_SCOPE": "MUTATION_SCOPE",
        "PAYLOAD_WRITE_FAILURE": "MUTATION_SCOPE",
        "DIFF_CHECK_FAILURE": "MUTATION_SCOPE",
        "MACHINE_PROTOCOL_FAILURE": "MACHINE_PROTOCOL",
        "PROCESS_START_FAILURE": "PROCESS_EXECUTION",
        "ARCHITECTURE_CONTRACT_INCOMPLETE": "ARCHITECTURE_CROSS_REFERENCE",
    }
    return mapping.get(classification, "UNCLASSIFIED")


@dataclass
class CampaignRun:
    run_id: str
    index: int
    result: str
    classification: str | None
    family: str | None
    detail: str
    worktree: str


def load_knowledge(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {
            "schema": KNOWLEDGE_SCHEMA,
            "generation": 0,
            "events": [],
            "families": {},
        }
    data = load_json(path)
    if data.get("schema") != KNOWLEDGE_SCHEMA:
        raise ChangeError("FAILURE_KNOWLEDGE_INVALID", "unknown failure knowledge schema")
    return data


def update_knowledge(previous: dict[str, Any], runs: list[CampaignRun], change_id: str) -> dict[str, Any]:
    knowledge = json.loads(json.dumps(previous))
    knowledge["schema"] = KNOWLEDGE_SCHEMA
    knowledge["generation"] = int(previous.get("generation", 0)) + 1
    knowledge.setdefault("events", [])
    knowledge.setdefault("families", {})
    for run_item in runs:
        if run_item.result == "PASS":
            continue
        event = {
            "change_id": change_id,
            "run_id": run_item.run_id,
            "classification": run_item.classification,
            "family": run_item.family,
            "detail": run_item.detail,
            "observed_at": now(),
        }
        knowledge["events"].append(event)
        fam = knowledge["families"].setdefault(run_item.family or "UNCLASSIFIED", {
            "observations": 0,
            "distinct_runs": [],
            "status": "OBSERVED",
        })
        fam["observations"] = int(fam.get("observations", 0)) + 1
        if run_item.run_id not in fam["distinct_runs"]:
            fam["distinct_runs"].append(run_item.run_id)
        if len(fam["distinct_runs"]) >= 2:
            fam["status"] = "SYSTEMIC_SUSPECTED"
    return knowledge


def stability_gate(runs: list[CampaignRun], knowledge: dict[str, Any], required_runs: int) -> tuple[str, list[str]]:
    blockers: list[str] = []
    if len(runs) != required_runs:
        blockers.append(f"required_campaign_runs={required_runs}; actual={len(runs)}")
    failures = [r for r in runs if r.result != "PASS"]
    if failures:
        blockers.append("current_candidate_failure")
    systemic = sorted(name for name, value in knowledge.get("families", {}).items() if value.get("status") == "SYSTEMIC_SUSPECTED")
    if systemic:
        blockers.append("unresolved_systemic_failure_families:" + ",".join(systemic))
    return ("PASS" if not blockers else "BLOCK", blockers)


def one_campaign(capsule_root: Path, source_repo: Path, manifest: dict[str, Any], index: int, temp_root: Path) -> CampaignRun:
    run_id = f"CAMPAIGN-{index:02d}-{uuid.uuid4().hex[:12]}"
    worktree = temp_root / run_id
    base = manifest["authority"]["base_commit"]
    try:
        git(source_repo, "worktree", "add", "--detach", "--force", str(worktree), base)
        apply_payload(capsule_root, worktree, manifest)
        semantic_candidate_checks(worktree, manifest)
        return CampaignRun(run_id, index, "PASS", None, None, "", str(worktree))
    except ChangeError as exc:
        return CampaignRun(run_id, index, "FAIL", exc.classification, family_for(exc.classification), exc.detail, str(worktree))
    except Exception as exc:
        return CampaignRun(run_id, index, "FAIL", "UNEXPECTED_EXCEPTION", "UNCLASSIFIED", repr(exc), str(worktree))
    finally:
        if worktree.exists():
            try:
                git(source_repo, "worktree", "remove", "--force", str(worktree))
            except Exception:
                shutil.rmtree(worktree, ignore_errors=True)
        try:
            git(source_repo, "worktree", "prune")
        except Exception:
            pass


def execute_campaigns(capsule_root: Path, source_repo: Path, manifest: dict[str, Any], profile: str, knowledge_path: Path | None, report_path: Path | None) -> dict[str, Any]:
    validate_manifest(manifest)
    verify_payloads(capsule_root, manifest)
    verify_repo_identity(source_repo, manifest)

    declared_profile = manifest["assurance"]["profile"]
    if profile != declared_profile:
        raise ChangeError("ASSURANCE_PROFILE_MISMATCH", f"manifest={declared_profile}; requested={profile}")
    required_runs = PROFILE_RUNS[profile]
    previous = load_knowledge(knowledge_path)

    with tempfile.TemporaryDirectory(prefix="cerebro-change-lab-") as temp:
        temp_root = Path(temp)
        runs = [one_campaign(capsule_root, source_repo, manifest, idx + 1, temp_root) for idx in range(required_runs)]

    knowledge = update_knowledge(previous, runs, manifest["change"]["id"])
    gate, blockers = stability_gate(runs, knowledge, required_runs)
    report = {
        "schema": REPORT_SCHEMA,
        "engine_version": ENGINE_VERSION,
        "change_id": manifest["change"]["id"],
        "profile": profile,
        "required_runs": required_runs,
        "knowledge_generation_in": int(previous.get("generation", 0)),
        "knowledge_generation_out": int(knowledge.get("generation", 0)),
        "runs": [asdict(item) for item in runs],
        "stability_gate": gate,
        "blockers": blockers,
        "ready_to_lock": gate == "PASS",
        "created_at": now(),
    }
    if knowledge_path is not None:
        knowledge_path.parent.mkdir(parents=True, exist_ok=True)
        knowledge_path.write_text(json.dumps(knowledge, indent=2) + "\n", encoding="utf-8")
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def selftest() -> dict[str, Any]:
    results: list[dict[str, Any]] = []

    def record(name: str, passed: bool, detail: str = "") -> None:
        results.append({"name": name, "result": "PASS" if passed else "FAIL", "detail": detail})

    with tempfile.TemporaryDirectory(prefix="cerebro-change-selftest-") as temp:
        root = Path(temp)
        repo = root / "repo"
        capsule = root / "capsule"
        payload = capsule / "payload"
        repo.mkdir(); payload.mkdir(parents=True)
        git(repo, "init")
        git(repo, "config", "user.email", "cerebro@example.invalid")
        git(repo, "config", "user.name", "Cerebro Test")
        git(repo, "remote", "add", "origin", "https://github.com/morgul-tech/Cerebro-Source-1.0.git")
        (repo / "base.txt").write_text("BASE\n", encoding="utf-8")
        git(repo, "add", "base.txt")
        git(repo, "commit", "-m", "base")
        base = git(repo, "rev-parse", "HEAD")
        (payload / "base.txt").write_text("NEW\n", encoding="utf-8")
        manifest = {
            "schema": SCHEMA_ID,
            "change": {"id": "SELFTEST", "title": "selftest"},
            "authority": {"repository": "morgul-tech/Cerebro-Source-1.0", "branch": "main", "base_commit": base},
            "assurance": {"profile": "FAST"},
            "files": [{
                "path": "base.txt", "operation": "replace", "payload": "payload/base.txt",
                "sha256": sha256_file(payload / "base.txt"),
                "baseline": {"state": "present", "git_blob_sha": git(repo, "rev-parse", "HEAD:base.txt")},
            }],
        }
        (capsule / "capsule.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        try:
            validate_manifest(manifest); verify_payloads(capsule, manifest); record("capsule_integrity", True)
        except Exception as exc:
            record("capsule_integrity", False, str(exc))

        # Git baseline identity must remain valid even when checkout bytes differ
        # from the committed blob representation (for example CRLF vs LF).
        try:
            git(repo, "config", "core.autocrlf", "true")
            (repo / "base.txt").unlink()
            git(repo, "checkout", "--", "base.txt")
            physical = (repo / "base.txt").read_bytes()
            verify_baseline(repo, manifest)
            record(
                "git_blob_vs_worktree_representation",
                b"\r\n" in physical and not changed_paths(repo),
                repr(physical),
            )
        except Exception as exc:
            record("git_blob_vs_worktree_representation", False, str(exc))

        # Scope parser test.
        (repo / "untracked.txt").write_text("x", encoding="utf-8")
        try:
            paths = changed_paths(repo)
            record("machine_protocol_nul_status", paths == ["untracked.txt"], repr(paths))
        finally:
            (repo / "untracked.txt").unlink()

        # Failure knowledge inheritance + systemic escalation.
        previous = {"schema": KNOWLEDGE_SCHEMA, "generation": 4, "events": [], "families": {}}
        fake_runs = [
            CampaignRun("r1", 1, "FAIL", "STRUCTURED_SERIALIZATION", "REPRESENTATION_AND_SERIALIZATION", "a", ""),
            CampaignRun("r2", 2, "FAIL", "CODE_SYNTAX", "REPRESENTATION_AND_SERIALIZATION", "b", ""),
        ]
        updated = update_knowledge(previous, fake_runs, "SELFTEST")
        fam = updated["families"].get("REPRESENTATION_AND_SERIALIZATION", {})
        record("iteration_inheritance", updated["generation"] == 5)
        record("failure_family_escalation", fam.get("status") == "SYSTEMIC_SUSPECTED")
        gate, blockers = stability_gate(fake_runs, updated, 2)
        record("systemic_root_blocking", gate == "BLOCK" and any("unresolved_systemic" in x for x in blockers), repr(blockers))

        # Final serialization tests.
        good_yaml = root / "good.yaml"; good_yaml.write_text("path: 'D:\\Cerebro\\Run'\n", encoding="utf-8")
        bad_yaml = root / "bad.yaml"; bad_yaml.write_text('path: "D:\\Cerebro\\Run"\n', encoding="utf-8")
        try:
            parse_structured(good_yaml); record("yaml_windows_path_safe", True)
        except Exception as exc:
            record("yaml_windows_path_safe", False, str(exc))
        try:
            parse_structured(bad_yaml); record("yaml_windows_path_rejected", False, "parser unexpectedly accepted")
        except ChangeError:
            record("yaml_windows_path_rejected", True)

        try:
            bad_ps = 'throw "BASELINE_STATE_INVALID:$relative:$baselineState"\n'
            good_ps = 'throw ("BASELINE_STATE_INVALID:{0}:{1}" -f $relative, $baselineState)\n'
            bad_findings = powershell_ambiguous_colon_interpolations(bad_ps)
            good_findings = powershell_ambiguous_colon_interpolations(good_ps)
            record(
                "powershell_ambiguous_colon_interpolation",
                bool(bad_findings) and not good_findings,
                f"bad={bad_findings}; good={good_findings}",
            )
        except Exception as exc:
            record("powershell_ambiguous_colon_interpolation", False, str(exc))

        # Contract evolution must propagate to schema self-identity.
        try:
            schema_path = Path(__file__).with_name("capsule-schema.json")
            schema_doc = load_json(schema_path)
            schema_id = str(schema_doc.get("$id", ""))
            schema_const = str(
                schema_doc.get("properties", {})
                .get("schema", {})
                .get("const", "")
            )
            record(
                "capsule_schema_cross_reference",
                schema_id == SCHEMA_ID and schema_const == SCHEMA_ID,
                f"engine={SCHEMA_ID}; id={schema_id}; const={schema_const}",
            )
        except Exception as exc:
            record("capsule_schema_cross_reference", False, str(exc))

        # Bounded candidate scope.
        test_repo = root / "candidate"
        git(repo, "worktree", "add", "--detach", "--force", str(test_repo), base)
        try:
            apply_payload(capsule, test_repo, manifest)
            semantic_candidate_checks(test_repo, manifest)
            record("bounded_candidate_apply", True)
        except Exception as exc:
            record("bounded_candidate_apply", False, str(exc))
        finally:
            git(repo, "worktree", "remove", "--force", str(test_repo))

    passed = all(item["result"] == "PASS" for item in results)
    return {"schema": "cerebro-change-engine-selftest/v0.1", "result": "PASS" if passed else "FAIL", "tests": results}


def main() -> int:
    parser = argparse.ArgumentParser(description="Cerebro Change Engine")
    sub = parser.add_subparsers(dest="command", required=True)

    verify = sub.add_parser("verify-capsule")
    verify.add_argument("--capsule-root", required=True)

    test = sub.add_parser("test")
    test.add_argument("--capsule-root", required=True)
    test.add_argument("--repository-root", required=True)
    test.add_argument("--profile", choices=sorted(PROFILE_RUNS), required=True)
    test.add_argument("--knowledge")
    test.add_argument("--report")

    sub.add_parser("selftest")
    args = parser.parse_args()

    try:
        if args.command == "selftest":
            report = selftest()
            print(json.dumps(report, indent=2))
            return 0 if report["result"] == "PASS" else 1

        capsule_root = Path(args.capsule_root).resolve()
        manifest = load_json(capsule_root / "capsule.json")
        validate_manifest(manifest)
        verify_payloads(capsule_root, manifest)
        if args.command == "verify-capsule":
            print(json.dumps({"result": "PASS", "classification": "CAPSULE_VERIFIED", "change_id": manifest["change"]["id"]}, indent=2))
            return 0

        report = execute_campaigns(
            capsule_root,
            Path(args.repository_root).resolve(),
            manifest,
            args.profile,
            Path(args.knowledge).resolve() if args.knowledge else None,
            Path(args.report).resolve() if args.report else None,
        )
        print(json.dumps(report, indent=2))
        return 0 if report["stability_gate"] == "PASS" else 2
    except ChangeError as exc:
        print(json.dumps({"result": "FAIL", "classification": exc.classification, "detail": exc.detail}, indent=2))
        return 1
    except Exception as exc:
        print(json.dumps({"result": "FAIL", "classification": "UNEXPECTED_EXCEPTION", "detail": repr(exc)}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
