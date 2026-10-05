"""SYNTHETIC owner fixture (reference only). Not a production owner, not a delegation authority.

It is the owner side of the fence in one process: a single lock orders every owner mutation (revoke, amend, expire,
approval changes) against every atomic_read() section, and a monotonic currentness counter stamps each read.
"""
from __future__ import annotations

import dataclasses
import threading
from contextlib import contextmanager
from typing import Iterator

from ..views import ApprovalView, DelegationView

LABEL = "SYNTHETIC_OWNER_FIXTURE_NOT_PRODUCTION"


class _Snapshot:
    def __init__(self, owner: "SyntheticOwner") -> None:
        self._owner = owner

    def currentness(self) -> int:
        return self._owner._seq

    def read_delegation(self, delegation_ref: str) -> "DelegationView | None":
        view = self._owner._delegations.get(delegation_ref)
        return None if view is None else dataclasses.replace(view, owner_currentness=self._owner._seq)

    def read_approval(self, approval_ref: str) -> "ApprovalView | None":
        return self._owner._approvals.get(approval_ref)


class SyntheticOwner:
    label = LABEL

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._seq = 0
        self._delegations: dict[str, DelegationView] = {}
        self._approvals: dict[str, ApprovalView] = {}
        self.log: list[tuple] = []  # (currentness_seq, event, ref)

    def _tick(self, event: str, ref: str) -> int:
        self._seq += 1
        self.log.append((self._seq, event, ref))
        return self._seq

    @contextmanager
    def atomic_read(self) -> Iterator[_Snapshot]:
        with self._lock:
            yield _Snapshot(self)

    # -- owner-side mutations (the only way state changes in this fixture) ---------------------------------------------
    def put_delegation(self, view: DelegationView) -> None:
        with self._lock:
            self._delegations[view.delegation_ref] = view
            self._tick("PUT_DELEGATION", view.delegation_ref)

    def amend(self, delegation_ref: str, **changes: object) -> None:
        """New owner revision (revision + 1) with the given field changes; status stays unless changed."""
        with self._lock:
            old = self._delegations[delegation_ref]
            self._delegations[delegation_ref] = dataclasses.replace(
                old, delegation_revision=old.delegation_revision + 1, **changes)
            self._tick("AMEND", delegation_ref)

    def revoke(self, delegation_ref: str) -> int:
        with self._lock:
            self.amend(delegation_ref, status="REVOKED")
            return self._tick("REVOKE", delegation_ref)

    def expire(self, delegation_ref: str) -> None:
        with self._lock:
            self.amend(delegation_ref, status="EXPIRED")

    def put_approval(self, view: ApprovalView) -> None:
        with self._lock:
            self._approvals[view.approval_ref] = view
            self._tick("PUT_APPROVAL", view.approval_ref)

    def withdraw_approval(self, approval_ref: str) -> None:
        with self._lock:
            self._approvals[approval_ref] = dataclasses.replace(self._approvals[approval_ref], status="WITHDRAWN")
            self._tick("WITHDRAW_APPROVAL", approval_ref)

    def seq_of(self, event: str, ref: str) -> "int | None":
        with self._lock:
            return next((s for s, e, r in self.log if e == event and r == ref), None)

    def read_for_test(self, delegation_ref: str) -> DelegationView:
        with self._lock:
            return self._delegations[delegation_ref]
