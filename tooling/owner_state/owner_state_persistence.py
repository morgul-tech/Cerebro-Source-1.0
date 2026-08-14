#!/usr/bin/env python3
"""Durable owner-state infrastructure for Project, Quality and Convergence.

Owner modules retain all semantic authority.  This port only validates their
complete state shapes, binds a precommit owner receipt, performs CAS/idempotent
PostgreSQL persistence, and emits a commit receipt after ``commit()`` succeeds.
Context state uses its dedicated hierarchical control-state port.
"""

from __future__ import annotations

import copy
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator


SOURCE_ROOT = Path(__file__).resolve().parents[2]
PATHS = (
    SOURCE_ROOT / "mcp",
    SOURCE_ROOT / "tooling" / "context",
    SOURCE_ROOT / "engines" / "project",
    SOURCE_ROOT / "engines" / "quality",
    SOURCE_ROOT / "engines" / "convergence",
)
for path in PATHS:
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from control_context_state_port import (  # noqa: E402
    StateAuthorizationError,
    StateBindingError,
    StateConflict,
    StatePortError,
    StateServiceUnavailable,
)
from control_context_state_postgres import (  # noqa: E402
    _canonical_text,
    _fetchone,
    _json_value,
    _mapped_database_error,
    _safe_close,
    _safe_rollback,
    _sha256,
)
from control_owner_effect_receipt import (  # noqa: E402
    build_owner_effect_receipt,
    validate_owner_effect_receipt,
)
from convergence_owner_effect import validate_convergence_state  # noqa: E402
from project_owner_effect import validate_project_basis  # noqa: E402
from quality_owner_effect import quality_trace_fingerprint, validate_quality_trace  # noqa: E402


OWNER_STATE_COMMIT_SCHEMA = "cerebro-owner-state-commit-receipt/v1"
OWNER_STATE_COMPLETION_SCHEMA = "cerebro-owner-state-persistence-completion/v1"
OWNER_VERIFICATION_SCHEMA = "cerebro-owner-state-persistence-verification/v1"
OWNERS = {"project", "quality", "convergence"}
EFFECTS = {
    "project": "REVISION_REQUIRED",
    "quality": "INVALIDATE_AFFECTED",
    "convergence": "REVALIDATE_AFFECTED",
}


class OwnerStatePersistenceError(StatePortError):
    pass


def _require(
    condition: bool,
    message: str,
    error: type[StatePortError] = OwnerStatePersistenceError,
) -> None:
    if not condition:
        raise error(message)


def _state_projection(owner: str, state: dict[str, Any]) -> dict[str, Any]:
    _require(owner in OWNERS, "owner-state-owner-invalid")
    _require(isinstance(state, dict), "owner-state-payload-object-required")
    try:
        if owner == "project":
            validate_project_basis(state)
            return {
                "owner": owner,
                "aggregate_ref": state["project_ref"],
                "state_ref": state["basis_ref"],
                "state_schema": state["schema"],
                "state_fingerprint": state["basis_fingerprint"],
            }
        if owner == "quality":
            validate_quality_trace(state)
            return {
                "owner": owner,
                "aggregate_ref": state["work_item_ref"],
                "state_ref": state["work_item_ref"],
                "state_schema": state["schema"],
                "state_fingerprint": quality_trace_fingerprint(state),
            }
        validate_convergence_state(state)
        return {
            "owner": owner,
            "aggregate_ref": state["state_ref"],
            "state_ref": state["state_ref"],
            "state_schema": state["schema"],
            "state_fingerprint": state["state_fingerprint"],
        }
    except Exception as exc:
        raise OwnerStatePersistenceError(f"{owner}-owner-state-validation-failed") from exc


def build_owner_state_commit_receipt(
    *,
    commit_kind: str,
    tenant_ref: str,
    workspace_ref: str,
    principal_ref: str,
    consumer_ref: str,
    session_ref: str,
    project_ref: str,
    owner: str,
    aggregate_ref: str,
    event_id: str,
    idempotency_key: str,
    request_fingerprint: str,
    owner_revision_before: int,
    owner_revision_after: int,
    input_state_ref: str | None,
    input_state_fingerprint: str | None,
    output_state_ref: str,
    output_state_fingerprint: str,
    state_schema: str,
    owner_effect_candidate: dict[str, Any] | None,
) -> dict[str, Any]:
    _require(commit_kind in {"INITIALIZE", "OWNER_EFFECT"}, "owner-state-commit-kind-invalid")
    candidate_ref = owner_effect_candidate.get("receipt_ref") if isinstance(owner_effect_candidate, dict) else None
    candidate_fingerprint = (
        owner_effect_candidate.get("receipt_fingerprint")
        if isinstance(owner_effect_candidate, dict)
        else None
    )
    receipt: dict[str, Any] = {
        "schema": OWNER_STATE_COMMIT_SCHEMA,
        "message_kind": "OWNER_STATE_COMMIT_RECEIPT",
        "backend": "POSTGRESQL",
        "commit_kind": commit_kind,
        "commit_protocol": "RETURN_ONLY_AFTER_DATABASE_COMMIT",
        "durability": "DATABASE_COMMIT_ACKNOWLEDGED_ON_RETURN",
        "tenant_ref": tenant_ref,
        "workspace_ref": workspace_ref,
        "principal_ref": principal_ref,
        "consumer_ref": consumer_ref,
        "session_ref": session_ref,
        "project_ref": project_ref,
        "owner": owner,
        "aggregate_ref": aggregate_ref,
        "event_id": event_id,
        "idempotency_key": idempotency_key,
        "request_fingerprint": request_fingerprint,
        "owner_revision_before": owner_revision_before,
        "owner_revision_after": owner_revision_after,
        "input_state_ref": input_state_ref,
        "input_state_fingerprint": input_state_fingerprint,
        "output_state_ref": output_state_ref,
        "output_state_fingerprint": output_state_fingerprint,
        "state_schema": state_schema,
        "owner_effect_candidate_ref": candidate_ref,
        "owner_effect_candidate_fingerprint": candidate_fingerprint,
        "commit_fingerprint": "",
        "commit_ref": "",
    }
    subject = copy.deepcopy(receipt)
    subject.pop("commit_fingerprint")
    subject.pop("commit_ref")
    receipt["commit_fingerprint"] = _sha256(subject)
    receipt["commit_ref"] = "OSC-" + receipt["commit_fingerprint"][:24].upper()
    validate_owner_state_commit_receipt(receipt)
    return receipt


def validate_owner_state_commit_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema", "message_kind", "backend", "commit_kind", "commit_protocol", "durability",
        "tenant_ref", "workspace_ref", "principal_ref", "consumer_ref", "session_ref",
        "project_ref", "owner", "aggregate_ref", "event_id", "idempotency_key",
        "request_fingerprint", "owner_revision_before", "owner_revision_after",
        "input_state_ref", "input_state_fingerprint", "output_state_ref",
        "output_state_fingerprint", "state_schema", "owner_effect_candidate_ref",
        "owner_effect_candidate_fingerprint", "commit_fingerprint", "commit_ref",
    }
    _require(isinstance(receipt, dict) and not required.difference(receipt), "owner-state-commit-fields-missing")
    _require(receipt.get("schema") == OWNER_STATE_COMMIT_SCHEMA, "owner-state-commit-schema-mismatch")
    _require(receipt.get("message_kind") == "OWNER_STATE_COMMIT_RECEIPT", "owner-state-commit-message-kind-mismatch")
    _require(receipt.get("backend") == "POSTGRESQL", "owner-state-commit-backend-mismatch")
    _require(receipt.get("commit_protocol") == "RETURN_ONLY_AFTER_DATABASE_COMMIT", "owner-state-commit-protocol-mismatch")
    _require(receipt.get("durability") == "DATABASE_COMMIT_ACKNOWLEDGED_ON_RETURN", "owner-state-commit-durability-mismatch")
    _require(receipt.get("owner") in OWNERS, "owner-state-commit-owner-invalid")
    for field in (
        "tenant_ref", "workspace_ref", "principal_ref", "consumer_ref", "session_ref",
        "project_ref", "aggregate_ref", "event_id", "idempotency_key", "output_state_ref",
        "state_schema",
    ):
        _require(isinstance(receipt.get(field), str) and bool(receipt[field].strip()), f"owner-state-commit-{field}-required")
    for field in ("request_fingerprint", "output_state_fingerprint", "commit_fingerprint"):
        value = receipt.get(field)
        _require(
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value),
            f"owner-state-commit-{field}-invalid",
        )
    before = receipt.get("owner_revision_before")
    after = receipt.get("owner_revision_after")
    _require(isinstance(before, int) and before >= 0 and after == before + 1, "owner-state-commit-revision-invalid")
    kind = receipt.get("commit_kind")
    candidate_ref = receipt.get("owner_effect_candidate_ref")
    candidate_fingerprint = receipt.get("owner_effect_candidate_fingerprint")
    if kind == "INITIALIZE":
        _require(before == 0, "owner-state-initialize-before-revision-must-be-zero")
        _require(receipt.get("input_state_ref") is None and receipt.get("input_state_fingerprint") is None, "owner-state-initialize-input-must-be-null")
        _require(candidate_ref is None and candidate_fingerprint is None, "owner-state-initialize-candidate-must-be-null")
    elif kind == "OWNER_EFFECT":
        _require(before >= 1, "owner-state-effect-before-revision-invalid")
        _require(isinstance(receipt.get("input_state_ref"), str) and bool(receipt["input_state_ref"]), "owner-state-effect-input-ref-required")
        input_fingerprint = receipt.get("input_state_fingerprint")
        _require(isinstance(input_fingerprint, str) and len(input_fingerprint) == 64, "owner-state-effect-input-fingerprint-required")
        _require(isinstance(candidate_ref, str) and candidate_ref.startswith("OER-"), "owner-state-effect-candidate-ref-required")
        _require(isinstance(candidate_fingerprint, str) and len(candidate_fingerprint) == 64, "owner-state-effect-candidate-fingerprint-required")
    else:
        raise OwnerStatePersistenceError("owner-state-commit-kind-invalid")
    subject = copy.deepcopy(receipt)
    subject.pop("commit_fingerprint", None)
    subject.pop("commit_ref", None)
    expected = _sha256(subject)
    _require(receipt.get("commit_fingerprint") == expected, "owner-state-commit-fingerprint-mismatch")
    _require(receipt.get("commit_ref") == "OSC-" + expected[:24].upper(), "owner-state-commit-ref-mismatch")
    return {
        "result": "PASS",
        "commit_ref": receipt["commit_ref"],
        "owner": receipt["owner"],
        "owner_revision_after": receipt["owner_revision_after"],
    }


def promote_owner_effect_receipt(
    *,
    candidate: dict[str, Any],
    commit: dict[str, Any],
) -> dict[str, Any]:
    validated = validate_owner_effect_receipt(
        candidate,
        expected_owner=commit.get("owner"),
        expected_effect=EFFECTS.get(commit.get("owner")),
    )
    _require(validated["current"] is False and candidate.get("result") == "CANDIDATE", "owner-state-precommit-candidate-required")
    validate_owner_state_commit_receipt(commit)
    _require(commit.get("commit_kind") == "OWNER_EFFECT", "owner-state-effect-commit-required")
    _require(commit.get("owner_effect_candidate_ref") == candidate.get("receipt_ref"), "owner-state-candidate-ref-mismatch")
    _require(commit.get("owner_effect_candidate_fingerprint") == candidate.get("receipt_fingerprint"), "owner-state-candidate-fingerprint-mismatch")
    for candidate_field, commit_field in (
        ("input_state_ref", "input_state_ref"),
        ("input_state_fingerprint", "input_state_fingerprint"),
        ("output_state_ref", "output_state_ref"),
        ("output_state_fingerprint", "output_state_fingerprint"),
    ):
        _require(candidate.get(candidate_field) == commit.get(commit_field), f"owner-state-{candidate_field}-mismatch")
    _require(candidate.get("state_mutated") is True, "owner-state-effect-must-mutate")
    return build_owner_effect_receipt(
        owner=candidate["owner"],
        control_decision_ref=candidate["control_decision_ref"],
        consolidation_result_ref=candidate["consolidation_result_ref"],
        effect=candidate["effect"],
        input_state_ref=candidate["input_state_ref"],
        input_state_fingerprint=candidate["input_state_fingerprint"],
        output_state_ref=candidate["output_state_ref"],
        output_state_fingerprint=candidate["output_state_fingerprint"],
        affected_refs=copy.deepcopy(candidate["affected_refs"]),
        evidence_refs=copy.deepcopy(candidate["evidence_refs"]),
        unaffected_state_preserved=candidate["unaffected_state_preserved"],
        state_mutated=True,
        persistence_evidence_ref=commit["commit_ref"],
    )


class PostgresOwnerStatePersistencePort:
    """Transactional persistence port; it never computes owner semantics."""

    def __init__(self, connection_factory: Callable[[], Any]):
        _require(callable(connection_factory), "owner-state-connection-factory-required")
        self._connection_factory = connection_factory

    @staticmethod
    def _require_scope(scopes: set[str], required: str) -> None:
        _require(required in scopes, f"required-scope-missing:{required}", StateAuthorizationError)

    @contextmanager
    def _transaction(
        self,
        *,
        tenant_ref: str,
        workspace_ref: str,
        principal_ref: str,
    ) -> Iterator[Any]:
        connection = None
        cursor = None
        try:
            try:
                connection = self._connection_factory()
            except StatePortError:
                raise
            except Exception as exc:
                raise StateServiceUnavailable("owner-state-service-unavailable") from exc
            cursor = connection.cursor()
            cursor.execute("SELECT set_config('cerebro.tenant_ref', %s, true)", (tenant_ref,))
            cursor.execute("SELECT set_config('cerebro.workspace_ref', %s, true)", (workspace_ref,))
            cursor.execute("SELECT set_config('cerebro.principal_ref', %s, true)", (principal_ref,))
            cursor.execute("SET CONSTRAINTS ALL DEFERRED")
            yield cursor
            connection.commit()
        except StatePortError:
            if connection is not None:
                _safe_rollback(connection)
            raise
        except Exception as exc:
            if connection is not None:
                _safe_rollback(connection)
            raise _mapped_database_error(exc) from exc
        finally:
            if cursor is not None:
                _safe_close(cursor)
            if connection is not None:
                _safe_close(connection)

    @staticmethod
    def _validate_identity(
        *,
        tenant_ref: str,
        workspace_ref: str,
        principal_ref: str,
        consumer_ref: str,
        session_ref: str,
        project_ref: str,
        event_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        values = {
            "tenant-ref": tenant_ref,
            "workspace-ref": workspace_ref,
            "principal-ref": principal_ref,
            "consumer-ref": consumer_ref,
            "session-ref": session_ref,
            "project-ref": project_ref,
        }
        if event_id is not None:
            values["event-id"] = event_id
        if idempotency_key is not None:
            values["idempotency-key"] = idempotency_key
        for field, value in values.items():
            _require(isinstance(value, str) and bool(value.strip()), f"owner-state-{field}-required")

    @staticmethod
    def _require_project(cursor: Any, tenant_ref: str, workspace_ref: str, project_ref: str) -> None:
        cursor.execute(
            """
            SELECT project_ref
              FROM cerebro_project_instances
             WHERE tenant_ref = %s AND workspace_ref = %s AND project_ref = %s
             FOR SHARE
            """,
            (tenant_ref, workspace_ref, project_ref),
        )
        if _fetchone(cursor) is None:
            raise StateBindingError("project-instance-not-found")

    @staticmethod
    def _require_revision_alignment(owner: str, state: dict[str, Any], owner_revision: int) -> None:
        if owner == "project":
            _require(
                state.get("basis_revision") == owner_revision,
                "project-basis-revision-must-match-owner-revision",
            )

    def _load_head(
        self,
        cursor: Any,
        *,
        tenant_ref: str,
        workspace_ref: str,
        project_ref: str,
        owner: str,
        aggregate_ref: str,
        for_update: bool = False,
        required: bool = True,
    ) -> dict[str, Any] | None:
        lock = " FOR UPDATE" if for_update else ""
        cursor.execute(
            """
            SELECT current_state_ref, owner_revision, state_schema, state_payload,
                   state_fingerprint, last_event_ref
              FROM cerebro_owner_state_heads
             WHERE tenant_ref = %s AND workspace_ref = %s AND project_ref = %s
               AND owner = %s AND aggregate_ref = %s
            """ + lock,
            (tenant_ref, workspace_ref, project_ref, owner, aggregate_ref),
        )
        row = _fetchone(cursor)
        if row is None:
            if required:
                raise StateBindingError("owner-state-head-not-found")
            return None
        state = _json_value(row.get("state_payload"), field="owner-state-payload")
        if not isinstance(state, dict):
            raise OwnerStatePersistenceError("persisted-owner-state-object-required")
        projection = _state_projection(owner, state)
        for projected, stored in (
            (projection["aggregate_ref"], aggregate_ref),
            (projection["state_ref"], row.get("current_state_ref")),
            (projection["state_schema"], row.get("state_schema")),
            (projection["state_fingerprint"], row.get("state_fingerprint")),
        ):
            if projected != stored:
                raise OwnerStatePersistenceError("persisted-owner-state-projection-mismatch")
        revision = row.get("owner_revision")
        _require(isinstance(revision, int) and revision >= 1, "persisted-owner-revision-invalid")
        self._require_revision_alignment(owner, state, revision)
        return {
            "owner": owner,
            "aggregate_ref": aggregate_ref,
            "project_ref": project_ref,
            "owner_revision": revision,
            "state": state,
            "state_ref": projection["state_ref"],
            "state_fingerprint": projection["state_fingerprint"],
            "last_event_ref": row["last_event_ref"],
        }

    def _load_commit(
        self,
        cursor: Any,
        *,
        tenant_ref: str,
        workspace_ref: str,
        principal_ref: str,
        consumer_ref: str,
        session_ref: str,
        idempotency_key: str | None = None,
        commit_ref: str | None = None,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        _require((idempotency_key is None) != (commit_ref is None), "owner-state-commit-lookup-key-invalid")
        predicate = "c.idempotency_key = %s" if idempotency_key is not None else "c.commit_ref = %s"
        lookup = idempotency_key if idempotency_key is not None else commit_ref
        lock = " FOR UPDATE OF c, r" if for_update else ""
        cursor.execute(
            """
            SELECT c.project_ref, c.owner, c.aggregate_ref, c.event_id,
                   c.request_fingerprint, c.commit_kind, c.commit_ref,
                   c.commit_payload, c.commit_fingerprint,
                   r.state_payload, r.output_state_fingerprint,
                   r.owner_effect_candidate_payload,
                   r.owner_effect_candidate_ref, r.owner_effect_candidate_fingerprint
              FROM cerebro_owner_state_commit_receipts AS c
              JOIN cerebro_owner_state_revisions AS r
                ON r.tenant_ref = c.tenant_ref
               AND r.workspace_ref = c.workspace_ref
               AND r.project_ref = c.project_ref
               AND r.owner = c.owner
               AND r.aggregate_ref = c.aggregate_ref
               AND r.owner_revision = c.owner_revision_after
             WHERE c.tenant_ref = %s AND c.workspace_ref = %s AND c.principal_ref = %s
               AND c.consumer_ref = %s AND c.session_ref = %s AND """ + predicate + lock,
            (tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref, lookup),
        )
        row = _fetchone(cursor)
        if row is None:
            return None
        commit = _json_value(row.get("commit_payload"), field="owner-state-commit-payload")
        state = _json_value(row.get("state_payload"), field="owner-state-revision-payload")
        candidate_raw = row.get("owner_effect_candidate_payload")
        candidate = None if candidate_raw is None else _json_value(candidate_raw, field="owner-effect-candidate-payload")
        if not isinstance(commit, dict) or not isinstance(state, dict):
            raise OwnerStatePersistenceError("persisted-owner-state-commit-bundle-invalid")
        validate_owner_state_commit_receipt(commit)
        projection = _state_projection(commit["owner"], state)
        for payload_value, stored_value in (
            (commit["commit_ref"], row.get("commit_ref")),
            (commit["commit_fingerprint"], row.get("commit_fingerprint")),
            (commit["output_state_fingerprint"], row.get("output_state_fingerprint")),
            (projection["state_fingerprint"], row.get("output_state_fingerprint")),
        ):
            if payload_value != stored_value:
                raise OwnerStatePersistenceError("persisted-owner-state-commit-projection-mismatch")
        if commit["commit_kind"] == "OWNER_EFFECT":
            if not isinstance(candidate, dict):
                raise OwnerStatePersistenceError("persisted-owner-effect-candidate-required")
            validate_owner_effect_receipt(
                candidate,
                expected_owner=commit["owner"],
                expected_effect=EFFECTS[commit["owner"]],
            )
            if (
                candidate.get("receipt_ref") != row.get("owner_effect_candidate_ref")
                or candidate.get("receipt_fingerprint") != row.get("owner_effect_candidate_fingerprint")
                or candidate.get("receipt_ref") != commit.get("owner_effect_candidate_ref")
                or candidate.get("receipt_fingerprint") != commit.get("owner_effect_candidate_fingerprint")
            ):
                raise OwnerStatePersistenceError("persisted-owner-effect-candidate-projection-mismatch")
        elif candidate is not None:
            raise OwnerStatePersistenceError("owner-state-initialization-cannot-have-candidate")
        return {"commit": commit, "state": state, "candidate": candidate, "row": row}

    @staticmethod
    def _completion(bundle: dict[str, Any]) -> dict[str, Any]:
        commit = bundle["commit"]
        candidate = bundle.get("candidate")
        receipt = (
            promote_owner_effect_receipt(candidate=candidate, commit=commit)
            if isinstance(candidate, dict)
            else None
        )
        return {
            "schema": OWNER_STATE_COMPLETION_SCHEMA,
            "result": "PASS",
            "owner": commit["owner"],
            "aggregate_ref": commit["aggregate_ref"],
            "owner_revision": commit["owner_revision_after"],
            "state": copy.deepcopy(bundle["state"]),
            "commit": copy.deepcopy(commit),
            "receipt": copy.deepcopy(receipt),
            "repository_permission_required": False,
        }

    @staticmethod
    def _insert_revision(
        cursor: Any,
        *,
        commit: dict[str, Any],
        state: dict[str, Any],
        candidate: dict[str, Any] | None,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO cerebro_owner_state_revisions (
                tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref,
                project_ref, owner, aggregate_ref, owner_revision, event_id,
                idempotency_key, input_state_ref, input_state_fingerprint,
                output_state_ref, output_state_fingerprint,
                owner_effect_candidate_ref, owner_effect_candidate_fingerprint,
                owner_effect_candidate_payload, control_decision_ref,
                consolidation_result_ref, state_schema, state_payload
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s::jsonb
            )
            """,
            (
                commit["tenant_ref"], commit["workspace_ref"], commit["principal_ref"],
                commit["consumer_ref"], commit["session_ref"], commit["project_ref"],
                commit["owner"], commit["aggregate_ref"], commit["owner_revision_after"],
                commit["event_id"], commit["idempotency_key"], commit["input_state_ref"],
                commit["input_state_fingerprint"], commit["output_state_ref"],
                commit["output_state_fingerprint"], commit["owner_effect_candidate_ref"],
                commit["owner_effect_candidate_fingerprint"],
                _canonical_text(candidate) if candidate is not None else None,
                candidate.get("control_decision_ref") if isinstance(candidate, dict) else None,
                candidate.get("consolidation_result_ref") if isinstance(candidate, dict) else None,
                commit["state_schema"], _canonical_text(state),
            ),
        )

    @staticmethod
    def _insert_commit(cursor: Any, commit: dict[str, Any]) -> None:
        cursor.execute(
            """
            INSERT INTO cerebro_owner_state_commit_receipts (
                tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref,
                project_ref, owner, aggregate_ref, event_id, idempotency_key,
                request_fingerprint, commit_kind, commit_ref, owner_revision_before,
                owner_revision_after, input_state_ref, input_state_fingerprint,
                output_state_ref, output_state_fingerprint,
                owner_effect_candidate_ref, owner_effect_candidate_fingerprint,
                commit_payload, commit_fingerprint
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s
            )
            """,
            (
                commit["tenant_ref"], commit["workspace_ref"], commit["principal_ref"],
                commit["consumer_ref"], commit["session_ref"], commit["project_ref"],
                commit["owner"], commit["aggregate_ref"], commit["event_id"],
                commit["idempotency_key"], commit["request_fingerprint"], commit["commit_kind"],
                commit["commit_ref"], commit["owner_revision_before"], commit["owner_revision_after"],
                commit["input_state_ref"], commit["input_state_fingerprint"],
                commit["output_state_ref"], commit["output_state_fingerprint"],
                commit["owner_effect_candidate_ref"], commit["owner_effect_candidate_fingerprint"],
                _canonical_text(commit), commit["commit_fingerprint"],
            ),
        )

    def initialize_owner_state(
        self,
        *,
        tenant_ref: str,
        workspace_ref: str,
        principal_ref: str,
        consumer_ref: str,
        session_ref: str,
        project_ref: str,
        owner: str,
        state: dict[str, Any],
        event_id: str,
        idempotency_key: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        self._require_scope(scopes, "project_state:transition")
        self._validate_identity(
            tenant_ref=tenant_ref,
            workspace_ref=workspace_ref,
            principal_ref=principal_ref,
            consumer_ref=consumer_ref,
            session_ref=session_ref,
            project_ref=project_ref,
            event_id=event_id,
            idempotency_key=idempotency_key,
        )
        projection = _state_projection(owner, state)
        if owner == "project":
            _require(projection["aggregate_ref"] == project_ref, "project-owner-aggregate-must-match-project")
        self._require_revision_alignment(owner, state, 1)
        request_subject = {
            "operation": "INITIALIZE_OWNER_STATE",
            "tenant_ref": tenant_ref,
            "workspace_ref": workspace_ref,
            "principal_ref": principal_ref,
            "consumer_ref": consumer_ref,
            "session_ref": session_ref,
            "project_ref": project_ref,
            "owner": owner,
            "aggregate_ref": projection["aggregate_ref"],
            "event_id": event_id,
            "idempotency_key": idempotency_key,
            "state": state,
        }
        request_fingerprint = _sha256(request_subject)
        result: dict[str, Any] | None = None
        with self._transaction(
            tenant_ref=tenant_ref,
            workspace_ref=workspace_ref,
            principal_ref=principal_ref,
        ) as cursor:
            existing = self._load_commit(
                cursor,
                tenant_ref=tenant_ref,
                workspace_ref=workspace_ref,
                principal_ref=principal_ref,
                consumer_ref=consumer_ref,
                session_ref=session_ref,
                idempotency_key=idempotency_key,
                for_update=True,
            )
            if existing is not None:
                if existing["commit"].get("request_fingerprint") != request_fingerprint:
                    raise StateConflict("owner-state-idempotency-key-reused-with-different-request")
                result = self._completion(existing)
            else:
                self._require_project(cursor, tenant_ref, workspace_ref, project_ref)
                head = self._load_head(
                    cursor,
                    tenant_ref=tenant_ref,
                    workspace_ref=workspace_ref,
                    project_ref=project_ref,
                    owner=owner,
                    aggregate_ref=projection["aggregate_ref"],
                    for_update=True,
                    required=False,
                )
                if head is not None:
                    raise StateConflict("owner-state-head-already-initialized")
                commit = build_owner_state_commit_receipt(
                    commit_kind="INITIALIZE",
                    tenant_ref=tenant_ref,
                    workspace_ref=workspace_ref,
                    principal_ref=principal_ref,
                    consumer_ref=consumer_ref,
                    session_ref=session_ref,
                    project_ref=project_ref,
                    owner=owner,
                    aggregate_ref=projection["aggregate_ref"],
                    event_id=event_id,
                    idempotency_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                    owner_revision_before=0,
                    owner_revision_after=1,
                    input_state_ref=None,
                    input_state_fingerprint=None,
                    output_state_ref=projection["state_ref"],
                    output_state_fingerprint=projection["state_fingerprint"],
                    state_schema=projection["state_schema"],
                    owner_effect_candidate=None,
                )
                cursor.execute(
                    """
                    INSERT INTO cerebro_owner_state_heads (
                        tenant_ref, workspace_ref, project_ref, owner, aggregate_ref,
                        current_state_ref, owner_revision, state_schema, state_payload,
                        state_fingerprint, last_event_ref
                    ) VALUES (%s, %s, %s, %s, %s, %s, 1, %s, %s::jsonb, %s, %s)
                    """,
                    (
                        tenant_ref, workspace_ref, project_ref, owner, projection["aggregate_ref"],
                        projection["state_ref"], projection["state_schema"], _canonical_text(state),
                        projection["state_fingerprint"], event_id,
                    ),
                )
                self._insert_revision(cursor, commit=commit, state=state, candidate=None)
                self._insert_commit(cursor, commit)
                result = self._completion({"commit": commit, "state": state, "candidate": None})
        _require(result is not None, "owner-state-initialize-result-missing")
        return result

    def commit_owner_effect(
        self,
        *,
        tenant_ref: str,
        workspace_ref: str,
        principal_ref: str,
        consumer_ref: str,
        session_ref: str,
        project_ref: str,
        owner: str,
        expected_owner_revision: int,
        candidate: dict[str, Any],
        output_state: dict[str, Any],
        event_id: str,
        idempotency_key: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        self._require_scope(scopes, "project_state:transition")
        self._validate_identity(
            tenant_ref=tenant_ref,
            workspace_ref=workspace_ref,
            principal_ref=principal_ref,
            consumer_ref=consumer_ref,
            session_ref=session_ref,
            project_ref=project_ref,
            event_id=event_id,
            idempotency_key=idempotency_key,
        )
        _require(owner in OWNERS, "owner-state-owner-invalid")
        _require(isinstance(expected_owner_revision, int) and expected_owner_revision >= 1, "expected-owner-revision-invalid")
        try:
            validated_candidate = validate_owner_effect_receipt(
                candidate,
                expected_owner=owner,
                expected_effect=EFFECTS[owner],
            )
        except Exception as exc:
            raise OwnerStatePersistenceError("owner-effect-candidate-validation-failed") from exc
        _require(
            validated_candidate["current"] is False and candidate.get("result") == "CANDIDATE",
            "owner-state-precommit-candidate-required",
        )
        projection = _state_projection(owner, output_state)
        if owner == "project":
            _require(projection["aggregate_ref"] == project_ref, "project-owner-aggregate-must-match-project")
        _require(candidate.get("output_state_ref") == projection["state_ref"], "owner-state-candidate-output-ref-mismatch")
        _require(candidate.get("output_state_fingerprint") == projection["state_fingerprint"], "owner-state-candidate-output-fingerprint-mismatch")
        _require(candidate.get("input_state_fingerprint") != projection["state_fingerprint"], "owner-state-effect-output-must-change")
        self._require_revision_alignment(owner, output_state, expected_owner_revision + 1)
        request_subject = {
            "operation": "COMMIT_OWNER_EFFECT",
            "tenant_ref": tenant_ref,
            "workspace_ref": workspace_ref,
            "principal_ref": principal_ref,
            "consumer_ref": consumer_ref,
            "session_ref": session_ref,
            "project_ref": project_ref,
            "owner": owner,
            "aggregate_ref": projection["aggregate_ref"],
            "event_id": event_id,
            "idempotency_key": idempotency_key,
            "expected_owner_revision": expected_owner_revision,
            "candidate": candidate,
            "output_state": output_state,
        }
        request_fingerprint = _sha256(request_subject)
        result: dict[str, Any] | None = None
        with self._transaction(
            tenant_ref=tenant_ref,
            workspace_ref=workspace_ref,
            principal_ref=principal_ref,
        ) as cursor:
            existing = self._load_commit(
                cursor,
                tenant_ref=tenant_ref,
                workspace_ref=workspace_ref,
                principal_ref=principal_ref,
                consumer_ref=consumer_ref,
                session_ref=session_ref,
                idempotency_key=idempotency_key,
                for_update=True,
            )
            if existing is not None:
                if existing["commit"].get("request_fingerprint") != request_fingerprint:
                    raise StateConflict("owner-state-idempotency-key-reused-with-different-request")
                result = self._completion(existing)
            else:
                self._require_project(cursor, tenant_ref, workspace_ref, project_ref)
                head = self._load_head(
                    cursor,
                    tenant_ref=tenant_ref,
                    workspace_ref=workspace_ref,
                    project_ref=project_ref,
                    owner=owner,
                    aggregate_ref=projection["aggregate_ref"],
                    for_update=True,
                )
                _require(head is not None, "owner-state-head-not-found", StateBindingError)
                if (
                    head["owner_revision"] != expected_owner_revision
                    or head["state_ref"] != candidate.get("input_state_ref")
                    or head["state_fingerprint"] != candidate.get("input_state_fingerprint")
                ):
                    raise StateConflict("owner-state-conflict-reload-and-reresolve")
                commit = build_owner_state_commit_receipt(
                    commit_kind="OWNER_EFFECT",
                    tenant_ref=tenant_ref,
                    workspace_ref=workspace_ref,
                    principal_ref=principal_ref,
                    consumer_ref=consumer_ref,
                    session_ref=session_ref,
                    project_ref=project_ref,
                    owner=owner,
                    aggregate_ref=projection["aggregate_ref"],
                    event_id=event_id,
                    idempotency_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                    owner_revision_before=expected_owner_revision,
                    owner_revision_after=expected_owner_revision + 1,
                    input_state_ref=head["state_ref"],
                    input_state_fingerprint=head["state_fingerprint"],
                    output_state_ref=projection["state_ref"],
                    output_state_fingerprint=projection["state_fingerprint"],
                    state_schema=projection["state_schema"],
                    owner_effect_candidate=candidate,
                )
                cursor.execute(
                    """
                    UPDATE cerebro_owner_state_heads
                       SET current_state_ref = %s, owner_revision = %s, state_schema = %s,
                           state_payload = %s::jsonb, state_fingerprint = %s,
                           last_event_ref = %s, updated_at = now()
                     WHERE tenant_ref = %s AND workspace_ref = %s AND project_ref = %s
                       AND owner = %s AND aggregate_ref = %s
                       AND owner_revision = %s AND current_state_ref = %s
                       AND state_fingerprint = %s
                    """,
                    (
                        projection["state_ref"], expected_owner_revision + 1,
                        projection["state_schema"], _canonical_text(output_state),
                        projection["state_fingerprint"], event_id, tenant_ref, workspace_ref,
                        project_ref, owner, projection["aggregate_ref"], expected_owner_revision,
                        head["state_ref"], head["state_fingerprint"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise StateConflict("owner-state-conflict-reload-and-reresolve")
                self._insert_revision(cursor, commit=commit, state=output_state, candidate=candidate)
                self._insert_commit(cursor, commit)
                result = self._completion(
                    {"commit": commit, "state": output_state, "candidate": candidate}
                )
        _require(result is not None, "owner-state-commit-result-missing")
        return result

    def read_owner_state(
        self,
        *,
        tenant_ref: str,
        workspace_ref: str,
        principal_ref: str,
        project_ref: str,
        owner: str,
        aggregate_ref: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        self._require_scope(scopes, "project_state:read")
        _require(owner in OWNERS, "owner-state-owner-invalid")
        with self._transaction(
            tenant_ref=tenant_ref,
            workspace_ref=workspace_ref,
            principal_ref=principal_ref,
        ) as cursor:
            head = self._load_head(
                cursor,
                tenant_ref=tenant_ref,
                workspace_ref=workspace_ref,
                project_ref=project_ref,
                owner=owner,
                aggregate_ref=aggregate_ref,
            )
        _require(head is not None, "owner-state-head-not-found", StateBindingError)
        return head

    def read_owner_commit_evidence(
        self,
        *,
        tenant_ref: str,
        workspace_ref: str,
        principal_ref: str,
        consumer_ref: str,
        session_ref: str,
        commit_ref: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        self._require_scope(scopes, "project_state:read")
        with self._transaction(
            tenant_ref=tenant_ref,
            workspace_ref=workspace_ref,
            principal_ref=principal_ref,
        ) as cursor:
            bundle = self._load_commit(
                cursor,
                tenant_ref=tenant_ref,
                workspace_ref=workspace_ref,
                principal_ref=principal_ref,
                consumer_ref=consumer_ref,
                session_ref=session_ref,
                commit_ref=commit_ref,
                for_update=True,
            )
            if bundle is None:
                raise StateBindingError("owner-state-commit-evidence-not-found")
            commit = bundle["commit"]
            _require(commit["commit_kind"] == "OWNER_EFFECT", "owner-state-effect-evidence-required")
            head = self._load_head(
                cursor,
                tenant_ref=tenant_ref,
                workspace_ref=workspace_ref,
                project_ref=commit["project_ref"],
                owner=commit["owner"],
                aggregate_ref=commit["aggregate_ref"],
                for_update=True,
            )
            _require(head is not None, "owner-state-current-head-not-found", StateBindingError)
            completion = self._completion(bundle)
            evidence = {
                "schema": "cerebro-owner-state-commit-evidence-bundle/v1",
                "commit": copy.deepcopy(commit),
                "candidate": copy.deepcopy(bundle["candidate"]),
                "state": copy.deepcopy(bundle["state"]),
                "completion": completion,
                "current_head": copy.deepcopy(head),
            }
        return evidence


class PostgresOwnerStatePersistenceVerifier:
    """Trusted verifier bound to one authenticated owner-sequence session."""

    def __init__(
        self,
        *,
        persistence_port: Any,
        tenant_ref: str,
        workspace_ref: str,
        principal_ref: str,
        consumer_ref: str,
        session_ref: str,
        scopes: set[str],
    ):
        _require(
            callable(getattr(persistence_port, "read_owner_commit_evidence", None)),
            "owner-state-evidence-reader-required",
        )
        PostgresOwnerStatePersistencePort._validate_identity(
            tenant_ref=tenant_ref,
            workspace_ref=workspace_ref,
            principal_ref=principal_ref,
            consumer_ref=consumer_ref,
            session_ref=session_ref,
            project_ref="BOUND_AT_COMMIT_EVIDENCE",
        )
        _require("project_state:read" in scopes, "owner-state-verifier-read-scope-required")
        self._port = persistence_port
        self._identity = {
            "tenant_ref": tenant_ref,
            "workspace_ref": workspace_ref,
            "principal_ref": principal_ref,
            "consumer_ref": consumer_ref,
            "session_ref": session_ref,
        }
        self._scopes = set(scopes)

    def verify(self, *, receipt: dict[str, Any]) -> dict[str, Any]:
        validated = validate_owner_effect_receipt(receipt)
        owner = validated["owner"]
        _require(owner in OWNERS, "owner-state-verifier-owner-invalid")
        _require(validated["current"] is True and receipt.get("result") == "PASS", "owner-state-current-PASS-receipt-required")
        commit_ref = receipt.get("persistence_evidence_ref")
        _require(isinstance(commit_ref, str) and bool(commit_ref), "owner-state-persistence-evidence-ref-required")
        evidence = self._port.read_owner_commit_evidence(
            **self._identity,
            commit_ref=commit_ref,
            scopes=set(self._scopes),
        )
        _require(isinstance(evidence, dict), "owner-state-evidence-bundle-required")
        commit = evidence.get("commit")
        candidate = evidence.get("candidate")
        head = evidence.get("current_head")
        _require(all(isinstance(value, dict) for value in (commit, candidate, head)), "owner-state-evidence-bundle-invalid")
        expected = promote_owner_effect_receipt(candidate=candidate, commit=commit)
        _require(receipt == expected, "owner-state-receipt-exact-match-required")
        _require(head.get("owner") == owner, "owner-state-current-owner-mismatch")
        _require(head.get("state_ref") == receipt.get("output_state_ref"), "owner-state-current-output-ref-mismatch")
        _require(head.get("state_fingerprint") == receipt.get("output_state_fingerprint"), "owner-state-output-no-longer-current")
        return {
            "schema": OWNER_VERIFICATION_SCHEMA,
            "result": "PASS",
            "verifier_ref": "CEREBRO-OWNER-POSTGRES-PERSISTENCE-VERIFIER-V1",
            "owner": owner,
            "owner_effect_receipt_ref": receipt["receipt_ref"],
            "owner_effect_receipt_fingerprint": receipt["receipt_fingerprint"],
            "persistence_evidence_ref": commit_ref,
            "output_state_ref": receipt["output_state_ref"],
            "output_state_fingerprint": receipt["output_state_fingerprint"],
        }
