"""BK11: the historical gateway's owner/return ports bound to the REAL Context PM owner + X9 receiver custody.

Replaces the SYNTHETIC owner bindings (owner_ports.py) for the X9 receive path; the gateway decision logic is the
pinned historical code and is not reimplemented. One authenticated X9-receiver Context client (its own principal,
persisted current session and host role tuple) calls only existing/minimal versioned operations:

  resolve_d1_pointer_v1 {op: resolve|verify_route}  exact D1 pointer digest -> ORIGINAL PM event mapping + the
                                                     referent's current owner cut (one Context transaction)
  consume_x9_pointer {event_id}                      EXISTING X9 consume: fresh PM reread, typed disposition
                                                     STALE / NO_MATERIAL_DELTA / MATERIAL_ROUTE, append-once +
                                                     exact readback (work_consumed=false, effect NONE_CLAIMED)

Mapping onto the gateway contract (owner_event_key = PM event_id, owner_revision = PM owner_seq):
  * read_current -> ProjectOwnerEvent with the referent's CURRENT owner_seq / snapshot hash (a newer owner commit
    for the same referent => higher revision => gateway STALE_SUPERSEDED);
  * DurableReturnPort.append/read_exact -> X9 disposition + independent ALREADY_DISPOSED readback; the receipt ref
    binds the exact disposition hash. A STALE disposition at append time is NOT a selected return (HOLD, NOACK);
  * settle_before_ack(pointer, d1) -> before ANY transit ACK for STALE / NO_MATERIAL_DELTA the matching durable X9
    disposition must be read back exactly; failure => NOACK, obligation pending.
ACK/PubAck never means actor work or effect.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .boundary import Lease
from .faults import Faults

RESOLVE_TOOL = "resolve_d1_pointer_v1"
CONSUME_TOOL = "consume_x9_pointer"
SETTLED_X9 = ("DISPOSITION_READBACK", "ALREADY_DISPOSED")
D1_TO_X9 = {"STALE": "STALE", "NO_MATERIAL_DELTA": "NO_MATERIAL_DELTA"}


class X9PortError(RuntimeError):
    pass


class X9ReturnNotClosed(RuntimeError):
    pass


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def _require_client(client: Any) -> Any:
    if getattr(client, "D1_SERVICE_OWNED_CONTEXT_CLIENT", False) is not True or not callable(
            getattr(client, "call", None)):
        raise X9PortError("SERVICE_OWNED_CONTEXT_CLIENT_REQUIRED")
    return client


class ContextX9OwnerEventReader:
    """gateway.ProjectOwnerEventReader over the Context PM owner, read with the X9 receiver's own authority."""

    def __init__(self, gw: Any, client: Any, *, faults: Faults) -> None:
        self._gw, self._client, self.faults = gw, _require_client(client), faults
        self.lease: Lease | None = None
        self.memo: dict[tuple[str, ...], tuple[int, str]] = {}
        self.rereads = 0
        self.route_checks = 0
        self._inflight: Any = None
        self.last_resolution: dict[str, Any] | None = None

    def bind_inflight(self, pointer: Any) -> None:
        self._inflight = pointer

    def resolution(self, pointer: Any) -> dict[str, Any]:
        res = self._client.call(RESOLVE_TOOL, {"op": "resolve", "message_id": pointer.message_id,
                                               "pointer_sha256": pointer.raw_sha256})
        d1 = res["mapping"]["d1"]
        pm = res["mapping"]["pm"]
        if (d1["message_id"], d1["owner_event_key"], d1["owner_revision"], d1["event_fingerprint"],
                d1["receiver_ref"], d1["referent"]["id"]) != (
                pointer.message_id, pointer.owner_event_key, pointer.owner_revision, pointer.event_fingerprint,
                pointer.receiver_ref, pointer.referent_id) or pm["event_id"] != pointer.owner_event_key \
                or res["pointer_sha256"] != pointer.raw_sha256:
            raise X9PortError("D1_POINTER_MAPPING_ROUNDTRIP_MISMATCH")
        return res

    def verify_pointer_route(self, delivery: Any) -> bool:
        self.route_checks += 1
        p = self._inflight
        if p is None or p.owner_event_key != delivery.owner_event_key:
            return False
        if self.lease is not None:
            self.lease.require("owner.route")
        got = self._client.call(RESOLVE_TOOL, {"op": "verify_route", "message_id": p.message_id,
                                               "pointer_sha256": p.raw_sha256})
        return got.get("exact") is True

    def verify_full_pointer(self, pointer: Any) -> bool:
        self.route_checks += 1
        got = self._client.call(RESOLVE_TOOL, {"op": "verify_route", "message_id": pointer.message_id,
                                               "pointer_sha256": pointer.raw_sha256})
        return got.get("exact") is True and got.get("receiver_ref") == pointer.receiver_ref

    def read_current(self, *, tenant_ref: str, workspace_ref: str, project_ref: str, owner_event_key: str):
        self.rereads += 1
        if self.lease is not None:
            self.lease.require("owner.read")
        self.faults.hit("owner.read_current")
        p = self._inflight
        if p is None or (p.tenant_ref, p.workspace_ref, p.project_ref, p.owner_event_key) != (
                tenant_ref, workspace_ref, project_ref, owner_event_key):
            raise X9PortError("NO_EXACT_POINTER_IN_FLIGHT")
        res = self.resolution(p)
        self.last_resolution = res
        cur = res["current"]
        if res.get("committed_readback") is not True or not isinstance(cur.get("owner_seq"), int):
            raise X9PortError("OWNER_CURRENT_READ_UNPROVEN")
        same = cur["event_id"] == p.owner_event_key
        if same and (cur["owner_seq"], cur["snapshot_sha256"]) != (p.owner_revision, p.event_fingerprint):
            raise X9PortError("OWNER_SAME_EVENT_IDENTITY_CONFLICT")
        self.memo[(tenant_ref, workspace_ref, project_ref, owner_event_key)] = (cur["owner_seq"], p.referent_id)
        return self._gw.ProjectOwnerEvent(
            tenant_ref=tenant_ref, workspace_ref=workspace_ref, project_ref=project_ref, owner_ref=p.owner_ref,
            owner_event_key=owner_event_key, owner_revision=cur["owner_seq"], event_fingerprint=cur["snapshot_sha256"],
            event_kind="OWNER_EFFECT_COMMITTED",
            source_kind="PROJECT_ENGINE_OWNER_COMMIT",      # gateway literal; the owner here is the PM owner ledger
            currentness="CURRENT", owner_commit_verified=True, authenticated_same_owner_readback=True,
            material=cur["ready_state"] == "MATERIAL_READY")


class ContextX9DispositionReturn:
    """gateway.DurableReturnPort -> the EXISTING X9 consume/disposition custody (append-once + exact readback)."""

    def __init__(self, gw: Any, client: Any, reader: ContextX9OwnerEventReader, *, faults: Faults) -> None:
        if type(reader) is not ContextX9OwnerEventReader:
            raise X9PortError("X9_OWNER_READER_REQUIRED")
        self._gw, self._client, self._reader, self.faults = gw, _require_client(client), reader, faults
        self.lease: Lease | None = None
        self.appends = 0
        self.consume_calls = 0
        self.last: dict[str, Any] | None = None
        self._appended: dict[str, Any] = {}

    def _consume(self, event_id: str) -> dict[str, Any]:
        self.consume_calls += 1
        self.faults.hit("x9.consume")
        return self._client.call(CONSUME_TOOL, {"event_id": event_id})

    def _expected(self, pointer: Any) -> dict[str, Any]:
        res = self._reader.resolution(pointer)
        return res["mapping"]["pm"]

    def _disposition(self, got: dict[str, Any], pm: dict[str, Any]) -> dict[str, Any]:
        if got.get("state") not in SETTLED_X9 or not isinstance(got.get("disposition"), dict):
            raise X9ReturnNotClosed(f"X9_{got.get('state')}:{got.get('reason')}")
        d = got["disposition"]
        if (d.get("event_id"), d.get("attempt_id"), d.get("work_consumed"), d.get("effect")) != (
                pm["event_id"], pm["commit_ref"], False, "NONE_CLAIMED"):
            raise X9ReturnNotClosed("X9_DISPOSITION_NOT_FOR_ORIGINAL_EVENT")
        return d

    def _settled(self, pointer: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        """Disposition (written now or already) + an INDEPENDENT fresh readback that must be identical."""
        pm = self._expected(pointer)
        first = self._disposition(self._consume(pm["event_id"]), pm)
        again = self._consume(pm["event_id"])
        if again.get("state") != "ALREADY_DISPOSED" or self._disposition(again, pm) != first:
            raise X9ReturnNotClosed("X9_DISPOSITION_EXACT_READBACK_UNPROVEN")
        return first, pm

    def append(self, record: Any):
        if self.lease is not None:
            self.lease.require("return.append")
        self.faults.hit("return.append")
        self.appends += 1
        p = self._reader._inflight
        if p is None or p.owner_event_key != record.owner_event_key:
            raise X9ReturnNotClosed("NO_EXACT_POINTER_IN_FLIGHT")
        disposition, _pm = self._settled(p)
        if disposition["disposition"] not in ("MATERIAL_ROUTE", "NO_MATERIAL_DELTA"):
            raise X9ReturnNotClosed(f"X9_DISPOSED_{disposition['disposition']}_NOT_A_SELECTED_RETURN")
        self.last = disposition
        self._appended[record.closure_id] = record
        return self._receipt(record, disposition)

    def _receipt(self, record: Any, disposition: dict[str, Any]):
        return self._gw.ReturnReceipt(closure_id=record.closure_id, fingerprint=record.fingerprint,
                                      receipt_ref="x9disp:" + _sha(disposition)[:40],
                                      owner_revision=record.owner_revision, provider_readback_verified=True)

    def read_exact(self, *, tenant_ref: str, workspace_ref: str, project_ref: str, receiver_ref: str,
                   closure_id: str):
        p = self._reader._inflight
        if p is None or self.last is None:
            return None
        pm = self._expected(p)
        got = self._consume(pm["event_id"])
        if got.get("state") != "ALREADY_DISPOSED":
            return None
        d = self._disposition(got, pm)
        record = self._appended.get(closure_id)
        if d != self.last or record is None or (record.tenant_ref, record.workspace_ref, record.project_ref,
                                                 record.receiver_ref) != (tenant_ref, workspace_ref, project_ref,
                                                                          receiver_ref):
            return None
        return self._receipt(record, d)                  # closure identity = the gateway's own record

    def verify_receipt(self, receipt_ref: str) -> dict[str, Any] | None:
        """Independent exact receipt check used before CONSUMED_SELECTED_RETURN is journaled/ACKed."""
        p = self._reader._inflight
        if p is None:
            return None
        try:
            disposition, pm = self._settled(p)
        except Exception:
            return None
        if "x9disp:" + _sha(disposition)[:40] != receipt_ref:
            return None
        return {"owner_event_key": pm["event_id"], "owner_event_fingerprint": p.event_fingerprint,
                "x9_disposition": disposition}

    def settle_before_ack(self, pointer: Any, d1: str, receipt_ref: str | None) -> dict[str, Any] | None:
        if d1 == "CONSUMED_SELECTED_RETURN":
            return None                                   # verify_receipt already read it back exactly
        expected = D1_TO_X9.get(d1)
        if expected is None:
            raise X9ReturnNotClosed(f"NO_X9_DISPOSITION_FOR_{d1}")
        disposition, _pm = self._settled(pointer)
        if disposition["disposition"] != expected:
            raise X9ReturnNotClosed(f"X9_DISPOSITION_{disposition['disposition']}_DISAGREES_WITH_{d1}")
        self.last = disposition
        return disposition
