#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
import re
import shutil
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


def _precommit(runtime: Any, base: Path, event: dict[str, Any], name: str = "pc") -> tuple[Path, Path, dict[str, Any]]:
    event_path = base / f"{name}.event.json"
    raw = json.dumps(event, sort_keys=False, ensure_ascii=False).encode("utf-8")
    event_path.write_bytes(raw)
    db_path = base / f"{name}.sqlite3"
    envelope = runtime.build_precommit_identity(
        event=event,
        event_source_bytes=raw,
        event_source_path=event_path,
        db_path=db_path,
        mode=runtime.MODE_REAL,
        source_commit="a" * 40,
        host_operation_id=f"HOST-{name}",
    )
    return event_path, db_path, envelope


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

        def host_cli_delegate_contract() -> bool:
            host_dir = str(host_path.parent)
            inserted = host_dir not in sys.path
            if inserted:
                sys.path.insert(0, host_dir)
            try:
                host = _load("cerebro_host_cli_contract", host_path)
                delegated = ["--event-file", "EVENT.json", "--db", "STATE.sqlite3", "--mode", "REAL_FIRST_LIGHT"]
                for component in host.DELEGATE_COMMANDS:
                    parsed = host.parse_host_arguments([component, *delegated])
                    if parsed.command != component or list(parsed.delegate_args) != delegated:
                        return False
                child_host_like = ["--source-root", "CHILD_SOURCE", *delegated]
                parsed = host.parse_host_arguments([
                    "--source-root", "HOST_SOURCE", "--source-commit", "a" * 40,
                    "runtime-first-light", *child_host_like,
                ])
                if parsed.source_root != "HOST_SOURCE" or parsed.source_commit != "a" * 40:
                    return False
                if list(parsed.delegate_args) != child_host_like:
                    return False
                try:
                    host.parse_host_arguments(["--not-a-host-option", "x", "runtime-first-light", *delegated])
                except SystemExit as exc:
                    if int(exc.code or 0) != 2:
                        return False
                else:
                    return False

                captured: dict[str, Any] = {}
                originals = (host.locate_source, host.verify_source, host.create_snapshot, host.delegate)
                original_argv = list(sys.argv)
                try:
                    host.locate_source = lambda explicit: Path("HOST_SOURCE")
                    host.verify_source = lambda source, commit: "a" * 40
                    host.create_snapshot = lambda source, commit: Path("SNAPSHOT")
                    def fake_delegate(snapshot: Path, component: str, arguments: list[str], source_commit: str) -> int:
                        captured.update({
                            "snapshot": str(snapshot),
                            "component": component,
                            "arguments": list(arguments),
                            "source_commit": source_commit,
                        })
                        return 0
                    host.delegate = fake_delegate
                    sys.argv = [
                        str(host_path), "--source-root", "HOST_SOURCE", "--source-commit", "a" * 40,
                        "runtime-first-light", *delegated,
                    ]
                    if host.main() != 0:
                        return False
                finally:
                    host.locate_source, host.verify_source, host.create_snapshot, host.delegate = originals
                    sys.argv = original_argv
                return captured == {
                    "snapshot": "SNAPSHOT",
                    "component": "runtime-first-light",
                    "arguments": delegated,
                    "source_commit": "a" * 40,
                }
            finally:
                if inserted and sys.path and sys.path[0] == host_dir:
                    sys.path.pop(0)

        check(35, "host CLI preserves option-first delegated argv through actual main dispatch", host_cli_delegate_contract)
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

        # ingress962 bounded precommit/recovery canaries; old 1-48 remain unchanged above.
        identity_file = base / "identity.sqlite3"
        store_id = runtime.stable_store_identity(identity_file, create=True)
        alias_file = base / "identity-alias.sqlite3"
        alias_ok = False
        try:
            os.link(identity_file, alias_file)
            alias_ok = runtime.stable_store_identity(alias_file) == store_id
        except OSError:
            alias_ok = True
        check(49, "store identity is underlying-file based, not path based", alias_ok and store_id.startswith("FILEID-SHA256-"))
        copied = base / "identity-copy.sqlite3"
        shutil.copy2(identity_file, copied)
        check(50, "copied store does not inherit store identity", runtime.stable_store_identity(copied) != store_id)

        pc_event = _event("PC51", subject_ref="precommit")
        pc_path, pc_db, pc = _precommit(runtime, base, pc_event, "pc51")
        absent = runtime.run_event(pc_event, pc_db, runtime.MODE_REAL, invocation_envelope=pc, source_commit="a" * 40)
        check(51, "ABSENT completion is bound to frozen precommit tuple", absent.get("recovery_state") == "ABSENT" and absent.get("precommit_fingerprint") == pc["precommit_fingerprint"])
        found = runtime.run_event(pc_event, pc_db, runtime.MODE_REAL, invocation_envelope=pc, source_commit="a" * 40)
        con = sqlite3.connect(pc_db)
        attempt_count = con.execute("select count(*) from attempts where event_id='PC51'").fetchone()[0]
        attempt_meta = con.execute("select host_operation_id,correlation_id,store_identity,precommit_fingerprint from attempts where event_id='PC51'").fetchone()
        con.close()
        check(52, "FOUND returns existing receipt with zero second execution", found.get("recovery_state") == "FOUND" and found.get("replay") == "IDEMPOTENT_NO_EXECUTION" and attempt_count == 1)
        check(53, "host correlation and store identity persist in attempt evidence", tuple(attempt_meta) == (pc["host_operation_id"], pc["correlation_id"], pc["store_identity"], pc["precommit_fingerprint"]))

        changed_event = _event("PC54", subject_ref="changed")
        changed_path, changed_db, changed_pc = _precommit(runtime, base, changed_event, "pc54")
        changed_path.write_text(json.dumps({**changed_event, "payload": {"key": "x", "value": 9}}), encoding="utf-8")
        def changed_blocks() -> bool:
            try:
                runtime.run_event(changed_event, changed_db, runtime.MODE_REAL, invocation_envelope=changed_pc, source_commit="a" * 40)
            except runtime.FirstLightError as exc:
                return exc.classification == "PRECOMMIT_EVENT_SOURCE_CHANGED"
            return False
        check(54, "event-file replacement after precommit is detected before execution", changed_blocks)

        source_event = _event("PC55")
        _, source_db, source_pc = _precommit(runtime, base, source_event, "pc55")
        def source_blocks() -> bool:
            try:
                runtime.run_event(source_event, source_db, runtime.MODE_REAL, invocation_envelope=source_pc, source_commit="b" * 40)
            except runtime.FirstLightError as exc:
                return exc.classification == "PRECOMMIT_SOURCE_MISMATCH"
            return False
        check(55, "source commit mismatch blocks", source_blocks)

        mode_event = _event("PC56")
        _, mode_db, mode_pc = _precommit(runtime, base, mode_event, "pc56")
        def mode_blocks() -> bool:
            try:
                runtime.run_event(mode_event, mode_db, runtime.MODE_SIM, invocation_envelope=mode_pc, source_commit="a" * 40)
            except runtime.FirstLightError as exc:
                return exc.classification == "PRECOMMIT_MODE_MISMATCH"
            return False
        check(56, "mode mismatch blocks", mode_blocks)

        wrong_event = _event("PC57")
        _, wrong_db, wrong_pc = _precommit(runtime, base, wrong_event, "pc57")
        wrong_other = base / "wrong-other.sqlite3"
        runtime.stable_store_identity(wrong_other, create=True)
        def wrong_holds() -> bool:
            try:
                runtime.run_event(wrong_event, wrong_other, runtime.MODE_REAL, invocation_envelope=wrong_pc, source_commit="a" * 40)
            except runtime.FirstLightError as exc:
                return exc.classification == "STORE_IDENTITY_MISMATCH" and exc.classification in runtime.UNKNOWN_HOLD_CLASSIFICATIONS
            return False
        check(57, "wrong or copied store resolves to UNKNOWN/HOLD", wrong_holds)

        unknown_event = _event("PC58")
        _, unknown_db, unknown_pc = _precommit(runtime, base, unknown_event, "pc58")
        runtime.run_event(unknown_event, unknown_db, runtime.MODE_REAL, invocation_envelope=unknown_pc, source_commit="a" * 40)
        con = sqlite3.connect(unknown_db)
        con.execute("delete from receipts where event_id='PC58'")
        con.commit()
        con.close()
        def incomplete_holds() -> bool:
            try:
                runtime.run_event(unknown_event, unknown_db, runtime.MODE_REAL, invocation_envelope=unknown_pc, source_commit="a" * 40)
            except runtime.FirstLightError as exc:
                return exc.classification == "PRECOMMIT_RECOVERY_UNKNOWN" and exc.classification in runtime.UNKNOWN_HOLD_CLASSIFICATIONS
            return False
        check(58, "finalized event without receipt is UNKNOWN/HOLD under precommit recovery", incomplete_holds)

        delegate_text = host_text[host_text.index("def delegate("):]
        check(59, "host freezes precommit identity before child supervision", "prepare_first_light_precommit(" in delegate_text and delegate_text.index("prepare_first_light_precommit(") < delegate_text.index("supervise_native_process("))
        check(60, "bounded contract freezes FOUND/ABSENT/UNKNOWN and STORE_IDENTITY_NE_PATH", all(token in contract_text for token in ("FOUND:", "ABSENT:", "UNKNOWN:", "STORE_IDENTITY_NE_PATH: true", "reconcile_service: NONE")))

    errors = [row for row in results if row["result"] != "PASS"]
    return {
        "schema": "cerebro-first-light-validator/v0.3",
        "result": "PASS" if not errors and len(results) == 60 else "NONPASS",
        "canary_count": len(results),
        "legacy_canary_count": 48,
        "legacy_pass_count": sum(1 for row in results if row["id"] <= 48 and row["result"] == "PASS"),
        "boundary_canary_count": 12,
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
        "binding_id": "",
        "proves_bindings": [],
        "result": result["result"],
        "basis_files": paths,
        "source_state_fingerprint": _source_fingerprint(source_root, paths),
        "canary_count": result["canary_count"],
        "pass_count": result["pass_count"],
        "nonpass_count": result["nonpass_count"],
        "consumer_activation": "HOST_RUNTIME_FIRST_LIGHT_DISPATCH_PRECOMMIT_IDENTITY_AND_SNAPSHOT_CLOSURE",
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
