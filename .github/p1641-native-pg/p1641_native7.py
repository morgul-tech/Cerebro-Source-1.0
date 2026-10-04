"""Route-only runner for the frozen P1625 DisposablePgOracle seven cases."""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
import unittest

import psycopg

EXPECTED = [
    ("revoke-first-reservation", "test_revoke_first_blocks_parent_reservation",
     "PARENT_AUTHORITY_CHANGED_BEFORE_COMMIT"),
    ("supersede-first-reservation", "test_supersede_first_blocks_parent_reservation",
     "PARENT_AUTHORITY_CHANGED_BEFORE_COMMIT"),
    ("expire-first-reservation", "test_expire_first_blocks_parent_reservation",
     "PARENT_AUTHORITY_CHANGED_BEFORE_COMMIT"),
    ("future-finite-decision-typed-unsupported",
     "test_future_finite_decision_is_typed_unsupported",
     "PARENT_TIMED_EXPIRY_UNSUPPORTED"),
    ("wrong-revision-task-basis", "test_wrong_revision_and_task_basis_rejected",
     "PARENT_AUTHORITY_CHANGED_BEFORE_COMMIT"),
    ("commit-first-revoke-serialization",
     "test_commit_first_reservation_serializes_revoke",
     "REVOKE_WAITS_FOR_COMMIT_FENCE"),
    ("issuer-worker-role-separation",
     "test_roles_cannot_cross_issuer_and_worker_boundary",
     "INSUFFICIENT_PRIVILEGE_BOTH_DIRECTIONS"),
]

ROLE_SPECS = {
    "worker": ("P1625_WORKER_DSN", "p1641_worker", "cerebro_nal_owner"),
    "rights": ("P1625_RIGHTS_DSN", "p1641_rights", "cerebro_human_rights_issuer"),
    "admin": ("P1625_ADMIN_DSN", "p1641_admin", "cerebro_pm_admin_issuer"),
    "decision": ("P1625_DECISION_DSN", "p1641_decision", "cerebro_pm_decision_issuer"),
}


def role_witness():
    witness = {}
    for kind, (env_name, session_expected, current_expected) in ROLE_SPECS.items():
        dsn = os.environ.get(env_name)
        if not dsn:
            raise RuntimeError(f"missing process-local DSN: {env_name}")
        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT session_user, current_user")
            session_user, current_user = cur.fetchone()
            cur.execute(
                "SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, rolcanlogin "
                "FROM pg_roles WHERE rolname = %s",
                (session_user,),
            )
            attrs = cur.fetchone()
        if session_user != session_expected or current_user != current_expected:
            raise RuntimeError(
                f"role identity mismatch {kind}: session={session_user} current={current_user}"
            )
        if attrs != (False, False, False, False, True):
            raise RuntimeError(f"role boundary mismatch {kind}: {attrs}")
        witness[kind] = {
            "session_user": session_user,
            "current_user": current_user,
            "rolsuper": False,
            "rolbypassrls": False,
            "rolcreatedb": False,
            "rolcreaterole": False,
            "rolcanlogin": True,
        }
    return witness


def load_frozen_test(candidate_root: pathlib.Path):
    nal_dir = candidate_root / "candidates" / "natural-actor-lifecycle-v0.1"
    sys.path[:0] = [
        str(nal_dir),
        str(candidate_root / "mcp"),
        str(candidate_root / "tooling" / "validator"),
    ]
    test_path = nal_dir / "test_parent_authority_pg.py"
    spec = importlib.util.spec_from_file_location("p1625_frozen_pg_oracle", test_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load frozen P1625 oracle")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    candidate_root = pathlib.Path(os.environ["P1641_CANDIDATE_ROOT"]).resolve()
    result_path = pathlib.Path(os.environ["P1641_RESULT_PATH"]).resolve()
    roles = role_witness()
    module = load_frozen_test(candidate_root)

    actual_methods = {
        name for name in dir(module.DisposablePgOracle) if name.startswith("test_")
    }
    expected_methods = {method for _, method, _ in EXPECTED}
    if actual_methods != expected_methods:
        raise RuntimeError(
            f"frozen oracle method set drift: expected={sorted(expected_methods)} "
            f"actual={sorted(actual_methods)}"
        )

    cases = []
    all_pass = True
    for case_name, method, causal in EXPECTED:
        suite = unittest.TestSuite([module.DisposablePgOracle(method)])
        result = unittest.TestResult()
        suite.run(result)
        status = "PASS"
        detail = causal
        if result.skipped:
            status = "SKIP"
            detail = result.skipped[0][1]
        elif result.failures:
            status = "FAIL"
            detail = result.failures[0][1][-2000:]
        elif result.errors:
            status = "ERROR"
            detail = result.errors[0][1][-2000:]
        if status != "PASS":
            all_pass = False
        cases.append(
            {
                "case": case_name,
                "test_method": method,
                "status": status,
                "causal_verdict": causal if status == "PASS" else detail,
            }
        )

    receipt = {
        "schema": "cerebro-p1641-native7-result/v1",
        "candidate_head": "09f7351d3c28b6fe50ddfccad4119413145b7820",
        "candidate_tree": "0d848aab9e3a82b9ad7f0e5e2c94102d0f477062",
        "candidate_parent": "3ac1fe9d4e24a86bce311f6a34893bb4ffe3ce15",
        "role_witness": roles,
        "cases": cases,
        "all7_pass": all_pass and len(cases) == 7,
    }
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"all7_pass": receipt["all7_pass"], "cases": cases}, sort_keys=True))
    return 0 if receipt["all7_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())