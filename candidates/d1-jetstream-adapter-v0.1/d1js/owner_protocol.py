"""The owner-outbox port the publisher actually needs (extracted from the synthetic duck interface).

Only two owner operations are used by ``OwnerOutboxPublisher``: ``outbox(message_id)`` (exact read of ONE intent)
and ``outbox_transition(...)`` (atomic compare-and-set on (state, claim_token)). Unconditional updates and
whole-world scans are not part of the port.

``require_owner_port`` admits exactly three named implementations by TYPE IDENTITY (no duck typing or arbitrary subclasses):
  * ``owner.SyntheticProjectOwner``           -- SYNTHETIC fixture mode (unchanged);
  * ``context_owner_port.ContextOwnerOutboxPort`` -- the service-owned Context PostgreSQL owner outbox.
  * ``production_runtime.ServiceLeasedOwnerPort`` -- that exact port with fresh server lease guards.
A caller-owned reader, callable, mapping, SQL string or any other object is refused, so publisher state can only be
read/claimed/settled through the owner's own durable store.
"""
from __future__ import annotations

from typing import Any, Protocol


class OwnerPortRejected(TypeError):
    pass


class OwnerOutboxPort(Protocol):
    def outbox(self, message_id: str) -> list[dict[str, Any]]: ...

    def outbox_transition(self, message_id: str, *, from_state: str, from_token: str | None,
                          **fields: Any) -> bool: ...


def require_owner_port(owner: Any) -> Any:
    from .context_owner_port import ContextOwnerOutboxPort
    from .owner import SyntheticProjectOwner
    from .production_runtime import ServiceLeasedOwnerPort
    if type(owner) not in (SyntheticProjectOwner, ContextOwnerOutboxPort, ServiceLeasedOwnerPort):
        raise OwnerPortRejected("OWNER_PORT_MUST_BE_SYNTHETIC_FIXTURE_OR_SERVICE_OWNED_CONTEXT_OUTBOX")
    return owner
