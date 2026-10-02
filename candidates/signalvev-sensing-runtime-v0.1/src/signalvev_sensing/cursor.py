"""8. EVIDENCE / CURSOR (receiver side): dedupe + highwater state, rebuilt by replaying an evidence-only log.

Not canonical truth: it records that an event id/fingerprint was SEEN and what typed outcome it got, so a restart
cannot turn a duplicate into new work. It never stores state text. Only VERIFIED frames (schema/identity/TTL ok,
applicable) are ever claimed -- an invalid or expired frame can never poison dedupe for the real event.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .model import DISPOSITIONS, HOLD_UNREADABLE
from .store import STORE_SCHEMA, JsonlStore, StoreCorrupt

PENDING = "PENDING"
RECOVERED = "RECOVERED_PENDING_OUTCOME_UNKNOWN"
MAX_CONFLICTS_PER_EVENT = 4
MAX_CONFLICTS_TOTAL = 10_000


class DedupeCursor:
    def __init__(self, path: Path | str | None = None) -> None:
        self._store = JsonlStore(path, name="receiver-cursor")
        self._events: dict[str, dict[str, Any]] = {}
        self._idem: dict[str, str] = {}
        self._high: dict[str, dict[str, Any]] = {}
        self._conflicts: set[tuple[str, str]] = set()
        self._conflicts_per_event: dict[str, int] = {}
        self.recovered_pending = 0
        self.write_failures = 0
        records = self._store.records()
        if records and records[0].get("schema") != STORE_SCHEMA:
            raise StoreCorrupt(f"cursor store schema {records[0].get('schema')!r} != {STORE_SCHEMA!r}")
        for rec in records:
            try:
                self._apply(rec)
            except (KeyError, TypeError):
                raise StoreCorrupt(f"cursor record of an incompatible format: {rec.get('kind')!r}") from None
        for event_id, ev in list(self._events.items()):          # crash between claim and finalize => typed hold
            if ev["disposition"] == PENDING:
                self.recovered_pending += 1
                self.finalize(event_id, HOLD_UNREADABLE, RECOVERED)

    def close(self) -> None:
        self._store.close()

    @property
    def repaired_tail(self) -> bool:
        return self._store.repaired_tail

    def _apply(self, rec: dict[str, Any]) -> None:
        kind = rec.get("kind")
        if kind == "CLAIM":
            self._events[rec["event_id"]] = {"event_id": rec["event_id"], "fingerprint": rec["fingerprint"],
                                             "d0_hash": rec["d0_hash"], "surfaced": False,
                                             "idem_key": rec["idem_key"], "referent_key": rec["referent_key"],
                                             "owner_seq": rec["owner_seq"], "disposition": PENDING, "reason": None}
            self._idem[rec["idem_key"]] = rec["event_id"]
        elif kind == "FINAL" and rec["event_id"] in self._events:
            ev = self._events[rec["event_id"]]
            ev["disposition"], ev["reason"] = rec["disposition"], rec["reason"]
            if rec["disposition"] in ("CONFLICT_HOLD", "HOLD_IDENTITY") and self._idem.get(ev["idem_key"]) == ev["event_id"]:
                del self._idem[ev["idem_key"]]      # an owner-REFUTED claim must not hold the idempotency scope hostage
            hw = self._high.get(ev["referent_key"])
            # Highwater advances ONLY on an owner-confirmed read AND owner-confirmed seq: an unverified signal (or a
            # forged owner_seq on a real revision) can never decide what is stale.
            if rec["disposition"] == "ACK_READ" and rec["seq_confirmed"] and (hw is None or ev["owner_seq"] > hw["owner_seq"]):
                self._high[ev["referent_key"]] = {"owner_seq": ev["owner_seq"], "fingerprint": ev["fingerprint"],
                                                  "event_id": ev["event_id"]}
        elif kind == "SURFACED" and rec["event_id"] in self._events:
            self._events[rec["event_id"]]["surfaced"] = True
        elif kind == "CONFLICT":
            self._conflicts.add((rec["event_id"], rec["seen_d0_hash"]))
            self._conflicts_per_event[rec["event_id"]] = self._conflicts_per_event.get(rec["event_id"], 0) + 1

    def _append(self, rec: dict[str, Any], *, strict: bool) -> None:
        """strict (claim): a write failure RAISES, so no work is done on state that cannot be made durable.
        best-effort (finalize/surfaced/conflict): memory stays authoritative for this process, the failure is counted,
        and after a restart the missing FINAL becomes the typed RECOVERED hold (conservative direction)."""
        try:
            self._store.append(rec)
        except Exception:  # noqa: BLE001
            self.write_failures += 1
            if strict:
                raise
        self._apply(rec)

    def event_count(self) -> int:
        return len(self._events)

    def get(self, event_id: str) -> dict[str, Any] | None:
        ev = self._events.get(event_id)
        return dict(ev) if ev else None

    def by_idem(self, idem_key: str) -> dict[str, Any] | None:
        eid = self._idem.get(idem_key)
        return self.get(eid) if eid else None

    def highwater(self, referent_key: str) -> dict[str, Any] | None:
        hw = self._high.get(referent_key)
        return dict(hw) if hw else None

    def claim(self, *, event_id: str, fingerprint: str, d0_hash: str, idem_key: str, referent_key: str,
              owner_seq: int) -> None:
        self._append({"kind": "CLAIM", "event_id": event_id, "fingerprint": fingerprint, "d0_hash": d0_hash,
                      "idem_key": idem_key, "referent_key": referent_key, "owner_seq": owner_seq}, strict=True)

    def mark_surfaced(self, event_id: str) -> None:
        self._append({"kind": "SURFACED", "event_id": event_id}, strict=False)

    def finalize(self, event_id: str, disposition: str, reason: str, *, seq_confirmed: bool = False) -> None:
        assert disposition in DISPOSITIONS
        self._append({"kind": "FINAL", "event_id": event_id, "disposition": disposition, "reason": reason,
                      "seq_confirmed": bool(seq_confirmed)}, strict=False)

    def note_conflict(self, event_id: str, seen_d0_hash: str) -> bool:
        """Record a conflicting frame. True only the first time this (event, d0 hash) conflict is seen."""
        if (event_id, seen_d0_hash) in self._conflicts:
            return False
        if (self._conflicts_per_event.get(event_id, 0) >= MAX_CONFLICTS_PER_EVENT
                or len(self._conflicts) >= MAX_CONFLICTS_TOTAL):
            return False                                    # flood: still CONFLICT_HOLD, but no more evidence/closures
        self._append({"kind": "CONFLICT", "event_id": event_id, "seen_d0_hash": seen_d0_hash}, strict=False)
        return True
