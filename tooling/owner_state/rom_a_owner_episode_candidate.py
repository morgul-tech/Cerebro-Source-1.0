#!/usr/bin/env python3
"""Inactive A1 admission candidate; explicit authenticated owner fence required.

This is PR95's corrected admission contract, separated from the normal
Project/Quality/Convergence port. Importing it opens no connection, registers
no host adapter and does not select its separate candidate migration manifest.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
from typing import Any, Callable, Iterator

if __package__:
    from .owner_state_persistence import (
        PostgresOwnerStatePersistencePort,
        StateAuthorizationError,
        StateConflict,
        StateServiceUnavailable,
        _canonical_text,
        _fetchone,
        _json_value,
        _require,
        _sha256,
    )
else:
    from owner_state_persistence import (
        PostgresOwnerStatePersistencePort,
        StateAuthorizationError,
        StateConflict,
        StateServiceUnavailable,
        _canonical_text,
        _fetchone,
        _json_value,
        _require,
        _sha256,
    )

from control_context_state_postgres import _fetchall


ROM_A_EPISODE_SCHEMA = "cerebro-rom-a-owner-episode/v1"


def _rom_a_episode_state(value: Any) -> dict[str, Any]:
    """Validate one complete A1-owned admission basis, not caller authority."""
    _require(isinstance(value, dict) and value.get("schema") == ROM_A_EPISODE_SCHEMA,
             "rom-a-owner-episode-schema-required")
    names = ("episode_ref", "task_ref", "task_revision", "actor_ref", "scope_ref",
             "mandate_ref", "mandate_revision", "human_provenance_ref",
             "dependency_ref", "dependency_revision", "dependency_source_ref",
             "selected_sha256")
    _require(all(isinstance(value.get(name), str) and bool(value[name].strip())
                 and value[name] == value[name].strip() for name in names),
             "rom-a-owner-episode-exact-refs-required")
    _require(len(value["selected_sha256"]) == 64
             and all(ch in "0123456789abcdef" for ch in value["selected_sha256"]),
             "rom-a-owner-selected-digest-invalid")
    _require(value.get("owner_ref") == "A1"
             and value.get("progress_state") in {"WAITING_DEPENDENCY", "STARTED", "DONE"}
             and value.get("dependency_state") in {"RESOLVED", "UNRESOLVED", "REOPENED"}
             and value.get("dependency_disposition") == "SOURCE_AFTER_DEPENDENCY"
             and all(type(value.get(name)) is bool for name in
                     ("paused", "revoked", "all_dependencies_resolved", "dependency_applicable"))
             and isinstance(value.get("other_unresolved_gates"), list)
             and all(isinstance(gate, str) and gate for gate in value["other_unresolved_gates"]),
             "rom-a-owner-episode-gates-invalid")
    return copy.deepcopy(value)


class PostgresRomAOwnerEpisodePort(PostgresOwnerStatePersistencePort):
    """No-send A1 admission prototype in the existing PostgreSQL transaction home.

    The constructor-bound ingress verifier is the only source of Human mandate
    and owner identity. This port never calls a sender and cannot satisfy the
    host's send_selected_under_owner_fence operation.
    """

    def __init__(self, connection_factory: Callable[[], Any], ingress_verifier: Any):
        super().__init__(connection_factory)
        _require(callable(getattr(ingress_verifier, "verify_current", None)),
                 "rom-a-authenticated-ingress-unbound")
        _require(callable(getattr(ingress_verifier, "current_fence", None)),
                 "rom-a-owner-currentness-fence-unbound", StateAuthorizationError)
        self._ingress_verifier = ingress_verifier

    @contextmanager
    def _fenced_current(self, operation: str, request: dict[str, Any]) -> Iterator[tuple[dict, dict]]:
        """Keep the owner/mandate fence through readback, CAS and database commit.

        The host-owned ingress adapter must serialize pause, revocation and
        dependency changes with this scope. A database row lock alone cannot
        fence an external owner. No live implementation is supplied here.
        """
        completed = False
        with self._ingress_verifier.current_fence(
            operation=operation, request=copy.deepcopy(request)
        ):
            yield self._trusted(operation, request)
            completed = True
        # An ingress context manager must not turn a failed/uncertain commit
        # into a successful return by suppressing the exception from the body.
        _require(completed, "rom-a-owner-fence-suppressed-failure", StateServiceUnavailable)

    @staticmethod
    def _expected_revision(value: Any) -> None:
        _require(type(value) is int and value >= 1,
                 "rom-a-positive-integer-owner-revision-required", StateConflict)

    def _trusted(self, operation: str, request: dict[str, Any]) -> tuple[dict, dict]:
        evidence = self._ingress_verifier.verify_current(
            operation=operation, request=copy.deepcopy(request))
        _require(isinstance(evidence, dict) and evidence.get("authenticated") is True
                 and evidence.get("currentness") == "CURRENT"
                 and evidence.get("owner_ref") == "A1"
                 and evidence.get("human_mandate_readback") is True,
                 "rom-a-current-authenticated-owner-ingress-required", StateAuthorizationError)
        identity = {name: evidence.get(name) for name in
                    ("tenant_ref", "workspace_ref", "principal_ref")}
        _require(all(isinstance(value, str) and bool(value.strip()) and value == value.strip()
                     for value in identity.values()),
                 "rom-a-ingress-identity-required", StateAuthorizationError)
        state = _rom_a_episode_state(evidence.get("owner_state"))
        _require(state["episode_ref"] == request.get("episode_ref"),
                 "rom-a-ingress-episode-mismatch", StateAuthorizationError)
        return identity, state

    @staticmethod
    def _row(cursor: Any, identity: dict, episode_ref: str) -> dict | None:
        cursor.execute(
            "SELECT owner_revision, owner_payload, admission_basis, admission_state "
            "FROM cerebro_rom_a_owner_episodes WHERE tenant_ref=%s AND workspace_ref=%s "
            "AND principal_ref=%s AND episode_ref=%s FOR UPDATE",
            (*identity.values(), episode_ref))
        return _fetchone(cursor)

    def provision(self, *, episode_ref: str) -> dict[str, Any]:
        with self._fenced_current("PROVISION", {"episode_ref": episode_ref}) as (identity, state):
            with self._transaction(**identity) as cursor:
                _require(self._row(cursor, identity, episode_ref) is None,
                         "rom-a-episode-already-provisioned", StateConflict)
                cursor.execute(
                    "INSERT INTO cerebro_rom_a_owner_episodes "
                    "(tenant_ref, workspace_ref, principal_ref, episode_ref, owner_revision, owner_payload) "
                    "VALUES (%s,%s,%s,%s,1,%s::jsonb)",
                    (*identity.values(), episode_ref, _canonical_text(state)))
        return {"state": "PROVISIONED", "episode_ref": episode_ref, "owner_revision": 1,
                "authority_added": "NONE", "delivery": "NOT_ATTEMPTED"}

    def revise(self, *, episode_ref: str, expected_owner_revision: int) -> dict[str, Any]:
        self._expected_revision(expected_owner_revision)
        with self._fenced_current("REVISE", {
            "episode_ref": episode_ref, "expected_owner_revision": expected_owner_revision
        }) as (identity, state):
            with self._transaction(**identity) as cursor:
                row = self._row(cursor, identity, episode_ref)
                _require(row is not None and row["owner_revision"] == expected_owner_revision,
                         "rom-a-owner-revision-conflict", StateConflict)
                previous = _json_value(row["owner_payload"], field="rom-a-owner-payload")
                _require(previous.get("task_ref") == state["task_ref"]
                         and previous.get("episode_ref") == state["episode_ref"],
                         "rom-a-episode-task-immutable", StateConflict)
                cursor.execute(
                    "UPDATE cerebro_rom_a_owner_episodes SET owner_revision=%s, "
                    "owner_payload=%s::jsonb, updated_at=now() WHERE tenant_ref=%s "
                    "AND workspace_ref=%s AND principal_ref=%s AND episode_ref=%s",
                    (expected_owner_revision + 1, _canonical_text(state),
                     *identity.values(), episode_ref))
        return {"state": "REVISED", "episode_ref": episode_ref,
                "owner_revision": expected_owner_revision + 1,
                "delivery": "NOT_ATTEMPTED"}

    def read_episode(self, *, episode_ref: str) -> dict[str, Any]:
        with self._fenced_current("READ", {"episode_ref": episode_ref}) as (identity, current):
            with self._transaction(**identity) as cursor:
                row = self._row(cursor, identity, episode_ref)
                _require(row is not None, "rom-a-owner-episode-missing", StateConflict)
                self._expected_revision(row["owner_revision"])
                persisted = _rom_a_episode_state(
                    _json_value(row["owner_payload"], field="rom-a-owner-payload"))
                _require(persisted["episode_ref"] == episode_ref,
                         "rom-a-persisted-episode-mismatch", StateConflict)
                cursor.execute(
                    "SELECT basis_fingerprint, owner_revision, admission_state "
                    "FROM cerebro_rom_a_admissions WHERE tenant_ref=%s "
                    "AND workspace_ref=%s AND principal_ref=%s AND episode_ref=%s",
                    (*identity.values(), episode_ref))
                admissions = []
                for item in _fetchall(cursor):
                    basis = item.get("basis_fingerprint")
                    revision = item.get("owner_revision")
                    _require(isinstance(basis, str) and len(basis) == 64
                             and all(ch in "0123456789abcdef" for ch in basis)
                             and type(revision) is int and 1 <= revision <= row["owner_revision"]
                             and item.get("admission_state") == "ADMITTED_UNCERTAIN",
                             "rom-a-persisted-admission-invalid", StateConflict)
                    admissions.append({"basis_fingerprint": basis,
                                       "owner_revision": revision, "state": "ADMITTED_UNCERTAIN"})
        # Historical admission debt survives a changed current owner basis. It
        # never makes that changed basis eligible and never proves delivery.
        persisted_basis, current_basis = _sha256(persisted), _sha256(current)
        matching = persisted_basis == current_basis
        return {"state": "CURRENT_READBACK" if matching else "OWNER_BASIS_DRIFT",
                "currentness": "CURRENT" if matching else "STALE", "episode_ref": episode_ref,
                "owner_revision": row["owner_revision"],
                "owner_basis_fingerprint": persisted_basis,
                "current_owner_basis_fingerprint": current_basis,
                "admitted_basis_fingerprints": sorted(item["basis_fingerprint"] for item in admissions),
                "admissions": sorted(admissions, key=lambda item: item["basis_fingerprint"]),
                "delivery": "NOT_PROVEN", "recipient_use": "NOT_PROVEN", "effect": "NOT_PROVEN"}

    def admit(self, *, episode_ref: str, expected_owner_revision: int,
              expected_basis_fingerprint: str) -> dict[str, Any]:
        self._expected_revision(expected_owner_revision)
        with self._fenced_current("ADMIT", {
            "episode_ref": episode_ref, "expected_owner_revision": expected_owner_revision,
            "expected_basis_fingerprint": expected_basis_fingerprint
        }) as (identity, current):
            basis = _sha256(current)
            _require(basis == expected_basis_fingerprint,
                     "rom-a-current-owner-basis-mismatch", StateConflict)
            _require(current["paused"] is False and current["revoked"] is False
                     and current["progress_state"] == "WAITING_DEPENDENCY"
                     and current["dependency_state"] == "RESOLVED"
                     and current["dependency_applicable"] is True
                     and current["all_dependencies_resolved"] is True
                     and not current["other_unresolved_gates"],
                     "rom-a-current-owner-gate-closed", StateConflict)
            with self._transaction(**identity) as cursor:
                row = self._row(cursor, identity, episode_ref)
                _require(row is not None and row["owner_revision"] == expected_owner_revision,
                         "rom-a-owner-revision-conflict", StateConflict)
                persisted = _json_value(row["owner_payload"], field="rom-a-owner-payload")
                _require(_sha256(persisted) == basis,
                         "rom-a-owner-basis-changed-before-admission", StateConflict)
                cursor.execute(
                    "SELECT basis_fingerprint FROM cerebro_rom_a_admissions WHERE tenant_ref=%s "
                    "AND workspace_ref=%s AND principal_ref=%s AND episode_ref=%s "
                    "AND basis_fingerprint=%s FOR UPDATE",
                    (*identity.values(), episode_ref, basis))
                _require(_fetchone(cursor) is None, "rom-a-same-basis-already-admitted", StateConflict)
                cursor.execute(
                    "INSERT INTO cerebro_rom_a_admissions "
                    "(tenant_ref,workspace_ref,principal_ref,episode_ref,basis_fingerprint,owner_revision) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    (*identity.values(), episode_ref, basis, expected_owner_revision))
                cursor.execute(
                    "UPDATE cerebro_rom_a_owner_episodes SET admission_basis=%s, "
                    "admission_state='ADMITTED_UNCERTAIN', updated_at=now() "
                    "WHERE tenant_ref=%s AND workspace_ref=%s AND principal_ref=%s AND episode_ref=%s",
                    (basis, *identity.values(), episode_ref))
        return {"state": "ADMITTED_UNCERTAIN", "episode_ref": episode_ref,
                "basis_fingerprint": basis, "owner_revision": expected_owner_revision,
                "delivery": "NOT_PROVEN", "recipient_use": "NOT_PROVEN", "effect": "NOT_PROVEN"}
