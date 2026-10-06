"""Owner-side publication: outbox intent -> durable send claim -> JetStream publish (Nats-Msg-Id) -> PubAck recorded
-> optional wake.

The owner outbox (synthetic, durable) holds the intent and exact payload hash BEFORE publish. Before ANY transport
effect the attempt is atomically claimed (INTENDED -> SEND_CLAIMED, compare-and-set on state + claim token), so a
process that dies anywhere after the claim leaves a durable "may have been published" marker; only one publisher can
win a claim. Every recovered ambiguous attempt (SEND_CLAIMED / UNKNOWN_PENDING / RECONCILE_HOLD) is resolved only by
a bounded stream lookup of the stable id + exact raw digest; a retry is allowed only after a COMPLETE lookup proves
absence in a stream without removals AND the last claim is older than ``claim_stale_s`` (it cannot still be in
flight). Incomplete, unavailable or retention-limited lookups stay visibly unresolved -- never a blind second publish,
whatever the broker duplicate window. PubAck stream/sequence is recorded BEFORE an optional wake. Same id with changed
bytes is CONFLICT_HOLD. With ``config.enabled=False`` no public entry touches the transport or the publication state.
"""
from __future__ import annotations

import hashlib
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from .faults import Faults
from .owner import (OUTBOX_CAPACITY, OUTBOX_CONFLICT, OUTBOX_INTENDED, OUTBOX_PUBLISHED, OUTBOX_RECONCILE_HOLD,
                    OUTBOX_SEND_CLAIMED, OUTBOX_UNKNOWN, SyntheticProjectOwner)
from .pointer import iso, parse_ts
from .transport import CapacityRejected, LookupUnavailable, StreamTransport

D1_DISABLED = "D1_DISABLED_NO_BROKER_ACTIVITY"     # returned, never stored: default-off leaves the outbox untouched
CLAIM_NOT_DURABLE = "CLAIM_NOT_DURABLE"            # claim write failed: nothing sent, intent unchanged
CLAIM_SUPERSEDED = "CLAIM_SUPERSEDED"              # another process moved the row; this one wrote nothing
RECOVERABLE = (OUTBOX_SEND_CLAIMED, OUTBOX_UNKNOWN, OUTBOX_RECONCILE_HOLD)


class LookupBudget:
    """Caps transport waits and actual get_msg calls across one reconciliation."""

    def __init__(self, calls: int, seconds: float) -> None:
        self.remaining = calls
        self.deadline = time.monotonic() + seconds
        self.used = 0

    def can_cover(self, span: int) -> bool:
        return span <= self.remaining and self.remaining_seconds() > 0

    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def timeout(self) -> float:
        remaining = self.remaining_seconds()
        if remaining <= 0:
            raise LookupUnavailable("incomplete: deadline exhausted")
        return remaining

    def take(self) -> bool:
        if self.remaining <= 0 or self.remaining_seconds() <= 0:
            return False
        self.remaining -= 1
        self.used += 1
        return True


class OwnerOutboxPublisher:
    def __init__(self, *, owner: SyntheticProjectOwner, transport: StreamTransport, config: Any, faults: Faults,
                 counters: Any = None, wake: Callable[[str, int], None] | None = None,
                 clock: Callable[[], Any] | None = None) -> None:
        self._owner, self._t, self._cfg, self.faults = owner, transport, config, faults
        self._counters, self._wake, self._clock = counters, wake, clock
        self.publish_calls = 0          # actual transport publish calls by this instance
        self.lookup_calls = 0           # actual transport get_msg calls by this instance

    def _bump(self, name: str) -> None:
        if self._counters is not None:
            self._counters.bump(name)

    def _now(self) -> datetime:
        return self._clock() if self._clock is not None else datetime.now(timezone.utc)

    def new_budget(self) -> LookupBudget:
        return LookupBudget(self._cfg.lookup_bound, self._cfg.lookup_time_budget_s)

    # ------------------------------------------------------------------ publish
    def publish(self, message_id: str, *, budget: LookupBudget | None = None) -> str:
        """Publish ONE outbox intent (or recover an ambiguous attempt). Returns the resulting outbox state."""
        if not self._cfg.enabled:
            return D1_DISABLED
        rows = self._owner.outbox(message_id)
        if not rows:
            return "NO_OUTBOX_INTENT"
        row = rows[0]
        if row["state"] in RECOVERABLE:
            return self.reconcile_publication(message_id, budget=budget)     # never a blind second publish
        if row["state"] != OUTBOX_INTENDED:
            return row["state"]
        if hashlib.sha256(bytes(row["pointer"])).hexdigest() != row["pointer_sha256"]:
            self._owner.outbox_update(message_id, state=OUTBOX_CONFLICT, last_reason="outbox bytes changed")
            self._bump("conflict_rejects")
            return OUTBOX_CONFLICT
        self.faults.hit("publish.before_send")       # crash here = before the claim: intent stays INTENDED
        return self._claim_and_send(row, reason="CLAIMED_BEFORE_SEND")

    def _claim_and_send(self, row: dict[str, Any], *, reason: str) -> str:
        message_id = row["message_id"]
        token = uuid.uuid4().hex
        try:
            won = self._owner.outbox_transition(
                message_id, from_state=row["state"], from_token=row["claim_token"], state=OUTBOX_SEND_CLAIMED,
                claim_token=token, claimed_at=iso(self._now()), attempts=(row["attempts"] or 0) + 1,
                last_reason=reason)
        except Exception:
            self._bump("claim_write_failures")
            return CLAIM_NOT_DURABLE                  # durable claim not proven: zero transport calls
        if not won:
            self._bump("claim_lost")
            return self._owner.outbox(message_id)[0]["state"]     # another publisher owns this attempt
        pointer = bytes(row["pointer"])
        t0 = time.monotonic()
        try:
            self.faults.hit("publish.after_claim_before_send")
            self.publish_calls += 1
            ack = self._t.publish(self._cfg.subject, pointer, message_id, self._cfg.publish_timeout_s)
            self.faults.hit("publish.after_send_before_puback")   # PubAck lost after the broker stored it
        except CapacityRejected as exc:
            return self._finish(message_id, token, OUTBOX_CAPACITY, "capacity_rejects", last_reason=exc.reason)
        except Exception as exc:
            return self._finish(message_id, token, OUTBOX_UNKNOWN, "puback_unknown",
                                last_reason=f"puback:{type(exc).__name__}")
        latency_ms = (time.monotonic() - t0) * 1000.0
        if self._counters is not None:
            self._counters.sample("puback_latency_ms", latency_ms)
        if ack.duplicate:
            self._bump("duplicate_pubacks")
            stored = self._safe_get(ack.seq)
            if stored is None or hashlib.sha256(stored[0]).hexdigest() != row["pointer_sha256"]:
                return self._finish(message_id, token, OUTBOX_CONFLICT, "conflict_rejects",
                                    last_reason="broker duplicate id carries other bytes or is unreadable")
        state = self._finish(message_id, token, OUTBOX_PUBLISHED, None, stream=ack.stream, stream_seq=ack.seq,
                             duplicate=int(ack.duplicate), puback_latency_ms=latency_ms, last_reason="PUBACK")
        if state == OUTBOX_PUBLISHED and self._wake is not None:
            try:
                self._wake(message_id, ack.seq)                     # optional; losing it loses nothing
            except Exception:
                self._bump("lost_wakes")
        return state

    def _finish(self, message_id: str, token: str, state: str, counter: str | None, **fields: Any) -> str:
        """Settle OUR claim only (compare-and-set on SEND_CLAIMED + our token)."""
        if not self._owner.outbox_transition(message_id, from_state=OUTBOX_SEND_CLAIMED, from_token=token,
                                             state=state, **fields):
            self._bump("claim_superseded")
            return CLAIM_SUPERSEDED
        if counter:
            self._bump(counter)
        return state

    def _safe_get(self, seq: int):
        try:
            self.lookup_calls += 1
            return self._t.get_msg(seq)
        except Exception:
            return None

    # ------------------------------------------------------------------ reconciliation
    def reconcile_publication(self, message_id: str, *, budget: LookupBudget | None = None) -> str:
        """Bounded actual-stream lookup of (Nats-Msg-Id, exact digest) before any retry."""
        if not self._cfg.enabled:
            return D1_DISABLED
        rows = self._owner.outbox(message_id)
        if not rows:
            return "NO_OUTBOX_INTENT"
        row = rows[0]
        if row["state"] not in RECOVERABLE:
            return row["state"]
        budget = budget or self.new_budget()

        def hold(reason: str) -> str:
            ok = self._owner.outbox_transition(message_id, from_state=row["state"], from_token=row["claim_token"],
                                               state=OUTBOX_RECONCILE_HOLD, last_reason=reason)
            if not ok:
                return self._owner.outbox(message_id)[0]["state"]
            self._bump("reconcile_holds")
            return OUTBOX_RECONCILE_HOLD

        try:
            st = self._t.stream_state(timeout=budget.timeout())
            if budget.remaining_seconds() <= 0:
                raise LookupUnavailable("incomplete: deadline exceeded after stream state")
            lo = max(st["first_seq"], 1)                          # an empty real stream reports first_seq 0
            span = max(0, st["last_seq"] - lo + 1) if st["messages"] > 0 else 0
            if st["messages"] > span or not budget.can_cover(span):
                raise LookupUnavailable(f"incomplete: span={span} messages={st['messages']} "
                                        f"budget_calls={budget.remaining}")
            found = None
            for seq in range(lo, lo + span):
                if not budget.take():
                    raise LookupUnavailable(f"incomplete: budget exhausted at seq {seq}")
                self.lookup_calls += 1
                got = self._t.get_msg(seq, timeout=budget.timeout())
                if budget.remaining_seconds() <= 0:
                    raise LookupUnavailable(f"incomplete: deadline exceeded after seq {seq}")
                if got is not None and got[1].get("Nats-Msg-Id") == message_id:
                    found = (seq, got[0])
                    break
        except LookupUnavailable as exc:
            return hold(f"lookup:{exc}")
        if budget.remaining_seconds() <= 0:
            return hold("lookup:incomplete: deadline exhausted before disposition")
        if found is not None:
            if hashlib.sha256(found[1]).hexdigest() != row["pointer_sha256"]:
                ok = self._owner.outbox_transition(message_id, from_state=row["state"], from_token=row["claim_token"],
                                                   state=OUTBOX_CONFLICT, last_reason="same id, other bytes")
                if ok:
                    self._bump("conflict_rejects")
                    return OUTBOX_CONFLICT
                return self._owner.outbox(message_id)[0]["state"]
            ok = self._owner.outbox_transition(message_id, from_state=row["state"], from_token=row["claim_token"],
                                               state=OUTBOX_PUBLISHED, stream=self._cfg.stream, stream_seq=found[0],
                                               duplicate=0, last_reason="RECONCILED_BY_LOOKUP")
            if ok:
                self._bump("reconciled_by_lookup")
                return OUTBOX_PUBLISHED
            return self._owner.outbox(message_id)[0]["state"]
        no_removals = st["last_seq"] == 0 or (st["first_seq"] <= 1 and
                                              st["messages"] == st["last_seq"] - st["first_seq"] + 1)
        if not no_removals:
            return hold("absent but stream has removals: retention prevents proof")
        if self._clock is not None and self._now() >= parse_ts(row["expires_at"]):
            return hold("proven absent but expired")
        claimed_at = row["claimed_at"]
        if claimed_at and (self._now() - parse_ts(claimed_at)).total_seconds() < self._cfg.claim_stale_s:
            # Absent now, but the last claimed attempt may still land: do not race it. Visible, unchanged state.
            self._bump("absent_claim_not_stale")
            return row["state"]
        # Proven absent (complete lookup, no removals, last claim stale): the single retry, same stable id.
        if budget.remaining_seconds() <= 0:
            return hold("lookup:incomplete: deadline exhausted before retry")
        self._bump("proven_absent_republish")
        return self._claim_and_send(row, reason="PROVEN_ABSENT_BY_LOOKUP")
