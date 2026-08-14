#!/usr/bin/env python3
"""DEEP scripted-contract validation for non-Context owner persistence."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Callable


SOURCE_ROOT = Path(__file__).resolve().parents[2]
for path in (
    SOURCE_ROOT / "mcp",
    SOURCE_ROOT / "tooling" / "context",
    SOURCE_ROOT / "tooling" / "owner_state",
    SOURCE_ROOT / "tooling" / "validator",
    SOURCE_ROOT / "engines" / "project",
    SOURCE_ROOT / "engines" / "quality",
    SOURCE_ROOT / "engines" / "convergence",
):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from control_context_postgres_validation import ScriptedConnection, ScriptedDatabaseError  # noqa: E402
from control_context_state_port import StateConflict, StateServiceUnavailable  # noqa: E402
from convergence_owner_effect import create_convergence_state, consume_convergence_revalidation_effect  # noqa: E402
from owner_state_persistence import (  # noqa: E402
    PostgresOwnerStatePersistencePort,
    PostgresOwnerStatePersistenceVerifier,
    validate_owner_state_commit_receipt,
)
from project_owner_effect import create_project_basis, consume_project_revision_effect  # noqa: E402
from quality_owner_effect import consume_quality_invalidation_effect, quality_trace_fingerprint  # noqa: E402
from quality_trace import new as new_quality_trace, pass_stage  # noqa: E402


def _expect_error(function: Callable[[], Any], expected: type[BaseException]) -> bool:
    try:
        function()
    except expected:
        return True
    return False


def _head_row(owner: str, state: dict[str, Any], *, revision: int = 1) -> dict[str, Any]:
    if owner == "project":
        state_ref = state["basis_ref"]
        fingerprint = state["basis_fingerprint"]
    elif owner == "quality":
        state_ref = state["work_item_ref"]
        fingerprint = quality_trace_fingerprint(state)
    else:
        state_ref = state["state_ref"]
        fingerprint = state["state_fingerprint"]
    return {
        "current_state_ref": state_ref,
        "owner_revision": revision,
        "state_schema": state["schema"],
        "state_payload": copy.deepcopy(state),
        "state_fingerprint": fingerprint,
        "last_event_ref": f"INIT-{owner}",
    }


def _commit_steps(owner: str, current: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"contains": "set_config('cerebro.tenant_ref'"},
        {"contains": "set_config('cerebro.workspace_ref'"},
        {"contains": "set_config('cerebro.principal_ref'"},
        {"contains": "SET CONSTRAINTS ALL DEFERRED"},
        {"contains": "FROM cerebro_owner_state_commit_receipts AS c", "rows": []},
        {"contains": "FROM cerebro_project_instances", "rows": [{"project_ref": "TOTAL_MCP_REVISION"}]},
        {"contains": "FROM cerebro_owner_state_heads", "rows": [_head_row(owner, current)]},
        {"contains": "UPDATE cerebro_owner_state_heads", "rowcount": 1},
        {"contains": "INSERT INTO cerebro_owner_state_revisions", "rowcount": 1},
        {"contains": "INSERT INTO cerebro_owner_state_commit_receipts", "rowcount": 1},
    ]


def _init_steps() -> list[dict[str, Any]]:
    return [
        {"contains": "set_config('cerebro.tenant_ref'"},
        {"contains": "set_config('cerebro.workspace_ref'"},
        {"contains": "set_config('cerebro.principal_ref'"},
        {"contains": "SET CONSTRAINTS ALL DEFERRED"},
        {"contains": "FROM cerebro_owner_state_commit_receipts AS c", "rows": []},
        {"contains": "FROM cerebro_project_instances", "rows": [{"project_ref": "TOTAL_MCP_REVISION"}]},
        {"contains": "FROM cerebro_owner_state_heads", "rows": []},
        {"contains": "INSERT INTO cerebro_owner_state_heads", "rowcount": 1},
        {"contains": "INSERT INTO cerebro_owner_state_revisions", "rowcount": 1},
        {"contains": "INSERT INTO cerebro_owner_state_commit_receipts", "rowcount": 1},
    ]


def _identity(owner: str) -> dict[str, Any]:
    return {
        "tenant_ref": "TENANT-1",
        "workspace_ref": "WORKSPACE-1",
        "principal_ref": "PRINCIPAL-1",
        "consumer_ref": "CONSUMER-1",
        "session_ref": "SESSION-1",
        "project_ref": "TOTAL_MCP_REVISION",
        "event_id": f"EVENT-{owner.upper()}-1",
        "idempotency_key": f"IDEMPOTENCY-{owner.upper()}-1",
        "scopes": {"project_state:read", "project_state:transition"},
    }


class EvidenceReader:
    def __init__(self, bundles: dict[str, dict[str, Any]]):
        self.bundles = copy.deepcopy(bundles)
        self.calls: list[dict[str, Any]] = []

    def read_owner_commit_evidence(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(copy.deepcopy(kwargs))
        return copy.deepcopy(self.bundles[kwargs["commit_ref"]])


def _evidence_bundle(completion: dict[str, Any]) -> dict[str, Any]:
    receipt = completion["receipt"]
    commit = completion["commit"]
    return {
        "schema": "cerebro-owner-state-commit-evidence-bundle/v1",
        "commit": copy.deepcopy(commit),
        "candidate": {
            **copy.deepcopy(receipt),
            "result": "CANDIDATE",
            "current": False,
            "persistence_evidence_ref": None,
            "receipt_fingerprint": commit["owner_effect_candidate_fingerprint"],
            "receipt_ref": commit["owner_effect_candidate_ref"],
        },
        "state": copy.deepcopy(completion["state"]),
        "completion": copy.deepcopy(completion),
        "current_head": {
            "owner": completion["owner"],
            "aggregate_ref": completion["aggregate_ref"],
            "project_ref": commit["project_ref"],
            "owner_revision": completion["owner_revision"],
            "state": copy.deepcopy(completion["state"]),
            "state_ref": receipt["output_state_ref"],
            "state_fingerprint": receipt["output_state_fingerprint"],
            "last_event_ref": commit["event_id"],
        },
    }


def selftest() -> dict[str, Any]:
    tests: list[dict[str, str]] = []

    def check(name: str, condition: bool) -> None:
        tests.append({"name": name, "result": "PASS" if condition else "FAIL"})

    consolidation_ref = "CCR-000000000000000000000001"
    control_decision_ref = "MCPD-OWNER-SEQUENCE-1"
    current_project = create_project_basis(
        "TOTAL_MCP_REVISION",
        {"objective": "A", "constraints": ["NO_SOURCE_STATE"]},
    )
    initialization_connection = ScriptedConnection(_init_steps())
    initialization_identity = _identity("project")
    initialization_identity["event_id"] = "EVENT-PROJECT-INIT"
    initialization_identity["idempotency_key"] = "IDEMPOTENCY-PROJECT-INIT"
    initialization = PostgresOwnerStatePersistencePort(
        lambda: initialization_connection
    ).initialize_owner_state(
        **initialization_identity,
        owner="project",
        state=current_project,
    )
    check(
        "owner-state-initialization-is-durable-but-not-an-effect-receipt",
        initialization["commit"]["commit_kind"] == "INITIALIZE"
        and initialization["receipt"] is None
        and initialization_connection.commit_called,
    )

    project_output, project_candidate = consume_project_revision_effect(
        owner_effect={
            "owner": "project", "effect": "REVISION_REQUIRED",
            "candidate_ref": consolidation_ref, "state_mutation_by_MCP": False,
        },
        control_decision_ref=control_decision_ref,
        consolidation_result_ref=consolidation_ref,
        current_basis=current_project,
        revised_payload={"objective": "A", "constraints": ["NO_SOURCE_STATE", "COMMIT_GATED_HNS"]},
        affected_refs=["CTX-ROOT"],
        evidence_refs=[consolidation_ref],
    )
    project_connection = ScriptedConnection(_commit_steps("project", current_project))
    project_completion = PostgresOwnerStatePersistencePort(lambda: project_connection).commit_owner_effect(
        **_identity("project"),
        owner="project",
        expected_owner_revision=1,
        candidate=project_candidate,
        output_state=project_output,
    )

    old_basis = hashlib.sha256(b"quality-old").hexdigest()
    current_quality = new_quality_trace("QUALITY-TOTAL-MCP", "DEEP", old_basis)
    pass_stage(current_quality, "REFINE", old_basis, ["E-REFINE"])
    pass_stage(current_quality, "CRITIQUE", old_basis, ["E-CRITIQUE"])
    quality_output, quality_candidate = consume_quality_invalidation_effect(
        owner_effect={
            "owner": "quality", "effect": "INVALIDATE_AFFECTED",
            "candidate_ref": consolidation_ref, "state_mutation_by_MCP": False,
        },
        control_decision_ref=control_decision_ref,
        consolidation_result_ref=consolidation_ref,
        current_trace=current_quality,
        new_basis_fingerprint=project_output["basis_fingerprint"],
        affected_stage_refs=["REFINE"],
        evidence_refs=[project_completion["receipt"]["receipt_ref"]],
    )
    quality_connection = ScriptedConnection(_commit_steps("quality", current_quality))
    quality_completion = PostgresOwnerStatePersistencePort(lambda: quality_connection).commit_owner_effect(
        **_identity("quality"),
        owner="quality",
        expected_owner_revision=1,
        candidate=quality_candidate,
        output_state=quality_output,
    )

    current_convergence = create_convergence_state(
        "CONV-TOTAL-MCP",
        old_basis,
        [
            {"family_id": "F-A", "state": "PASS", "depends_on": [], "pass_basis_fingerprint": old_basis, "invalidated_by": []},
            {"family_id": "F-B", "state": "PASS", "depends_on": ["F-A"], "pass_basis_fingerprint": old_basis, "invalidated_by": []},
            {"family_id": "F-C", "state": "PASS", "depends_on": [], "pass_basis_fingerprint": old_basis, "invalidated_by": []},
        ],
    )
    convergence_output, convergence_candidate = consume_convergence_revalidation_effect(
        owner_effect={
            "owner": "convergence", "effect": "REVALIDATE_AFFECTED",
            "candidate_ref": consolidation_ref, "state_mutation_by_MCP": False,
        },
        control_decision_ref=control_decision_ref,
        consolidation_result_ref=consolidation_ref,
        current_state=current_convergence,
        new_basis_fingerprint=project_output["basis_fingerprint"],
        directly_affected_family_refs=["F-A"],
        evidence_refs=[quality_completion["receipt"]["receipt_ref"]],
    )
    convergence_connection = ScriptedConnection(_commit_steps("convergence", current_convergence))
    convergence_completion = PostgresOwnerStatePersistencePort(
        lambda: convergence_connection
    ).commit_owner_effect(
        **_identity("convergence"),
        owner="convergence",
        expected_owner_revision=1,
        candidate=convergence_candidate,
        output_state=convergence_output,
    )

    completions = [project_completion, quality_completion, convergence_completion]
    check(
        "all-three-owner-ports-return-current-PASS-only-after-commit",
        all(
            completion["receipt"]["result"] == "PASS"
            and completion["receipt"]["current"] is True
            and validate_owner_state_commit_receipt(completion["commit"])["result"] == "PASS"
            for completion in completions
        )
        and all(connection.commit_called for connection in (project_connection, quality_connection, convergence_connection)),
    )
    check(
        "ordered-owner-evidence-chain-is-explicit-project-quality-convergence",
        project_completion["receipt"]["receipt_ref"] in quality_candidate["evidence_refs"]
        and quality_completion["receipt"]["receipt_ref"] in convergence_candidate["evidence_refs"],
    )
    check(
        "owner-commits-are-separate-transactions-not-cross-owner-merge",
        len({id(project_connection), id(quality_connection), id(convergence_connection)}) == 3
        and all(not connection.cursor_instance.steps for connection in (project_connection, quality_connection, convergence_connection)),
    )

    # Reconstruct the original candidate directly from the semantic fixtures for
    # exact verifier evidence; a PASS receipt is never trusted as its own proof.
    candidate_by_owner = {
        "project": project_candidate,
        "quality": quality_candidate,
        "convergence": convergence_candidate,
    }
    bundles: dict[str, dict[str, Any]] = {}
    for completion in completions:
        bundle = _evidence_bundle(completion)
        bundle["candidate"] = copy.deepcopy(candidate_by_owner[completion["owner"]])
        bundles[completion["commit"]["commit_ref"]] = bundle
    reader = EvidenceReader(bundles)
    verifier = PostgresOwnerStatePersistenceVerifier(
        persistence_port=reader,
        tenant_ref="TENANT-1",
        workspace_ref="WORKSPACE-1",
        principal_ref="PRINCIPAL-1",
        consumer_ref="CONSUMER-1",
        session_ref="SESSION-1",
        scopes={"project_state:read"},
    )
    verifications = [verifier.verify(receipt=completion["receipt"]) for completion in completions]
    check(
        "trusted-verifier-binds-all-three-current-owner-receipts",
        [item["owner"] for item in verifications] == ["project", "quality", "convergence"]
        and all(item["result"] == "PASS" for item in verifications),
    )

    replay_row = {
        "project_ref": "TOTAL_MCP_REVISION",
        "owner": "project",
        "aggregate_ref": "TOTAL_MCP_REVISION",
        "event_id": project_completion["commit"]["event_id"],
        "request_fingerprint": project_completion["commit"]["request_fingerprint"],
        "commit_kind": "OWNER_EFFECT",
        "commit_ref": project_completion["commit"]["commit_ref"],
        "commit_payload": copy.deepcopy(project_completion["commit"]),
        "commit_fingerprint": project_completion["commit"]["commit_fingerprint"],
        "state_payload": copy.deepcopy(project_output),
        "output_state_fingerprint": project_output["basis_fingerprint"],
        "owner_effect_candidate_payload": copy.deepcopy(project_candidate),
        "owner_effect_candidate_ref": project_candidate["receipt_ref"],
        "owner_effect_candidate_fingerprint": project_candidate["receipt_fingerprint"],
    }
    replay_steps = [
        {"contains": "set_config('cerebro.tenant_ref'"},
        {"contains": "set_config('cerebro.workspace_ref'"},
        {"contains": "set_config('cerebro.principal_ref'"},
        {"contains": "SET CONSTRAINTS ALL DEFERRED"},
        {"contains": "FROM cerebro_owner_state_commit_receipts AS c", "rows": [replay_row]},
    ]
    replay_connection = ScriptedConnection(replay_steps)
    replay = PostgresOwnerStatePersistencePort(lambda: replay_connection).commit_owner_effect(
        **_identity("project"),
        owner="project",
        expected_owner_revision=1,
        candidate=project_candidate,
        output_state=project_output,
    )
    check(
        "exact-idempotency-replay-returns-original-owner-commit",
        replay == project_completion and replay_connection.commit_called,
    )

    conflict_steps = _commit_steps("project", current_project)
    conflict_steps[6]["rows"] = [_head_row("project", project_output, revision=2)]
    conflict_connection = ScriptedConnection(conflict_steps[:7])
    check(
        "owner-CAS-conflict-fails-closed-before-any-write",
        _expect_error(
            lambda: PostgresOwnerStatePersistencePort(lambda: conflict_connection).commit_owner_effect(
                **_identity("project"), owner="project", expected_owner_revision=1,
                candidate=project_candidate, output_state=project_output,
            ),
            StateConflict,
        )
        and conflict_connection.rollback_called,
    )
    failed_commit_connection = ScriptedConnection(
        _commit_steps("project", current_project),
        commit_error=ScriptedDatabaseError("08006"),
    )
    check(
        "owner-state-database-commit-failure-cannot-return-PASS",
        _expect_error(
            lambda: PostgresOwnerStatePersistencePort(lambda: failed_commit_connection).commit_owner_effect(
                **_identity("project"), owner="project", expected_owner_revision=1,
                candidate=project_candidate, output_state=project_output,
            ),
            StateServiceUnavailable,
        )
        and failed_commit_connection.rollback_called,
    )

    result = "PASS" if all(item["result"] == "PASS" for item in tests) else "FAIL"
    return {
        "schema": "cerebro-owner-state-persistence-contract-selftest/v1",
        "result": result,
        "evidence_class": "SCRIPTED_DBAPI_CONTRACT_NOT_LIVE_POSTGRESQL",
        "live_postgresql_executed": False,
        "test_count": len(tests),
        "failures": [item for item in tests if item["result"] != "PASS"],
        "tests": tests,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["selftest"])
    parser.parse_args()
    result = selftest()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
