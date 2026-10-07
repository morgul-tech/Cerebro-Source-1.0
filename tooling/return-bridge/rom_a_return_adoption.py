#!/usr/bin/env python3
"""One deterministic Rom A task/return/continuation selection inside an existing turn.

The caller supplies a current normal-task bundle and owns provider currentness,
semantic review, worklist writing and any later actor action. This adapter reads
local bytes, reuses BK04/BK05 as-is, and emits one non-authoritative receipt.
It never wakes a model, grants authority, or performs a provider operation.

The normal return invocation takes an existing PR73 capture and a typed return
observation through --capture-record/--return-input. --bundle remains the
legacy local test/diagnostic form and does not establish recipient use.

Bundle fields: schema; task {actor_ref, generation_ref, carrier_ref, arc_ref,
task_ref, task_revision, original_path,
original_sha256, source_head, effect_class, privacy_class, live_scope,
authority_class, return_target, way_home, allowed_paths, required_invariants,
stop_edges}; return {actor_ref, generation_ref, carrier_ref, arc_ref,
task_ref, task_revision, original_sha256,
contract_kind}; optional bk04 {owner_facts_path, binding_path}; optional bk05
{parent_manifest_path, verifier_path, verifier_delta_path, currentness_path,
prior_record_path}. All paths refer to existing local files. Only
contract_kind=BK04_OWNER_FACTS or FACT_RETURN makes BK04 relevant. Example invocation:
python -B tooling/return-bridge/rom_a_return_adoption.py --bundle BUNDLE.json
    --out-dir NEW_OUTPUT_DIRECTORY
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any


SOURCE_ROOT = Path(__file__).resolve().parents[2]
SCHEMA = "cerebro-rom-a-return-adoption/v1"
MAX_INPUT_BYTES = 2_000_000
FACT_RETURN_KINDS = frozenset({"BK04_OWNER_FACTS", "FACT_RETURN"})


def _load_module(name: str, path: Path) -> Any:
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError("QUALIFIED_MODULE_UNAVAILABLE")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read(reference: Any) -> bytes:
    if not isinstance(reference, str) or not reference:
        raise ValueError("INPUT_REFERENCE_MISSING")
    path = Path(reference)
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise ValueError("INPUT_REFERENCE_UNAVAILABLE")
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError("INPUT_TOO_LARGE")
    return path.read_bytes()


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(name + "_OBJECT_REQUIRED")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(name + "_REQUIRED")
    return value


def evaluate(bundle: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
    """Select existing BK05 capsule or the exact original; BK04 never stops unrelated work."""
    if bundle.get("schema") != SCHEMA:
        raise ValueError("BUNDLE_SCHEMA_MISMATCH")
    task = _object(bundle.get("task"), "TASK")
    returned = _object(bundle.get("return"), "RETURN")
    actor = _text(task.get("actor_ref"), "ACTOR")
    generation = _text(task.get("generation_ref"), "GENERATION")
    carrier = _text(task.get("carrier_ref"), "CARRIER")
    arc = _text(task.get("arc_ref"), "ARC")
    task_ref = _text(task.get("task_ref"), "TASK_REF")
    task_revision = _text(task.get("task_revision"), "TASK_REVISION")
    original = _read(task.get("original_path"))
    if _sha(original) != task.get("original_sha256"):
        raise ValueError("ORIGINAL_HASH_MISMATCH")

    bk04_status: dict[str, Any] = {"state": "SKIPPED_NOT_ELIGIBLE", "blocks_other_work": False}
    bk04_input = bundle.get("bk04")
    if returned.get("contract_kind") in FACT_RETURN_KINDS and bk04_input is not None:
        bk04_input = _object(bk04_input, "BK04")
        if bk04_input.get("owner_facts_path") and bk04_input.get("binding_path"):
            bk04 = _load_module("bk04_verify", SOURCE_ROOT / "candidates/bk04-episode-verifier-v1/bk04_verify.py")
            try:
                binding = json.loads(_read(bk04_input["binding_path"]))
                parsed, digest = bk04.load_consumer_input(_read(bk04_input["owner_facts_path"]), binding)
                finding = bk04.classify_document(parsed, digest)
                bk04_status = {"state": finding.get("overall_status", "UNKNOWN"),
                               "finding": finding, "blocks_other_work": False}
            except (bk04.InputError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
                bk04_status = {"state": "UNKNOWN", "reason": type(exc).__name__,
                               "blocks_other_work": False}
        else:
            bk04_status = {"state": "UNKNOWN", "reason": "OWNER_FACTS_OR_BINDING_MISSING",
                           "blocks_other_work": False}
    elif returned.get("contract_kind") in FACT_RETURN_KINDS:
        normal_facts = bundle.get("bk04_normal_facts")
        if isinstance(normal_facts, dict):
            bk04 = _load_module("bk04_verify_normal_return", SOURCE_ROOT / "candidates/bk04-episode-verifier-v1/bk04_verify.py")
            try:
                finding = bk04.classify_normal_return_facts(normal_facts)
                bk04_status = {"state": finding["overall_status"], "finding": finding,
                               "blocks_other_work": False}
            except bk04.InputError as exc:
                bk04_status = {"state": "UNKNOWN", "reason": exc.code,
                               "blocks_other_work": False}
        else:
            bk04_status = {"state": "UNKNOWN", "reason": "OWNER_FACTS_OR_BINDING_MISSING",
                           "blocks_other_work": False}

    selection = "FULL_ORIGINAL_TASK"
    bk05_status: dict[str, Any] = {"decision": "NOT_ELIGIBLE", "reasons": ["VALID_DELTA_NOT_SUPPLIED"]}
    payload = original
    returned_actor = returned.get("actor_ref")
    returned_arc = returned.get("arc_ref")
    if bk04_status["state"] == "CONFLICT":
        bk05_status = {"decision": "FULL_TASK_REQUIRED", "reasons": ["BK04_CONFLICT_LOCAL_REVIEW"]}
    elif (returned_actor != actor or returned.get("generation_ref") != generation or
            returned.get("carrier_ref") != carrier or returned_arc != arc or
            returned.get("task_ref") != task_ref or
            returned.get("task_revision") != task_revision or
            returned.get("original_sha256") != _sha(original)):
        bk05_status = {"decision": "FULL_TASK_REQUIRED", "reasons": ["ACTOR_ARC_OR_ORIGINAL_CHANGED"]}
    elif isinstance(bundle.get("bk05"), dict):
        refs = bundle["bk05"]
        needed = ("parent_manifest_path", "verifier_path", "verifier_delta_path", "currentness_path")
        if all(refs.get(name) for name in needed):
            core = _load_module("bk05_core", SOURCE_ROOT / "candidates/bk05-capsule-tool-v1/src/bk05_capsule/core.py")
            try:
                manifest_raw = _read(refs["parent_manifest_path"])
                manifest = json.loads(manifest_raw)
                evaluation = core.evaluate(
                    original, manifest_raw, _read(refs["verifier_path"]),
                    _read(refs["verifier_delta_path"]), _read(refs["currentness_path"]),
                    prior=_read(refs["prior_record_path"]) if refs.get("prior_record_path") else None,
                )
                bk05_status = evaluation.decision_document()
                if evaluation.decision == core.LOCAL_CAPSULE_CANDIDATE:
                    capsule = evaluation.capsule
                    identity_fields = ("effect_class", "privacy_class", "live_scope", "authority_class",
                                       "return_target", "way_home")
                    list_fields = (("required_invariants", "required_invariants"),
                                   ("stop_edges", "stop_edges"))
                    same_scope = all(task.get(name) == capsule.get(name) for name in identity_fields)
                    same_scope = same_scope and task.get("source_head") == capsule.get("source_head")
                    same_scope = same_scope and task.get("allowed_paths") == manifest.get("allowed_paths")
                    same_scope = same_scope and all(
                        task.get(task_name) == capsule.get(capsule_name)
                        for task_name, capsule_name in list_fields)
                    same_scope = same_scope and (
                        isinstance(task.get("allowed_paths"), list)
                        and set(capsule["allowed_repair_paths"]).issubset(set(task["allowed_paths"]))
                    )
                    if (capsule["actor_ref"] == actor and capsule["parent"]["task_ref"] == task_ref
                            and capsule["parent"]["revision"] == task_revision
                            and same_scope):
                        selection = "BK05_STRUCTURAL_CAPSULE_CANDIDATE"
                        payload = core.capsule_file_bytes(capsule)
                    else:
                        bk05_status = {**bk05_status,
                                       "adapter_rejection": "TASK_IDENTITY_OR_SCOPE_MISMATCH"}
            except (OSError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
                bk05_status = {"decision": "INVALID_INPUT", "reasons": [type(exc).__name__]}
        else:
            bk05_status = {"decision": "FULL_TASK_REQUIRED", "reasons": ["BK05_INPUTS_INCOMPLETE"]}

    receipt = {
        "schema": SCHEMA + "-receipt", "task_ref": task_ref, "task_revision": task_revision,
        "actor_ref": actor,
        "generation_ref": generation, "carrier_ref": carrier, "arc_ref": arc,
        "selection": selection, "selected_sha256": _sha(payload), "selected_bytes": len(payload),
        "original_sha256": _sha(original), "bk04": bk04_status, "bk05": bk05_status,
        "return_target": task.get("return_target"), "way_home": task.get("way_home"),
        "authority": "NONE", "provider_currentness_proven": False,
        "semantic_review_proven": False, "work_consumed": False, "effect": "NONE_CLAIMED",
        "task_eligibility_is_not_same_arc_proof": True,
        "model_wake_per_subcheck": False,
    }
    return receipt, payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--bundle", help="legacy local task/return reference bundle JSON")
    source.add_argument("--capture-record", help="PR73 capture.json for normal return")
    parser.add_argument("--return-input", help="typed normal return observation JSON")
    parser.add_argument("--out-dir", required=True, help="new directory for one receipt and selected bytes")
    for name in ("owner-facts", "owner-binding", "parent-manifest", "verifier",
                 "verifier-delta", "currentness", "prior-record"):
        parser.add_argument("--" + name)
    args = parser.parse_args()
    try:
        if args.capture_record:
            if not args.return_input:
                raise ValueError("RETURN_INPUT_REQUIRED")
            normal = _load_module("rom_a_normal_path", SOURCE_ROOT /
                                  "tooling/return-bridge/rom_a_normal_path.py")
            result = normal.prepare(
                capture_record=args.capture_record, return_input=args.return_input,
                out_dir=args.out_dir, owner_facts=args.owner_facts,
                owner_binding=args.owner_binding, parent_manifest=args.parent_manifest,
                verifier=args.verifier, verifier_delta=args.verifier_delta,
                currentness=args.currentness, prior_record=args.prior_record)
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.return_input or any(getattr(args, name) is not None for name in (
                "owner_facts", "owner_binding", "parent_manifest", "verifier",
                "verifier_delta", "currentness", "prior_record")):
            raise ValueError("NORMAL_INPUTS_REQUIRE_CAPTURE_RECORD")
        bundle = json.loads(_read(args.bundle))
        receipt, payload = evaluate(_object(bundle, "BUNDLE"))
        out = Path(args.out_dir)
        if out.exists():
            raise ValueError("OUTPUT_EXISTS")
        out.mkdir(parents=True)
        name = "capsule.json" if receipt["selection"].startswith("BK05_") else "original_task.txt"
        (out / name).write_bytes(payload)
        (out / "receipt.json").write_text(json.dumps(receipt, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps({"selection": receipt["selection"], "receipt": str(out / "receipt.json"),
                          "selected": str(out / name), "authority": "NONE"}))
        return 0
    except (OSError, ValueError, TypeError) as exc:
        print(json.dumps({"result": "REFUSED", "reason": str(exc).split(":", 1)[0]}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
