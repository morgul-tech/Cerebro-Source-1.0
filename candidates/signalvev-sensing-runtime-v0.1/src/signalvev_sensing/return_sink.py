"""10. SELECTED DURABLE RETURN INTERFACE. Describes WHAT a material typed closure is and WHERE it goes (an injected sink).

This package never writes Drive or any canonical state. InMemoryReturnSink is a test double; NullReturnSink drops.
The closure carries ids, hashes, typed outcome and Way Home -- never grounding text. ACK_READ != WORK_CONSUMED != EFFECT.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .activation import ActivationDecision
from .model import (ACK_READ, AUTHORITY, CONFLICT_HOLD, HOLD_APPLICABILITY, HOLD_IDENTITY, HOLD_UNREADABLE,
                    STALE_SUPERSEDED, sha256_hex)

# Material = someone downstream needs to know. NOT_APPLICABLE / DUPLICATE / EXPIRED / HOLD_SCHEMA are noise or forgeable:
# evidence only (counter or flight recorder), never a sink write.
RETURN_SELECTED = frozenset({ACK_READ, STALE_SUPERSEDED, HOLD_UNREADABLE, HOLD_IDENTITY, CONFLICT_HOLD,
                             HOLD_APPLICABILITY})


def select_for_return(disposition: str) -> bool:
    return disposition in RETURN_SELECTED


@dataclass(frozen=True)
class ClosureRecord:
    closure_id: str
    event_id: str
    owner_ref: str
    referent_type: str
    referent_id: str
    revision_after: str
    disposition: str
    reason: str
    receipt_stage: str                 # v0.1 stage actually reached on the receiver: READ (ACK_READ only) else DELIVERED
    observed_sha256: str | None
    activation: ActivationDecision
    way_home: tuple[str, ...]
    work_consumed: bool = False        # ACK_READ is not WORK_CONSUMED
    effect: str = "NONE_CLAIMED"       # ... and not EFFECT
    authority: str = AUTHORITY
    is_truth_store: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"closure_id": self.closure_id, "event_id": self.event_id, "owner_ref": self.owner_ref,
                "referent": {"type": self.referent_type, "id": self.referent_id}, "revision_after": self.revision_after,
                "disposition": self.disposition, "reason": self.reason, "receipt_stage": self.receipt_stage,
                "observed_sha256": self.observed_sha256, "activation": self.activation.as_dict(),
                "way_home": list(self.way_home), "work_consumed": self.work_consumed, "effect": self.effect,
                "authority": self.authority, "is_truth_store": self.is_truth_store}


def make_closure(*, event_id, owner_ref, referent_type, referent_id, revision_after, disposition, reason,
                 observed_sha256, activation, way_home) -> ClosureRecord:
    return ClosureRecord(
        closure_id=sha256_hex(f"{event_id}|{disposition}|{reason}".encode())[:32], event_id=event_id,
        owner_ref=owner_ref, referent_type=referent_type, referent_id=referent_id, revision_after=revision_after,
        disposition=disposition, reason=reason, receipt_stage="READ" if disposition == ACK_READ else "DELIVERED",
        observed_sha256=observed_sha256, activation=activation, way_home=tuple(way_home))


class ReturnSink(Protocol):
    def deliver(self, closure: ClosureRecord) -> None: ...


class InMemoryReturnSink:
    """Test double. Not durable. A real sink (owner-bound, selected durable) is a future owner's job."""

    def __init__(self) -> None:
        self.records: list[ClosureRecord] = []

    def deliver(self, closure: ClosureRecord) -> None:
        self.records.append(closure)


class NullReturnSink:
    def deliver(self, closure: ClosureRecord) -> None:
        return None
