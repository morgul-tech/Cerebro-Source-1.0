"""PM-owned ready receipt ledger in the existing Context PostgreSQL transaction.

Only a host-pinned PM session may commit or read. Tool arguments never assign a
role or supply receipt identities. This module does not create a READY packet.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping


SHA = re.compile(r"^[0-9a-f]{64}$")
REF = re.compile(r"^[^\s\r\n]{1,256}$")


class PmOwnerError(ValueError):
    pass


def _need(condition: bool, code: str) -> None:
    if not condition:
        raise PmOwnerError(code)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _row(cursor: Any) -> dict[str, Any] | None:
    item = cursor.fetchone()
    if item is None:
        return None
    if isinstance(item, Mapping):
        return dict(item)
    return dict(zip((column[0] for column in cursor.description), item))


@dataclass(frozen=True)
class PmOwnerAssignment:
    owner_ref: str
    tenant_ref: str
    workspace_ref: str
    project_ref: str
    principal_ref: str
    consumer_ref: str
    session_ref: str
    session_binding_id: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PmOwnerAssignment":
        _need(isinstance(value, Mapping) and set(value) == set(cls.__dataclass_fields__),
              "PM_OWNER_ASSIGNMENT_EXACT_FIELDS_REQUIRED")
        assignment = cls(**value)
        for key in ("owner_ref", "tenant_ref", "workspace_ref", "project_ref", "principal_ref",
                    "consumer_ref", "session_ref", "session_binding_id"):
            _need(isinstance(getattr(assignment, key), str) and REF.fullmatch(getattr(assignment, key)) is not None,
                  "PM_OWNER_ASSIGNMENT_INVALID")
        return assignment


class PostgresPmOwner:
    """One actual producer-owned commit and exact receipt/current reads."""

    def __init__(self, *, state_port: Any, assignment: PmOwnerAssignment, d1_outbox: Any | None = None):
        _need(callable(getattr(state_port, "_transaction", None))
              and callable(getattr(state_port, "_load_session", None)), "PM_OWNER_STATE_PORT_REQUIRED")
        _need(type(assignment) is PmOwnerAssignment, "PM_OWNER_ASSIGNMENT_REQUIRED")
        # BK11: optional host-constructed D1 owner outbox (default None = unchanged behaviour). Only the
        # service-owned PmOwnerD1Outbox type is accepted; callers can never inject a callable or SQL here.
        if d1_outbox is not None:
            from pm_owner_d1_outbox import PmOwnerD1Outbox
            _need(type(d1_outbox) is PmOwnerD1Outbox and d1_outbox._pm == assignment,
                  "PM_OWNER_D1_OUTBOX_BINDING_INVALID")
        self._state = state_port
        self._assignment = assignment
        self._d1_outbox = d1_outbox

    def _current_session(self, cursor: Any, identity: Any,
                         transport_session: Mapping[str, Any] | None, *, write: bool) -> None:
        a = self._assignment
        _need(transport_session is not None and transport_session.get("session_ref") == a.session_ref,
              "PM_OWNER_HOST_SESSION_REQUIRED")
        _need((identity.tenant_ref, identity.workspace_ref, identity.principal_ref, identity.consumer_ref)
              == (a.tenant_ref, a.workspace_ref, a.principal_ref, a.consumer_ref),
              "PM_OWNER_ASSIGNED_IDENTITY_REQUIRED")
        session = self._state._load_session(cursor, tenant_ref=a.tenant_ref, workspace_ref=a.workspace_ref,
                                            principal_ref=a.principal_ref, consumer_ref=a.consumer_ref,
                                            session_ref=a.session_ref, for_update=write)
        _need(session is not None, "PM_OWNER_CURRENT_SESSION_REQUIRED")
        project = self._state._load_project(cursor, tenant_ref=a.tenant_ref,
                                             workspace_ref=a.workspace_ref, project_ref=a.project_ref,
                                             for_update=write)
        _need(project is not None and project.get("project_status") == "ACTIVE",
              "PM_OWNER_ACTIVE_PROJECT_REQUIRED")
        _need((session["project_ref"], session["session_binding_id"])
              == (a.project_ref, a.session_binding_id)
              and session["project_revision"] == project["revision"]
              and type(session["session_revision"]) is int and session["session_revision"] >= 1
              and isinstance(session["fingerprint"], str)
              and SHA.fullmatch(session["fingerprint"]) is not None,
              "PM_OWNER_STALE_SESSION_OR_PROJECT")

    def _transaction(self, identity: Any, transport_session: Mapping[str, Any] | None, *, write: bool):
        scope = "project_state:transition" if write else "project_state:read"
        _need(scope in identity.state_scopes, "PM_OWNER_SCOPE_REQUIRED")
        a = self._assignment
        return self._state._transaction(tenant_ref=a.tenant_ref, workspace_ref=a.workspace_ref,
                                         principal_ref=a.principal_ref, read_only=not write,
                                         repeatable_read=True, passthrough_errors=(PmOwnerError,))

    @staticmethod
    def _record(row: dict[str, Any]) -> dict[str, Any]:
        value = row["record_payload"]
        if isinstance(value, str):
            value = json.loads(value)
        _need(isinstance(value, dict) and row["snapshot_sha256"] == value.get("snapshot_sha256")
              and _hash({key: item for key, item in value.items() if key != "snapshot_sha256"})
              == row["snapshot_sha256"],
              "PM_OWNER_RECORD_DRIFT")
        return value

    def commit(self, args: dict[str, Any], identity: Any, transport_session: Mapping[str, Any] | None) -> dict[str, Any]:
        required = {"idempotency_key", "referent_id", "claim_ref", "packet_ref", "queue_ref",
                    "claim_body", "packet_body", "queue_body", "ready_state", "way_home",
                    "expected_current_receipt_ref"}
        _need(set(args) == required, "PM_OWNER_COMMIT_EXACT_FIELDS_REQUIRED")
        for key in ("idempotency_key", "referent_id", "claim_ref", "packet_ref", "queue_ref"):
            _need(isinstance(args[key], str) and REF.fullmatch(args[key]) is not None,
                  "PM_OWNER_COMMIT_REF_INVALID")
        _need(args["referent_id"].startswith("pm-ready:"), "PM_OWNER_REFERENT_INVALID")
        _need(args["ready_state"] in ("BIND_AVAILABLE", "MATERIAL_READY"), "PM_OWNER_READY_STATE_INVALID")
        _need(all(isinstance(args[key], dict) for key in ("claim_body", "packet_body", "queue_body"))
              and "material" in args["packet_body"], "PM_OWNER_ATOMIC_BODIES_REQUIRED")
        _need(isinstance(args["way_home"], list) and bool(args["way_home"])
              and all(isinstance(v, str) and REF.fullmatch(v) is not None for v in args["way_home"]),
              "PM_OWNER_WAY_HOME_INVALID")
        expected = args["expected_current_receipt_ref"]
        _need(expected is None or isinstance(expected, str) and expected.startswith("pm-receipt:"),
              "PM_OWNER_EXPECTED_CURRENT_INVALID")
        _need(len(_canonical(args).encode("utf-8")) <= 65536, "PM_OWNER_PACKET_TOO_LARGE")
        fingerprint = _hash(args)
        a = self._assignment
        with self._transaction(identity, transport_session, write=True) as cursor:
            self._current_session(cursor, identity, transport_session, write=True)
            cursor.execute("""INSERT INTO cerebro_pm_owner_heads
                (tenant_ref,workspace_ref,project_ref,owner_ref,next_seq)
                VALUES (%s,%s,%s,%s,1) ON CONFLICT DO NOTHING""",
                (a.tenant_ref,a.workspace_ref,a.project_ref,a.owner_ref))
            cursor.execute("""SELECT next_seq FROM cerebro_pm_owner_heads
                WHERE tenant_ref=%s AND workspace_ref=%s AND owner_ref=%s FOR UPDATE""",
                (a.tenant_ref,a.workspace_ref,a.owner_ref))
            head = _row(cursor)
            _need(head is not None, "PM_OWNER_HEAD_MISSING")
            cursor.execute("""SELECT record_payload,snapshot_sha256,request_fingerprint
                FROM cerebro_pm_owner_receipts WHERE tenant_ref=%s AND workspace_ref=%s
                AND owner_ref=%s AND idempotency_key=%s""",
                (a.tenant_ref,a.workspace_ref,a.owner_ref,args["idempotency_key"]))
            prior = _row(cursor)
            if prior is not None:
                _need(prior["request_fingerprint"] == fingerprint, "PM_OWNER_IDEMPOTENCY_CONFLICT")
                return {"record": self._record(prior), "replayed": True}
            cursor.execute("""SELECT receipt_ref FROM cerebro_pm_owner_current
                WHERE tenant_ref=%s AND workspace_ref=%s AND owner_ref=%s AND referent_id=%s FOR UPDATE""",
                (a.tenant_ref,a.workspace_ref,a.owner_ref,args["referent_id"]))
            current = _row(cursor)
            _need((current["receipt_ref"] if current else None) == expected,
                  "PM_OWNER_CURRENT_CAS_CONFLICT")
            previous_revision = None
            if current is not None:
                cursor.execute("""SELECT record_payload,snapshot_sha256
                    FROM cerebro_pm_owner_receipts WHERE tenant_ref=%s AND workspace_ref=%s
                    AND owner_ref=%s AND receipt_ref=%s""",
                    (a.tenant_ref,a.workspace_ref,a.owner_ref,current["receipt_ref"]))
                previous = _row(cursor)
                _need(previous is not None, "PM_OWNER_PREVIOUS_RECEIPT_MISSING")
                previous_revision = self._record(previous)["revision_after"]
            seq = head["next_seq"]
            stamp = uuid.uuid4().hex
            revision = "pm-revision:" + stamp
            packet_sha = _hash(args["packet_body"])
            record = {
                "owner_ref": a.owner_ref, "receipt_ref": "pm-receipt:" + stamp,
                "event_id": "pm-event:" + stamp, "referent_id": args["referent_id"],
                "owner_seq": seq, "provider_revision": seq, "revision_after": revision,
                "revision_before": previous_revision,
                "claim_ref": args["claim_ref"], "packet_ref": args["packet_ref"],
                "queue_ref": args["queue_ref"], "claim_revision": revision,
                "packet_revision": revision, "queue_revision": revision,
                "packet_sha256": packet_sha, "ready_state": args["ready_state"],
                "snapshot_ref": "pm-snapshot:" + stamp, "commit_ref": "pm-commit:" + stamp,
                "readback_ref": "pm-readback:" + stamp,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "way_home": args["way_home"],
            }
            record["snapshot_sha256"] = _hash(record)
            projection = {"material_sha256": _hash(args["packet_body"]["material"]),
                          "source_cut": "pm-cut:" + stamp,
                          "active_hold": args["packet_body"].get("active_hold")}
            _need(projection["active_hold"] is None or isinstance(projection["active_hold"], dict),
                  "PM_OWNER_HOLD_INVALID")
            cursor.execute("""INSERT INTO cerebro_pm_owner_receipts
                (tenant_ref,workspace_ref,project_ref,owner_ref,receipt_ref,referent_id,
                 idempotency_key,request_fingerprint,snapshot_sha256,record_payload,projection_payload,
                 claim_payload,packet_payload,queue_payload)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb)""",
                (a.tenant_ref,a.workspace_ref,a.project_ref,a.owner_ref,record["receipt_ref"],
                 args["referent_id"],args["idempotency_key"],fingerprint,record["snapshot_sha256"],
                 _canonical(record),_canonical(projection),_canonical(args["claim_body"]),
                 _canonical(args["packet_body"]),_canonical(args["queue_body"])))
            cursor.execute("""INSERT INTO cerebro_pm_owner_current
                (tenant_ref,workspace_ref,owner_ref,referent_id,receipt_ref)
                VALUES (%s,%s,%s,%s,%s) ON CONFLICT (tenant_ref,workspace_ref,owner_ref,referent_id)
                DO UPDATE SET receipt_ref=EXCLUDED.receipt_ref""",
                (a.tenant_ref,a.workspace_ref,a.owner_ref,args["referent_id"],record["receipt_ref"]))
            cursor.execute("""UPDATE cerebro_pm_owner_heads SET next_seq=next_seq+1
                WHERE tenant_ref=%s AND workspace_ref=%s AND owner_ref=%s""",
                (a.tenant_ref,a.workspace_ref,a.owner_ref))
            if self._d1_outbox is not None and record["ready_state"] == "MATERIAL_READY":
                # Same transaction: the D1 obligation commits or rolls back together with this receipt.
                self._d1_outbox.enqueue_committed(cursor, record)
        return {"record": record, "replayed": False}

    def read(self, args: dict[str, Any], identity: Any,
             transport_session: Mapping[str, Any] | None) -> dict[str, Any]:
        _need(set(args) in ({"receipt_ref"}, {"referent_id"}), "PM_OWNER_READ_EXACT_SELECTOR_REQUIRED")
        key, value = next(iter(args.items()))
        _need(isinstance(value, str) and REF.fullmatch(value) is not None,
              "PM_OWNER_READ_REF_INVALID")
        a = self._assignment
        with self._transaction(identity, transport_session, write=False) as cursor:
            self._current_session(cursor, identity, transport_session, write=False)
            if key == "referent_id":
                cursor.execute("""SELECT receipt_ref FROM cerebro_pm_owner_current
                    WHERE tenant_ref=%s AND workspace_ref=%s AND owner_ref=%s AND referent_id=%s""",
                    (a.tenant_ref,a.workspace_ref,a.owner_ref,value))
                pointer = _row(cursor)
                _need(pointer is not None, "PM_OWNER_CURRENT_MISSING")
                value = pointer["receipt_ref"]
            cursor.execute("""SELECT record_payload,snapshot_sha256,projection_payload
                FROM cerebro_pm_owner_receipts WHERE tenant_ref=%s AND workspace_ref=%s
                AND owner_ref=%s AND receipt_ref=%s""",
                (a.tenant_ref,a.workspace_ref,a.owner_ref,value))
            row = _row(cursor)
            _need(row is not None, "PM_OWNER_RECEIPT_MISSING")
            record = self._record(row)
            projection = row["projection_payload"]
            if isinstance(projection, str):
                projection = json.loads(projection)
        return {"record": record, "projection": projection, "current": key == "referent_id"}
