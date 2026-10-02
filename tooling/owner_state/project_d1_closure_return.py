"""Default-off Project Engine D1 closure ReturnPort on the owner-state DB.

No event is selected or generated here. A future Project owner supplies a
trusted committed-event reader; this port only persists and confirms its
selected return. CLOSED is never returned before an independent exact readback.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Protocol

try:
    from ..context.control_context_state_postgres import _fetchone, _sha256
except ImportError:
    from control_context_state_postgres import _fetchone, _sha256


class ProjectOwnerCommitReader(Protocol):
    def read_committed_owner_event(
        self, *, tenant_ref: str, workspace_ref: str, project_ref: str,
        owner_event_key: str,
    ) -> dict[str, Any]: ...


class ProjectD1ReturnPort(Protocol):
    def append_and_confirm(self, *, tenant_ref: str, workspace_ref: str,
                           project_ref: str, receiver_ref: str, closure_id: str,
                           closure_fingerprint: str, owner_event_key: str,
                           owner_event_fingerprint: str) -> dict[str, Any]: ...


_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")


def _hold(reason: str, *, mutation: bool | str = False) -> dict[str, Any]:
    return {"result": "HOLD", "closure_state": "NOT_CLOSED",
            "mutated": mutation, "reason": reason}


class PostgresProjectD1ClosureReturnPort:
    """One immutable closure row per exact Project/receiver/closure key."""

    def __init__(self, *, connection_factory: Callable[[], Any],
                 owner_commit_reader: ProjectOwnerCommitReader):
        if not callable(connection_factory):
            raise ValueError("owner-state-connection-factory-required")
        if not callable(getattr(owner_commit_reader, "read_committed_owner_event", None)):
            raise ValueError("project-owner-committed-event-reader-required")
        self._connect = connection_factory
        self._owner_reader = owner_commit_reader

    @staticmethod
    def _scope(cursor: Any, *, tenant_ref: str, workspace_ref: str,
               project_ref: str) -> None:
        for name, value in (("tenant_ref", tenant_ref), ("workspace_ref", workspace_ref),
                            ("project_ref", project_ref)):
            cursor.execute("SELECT set_config(%s, %s, true)", ("cerebro." + name, value))

    def append_and_confirm(self, *, tenant_ref: str, workspace_ref: str,
                           project_ref: str, receiver_ref: str, closure_id: str,
                           closure_fingerprint: str, owner_event_key: str,
                           owner_event_fingerprint: str) -> dict[str, Any]:
        identity = (tenant_ref, workspace_ref, project_ref, receiver_ref, closure_id)
        if any(not isinstance(value, str) or not value.strip() or value != value.strip()
               for value in identity + (owner_event_key,)):
            return _hold("closure-exact-identity-required")
        if any(_FINGERPRINT.fullmatch(value or "") is None for value in
               (closure_fingerprint, owner_event_fingerprint)):
            return _hold("closure-fingerprint-invalid")
        try:
            owner = self._owner_reader.read_committed_owner_event(
                tenant_ref=tenant_ref, workspace_ref=workspace_ref,
                project_ref=project_ref, owner_event_key=owner_event_key)
        except Exception:
            return _hold("project-owner-commit-readback-unavailable")
        if not isinstance(owner, dict) or any((
            owner.get("tenant_ref") != tenant_ref,
            owner.get("workspace_ref") != workspace_ref,
            owner.get("project_ref") != project_ref,
            owner.get("owner") != "project",
            owner.get("event_state") != "OWNER_EFFECT_COMMITTED",
            owner.get("owner_event_key") != owner_event_key,
            owner.get("owner_event_fingerprint") != owner_event_fingerprint,
            owner.get("provider_readback_verified") is not True,
        )):
            return _hold("project-owner-commit-not-proven")

        receipt = {
            "schema": "cerebro-project-d1-closure-receipt/v1",
            "tenant_ref": tenant_ref, "workspace_ref": workspace_ref,
            "project_ref": project_ref, "receiver_ref": receiver_ref,
            "closure_id": closure_id, "closure_revision": 1,
            "closure_fingerprint": closure_fingerprint,
            "owner_event_key": owner_event_key,
            "owner_event_fingerprint": owner_event_fingerprint,
        }
        receipt_ref = "D1C-" + _sha256(identity)[:32].upper()
        receipt_fingerprint = _sha256({**receipt, "receipt_ref": receipt_ref})
        import json
        payload = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
        expected = {
            "closure_revision": 1, "closure_fingerprint": closure_fingerprint,
            "owner_event_key": owner_event_key,
            "owner_event_fingerprint": owner_event_fingerprint,
            "receipt_ref": receipt_ref, "receipt_fingerprint": receipt_fingerprint,
            "receipt_payload": receipt,
        }
        connection = None
        inserted = False
        try:
            connection = self._connect()
            with connection.cursor() as cursor:
                self._scope(cursor, tenant_ref=tenant_ref, workspace_ref=workspace_ref,
                            project_ref=project_ref)
                cursor.execute(
                    """INSERT INTO cerebro_project_d1_closure_ledger
                       (tenant_ref,workspace_ref,project_ref,receiver_ref,closure_id,
                        closure_fingerprint,owner_event_key,owner_event_fingerprint,
                        receipt_ref,receipt_fingerprint,receipt_payload)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                       ON CONFLICT (tenant_ref,workspace_ref,project_ref,receiver_ref,closure_id)
                       DO NOTHING RETURNING closure_revision""",
                    identity + (closure_fingerprint, owner_event_key,
                                owner_event_fingerprint, receipt_ref,
                                receipt_fingerprint, payload),
                )
                inserted = _fetchone(cursor) is not None
                cursor.execute(
                    """SELECT closure_revision,closure_fingerprint,owner_event_key,
                              owner_event_fingerprint,receipt_ref,receipt_fingerprint,
                              receipt_payload
                         FROM cerebro_project_d1_closure_ledger
                        WHERE tenant_ref=%s AND workspace_ref=%s AND project_ref=%s
                          AND receiver_ref=%s AND closure_id=%s""", identity)
                row = _fetchone(cursor)
                if row is None:
                    connection.rollback()
                    return _hold("closure-collision-visibility-unproven")
                if row != expected:
                    connection.rollback()
                    return {"result": "CONFLICT_HOLD", "closure_state": "NOT_CLOSED",
                            "mutated": False, "reason": "closure-key-fingerprint-conflict"}
            connection.commit()
        except Exception:
            if connection is not None:
                try:
                    connection.rollback()
                except Exception:
                    pass
            return _hold("closure-write-commit-unproven", mutation="UNKNOWN")
        finally:
            if connection is not None:
                connection.close()

        read_connection = None
        try:
            read_connection = self._connect()
            if read_connection is connection:
                return _hold("closure-independent-readback-required", mutation=inserted)
            with read_connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                self._scope(cursor, tenant_ref=tenant_ref, workspace_ref=workspace_ref,
                            project_ref=project_ref)
                cursor.execute(
                    """SELECT closure_revision,closure_fingerprint,owner_event_key,
                              owner_event_fingerprint,receipt_ref,receipt_fingerprint,
                              receipt_payload
                         FROM cerebro_project_d1_closure_ledger
                        WHERE tenant_ref=%s AND workspace_ref=%s AND project_ref=%s
                          AND receiver_ref=%s AND closure_id=%s""", identity)
                row = _fetchone(cursor)
            read_connection.commit()
            if row != expected:
                return _hold("closure-exact-provider-readback-mismatch", mutation=inserted)
        except Exception:
            if read_connection is not None:
                try:
                    read_connection.rollback()
                except Exception:
                    pass
            return _hold("closure-provider-readback-unavailable", mutation=inserted)
        finally:
            if read_connection is not None:
                read_connection.close()
        return {"result": "PASS", "closure_state": "CLOSED", "mutated": inserted,
                "receipt_ref": receipt_ref, "receipt_fingerprint": receipt_fingerprint,
                "closure_revision": 1, "receipt": receipt}
