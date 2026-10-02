"""Owner side: owner event -> D0 frame -> transport, with a write-ahead SEND LEDGER enforcing UNKNOWN_SEND => NO_REPLAY.

Sender receipts reuse v0.1 ReceiptTrail (PRODUCED, then TRANSPORT_ACCEPTED only when the transport proved it).
The ledger is evidence for replay-safety, not truth. A re-announcement of a referent is a NEW owner event with a
new event_id (the owner's decision), never a replay of an unknown-outcome send.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from . import _reference as ref
from .d0 import build_frame
from .model import SensingError
from .owner_event import accept_owner_event
from .store import JsonlStore
from .transport import ACCEPTED, NOT_SENT, UNKNOWN_SEND, NotSentError, Transport, TransportResult

INTENDED = "INTENDED"
IN_FLIGHT = "IN_FLIGHT"
CRASH_REASON = "CRASH_BETWEEN_INTENT_AND_OUTCOME"


class SendLedger:
    def __init__(self, path: Path | str | None = None) -> None:
        self._store = JsonlStore(path, name="sender-ledger")
        self._state: dict[str, dict[str, Any]] = {}
        for rec in self._store.records():
            self._apply(rec)
        for event_id, st in list(self._state.items()):
            if st["state"] == INTENDED:                         # write-ahead intent without outcome => unknown
                self.outcome(event_id, UNKNOWN_SEND, CRASH_REASON)

    def close(self) -> None:
        self._store.close()

    @property
    def is_durable(self) -> bool:
        """A sender may publish only with file-backed, fsynced pre-send custody.

        The pathless ledger remains useful for pure ledger tests, but it cannot
        guard a transport call across process restart.
        """
        return self._store.path is not None

    def _apply(self, rec: dict[str, Any]) -> None:
        if rec.get("kind") == "INTENT":
            self._state[rec["event_id"]] = {"state": INTENDED, "fingerprint": rec["fingerprint"], "reason": None,
                                            "attempts": self._state.get(rec["event_id"], {}).get("attempts", 0) + 1}
        elif rec.get("kind") == "OUTCOME" and rec["event_id"] in self._state:
            self._state[rec["event_id"]].update(state=rec["state"], reason=rec["reason"])

    def state(self, event_id: str) -> str | None:
        st = self._state.get(event_id)
        return st["state"] if st else None

    def fingerprint(self, event_id: str) -> str | None:
        st = self._state.get(event_id)
        return st["fingerprint"] if st else None

    def intend(self, event_id: str, fingerprint: str) -> None:
        rec = {"kind": "INTENT", "event_id": event_id, "fingerprint": fingerprint}
        self._store.append(rec)
        self._apply(rec)

    def outcome(self, event_id: str, state: str, reason: str) -> None:
        rec = {"kind": "OUTCOME", "event_id": event_id, "state": state, "reason": reason}
        self._store.append(rec)
        self._apply(rec)


@dataclass(frozen=True)
class SendOutcome:
    event_id: str
    state: str                       # ACCEPTED | NOT_SENT | UNKNOWN_SEND | IN_FLIGHT (a concurrent send of the same event)
    reason: str
    replay_refused: bool
    stages: tuple[str, ...]          # v0.1 receipt stages actually reached on the sender side (<= TRANSPORT_ACCEPTED)


class SensingSender:
    def __init__(self, *, transport: Transport, ledger: SendLedger, clock: Callable[[], float] = time.time,
                 ttl_seconds: int = 30) -> None:
        if not isinstance(ledger, SendLedger) or not ledger.is_durable:
            raise SensingError("DURABLE_SEND_LEDGER_REQUIRED", "publishing needs fsynced pre-send custody")
        self._transport, self._ledger, self._clock, self._ttl = transport, ledger, clock, ttl_seconds
        self._lock = threading.RLock()     # short sections only: never held across publish()
        self._inflight: set[str] = set()

    def send(self, raw_event: Mapping[str, Any]) -> SendOutcome:
        if not self._ledger.is_durable:
            raise SensingError("DURABLE_SEND_LEDGER_REQUIRED", "publishing needs fsynced pre-send custody")
        ev = accept_owner_event(raw_event)                       # commit+readback gate happens here
        built = build_frame(ev, now_epoch=self._clock(), ttl_seconds=self._ttl)
        with self._lock:                                          # check + write-ahead intent: short, atomic
            prior = self._ledger.state(ev.event_id)
            if prior is not None:
                if self._ledger.fingerprint(ev.event_id) != built.fingerprint:
                    raise SensingError("EVENT_ID_REUSED_WITH_DIFFERENT_CONTENT", ev.event_id)
                if ev.event_id in self._inflight:                 # another thread (or a re-entrant call) is publishing it
                    return SendOutcome(ev.event_id, IN_FLIGHT, "REPLAY_REFUSED_SEND_IN_FLIGHT", True, ("PRODUCED",))
                if prior in (ACCEPTED, UNKNOWN_SEND, INTENDED):   # INTENDED = an attempt whose outcome was never recorded
                    state = ACCEPTED if prior == ACCEPTED else UNKNOWN_SEND
                    stages = ("PRODUCED", "TRANSPORT_ACCEPTED") if state == ACCEPTED else ("PRODUCED",)
                    return SendOutcome(ev.event_id, state, "REPLAY_REFUSED_NO_SECOND_PUBLISH", True, stages)
            trail = ref.ReceiptTrail(ev.event_id, ev.owner_ref, "sensing-runtime-v0.1")
            trail.emit("PRODUCED")
            self._ledger.intend(ev.event_id, built.fingerprint)   # write-ahead: crash after this => UNKNOWN_SEND
            self._inflight.add(ev.event_id)
        try:
            try:
                result = self._transport.publish(built.subject, built.data)   # lock NOT held: handlers may re-enter send()
            except NotSentError:
                result = TransportResult(NOT_SENT, "TRANSPORT_PROVED_NOT_WRITTEN")
            except Exception:  # noqa: BLE001 - a raising transport may have written: unknown, never retried
                result = TransportResult(UNKNOWN_SEND, "TRANSPORT_RAISED_WRITE_STATE_UNPROVEN")
            with self._lock:
                self._ledger.outcome(ev.event_id, result.state, result.reason)
        finally:
            with self._lock:
                self._inflight.discard(ev.event_id)
        if result.state == ACCEPTED:
            trail.emit("TRANSPORT_ACCEPTED")
        return SendOutcome(ev.event_id, result.state, result.reason, False, tuple(trail.stages_reached))
