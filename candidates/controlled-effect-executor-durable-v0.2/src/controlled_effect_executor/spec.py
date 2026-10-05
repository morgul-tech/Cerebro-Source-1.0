"""BatchSpec: the exact, immutable, canonical identity of one admitted batch of provider operations.

The digest binds every field the work order lists (operations in order, target, precondition version, approved
artifact version, actor + generation, delegation ref + revision + expiry, Human approval reference, idempotency
key, optional rollback id). A profile hash is NOT a batch identity and is not accepted anywhere in this package.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .canonical import canonical_text, require_ref, sha256_hex
from .errors import BatchSpecError

SPEC_SCHEMA = "cerebro-controlled-effect-batch-spec/v1"
MAX_OPERATIONS = 64
ParamValue = "str | int | bool | None"


@dataclass(frozen=True)
class Operation:
    """One exact provider operation: a name plus scalar parameters (no floats)."""

    name: str
    params: tuple = ()  # tuple[tuple[str, str|int|bool|None], ...] sorted by key; use Operation.of()

    def __post_init__(self) -> None:
        require_ref(self.name, "operation.name")
        if not isinstance(self.params, tuple):
            raise BatchSpecError("operation-params-must-be-tuple:use-Operation.of")
        seen: set[str] = set()
        for item in self.params:
            if not (isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str)):
                raise BatchSpecError("operation-param-shape")
            key, value = item
            if key in seen:
                raise BatchSpecError(f"operation-param-duplicate:{key}")
            seen.add(key)
            if not (value is None or isinstance(value, (str, int, bool))):
                raise BatchSpecError(f"operation-param-type:{key}")
        if [k for k, _ in self.params] != sorted(seen):
            raise BatchSpecError("operation-params-not-sorted:use-Operation.of")
        canonical_text(self.as_dict())  # scalar/utf-8 check

    @classmethod
    def of(cls, name: str, **params: Any) -> "Operation":
        return cls(name=name, params=tuple(sorted(params.items())))

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "params": {k: v for k, v in self.params}}


def _int(value: Any, field: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise BatchSpecError(f"int-invalid:{field}")
    return value


@dataclass(frozen=True)
class BatchSpec:
    work_order_ref: str
    effect_ref: str
    operations: tuple
    target_identity: str
    target_precondition_version: "str | None"
    artifact_version: str
    actor_ref: str
    actor_generation: int
    delegation_ref: str
    delegation_revision: int
    delegation_expiry: "int | None"
    human_approval_ref: str
    idempotency_key: str
    rollback_ref: "str | None" = None

    def __post_init__(self) -> None:
        for field in ("work_order_ref", "effect_ref", "target_identity", "artifact_version", "actor_ref",
                      "delegation_ref", "human_approval_ref", "idempotency_key"):
            require_ref(getattr(self, field), field)
        for field in ("target_precondition_version", "rollback_ref"):
            if getattr(self, field) is not None:
                require_ref(getattr(self, field), field)
        _int(self.actor_generation, "actor_generation", 0)
        _int(self.delegation_revision, "delegation_revision", 1)
        if self.delegation_expiry is not None:
            _int(self.delegation_expiry, "delegation_expiry", 0)
        if not isinstance(self.operations, tuple) or not (1 <= len(self.operations) <= MAX_OPERATIONS):
            raise BatchSpecError("operations-must-be-a-non-empty-tuple")
        for op in self.operations:
            if not isinstance(op, Operation):
                raise BatchSpecError("operations-must-be-Operation")

    # -- identity -------------------------------------------------------------------------------------------------
    def basis(self) -> dict[str, Any]:
        """The exact canonical basis the digest covers. Ordered operation list; nothing derived, nothing mutable."""
        return {
            "schema": SPEC_SCHEMA,
            "work_order_ref": self.work_order_ref,
            "effect_ref": self.effect_ref,
            "operations": [op.as_dict() for op in self.operations],
            "target_identity": self.target_identity,
            "target_precondition_version": self.target_precondition_version,
            "artifact_version": self.artifact_version,
            "actor_ref": self.actor_ref,
            "actor_generation": self.actor_generation,
            "delegation_ref": self.delegation_ref,
            "delegation_revision": self.delegation_revision,
            "delegation_expiry": self.delegation_expiry,
            "human_approval_ref": self.human_approval_ref,
            "idempotency_key": self.idempotency_key,
            "rollback_ref": self.rollback_ref,
        }

    def canonical_text(self) -> str:
        return canonical_text(self.basis())

    @property
    def digest(self) -> str:
        return sha256_hex(self.basis())

    @property
    def batch_ref(self) -> str:
        return "BATCH-" + self.digest[:24].upper()

    @property
    def operations_digest(self) -> str:
        """Digest of the ordered exact operation list alone (what a Human approval fixture may bind)."""
        return sha256_hex([op.as_dict() for op in self.operations])

    @property
    def operation_names(self) -> tuple[str, ...]:
        return tuple(op.name for op in self.operations)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "BatchSpec":
        ops = tuple(Operation.of(o["name"], **dict(o.get("params", {}))) for o in data["operations"])
        fields = {k: v for k, v in data.items() if k not in {"schema", "operations"}}
        return cls(operations=ops, **fields)
