"""Host-owned Context PM/X9 ports for the PR88 normal Signalvev host.

Only an in-process privileged Context host can supply the two authenticated
MCP contexts. This module never creates a session, credential, grant or receipt.
The public CLI cannot construct LiveRuntimeBindings and remains unbound.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from signalvev_client.config import ClientConfig
from signalvev_client.pm_x9 import MODE_PRODUCTION, PmX9HostPorts, PmX9Settings, PmX9Unbound
from signalvev_adapters.pm_owner_commit import PmCommittedReadyHint, PmCurrentRead


@dataclass(frozen=True)
class LiveRuntimeBindings:
    pm_owner: Any
    producer_host: Any
    receiver_host: Any
    pm_context: Any
    x9_context: Any
    receipt_ref: str
    client_config: ClientConfig
    clock: Callable[[], datetime]
    connect_fn: Callable[..., Any] | None = None
    enabled: bool = False


def _unbound(code: str) -> PmX9Unbound:
    return PmX9Unbound("PM_X9_CURRENT_HOST_COMPOSITION_UNAVAILABLE", [
        {"port": "current_context_host", "status": "MISSING", "detail": code}
    ])


def _need(value: bool, code: str) -> None:
    if not value:
        raise _unbound(code)


def _current_context(context: Any, expected: Any, role: str) -> None:
    """Check transport identity; the Context ports recheck persisted state per call."""
    from control_context_tools import McpToolCallContext

    _need(type(context) is McpToolCallContext, role + "_HOST_CONTEXT_REQUIRED")
    identity = context.identity
    identity.validate()
    transport = context.transport_session()
    _need(isinstance(transport, Mapping) and transport.get("session_ref") == expected.session_ref,
          role + "_CURRENT_SESSION_REQUIRED")
    _need((identity.tenant_ref, identity.workspace_ref, identity.principal_ref,
           identity.consumer_ref) ==
          (expected.tenant_ref, expected.workspace_ref, expected.principal_ref,
           expected.consumer_ref), role + "_IDENTITY_MISMATCH")
    _need(isinstance(context.private_headers, Mapping) and bool(context.private_headers),
          role + "_PRIVATE_AUTH_REQUIRED")


def _record_to_hint(record: Mapping[str, Any]) -> PmCommittedReadyHint:
    names = PmCommittedReadyHint.__dataclass_fields__
    values = {name: record[name] for name in names if name not in (
        "authenticated", "committed", "readback_verified", "consistent_snapshot", "way_home")}
    values.update(authenticated=True, committed=True, readback_verified=True,
                  consistent_snapshot=True, way_home=tuple(record["way_home"]))
    return PmCommittedReadyHint(**values)


def _record_to_current(record: Mapping[str, Any], projection: Mapping[str, Any],
                       referent_id: str, expected_revision: str) -> PmCurrentRead:
    from x9_receiver_host import _receiver_active_hold

    revision = record["revision_after"]
    # Opaque revisions have no order. A different value is not proof of a
    # supersession direction; the owner sequence in a pointer decides that.
    relation = "SAME" if revision == expected_revision else "UNKNOWN"
    return PmCurrentRead(
        authenticated=True, readback_verified=True,
        owner_ref=record["owner_ref"], referent_id=referent_id,
        current_revision=revision, relation=relation,
        snapshot_sha256=record["snapshot_sha256"], owner_seq=record["owner_seq"],
        claim_ref=record["claim_ref"], packet_ref=record["packet_ref"],
        queue_ref=record["queue_ref"], packet_sha256=record["packet_sha256"],
        ready_state=record["ready_state"], material_sha256=projection["material_sha256"],
        source_cut=projection["source_cut"], consistent_snapshot=True,
        active_hold=_receiver_active_hold(projection.get("active_hold")),
    )


class _PmPort:
    def __init__(self, owner: Any, producer: Any, context: Any, api: Any):
        self._owner, self._producer, self._context, self._api = owner, producer, context, api

    def read_committed_ready_hint(self, receipt_ref: str, *, expected_owner_ref: str) -> PmCommittedReadyHint:
        _need(expected_owner_ref == self._owner._assignment.owner_ref, "PM_OWNER_MISMATCH")
        # Context's receipt read locks current owner row and independently
        # verifies immutable receipt, packet and material hashes.
        record, _projection = self._producer._receipt(self._api, receipt_ref)
        _need(record["owner_ref"] == expected_owner_ref, "PM_RECEIPT_OWNER_MISMATCH")
        return _record_to_hint(record)

    def reread_ready_hint(self, referent_id: str, *, expected_owner_ref: str,
                          expected_revision: str) -> PmCurrentRead:
        _need(expected_owner_ref == self._owner._assignment.owner_ref, "PM_OWNER_MISMATCH")
        context = self._context
        current = self._owner.read({"referent_id": referent_id}, context.identity, context.transport_session())
        _need(current.get("current") is True, "PM_CURRENT_SELECTOR_REQUIRED")
        record = current["record"]
        _need(record["referent_id"] == referent_id and record["owner_ref"] == expected_owner_ref,
              "PM_CURRENT_IDENTITY_MISMATCH")
        # Recheck the same current immutable receipt through the producer's
        # packet/material-hash verifier; a changed current row fails closed.
        checked, projection = self._producer._receipt(self._api, record["receipt_ref"])
        _need(checked == record, "PM_CURRENT_READBACK_MISMATCH")
        return _record_to_current(record, projection, referent_id, expected_revision)


class _X9RereadPort:
    def __init__(self, api: Any, config: Any, owner: Any):
        self._api, self._config, self._owner = api, config, owner

    def reread_ready_hint(self, referent_id: str, *, expected_owner_ref: str,
                          expected_revision: str) -> PmCurrentRead:
        from pm_owner_atomic import PostgresPmOwner, _row
        import json

        owner = self._owner._assignment
        _need(expected_owner_ref == owner.owner_ref, "X9_PM_OWNER_MISMATCH")
        with self._api._transaction(write=False) as (cursor, identity, _session, _config, binding):
            _need(binding.role == "receiver" and
                  identity.principal_ref == self._config.receiver.identity.principal_ref,
                  "X9_RECEIVER_READ_AUTHORIZATION_REQUIRED")
            cursor.execute("""SELECT receipt_ref FROM cerebro_pm_owner_current
                WHERE tenant_ref=%s AND workspace_ref=%s AND owner_ref=%s AND referent_id=%s
                FOR SHARE""", (owner.tenant_ref, owner.workspace_ref, owner.owner_ref, referent_id))
            current = _row(cursor)
            _need(current is not None, "X9_PM_CURRENT_MISSING")
            query = """SELECT record_payload,snapshot_sha256,projection_payload FROM cerebro_pm_owner_receipts
                WHERE tenant_ref=%s AND workspace_ref=%s AND project_ref=%s AND owner_ref=%s
                AND referent_id=%s AND receipt_ref=%s"""
            params = (owner.tenant_ref, owner.workspace_ref, owner.project_ref, owner.owner_ref,
                      referent_id, current["receipt_ref"])
            cursor.execute(query, params)
            first = _row(cursor)
            _need(first is not None, "X9_PM_RECEIPT_MISSING")
            record = PostgresPmOwner._record(first)
            projection = first["projection_payload"]
            if isinstance(projection, str):
                projection = json.loads(projection)
            cursor.execute(query, params)
            second = _row(cursor)
            _need(second is not None and PostgresPmOwner._record(second) == record
                  and second["projection_payload"] == first["projection_payload"],
                  "X9_PM_READBACK_MISMATCH")
        _need(isinstance(projection, dict) and record["referent_id"] == referent_id
              and record["receipt_ref"] == current["receipt_ref"], "X9_PM_PROJECTION_INVALID")
        return _record_to_current(record, projection, referent_id, expected_revision)


def _make_ports(settings: PmX9Settings, *, host_runtime: LiveRuntimeBindings | None = None) -> PmX9HostPorts:
    """Bind only actual service-owned, current role contexts and exact new receipt."""
    _need(type(settings) is PmX9Settings, "SETTINGS_REQUIRED")
    _need(settings.mode == MODE_PRODUCTION, "PRODUCTION_MODE_REQUIRED")
    _need(type(host_runtime) is LiveRuntimeBindings and host_runtime.enabled is True,
          "EXPLICIT_HOST_RUNTIME_REQUIRED")
    from pm_owner_atomic import PostgresPmOwner
    from x9_producer_host import X9ProducerHost
    from x9_receiver_host import X9ReceiverHost

    runtime = host_runtime
    _need(type(runtime.pm_owner) is PostgresPmOwner and type(runtime.producer_host) is X9ProducerHost
          and type(runtime.receiver_host) is X9ReceiverHost, "CONTEXT_SERVICE_PORTS_REQUIRED")
    _need(isinstance(runtime.receipt_ref, str) and runtime.receipt_ref.startswith("pm-receipt:"),
          "FRESH_RECEIPT_REF_REQUIRED")
    _need(isinstance(runtime.client_config, ClientConfig) and callable(runtime.clock),
          "HOST_TRANSPORT_AND_CLOCK_REQUIRED")
    config = runtime.producer_host._config
    _need(runtime.receiver_host._config == config, "CUSTODY_CONFIG_MISMATCH")
    config.validate()
    _need(runtime.pm_owner._assignment == runtime.producer_host._pm_owner_assignment,
          "PM_ASSIGNMENT_MISMATCH")
    _current_context(runtime.pm_context, config.producer.identity, "PM")
    _current_context(runtime.x9_context, config.receiver.identity, "X9")
    _need(runtime.pm_context is not runtime.x9_context and
          config.producer.identity.session_ref != config.receiver.identity.session_ref,
          "PM_X9_DISTINCT_SESSIONS_REQUIRED")
    producer_channel, producer_api = runtime.producer_host._channel(runtime.pm_context)
    x9_channel, x9_api = runtime.receiver_host._channel(runtime.x9_context)
    # Both APIs reverify persisted project/session and role-bound scopes here.
    with producer_api._transaction(write=False):
        pass
    with x9_api._transaction(write=False):
        pass
    record, _projection = runtime.producer_host._receipt(producer_api, runtime.receipt_ref)
    _need(record["ready_state"] == "MATERIAL_READY", "PM_MATERIAL_NOT_READY")
    _need((record["owner_ref"], record["claim_ref"], record["packet_ref"],
           record["queue_ref"], record["packet_sha256"], record["commit_ref"]) ==
          (settings.owner_ref, settings.claim_ref, settings.packet_ref,
           settings.queue_ref, settings.packet_sha256, settings.attempt_ref),
          "FRESH_RECEIPT_SETTINGS_MISMATCH")
    _need((settings.producer_principal, settings.x9_principal, settings.x9_session_ref) ==
          (config.producer.identity.principal_ref, config.receiver.identity.principal_ref,
           config.receiver.identity.session_ref), "CURRENT_ROLE_SETTINGS_MISMATCH")
    return PmX9HostPorts(
        pm_port=_PmPort(runtime.pm_owner, runtime.producer_host, runtime.pm_context, producer_api),
        pm_reread_port=_X9RereadPort(x9_api, config, runtime.pm_owner),
        producer_channel=producer_channel, x9_channel=x9_channel,
        clock=runtime.clock, client_config=runtime.client_config,
        connect_fn=runtime.connect_fn,
    )


def make_ports(settings: PmX9Settings, *, host_runtime: LiveRuntimeBindings | None = None) -> PmX9HostPorts:
    try:
        return _make_ports(settings, host_runtime=host_runtime)
    except PmX9Unbound:
        raise
    except Exception:
        # Context may include tokens, headers, DSNs or SQL in exceptions.
        raise _unbound("CONTEXT_HOST_READ_UNAVAILABLE") from None
