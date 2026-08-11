#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

SCHEMA = "cerebro-aa003-shadow-validation/v0.1"
SUITE_SCHEMA = "cerebro-adaptive-control-shadow-scenarios/v0.1"
VALIDATOR_VERSION = "0.1.0"
DEFAULT_SCENARIO_REL = Path("tooling/change/adaptive-control-shadow-scenarios.json")

sys.dont_write_bytecode = True


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def git_blob_sha(path: Path) -> str:
    data = path.read_bytes()
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


def git_head(root: Path) -> str | None:
    try:
        cp = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if cp.returncode != 0:
        return None
    value = cp.stdout.strip()
    return value if len(value) == 40 else None


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"json-object-required:{path}")
    return value


def load_resolver(path: Path):
    spec = importlib.util.spec_from_file_location("cerebro_aa001_shadow_subject", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("resolver-import-spec-failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "resolve"):
        raise RuntimeError("resolver-missing-resolve")
    return module


def semantic_projection(result: dict[str, Any]) -> dict[str, Any]:
    decision = result.get("mcp_control_decision", {})
    profile = result.get("execution_profile", {})
    caps = result.get("capability_resolution", {})
    projected_caps: dict[str, Any] = {}
    if isinstance(caps, dict):
        for key, value in sorted(caps.items()):
            if not isinstance(value, dict):
                continue
            projected_caps[key] = {
                "state": value.get("state"),
                "action": value.get("action"),
                "selected": value.get("selected"),
            }
    efficiency = profile.get("efficiency", {}) if isinstance(profile, dict) else {}
    return {
        "outcome": decision.get("outcome"),
        "human_boundary": decision.get("human_boundary"),
        "analysis_depth": profile.get("analysis_depth"),
        "verification_depth": profile.get("verification_depth"),
        "continuation_effect": result.get("continuation_effect"),
        "capabilities": projected_caps,
        "efficiency_bias": efficiency.get("efficiency_bias") if isinstance(efficiency, dict) else None,
        "mandatory_assurance_effect": efficiency.get("mandatory_assurance_effect") if isinstance(efficiency, dict) else None,
        "live_control_authority": result.get("live_control_authority"),
    }


def subset_mismatches(expected: Any, actual: Any, path: str = "") -> list[str]:
    mismatches: list[str] = []
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{path or '$'}:expected-object:actual={type(actual).__name__}"]
        for key, value in expected.items():
            child = f"{path}.{key}" if path else key
            if key not in actual:
                mismatches.append(f"{child}:missing")
            else:
                mismatches.extend(subset_mismatches(value, actual[key], child))
        return mismatches
    if expected != actual:
        mismatches.append(f"{path}:expected={expected!r}:actual={actual!r}")
    return mismatches


def scenario_steps(scenario: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(scenario.get("steps"), list):
        return [step for step in scenario["steps"] if isinstance(step, dict)]
    return [{
        "id": "SINGLE",
        "request": scenario.get("request", {}),
        "expected": scenario.get("expected", {}),
    }]


def evaluate_once(resolver, suite: dict[str, Any]) -> dict[str, Any]:
    scenario_reports: list[dict[str, Any]] = []
    semantic_run: list[dict[str, Any]] = []
    metric_hits: dict[str, int] = {name: 0 for name in suite.get("hard_metrics", [])}
    for scenario in suite.get("scenarios", []):
        if not isinstance(scenario, dict):
            continue
        sid = str(scenario.get("id", "UNKNOWN"))
        metrics = [str(x) for x in scenario.get("metric_on_mismatch", ["hard_oracle_mismatches"])]
        step_reports: list[dict[str, Any]] = []
        for step in scenario_steps(scenario):
            step_id = str(step.get("id", "STEP"))
            request = step.get("request", {})
            expected = step.get("expected", {})
            if not isinstance(request, dict) or not isinstance(expected, dict):
                raise ValueError(f"invalid-scenario-step:{sid}:{step_id}")
            result = resolver.resolve(request)
            projection = semantic_projection(result)
            mismatches = subset_mismatches(expected, projection)
            if result.get("live_control_authority") is not False:
                mismatches.append("live_control_authority:must-be-false")
                metric_hits["live_control_observations"] = metric_hits.get("live_control_observations", 0) + 1
            if mismatches:
                for metric in metrics:
                    metric_hits[metric] = metric_hits.get(metric, 0) + 1
            step_reports.append({
                "step_id": step_id,
                "result": "PASS" if not mismatches else "FAIL",
                "expected": expected,
                "projection": projection,
                "mismatches": mismatches,
            })
            semantic_run.append({"scenario_id": sid, "step_id": step_id, "projection": projection})
        scenario_reports.append({
            "scenario_id": sid,
            "result": "PASS" if all(x["result"] == "PASS" for x in step_reports) else "FAIL",
            "steps": step_reports,
        })
    return {
        "scenario_reports": scenario_reports,
        "semantic_run": semantic_run,
        "metric_hits": metric_hits,
    }


def merge_metric_max(target: dict[str, int], source: dict[str, int]) -> None:
    for key, value in source.items():
        target[key] = max(target.get(key, 0), int(value))


def validate(source_root: Path, scenario_path: Path, runs: int | None = None) -> dict[str, Any]:
    suite = load_json(scenario_path)
    if suite.get("schema") != SUITE_SCHEMA:
        raise ValueError("shadow-suite-schema-invalid")
    subject = suite.get("subject")
    if not isinstance(subject, dict):
        raise ValueError("shadow-suite-subject-missing")

    resolver_path = source_root / str(subject.get("resolver_path", ""))
    contract_path = source_root / str(subject.get("contract_path", ""))
    expected_resolver_blob = str(subject.get("resolver_git_blob_sha", ""))
    expected_contract_blob = str(subject.get("contract_git_blob_sha", ""))
    expected_source_commit = str(subject.get("source_commit", ""))

    identity = {
        "expected_source_commit": expected_source_commit,
        "observed_source_commit": git_head(source_root),
        "resolver_git_blob_sha": git_blob_sha(resolver_path) if resolver_path.is_file() else None,
        "expected_resolver_git_blob_sha": expected_resolver_blob,
        "contract_git_blob_sha": git_blob_sha(contract_path) if contract_path.is_file() else None,
        "expected_contract_git_blob_sha": expected_contract_blob,
        "scenario_catalog_sha256": sha256_file(scenario_path),
    }
    identity_mismatches: list[str] = []
    if identity["resolver_git_blob_sha"] != expected_resolver_blob:
        identity_mismatches.append("resolver-git-blob-mismatch")
    if identity["contract_git_blob_sha"] != expected_contract_blob:
        identity_mismatches.append("contract-git-blob-mismatch")
    if identity["observed_source_commit"] is not None and identity["observed_source_commit"] != expected_source_commit:
        identity_mismatches.append("source-commit-mismatch")

    metric_totals: dict[str, int] = {str(name): 0 for name in suite.get("hard_metrics", [])}
    if identity_mismatches:
        metric_totals["candidate_identity_drift"] = len(identity_mismatches)

    resolver = load_resolver(resolver_path) if not identity_mismatches else None
    run_count = int(runs or suite.get("determinism_runs") or 3)
    if run_count < 2:
        raise ValueError("shadow-runs-must-be-at-least-2")

    runs_out: list[dict[str, Any]] = []
    semantic_fingerprints: list[str] = []
    if resolver is not None:
        for index in range(run_count):
            once = evaluate_once(resolver, suite)
            semantic_fingerprint = sha256_bytes(canonical(once["semantic_run"]).encode("utf-8"))
            semantic_fingerprints.append(semantic_fingerprint)
            merge_metric_max(metric_totals, once["metric_hits"])
            runs_out.append({
                "run": index + 1,
                "semantic_fingerprint": semantic_fingerprint,
                "scenario_reports": once["scenario_reports"],
            })

    if semantic_fingerprints and len(set(semantic_fingerprints)) != 1:
        metric_totals["semantic_nondeterminism"] = len(set(semantic_fingerprints)) - 1

    hard_failures = {k: v for k, v in metric_totals.items() if int(v) > 0}
    scenario_count = len([x for x in suite.get("scenarios", []) if isinstance(x, dict)])
    step_count = sum(len(scenario_steps(x)) for x in suite.get("scenarios", []) if isinstance(x, dict))
    fingerprint_material = {
        "subject": identity,
        "suite_sha256": identity["scenario_catalog_sha256"],
        "validator_version": VALIDATOR_VERSION,
    }
    report = {
        "schema": SCHEMA,
        "result": "PASS" if not hard_failures else "FAIL",
        "authority": "EVIDENCE_ONLY",
        "patch_ref": "PATCH-AA-003",
        "subject_patch_ref": "PATCH-AA-001",
        "validator_version": VALIDATOR_VERSION,
        "live_control_authority": False,
        "candidate_modified": False,
        "promotion_authority": "NONE",
        "shadow_validation_required_before_promotion": True,
        "identity": identity,
        "identity_mismatches": identity_mismatches,
        "scenario_count": scenario_count,
        "step_count": step_count,
        "shadow_run_count": run_count,
        "semantic_fingerprints": semantic_fingerprints,
        "metrics": metric_totals,
        "hard_failures": hard_failures,
        "runs": runs_out,
        "source_state_fingerprint": sha256_bytes(canonical(fingerprint_material).encode("utf-8")),
    }
    return report


def emit(report: dict[str, Any], output: str | None) -> int:
    text = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if output:
        out = Path(output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0 if report.get("result") == "PASS" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Cerebro AA-003 adaptive control shadow validator")
    parser.add_argument("command", nargs="?", choices=["shadow", "activation-probe"], default="shadow")
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--scenario-path")
    parser.add_argument("--runs", type=int)
    parser.add_argument("--output")
    args = parser.parse_args()

    source_root = Path(args.source_root).resolve()
    scenario_path = Path(args.scenario_path).resolve() if args.scenario_path else source_root / DEFAULT_SCENARIO_REL
    report = validate(source_root, scenario_path, args.runs)
    return emit(report, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
