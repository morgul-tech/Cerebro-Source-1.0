#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from first_light_runtime import (
    FirstLightError,
    FirstLightStore,
    MODE_SIM,
    event_fingerprint,
    semantic_fingerprint,
)

SCHEMA = "cerebro-temporalis/v0.1"
DENIED_SCENARIO_KEYS = {
    "network", "process", "spawn", "shell", "source_mutation",
    "shared_mutation", "filesystem_external", "material_effect",
}


def _safe_sim_path(sim_root: Path, candidate: Path) -> Path:
    root = sim_root.resolve()
    value = candidate.resolve()
    try:
        value.relative_to(root)
    except ValueError as exc:
        raise FirstLightError("TEMPORALIS_FILESYSTEM_ESCAPE", str(value)) from exc
    return value


def _reject_side_effect_intent(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized in DENIED_SCENARIO_KEYS and item not in (None, False, "", [], {}):
                raise FirstLightError("TEMPORALIS_SIDE_EFFECT_DENY", f"{path}.{key}")
            _reject_side_effect_intent(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_side_effect_intent(item, f"{path}[{index}]")


def run_scenario(scenario: dict[str, Any], sim_root: Path) -> dict[str, Any]:
    if str(scenario.get("schema") or "") not in {"cerebro-temporalis-scenario/v0.1", "cerebro-temporalis-scenario/v1"}:
        raise FirstLightError("TEMPORALIS_SCENARIO_SCHEMA_INVALID")
    _reject_side_effect_intent(scenario)
    seed = scenario.get("seed", 0)
    try:
        max_steps = int(scenario.get("max_steps", 256))
    except (TypeError, ValueError) as exc:
        raise FirstLightError("TEMPORALIS_MAX_STEPS_INVALID") from exc
    if max_steps < 1 or max_steps > 10000:
        raise FirstLightError("TEMPORALIS_MAX_STEPS_INVALID")
    events = scenario.get("events")
    if not isinstance(events, list):
        raise FirstLightError("TEMPORALIS_EVENTS_INVALID")
    ordered = []
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise FirstLightError("TEMPORALIS_EVENT_INVALID", str(index))
        try:
            logical_time = int(event.get("logical_time", 0))
            sequence = int(event.get("scenario_sequence", index))
        except (TypeError, ValueError) as exc:
            raise FirstLightError("TEMPORALIS_LOGICAL_TIME_INVALID", str(index)) from exc
        item = dict(event)
        item.setdefault("event_id", f"SIM-{seed}-{sequence}")
        item.setdefault("subject_ref", "temporalis/default")
        item.setdefault("intended_consequence_class", "SIMULATION_ONLY")
        item.setdefault("capabilities", ["LOCAL_EVIDENCE"])
        ordered.append((logical_time, sequence, str(item["event_id"]), item))
    ordered.sort(key=lambda row: (row[0], row[1], row[2]))
    if len(ordered) > max_steps:
        raise FirstLightError("TEMPORALIS_STEP_LIMIT_EXCEEDED")

    sim_root.mkdir(parents=True, exist_ok=True)
    db_path = _safe_sim_path(sim_root, sim_root / "temporalis.sqlite3")
    store = FirstLightStore(db_path)
    trace = []
    try:
        for logical_time, sequence, _, event in ordered:
            receipt = store.process(
                event,
                mode=MODE_SIM,
                currentness_cursor=f"LOGICAL:{logical_time}:{sequence}",
            )
            trace.append({
                "logical_time": logical_time,
                "scenario_sequence": sequence,
                "event_id": event["event_id"],
                "event_fingerprint": event_fingerprint(event),
                "receipt_fingerprint": receipt["receipt_fingerprint"],
                "state_fingerprint": receipt["state_fingerprint"],
                "result_class": receipt["result_class"],
            })
    finally:
        store.close()
    semantic = {
        "schema": SCHEMA,
        "seed": seed,
        "side_effects": "DENY",
        "trace": trace,
    }
    return {
        **semantic,
        "trace_fingerprint": semantic_fingerprint(semantic),
        "material_effect": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Cerebro TEMPORALIS deterministic simulation")
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--sim-root", required=True)
    args = parser.parse_args()
    try:
        scenario = json.loads(Path(args.scenario).read_text(encoding="utf-8"))
        result = run_scenario(scenario, Path(args.sim_root))
        print(json.dumps(result, sort_keys=True, indent=2, ensure_ascii=False))
        return 0
    except FirstLightError as exc:
        print(json.dumps({
            "schema": SCHEMA,
            "result": "BLOCK",
            "classification": exc.classification,
            "detail": exc.detail,
            "side_effects": "DENY",
            "material_effect": False,
        }, sort_keys=True, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
