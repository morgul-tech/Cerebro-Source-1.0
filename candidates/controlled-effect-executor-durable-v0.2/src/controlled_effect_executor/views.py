"""Typed, immutable views of OWNER state that this package only CONSUMES.

This package never mints, extends, restores or revokes a delegation and never mints Human/PM authority. The owner
supplies these views at admission time, inside one owner-linearizable read (see ports.OwnerFencePort).
"""
from __future__ import annotations

from dataclasses import dataclass

from .canonical import require_ref
from .errors import BatchSpecError

DELEGATION_STATUSES = ("ACTIVE", "REVOKED", "EXPIRED")
APPROVAL_STATUSES = ("APPROVED", "WITHDRAWN")


@dataclass(frozen=True)
class TargetScopeEntry:
    """A target the delegation covers. artifact_versions=None means any approved version of that target."""

    target_identity: str
    artifact_versions: "frozenset | None" = None

    def __post_init__(self) -> None:
        require_ref(self.target_identity, "target_scope.target_identity")
        if self.artifact_versions is not None:
            if not isinstance(self.artifact_versions, frozenset) or not self.artifact_versions:
                raise BatchSpecError("target-scope-versions-must-be-a-non-empty-frozenset-or-None")
            for v in self.artifact_versions:
                require_ref(v, "target_scope.artifact_version")


@dataclass(frozen=True)
class DelegationView:
    delegation_ref: str
    delegation_revision: int
    status: str
    actor_ref: str
    actor_generation: int
    allowed_operations: frozenset
    target_scope: tuple
    expires_at: "int | None"
    owner_currentness: int  # owner's monotonic currentness revision at the moment of this read

    def __post_init__(self) -> None:
        require_ref(self.delegation_ref, "delegation_ref")
        require_ref(self.actor_ref, "actor_ref")
        if self.status not in DELEGATION_STATUSES:
            raise BatchSpecError("delegation-status-invalid")
        for name in ("delegation_revision", "actor_generation", "owner_currentness"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise BatchSpecError(f"int-invalid:{name}")
        if self.expires_at is not None and (isinstance(self.expires_at, bool) or not isinstance(self.expires_at, int)):
            raise BatchSpecError("int-invalid:expires_at")
        if not isinstance(self.allowed_operations, frozenset):
            raise BatchSpecError("allowed-operations-must-be-frozenset")
        for op in self.allowed_operations:
            require_ref(op, "allowed_operation")
        if not isinstance(self.target_scope, tuple) or not all(isinstance(e, TargetScopeEntry) for e in self.target_scope):
            raise BatchSpecError("target-scope-must-be-tuple-of-TargetScopeEntry")


@dataclass(frozen=True)
class ApprovalView:
    """A synthetic Human-approval FIXTURE view: what the approval reference is bound to. Not Human authority."""

    approval_ref: str
    status: str
    target_identity: str
    artifact_version: str
    operations_digest: str

    def __post_init__(self) -> None:
        require_ref(self.approval_ref, "approval_ref")
        require_ref(self.target_identity, "approval.target_identity")
        require_ref(self.artifact_version, "approval.artifact_version")
        if self.status not in APPROVAL_STATUSES:
            raise BatchSpecError("approval-status-invalid")
        if not (isinstance(self.operations_digest, str) and len(self.operations_digest) == 64):
            raise BatchSpecError("approval-operations-digest-invalid")
