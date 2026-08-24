#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

PROOF_SCHEMA = "cerebro-first-light-activation-proof/v1"
sys.dont_write_bytecode = True


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("module-load-failed:" + str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_fingerprint(root: Path, paths: list[str]) -> str:
    rows = [f"{p}|{_sha256(root / p)}" for p in sorted(paths)]
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def _event(event_id: str = "E1", **updates: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "event_id": event_id,
        "event_type": "SET",
        "subject_ref": "demo",
        "intended_consequence_class": "LOCAL_EVIDENCE_ONLY",
        "capabilities": ["LOCAL_EVIDENCE"],
        "payload": {"key": "x", "value": 1},
    }
    value.update(updates)
    return value


def _blocks(runtime: Any, event: dict[str, Any], db: Path, classification: str, mode: str | None = None) -> bool:
    try:
        runtime.run_event(event, db, mode or runtime.MODE_REAL)
    except runtime.FirstLightError as exc:
        return exc.classification == classification
    return False


def _sim_blocks(temporalis: Any, runtime: Any, scenario: dict[str, Any], root: Path, classification: str) -> bool:
    try:
        temporalis.run_scenario(scenario, root)
    except runtime.FirstLightError as exc:
        return exc.classification == classification
    return False


def run_canaries(root: Path) -> dict[str, Any]:
    runtime_path = root / "tooling/runtime-host/first_light_runtime.py"
    temporalis_path = root / "tooling/runtime-host/temporalis.py"
    contract_path = root / "tooling/runtime-host/first-light-contract.yaml"
    component_path = root / "tooling/runtime-host/component.yaml"
    host_path = root / "tooling/host/cerebro_host.py"
    paths = (runtime_path, temporalis_path, contract_path, component_path, host_path)
    missing = [f"MISSING:{path.relative_to(root)}" for path in paths if not path.is_file()]
    if missing:
        return {"result": "NONPASS", "errors": missing, "canaries": []}

    for path in (runtime_path, temporalis_path, host_path):
        compile(path.read_text(encoding="utf-8-sig"), str(path), "exec")
    runtime = _load("first_light_runtime", runtime_path)
    temporalis = _load("temporalis", temporalis_path)
    runtime_text = runtime_path.read_text(encoding="utf-8")
    temporalis_text = temporalis_path.read_text(encoding="utf-8")
    contract_text = contract_path.read_text(encoding="utf-8")
    host_text = host_path.read_text(encoding="utf-8-sig")
    host_tree = ast.parse(host_text)
    host_strings = [node.value for node in ast.walk(host_tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)]

    results: list[dict[str, Any]] = []

    def check(cid: int, name: str, test: bool | Callable[[], bool], detail: str = "") -> None:
        try:
            passed = bool(test() if callable(test) else test)
            observed = detail
        except Exception as exc:
            passed = False
            observed = f"{type(exc).__name__}:{exc}"
        results.append({"id": cid, "name": name, "result": "PASS" if passed else "NONPASS", "detail": observed})

    with tempfile.TemporaryDirectory(prefix="cerebro-fl-") as temp_value:
        base = Path(temp_value)
        event = _event()
        check(1, "same semantic event -> same canonical fingerprint", runtime.event_fingerprint(event) == runtime.event_fingerprint(dict(event)))

        db = base / "first.sqlite3"
        receipt = runtime.run_event(event, db, runtime.MODE_REAL)
        replay = runtime.run_event(event, db, runtime.MODE_REAL)
        collision = _event(payload={"key": "x", "value": 2})
        check(2, "same event_id/different fingerprint -> BLOCK", _blocks(runtime, collision, db, "EVENT_ID_FINGERPRINT_COLLISION"))
        def replay_is_single() -> bool:
            con = sqlite3.connect(db)
            try:
                count = con.execute("select count(*) from attempts").fetchone()[0]
            finally:
                con.close()
            return replay.get("replay") == "IDEMPOTENT_NO_EXECUTION" and count == 1
        check(3, "finalized replay performs no second execution", replay_is_single)
        check(4, "undeclared event type -> BLOCK", _blocks(runtime, _event("E4", event_type="UNDECLARED"), base / "t4.sqlite3", "EVENT_TYPE_UNDECLARED"))
        check(5, "denied material capability -> BLOCK", _blocks(runtime, _event("E5", capabilities=["MATERIAL_EFFECT"]), base / "t5.sqlite3", "CAPABILITY_DENIED"))
        check(6, "fixture cannot authorize external/material effect", receipt["material_effect"] is False)
        check(7, "MCP decision absent leaves evidence-only authority", receipt["authority"] == "EVIDENCE_ONLY")
        stale = runtime.run_event(_event("E8", mcp_decision={"id": "STALE"}), base / "t8.sqlite3", runtime.MODE_REAL)
        check(8, "stale/unrecognized MCP field cannot grant material effect", stale["material_effect"] is False and stale["authority"] == "EVIDENCE_ONLY")

        r9a = runtime.run_event(_event("E9"), base / "t9a.sqlite3", runtime.MODE_REAL)
        r9b = runtime.run_event(_event("E9"), base / "t9b.sqlite3", runtime.MODE_REAL)
        check(9, "same reducer input -> same semantic state", r9a["state_fingerprint"] == r9b["state_fingerprint"])
        obs_a = _event("E10", observation_metadata={"wall": "A"})
        obs_b = _event("E10", observation_metadata={"wall": "B"})
        check(10, "wall observation excluded from semantic identity", runtime.event_fingerprint(obs_a) == runtime.event_fingerprint(obs_b))

        tx_db = base / "tx.sqlite3"
        store = runtime.FirstLightStore(tx_db)
        try:
            try:
                store.process(_event("E11"), mode=runtime.MODE_REAL, fault_injection="AFTER_EVENT_INSERT")
            except RuntimeError:
                pass
            counts = tuple(store.connection.execute(f"select count(*) from {table}").fetchone()[0] for table in ("events", "event_state", "attempts", "receipts"))
        finally:
            store.close()
        check(11, "injected commit fault leaves no partial logical commit", counts == (0, 0, 0, 0), str(counts))
        recovered = runtime.run_event(_event("E11"), tx_db, runtime.MODE_REAL)
        check(12, "post-fault manual recovery finalizes exactly once", recovered["result_class"] == "PASS" and recovered["state_revision"] == 1)

        concurrent_db = base / "concurrent.sqlite3"
        pre = runtime.FirstLightStore(concurrent_db)
        pre.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            concurrent = list(pool.map(lambda _: runtime.run_event(_event("E13"), concurrent_db, runtime.MODE_REAL), range(2)))
        check(13, "concurrent duplicate start serializes to one execution", sum(item.get("replay") == "IDEMPOTENT_NO_EXECUTION" for item in concurrent) == 1 and len({item["receipt_id"] for item in concurrent}) == 1)
        check(14, "execution and verification truth are separate", receipt["execution_truth"] != receipt["verification_truth"])

        invalid_path = base / "invalid-event.json"
        invalid_path.write_text(json.dumps(_event("E15", event_type="INVALID")), encoding="utf-8")
        cli_env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        cli = subprocess.run([sys.executable, "-B", str(runtime_path), "--event-file", str(invalid_path), "--db", str(base / "cli.sqlite3")], text=True, capture_output=True, check=False, env=cli_env)
        cli_body = json.loads(cli.stdout)
        check(15, "process exit alone cannot create semantic PASS", cli.returncode == 2 and cli_body["result_class"] == "BLOCK")
        corrupt = base / "corrupt.sqlite3"
        corrupt.write_bytes(b"not-a-sqlite-database")
        before_corrupt = _sha256(corrupt)
        valid_path = base / "valid-event.json"
        valid_path.write_text(json.dumps(_event("E16")), encoding="utf-8")
        bad_db = subprocess.run([sys.executable, "-B", str(runtime_path), "--event-file", str(valid_path), "--db", str(corrupt)], text=True, capture_output=True, check=False, env=cli_env)
        bad_body = json.loads(bad_db.stdout)
        check(16, "unavailable execution outcome remains UNKNOWN", bad_db.returncode == 3 and bad_body["result_class"] == "UNKNOWN")
        check(17, "heartbeat is liveness only", "heartbeat_proves_progress_or_success: false" in contract_text and "heartbeat" not in receipt)

        scenario = {
            "schema": "cerebro-temporalis-scenario/v0.1",
            "seed": 7,
            "events": [
                {**_event("T2", subject_ref="sim", intended_consequence_class="SIMULATION_ONLY", payload={"key": "b", "value": 2}), "logical_time": 2, "scenario_sequence": 1},
                {**_event("T1", subject_ref="sim", intended_consequence_class="SIMULATION_ONLY", payload={"key": "a", "value": 1}), "logical_time": 1, "scenario_sequence": 0},
            ],
        }
        try:
            temporalis._safe_sim_path(base / "sim-root", base / "escape")
            escaped = False
        except runtime.FirstLightError as exc:
            escaped = exc.classification == "TEMPORALIS_FILESYSTEM_ESCAPE"
        check(18, "TEMPORALIS filesystem escape -> DENY", escaped)
        for cid, key in ((19, "spawn"), (20, "network"), (21, "source_mutation")):
            bad = dict(scenario)
            bad[key] = True
            check(cid, f"TEMPORALIS {key} -> DENY", _sim_blocks(temporalis, runtime, bad, base / f"sim{cid}", "TEMPORALIS_SIDE_EFFECT_DENY"))

        sim_a = temporalis.run_scenario(scenario, base / "simA")
        sim_b = temporalis.run_scenario(scenario, base / "simB")
        check(22, "same scenario/seed -> same semantic trace", sim_a["trace_fingerprint"] == sim_b["trace_fingerprint"])
        reversed_scenario = dict(scenario)
        reversed_scenario["events"] = list(reversed(scenario["events"]))
        sim_c = temporalis.run_scenario(reversed_scenario, base / "simC")
        check(23, "logical order is independent of input order", sim_a["trace_fingerprint"] == sim_c["trace_fingerprint"])
        invalid_steps = dict(scenario)
        invalid_steps["max_steps"] = "invalid"
        check(24, "invalid TEMPORALIS bounds fail closed", _sim_blocks(temporalis, runtime, invalid_steps, base / "sim24", "TEMPORALIS_MAX_STEPS_INVALID"))
        secret_pattern = re.compile(r"(?i)(api[_-]?key|access[_-]?token|secret)\s*=")
        check(25, "embedded secret assignment absent", not secret_pattern.search(runtime_text + temporalis_text))

        recorded = _event("M26R", event_type="MODEL_RECORDED", subject_ref="model", capabilities=["LOCAL_EVIDENCE", "MODEL_RECORDED"], payload={"request": {"q": "x"}, "recorded_response": {"a": 1}})
        stub = _event("M26S", event_type="MODEL_STUB", subject_ref="model", capabilities=["LOCAL_EVIDENCE", "MODEL_STUB"], payload={"request": {"q": "x"}, "stub_response": {"a": 1}})
        model_pairs = [(recorded, "recorded"), (stub, "stub")]
        deterministic = all(runtime.run_event(item, base / f"{name}a.sqlite3", runtime.MODE_REAL)["state_fingerprint"] == runtime.run_event(item, base / f"{name}b.sqlite3", runtime.MODE_REAL)["state_fingerprint"] for item, name in model_pairs)
        check(26, "STUB and RECORDED adapters replay deterministically", deterministic)
        outage_db = base / "outage.sqlite3"
        before = runtime.run_event(_event("M27A", subject_ref="outage"), outage_db, runtime.MODE_REAL)
        denied = _blocks(runtime, _event("M27B", subject_ref="outage", event_type="MODEL_RECORDED", capabilities=["LOCAL_EVIDENCE", "MODEL_RECORDED", "NETWORK"], payload={"recorded_response": {}}), outage_db, "CAPABILITY_DENIED")
        con = sqlite3.connect(outage_db)
        after_fp = con.execute("select state_fingerprint from event_state where subject_ref='outage'").fetchone()[0]
        con.close()
        check(27, "model outage intent cannot alter committed prior state", denied and after_fp == before["state_fingerprint"])
        check(28, "local event/history store authority=EVIDENCE_ONLY", receipt["authority"] == "EVIDENCE_ONLY")
        check(29, "PM and scheduler mutation capabilities denied", {"PM_MUTATION", "SCHEDULER_MUTATION"} <= runtime.DENIED_CAPABILITIES)
        check(30, "Source mutation request is dynamically blocked", _blocks(runtime, _event("E30", capabilities=["SOURCE_MUTATION"]), base / "t30.sqlite3", "CAPABILITY_DENIED"))
        check(31, "environment/mode mismatch fails closed", _blocks(runtime, _event("E31"), base / "t31.sqlite3", "MODE_INVALID", "GOVERNED_MATERIAL"))
        check(32, "interruption contract prohibits blind retry", "blind_retry: PROHIBITED" in contract_text)
        check(33, "corrupted DB is not auto-deleted or rewritten", bad_db.returncode == 3 and corrupt.exists() and _sha256(corrupt) == before_corrupt)
        relocated_event = _event("R34")
        relocated = runtime.run_event(relocated_event, base / "relocated" / "deep" / "db.sqlite3", runtime.MODE_REAL)
        check(34, "run-root relocation preserves semantic identity", relocated["event_fingerprint"] == runtime.event_fingerprint(relocated_event))

        check(35, "host normal consumer dispatch and snapshot closure are wired", host_strings.count("runtime-first-light") >= 3 and host_strings.count("first_light_runtime.py") >= 2)
        check(36, "intended consequence is immutable in receipt lineage", receipt["intended_consequence_class"] == event["intended_consequence_class"])
        check(37, "producer scope and consequence state are independently derived", receipt["producer_scope_state"] == "SCOPE_CLOSED" and receipt["consequence_state"] == "LOCAL_EVIDENCE_ONLY" and sim_a["side_effects"] == "DENY")
        check(38, "PM admission/provider readback never proves effect", "pm_admission_proves_effect: false" in contract_text and "provider_readback_proves_effect: false" in contract_text)
        check(39, "First Light cannot self-attest or own assurance", "FIRST_LIGHT_CANNOT_SELF_ATTEST" in contract_text)
        check(40, "persistent Doctor actor/session dependency is zero", "NO_PERSISTENT_DOCTOR_ACTOR_DEPENDENCY" in contract_text)
        check(41, "affected-scope widening stays external assurance-owned", "IMMUNE_ASSURANCE_IS_EXTERNAL_INPUT_WHEN_SEPARATELY_AUTHORIZED" in contract_text)
        check(42, "prepublication rollback remains governing-delivery concern", "material_effect_authority: NONE" in contract_text)
        check(43, "postpublication quarantine remains governing-delivery concern", "runtime_authority: NONE" in contract_text and "source_authority: NONE" in contract_text)
        check(44, "shadow material path is denied", _blocks(runtime, _event("E44", capabilities=["MATERIAL_EFFECT"]), base / "t44.sqlite3", "CAPABILITY_DENIED"))
        fake_attestation = runtime.run_event(_event("E45", assurance_attestation={"result": "ALLOW"}), base / "t45.sqlite3", runtime.MODE_REAL)
        check(45, "external attestation fields are non-authorizing", fake_attestation["material_effect"] is False and fake_attestation["authority"] == "EVIDENCE_ONLY")
        succession = runtime.run_event(_event("E46", generation="SUCCESSOR"), base / "t46.sqlite3", runtime.MODE_REAL)
        check(46, "generation succession confers no authority", succession["authority"] == "EVIDENCE_ONLY")
        check(47, "receipt exposes currentness, consequence and next owner", all(receipt.get(name) for name in ("currentness_cursor", "consequence_state", "next_owner")))
        check(48, "personal-capsule domain is absent from First Light/Temporalis", "capsule" not in runtime_text.lower() and "capsule" not in temporalis_text.lower())

    errors = [row for row in results if row["result"] != "PASS"]
    return {
        "schema": "cerebro-first-light-validator/v0.2",
        "result": "PASS" if not errors and len(results) == 48 else "NONPASS",
        "canary_count": len(results),
        "pass_count": len(results) - len(errors),
        "nonpass_count": len(errors),
        "canaries": results,
    }


def activation_probe(source_root: Path, output: Path) -> int:
    result = run_canaries(source_root)
    paths = [
        "tooling/runtime-host/first_light_runtime.py",
        "tooling/runtime-host/temporalis.py",
        "tooling/runtime-host/first-light-contract.yaml",
        "tooling/validator/first_light_runtime_validation.py",
        "tooling/runtime-host/component.yaml",
        "tooling/host/cerebro_host.py",
    ]
    proof = {
        "schema": PROOF_SCHEMA,
        "result": result["result"],
        "basis_files": paths,
        "source_state_fingerprint": _source_fingerprint(source_root, paths),
        "canary_count": result["canary_count"],
        "pass_count": result["pass_count"],
        "nonpass_count": result["nonpass_count"],
        "consumer_activation": "HOST_RUNTIME_FIRST_LIGHT_DISPATCH_AND_SNAPSHOT_CLOSURE",
        "source_mutation_by_probe": False,
        "runtime_material_effect": False,
        "details": result,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(proof, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return 0 if proof["result"] == "PASS" else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    validate_parser = sub.add_parser("validate")
    validate_parser.add_argument("--candidate-root", required=True)
    activation_parser = sub.add_parser("activation-probe")
    activation_parser.add_argument("--source-root", required=True)
    activation_parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.cmd == "validate":
        result = run_canaries(Path(args.candidate_root))
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0 if result["result"] == "PASS" else 1
    return activation_probe(Path(args.source_root), Path(args.output))


if __name__ == "__main__":
    raise SystemExit(main())
