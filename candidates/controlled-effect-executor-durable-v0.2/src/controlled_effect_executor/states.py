"""The closed state vocabulary and the only legal transitions of an admission's progress."""
from __future__ import annotations

FENCED = "FENCED"
IN_FLIGHT = "IN_FLIGHT"
COMMITTED_READBACK = "COMMITTED_READBACK"
NO_COMMIT = "NO_COMMIT"
UNKNOWN_EFFECT = "UNKNOWN_EFFECT"
DENIED = "DENIED"  # a RESULT only: a denied request creates no admission and consumes no idempotency key

STATES = (FENCED, IN_FLIGHT, COMMITTED_READBACK, NO_COMMIT, UNKNOWN_EFFECT, DENIED)
TERMINAL = frozenset({COMMITTED_READBACK, NO_COMMIT})

# state -> states reachable by exactly one ledger event. Terminal states have no successor; there is no edge
# back to FENCED or IN_FLIGHT from UNKNOWN_EFFECT/NO_COMMIT: automatic retry is not representable.
TRANSITIONS = {
    FENCED: frozenset({IN_FLIGHT}),
    IN_FLIGHT: frozenset({IN_FLIGHT, UNKNOWN_EFFECT, COMMITTED_READBACK}),
    UNKNOWN_EFFECT: frozenset({UNKNOWN_EFFECT, COMMITTED_READBACK, NO_COMMIT}),
    COMMITTED_READBACK: frozenset(),
    NO_COMMIT: frozenset(),
}
