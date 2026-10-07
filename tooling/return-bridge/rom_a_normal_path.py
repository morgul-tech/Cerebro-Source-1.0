#!/usr/bin/env python3
"""Run BK04/BK05 selection from an existing PR73 capture during a normal A1 return.

This is a local, non-authoritative preparation step. The caller supplies an
observed return; actual review/currentness files beside that return are bound
to the captured episode when present. The CLI never invents review,
authenticates a provider, sends work, or claims recipient use. Only the
existing normal host may dispatch selected bytes through its bound sender.

Example:
  python -B tooling/return-bridge/rom_a_normal_path.py \
    --capture-record ABS/capture.json --return-input ABS/return.json \
    --verifier ABS/verifier.txt --verifier-delta ABS/verifier_delta.json \
    --currentness ABS/currentness.json --out-dir ABS/new-result
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any


SOURCE_ROOT = Path(__file__).resolve().parents[2]
SCHEMA = "cerebro-rom-a-normal-path/v1"
RETURN_SCHEMA = "cerebro-rom-a-return-observation/v1"
MAX_INPUT_BYTES = 2_000_000
RETURN_FIELDS = frozenset({
    "actor_ref", "generation_ref", "carrier_ref", "arc_ref", "task_ref",
    "task_revision", "original_sha256", "contract_kind",
})
REPAIR_KINDS = frozenset({"REPAIR", "REFINE"})
PARENT_FIELDS = (
    "actor_ref", "effect_class", "privacy_class", "live_scope", "authority_class",
    "allowed_paths", "required_invariants", "stop_edges", "return_target",
    "way_home", "source_head",
)


def _module(name: str, path: Path) -> Any:
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError("MODULE_UNAVAILABLE")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in items:
        if key in out:
            raise ValueError("DUPLICATE_JSON_KEY")
        out[key] = value
    return out


def _json(raw: bytes) -> dict[str, Any]:
    try:
        obj = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                         parse_constant=lambda _: (_ for _ in ()).throw(ValueError("NONFINITE_JSON")))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("JSON_INVALID") from exc
    if not isinstance(obj, dict):
        raise ValueError("JSON_OBJECT_REQUIRED")
    return obj


def _read(path_text: str | None) -> tuple[Path, bytes]:
    if not isinstance(path_text, str) or not path_text:
        raise ValueError("INPUT_PATH_MISSING")
    path = Path(path_text)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError("INPUT_PATH_UNAVAILABLE")
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError("INPUT_TOO_LARGE")
    return path, path.read_bytes()


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(name + "_INVALID")
    return value


def _capture(record_path_text: str) -> tuple[dict[str, Any], Path, bytes, dict[str, Any]]:
    record_path, record_raw = _read(record_path_text)
    if record_path.name != "capture.json" or record_path.parent.is_symlink():
        raise ValueError("CAPTURE_PATH_INVALID")
    record = _json(record_raw)
    capture = _module("rom_a_dispatch_capture_normal", SOURCE_ROOT /
                      "tooling/return-bridge/rom_a_dispatch_capture.py")
    if record_raw != capture._canonical(record) + b"\n":
        raise ValueError("CAPTURE_RECORD_NOT_CANONICAL")
    task = record.get("task")
    provenance = record.get("provenance")
    review = record.get("semantic_review")
    if not isinstance(task, dict) or not isinstance(provenance, dict) or not isinstance(review, dict):
        raise ValueError("CAPTURE_RECORD_INVALID")
    if set(provenance) != {"status", "original_dispatch_ref", "assertion"} or \
            set(review) != {"status", "receipt_ref", "assertion"} or \
            provenance["assertion"] != "CALLER_SUPPLIED_NOT_VERIFIED" or \
            review["assertion"] != "CALLER_SUPPLIED_NOT_VERIFIED":
        raise ValueError("CAPTURE_ASSERTION_INVALID")
    request = {"schema": capture.SCHEMA, "task": task,
               "provenance": {k: provenance[k] for k in ("status", "original_dispatch_ref")},
               "semantic_review": {k: review[k] for k in ("status", "receipt_ref")}}
    capture._validate_request(request)
    original_path, original = _read(str(record_path.parent / "original_task.txt"))
    key = _sha(capture._canonical([task["task_ref"], task["task_revision"]]))
    expected = {
        "schema": capture.SCHEMA + "-receipt", "capture_key": key, "task": task,
        "original_sha256": _sha(original), "original_bytes": len(original),
        "provenance": provenance, "semantic_review": review,
        "identity_currentness": "CALLER_SUPPLIED_NOT_PROVIDER_VERIFIED",
        "capture_stage": "LOCAL_CAPTURE_TIMING_NOT_PROVIDER_VERIFIED",
        "publish_durability": capture.PUBLISH_DURABILITY,
        "authority": "NONE", "effect": "NONE_CLAIMED", "work_consumed": False,
        "recipient_read_or_use_proven": False,
    }
    if record != expected or record_path.parent.name != "capture-" + key:
        raise ValueError("CAPTURE_READBACK_MISMATCH")
    original.decode("utf-8", errors="strict")
    return task, original_path, original, {
        "record_path": str(record_path), "record_sha256": _sha(record_raw),
        "original_path": str(original_path), "original_sha256": _sha(original),
        "original_bytes": len(original), "publish_durability": record["publish_durability"],
        "provenance": provenance, "semantic_review": review,
    }


def _return_input(path_text: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    path, raw = _read(path_text)
    obj = _json(raw)
    if set(obj) not in ({"schema", "observation_ref", "return"},
                        {"schema", "observation_ref", "return", "facts"}) or obj["schema"] != RETURN_SCHEMA:
        raise ValueError("RETURN_SCHEMA_INVALID")
    _text(obj["observation_ref"], "RETURN_OBSERVATION_REF")
    returned = obj["return"]
    if not isinstance(returned, dict) or set(returned) != RETURN_FIELDS:
        raise ValueError("RETURN_FIELDS_INVALID")
    for field in RETURN_FIELDS:
        _text(returned[field], "RETURN_" + field.upper())
    facts = obj.get("facts")
    if facts is not None and not isinstance(facts, dict):
        raise ValueError("RETURN_FACTS_OBJECT_REQUIRED")
    return returned, {"path": str(path), "sha256": _sha(raw),
                      "bytes": len(raw), "observation_ref": obj["observation_ref"],
                      "authority": "CALLER_SUPPLIED_NOT_PROVIDER_VERIFIED"}, facts


def _parent_manifest(task: dict[str, Any], original_sha256: str,
                     supplied_path: str | None) -> tuple[bytes, str]:
    derived = {"task_ref": task["task_ref"], "parent_revision": task["task_revision"],
               "parent_sha256": original_sha256,
               **{name: task[name] for name in PARENT_FIELDS}}
    if supplied_path is None:
        manifest = {**derived, "semantic_review": {"status": "UNREVIEWED", "receipt_ref": ""}}
        return (json.dumps(manifest, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":")) + "\n").encode("utf-8"), "DERIVED_UNREVIEWED"
    _, raw = _read(supplied_path)
    manifest = _json(raw)
    if set(manifest) != set(derived) | {"semantic_review"} or \
            any(manifest.get(key) != value for key, value in derived.items()):
        raise ValueError("PARENT_MANIFEST_CAPTURE_MISMATCH")
    review = manifest["semantic_review"]
    if not isinstance(review, dict) or set(review) != {"status", "receipt_ref"}:
        raise ValueError("PARENT_REVIEW_FIELDS_INVALID")
    return raw, "SUPPLIED_REVIEW_NOT_AUTHENTICATED_HERE"


def _input_ref(path_text: str | None) -> dict[str, Any] | None:
    if path_text is None:
        return None
    path, raw = _read(path_text)
    return {"path": str(path), "sha256": _sha(raw), "bytes": len(raw)}


def _episode_inputs(task: dict[str, Any], original_sha256: str,
                    returned: dict[str, Any],
                    return_ref: dict[str, Any], supplied: dict[str, str | None]
                    ) -> tuple[dict[str, str | None], list[str]]:
    """Use only named files beside the same-episode return, never a broad search."""
    paths = dict(supplied)
    if returned["contract_kind"] not in REPAIR_KINDS or any(
        returned[name] != task[name] for name in
        ("actor_ref", "generation_ref", "carrier_ref", "arc_ref", "task_ref", "task_revision")
    ) or returned["original_sha256"] != original_sha256:
        return paths, []
    episode = Path(return_ref["path"]).parent
    discovered = []
    names = {"parent_manifest": "parent_manifest.json", "verifier": "verifier.txt",
             "verifier_delta": "verifier_delta.json", "currentness": "currentness.json"}
    for key, name in names.items():
        candidate = episode / name
        if paths[key] is None and candidate.is_file() and not candidate.is_symlink():
            paths[key] = str(candidate)
            discovered.append(key)
    return paths, discovered


def _bind_delta(raw: bytes, actor: str, verifier_raw: bytes
                ) -> tuple[bytes, list[dict[str, str]], list[str]]:
    """Complete two mechanical bindings only; never alter a reviewer-supplied value."""
    try:
        delta = _json(raw)
    except ValueError:
        return raw, [], []  # The BK05 evaluator reports the malformed input.
    expected = {"requested_actor_ref": actor, "verifier_sha256": _sha(verifier_raw)}
    sources = {"requested_actor_ref": "captured_task.actor_ref",
               "verifier_sha256": "exact_verifier_bytes"}
    repairs = []
    conflicts = []
    for field, value in expected.items():
        if field not in delta:
            delta[field] = value
            repairs.append({"field": field, "source": sources[field], "value": value})
        elif delta[field] != value:
            conflicts.append(field)
    if conflicts:
        return raw, [], conflicts
    if not repairs:
        return raw, [], []
    return (json.dumps(delta, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":")) + "\n").encode("utf-8"), repairs, []


def prepare(**kwargs: Any) -> dict[str, Any]:
    """Public file/CLI path cannot transport a reader or self-issued selection."""
    if any(key in kwargs for key in ("bounded_provider_reader", "provider_reader", "_host_selection")):
        raise ValueError("bounded-context-normal-host-binding-required")
    return _prepare(**kwargs)


def _prepare(*, capture_record: str, return_input: str, out_dir: str,
            owner_facts: str | None = None, owner_binding: str | None = None,
            parent_manifest: str | None = None, verifier: str | None = None,
            verifier_delta: str | None = None, currentness: str | None = None,
            prior_record: str | None = None,
            bounded_context: str | None = None, _host_selection: dict[str, Any] | None = None) -> dict[str, Any]:
    """Prepare one selection and receipt; never send it or assert recipient use."""
    task, original_path, original, capture_ref = _capture(capture_record)
    returned, return_ref, return_facts = _return_input(return_input)
    supplied = {"owner_facts": owner_facts, "owner_binding": owner_binding,
                "parent_manifest": parent_manifest, "verifier": verifier,
                "verifier_delta": verifier_delta, "currentness": currentness,
                "prior_record": prior_record}
    inputs, discovered = _episode_inputs(
        task, capture_ref["original_sha256"], returned, return_ref, supplied)
    owner_facts, owner_binding = inputs["owner_facts"], inputs["owner_binding"]
    parent_manifest, verifier = inputs["parent_manifest"], inputs["verifier"]
    verifier_delta, currentness = inputs["verifier_delta"], inputs["currentness"]
    prior_record = inputs["prior_record"]
    selection = None
    if bounded_context is not None:
        _, context_raw = _read(bounded_context)
        context_read = _json(context_raw)
        if not isinstance(_host_selection, dict) or any(
            context_read.get(key) != _host_selection.get(key)
            for key in ("content_ref", "selector_kind", "selector")
        ):
            raise ValueError("bounded-context-normal-host-binding-required")
        selection = _host_selection
    refs = {name: _input_ref(path) for name, path in (
        ("owner_facts", owner_facts), ("owner_binding", owner_binding),
        ("parent_manifest", parent_manifest), ("verifier", verifier),
        ("verifier_delta", verifier_delta), ("currentness", currentness),
        ("prior_record", prior_record))}
    fact_return = returned["contract_kind"] in {"BK04_OWNER_FACTS", "FACT_RETURN"}
    if return_facts is not None and not fact_return:
        raise ValueError("BK04_FACTS_KIND_MISMATCH")
    if not fact_return and (owner_facts is not None or owner_binding is not None):
        raise ValueError("OWNER_FACTS_KIND_MISMATCH")
    try:
        manifest_raw, manifest_basis = _parent_manifest(
            task, capture_ref["original_sha256"], parent_manifest)
    except ValueError:
        if "parent_manifest" not in discovered:
            raise
        parent_manifest = None
        inputs["parent_manifest"] = None
        discovered.remove("parent_manifest")
        manifest_raw, manifest_basis = _parent_manifest(
            task, capture_ref["original_sha256"], None)
    missing = []
    if fact_return and return_facts is None:
        missing.extend(name for name, value in (("owner_facts", owner_facts),
                                                 ("owner_binding", owner_binding)) if value is None)
    repair_return = returned["contract_kind"] in REPAIR_KINDS
    if repair_return:
        missing.extend(name for name, value in (("verifier", verifier),
                                                 ("verifier_delta", verifier_delta),
                                                 ("currentness", currentness)) if value is None)
        if parent_manifest is None:
            missing.append("qualified_parent_review_manifest")
    out = Path(out_dir)
    if not out.is_absolute() or out.exists() or out.is_symlink() or not out.parent.is_dir():
        raise ValueError("OUTPUT_TARGET_INVALID")
    adapter = _module("rom_a_return_adoption_normal", SOURCE_ROOT /
                      "tooling/return-bridge/rom_a_return_adoption.py")
    pending = Path(tempfile.mkdtemp(prefix=".rom-a-normal-pending-", dir=out.parent))
    try:
        manifest_path = pending / "parent_manifest.json"
        manifest_path.write_bytes(manifest_raw)
        repairs: list[dict[str, str]] = []
        delta_conflicts: list[str] = []
        delta_for_core = verifier_delta
        if repair_return and verifier is not None and verifier_delta is not None:
            _, verifier_raw = _read(verifier)
            _, delta_raw = _read(verifier_delta)
            bound_raw, repairs, delta_conflicts = _bind_delta(
                delta_raw, task["actor_ref"], verifier_raw)
            if repairs:
                bound_path = pending / "verifier_delta_bound.json"
                bound_path.write_bytes(bound_raw)
                delta_for_core = str(bound_path)
        if delta_conflicts:
            missing.append("verifier_delta_conflict")
        bundle: dict[str, Any] = {"schema": adapter.SCHEMA,
            "task": {**task, "original_path": str(original_path),
                     "original_sha256": capture_ref["original_sha256"]},
            "return": returned}
        if fact_return:
            if return_facts is not None and (owner_facts is not None or owner_binding is not None):
                raise ValueError("BK04_FACT_SOURCE_AMBIGUOUS")
            if return_facts is not None:
                bundle["bk04_normal_facts"] = {
                    "schema": "bk04-normal-return-facts/v1",
                    "task": {"task_ref": task["task_ref"], "task_revision": task["task_revision"],
                             "actor_ref": task["actor_ref"],
                             "original_sha256": capture_ref["original_sha256"],
                             "original_bytes": capture_ref["original_bytes"],
                             "original_source_ref": (capture_ref["provenance"]["original_dispatch_ref"]
                                                     or capture_ref["original_path"])},
                    "return": {"task_ref": returned["task_ref"],
                               "task_revision": returned["task_revision"],
                               "actor_ref": returned["actor_ref"],
                               "original_sha256": returned["original_sha256"],
                               "result_source_ref": return_facts.get("result_source_ref"),
                               "completed_work": return_facts.get("completed_work"),
                               "local_remainder": return_facts.get("local_remainder"),
                               "responsibility": return_facts.get("responsibility")},
                }
            if owner_facts is not None or owner_binding is not None:
                bundle["bk04"] = {"owner_facts_path": owner_facts,
                                  "binding_path": owner_binding}
        if repair_return and not delta_conflicts and all(value is not None for value in
                                 (verifier, delta_for_core, currentness)):
            bundle["bk05"] = {"parent_manifest_path": str(manifest_path),
                              "verifier_path": verifier, "verifier_delta_path": delta_for_core,
                              "currentness_path": currentness}
            if prior_record is not None:
                bundle["bk05"]["prior_record_path"] = prior_record
        adoption, payload = adapter.evaluate(bundle)
        name = "capsule.json" if adoption["selection"].startswith("BK05_") else "original_task.txt"
        selected_path = pending / name
        selected_path.write_bytes(payload)
        context_ref = None
        if selection is not None:
            context_path = pending / "context_selected.txt"
            context_path.write_text(selection["selected_text"], encoding="utf-8")
            context_ref = {key: value for key, value in selection.items() if key != "selected_text"}
            context_ref["file"] = context_path.name
        receipt = {"schema": SCHEMA + "-receipt", "capture": capture_ref,
                   "return_input": return_ref, "input_refs": refs,
                   "parent_manifest": {"basis": manifest_basis, "sha256": _sha(manifest_raw),
                                       "bytes": len(manifest_raw)},
                   "missing_inputs": missing, "episode_inputs_discovered": discovered,
                   "delta_field_repairs": repairs, "delta_field_conflicts": delta_conflicts,
                   "adoption": adoption,
                   "selected": {"file": name, "sha256": _sha(payload), "bytes": len(payload),
                                "selection": adoption["selection"],
                                "recipient_delivery_qualified": False},
                   "recipient_use": {"state": "NOT_OBSERVED", "readback_ref": None,
                                     "selected_sha256": _sha(payload)},
                   "dispatch": {"state": "NOT_SENT", "delivery_ref": None,
                                "recipient_ref": task["actor_ref"]},
                   "authority": "NONE", "provider_currentness_proven": False,
                   "semantic_review_proven": False, "effect": "NONE_CLAIMED"}
        if context_ref is not None:
            receipt["bounded_context"] = context_ref
        (pending / "receipt.json").write_text(
            json.dumps(receipt, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        os.rename(pending, out)
        return {"receipt": str(out / "receipt.json"), "selected": str(out / name),
                "selection": adoption["selection"], "selected_sha256": _sha(payload),
                "missing_inputs": missing, "recipient_use": "NOT_OBSERVED", "authority": "NONE"}
    finally:
        if pending.exists():
            shutil.rmtree(pending)


def _dispatch_selected(result: dict[str, Any], sender: Any) -> dict[str, Any]:
    """Host-owned send of exactly the selected file; acceptance is not recipient use."""
    if not callable(getattr(sender, "send_selected", None)):
        raise ValueError("SELECTED_DISPATCHER_UNBOUND")
    receipt_path, receipt_raw = _read(result["receipt"])
    receipt = _json(receipt_raw)
    selected_path, payload = _read(result["selected"])
    selected = receipt.get("selected")
    adoption = receipt.get("adoption")
    if (not isinstance(selected, dict) or not isinstance(adoption, dict)
            or selected_path.parent != receipt_path.parent
            or selected_path.name != selected.get("file")
            or _sha(payload) != selected.get("sha256")
            or receipt.get("dispatch", {}).get("state") != "NOT_SENT"):
        raise ValueError("SELECTED_DISPATCH_READBACK_MISMATCH")
    recipient = adoption["actor_ref"]
    response = sender.send_selected(
        recipient_ref=recipient, selected_bytes=payload,
        selected_sha256=selected["sha256"], selection=selected["selection"],
        task_ref=adoption["task_ref"], task_revision=adoption["task_revision"])
    if (not isinstance(response, dict) or response.get("state") != "ACCEPTED"
            or response.get("recipient_ref") != recipient
            or response.get("selected_sha256") != selected["sha256"]
            or not isinstance(response.get("delivery_ref"), str)
            or not response["delivery_ref"].strip()):
        raise ValueError("SELECTED_DISPATCH_NOT_ACCEPTED")
    receipt["dispatch"] = {"state": "SENT_ACCEPTED", "delivery_ref": response["delivery_ref"],
                           "recipient_ref": recipient,
                           "selected_sha256": selected["sha256"],
                           "assertion": "HOST_SENDER_RETURN_NOT_RECIPIENT_USE"}
    updated = (json.dumps(receipt, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    temporary = receipt_path.with_name(".receipt-dispatch-pending.json")
    with temporary.open("xb") as stream:
        stream.write(updated)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, receipt_path)
    return {**result, "dispatch": "SENT_ACCEPTED", "delivery_ref": response["delivery_ref"],
            "recipient_use": "NOT_OBSERVED"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-record", required=True)
    parser.add_argument("--return-input", required=True)
    parser.add_argument("--out-dir", required=True)
    for name in ("owner-facts", "owner-binding", "parent-manifest", "verifier",
                 "verifier-delta", "currentness", "prior-record", "bounded-context"):
        parser.add_argument("--" + name)
    args = parser.parse_args()
    try:
        result = prepare(capture_record=args.capture_record, return_input=args.return_input,
                         out_dir=args.out_dir, owner_facts=args.owner_facts,
                         owner_binding=args.owner_binding, parent_manifest=args.parent_manifest,
                         verifier=args.verifier, verifier_delta=args.verifier_delta,
                         currentness=args.currentness, prior_record=args.prior_record,
                         bounded_context=args.bounded_context)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, UnicodeError) as exc:
        print(json.dumps({"result": "REFUSED", "reason": str(exc).split(":", 1)[0]}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
