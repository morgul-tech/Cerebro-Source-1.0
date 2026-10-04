"""P1625 focused local tests; PG oracle runs only with disposable DSNs/schema."""
from __future__ import annotations

import copy
import hashlib
import os
import pathlib
import sys
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import psycopg

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "mcp"), str(ROOT / "tooling" / "validator")]
from contextlib import nullcontext
from lifecycle import Hold, Result, digest
from postgres_store import PostgresStore
from parent_authority_pg import PgParentAuthority, PgParentAuthorityIssuer
from worker_episode_runner import BoundLocalFileReader, IssuedEpisode, WorkerEpisodeRunner
from nal_authority_revocation_validation import Episode, UnknownAfterCommitStore
from pm_authority_v02_validation import (
    MANDATE, PM_ACTOR, PM_GENERATION, WORKER_ACTOR, WORKER_GENERATION,
    SOURCE, METHOD, BoundPmAuthorityV02, WorkerContextAuthError,
    _canonical_sha256,
)


def local_episode(*, store=None):
    ep = Episode(store=store)
    rights = ep.pm_state.rights
    rights["operations"].append("LOCAL_READ_SHA256")
    rights["operation_scopes"]["LOCAL_READ_SHA256"] = ["TASK_EXECUTE"]
    rights["basis_sha256"] = _canonical_sha256(
        BoundPmAuthorityV02._rights_basis(rights))
    ep.pm_state.delegation["operations"].append("LOCAL_READ_SHA256")
    ep.worker_owners.operation = "LOCAL_READ_SHA256"
    ep.worker_owners.effect_target_ref = "LOCAL-MATERIAL"
    ep.snapshot = ep.pm.read_current_consumer_authority(
        mandate_ref=MANDATE, pm_actor_ref=PM_ACTOR,
        pm_generation_ref=PM_GENERATION,
        worker_generation_ref=WORKER_GENERATION,
        worker_actor_ref=WORKER_ACTOR, source_revision=SOURCE,
        method_fingerprint=METHOD, task_ref=ep.worker_owners.task_ref,
        expected_task_revision=ep.worker_owners.task_revision,
        expected_task_sha256=ep.worker_owners.task_sha256)
    ep.evidence.snapshot = ep.snapshot
    ep.seed_base()
    ep.seed_consume()
    ep.seed_start()
    task = ep.worker_owners
    issued = IssuedEpisode(
        MANDATE, PM_ACTOR, PM_GENERATION, WORKER_ACTOR, WORKER_GENERATION,
        SOURCE, METHOD, task.task_ref, task.task_revision, task.task_sha256,
        ep.binding_ref, ep.consume_ref, ep.start_ref)
    return ep, issued


class SyntheticTerminalOwner:
    def __init__(self, ep, expected_sha):
        self.ep = ep
        self.expected_sha = expected_sha
        self.calls = 0

    def record_local_digest(self, *, snapshot, binding_ref,
                            material_sha256, material_bytes):
        self.calls += 1
        assert snapshot == self.ep.snapshot
        assert binding_ref == self.ep.binding_ref
        assert material_sha256 == self.expected_sha
        assert material_bytes == 7
        ref = self.ep.seed_terminal()
        self.ep.evidence.rows[("WORKER_TERMINAL", ref)].update(
            material_sha256=material_sha256, material_bytes=material_bytes)
        return {"evidence_ref": ref, "terminal_ref": "TERMINAL-P1613",
                "material_sha256": material_sha256,
                "material_bytes": material_bytes}


class SyntheticAdmissionOwner:
    def __init__(self, ep):
        self.ep = ep
        self.calls = 0

    def admit_terminal(self, *, snapshot, terminal_ref, material_sha256):
        self.calls += 1
        assert snapshot == self.ep.snapshot
        assert terminal_ref == "TERMINAL-P1613"
        ref = self.ep.seed_admission()
        self.ep.evidence.rows[("PM_ADMISSION", ref)]["material_sha256"] = material_sha256
        return ref


def make_local_runner(ep, root):
    path = root / "input.bin"
    path.write_bytes(b"payload")
    terminal = SyntheticTerminalOwner(ep, hashlib.sha256(b"payload").hexdigest())
    admission = SyntheticAdmissionOwner(ep)
    runner = WorkerEpisodeRunner(
        ep.pm, ep.nal, BoundLocalFileReader(root, {"LOCAL-MATERIAL": path}),
        terminal, admission, enabled=True)
    return runner, terminal, admission


def fail_before_commit_once(store):
    def fail():
        store.before_commit = None
        raise Hold("PG_COMMIT_OUTCOME_UNKNOWN")
    store.before_commit = fail


class LocalCallsiteProof(unittest.TestCase):
    def test_default_off_has_no_action(self):
        ep, issued = local_episode()
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            path = root / "input.bin"
            path.write_bytes(b"payload")
            owner = SyntheticTerminalOwner(ep, hashlib.sha256(b"payload").hexdigest())
            runner = WorkerEpisodeRunner(
                ep.pm, ep.nal, BoundLocalFileReader(root, {"LOCAL-MATERIAL": path}),
                owner, SyntheticAdmissionOwner(ep))
            with self.assertRaises(Hold) as caught:
                runner.run(issued)
            self.assertEqual(caught.exception.code, "WORKER_EPISODE_DEFAULT_OFF")
            self.assertEqual(owner.calls, 0)

    def test_one_real_local_read_consumed_and_closed_once(self):
        ep, issued = local_episode()
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            path = root / "input.bin"
            path.write_bytes(b"payload")
            sha = hashlib.sha256(b"payload").hexdigest()
            terminal = SyntheticTerminalOwner(ep, sha)
            admission = SyntheticAdmissionOwner(ep)
            runner = WorkerEpisodeRunner(
                ep.pm, ep.nal, BoundLocalFileReader(root, {"LOCAL-MATERIAL": path}),
                terminal, admission, enabled=True)
            result = runner.run(issued)
            self.assertEqual(result["status"], "LOCAL_NAL_EPISODE_CLOSED")
            self.assertEqual(result["material_sha256"], sha)
            self.assertEqual(result["material_bytes"], 7)
            self.assertEqual(result["receipt"]["task_ref"], issued.task_ref)
            self.assertEqual(terminal.calls, 1)
            self.assertEqual(admission.calls, 1)
            restart = runner.run(issued)
            self.assertEqual(restart["status"], "HOLD_RESTART_REQUALIFICATION")
            self.assertEqual(terminal.calls, 1)
            self.assertEqual(admission.calls, 1)

    def test_revoke_first_blocks_consume_and_local_read(self):
        ep, issued = local_episode()
        ep.revoke_parent()
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            path = root / "input.bin"
            path.write_bytes(b"payload")
            terminal = SyntheticTerminalOwner(
                ep, hashlib.sha256(b"payload").hexdigest())
            runner = WorkerEpisodeRunner(
                ep.pm, ep.nal,
                BoundLocalFileReader(root, {"LOCAL-MATERIAL": path}),
                terminal, SyntheticAdmissionOwner(ep), enabled=True)
            with self.assertRaises(WorkerContextAuthError):
                runner.run(issued)
            self.assertEqual(ep.store.read(WORKER_ACTOR)["current_task"]["status"],
                             "ADMITTED")
            self.assertEqual(terminal.calls, 0)

    def test_unknown_commit_holds_without_start_or_work(self):
        store = UnknownAfterCommitStore()
        ep, issued = local_episode(store=store)
        store.unknown_once = True
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            path = root / "input.bin"
            path.write_bytes(b"payload")
            terminal = SyntheticTerminalOwner(
                ep, hashlib.sha256(b"payload").hexdigest())
            runner = WorkerEpisodeRunner(
                ep.pm, ep.nal,
                BoundLocalFileReader(root, {"LOCAL-MATERIAL": path}),
                terminal, SyntheticAdmissionOwner(ep), enabled=True)
            result = runner.run(issued)
            self.assertEqual(result["status"], "HOLD_UNKNOWN")
            self.assertEqual(result["authoritative_readback"],
                             "TRANSITION_COMMITTED_READBACK")
            self.assertEqual(ep.store.read(WORKER_ACTOR)["current_task"]["status"],
                             "CONSUMED")
            self.assertEqual(terminal.calls, 0)
            self.assertEqual(runner.run(issued)["status"],
                             "HOLD_RESTART_REQUALIFICATION")

    def test_local_reader_rejects_unbound_and_escape(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as outside:
            root = pathlib.Path(temp)
            escaped = pathlib.Path(outside) / "x"
            escaped.write_bytes(b"x")
            reader = BoundLocalFileReader(root, {"ESCAPE": escaped})
            for ref, code in (("OTHER", "LOCAL_TARGET_NOT_BOUND"),
                              ("ESCAPE", "LOCAL_TARGET_OUTSIDE_BOUND_ROOT")):
                with self.subTest(ref=ref), self.assertRaises(Hold) as caught:
                    reader.digest(ref)
                self.assertEqual(caught.exception.code, code)


class ExactUnknownOutcomeProof(unittest.TestCase):
    @staticmethod
    def mark_owner_call(owner, method, callback):
        original = getattr(owner, method)
        def wrapped(**kwargs):
            receipt = original(**kwargs)
            callback()
            return receipt
        setattr(owner, method, wrapped)

    def test_terminal_committed_lost_ack_exact_once_and_repeat_readback(self):
        store = UnknownAfterCommitStore()
        ep, issued = local_episode(store=store)
        with tempfile.TemporaryDirectory() as temp:
            runner, terminal, admission = make_local_runner(ep, pathlib.Path(temp))
            self.mark_owner_call(
                terminal, "record_local_digest",
                lambda: setattr(store, "unknown_once", True))
            first = runner.run(issued)
            self.assertEqual(first["status"], "COMMITTED_EXACT_READBACK")
            self.assertEqual(first["transition"], "WORK_TERMINAL")
            self.assertEqual(first["outcome"], "COMMITTED_EXACT")
            self.assertEqual(
                runner.read_ambiguous_outcome(
                    first["outcome_key"], "WORK_TERMINAL"), first)
            state = store.read(WORKER_ACTOR)
            self.assertEqual(state["current_task"]["status"], "TERMINAL")
            self.assertEqual(len(state["terminals"]), 1)
            self.assertEqual(len(state["receipts"]), 0)
            self.assertEqual(terminal.calls, 1)
            self.assertEqual(admission.calls, 0)
            self.assertEqual(runner.run(issued)["status"],
                             "HOLD_RESTART_REQUALIFICATION")
            self.assertEqual(terminal.calls, 1)

    def test_terminal_before_commit_unknown_absent_no_replay(self):
        ep, issued = local_episode()
        with tempfile.TemporaryDirectory() as temp:
            runner, terminal, admission = make_local_runner(ep, pathlib.Path(temp))
            self.mark_owner_call(
                terminal, "record_local_digest",
                lambda: fail_before_commit_once(ep.store))
            first = runner.run(issued)
            self.assertEqual((first["status"], first["outcome"]),
                             ("HOLD_UNKNOWN", "ABSENT"))
            self.assertEqual(
                runner.read_ambiguous_outcome(
                    first["outcome_key"], "WORK_TERMINAL")["outcome"], "ABSENT")
            state = ep.store.read(WORKER_ACTOR)
            self.assertEqual(state["current_task"]["status"], "STARTED")
            self.assertEqual(state["terminals"], {})
            self.assertEqual(admission.calls, 0)
            self.assertEqual(runner.run(issued)["status"],
                             "HOLD_RESTART_REQUALIFICATION")
            self.assertEqual(terminal.calls, 1)

    def test_close_committed_lost_ack_exact_receipt_outbox_once(self):
        store = UnknownAfterCommitStore()
        ep, issued = local_episode(store=store)
        with tempfile.TemporaryDirectory() as temp:
            runner, terminal, admission = make_local_runner(ep, pathlib.Path(temp))
            self.mark_owner_call(
                admission, "admit_terminal",
                lambda: setattr(store, "unknown_once", True))
            first = runner.run(issued)
            self.assertEqual(first["status"], "COMMITTED_EXACT_READBACK")
            self.assertEqual(first["transition"], "TASK_CLOSE")
            self.assertEqual(first["receipt"]["task_ref"], issued.task_ref)
            self.assertEqual(
                runner.read_ambiguous_outcome(
                    first["outcome_key"], "TASK_CLOSE"), first)
            state = store.read(WORKER_ACTOR)
            self.assertEqual(state["current_task"]["status"], "CLOSED")
            self.assertEqual((len(state["terminals"]), len(state["receipts"]),
                              len(state["outbox"])), (1, 1, 1))
            self.assertEqual((terminal.calls, admission.calls), (1, 1))
            self.assertEqual(runner.run(issued)["status"],
                             "HOLD_RESTART_REQUALIFICATION")
            self.assertEqual((terminal.calls, admission.calls), (1, 1))

    def test_close_before_commit_unknown_absent_no_replay(self):
        ep, issued = local_episode()
        with tempfile.TemporaryDirectory() as temp:
            runner, terminal, admission = make_local_runner(ep, pathlib.Path(temp))
            self.mark_owner_call(
                admission, "admit_terminal",
                lambda: fail_before_commit_once(ep.store))
            first = runner.run(issued)
            self.assertEqual((first["status"], first["outcome"]),
                             ("HOLD_UNKNOWN", "ABSENT"))
            self.assertEqual(
                runner.read_ambiguous_outcome(
                    first["outcome_key"], "TASK_CLOSE")["outcome"], "ABSENT")
            state = ep.store.read(WORKER_ACTOR)
            self.assertEqual(state["current_task"]["status"], "TERMINAL")
            self.assertEqual((len(state["terminals"]), len(state["receipts"]),
                              len(state["outbox"])), (1, 0, 0))
            self.assertEqual((terminal.calls, admission.calls), (1, 1))
            self.assertEqual(runner.run(issued)["status"],
                             "HOLD_RESTART_REQUALIFICATION")

    def test_wrong_persisted_result_material_admission_receipt_outbox_hold(self):
        store = UnknownAfterCommitStore()
        ep, issued = local_episode(store=store)
        with tempfile.TemporaryDirectory() as temp:
            runner, terminal, admission = make_local_runner(ep, pathlib.Path(temp))
            self.mark_owner_call(
                admission, "admit_terminal",
                lambda: setattr(store, "unknown_once", True))
            first = runner.run(issued)
            key = first["outcome_key"]
            original_state = copy.deepcopy(store._states[WORKER_ACTOR])
            original_terminal = copy.deepcopy(
                ep.evidence.rows[("WORKER_TERMINAL", key["terminal_evidence_ref"])])
            original_admission = copy.deepcopy(
                ep.evidence.rows[("PM_ADMISSION", key["admission_ref"])])
            ref = key["terminal_ref"]
            receipt_ref = first["receipt"]["receipt_ref"]
            event_ref = first["receipt"]["event_ref"]
            mutations = (
                ("result", lambda s: s["terminals"][ref].__setitem__("result", "HOLD")),
                ("material", lambda s: s["terminals"][ref].__setitem__(
                    "material_sha256", "0" * 64)),
                ("receipt", lambda s: s["receipts"][ref].__setitem__(
                    "receipt_ref", "WRONG")),
                ("outbox", lambda s: s["outbox"][event_ref]["event"].__setitem__(
                    "receipt_ref", "WRONG")),
            )
            for label, mutate in mutations:
                with self.subTest(label=label):
                    store._states[WORKER_ACTOR] = copy.deepcopy(original_state)
                    mutate(store._states[WORKER_ACTOR])
                    result = runner.read_ambiguous_outcome(key, "TASK_CLOSE")
                    self.assertEqual(result["status"], "HOLD_UNKNOWN")
                    self.assertNotEqual(result["outcome"], "COMMITTED_EXACT")
            store._states[WORKER_ACTOR] = copy.deepcopy(original_state)
            ep.evidence.rows[("WORKER_TERMINAL", key["terminal_evidence_ref"])][
                "material_sha256"] = "0" * 64
            self.assertEqual(
                runner.read_ambiguous_outcome(key, "TASK_CLOSE")["outcome"],
                "OWNER_TERMINAL_MISMATCH")
            ep.evidence.rows[("WORKER_TERMINAL", key["terminal_evidence_ref"])] = (
                original_terminal)
            ep.evidence.rows[("PM_ADMISSION", key["admission_ref"])][
                "material_sha256"] = "0" * 64
            self.assertEqual(
                runner.read_ambiguous_outcome(key, "TASK_CLOSE")["outcome"],
                "OWNER_ADMISSION_MISMATCH")
            ep.evidence.rows[("PM_ADMISSION", key["admission_ref"])] = original_admission
            self.assertEqual(
                runner.read_ambiguous_outcome(key, "TASK_CLOSE")["status"],
                "COMMITTED_EXACT_READBACK")

    def test_forged_owner_admission_cannot_close(self):
        ep, issued = local_episode()
        with tempfile.TemporaryDirectory() as temp:
            runner, terminal, admission = make_local_runner(ep, pathlib.Path(temp))
            original = admission.admit_terminal
            def forged(**kwargs):
                ref = original(**kwargs)
                ep.evidence.rows[("PM_ADMISSION", ref)]["material_sha256"] = "0" * 64
                return ref
            admission.admit_terminal = forged
            with self.assertRaises(Hold) as caught:
                runner.run(issued)
            self.assertEqual(caught.exception.code,
                             "OWNER_ADMISSION_MATERIAL_READBACK_REQUIRED")
            state = ep.store.read(WORKER_ACTOR)
            self.assertEqual(state["current_task"]["status"], "TERMINAL")
            self.assertEqual((len(state["terminals"]), len(state["receipts"]),
                              len(state["outbox"])), (1, 0, 0))

    def test_forged_outcome_key_cannot_resolve_committed_close(self):
        store = UnknownAfterCommitStore()
        ep, issued = local_episode(store=store)
        with tempfile.TemporaryDirectory() as temp:
            runner, terminal, admission = make_local_runner(ep, pathlib.Path(temp))
            self.mark_owner_call(
                admission, "admit_terminal",
                lambda: setattr(store, "unknown_once", True))
            first = runner.run(issued)
            for field, wrong in (
                ("task_revision", 9),
                ("task_sha256", "0" * 64),
                ("generation_ref", "WRONG"),
                ("result", "HOLD"),
                ("admission_ref", "WRONG"),
            ):
                with self.subTest(field=field):
                    key = dict(first["outcome_key"])
                    key[field] = wrong
                    result = runner.read_ambiguous_outcome(key, "TASK_CLOSE")
                    self.assertEqual(result["status"], "HOLD_UNKNOWN")
            self.assertEqual(
                runner.read_ambiguous_outcome(
                    first["outcome_key"], "TASK_CLOSE")["status"],
                "COMMITTED_EXACT_READBACK")


    def test_caller_terminal_receipt_cannot_forge_material_or_close(self):
        ep, issued = local_episode()
        with tempfile.TemporaryDirectory() as temp:
            runner, terminal, admission = make_local_runner(ep, pathlib.Path(temp))
            original = terminal.record_local_digest
            def forged(**kwargs):
                row = original(**kwargs)
                row["material_sha256"] = "0" * 64
                return row
            terminal.record_local_digest = forged
            with self.assertRaises(Hold) as caught:
                runner.run(issued)
            self.assertEqual(caught.exception.code,
                             "TRUSTED_TERMINAL_OWNER_RECEIPT_REQUIRED")
            state = ep.store.read(WORKER_ACTOR)
            self.assertEqual(state["current_task"]["status"], "STARTED")
            self.assertEqual((len(state["terminals"]), len(state["receipts"]),
                              len(state["outbox"])), (0, 0, 0))
            self.assertEqual(admission.calls, 0)


class ParentAdapterProof(unittest.TestCase):
    def test_default_off_does_not_connect(self):
        def forbidden():
            self.fail("default OFF connected")
        with self.assertRaises(Hold) as caught:
            PgParentAuthority(forbidden).read_current(mandate_ref="M")
        self.assertEqual(caught.exception.code, "PARENT_AUTHORITY_DEFAULT_OFF")
        with self.assertRaises(Hold) as caught:
            PgParentAuthorityIssuer(forbidden, "rights").issue_rights({})
        self.assertEqual(caught.exception.code, "PARENT_ISSUER_DEFAULT_OFF")

    def test_cursor_required_and_hash_not_authority(self):
        owner = PgParentAuthority(enabled=True)
        basis = {"decision_ref": "D"}
        with self.assertRaises(Hold) as caught:
            owner.hold_current(
                basis=basis, basis_fingerprint=digest(basis),
                expected_mandate_owner_revision=1,
                expected_delegation_owner_revision=1,
                expected_delegation_fence_revision=1)
        self.assertEqual(caught.exception.code,
                         "PARENT_SAME_TRANSACTION_CURSOR_REQUIRED")
        with self.assertRaises(Hold) as caught:
            owner.hold_current(
                basis=basis, basis_fingerprint="0" * 64,
                expected_mandate_owner_revision=1,
                expected_delegation_owner_revision=1,
                expected_delegation_fence_revision=1,
                cursor=object(), expected_task={})
        self.assertEqual(caught.exception.code,
                         "PARENT_BASIS_FINGERPRINT_MISMATCH")


    def test_finite_parent_denied_before_nal_transition_draft(self):
        class TimedError(psycopg.Error):
            sqlstate = "P0T01"

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def execute(self, sql, *_):
                self.sql = sql
                if "acquire_parent_fence" in sql:
                    raise TimedError("PARENT_TIMED_EXPIRY_UNSUPPORTED")

            def fetchone(self):
                return None if "state_json" in self.sql else ("cerebro_nal_owner",)

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def transaction(self):
                return nullcontext()

            def cursor(self):
                return Cursor()

        store = PostgresStore(lambda: Connection(), enabled=True)
        store._preflight = lambda cursor: None
        owner = PgParentAuthority(enabled=True)
        basis = {"decision_ref": "D"}
        calls = []
        def fence_factory(snapshot, *, cursor):
            return owner.hold_current(
                basis=basis, basis_fingerprint=digest(basis),
                expected_mandate_owner_revision=1,
                expected_delegation_owner_revision=1,
                expected_delegation_fence_revision=1,
                cursor=cursor, expected_task={})
        def transition(state):
            calls.append("DRAFT")
            return {"actor_ref": "A", "generation_ref": "G"}, Result(
                "WRITTEN", mutated=True)
        with self.assertRaises(Hold) as caught:
            store.apply("A", transition, owner_fence_factory=fence_factory)
        self.assertEqual(caught.exception.code,
                         "PARENT_TIMED_EXPIRY_UNSUPPORTED")
        self.assertEqual(calls, [])
        sql = (ROOT / "candidates" / "natural-actor-lifecycle-v0.1" /
               "schema_parent_authority_local_v1.sql").read_text()
        for row in ("r", "d", "t"):
            self.assertIn(
                f"IF FOUND AND {row}.expires_at IS NOT NULL THEN", sql)
        self.assertEqual(sql.count("ERRCODE='P0T01'"), 3)
        child = (ROOT / "candidates" / "natural-actor-lifecycle-v0.1" /
                 "schema_owner_binding_local_v1.sql").read_text()
        self.assertNotIn("expires_at", child)

class UnknownCommitMappingProof(unittest.TestCase):
    def test_committed_but_bad_readback_is_hold_unknown(self):
        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def execute(self, *_):
                pass

            def fetchone(self):
                return None

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def transaction(self):
                return nullcontext()

            def cursor(self):
                return Cursor()

        store = PostgresStore(lambda: Connection(), enabled=True)
        store._preflight = lambda cursor: None
        store._append_ledgers = lambda cursor, old, new: None
        store.read = lambda actor: {"actor_ref": actor, "generation_ref": "G",
                                    "wrong": True}
        with self.assertRaises(Hold) as caught:
            store.apply(
                "A", lambda old: ({"actor_ref": "A", "generation_ref": "G"},
                                  Result("SAVED", mutated=True)))
        self.assertEqual(caught.exception.code, "PG_COMMIT_OUTCOME_UNKNOWN")


PG_DSNS = {
    kind: os.environ.get("P1625_" + kind.upper() + "_DSN")
    for kind in ("worker", "rights", "admin", "decision")
}


@unittest.skipUnless(all(PG_DSNS.values()),
                     "disposable P1625 PostgreSQL DSNs and migration required")
class DisposablePgOracle(unittest.TestCase):
    def setUp(self):
        self.ep, _ = local_episode()
        suffix = uuid.uuid4().hex[:10]
        # Distinct refs avoid collisions across an already migrated disposable DB.
        r = self.ep.pm_state.rights
        d = self.ep.pm_state.delegation
        t = self.ep.worker_owners
        r["mandate_ref"] += "-" + suffix
        r["basis_sha256"] = _canonical_sha256(
            BoundPmAuthorityV02._rights_basis(r))
        d["mandate_ref"] = r["mandate_ref"]
        d["delegation_ref"] += "-" + suffix
        t.delegation_ref = d["delegation_ref"]
        t.decision_ref += "-" + suffix
        t.task_ref += "-" + suffix
        original_task = t.task
        t.task = lambda **kwargs: {
            **original_task(**kwargs),
            "authority_mandate_ref": r["mandate_ref"],
        }
        self.ep.snapshot = self.ep.pm.read_current_consumer_authority(
            mandate_ref=r["mandate_ref"], pm_actor_ref=PM_ACTOR,
            pm_generation_ref=PM_GENERATION,
            worker_generation_ref=WORKER_GENERATION,
            worker_actor_ref=WORKER_ACTOR, source_revision=SOURCE,
            method_fingerprint=METHOD, task_ref=t.task_ref,
            expected_task_revision=t.task_revision,
            expected_task_sha256=t.task_sha256)
        self.connect = {k: (lambda key=k: psycopg.connect(PG_DSNS[key]))
                        for k in PG_DSNS}
        self.rights = PgParentAuthorityIssuer(self.connect["rights"], "rights",
                                              enabled=True)
        self.admin = PgParentAuthorityIssuer(self.connect["admin"], "delegation",
                                             enabled=True)
        self.decision = PgParentAuthorityIssuer(self.connect["decision"], "decision",
                                                enabled=True)
        self.worker = PgParentAuthority(self.connect["worker"], enabled=True)
        self.rights.issue_rights(r)
        self.admin.issue_delegation(d)
        self.decision.issue_decision(self.ep.snapshot)

    def hold(self):
        snap = self.ep.snapshot
        with self.connect["worker"]() as conn, conn.cursor() as cur:
            fence = self.worker.hold_current(
                basis=snap["immutable_basis"],
                basis_fingerprint=snap["basis_fingerprint"],
                expected_mandate_owner_revision=snap["rights"]["owner_revision"],
                expected_delegation_owner_revision=snap["delegation"]["owner_revision"],
                expected_delegation_fence_revision=snap["delegation"]["fence_revision"],
                expected_task=snap["task"], cursor=cur)
            with fence:
                fence.assert_current()

    def test_revoke_first_blocks_parent_reservation(self):
        self.rights.revoke(self.ep.pm_state.rights["mandate_ref"], 10)
        with self.assertRaises(Hold) as caught:
            self.hold()
        self.assertEqual(caught.exception.code, "PARENT_AUTHORITY_CHANGED_BEFORE_COMMIT")

    def test_supersede_first_blocks_parent_reservation(self):
        self.admin.retire(
            self.ep.pm_state.delegation["delegation_ref"], 20, "SUPERSEDED")
        with self.assertRaises(Hold) as caught:
            self.hold()
        self.assertEqual(caught.exception.code,
                         "PARENT_AUTHORITY_CHANGED_BEFORE_COMMIT")

    def test_expire_first_blocks_parent_reservation(self):
        self.decision.retire(
            self.ep.snapshot["immutable_basis"]["decision_ref"], 1, "EXPIRED")
        with self.assertRaises(Hold) as caught:
            self.hold()
        self.assertEqual(caught.exception.code,
                         "PARENT_AUTHORITY_CHANGED_BEFORE_COMMIT")

    def test_future_finite_decision_is_typed_unsupported(self):
        snapshot = copy.deepcopy(self.ep.snapshot)
        basis = snapshot["immutable_basis"]
        basis["task_ref"] += "-FINITE"
        basis["decision_ref"] += "-FINITE"
        snapshot["task"]["task_ref"] = basis["task_ref"]
        snapshot["task"]["decision_ref"] = basis["decision_ref"]
        snapshot["basis_fingerprint"] = digest(basis)
        self.decision.issue_decision(
            snapshot, expires_at=datetime.now(timezone.utc) + timedelta(days=1))
        with self.connect["worker"]() as conn, conn.cursor() as cur:
            fence = self.worker.hold_current(
                basis=basis,
                basis_fingerprint=snapshot["basis_fingerprint"],
                expected_mandate_owner_revision=snapshot["rights"]["owner_revision"],
                expected_delegation_owner_revision=snapshot["delegation"]["owner_revision"],
                expected_delegation_fence_revision=snapshot["delegation"]["fence_revision"],
                expected_task=snapshot["task"], cursor=cur)
            with self.assertRaises(Hold) as caught:
                with fence:
                    self.fail("future finite authority reached NAL draft")
            self.assertEqual(caught.exception.code,
                             "PARENT_TIMED_EXPIRY_UNSUPPORTED")

    def test_wrong_revision_and_task_basis_rejected(self):
        snap = self.ep.snapshot
        with self.connect["worker"]() as conn, conn.cursor() as cur:
            fence = self.worker.hold_current(
                basis=snap["immutable_basis"],
                basis_fingerprint=snap["basis_fingerprint"],
                expected_mandate_owner_revision=11,
                expected_delegation_owner_revision=20,
                expected_delegation_fence_revision=30,
                expected_task=snap["task"], cursor=cur)
            with self.assertRaises(Hold) as caught:
                fence.assert_current()
            self.assertEqual(caught.exception.code,
                             "PARENT_AUTHORITY_CHANGED_BEFORE_COMMIT")
        wrong = dict(snap["immutable_basis"])
        wrong["worker_generation_ref"] = "WRONG-GENERATION"
        with self.connect["worker"]() as conn, conn.cursor() as cur:
            fence = self.worker.hold_current(
                basis=wrong, basis_fingerprint=digest(wrong),
                expected_mandate_owner_revision=10,
                expected_delegation_owner_revision=20,
                expected_delegation_fence_revision=30,
                expected_task=snap["task"], cursor=cur)
            with self.assertRaises(Hold) as caught:
                fence.assert_current()
            self.assertEqual(caught.exception.code,
                             "PARENT_AUTHORITY_CHANGED_BEFORE_COMMIT")

    def test_commit_first_reservation_serializes_revoke(self):
        acquired = threading.Event()
        permit = threading.Event()
        revoking = threading.Event()
        revoked = threading.Event()
        snap = self.ep.snapshot

        def hold_transaction():
            with self.connect["worker"]() as conn, conn.cursor() as cur:
                fence = self.worker.hold_current(
                    basis=snap["immutable_basis"],
                    basis_fingerprint=snap["basis_fingerprint"],
                    expected_mandate_owner_revision=snap["rights"]["owner_revision"],
                    expected_delegation_owner_revision=snap["delegation"]["owner_revision"],
                    expected_delegation_fence_revision=snap["delegation"]["fence_revision"],
                    expected_task=snap["task"], cursor=cur)
                with fence:
                    fence.assert_current()
                    acquired.set()
                    if not permit.wait(5):
                        raise RuntimeError("test timed out")

        def revoke():
            revoking.set()
            result = self.rights.revoke(self.ep.pm_state.rights["mandate_ref"], 10)
            revoked.set()
            return result

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(hold_transaction)
            self.assertTrue(acquired.wait(5))
            second = pool.submit(revoke)
            self.assertTrue(revoking.wait(5))
            self.assertFalse(revoked.wait(0.1))
            permit.set()
            first.result(timeout=5)
            second.result(timeout=5)
        self.assertTrue(revoked.is_set())

    def test_roles_cannot_cross_issuer_and_worker_boundary(self):
        with self.connect["worker"]() as conn, conn.cursor() as cur:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                cur.execute("SELECT pm_authority.revoke_rights(%s,%s)",
                            (self.ep.pm_state.rights["mandate_ref"], 10))
        with self.connect["rights"]() as conn, conn.cursor() as cur:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                cur.execute("SELECT pm_authority.acquire_parent_fence(%s,%s,%s,%s,%s)",
                            (psycopg.types.json.Jsonb(self.ep.snapshot["immutable_basis"]),
                             self.ep.snapshot["basis_fingerprint"],10,20,30))


if __name__ == "__main__":
    unittest.main()
