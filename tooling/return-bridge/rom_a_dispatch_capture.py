#!/usr/bin/env python3
"""Freeze a Rom A dispatch's exact producer bytes before the caller sends it.

This is local evidence, not a sender, identity oracle, review service, or BK05
decision. The caller must pass the original UTF-8 prompt bytes, never a parsed
chat turn or reconstructed start post. A later recipient-use receipt is separate.
File fsync and local rename/readback do not attest crash-durable parent metadata.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any


SCHEMA = "cerebro-rom-a-dispatch-capture/v1"
PUBLISH_DURABILITY = "LOCAL_READBACK_ONLY_CRASH_DURABILITY_UNPROVEN"
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
            "original_path": str(original_path), "receipt": observed,
            "publish_durability": PUBLISH_DURABILITY}


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
        "publish_durability": PUBLISH_DURABILITY,
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


OWNER_EPISODE_SCHEMA = "cerebro-rom-a-owner-episode/v1"
OWNER_EPISODE_DURABILITY = "LOCAL_FSYNC_RENAME_CRASH_DURABILITY_UNPROVEN"


@contextmanager
def _episode_lock(path: Path):
    """Serialize one existing capture episode; no cross-task registry is created."""
    if path.is_symlink():
        raise ValueError("OWNER_EPISODE_LOCK_SYMLINK")
    with path.open("a+b") as stream:
        stream.seek(0)
        if not stream.read(1):
            stream.seek(0); stream.write(b"1"); stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _replace_episode_state(path: Path, state: dict[str, Any]) -> None:
    raw = _canonical(state) + b"\n"
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".owner-state-", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        if path.read_bytes() != raw:
            raise ValueError("OWNER_EPISODE_READBACK_MISMATCH")
    finally:
        temporary.unlink(missing_ok=True)


class BoundRomAOwnerEpisodePort:
    """A1-owned episode extension; old captures remain non-authoritative.

    Authentication, the Human mandate and material dependency are read from
    constructor-bound owner ports on every operation. This local file is only
    a revisioned continuation/reservation record, never a source of authority.
    """

    def __init__(self, *, capture_record: Path, owner_identity_reader: Any,
                 mandate_reader: Any, dependency_reader: Any):
        for port, method in ((owner_identity_reader, "read_current"),
                             (mandate_reader, "read_current"),
                             (dependency_reader, "read_current")):
            if not callable(getattr(port, method, None)):
                raise ValueError("OWNER_EPISODE_TRUSTED_READER_UNBOUND")
        self.capture_record = Path(capture_record)
        if self.capture_record.name != "capture.json" or not self.capture_record.is_file():
            raise ValueError("OWNER_EPISODE_EXACT_CAPTURE_RECORD_REQUIRED")
        self.episode = self.capture_record.parent
        self.capture_root = self.episode.parent
        self.owner_identity_reader = owner_identity_reader
        self.mandate_reader = mandate_reader
        self.dependency_reader = dependency_reader

    def _capture(self, task_ref: str, task_revision: str) -> tuple[Path, dict[str, Any], bytes]:
        key = _sha(_canonical([task_ref, task_revision]))
        episode = self.episode
        if (self.capture_root.is_symlink() or episode.is_symlink()
                or not episode.is_dir() or episode.name != "capture-" + key):
            raise ValueError("OWNER_EPISODE_CAPTURE_MISSING")
        record_path, original_path = episode / "capture.json", episode / "original_task.txt"
        if record_path.is_symlink() or original_path.is_symlink():
            raise ValueError("OWNER_EPISODE_CAPTURE_SYMLINK")
        record = json.loads(record_path.read_bytes(), object_pairs_hook=_unique_pairs)
        task = record.get("task")
        if (not isinstance(task, dict) or task.get("task_ref") != task_ref
                or task.get("task_revision") != task_revision):
            raise ValueError("OWNER_EPISODE_CAPTURE_TASK_MISMATCH")
        _validate_request({"schema": SCHEMA, "task": task,
                           "provenance": {key: record["provenance"][key]
                                          for key in ("status", "original_dispatch_ref")},
                           "semantic_review": {key: record["semantic_review"][key]
                                               for key in ("status", "receipt_ref")}})
        original = original_path.read_bytes()
        expected = {
            "schema": SCHEMA + "-receipt", "capture_key": key, "task": task,
            "original_sha256": _sha(original), "original_bytes": len(original),
            "provenance": {**{name: record["provenance"][name]
                              for name in ("status", "original_dispatch_ref")},
                           "assertion": "CALLER_SUPPLIED_NOT_VERIFIED"},
            "semantic_review": {**{name: record["semantic_review"][name]
                                 for name in ("status", "receipt_ref")},
                                "assertion": "CALLER_SUPPLIED_NOT_VERIFIED"},
            "identity_currentness": "CALLER_SUPPLIED_NOT_PROVIDER_VERIFIED",
            "capture_stage": "LOCAL_CAPTURE_TIMING_NOT_PROVIDER_VERIFIED",
            "publish_durability": PUBLISH_DURABILITY, "authority": "NONE",
            "effect": "NONE_CLAIMED", "work_consumed": False,
            "recipient_read_or_use_proven": False,
        }
        _readback(episode, expected)
        return episode, expected, original

    def _owners(self, mandate_ref: str, dependency_ref: str) -> tuple[dict, dict, dict]:
        identity = self.owner_identity_reader.read_current()
        mandate = self.mandate_reader.read_current(mandate_ref=mandate_ref)
        dependency = self.dependency_reader.read_current(dependency_ref=dependency_ref)
        if (not all(isinstance(value, dict) for value in (identity, mandate, dependency))
                or identity.get("authenticated") is not True
                or identity.get("owner_ref") != "A1"
                or identity.get("currentness") != "CURRENT"
                or mandate.get("owner_ref") != identity["owner_ref"]
                or mandate.get("mandate_ref") != mandate_ref
                or mandate.get("authenticated_readback") is not True
                or mandate.get("currentness") != "CURRENT"
                or mandate.get("human_authorized") is not True
                or not isinstance(mandate.get("human_provenance_ref"), str)
                or not mandate["human_provenance_ref"]
                or dependency.get("dependency_ref") != dependency_ref
                or dependency.get("readback_verified") is not True
                or dependency.get("disposition") != "SOURCE_AFTER_DEPENDENCY"
                or dependency.get("currentness") != "CURRENT"):
            raise ValueError("OWNER_EPISODE_CURRENT_HUMAN_AND_DEPENDENCY_READBACK_REQUIRED")
        return identity, mandate, dependency

    def provision(self, *, task_ref: str, task_revision: str, dependency_ref: str,
                  dependency_revision: str, mandate_ref: str,
                  expected_episode_revision: int = 0) -> dict[str, Any]:
        """Explicit A1 ingress; a legacy capture is never silently promoted."""
        episode, capture, _ = self._capture(task_ref, task_revision)
        identity, mandate, dependency = self._owners(mandate_ref, dependency_ref)
        if (mandate.get("task_ref") != task_ref
                or mandate.get("task_revision") != task_revision
                or mandate.get("actor_ref") != capture["task"]["actor_ref"]
                or mandate.get("scope_ref") != capture["task"]["live_scope"]
                or dependency.get("revision") != dependency_revision
                or not isinstance(dependency.get("source_ref"), str)
                or not dependency["source_ref"]
                or type(expected_episode_revision) is not int
                or expected_episode_revision < 0):
            raise ValueError("OWNER_EPISODE_EXACT_MANDATE_OR_DEPENDENCY_MISMATCH")
        path = episode / "owner-state.json"
        if path.is_symlink():
            raise ValueError("OWNER_EPISODE_STATE_SYMLINK")
        with _episode_lock(episode / "owner-state.lock"):
            previous = json.loads(path.read_bytes(), object_pairs_hook=_unique_pairs) if path.exists() else None
            prior_revision = previous["episode_revision"] if isinstance(previous, dict) else 0
            if prior_revision != expected_episode_revision:
                raise ValueError("OWNER_EPISODE_REVISION_CONFLICT")
            if previous is not None and (previous.get("task_ref"), previous.get("task_revision")) != (task_ref, task_revision):
                raise ValueError("OWNER_EPISODE_TASK_IMMUTABLE")
            if previous is not None and previous.get("reservation") is not None and all(
                previous.get(name) == value for name, value in (
                    ("mandate_revision", mandate["revision"]),
                    ("dependency_revision", dependency_revision),
                    ("dependency_source_ref", dependency["source_ref"]),
                )
            ):
                raise ValueError("OWNER_EPISODE_SAME_BASIS_RESERVATION_CANNOT_RESET")
            state = {
                "schema": OWNER_EPISODE_SCHEMA, "task_ref": task_ref,
                "task_revision": task_revision, "capture_key": capture["capture_key"],
                "selected_sha256": capture["original_sha256"],
                "owner_ref": identity["owner_ref"], "mandate_ref": mandate_ref,
                "mandate_revision": mandate["revision"],
                "human_provenance_ref": mandate["human_provenance_ref"],
                "dependency_ref": dependency_ref,
                "dependency_revision": dependency_revision,
                "dependency_source_ref": dependency["source_ref"],
                "episode_revision": prior_revision + 1,
                "reservation": None,
                "authority": "HUMAN_MANDATE_REREAD_REQUIRED",
            }
            _replace_episode_state(path, state)
        return {"state": "PROVISIONED_READBACK", "episode_revision": state["episode_revision"],
                "task_ref": task_ref, "dependency_ref": dependency_ref,
                "authority_added": "NONE", "provider_verified_claim": False,
                "persistence_ceiling": OWNER_EPISODE_DURABILITY}

    def _state(self, task_ref: str, dependency_ref: str) -> tuple[Path, dict, dict, bytes]:
        if not isinstance(task_ref, str) or not isinstance(dependency_ref, str):
            raise ValueError("OWNER_EPISODE_REFS_REQUIRED")
        path = self.episode / "owner-state.json"
        if not path.is_file() or path.is_symlink():
            raise ValueError("OWNER_EPISODE_NOT_PROVISIONED")
        state = json.loads(path.read_bytes(), object_pairs_hook=_unique_pairs)
        if state.get("task_ref") != task_ref or state.get("dependency_ref") != dependency_ref:
            raise ValueError("OWNER_EPISODE_EXACT_TARGET_MISMATCH")
        capture_dir, capture, original = self._capture(task_ref, state["task_revision"])
        if capture_dir != self.episode or state.get("schema") != OWNER_EPISODE_SCHEMA or \
                state.get("capture_key") != capture["capture_key"] or \
                state.get("selected_sha256") != capture["original_sha256"]:
            raise ValueError("OWNER_EPISODE_STATE_CAPTURE_MISMATCH")
        return path, state, capture, original

    def read_current(self, *, task_ref: str, dependency_ref: str) -> dict[str, Any]:
        path, state, capture, original = self._state(task_ref, dependency_ref)
        _, mandate, dependency = self._owners(state["mandate_ref"], dependency_ref)
        if (mandate.get("revision") != state["mandate_revision"]
                or mandate.get("human_provenance_ref") != state["human_provenance_ref"]
                or mandate.get("task_ref") != task_ref
                or mandate.get("task_revision") != state["task_revision"]
                or mandate.get("actor_ref") != capture["task"]["actor_ref"]
                or dependency.get("revision") != state["dependency_revision"]
                or dependency.get("source_ref") != state["dependency_source_ref"]):
            raise ValueError("OWNER_EPISODE_STALE_MANDATE_OR_DEPENDENCY")
        reservation = state.get("reservation")
        prior = None if not isinstance(reservation, dict) else {
            "basis_fingerprint": reservation["basis_fingerprint"],
            "result": reservation.get("result", "RESERVED_UNCERTAIN")}
        return {
            "owner_readback_verified": True, "currentness": "CURRENT",
            "task": {"task_ref": task_ref, "task_revision": state["task_revision"],
                     "actor_ref": mandate["actor_ref"],
                     "authority_ref": state["human_provenance_ref"],
                     "authority_state": "AUTHORIZED" if mandate.get("human_authorized") is True else "REVOKED",
                     "scope_ref": mandate["scope_ref"],
                     "progress_state": mandate["progress_state"],
                     "paused": mandate["paused"], "revoked": mandate["revoked"],
                     "dependency_refs": mandate["dependency_refs"],
                     "all_dependencies_resolved": mandate["all_dependencies_resolved"],
                     "other_unresolved_gates": mandate["other_unresolved_gates"],
                     "selected_bytes": original, "selected_sha256": state["selected_sha256"],
                     "preflight_request": mandate["preflight_request"]},
            "dependency": {"dependency_ref": dependency_ref,
                           "dependency_revision": state["dependency_revision"],
                           "source_ref": state["dependency_source_ref"],
                           "disposition": dependency["disposition"],
                           "state": dependency["state"],
                           "applicable": dependency["applicable"]},
            "prior_reconsideration": prior,
            "episode_revision": state["episode_revision"],
            "episode_state_path": str(path),
            "authority_added": "NONE", "provider_verified_claim": False,
            "persistence_ceiling": OWNER_EPISODE_DURABILITY,
        }

    def reserve_reconsideration(self, *, task_ref: str, task_revision: str,
                                dependency_ref: str, dependency_revision: str,
                                basis_fingerprint: str) -> dict[str, Any]:
        path, state, _, _ = self._state(task_ref, dependency_ref)
        with _episode_lock(path.with_name("owner-state.lock")):
            latest = json.loads(path.read_bytes(), object_pairs_hook=_unique_pairs)
            if (latest.get("episode_revision") != state["episode_revision"]
                    or latest.get("task_revision") != task_revision
                    or latest.get("dependency_revision") != dependency_revision
                    or latest.get("reservation") is not None):
                return {"state": "STALE_OR_ALREADY_RESERVED"}
            current = self.read_current(task_ref=task_ref, dependency_ref=dependency_ref)
            if current["episode_revision"] != latest["episode_revision"]:
                return {"state": "STALE_OR_ALREADY_RESERVED"}
            latest["reservation"] = {"basis_fingerprint": basis_fingerprint,
                                     "result": "RESERVED_UNCERTAIN"}
            latest["episode_revision"] += 1
            _replace_episode_state(path, latest)
        return {"state": "RESERVED", "basis_fingerprint": basis_fingerprint,
                "task_revision": task_revision, "dependency_revision": dependency_revision}

    def record_reconsideration_result(self, *, task_ref: str, dependency_ref: str,
                                      basis_fingerprint: str, result: str,
                                      delivery_ref: str | None) -> dict[str, Any]:
        path, _, _, _ = self._state(task_ref, dependency_ref)
        with _episode_lock(path.with_name("owner-state.lock")):
            latest = json.loads(path.read_bytes(), object_pairs_hook=_unique_pairs)
            reservation = latest.get("reservation")
            if (not isinstance(reservation, dict)
                    or reservation.get("basis_fingerprint") != basis_fingerprint
                    or reservation.get("result") != "RESERVED_UNCERTAIN"
                    or result not in {"SENT_ACCEPTED", "SEND_UNCERTAIN"}
                    or (result == "SENT_ACCEPTED" and not delivery_ref)):
                return {"state": "RESERVATION_RESULT_CONFLICT"}
            reservation.update(result=result, delivery_ref=delivery_ref)
            latest["episode_revision"] += 1
            _replace_episode_state(path, latest)
        return {"state": "RECORDED", "basis_fingerprint": basis_fingerprint}


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
                          "publish_durability": result["publish_durability"],
                          "authority": "NONE"}, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        print(json.dumps({"state": "REFUSED", "reason": str(exc).split(":", 1)[0]}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
