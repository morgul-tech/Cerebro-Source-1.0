"""Local, append-only, EVIDENCE-ONLY log of the receiver's selected typed closures (a `ReturnSink`).

Not a canonical writer: it never touches Drive or any owner/PM state. Records are flat ids/hashes/codes (the existing
JsonlStore refuses free text and nested state). ACK_READ != WORK_CONSUMED != EFFECT; each record says so explicitly.
"""
from __future__ import annotations

from pathlib import Path

from . import _bootstrap  # noqa: F401
from signalvev_sensing import ClosureRecord, JsonlStore


class ClosureLogSink:
    def __init__(self, path: Path | str) -> None:
        self._store = JsonlStore(path, name="closure-log")

    def deliver(self, closure: ClosureRecord) -> None:
        self._store.append({
            "kind": "CLOSURE", "closure_id": closure.closure_id, "event_id": closure.event_id,
            "owner_ref": closure.owner_ref, "referent_type": closure.referent_type, "referent_id": closure.referent_id,
            "revision_after": closure.revision_after, "disposition": closure.disposition, "reason": closure.reason,
            "receipt_stage": closure.receipt_stage, "observed_sha256": closure.observed_sha256,
            "activation": closure.activation.decision, "wake_bound": closure.activation.wake_bound,
            "way_home": list(closure.way_home), "work_consumed": closure.work_consumed, "effect": closure.effect,
            "authority": closure.authority, "is_truth_store": closure.is_truth_store})

    def close(self) -> None:
        self._store.close()
