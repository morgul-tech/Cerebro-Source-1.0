"""Explicit settlement evidence: the smallest extra readback needed for an honest, FINAL NO_COMMIT.

V0.1 claimed NO_COMMIT from an authoritative, complete applied-history that merely lacked the attempt's correlation.
That is a point-in-time absence: an old operation that is still pending at the provider can commit later. V0.2 keeps
V0.1's classification for everything else but refuses NO_COMMIT unless the provider ALSO reports, authoritatively and
read-only, that this correlation is SETTLED_ABSENT, i.e. it has been closed/fenced so that it can never apply.

This package never writes a fence, a tombstone or a cancellation to a provider. It only READS the provider's own
statement. How a real provider reaches SETTLED_ABSENT (its own timeout, fencing token, tombstone) is NOT designed here
and is the first unproven edge (see ARCHITECTURE.md).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from controlled_effect_executor.executor import classify_observation
from controlled_effect_executor.ports import TargetObservation
from controlled_effect_executor.spec import BatchSpec

APPLIED = "APPLIED"
SETTLED_ABSENT = "SETTLED_ABSENT"
PENDING = "PENDING"
UNKNOWN = "UNKNOWN"
SETTLEMENT_STATUSES = (APPLIED, SETTLED_ABSENT, PENDING, UNKNOWN)


@dataclass(frozen=True)
class SettlementEvidence:
    correlation_ref: str
    status: str  # APPLIED | SETTLED_ABSENT | PENDING | UNKNOWN
    authoritative: bool
    evidence_ref: "str | None" = None  # the provider's own record identifier, if any
    authenticated: bool = False  # trusted adapter assertion; real credential/port binding remains unproven here

    def as_dict(self) -> dict:
        return {"correlation_ref": self.correlation_ref, "status": self.status,
                "authoritative": self.authoritative, "evidence_ref": self.evidence_ref,
                "authenticated": self.authenticated}


@runtime_checkable
class ProviderSettlementReadbackPort(Protocol):
    """Read-only, by construction. A readback object lacking this method can never yield a final NO_COMMIT."""

    def read_settlement(self, correlation_ref: str) -> SettlementEvidence: ...


def classify_durable(correlation_ref: str, spec: BatchSpec, obs: object, settlement: object, *,
                     ack_seen: bool) -> tuple:
    """Pure. (classification, reason): COMMITTED | NO_COMMIT | INDETERMINATE.

    Everything V0.1 decides except NO_COMMIT is passed through unchanged. A V0.1 NO_COMMIT is downgraded to
    INDETERMINATE unless explicit, authoritative, authenticated settlement evidence for THIS correlation says
    SETTLED_ABSENT. This local flag is an adapter contract, not proof that a real provider integration exists.
    The reason code of an accepted NO_COMMIT stays V0.1's READBACK_PROVES_NO_COMMIT (receipt semantics unchanged)."""
    base, reason = classify_observation(correlation_ref, spec, obs, ack_seen=ack_seen)
    valid = isinstance(settlement, SettlementEvidence) and settlement.correlation_ref == correlation_ref \
        and settlement.status in SETTLEMENT_STATUSES
    if base == "COMMITTED":
        if valid and settlement.authoritative and settlement.authenticated and settlement.status == SETTLED_ABSENT:
            return "INDETERMINATE", "READBACK_CONTRADICTS_SETTLEMENT"
        return base, reason
    if base != "NO_COMMIT":
        return base, reason
    if not valid:
        return "INDETERMINATE", "NO_COMMIT_REFUSED_NO_SETTLEMENT_EVIDENCE"
    if not settlement.authoritative:
        return "INDETERMINATE", "SETTLEMENT_NOT_AUTHORITATIVE"
    if settlement.status == SETTLED_ABSENT:
        if not settlement.authenticated or not settlement.evidence_ref:
            return "INDETERMINATE", "SETTLEMENT_NOT_AUTHENTICATED"
        return "NO_COMMIT", reason
    if settlement.status == PENDING:
        return "INDETERMINATE", "LATE_COMMIT_STILL_POSSIBLE_SETTLEMENT_PENDING"
    if settlement.status == APPLIED:
        return "INDETERMINATE", "SETTLEMENT_APPLIED_BUT_NOT_IN_OBSERVATION"
    return "INDETERMINATE", "NO_COMMIT_REFUSED_NO_SETTLEMENT_EVIDENCE"


def read_evidence(readback: object, correlation_ref: str, target_identity: str) -> tuple:
    """Read settlement FIRST, then the observation (settlement finality is irrevocable, so the order cannot create a
    false NO_COMMIT; the reverse order could pair a stale-empty observation with a later settlement read). Any failure
    to read is 'no evidence', never a decision. Returns (observation|None, settlement|None)."""
    settlement = None
    reader = getattr(readback, "read_settlement", None)
    if callable(reader):
        try:
            settlement = reader(correlation_ref)
        except Exception:  # noqa: BLE001 - unavailable settlement proves nothing
            settlement = None
    try:
        obs = readback.read_target_state(target_identity)
    except Exception:  # noqa: BLE001
        obs = None
    return obs, settlement


__all__ = ["APPLIED", "PENDING", "ProviderSettlementReadbackPort", "SETTLED_ABSENT", "SETTLEMENT_STATUSES",
           "SettlementEvidence", "TargetObservation", "UNKNOWN", "classify_durable", "read_evidence"]
