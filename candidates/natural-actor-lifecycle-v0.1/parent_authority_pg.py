"""P1625 disposable parent-authority port; no production connection or issuer."""
from __future__ import annotations

import copy
from typing import Callable

import psycopg
from psycopg.types.json import Jsonb

from lifecycle import Hold, digest


class PgParentAuthority:
    """Read-only worker port plus same-cursor parent reservation, default OFF."""

    def __init__(self, connect_worker: Callable | None = None, *, enabled: bool = False):
        self.connect_worker = connect_worker
        self.enabled = enabled

    def _read(self, sql: str, params: tuple) -> dict:
        if not self.enabled or self.connect_worker is None:
            raise Hold("PARENT_AUTHORITY_DEFAULT_OFF")
        try:
            with self.connect_worker() as conn, conn.cursor() as cur:
                cur.execute("SELECT current_user")
                if cur.fetchone()[0] != "cerebro_nal_owner":
                    raise Hold("PARENT_WORKER_ROLE_REQUIRED")
                cur.execute(sql, params)
                row = cur.fetchone()
                if row is None or not isinstance(row[0], dict):
                    raise Hold("PARENT_OWNER_READBACK_MISSING")
                return copy.deepcopy(row[0])
        except Hold:
            raise
        except Exception as exc:
            raise Hold("PARENT_OWNER_READ_UNAVAILABLE") from exc

    def read_current(self, *, mandate_ref: str | None = None,
                     pm_actor_ref: str | None = None,
                     pm_generation_ref: str | None = None) -> dict:
        # The same object can fill BoundPmAuthorityV02's two explicit reader ports.
        if mandate_ref is None:
            raise Hold("MANDATE_REF_REQUIRED")
        if pm_actor_ref is None and pm_generation_ref is None:
            return self._read("SELECT pm_authority.read_rights(%s)", (mandate_ref,))
        if not pm_actor_ref or not pm_generation_ref:
            raise Hold("PM_ACTOR_GENERATION_REQUIRED")
        return self._read(
            "SELECT pm_authority.read_delegation(%s,%s,%s)",
            (pm_actor_ref, pm_generation_ref, mandate_ref),
        )

    def hold_current(self, *, basis: dict, basis_fingerprint: str,
                     expected_mandate_owner_revision: int,
                     expected_delegation_owner_revision: int,
                     expected_delegation_fence_revision: int,
                     cursor=None, expected_task: dict | None = None):
        if not self.enabled:
            raise Hold("PARENT_AUTHORITY_DEFAULT_OFF")
        if cursor is None or expected_task is None:
            raise Hold("PARENT_SAME_TRANSACTION_CURSOR_REQUIRED")
        if digest(basis) != basis_fingerprint:
            raise Hold("PARENT_BASIS_FINGERPRINT_MISMATCH")
        return PgParentFence(
            cursor, copy.deepcopy(basis), basis_fingerprint,
            (expected_mandate_owner_revision,
             expected_delegation_owner_revision,
             expected_delegation_fence_revision),
            copy.deepcopy(expected_task),
        )


class PgParentFence:
    def __init__(self, cursor, basis: dict, fingerprint: str,
                 revisions: tuple[int, int, int], expected_task: dict):
        self.cursor = cursor
        self.basis = basis
        self.fingerprint = fingerprint
        self.revisions = revisions
        self.expected_task = expected_task

    def __enter__(self):
        # Reserve before NAL drafts state/ledgers. The same check runs again at
        # the commit guard; owner row locks persist across both checks.
        self.assert_current()
        return self

    def __exit__(self, *_):
        # The database transaction owns the row locks, including at COMMIT.
        return False

    def assert_current(self) -> None:
        if digest(self.basis) != self.fingerprint:
            raise Hold("PARENT_BASIS_FINGERPRINT_MISMATCH")
        try:
            self.cursor.execute("SELECT current_user")
            if self.cursor.fetchone()[0] != "cerebro_nal_owner":
                raise Hold("PARENT_WORKER_ROLE_REQUIRED")
            self.cursor.execute(
                "SELECT pm_authority.acquire_parent_fence(%s,%s,%s,%s,%s)",
                (Jsonb(self.basis), self.fingerprint, *self.revisions),
            )
            row = self.cursor.fetchone()
            actual = row[0] if row else None
        except Hold:
            raise
        except psycopg.Error as exc:
            if exc.sqlstate == "P0T01":
                raise Hold("PARENT_TIMED_EXPIRY_UNSUPPORTED") from exc
            if isinstance(exc, psycopg.errors.RaiseException):
                raise Hold("PARENT_AUTHORITY_CHANGED_BEFORE_COMMIT") from exc
            raise Hold("PARENT_AUTHORITY_FENCE_UNAVAILABLE") from exc
        except Exception as exc:
            raise Hold("PARENT_AUTHORITY_FENCE_UNAVAILABLE") from exc
        if not isinstance(actual, dict) or actual.get("readback_verified") is not True:
            raise Hold("PARENT_AUTHORITY_READBACK_MISSING")
        if any(actual.get(k) != self.expected_task.get(k) for k in
               ("claim_ref", "packet_ref", "queue_ref")):
            raise Hold("PARENT_TASK_LINEAGE_MISMATCH")
        if actual.get("provenance_ref") != self.expected_task.get("readback_ref"):
            raise Hold("PARENT_TASK_PROVENANCE_MISMATCH")
        if any(actual.get(k) != self.basis.get(k) for k in
               ("decision_ref", "task_ref", "task_revision", "task_sha256",
                "mandate_ref", "delegation_ref")):
            raise Hold("PARENT_DECISION_BASIS_MISMATCH")
        if actual.get("basis_fingerprint") != self.fingerprint:
            raise Hold("PARENT_DECISION_BASIS_MISMATCH")


class PgParentAuthorityIssuer:
    """Separate local fixture issuance; cannot run through the worker role."""

    ROLES = {
        "rights": "cerebro_human_rights_issuer",
        "delegation": "cerebro_pm_admin_issuer",
        "decision": "cerebro_pm_decision_issuer",
    }

    def __init__(self, connect: Callable | None, role: str, *, enabled: bool = False):
        if role not in self.ROLES:
            raise ValueError("unknown issuer kind")
        self.connect = connect
        self.role = role
        self.enabled = enabled

    def _call(self, sql: str, params: tuple) -> dict:
        if not self.enabled or self.connect is None:
            raise Hold("PARENT_ISSUER_DEFAULT_OFF")
        try:
            with self.connect() as conn, conn.cursor() as cur:
                cur.execute("SELECT current_user")
                if cur.fetchone()[0] != self.ROLES[self.role]:
                    raise Hold("PARENT_ISSUER_ROLE_REQUIRED")
                cur.execute(sql, params)
                row = cur.fetchone()
                if row is None or not isinstance(row[0], dict):
                    raise Hold("PARENT_ISSUE_RECEIPT_MISSING")
                return copy.deepcopy(row[0])
        except Hold:
            raise
        except Exception as exc:
            raise Hold("PARENT_ISSUE_UNAVAILABLE") from exc

    def issue_rights(self, rights: dict, expires_at=None) -> dict:
        if self.role != "rights":
            raise Hold("RIGHTS_ISSUER_ROLE_REQUIRED")
        return self._call("SELECT pm_authority.issue_rights(%s,%s)",
                          (Jsonb(rights), expires_at))

    def issue_delegation(self, delegation: dict, expires_at=None) -> dict:
        if self.role != "delegation":
            raise Hold("DELEGATION_ISSUER_ROLE_REQUIRED")
        return self._call("SELECT pm_authority.issue_delegation(%s,%s)",
                          (Jsonb(delegation), expires_at))

    def issue_decision(self, snapshot: dict, expires_at=None) -> dict:
        if self.role != "decision":
            raise Hold("DECISION_ISSUER_ROLE_REQUIRED")
        basis, task = snapshot["immutable_basis"], snapshot["task"]
        if digest(basis) != snapshot["basis_fingerprint"]:
            raise Hold("PARENT_BASIS_FINGERPRINT_MISMATCH")
        return self._call(
            "SELECT pm_authority.issue_decision(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (basis["decision_ref"], basis["task_ref"], basis["task_revision"],
             basis["task_sha256"], task["claim_ref"], task["packet_ref"],
             task["queue_ref"], task["readback_ref"], Jsonb(basis),
             snapshot["basis_fingerprint"], expires_at),
        )

    def retire(self, ref: str, expected_revision: int, status: str) -> dict:
        if status not in {"SUPERSEDED", "EXPIRED"}:
            raise Hold("PARENT_RETIRE_STATUS_INVALID")
        sql = {
            "rights": "SELECT pm_authority.retire_rights(%s,%s,%s)",
            "delegation": "SELECT pm_authority.retire_delegation(%s,%s,%s)",
            "decision": "SELECT pm_authority.retire_decision(%s,%s,%s)",
        }[self.role]
        return self._call(sql, (ref, expected_revision, status))

    def revoke(self, ref: str, expected_revision: int) -> dict:
        sql = {
            "rights": "SELECT pm_authority.revoke_rights(%s,%s)",
            "delegation": "SELECT pm_authority.revoke_delegation(%s,%s)",
            "decision": "SELECT pm_authority.revoke_decision(%s,%s)",
        }[self.role]
        return self._call(sql, (ref, expected_revision))
