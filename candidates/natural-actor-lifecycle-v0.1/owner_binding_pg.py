"""C995 disposable PM owner-binding port, with no production provider claim.

The issuer and NAL closer use different PostgreSQL roles. The close fence uses
the *same cursor and transaction* as PostgresStore.apply; the SECURITY DEFINER
function locks the PM-owned row until that transaction commits or rolls back.
"""
from __future__ import annotations

import copy
from contextlib import ExitStack
from typing import Callable

import psycopg
from psycopg.types.json import Jsonb

from lifecycle import BINDING_SCHEMA, Hold, Lifecycle, digest


class PgOwnerBindingIssuer:
    """Fixture issuer role for local disposable proof, never a real PM signer."""

    def __init__(self, connect: Callable, *, enabled: bool = False):
        self.connect = connect
        self.enabled = enabled

    def _call(self, sql: str, params: tuple = ()):
        if not self.enabled:
            raise Hold("PM_BINDING_ISSUER_DEFAULT_OFF")
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT current_user")
            if cur.fetchone()[0] != "cerebro_pm_binding_issuer":
                raise Hold("PM_BINDING_ISSUER_ROLE_REQUIRED")
            cur.execute(sql, params)
            row = cur.fetchone()
            return copy.deepcopy(row[0]) if row else None

    def _prepare(self, payload: dict) -> dict:
        revision = self._call("SELECT pm_binding.reserve_revision()")
        e = copy.deepcopy(payload)
        for key in ("evidence_ref", "issue_receipt", "current", "revoked",
                    "revoke_receipt", "supersede_receipt", "successor_ref"):
            e.pop(key, None)
        e["schema"] = BINDING_SCHEMA
        e["provider_revision"] = str(revision)
        e["owner_revision"] = "pm-local-r" + str(revision)
        e["canonical_fingerprint"] = digest(Lifecycle._binding_core(e))
        return e

    def issue(self, payload: dict) -> dict:
        e = self._prepare(payload)
        return self._call("SELECT pm_binding.issue(%s)", (Jsonb(e),))

    def supersede(self, old_ref: str, expected_revision: int, payload: dict) -> dict:
        e = self._prepare(payload)
        return self._call("SELECT pm_binding.supersede(%s,%s,%s)",
                          (old_ref, expected_revision, Jsonb(e)))

    def revoke(self, binding_ref: str, expected_revision: int) -> dict:
        return self._call("SELECT pm_binding.revoke(%s,%s)",
                          (binding_ref, expected_revision))

    def read_current(self, binding_ref: str) -> dict | None:
        return self._call("SELECT pm_binding.read_current(%s)", (binding_ref,))

    def read_receipt(self, receipt_ref: str) -> dict | None:
        return self._call("SELECT pm_binding.read_receipt(%s)", (receipt_ref,))


class PgOwnerBindingEvidence:
    """NAL evidence reader overlay: PM binding from PG, other facts unchanged."""

    def __init__(self, connect_closer: Callable, fallback, *, enabled: bool = False,
                 transition_parent_fence_factory: Callable | None = None):
        self.connect_closer = connect_closer
        self.fallback = fallback
        self.enabled = enabled
        self.transition_parent_fence_factory = transition_parent_fence_factory

    def with_parent_factory(self, factory: Callable):
        """Give one task a private fence binding; never mutate a shared reader."""
        if not callable(factory):
            raise Hold("PARENT_AUTHORITY_FENCE_UNAVAILABLE")
        return PgOwnerBindingEvidence(
            self.connect_closer, self.fallback, enabled=self.enabled,
            transition_parent_fence_factory=factory,
        )

    def read(self, kind: str, ref: str) -> dict | None:
        if kind != "OWNER_BINDING":
            return self.fallback.read(kind, ref)
        if not self.enabled:
            raise Hold("PM_BINDING_READER_DEFAULT_OFF")
        try:
            with self.connect_closer() as conn, conn.cursor() as cur:
                cur.execute("SELECT current_user")
                if cur.fetchone()[0] != "cerebro_nal_owner":
                    raise Hold("NAL_CLOSER_ROLE_REQUIRED")
                cur.execute("SELECT pm_binding.read_current(%s)", (ref,))
                row = cur.fetchone()
                return copy.deepcopy(row[0]) if row else None
        except Hold:
            raise
        except Exception as exc:
            raise Hold("PM_BINDING_READ_UNAVAILABLE") from exc

    def owner_commit_fence(self, binding_ref: str, *, cursor=None):
        if not self.enabled:
            raise Hold("PM_BINDING_READER_DEFAULT_OFF")
        if cursor is None:
            raise Hold("OWNER_COMMIT_FENCE_TRANSACTION_PORT_UNAVAILABLE")
        return PgOwnerFence(cursor, binding_ref)

    def transition_authority_fence(self, binding_ref: str, *, transition: str,
                                   task: dict, expected_binding: dict, cursor=None):
        """Require parent authority plus the existing child binding row lock.

        The actor row is already locked by PostgresStore.apply.  This method
        then acquires the injected parent-authority reservation followed by the
        existing same-transaction child binding fence.  Production remains
        default-off until a lawful parent fence factory is bound.
        """
        if not self.enabled:
            raise Hold("PM_BINDING_READER_DEFAULT_OFF")
        if cursor is None:
            raise Hold("OWNER_COMMIT_FENCE_TRANSACTION_PORT_UNAVAILABLE")
        factory = self.transition_parent_fence_factory
        if not callable(factory):
            raise Hold("PARENT_AUTHORITY_FENCE_UNAVAILABLE")
        try:
            parent = factory(
                binding_ref=binding_ref,
                transition=transition,
                task=copy.deepcopy(task),
                expected_binding=copy.deepcopy(expected_binding),
                cursor=cursor,
            )
        except Hold:
            raise
        except Exception as exc:
            raise Hold("PARENT_AUTHORITY_FENCE_UNAVAILABLE") from exc
        if not all(callable(getattr(parent, name, None)) for name in
                   ("__enter__", "__exit__", "assert_current")):
            raise Hold("PARENT_AUTHORITY_FENCE_UNAVAILABLE")
        return PgTransitionAuthorityFence(parent, PgOwnerFence(cursor, binding_ref))


class PgTransitionAuthorityFence:
    """Deterministic actor -> parent -> child lock order for new transitions."""

    def __init__(self, parent_fence, child_fence):
        self.parent_fence = parent_fence
        self.child_fence = child_fence
        self._stack = None

    def __enter__(self):
        self._stack = ExitStack()
        self._stack.__enter__()
        self._stack.enter_context(self.parent_fence)
        self._stack.enter_context(self.child_fence)
        return self

    def __exit__(self, *exc):
        return self._stack.__exit__(*exc) if self._stack is not None else False

    def assert_current(self, expected: dict, transition: str) -> None:
        try:
            self.parent_fence.assert_current()
        except Hold:
            raise
        except Exception as exc:
            raise Hold("PARENT_AUTHORITY_CHANGED_BEFORE_COMMIT") from exc
        self.child_fence.assert_current(expected)


class PgOwnerFence:
    def __init__(self, cursor, binding_ref: str):
        self.cursor = cursor
        self.binding_ref = binding_ref

    def __enter__(self):
        return self

    def __exit__(self, *_):
        # PostgreSQL owns the lock lifetime; it ends with the NAL transaction.
        return False

    def assert_current(self, expected: dict) -> None:
        if expected.get("binding_ref") != self.binding_ref:
            raise Hold("OWNER_BINDING_CHANGED_BEFORE_COMMIT")
        try:
            self.cursor.execute("SELECT pm_binding.acquire_close_fence(" +
                                ",".join(["%s"] * 8) + ")",
                                (self.binding_ref, int(expected["provider_revision"]),
                                 expected["actor_ref"], expected["generation_ref"],
                                 expected["task_ref"], expected["claim_ref"],
                                 expected["packet_ref"], expected["queue_ref"]))
            actual = self.cursor.fetchone()[0]
        except psycopg.errors.RaiseException as exc:
            raise Hold("OWNER_BINDING_CHANGED_BEFORE_COMMIT") from exc
        except Exception as exc:
            raise Hold("OWNER_COMMIT_FENCE_UNAVAILABLE") from exc
        if actual != expected or actual.get("current") is not True or \
                actual.get("revoked") is not False:
            raise Hold("OWNER_BINDING_CHANGED_BEFORE_COMMIT")
