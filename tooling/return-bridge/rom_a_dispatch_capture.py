#!/usr/bin/env python3
"""Freeze a Rom A dispatch's exact producer bytes before the caller sends it.

This is local evidence, not a sender, identity oracle, review service, or BK05
decision. The caller must pass the original UTF-8 prompt bytes, never a parsed
chat turn or reconstructed start post. A later recipient-use receipt is separate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any


SCHEMA = "cerebro-rom-a-dispatch-capture/v1"
MAX_PROMPT_BYTES = 2_000_000
HEX40 = re.compile(r"^[0-9a-f]{40}$")
TASK_FIELDS = frozenset({
    "task_ref", "task_revision", "actor_ref", "generation_ref", "carrier_ref",
    "arc_ref", "source_head", "effect_class", "privacy_class", "live_scope",
    "authority_class", "return_target", "way_home", "allowed_paths",
    "required_invariants", "stop_edges",
})


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _write_durable(path: Path, raw: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise ValueError(name + "_REQUIRED")
    return value


def _list(value: object, name: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(name + "_REQUIRED")
    result = [_text(item, name) for item in value]
    if len(result) != len(set(result)):
        raise ValueError(name + "_DUPLICATE")
    return result


def _validate_request(request: object) -> dict[str, Any]:
    if not isinstance(request, dict) or set(request) - {"schema", "task", "provenance", "semantic_review"}:
        raise ValueError("REQUEST_FIELDS_INVALID")
    if request.get("schema") != SCHEMA or not isinstance(request.get("task"), dict):
        raise ValueError("REQUEST_SCHEMA_INVALID")
    task = request["task"]
    if set(task) != TASK_FIELDS:
        raise ValueError("TASK_FIELDS_INVALID")
    for field in TASK_FIELDS - {"allowed_paths", "required_invariants", "stop_edges"}:
        _text(task[field], field.upper())
    if not HEX40.fullmatch(task["source_head"]):
        raise ValueError("SOURCE_HEAD_INVALID")
    for field in ("allowed_paths", "required_invariants", "stop_edges"):
        _list(task[field], field.upper())
    provenance = request.get("provenance")
    if provenance is None:
        provenance = {"status": "UNVERIFIED", "original_dispatch_ref": ""}
    if (not isinstance(provenance, dict) or set(provenance) != {"status", "original_dispatch_ref"}
            or provenance["status"] not in {"CALLER_ASSERTED", "UNVERIFIED"}
            or not isinstance(provenance["original_dispatch_ref"], str)):
        raise ValueError("PROVENANCE_INVALID")
    if provenance["status"] == "CALLER_ASSERTED":
        _text(provenance["original_dispatch_ref"], "ORIGINAL_DISPATCH_REF")
    elif provenance["original_dispatch_ref"]:
        raise ValueError("UNVERIFIED_PROVENANCE_REF_CONFLICT")
    review = request.get("semantic_review")
    if review is None:
        review = {"status": "UNREVIEWED", "receipt_ref": ""}
    if (not isinstance(review, dict) or set(review) != {"status", "receipt_ref"}
            or review["status"] not in {"OWNER_REVIEWED", "UNREVIEWED"}
            or not isinstance(review["receipt_ref"], str)):
        raise ValueError("REVIEW_INVALID")
    if review["status"] == "OWNER_REVIEWED":
        _text(review["receipt_ref"], "REVIEW_RECEIPT_REF")
    elif review["receipt_ref"]:
        raise ValueError("UNREVIEWED_RECEIPT_CONFLICT")
    if provenance["status"] == "UNVERIFIED" and review["status"] != "UNREVIEWED":
        raise ValueError("PROVENANCE_REQUIRED_FOR_REVIEW")
    return {"task": task, "provenance": provenance, "semantic_review": review}


def _readback(directory: Path, expected: dict[str, Any]) -> dict[str, Any]:
    record_path = directory / "capture.json"
    original_path = directory / "original_task.txt"
    if (not directory.is_dir() or directory.is_symlink() or record_path.is_symlink()
            or original_path.is_symlink() or not record_path.is_file() or not original_path.is_file()):
        raise ValueError("CAPTURE_READBACK_UNAVAILABLE")
    try:
        record_bytes = record_path.read_bytes()
        observed = json.loads(record_bytes, object_pairs_hook=_unique_pairs)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("CAPTURE_READBACK_INVALID") from exc
    raw = original_path.read_bytes()
    if (observed != expected or record_bytes != _canonical(expected) + b"\n"
            or len(raw) != expected["original_bytes"]
            or _sha(raw) != expected["original_sha256"]):
        raise ValueError("COLLISION")
    return {"state": "EXACT_REUSE", "capture_path": str(record_path),
            "original_path": str(original_path), "receipt": observed}


def capture(root: Path, request: object, original: bytes) -> dict[str, Any]:
    """Atomically install one immutable (task_ref, revision) record or read it back."""
    if not isinstance(original, bytes) or not original or len(original) > MAX_PROMPT_BYTES:
        raise ValueError("ORIGINAL_BYTES_INVALID")
    try:
        original.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ValueError("ORIGINAL_NON_UTF8") from exc
    validated = _validate_request(request)
    task = validated["task"]
    key = _sha(_canonical([task["task_ref"], task["task_revision"]]))
    record = {
        "schema": SCHEMA + "-receipt", "capture_key": key,
        "task": task, "original_sha256": _sha(original), "original_bytes": len(original),
        "provenance": {**validated["provenance"], "assertion": "CALLER_SUPPLIED_NOT_VERIFIED"},
        "semantic_review": {**validated["semantic_review"], "assertion": "CALLER_SUPPLIED_NOT_VERIFIED"},
        "identity_currentness": "CALLER_SUPPLIED_NOT_PROVIDER_VERIFIED",
        "capture_stage": "LOCAL_CAPTURE_TIMING_NOT_PROVIDER_VERIFIED",
        "authority": "NONE", "effect": "NONE_CLAIMED", "work_consumed": False,
        "recipient_read_or_use_proven": False,
    }
    if root.is_symlink():
        raise ValueError("CAPTURE_ROOT_SYMLINK")
    root.mkdir(parents=True, exist_ok=True)
    destination = root / ("capture-" + key)
    if destination.exists() or destination.is_symlink():
        return _readback(destination, record)
    temporary = Path(tempfile.mkdtemp(prefix=".capture-pending-", dir=root))
    try:
        _write_durable(temporary / "original_task.txt", original)
        _write_durable(temporary / "capture.json", _canonical(record) + b"\n")
        try:
            os.rename(temporary, destination)
        except OSError:
            if destination.exists() or destination.is_symlink():
                return _readback(destination, record)
            raise
        result = _readback(destination, record)
        result["state"] = "CAPTURED_READBACK"
        return result
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--original", required=True, type=Path)
    parser.add_argument("--capture-root", required=True, type=Path)
    args = parser.parse_args()
    try:
        if not args.request.is_file() or args.request.is_symlink() or args.request.stat().st_size > 100_000:
            raise ValueError("REQUEST_FILE_INVALID")
        if not args.original.is_file() or args.original.is_symlink() or args.original.stat().st_size > MAX_PROMPT_BYTES:
            raise ValueError("ORIGINAL_FILE_INVALID")
        request = json.loads(args.request.read_bytes(), object_pairs_hook=_unique_pairs,
                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError("NONFINITE_JSON")))
        result = capture(args.capture_root, request, args.original.read_bytes())
        print(json.dumps({"state": result["state"], "capture_path": result["capture_path"],
                          "original_path": result["original_path"],
                          "original_sha256": result["receipt"]["original_sha256"],
                          "original_bytes": result["receipt"]["original_bytes"],
                          "authority": "NONE"}, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        print(json.dumps({"state": "REFUSED", "reason": str(exc).split(":", 1)[0]}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
