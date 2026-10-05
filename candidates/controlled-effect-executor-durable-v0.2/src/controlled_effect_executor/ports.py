"""Interfaces (typing.Protocol) the candidate depends on. Nothing here performs I/O.

OwnerFencePort is the smallest interface that expresses the ATOMIC requirement: the owner's delegation/approval
read and the admission insert must be one owner-linearizable section. The in-memory reference satisfies it with one
lock; a production owner would need the same property from its own store (e.g. one transaction holding a row lock on
the delegation row). That production mechanism is NOT provided here and is NOT proven by this candidate.
"""
from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .views import ApprovalView, DelegationView


class OwnerSnapshot(Protocol):
    def read_delegation(self, delegation_ref: str) -> "DelegationView | None": ...

    def read_approval(self, approval_ref: str) -> "ApprovalView | None": ...

    def currentness(self) -> int:
        """The owner's LIVE currentness revision right now (used for the compare-and-set at insert time)."""
        ...


class OwnerFencePort(Protocol):
    def atomic_read(self) -> "AbstractContextManager[OwnerSnapshot]":
        """Enter an owner-linearizable section: no revoke/bump can interleave until the section exits."""
        ...


@dataclass(frozen=True)
class ProviderAck:
    """What an adapter returns on a received response. An ack is a claim to be checked by readback, never proof."""

    correlation_ref: str


@dataclass(frozen=True)
class TargetObservation:
    """Result of a READ-ONLY authoritative provider-state inspection."""

    target_identity: str
    observed_version: str
    artifact_version: str
    applied_correlation_refs: tuple  # correlation refs of every effect the provider has applied to this target
    history_complete: bool  # True only if applied_correlation_refs is the provider's complete applied history
    authoritative: bool  # False => the observation cannot resolve anything

    def as_dict(self) -> dict:
        return {"target_identity": self.target_identity, "observed_version": self.observed_version,
                "artifact_version": self.artifact_version,
                "applied_correlation_refs": list(self.applied_correlation_refs),
                "history_complete": self.history_complete, "authoritative": self.authoritative}


@runtime_checkable
class ProviderEffectPort(Protocol):
    """Injected, local synthetic/mock adapter only in this package. Called by the executor and nothing else."""

    def apply(self, grant: object) -> ProviderAck: ...


@runtime_checkable
class ProviderReadbackPort(Protocol):
    """Read-only. Has no mutation method by construction."""

    def read_target_state(self, target_identity: str) -> TargetObservation: ...
