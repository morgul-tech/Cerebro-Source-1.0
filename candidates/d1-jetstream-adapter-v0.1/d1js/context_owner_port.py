"""Service-owned owner-outbox port over the versioned Context operations (BK11 owner outbox).

The publisher never touches the Context database. A thin Context client (one authenticated, host-assigned D1
publisher principal + its persisted current session) calls exactly:
  read_pm_owner_d1_outbox_v1   {op: read_exact | list_pending}   (project_state:read)
  settle_pm_owner_d1_outbox_v1 {message_id, from_state, from_token, fields}   (project_state:transition)
The Context side derives everything else (route, stream, receiver, identity, bytes) from its own committed rows.

``client`` must be a service-owned client object (``D1_SERVICE_OWNED_CONTEXT_CLIENT is True`` with a ``call``
method); a bare callable is refused. Any client/transport failure raises (the publisher maps a failed claim write to
CLAIM_NOT_DURABLE with zero broker calls).
"""
from __future__ import annotations

import base64
from typing import Any

READ_TOOL = "read_pm_owner_d1_outbox_v1"
SETTLE_TOOL = "settle_pm_owner_d1_outbox_v1"
MAX_LIST = 64


class ContextOwnerPortError(RuntimeError):
    pass


def _row(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    row = dict(raw)
    row["pointer"] = base64.b64decode(row["pointer"], validate=True)
    return row


class ContextOwnerOutboxPort:
    """Implements owner_protocol.OwnerOutboxPort against the Context service (no SQL, no caller state)."""

    def __init__(self, client: Any) -> None:
        if (callable(client) and not hasattr(client, "call")) or getattr(
                client, "D1_SERVICE_OWNED_CONTEXT_CLIENT", False) is not True or not callable(
                getattr(client, "call", None)):
            raise ContextOwnerPortError("SERVICE_OWNED_CONTEXT_CLIENT_REQUIRED")
        self._client = client
        self.reads = 0
        self.settles = 0

    # ------------------------------------------------------------------ OwnerOutboxPort
    def outbox(self, message_id: str | None = None) -> list[dict[str, Any]]:
        if message_id is None:
            raise ContextOwnerPortError("WHOLE_WORLD_OUTBOX_SCAN_REFUSED: use pending(after, limit)")
        self.reads += 1
        got = self._client.call(READ_TOOL, {"op": "read_exact", "message_id": message_id})
        row = _row(got.get("row"))
        return [row] if row is not None else []

    def outbox_transition(self, message_id: str, *, from_state: str, from_token: str | None,
                          **fields: Any) -> bool:
        fields.pop("updated_at", None)                      # set by the Context trigger
        if "duplicate" in fields and fields["duplicate"] is not None:
            fields["duplicate"] = bool(fields["duplicate"])
        if "puback_latency_ms" in fields and fields["puback_latency_ms"] is not None:
            fields["puback_latency_ms"] = float(fields["puback_latency_ms"])
        self.settles += 1
        got = self._client.call(SETTLE_TOOL, {"message_id": message_id, "from_state": from_state,
                                              "from_token": from_token, "fields": fields})
        if not isinstance(got.get("won"), bool):
            raise ContextOwnerPortError("SETTLE_RESULT_INVALID")
        return got["won"]

    def outbox_update(self, message_id: str, **fields: Any) -> None:
        raise ContextOwnerPortError("UNCONDITIONAL_OUTBOX_UPDATE_REFUSED")

    # ------------------------------------------------------------------ bounded discovery
    def recovery_candidates(self, *, after: dict[str, str] | None = None,
                            limit: int = 16) -> tuple[list[dict[str, Any]], dict[str, str] | None, bool]:
        """Read all owner obligations in the assigned route, including PubAcked rows.

        These are candidates, NOT asserted unresolved or permission to republish.
        Receiver disposition/auth/expiry policy is a separate qualified recovery seam.
        """
        if type(limit) is not int or not 1 <= limit <= MAX_LIST:
            raise ContextOwnerPortError("RECOVERY_LIMIT_OUT_OF_BOUNDS")
        if after is not None and (type(after) is not dict or set(after) != {"enqueued_at", "event_id"}
                or any(type(v) is not str or not v for v in after.values())):
            raise ContextOwnerPortError("RECOVERY_CURSOR_INVALID")
        args: dict[str, Any] = {"op": "list_recovery_candidates", "limit": limit}
        if after is not None: args["after"] = after
        self.reads += 1
        got = self._client.call(READ_TOOL, args)
        if not isinstance(got, dict) or type(got.get("rows")) is not list or len(got["rows"]) > limit:
            raise ContextOwnerPortError("RECOVERY_RESULT_INVALID")
        return [_row(r) for r in got["rows"]], got.get("next"), bool(got.get("drain_enabled"))

    def pending(self, *, after: dict[str, str] | None = None, limit: int = 16) -> tuple[list[dict[str, Any]],
                                                                                      dict[str, str] | None, bool]:
        if not (1 <= limit <= MAX_LIST):
            raise ContextOwnerPortError("PENDING_LIMIT_OUT_OF_BOUNDS")
        args: dict[str, Any] = {"op": "list_pending", "limit": limit}
        if after is not None:
            args["after"] = after
        self.reads += 1
        got = self._client.call(READ_TOOL, args)
        return [_row(r) for r in got["rows"]], got.get("next"), bool(got.get("drain_enabled"))
