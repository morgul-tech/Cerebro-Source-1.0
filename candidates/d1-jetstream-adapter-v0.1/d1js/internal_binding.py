"""BK11 explicit offline/internal binding composition (DEFAULT OFF). Fixture mode stays exactly as before.

The SYNTHETIC_TEST_ONLY gates are NOT deleted: ``D1Fixture`` still composes only synthetic owner/return ports and
``D1Config`` still refuses a non-loopback broker. This module adds a SEPARATE composition that binds the publisher
and the receiver to the service-owned Context owner outbox / X9 custody instead -- and only when an explicit
``InternalBinding`` says so:

  enabled=True                         (default False -> every compose_* refuses before any client/broker use)
  owner_mode="CONTEXT_SERVICE_OWNER"   (the only internal mode)
  authorization_ref=<non-secret ref>   naming the later service-owned credential/role grant (installation input;
                                       nothing is issued here)
  role="publisher" | "receiver_only"   receiver_only never builds a publisher; its transport refuses publish.

Publisher work is one explicit bounded call (``publish_pending_once``): it lists at most ``limit`` scoped pending
obligations through the Context port (cursor/limit, never a whole-world scan) and runs the UNCHANGED publisher state
machine (claim-before-send CAS, bounded lookup budget/deadline, no blind retry) per row with one shared budget.
No scheduler, daemon, registry, engine or fleet wake.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .boundary import SyntheticSafeBoundaryHost
from .composition import RECEIVER, D1Receiver
from .config import D1Config
from .context_owner_port import ContextOwnerOutboxPort
from .faults import Faults
from .journal import ReceiverJournal
from .publisher import OwnerOutboxPublisher
from .reference_root import load_gateway
from .transport import TransportError
from .x9_ports import ContextX9DispositionReturn, ContextX9OwnerEventReader

CONTEXT_OWNER_MODE = "CONTEXT_SERVICE_OWNER"
RECEIVE_ONLY_DENIED = "RECEIVE_ONLY_PUBLISH_DENIED"
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/#=-]{0,159}$")


class InternalBindingRefused(RuntimeError):
    pass


class PublishDenied(TransportError):
    """Receive-only binding: publication refused before any broker call."""


@dataclass(frozen=True)
class InternalBinding:
    enabled: bool = False
    owner_mode: str = ""
    authorization_ref: str = ""
    role: str = ""
    receiver_ref: str = ""
    tenant_ref: str = ""
    workspace_ref: str = ""
    project_ref: str = ""

    def validate(self, *, role: str) -> "InternalBinding":
        if self.enabled is not True:
            raise InternalBindingRefused("INTERNAL_BINDING_DEFAULT_OFF")
        if self.owner_mode != CONTEXT_OWNER_MODE:
            raise InternalBindingRefused("OWNER_MODE_MUST_BE_CONTEXT_SERVICE_OWNER")
        if not _REF.fullmatch(self.authorization_ref or ""):
            raise InternalBindingRefused("SERVICE_OWNED_AUTHORIZATION_REF_REQUIRED")
        if self.role != role:
            raise InternalBindingRefused(f"BINDING_ROLE_MISMATCH:{self.role}!={role}")
        if role == "receiver_only" and not all(_REF.fullmatch(v or "") for v in (
                self.receiver_ref, self.tenant_ref, self.workspace_ref, self.project_ref)):
            raise InternalBindingRefused("RECEIVER_SCOPE_REQUIRED")
        return self


class ReceiveOnlyTransport:
    """Wraps the real transport for a receiver_only binding: publish is denied before any network call."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.denied_publishes = 0

    def publish(self, *args: Any, **kwargs: Any):
        self.denied_publishes += 1
        raise PublishDenied(RECEIVE_ONLY_DENIED)

    def __getattr__(self, name: str) -> Any:
        if name == "publish":
            raise AttributeError(name)
        return getattr(self._inner, name)


def compose_context_publisher(*, binding: InternalBinding, config: D1Config, transport: Any, client: Any,
                              faults: Faults | None = None, counters: Any = None,
                              clock: Callable[[], datetime] | None = None) -> "ContextPublisher":
    binding.validate(role="publisher")
    cfg = config.validate()
    port = ContextOwnerOutboxPort(client)
    publisher = OwnerOutboxPublisher(owner=port, transport=transport, config=cfg, faults=faults or Faults(),
                                     counters=counters, clock=clock)
    return ContextPublisher(port, publisher, cfg)


class ContextPublisher:
    def __init__(self, port: ContextOwnerOutboxPort, publisher: OwnerOutboxPublisher, config: D1Config) -> None:
        self.port, self.publisher, self.config = port, publisher, config

    def publish_pending_once(self, *, limit: int = 8) -> list[dict[str, Any]]:
        """ONE bounded explicit drain of the scoped publication obligations (no loop beyond ``limit``)."""
        if not self.config.enabled:
            return [{"result": "D1_DISABLED_NO_BROKER_ACTIVITY"}]
        rows, _next, drain_enabled = self.port.pending(limit=limit)
        if not drain_enabled:
            return [{"message_id": r["message_id"], "state": r["state"], "result": "DRAIN_DISABLED_RETAINED"}
                    for r in rows]
        budget = self.publisher.new_budget()
        out = []
        for row in rows:
            state = self.publisher.publish(row["message_id"], budget=budget)
            out.append({"message_id": row["message_id"], "from": row["state"], "result": state})
        return out


def compose_x9_receiver(*, binding: InternalBinding, config: D1Config, transport: Any, client: Any,
                        gateway_root: Path, journal_path: Path, faults: Faults | None = None,
                        host: SyntheticSafeBoundaryHost | None = None,
                        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> D1Receiver:
    """Receiver-only composition: real Context owner read + X9 disposition custody; no publisher exists."""
    binding.validate(role="receiver_only")
    cfg = config.validate()
    faults = faults or Faults()
    gw = load_gateway(gateway_root)
    reader = ContextX9OwnerEventReader(gw, client, faults=faults)
    ret = ContextX9DispositionReturn(gw, client, reader, faults=faults)
    journal = ReceiverJournal(journal_path, faults=faults)
    return D1Receiver(gw=gw, transport=ReceiveOnlyTransport(transport), journal=journal, owner=None,
                      host=host or SyntheticSafeBoundaryHost(), config=cfg, faults=faults, clock=clock,
                      receiver_ref=binding.receiver_ref,
                      interests=frozenset({(binding.tenant_ref, binding.workspace_ref, binding.project_ref,
                                            binding.receiver_ref)}),
                      owner_reader=reader, return_mapping=ret)


def receive_only_publish(receiver: D1Receiver) -> str:
    """The receive-only composition has no publisher; any attempt through its transport is denied pre-network."""
    try:
        receiver._t.publish("cerebro.test.d1.pointer", b"{}", "d1:" + "0" * 40, 1.0)
    except PublishDenied:
        return RECEIVE_ONLY_DENIED
    raise InternalBindingRefused("RECEIVE_ONLY_TRANSPORT_PUBLISHED")


__all__ = ["InternalBinding", "InternalBindingRefused", "ReceiveOnlyTransport", "PublishDenied",
           "compose_context_publisher", "compose_x9_receiver", "ContextPublisher", "RECEIVE_ONLY_DENIED",
           "CONTEXT_OWNER_MODE", "RECEIVER"]
