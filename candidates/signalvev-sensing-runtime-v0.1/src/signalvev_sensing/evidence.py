"""8. FLIGHT RECORDER (evidence only) + first-broken-edge explanation via v0.9. Ids/hashes/codes only; no state text."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from . import _reference as ref
from .store import JsonlStore


class FlightRecorder:
    MAX_PER_KIND = 10_000        # beyond this a kind degrades to a counter: floods cannot grow the evidence log
    MAX_BY_KIND = {"INGRESS_REJECT": 1_000}   # rejects are the peer-controlled flood vector: smaller budget

    def __init__(self, path: Path | str | None = None, *, max_per_kind: int | None = None) -> None:
        self._store = JsonlStore(path, name="flight-recorder")
        self.counters: Counter[str] = Counter()
        self._max_override = max_per_kind
        # Rebuilt from the file, so the budget bounds the FILE across restarts, not just one process.
        self._written: Counter[str] = Counter(r["kind"] for r in self._store.records() if r.get("kind") != "HEADER")

    def _max_for(self, kind: str) -> int:
        return self._max_override if self._max_override is not None else self.MAX_BY_KIND.get(kind, self.MAX_PER_KIND)

    def close(self) -> None:
        self._store.close()

    def record(self, kind: str, **fields: Any) -> None:
        if self._written[kind] >= self._max_for(kind):
            self.counters[f"{kind}_SUPPRESSED"] += 1
            return
        self._store.append({"kind": kind, **fields})
        self._written[kind] += 1

    def count(self, key: str) -> None:
        """Counter-only path (no per-frame write): used for NOT_APPLICABLE so irrelevant traffic leaves no log trail."""
        self.counters[key] += 1

    def records(self) -> list[dict[str, Any]]:
        return self._store.records()


def reconstruct_edges(event_id: str, *, send_ledger=None, cursor=None) -> dict[str, Any]:
    """Explain 'what happened to event X' with v0.9's FirstBrokenEdgeRecorder, but only when BOTH sides are visible.
    One side alone cannot see the other side's receipts; claiming BROKEN from a half view would fabricate a break."""
    observed: set[str] = set()
    if send_ledger is not None and send_ledger.state(event_id) is not None:
        observed.add("PRODUCED")
        if send_ledger.state(event_id) == "ACCEPTED":
            observed.add("TRANSPORT_ACCEPTED")
    if cursor is not None:
        ev = cursor.get(event_id)
        if ev is not None:
            observed.add("DELIVERED")
            if ev["disposition"] == "ACK_READ":
                observed.add("READ")
    if send_ledger is None or cursor is None:
        return {"status": "PARTIAL_LOCAL_VIEW", "first_broken_edge": None, "reached_stages": tuple(sorted(observed))}
    return ref.FirstBrokenEdgeRecorder().reconstruct(observed)
