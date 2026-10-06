#!/usr/bin/env python3
"""Bind exact Rom A capture, verifier, and Source-currentness files to BK05 inputs.

This is an offline producer. It does not authenticate caller metadata, manufacture a
review, read a host identity, or decide that a local candidate may be consumed. Missing
or mismatched evidence yields a durable gap report; complete inputs are passed to the
existing BK05 structural evaluator without upgrading parent review state.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
from typing import Any
import io
import zipfile


PR73_HEAD = "ea34ba16c4868ed35812c1c714dcf8d8eff295dd"
PR73_CAPTURE_BLOB = "27e2a2a31398d247a26682315a4e144f3adeb317"
EXPECTED_PARENT_SHA256 = "fafa8d2518a281c8fcc3341fa3729c0d0e10c9dac8ee6c515e35874bedbf83d0"
EXPECTED_PARENT_BYTES = 854
ANCESTOR_SHA256 = "df8ecdcdf5c451cdfeca4948869c5940ead0f986acfb211f2f1bb7dbad17a9f5"
ANCESTOR_BYTES = 2359
DISPATCH_ARCHIVE_SHA256 = "499e01c4c6bd78180b6100a5d37931dd55d2f0483343413e5241e782e385216d"
PARENT_REVIEW_REF = "A1-CORE-PARENT-SEMANTIC-REVIEW-20261006-001#1B-BK-real-input-producer"
PARENT_REVIEW_RECEIPT_SHA256 = "79aeeb295ed0d2d649b5cc091455ae9772016005ab83c0b643d62c32a90b31ec"
EVIDENCE_ROWS = {
    "RYG353": "655 PR73 durability repair terminal; implementation evidence, not A4 review",
    "RYG358": "A4 PR73 minimum recheck PASS; prior-head code verdict, not current producer review",
    "RYG359": "A4 PR75 changed-risk PASS; prior-head code verdict, not current producer review",
    "RYG365": "A1 CB16 adapter start; not an A4 verifier return and excluded from verifier evidence",
    "RYG362": "Historical 2359-byte ancestor original; bytes only, metadata remains unreviewed",
    "A1-CORE-PARENT-SEMANTIC-REVIEW-20261006-001": "Attributable parent-scope review for the exact 854-byte current 1B dispatch",
}
REQUIRED_INPUTS = (
    "ancestor_original.txt",
    "parent.txt",
    "capture.json",
    "parent_manifest.json",
    "owner_review_receipt.json",
    "dispatch_capture_archive.zip",
    "dispatch_captures.json",
    "currentness.json",
)
REVIEW_INPUTS = (
    "verifier.txt",
    "verifier_delta.json",
)
SCOPE_FIELDS = (
    "actor_ref", "effect_class", "privacy_class", "live_scope", "authority_class", "return_target",
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _dump(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("DUPLICATE_JSON_KEY:" + key)
        result[key] = value
    return result


def _read_json(path: Path, *, object_only: bool = True) -> tuple[bytes | None, Any, str | None]:
    if not path.is_file() or path.is_symlink():
        return None, None, "INPUT_ABSENT_OR_NOT_REGULAR"
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8", errors="strict")
        if text.startswith("\ufeff"):
            raise ValueError("JSON_BOM_PROHIBITED")
        obj = json.loads(text, object_pairs_hook=_unique_pairs,
                         parse_constant=lambda _: (_ for _ in ()).throw(ValueError("NONFINITE_JSON")))
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        return raw, None, str(exc).split(":", 1)[0]
    if object_only and not isinstance(obj, dict):
        return raw, None, "JSON_OBJECT_REQUIRED"
    return raw, obj, None


def _bindings(evidence_root: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for name in (*REQUIRED_INPUTS, *REVIEW_INPUTS):
        path = evidence_root / name
        if path.is_file() and not path.is_symlink():
            raw = path.read_bytes()
            result[name] = {"path": name, "bytes": len(raw), "sha256": _sha(raw), "state": "READ"}
        else:
            result[name] = {"path": name, "bytes": None, "sha256": None, "state": "MISSING"}
    return result


def _source_binding(source_root: Path, currentness_raw: bytes | None) -> dict[str, Any]:
    paths = (
        "candidates/bk05-capsule-tool-v1/CONTRACT.md",
        "candidates/bk05-capsule-tool-v1/src/bk05_capsule/core.py",
        "tooling/return-bridge/rom_a_review_input.py",
    )
    files: dict[str, Any] = {}
    for relative in paths:
        path = source_root / relative
        raw = path.read_bytes() if path.is_file() and not path.is_symlink() else None
        files[relative] = {"bytes": len(raw) if raw is not None else None,
                           "sha256": _sha(raw) if raw is not None else None,
                           "state": "READ" if raw is not None else "MISSING"}
    return {
        "schema": "cerebro-rom-a-bk05-source-binding/v1",
        "source_head": None,
        "source_head_basis": "currentness.json is caller-supplied; no git/provider currentness is inferred",
        "currentness_file": {"path": "currentness.json", "bytes": len(currentness_raw) if currentness_raw is not None else None,
                             "sha256": _sha(currentness_raw) if currentness_raw is not None else None},
        "basis_files": files,
        "authority": "NONE",
        "host_binding": "NONE",
    }


def _gap(source_root: Path, bindings: dict[str, dict[str, Any]], missing: list[str], code: str,
         *, currentness_raw: bytes | None, detail: Any = None, **extra: Any) -> dict[str, Any]:
    source_binding = _source_binding(source_root, currentness_raw)
    source_binding.update({"input_bindings": bindings, "evidence_rows": EVIDENCE_ROWS,
                           "pr73_capture_implementation": {"commit": PR73_HEAD,
                                                            "path": "tooling/return-bridge/rom_a_dispatch_capture.py",
                                                            "git_blob": PR73_CAPTURE_BLOB}})
    result: dict[str, Any] = {
        "state": "EXACT_INPUT_GAP",
        "schema": "cerebro-rom-a-bk05-review-input-gap/v1",
        "gap": code,
        "missing_inputs": missing,
        "expected_parent": {"path": "parent.txt", "bytes": EXPECTED_PARENT_BYTES,
                             "sha256": EXPECTED_PARENT_SHA256},
        "expected_ancestor_original": {"path": "ancestor_original.txt", "bytes": ANCESTOR_BYTES,
                                        "sha256": ANCESTOR_SHA256},
        "source_binding": source_binding,
        "limits": ["NO_CAPSULE", "NO_REVIEW_PROMOTION", "NO_HOST_BINDING", "NO_RECIPIENT_USE_CLAIM"],
        "authority": "NONE",
        "host_binding": "NONE",
    }
    if detail is not None:
        result["detail"] = detail
    result.update(extra)
    return result


def _load_core(source_root: Path):
    path = source_root / "candidates/bk05-capsule-tool-v1/src/bk05_capsule/core.py"
    spec = importlib.util.spec_from_file_location("bk05_review_input_core", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("BK05_CORE_NOT_IMPORTABLE")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _capture_gap(parent_raw: bytes, receipt: dict[str, Any]) -> str | None:
    if len(parent_raw) != EXPECTED_PARENT_BYTES or _sha(parent_raw) != EXPECTED_PARENT_SHA256:
        return "ORIGINAL_CAPTURE_BYTES_DO_NOT_MATCH_RYG362"
    if receipt.get("schema") != "cerebro-rom-a-dispatch-capture/v1-receipt":
        return "PR73_CAPTURE_RECEIPT_SCHEMA_MISMATCH"
    if receipt.get("original_sha256") != EXPECTED_PARENT_SHA256 or receipt.get("original_bytes") != EXPECTED_PARENT_BYTES:
        return "PR73_CAPTURE_RECEIPT_DOES_NOT_BIND_RYG362_ORIGINAL"
    if receipt.get("authority") != "NONE" or receipt.get("effect") != "NONE_CLAIMED":
        return "PR73_CAPTURE_AUTHORITY_OR_EFFECT_CEILING_MISMATCH"
    if receipt.get("recipient_read_or_use_proven") is not False:
        return "PR73_CAPTURE_CANNOT_CLAIM_RECIPIENT_USE"
    if receipt.get("publish_durability") != "LOCAL_READBACK_ONLY_CRASH_DURABILITY_UNPROVEN":
        return "PR73_CAPTURE_DURABILITY_CEILING_MISMATCH"
    if not isinstance(receipt.get("task"), dict):
        return "PR73_CAPTURE_TASK_METADATA_MISSING"
    return None


def _parent_review_gap(parent_raw: bytes, capture: dict[str, Any], manifest: dict[str, Any],
                       review: dict[str, Any]) -> str | None:
    if _sha(parent_raw) != EXPECTED_PARENT_SHA256 or len(parent_raw) != EXPECTED_PARENT_BYTES:
        return "CURRENT_PARENT_BYTES_DO_NOT_MATCH_OWNER_REVIEWED_DISPATCH"
    if manifest.get("task_ref") != "1B-BK-real-input-producer" or manifest.get("parent_revision") != "1":
        return "OWNER_REVIEWED_PARENT_IDENTITY_MISMATCH"
    if manifest.get("parent_sha256") != _sha(parent_raw):
        return "OWNER_REVIEWED_PARENT_HASH_MISMATCH"
    if manifest.get("semantic_review") != {"receipt_ref": PARENT_REVIEW_REF, "status": "OWNER_REVIEWED"}:
        return "OWNER_REVIEW_MANIFEST_REFERENCE_MISMATCH"
    if review.get("schema") != "a1-core-parent-semantic-review/v1":
        return "OWNER_REVIEW_RECEIPT_SCHEMA_MISMATCH"
    if review.get("task_ref") != manifest.get("task_ref") or review.get("original_sha256") != _sha(parent_raw):
        return "OWNER_REVIEW_RECEIPT_PARENT_BINDING_MISMATCH"
    if review.get("assertion") != "CALLER_ASSERTED_UNAUTHENTICATED":
        return "OWNER_REVIEW_ASSERTION_CEILING_MISMATCH"
    if review.get("reviewer") != "A1 Human-appointed coordinator; native ROLE_NULL claims NONE":
        return "OWNER_REVIEW_REVIEWER_ATTRIBUTION_MISMATCH"
    if review.get("not_proven") is None or not {
        "verifier review or delta", "capsule eligibility", "recipient capsule use",
    }.issubset(set(review.get("not_proven", []))):
        return "OWNER_REVIEW_LIMITS_INCOMPLETE"
    task = capture.get("task")
    if not isinstance(task, dict):
        return "CAPTURE_TASK_METADATA_MISSING"
    compare_fields = (
        "task_ref", "task_revision", "actor_ref", "effect_class", "privacy_class", "live_scope",
        "authority_class", "return_target", "way_home", "source_head", "allowed_paths",
        "required_invariants", "stop_edges",
    )
    manifest_fields = {"task_revision": "parent_revision"}
    for field in compare_fields:
        manifest_key = manifest_fields.get(field, field)
        if task.get(field) != manifest.get(manifest_key):
            return "OWNER_REVIEWED_SCOPE_DIFFERS_FROM_CAPTURE:" + field
    # The historical PR73 capture metadata explicitly remains unreviewed. Only the
    # separate attributable receipt above permits the current parent manifest status.
    if capture.get("semantic_review", {}).get("status") != "UNREVIEWED":
        return "HISTORICAL_CAPTURE_REVIEW_CEILING_CHANGED"
    return None


def _repair_same_actor(delta: dict[str, Any], parent_actor: str) -> tuple[dict[str, Any], list[dict[str, str]]]:
    repaired = dict(delta)
    repairs: list[dict[str, str]] = []
    if repaired.get("requested_actor_ref") != parent_actor:
        repairs.append({"field": "requested_actor_ref", "source": "parent_manifest.actor_ref",
                        "before": str(repaired.get("requested_actor_ref")), "after": parent_actor,
                        "reason": "BK05_REQUESTED_ACTOR_MUST_EQUAL_THE_REVIEWED_PARENT_ACTOR"})
        repaired["requested_actor_ref"] = parent_actor
    return repaired, repairs


def prepare(evidence_root: Path, source_root: Path) -> dict[str, Any]:
    evidence_root = evidence_root.resolve()
    source_root = source_root.resolve()
    bindings = _bindings(evidence_root)
    missing = [name for name, info in bindings.items() if info["state"] != "READ"]
    required_missing = [name for name in missing if name in REQUIRED_INPUTS]
    loaded = {name: (evidence_root / name).read_bytes()
              for name in (*REQUIRED_INPUTS, *REVIEW_INPUTS) if bindings[name]["state"] == "READ"}
    if required_missing:
        return _gap(source_root, bindings, missing, "PARENT_OR_CURRENTNESS_EVIDENCE_INCOMPLETE",
                    currentness_raw=loaded.get("currentness.json"))

    ancestor = loaded["ancestor_original.txt"]
    if len(ancestor) != ANCESTOR_BYTES or _sha(ancestor) != ANCESTOR_SHA256:
        return _gap(source_root, bindings, missing, "ANCESTOR_ORIGINAL_DOES_NOT_MATCH_RYG362",
                    currentness_raw=loaded["currentness.json"],
                    observed_ancestor={"bytes": len(ancestor), "sha256": _sha(ancestor)})

    capture_raw, capture, capture_error = _read_json(evidence_root / "capture.json")
    manifest_raw, manifest, manifest_error = _read_json(evidence_root / "parent_manifest.json")
    owner_raw, owner_review, owner_error = _read_json(evidence_root / "owner_review_receipt.json")
    current_raw, currentness, current_error = _read_json(evidence_root / "currentness.json")
    dispatch_raw, dispatch_index, dispatch_error = _read_json(
        evidence_root / "dispatch_captures.json", object_only=False
    )
    if any((capture_error, manifest_error, owner_error, current_error, dispatch_error)) or any(
        value is None for value in (capture, manifest, owner_review, currentness, dispatch_index)
    ):
        return _gap(source_root, bindings, missing, "REQUIRED_EVIDENCE_JSON_INVALID",
                    currentness_raw=loaded["currentness.json"],
                    detail={"capture": capture_error, "parent_manifest": manifest_error,
                            "owner_review_receipt": owner_error, "currentness": current_error,
                            "dispatch_captures": dispatch_error})

    capture_gap = _capture_gap(loaded["parent.txt"], capture)
    if capture_gap:
        return _gap(source_root, bindings, missing, capture_gap, currentness_raw=current_raw)
    review_gap = _parent_review_gap(loaded["parent.txt"], capture, manifest, owner_review)
    if review_gap:
        return _gap(source_root, bindings, missing, review_gap, currentness_raw=current_raw)
    if _sha(owner_raw or b"") != PARENT_REVIEW_RECEIPT_SHA256:
        return _gap(source_root, bindings, missing, "OWNER_REVIEW_RECEIPT_FILE_HASH_MISMATCH",
                    currentness_raw=current_raw)

    try:
        with zipfile.ZipFile(io.BytesIO(loaded["dispatch_capture_archive.zip"])) as archive:
            archive_index = archive.read("captures.json")
            archive_parent = archive.read("1B-BK-real-input-producer/original_task.txt")
            archive_capture = json.loads(archive_index.decode("utf-8"))
    except (KeyError, OSError, zipfile.BadZipFile, UnicodeError, ValueError) as exc:
        return _gap(source_root, bindings, missing, "DISPATCH_CAPTURE_ARCHIVE_INVALID",
                    currentness_raw=current_raw, detail=str(exc).split(":", 1)[0])
    if (_sha(loaded["dispatch_capture_archive.zip"]) != DISPATCH_ARCHIVE_SHA256
            or archive_index != loaded["dispatch_captures.json"]
            or archive_parent != loaded["parent.txt"]
            or not isinstance(dispatch_index, list)
            or len(dispatch_index) != 5 or archive_capture != dispatch_index):
        return _gap(source_root, bindings, missing, "FIVE_DISPATCH_CAPTURE_ARCHIVE_BINDING_MISMATCH",
                    currentness_raw=current_raw)
    matching_dispatch = [item for item in dispatch_index
                         if isinstance(item, dict) and item.get("task") == "1B-BK-real-input-producer"]
    if (len(matching_dispatch) != 1 or matching_dispatch[0].get("sha256") != _sha(loaded["parent.txt"])
            or matching_dispatch[0].get("bytes") != len(loaded["parent.txt"])):
        return _gap(source_root, bindings, missing, "CURRENT_PARENT_NOT_IN_FIVE_DISPATCH_CAPTURE_SET",
                    currentness_raw=current_raw)

    if (currentness.get("observed_source_head") != manifest.get("source_head")
            or currentness.get("observed_parent_revision") != manifest.get("parent_revision")):
        return _gap(source_root, bindings, missing, "SOURCE_OR_PARENT_CURRENTNESS_DOES_NOT_MATCH_REVIEWED_PARENT",
                    currentness_raw=current_raw,
                    currentness_observed={"source_head": currentness.get("observed_source_head"),
                                          "parent_revision": currentness.get("observed_parent_revision")},
                    parent_basis={"source_head": manifest.get("source_head"),
                                  "parent_revision": manifest.get("parent_revision")})

    absent_a4 = [name for name in REVIEW_INPUTS if bindings[name]["state"] != "READ"]
    source_binding = _source_binding(source_root, current_raw)
    source_binding.update({
        "source_head": currentness.get("observed_source_head"),
        "parent_revision": currentness.get("observed_parent_revision"),
        "input_bindings": bindings,
        "evidence_rows": EVIDENCE_ROWS,
        "pr73_capture_implementation": {"commit": PR73_HEAD,
                                         "path": "tooling/return-bridge/rom_a_dispatch_capture.py",
                                         "git_blob": PR73_CAPTURE_BLOB},
        "capture_metadata_review_state": capture.get("semantic_review", {}).get("status"),
        "owner_review_manifest_file_sha256": _sha(manifest_raw or b""),
        "owner_review_receipt_file_sha256": _sha(owner_raw or b""),
        "dispatch_archive_sha256": _sha(loaded["dispatch_capture_archive.zip"]),
        "ancestor_original": {"bytes": len(ancestor), "sha256": _sha(ancestor),
                              "receipt_state": "RAW_BYTES_ONLY; HISTORICAL_METADATA_UNREVIEWED"},
    })
    if absent_a4:
        return {
            "state": "EXACT_INPUT_GAP",
            "schema": "cerebro-rom-a-bk05-review-input-gap/v1",
            "gap": "A4_NEW_HEAD_VERIFIER_BYTES_AND_TYPED_DELTA_REQUIRED",
            "missing_inputs": absent_a4,
            "parent": {"path": "parent.txt", "bytes": len(loaded["parent.txt"]),
                       "sha256": _sha(loaded["parent.txt"]), "task_ref": manifest["task_ref"],
                       "revision": manifest["parent_revision"], "actor_ref": manifest["actor_ref"],
                       "allowed_paths": manifest["allowed_paths"]},
            "parent_review": {"state": manifest["semantic_review"]["status"],
                              "receipt_ref": manifest["semantic_review"]["receipt_ref"],
                              "receipt_sha256": _sha(owner_raw or b""),
                              "assertion": owner_review["assertion"]},
            "capture": {"path": "capture.json", "sha256": _sha(capture_raw or b""),
                        "semantic_review": capture["semantic_review"]["status"],
                        "effect": capture["effect"], "authority": capture["authority"],
                        "publish_durability": capture["publish_durability"]},
            "currentness": currentness,
            "source_binding": source_binding,
            "prior_verdicts_not_substitutes_for_current_a4_review": {
                "RYG353": EVIDENCE_ROWS["RYG353"],
                "RYG358": EVIDENCE_ROWS["RYG358"],
                "RYG359": EVIDENCE_ROWS["RYG359"],
                "RYG365": EVIDENCE_ROWS["RYG365"],
            },
            "next_required_evidence": [
                "A4 verifier.txt bytes that review this producer's exact immutable head and finding",
                "A4 verifier_delta.json with exact reviewer return ref, current reviewed head, and typed test delta",
            ],
            "producer_field_repair": "requested_actor_ref is copied from the reviewed parent actor when a real A4 delta arrives; source delta bytes are retained unchanged and the correction is hash-bound",
            "limits": ["NO_CAPSULE_UNTIL_CURRENT_A4_REVIEW", "NO_HOST_BINDING", "NO_RECIPIENT_USE_CLAIM",
                       "NO_REVIEW_PROMOTION_FROM_CAPTURE_METADATA"],
            "authority": "NONE",
            "host_binding": "NONE",
        }

    verifier_raw = loaded["verifier.txt"]
    delta_raw, delta_source, delta_error = _read_json(evidence_root / "verifier_delta.json")
    if delta_error or delta_source is None:
        return _gap(source_root, bindings, missing, "A4_VERIFIER_DELTA_INVALID",
                    currentness_raw=current_raw, detail=delta_error)
    delta, repairs = _repair_same_actor(delta_source, manifest["actor_ref"])
    delta["verifier_sha256"] = _sha(verifier_raw)

    core = _load_core(source_root)
    evaluated = core.evaluate(
        loaded["parent.txt"], manifest_raw, verifier_raw, _dump(delta), current_raw,
    )
    decision = evaluated.decision_document()
    source_binding["producer_field_repairs"] = repairs
    package = {
        "state": "BK05_INPUT_PACKAGE",
        "schema": "cerebro-rom-a-bk05-review-input-package/v1",
        "decision": decision,
        "producer_field_repairs": repairs,
        "source_binding": source_binding,
        "authority": "NONE",
        "host_binding": "NONE",
        "limits": ["PARENT_SCOPE_IS_CALLER_ASSERTED_UNREVIEWED", "CURRENTNESS_IS_CALLER_SUPPLIED_NOT_LIVE_PROOF",
                   "NO_CONSUMPTION_OR_DEPLOYMENT", "NO_RECIPIENT_USE_CLAIM"],
    }
    if evaluated.capsule is not None:
        package["capsule"] = evaluated.capsule
        package["measurement"] = evaluated.measurement
    else:
        package["exact_gap"] = decision.get("reasons", [])
    package["inputs"] = {
        "parent.txt": loaded["parent.txt"].decode("utf-8"),
        "ancestor_original.txt": ancestor.decode("utf-8"),
        "capture.json": capture,
        "verifier.txt": verifier_raw.decode("utf-8"),
        "parent_manifest.json": manifest,
        "owner_review_receipt.json": owner_review,
        "verifier_delta.source.json": delta_source,
        "verifier_delta.json": delta,
        "currentness.json": currentness,
    }
    package["input_file_bindings"] = bindings
    return package


def write_package(result: dict[str, Any], out_dir: Path, evidence_root: Path) -> None:
    if out_dir.exists() or out_dir.is_symlink():
        raise FileExistsError("OUTPUT_EXISTS")
    bindings = result.get("source_binding", {}).get("input_bindings", {})
    raw_inputs: dict[str, bytes] = {}
    for name, binding in bindings.items():
        if binding.get("state") != "READ":
            continue
        if name not in (*REQUIRED_INPUTS, *REVIEW_INPUTS):
            raise ValueError("INPUT_NAME_NOT_ALLOWED")
        path = evidence_root / name
        if not path.is_file() or path.is_symlink():
            raise ValueError("INPUT_CHANGED_AFTER_PREPARE")
        raw = path.read_bytes()
        if len(raw) != binding.get("bytes") or _sha(raw) != binding.get("sha256"):
            raise ValueError("INPUT_CHANGED_AFTER_PREPARE")
        raw_inputs[name] = raw
    out_dir.mkdir(parents=True)
    inputs_dir = out_dir / "inputs"
    inputs_dir.mkdir()
    for name, raw in raw_inputs.items():
        (inputs_dir / name).write_bytes(raw)
    name = "gap.json" if result.get("state") == "EXACT_INPUT_GAP" else "review-input.json"
    (out_dir / name).write_bytes(_dump(result))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = prepare(args.evidence_dir, args.source_root)
        write_package(result, args.out_dir, args.evidence_dir)
        print(json.dumps({"state": result["state"], "output": str(args.out_dir),
                          "decision": result.get("decision", {}).get("decision"),
                          "missing_inputs": result.get("missing_inputs", []),
                          "authority": "NONE", "host_binding": "NONE"}, sort_keys=True))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"state": "REFUSED", "reason": str(exc).split(":", 1)[0],
                          "authority": "NONE", "host_binding": "NONE"}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
