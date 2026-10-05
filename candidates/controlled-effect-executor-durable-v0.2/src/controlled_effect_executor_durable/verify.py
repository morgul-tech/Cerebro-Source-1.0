"""Provenance verification for the durable ledger: V0.1's full recomputation plus the V0.2 NO_COMMIT requirement."""
from __future__ import annotations

from controlled_effect_executor.receipts import Provenance, verify_provenance

from .settlement import SETTLED_ABSENT


def verify_provenance_durable(p: Provenance) -> tuple:
    """Returns problem codes (empty == intact). Proves ledger CONSISTENCY (hash chain, legal edges, a resolved state
    derived from its classification) and that every NO_COMMIT carries explicit SETTLED_ABSENT evidence. It does not
    prove that a readback was genuine: there are no signatures in this candidate."""
    problems = list(verify_provenance(p))
    for i, ev in enumerate(p.events, start=1):
        if ev.event_kind == "RECONCILIATION" and ev.state_after == "NO_COMMIT":
            d = dict(ev.detail)
            if d.get("settlement_status") != SETTLED_ABSENT or not d.get("settlement_digest"):
                problems.append(f"EVENT_NO_COMMIT_WITHOUT_SETTLEMENT:{i}")
    return tuple(problems)
