#!/usr/bin/env python3
"""Local E material entry. Existing capture/BK04/BK05 contracts; never send.

The file CLI has no recipient-memory switch and uses the full-original reserve.
A legitimate in-process owner may supply held_basis_reader; exact held bytes
are checked, but semantic qualification/currentness remain that owner's duty.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
BRIDGE = ROOT / "tooling/return-bridge"
LIMIT = 2_000_000
SIDEFILES = {"parent_manifest": "parent_manifest.json", "verifier": "verifier.txt",
             "verifier_delta": "verifier_delta.json", "currentness": "currentness.json",
             "prior_record": "prior_record.json"}
DEPENDENCIES = (
    "tooling/return-bridge/rom_a_dispatch_capture.py",
    "tooling/return-bridge/rom_a_normal_path.py",
    "tooling/return-bridge/rom_a_return_adoption.py",
    "tooling/return-bridge/work_error_reflex.py",
    "tooling/return-bridge/task_local_profile.py",
    "tooling/return-bridge/task_capacity_care.py",
    "candidates/bk04-episode-verifier-v1/bk04_verify.py",
    "candidates/bk05-capsule-tool-v1/src/bk05_capsule/core.py",
)


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _path(value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute() or any(
        p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction())
        for p in (path, *path.parents)
    ):
        raise ValueError("LOCAL_ABSOLUTE_NONLINK_PATH_REQUIRED")
    return path


def _read(value: str | Path) -> bytes:
    path = _path(value)
    if not path.is_file() or path.stat().st_size > LIMIT:
        raise ValueError("INPUT_FILE_UNAVAILABLE_OR_TOO_LARGE")
    return path.read_bytes()


def _load(name: str):
    path = BRIDGE / (name + ".py")
    _read(path)
    spec = importlib.util.spec_from_file_location("e_entry_" + name, path)
    if spec is None or spec.loader is None:
        raise ValueError("MODULE_UNAVAILABLE")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def presend(*, request: str, original: str, capture_root: str) -> dict[str, Any]:
    """Caller invokes BEFORE its legitimate send, never after receipt/delivery."""
    capture = _load("rom_a_dispatch_capture")
    obj = json.loads(_read(request), object_pairs_hook=capture._unique_pairs,
                     parse_constant=lambda _: (_ for _ in ()).throw(ValueError("NONFINITE_JSON")))
    return capture.capture(_path(capture_root), obj, _read(original))


def _reuse(out: Path, fingerprint: str) -> dict[str, Any]:
    manifest_path = out / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("OUTPUT_INCOMPLETE_RECONCILE_SAME_ATTEMPT")
    manifest = json.loads(_read(manifest_path))
    if manifest.get("input_sha256") != fingerprint:
        raise ValueError("IDEMPOTENCY_CONFLICT")
    expected = manifest.get("files")
    if not isinstance(expected, dict):
        raise ValueError("OUTPUT_CONFLICT")
    actual = set()
    for path in out.rglob("*"):
        _path(path)
        if path.is_file() and path != manifest_path:
            rel = path.relative_to(out).as_posix()
            actual.add(rel)
            raw = _read(path)
            if expected.get(rel) != {"bytes": len(raw), "sha256": _sha(raw)}:
                raise ValueError("OUTPUT_CONFLICT")
    if actual != set(expected):
        raise ValueError("OUTPUT_CONFLICT")
    return {"state": "EXACT_REUSE", "manifest": str(manifest_path),
            "selection": manifest["selection"], "authority": "NONE"}


def finalize(*, capture_record: str, return_input: str, output_root: str,
             held_basis_reader: Callable | None = None, **sidefiles: str) -> dict[str, Any]:
    """Freeze typed inputs, call existing control, publish one local manifest last.

    held_basis_reader(task, original_sha256, review_sha256) is a direct owner
    callback returning (qualified_current_ref, held_original_bytes, held_review_bytes).
    It must read the actual recipient's already-qualified current basis. Neither
    a callback reference nor byte equality proves semantic context or provider state.
    No serialized flag/file enables this boundary in the CLI.
    """
    if set(sidefiles) - set(SIDEFILES):
        raise ValueError("UNSUPPORTED_INPUT")
    if held_basis_reader is not None and not callable(held_basis_reader):
        raise ValueError("HELD_BASIS_READER_REQUIRED_NOT_FLAG")
    normal = _load("rom_a_normal_path")
    task, _, original, _ = normal._capture(str(_path(capture_record)))
    returned, return_ref, facts, _ = normal._return_input(str(_path(return_input)))
    if any(returned[k] != task[k] for k in (
        "task_ref", "task_revision", "actor_ref", "generation_ref", "carrier_ref", "arc_ref"
    )) or returned["original_sha256"] != _sha(original):
        raise ValueError("RETURN_IDENTITY_OR_ORIGINAL_MISMATCH")
    kind = returned["contract_kind"]
    if kind not in {"FACT_RETURN", "REPAIR", "REFINE"}:
        raise ValueError("MATERIAL_RETURN_KIND_REQUIRED")
    if kind == "FACT_RETURN" and facts is None:
        raise ValueError("TYPED_PRODUCER_FACTS_REQUIRED")
    capdir = Path(capture_record).parent
    episode = Path(return_input).parent
    captured: dict[str, bytes] = {
        "capture/" + capdir.name + "/capture.json": _read(capture_record),
        "capture/" + capdir.name + "/original_task.txt": original,
        "episode/return.json": _read(return_input),
    }
    for name in ("task_local_profile.json", "task_capacity_care.json"):
        path = capdir / name
        if path.exists() or path.is_symlink():
            captured["capture/" + capdir.name + "/" + name] = _read(path)
    chosen = {}
    for arg, name in SIDEFILES.items():
        path = Path(sidefiles[arg]) if arg in sidefiles else episode / name
        if arg in sidefiles or path.exists() or path.is_symlink():
            captured["episode/" + name] = _read(path)
            chosen[arg] = name
    error = episode / "work_error.json"
    if error.exists() or error.is_symlink():
        captured["episode/work_error.json"] = _read(error)
    if "currentness" in chosen:
        current = normal._json(captured["episode/currentness.json"])
        if (current.get("observed_source_head") != task["source_head"] or
                current.get("observed_parent_revision") != task["task_revision"]):
            raise ValueError("STALE_BASIS_REQUIRES_OWNER_RECONCILIATION")
    held = None
    if held_basis_reader is not None and kind in normal.REPAIR_KINDS and "verifier" in chosen:
        value = held_basis_reader(dict(task), _sha(original), _sha(captured["episode/verifier.txt"]))
        if (not isinstance(value, tuple) or len(value) != 3 or
                not isinstance(value[0], str) or not value[0].strip() or
                type(value[1]) is not bytes or type(value[2]) is not bytes):
            raise ValueError("QUALIFIED_HELD_BYTES_AND_CURRENT_REF_REQUIRED")
        held = {"current_ref": value[0], "original_sha256": _sha(value[1]),
                "review_sha256": _sha(value[2]),
                "exact": value[1] == original and value[2] == captured["episode/verifier.txt"]}
    code = {p: _sha(_read(ROOT / p)) for p in DEPENDENCIES}
    code[Path(__file__).relative_to(ROOT).as_posix()] = _sha(_read(Path(__file__)))
    fingerprint = _sha(_canonical({"inputs": {p: _sha(b) for p, b in captured.items()},
                                   "held_basis": held, "code": code}))
    key = _sha(_canonical([task["task_ref"], task["task_revision"], return_ref["observation_ref"]]))
    root = _path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    out = root / ("return-" + key)
    if out.exists():
        return _reuse(out, fingerprint)
    try:
        out.mkdir()  # Exclusive reservation; an incomplete prior result is never replayed.
    except FileExistsError:
        return _reuse(out, fingerprint)
    try:
        for rel, raw in captured.items():
            path = out / "inputs" / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        result = normal.prepare(
            capture_record=str(out / "inputs/capture" / capdir.name / "capture.json"),
            return_input=str(out / "inputs/episode/return.json"), out_dir=str(out / "control"),
            **{arg: str(out / "inputs/episode" / name) for arg, name in chosen.items()})
        receipt = normal._json((out / "control/receipt.json").read_bytes())
        adoption = receipt["adoption"]
        finding = adoption["bk04"].get("finding", {})
        if kind == "FACT_RETURN" and finding.get("structural_overall_status") != "PASS":
            raise ValueError("PRODUCER_FACTS_INCOMPLETE_OR_CONFLICT")
        if receipt["delta_field_conflicts"]:
            raise ValueError("BK05_INVALID_DELTA_BINDING_CONFLICT")
        if adoption["bk05"]["decision"] in {"INVALID_INPUT", "HOLD_STALE"}:
            raise ValueError("BK05_INVALID_OR_STALE_RECONCILE_SAME_ATTEMPT")
        selection, payload = "FULL_ORIGINAL_TASK", original
        reason = "FULL_CURRENT_ORIGINAL_RESERVE"
        if result["selection"] == "BK05_STRUCTURAL_CAPSULE_CANDIDATE":
            reason = "RECIPIENT_HELD_BASIS_NOT_QUALIFIED"
            if held is not None and held["exact"]:
                core = normal._module("e_entry_bk05", ROOT / DEPENDENCIES[-1])
                capsule = Path(result["selected"]).read_bytes()
                decision, _, _ = core.reconstruct(capsule, original, captured["episode/verifier.txt"])
                if decision != core.RECONSTRUCTED:
                    raise ValueError("CAPSULE_RECONSTRUCTION_REFUSED")
                selection, payload = result["selection"], capsule
                reason = "OWNER_QUALIFIED_HELD_BYTES_STRUCTURALLY_RECONSTRUCTED"
        selected = "capsule.json" if selection.startswith("BK05_") else "original_task.txt"
        (out / selected).write_bytes(payload)
        files = {p.relative_to(out).as_posix(): {"bytes": p.stat().st_size, "sha256": _sha(p.read_bytes())}
                 for p in out.rglob("*") if p.is_file()}
        manifest = {"input_sha256": fingerprint, "key": key, "files": files,
                    "selection": selection, "selected": selected, "reason": reason,
                    "control_receipt": "control/receipt.json", "code_sha256": code,
                    "missing_inputs": result["missing_inputs"], "held_basis": held,
                    "qualification": "LOCAL_STRUCTURAL_ONLY_NOT_PROVIDER_OR_SEMANTIC_ATTESTATION",
                    "recipient_use": "NOT_OBSERVED", "dispatch": "NOT_SENT", "authority": "NONE"}
        with (out / "manifest.json").open("xb") as stream:
            stream.write(_canonical(manifest))
            stream.flush()
            os.fsync(stream.fileno())
        return {"state": "PREPARED_LOCAL", "manifest": str(out / "manifest.json"),
                "selection": selection, "authority": "NONE"}
    except BaseException:
        shutil.rmtree(out)  # Only this call's exclusively created local output; never caller inputs.
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    pre = sub.add_parser("presend")
    for name in ("request", "original", "capture-root"):
        pre.add_argument("--" + name, required=True)
    final = sub.add_parser("finalize")
    for name in ("capture-record", "return-input", "output-root"):
        final.add_argument("--" + name, required=True)
    for name in SIDEFILES:
        final.add_argument("--" + name.replace("_", "-"))
    args = vars(parser.parse_args())
    action = args.pop("action")
    try:
        result = (presend if action == "presend" else finalize)(
            **{key: value for key, value in args.items() if value is not None})
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, ImportError) as exc:
        # Do not leak raw payloads, private paths or exception messages to stdout.
        reason = str(exc) if isinstance(exc, ValueError) and str(exc).replace("_", "").isalnum() else type(exc).__name__
        print(json.dumps({"state": "REFUSED", "reason": reason, "authority": "NONE"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
