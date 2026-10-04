#!/usr/bin/env python3
"""P1613 full-chain NAL authority/revocation local proof.

Local/default-off only. Synthetic owner ports exercise the exact integrated
WORKER/PM-authority/NAL seam; they do not install a Human mandate, production
PM delegation, provider owner, database credential, Worker, task or effect.
"""
from __future__ import annotations

import copy
import hashlib
import pathlib
import sys
import threading
import time
import unittest
from contextlib import ExitStack

ROOT = pathlib.Path(__file__).resolve().parents[2]
NAL = ROOT / "candidates" / "natural-actor-lifecycle-v0.1"
sys.path[:0] = [str(ROOT / "mcp"), str(ROOT / "tooling" / "validator"), str(NAL)]

from lifecycle import (  # noqa: E402
    BINDING_SCHEMA,
    HISTORICAL_TERMINAL_SCHEMA,
    TERMINAL_SCHEMA,
    Hold,
    Lifecycle,
    MemoryStore,
    digest,
)
from worker_context_auth import (  # noqa: E402
    BoundPmAuthorityV02,
    BoundWorkerPolicyResolver,
    WorkerContextAuthError,
)
from owner_binding_pg import PgOwnerBindingEvidence  # noqa: E402
from pm_authority_v02_validation import (  # noqa: E402
    ADMIN_GRANT,
    DELEGATION,
    MANDATE,
    METHOD,
    PM_ACTOR,
    PM_GENERATION,
    SOURCE,
    WORKER_ACTOR,
    WORKER_GENERATION,
    AdminOwner,
    EffectOwner,
    MandateReader,
    Reader,
    SharedAuthorityState,
    WorkerClaimReader,
    WorkerOwners,
)


class ChildFence:
    def __init__(self, evidence, binding_ref):
        self.evidence = evidence
        self.binding_ref = binding_ref
        self.entered = False

    def __enter__(self):
        self.evidence.child_lock.acquire()
        self.entered = True
        return self

    def __exit__(self, *_):
        self.entered = False
        self.evidence.child_lock.release()
        return False

    def assert_current(self, expected):
        if not self.entered:
            raise Hold("OWNER_COMMIT_FENCE_UNAVAILABLE")
        actual = self.evidence.rows.get(("OWNER_BINDING", self.binding_ref))
        if actual != expected or actual.get("current") is not True or actual.get("revoked") is not False:
            raise Hold("OWNER_BINDING_CHANGED_BEFORE_COMMIT")


class CombinedFence:
    """Same local order as candidate PG adapter: actor -> parent -> child."""

    def __init__(self, parent, child):
        self.parent = parent
        self.child = child
        self.stack = None

    def __enter__(self):
        self.stack = ExitStack()
        self.stack.__enter__()
        self.stack.enter_context(self.parent)
        self.stack.enter_context(self.child)
        return self

    def __exit__(self, *exc):
        return self.stack.__exit__(*exc)

    def assert_current(self, expected, transition):
        self.parent.assert_current()
        self.child.assert_current(expected)


class AuthorityEvidence:
    def __init__(self, pm, snapshot):
        self.rows = {}
        self.child_lock = threading.RLock()
        self.pm = pm
        self.snapshot = snapshot

    def seed(self, kind, ref, **fields):
        row = {"evidence_ref": ref, "provider_revision": "p1613-fixture-r1", **fields}
        with self.child_lock:
            self.rows[(kind, ref)] = row
        return row

    def read(self, kind, ref):
        with self.child_lock:
            return copy.deepcopy(self.rows.get((kind, ref)))

    def revoke_child(self, binding_ref):
        with self.child_lock:
            row = self.rows[("OWNER_BINDING", binding_ref)]
            row["current"] = False
            row["revoked"] = True

    def owner_commit_fence(self, binding_ref, *, cursor=None):
        return ChildFence(self, binding_ref)

    def transition_authority_fence(
        self, binding_ref, *, transition, task, expected_binding, cursor=None
    ):
        parent_factory = self.pm.nal_transition_parent_fence_factory(self.snapshot)
        parent = parent_factory(
            binding_ref=binding_ref,
            transition=transition,
            task=copy.deepcopy(task),
            expected_binding=copy.deepcopy(expected_binding),
            cursor=cursor,
        )
        return CombinedFence(parent, ChildFence(self, binding_ref))


class UnknownAfterCommitStore(MemoryStore):
    def __init__(self):
        super().__init__()
        self.unknown_once = False

    def apply(self, actor_ref, transition, *, owner_fence_factory=None):
        result = super().apply(
            actor_ref, transition, owner_fence_factory=owner_fence_factory
        )
        if self.unknown_once and result.mutated:
            self.unknown_once = False
            raise Hold("PG_COMMIT_OUTCOME_UNKNOWN")
        return result


class Episode:
    def __init__(self, *, store=None):
        self.pm_state = SharedAuthorityState()
        self.worker_owners = WorkerOwners()
        self.worker_policy = BoundWorkerPolicyResolver(
            human_intent_reader=object(),
            mandate_reader=MandateReader(self.worker_owners),
            claim_reader=WorkerClaimReader(self.worker_owners),
            enabled=True,
        )
        self.pm = BoundPmAuthorityV02(
            self.worker_policy,
            Reader(self.pm_state.rights_current),
            Reader(self.pm_state.delegation_current),
            AdminOwner(self.pm_state),
            EffectOwner(self.pm_state),
            enabled=True,
        )
        self.pm.bind_or_renew_pm_generation(
            mandate_ref=MANDATE,
            pm_actor_ref=PM_ACTOR,
            pm_generation_ref=PM_GENERATION,
        )
        self.snapshot = self.pm.read_current_consumer_authority(
            mandate_ref=MANDATE,
            pm_actor_ref=PM_ACTOR,
            pm_generation_ref=PM_GENERATION,
            worker_generation_ref=WORKER_GENERATION,
            worker_actor_ref=WORKER_ACTOR,
            source_revision=SOURCE,
            method_fingerprint=METHOD,
            task_ref=self.worker_owners.task_ref,
            expected_task_revision=self.worker_owners.task_revision,
            expected_task_sha256=self.worker_owners.task_sha256,
        )
        self.evidence = AuthorityEvidence(self.pm, self.snapshot)
        self.store = store or MemoryStore()
        self.nal = Lifecycle(self.store, self.evidence, enabled=True)
        self.binding_ref = "BINDING-P1613"
        self.consume_ref = "CONSUME-P1613"
        self.start_ref = "START-P1613"

    def seed_base(self):
        self.evidence.seed(
            "ROLE_READY",
            "READY-P1613",
            actor_ref=WORKER_ACTOR,
            generation_ref=WORKER_GENERATION,
            pre_role_lifecycle="READY_UNBOUND",
            birth_role=None,
            attached_role="WORKER",
            claim_ref=None,
            source_revision=SOURCE,
            role_projection_fingerprint="ROLE-PROJECTION-P1613",
            role_attach_receipt="ROLE-ATTACH-P1613",
        )
        self.nal.register_worker_ready(
            WORKER_ACTOR, WORKER_GENERATION, "READY-P1613"
        )

        task = self.worker_owners.task(task_ref=self.worker_owners.task_ref)
        self.evidence.seed(
            "ADMITTED_TASK",
            "ADMITTED-P1613",
            actor_ref=WORKER_ACTOR,
            generation_ref=WORKER_GENERATION,
            actor_role="WORKER",
            source_revision=SOURCE,
            role_projection_fingerprint="ROLE-PROJECTION-P1613",
            task_ref=task["task_ref"],
            attempt_ref="ATTEMPT-P1613",
            claim_ref=task["claim_ref"],
            packet_ref=task["packet_ref"],
            queue_ref=task["queue_ref"],
            task_payload_sha256=task["task_sha256"],
            admission_receipt="TASK-ADMISSION-P1613",
            owner_revision="OWNER-TASK-P1613-R1",
            admitted=True,
            task_revision=task["task_revision"],
            decision_ref=task["decision_ref"],
            provenance_ref=task["readback_ref"],
        )
        self.nal.admit_task(WORKER_ACTOR, "ADMITTED-P1613")

        state = self.store.read(WORKER_ACTOR)
        task_state = state["current_task"]
        core = {
            "schema": BINDING_SCHEMA,
            "binding_ref": self.binding_ref,
            "semantic_ref": "semantic:p1613",
            "actor_ref": WORKER_ACTOR,
            "generation_ref": WORKER_GENERATION,
            "task_ref": task_state["task_ref"],
            "attempt_ref": task_state["attempt_ref"],
            "claim_ref": task_state["claim_ref"],
            "packet_ref": task_state["packet_ref"],
            "queue_ref": task_state["queue_ref"],
            "actor_role": "WORKER",
            "source_revision": SOURCE,
            "role_projection_fingerprint": "ROLE-PROJECTION-P1613",
            "task_payload_sha256": task_state["task_payload_sha256"],
            "owner_revision": "CHILD-R1",
            "provider_revision": "p1613-fixture-r1",
            "admission_receipt": task_state["admission_receipt"],
            "supersedes": None,
        }
        self.evidence.seed(
            "OWNER_BINDING",
            self.binding_ref,
            **core,
            current=True,
            revoked=False,
            canonical_fingerprint=digest(core),
            issue_receipt="BINDING-ISSUE-P1613",
        )
        self.nal.issue_binding(WORKER_ACTOR, self.binding_ref)

    def worker_evidence(self, kind, ref, **extra):
        task = self.store.read(WORKER_ACTOR)["current_task"]
        self.evidence.seed(
            kind,
            ref,
            actor_ref=WORKER_ACTOR,
            generation_ref=WORKER_GENERATION,
            attempt_ref=task["attempt_ref"],
            claim_ref=task["claim_ref"],
            packet_ref=task["packet_ref"],
            queue_ref=task["queue_ref"],
            binding_ref=task["binding_ref"],
            receipt_ref="RECEIPT-" + ref,
            **extra,
        )
        return ref

    def seed_consume(self):
        return self.worker_evidence("WORKER_CONSUME", self.consume_ref)

    def seed_start(self):
        return self.worker_evidence("WORKER_START", self.start_ref)

    def seed_terminal(self, ref="TERMINAL-EVIDENCE-P1613", result="PASS"):
        task = self.store.read(WORKER_ACTOR)["current_task"]
        self.evidence.seed(
            "WORKER_TERMINAL",
            ref,
            schema=TERMINAL_SCHEMA,
            terminal_ref="TERMINAL-P1613",
            terminal_digest=digest(["terminal", result, task["task_ref"]]),
            result=result,
            actor_ref=WORKER_ACTOR,
            generation_ref=WORKER_GENERATION,
            attempt_ref=task["attempt_ref"],
            claim_ref=task["claim_ref"],
            packet_ref=task["packet_ref"],
            queue_ref=task["queue_ref"],
            binding_ref=task["binding_ref"],
            receipt_ref="RECEIPT-" + ref,
        )
        return ref

    def seed_admission(self, ref="PM-ADMISSION-P1613"):
        state = self.store.read(WORKER_ACTOR)
        terminal = state["terminals"]["TERMINAL-P1613"]
        self.evidence.seed(
            "PM_ADMISSION",
            ref,
            admitted=True,
            actor_ref=WORKER_ACTOR,
            generation_ref=WORKER_GENERATION,
            terminal_ref=terminal["terminal_ref"],
            terminal_digest=terminal["terminal_digest"],
            binding_ref=terminal["binding_ref"],
            claim_ref=terminal["claim_ref"],
            packet_ref=terminal["packet_ref"],
            queue_ref=terminal["queue_ref"],
            result=terminal["result"],
        )
        return ref

    def revoke_parent(self):
        # Direct owner-state mutation is the controlled "revoke committed after
        # read but before NAL commit" test hook. The commit guard must detect it.
        self.pm_state.delegation["current"] = False
        self.pm_state.delegation["revoked"] = True
        self.pm_state.delegation["owner_revision"] += 1
        self.pm_state.delegation["fence_revision"] += 1

    def revoke_child(self):
        self.evidence.revoke_child(self.binding_ref)


class NalAuthorityRevocationProof(unittest.TestCase):
    def assert_hold(self, code, fn):
        with self.assertRaises(Hold) as caught:
            fn()
        self.assertEqual(caught.exception.code, code)

    def test_pg_transition_adapter_without_parent_fence_fails_closed(self):
        adapter = PgOwnerBindingEvidence(
            connect_closer=lambda: None,
            fallback=object(),
            enabled=True,
            transition_parent_fence_factory=None,
        )
        with self.assertRaises(Hold) as caught:
            adapter.transition_authority_fence(
                "BINDING-P1613",
                transition="WORK_CONSUME",
                task={},
                expected_binding={},
                cursor=object(),
            )
        self.assertEqual(caught.exception.code, "PARENT_AUTHORITY_FENCE_UNAVAILABLE")

    def test_parent_revoke_after_read_before_consume_commit_zero_transition(self):
        ep = Episode()
        ep.seed_base()
        ep.seed_consume()
        ep.store.before_commit = ep.revoke_parent
        self.assert_hold(
            "FULL_AUTHORITY_CHANGED_BEFORE_COMMIT",
            lambda: ep.nal.worker_consume(WORKER_ACTOR, ep.consume_ref),
        )
        state = ep.store.read(WORKER_ACTOR)
        self.assertEqual(state["current_task"]["status"], "ADMITTED")
        self.assertIsNone(state["current_task"]["consume_ref"])

    def test_child_revoke_after_read_before_start_commit_zero_transition(self):
        ep = Episode()
        ep.seed_base()
        ep.seed_consume()
        ep.nal.worker_consume(WORKER_ACTOR, ep.consume_ref)
        ep.seed_start()
        ep.store.before_commit = ep.revoke_child
        self.assert_hold(
            "OWNER_BINDING_CHANGED_BEFORE_COMMIT",
            lambda: ep.nal.worker_start(WORKER_ACTOR, ep.start_ref),
        )
        state = ep.store.read(WORKER_ACTOR)
        self.assertEqual(state["current_task"]["status"], "CONSUMED")
        self.assertIsNone(state["current_task"]["start_ref"])

    def test_start_before_parent_revoke_gives_no_grace_to_progress_terminal(self):
        ep = Episode()
        ep.seed_base()
        ep.seed_consume()
        ep.nal.worker_consume(WORKER_ACTOR, ep.consume_ref)
        ep.seed_start()
        ep.nal.worker_start(WORKER_ACTOR, ep.start_ref)
        terminal_ref = ep.seed_terminal()
        ep.revoke_parent()
        self.assert_hold(
            "FULL_AUTHORITY_CHANGED_BEFORE_COMMIT",
            lambda: ep.nal.worker_terminal(WORKER_ACTOR, terminal_ref),
        )
        state = ep.store.read(WORKER_ACTOR)
        self.assertEqual(state["current_task"]["status"], "STARTED")
        self.assertEqual(state["terminals"], {})

    def test_transition_first_then_revoke_preserves_one_transition_receipt_state(self):
        ep = Episode()
        ep.seed_base()
        ep.seed_consume()
        first = ep.nal.worker_consume(WORKER_ACTOR, ep.consume_ref)
        self.assertEqual(first.status, "WORK_CONSUMED")
        ep.revoke_parent()
        # Exact replay is truth readback, not new authority/effect.
        replay = ep.nal.worker_consume(WORKER_ACTOR, ep.consume_ref)
        self.assertEqual(replay.status, "REPLAY_CONSUME")
        ep.seed_start()
        self.assert_hold(
            "FULL_AUTHORITY_CHANGED_BEFORE_COMMIT",
            lambda: ep.nal.worker_start(WORKER_ACTOR, ep.start_ref),
        )

    def test_ambiguous_commit_returns_hold_unknown_then_authoritative_readback(self):
        store = UnknownAfterCommitStore()
        ep = Episode(store=store)
        ep.seed_base()
        ep.seed_consume()
        store.unknown_once = True
        self.assert_hold(
            "HOLD_UNKNOWN",
            lambda: ep.nal.worker_consume(WORKER_ACTOR, ep.consume_ref),
        )
        reconciled = ep.nal.reconcile_transition(
            WORKER_ACTOR, "WORK_CONSUME", ep.consume_ref
        )
        self.assertEqual(reconciled.status, "TRANSITION_COMMITTED_READBACK")
        self.assertEqual(
            ep.nal.worker_consume(WORKER_ACTOR, ep.consume_ref).status,
            "REPLAY_CONSUME",
        )

    def test_parent_revoke_after_read_before_progress_terminal_commit_zero_terminal(self):
        ep = Episode()
        ep.seed_base()
        ep.seed_consume()
        ep.nal.worker_consume(WORKER_ACTOR, ep.consume_ref)
        ep.seed_start()
        ep.nal.worker_start(WORKER_ACTOR, ep.start_ref)
        terminal_ref = ep.seed_terminal()
        ep.store.before_commit = ep.revoke_parent
        self.assert_hold(
            "FULL_AUTHORITY_CHANGED_BEFORE_COMMIT",
            lambda: ep.nal.worker_terminal(WORKER_ACTOR, terminal_ref),
        )
        state = ep.store.read(WORKER_ACTOR)
        self.assertEqual(state["current_task"]["status"], "STARTED")
        self.assertEqual(state["terminals"], {})

    def test_normal_close_after_parent_revoke_is_denied_and_creates_no_outbox(self):
        ep = Episode()
        ep.seed_base()
        ep.seed_consume()
        ep.nal.worker_consume(WORKER_ACTOR, ep.consume_ref)
        ep.seed_start()
        ep.nal.worker_start(WORKER_ACTOR, ep.start_ref)
        terminal_ref = ep.seed_terminal()
        ep.nal.worker_terminal(WORKER_ACTOR, terminal_ref)
        admission = ep.seed_admission()
        ep.revoke_parent()
        self.assert_hold(
            "FULL_AUTHORITY_CHANGED_BEFORE_COMMIT",
            lambda: ep.nal.admit_release_once(
                WORKER_ACTOR, "TERMINAL-P1613", admission
            ),
        )
        state = ep.store.read(WORKER_ACTOR)
        self.assertEqual(state["receipts"], {})
        self.assertEqual(state["outbox"], {})
        self.assertEqual(state["current_task"]["status"], "TERMINAL")

    def test_close_commit_first_preserves_one_receipt_replay_after_revoke(self):
        ep = Episode()
        ep.seed_base()
        ep.seed_consume()
        ep.nal.worker_consume(WORKER_ACTOR, ep.consume_ref)
        ep.seed_start()
        ep.nal.worker_start(WORKER_ACTOR, ep.start_ref)
        terminal_ref = ep.seed_terminal()
        ep.nal.worker_terminal(WORKER_ACTOR, terminal_ref)
        admission = ep.seed_admission()
        first = ep.nal.admit_release_once(
            WORKER_ACTOR, "TERMINAL-P1613", admission
        )
        self.assertEqual(first.status, "TASK_CLOSED_RECEIPTED_OUTBOXED")
        receipt = copy.deepcopy(first.receipt)
        state = ep.store.read(WORKER_ACTOR)
        self.assertEqual(len(state["receipts"]), 1)
        self.assertEqual(len(state["outbox"]), 1)
        ep.revoke_parent()
        replay = ep.nal.admit_release_once(
            WORKER_ACTOR, "TERMINAL-P1613", admission
        )
        self.assertEqual(replay.status, "REPLAY_SAME_RECEIPT")
        self.assertEqual(replay.receipt, receipt)
        state = ep.store.read(WORKER_ACTOR)
        self.assertEqual(len(state["receipts"]), 1)
        self.assertEqual(len(state["outbox"]), 1)

    def test_pre_revoke_complete_delayed_historical_success_is_non_authorizing(self):
        ep = Episode()
        ep.seed_base()
        ep.seed_consume()
        ep.nal.worker_consume(WORKER_ACTOR, ep.consume_ref)
        ep.seed_start()
        ep.nal.worker_start(WORKER_ACTOR, ep.start_ref)
        ep.revoke_parent()
        task = ep.store.read(WORKER_ACTOR)["current_task"]
        result_sha = hashlib.sha256(b"already complete before revoke").hexdigest()
        ep.evidence.seed(
            "HISTORICAL_TERMINAL",
            "HIST-EVIDENCE-1",
            schema=HISTORICAL_TERMINAL_SCHEMA,
            report_ref="HIST-REPORT-1",
            disposition="HISTORICAL_AFTER_REVOKE",
            readback_verified=True,
            actor_ref=WORKER_ACTOR,
            generation_ref=WORKER_GENERATION,
            task_ref=task["task_ref"],
            task_revision=task["task_revision"],
            task_sha256=task["task_payload_sha256"],
            claim_ref=task["claim_ref"],
            packet_ref=task["packet_ref"],
            queue_ref=task["queue_ref"],
            decision_ref=task["decision_ref"],
            provenance_ref=task["provenance_ref"],
            binding_ref=task["binding_ref"],
            parent_revoke_ref="PARENT-REVOKE-R11",
            parent_revoked=True,
            parent_revoke_owner_revision=11,
            parent_revoke_readback_ref="PARENT-REVOKE-RB-11",
            historical_readback_ref="HIST-RB-1",
            result="PASS",
            substantive_complete_before_revoke=True,
            completion_evidence_ref="COMPLETE-BEFORE-REVOKE-1",
            result_sha256=result_sha,
            pre_revoke_execution_verified=True,
            pre_revoke_execution_ref="START-EXECUTION-RB-1",
        )
        outbox_before = copy.deepcopy(ep.store.read(WORKER_ACTOR)["outbox"])
        first = ep.nal.report_historical_terminal_after_revoke(
            WORKER_ACTOR, "HIST-EVIDENCE-1"
        )
        self.assertEqual(first.status, "HISTORICAL_AFTER_REVOKE")
        self.assertEqual(first.receipt["authority"], "NONE")
        self.assertFalse(first.receipt["new_work_authorized"])
        self.assertFalse(first.receipt["new_effect_authorized"])
        self.assertFalse(first.receipt["owner_obligation_created"])
        self.assertFalse(first.receipt["outbox_created"])
        self.assertEqual(ep.store.read(WORKER_ACTOR)["outbox"], outbox_before)
        replay = ep.nal.report_historical_terminal_after_revoke(
            WORKER_ACTOR, "HIST-EVIDENCE-1"
        )
        self.assertEqual(replay.status, "REPLAY_HISTORICAL_CLOSE")
        self.assertEqual(replay.receipt, first.receipt)

    def test_success_style_historical_report_without_pre_revoke_completion_denied(self):
        ep = Episode()
        ep.seed_base()
        ep.seed_consume()
        ep.nal.worker_consume(WORKER_ACTOR, ep.consume_ref)
        ep.seed_start()
        ep.nal.worker_start(WORKER_ACTOR, ep.start_ref)
        ep.revoke_parent()
        task = ep.store.read(WORKER_ACTOR)["current_task"]
        ep.evidence.seed(
            "HISTORICAL_TERMINAL",
            "HIST-EVIDENCE-BAD",
            schema=HISTORICAL_TERMINAL_SCHEMA,
            report_ref="HIST-REPORT-BAD",
            disposition="HISTORICAL_AFTER_REVOKE",
            readback_verified=True,
            actor_ref=WORKER_ACTOR,
            generation_ref=WORKER_GENERATION,
            task_ref=task["task_ref"],
            task_revision=task["task_revision"],
            task_sha256=task["task_payload_sha256"],
            claim_ref=task["claim_ref"],
            packet_ref=task["packet_ref"],
            queue_ref=task["queue_ref"],
            decision_ref=task["decision_ref"],
            provenance_ref=task["provenance_ref"],
            binding_ref=task["binding_ref"],
            parent_revoke_ref="PARENT-REVOKE-R11",
            parent_revoked=True,
            parent_revoke_owner_revision=11,
            parent_revoke_readback_ref="PARENT-REVOKE-RB-11",
            historical_readback_ref="HIST-RB-BAD",
            result="PASS",
            substantive_complete_before_revoke=False,
            completion_evidence_ref=None,
            result_sha256=hashlib.sha256(b"incomplete").hexdigest(),
            pre_revoke_execution_verified=True,
            pre_revoke_execution_ref="START-EXECUTION-RB-1",
        )
        self.assert_hold(
            "HISTORICAL_SUCCESS_PRE_REVOKE_COMPLETION_UNPROVEN",
            lambda: ep.nal.report_historical_terminal_after_revoke(
                WORKER_ACTOR, "HIST-EVIDENCE-BAD"
            ),
        )
        self.assertEqual(ep.store.read(WORKER_ACTOR)["outbox"], {})

    def test_aborted_revoked_administrative_close_authorizes_zero_effect(self):
        ep = Episode()
        ep.seed_base()
        ep.seed_consume()
        ep.nal.worker_consume(WORKER_ACTOR, ep.consume_ref)
        ep.seed_start()
        ep.nal.worker_start(WORKER_ACTOR, ep.start_ref)
        ep.revoke_parent()
        task = ep.store.read(WORKER_ACTOR)["current_task"]
        ep.evidence.seed(
            "HISTORICAL_TERMINAL",
            "ABORT-EVIDENCE-1",
            schema=HISTORICAL_TERMINAL_SCHEMA,
            report_ref="ABORT-REPORT-1",
            disposition="ABORTED_REVOKED",
            readback_verified=True,
            actor_ref=WORKER_ACTOR,
            generation_ref=WORKER_GENERATION,
            task_ref=task["task_ref"],
            task_revision=task["task_revision"],
            task_sha256=task["task_payload_sha256"],
            claim_ref=task["claim_ref"],
            packet_ref=task["packet_ref"],
            queue_ref=task["queue_ref"],
            decision_ref=task["decision_ref"],
            provenance_ref=task["provenance_ref"],
            binding_ref=task["binding_ref"],
            parent_revoke_ref="PARENT-REVOKE-R11",
            parent_revoked=True,
            parent_revoke_owner_revision=11,
            parent_revoke_readback_ref="PARENT-REVOKE-RB-11",
            historical_readback_ref="ABORT-RB-1",
            result="ABORTED",
            substantive_complete_before_revoke=False,
            completion_evidence_ref=None,
            result_sha256=None,
            pre_revoke_execution_verified=True,
            pre_revoke_execution_ref="START-EXECUTION-RB-1",
        )
        result = ep.nal.report_historical_terminal_after_revoke(
            WORKER_ACTOR, "ABORT-EVIDENCE-1"
        )
        self.assertEqual(result.status, "ABORTED_REVOKED")
        self.assertEqual(ep.store.read(WORKER_ACTOR)["outbox"], {})
        self.assertEqual(ep.store.read(WORKER_ACTOR)["terminals"], {})

    def test_actor_parent_child_lock_order_does_not_deadlock_revoke_waiter(self):
        ep = Episode()
        ep.seed_base()
        ep.seed_consume()
        entered = threading.Event()
        release = threading.Event()
        revoked = threading.Event()

        original_apply = ep.store.apply

        def paused_apply(actor_ref, transition, *, owner_fence_factory=None):
            def before():
                entered.set()
                self.assertTrue(release.wait(5), "release timeout")
            prior = ep.store.before_commit
            ep.store.before_commit = before
            try:
                return original_apply(
                    actor_ref, transition, owner_fence_factory=owner_fence_factory
                )
            finally:
                ep.store.before_commit = prior

        ep.store.apply = paused_apply

        def revoke():
            self.assertTrue(entered.wait(5), "transition did not reach barrier")
            # Same owner ordering as candidate: parent first, child second.
            with ep.pm_state.lock:
                with ep.evidence.child_lock:
                    ep.pm_state.delegation["current"] = False
                    ep.pm_state.delegation["revoked"] = True
                    ep.evidence.rows[("OWNER_BINDING", ep.binding_ref)]["current"] = False
                    ep.evidence.rows[("OWNER_BINDING", ep.binding_ref)]["revoked"] = True
            revoked.set()

        transition_result = {}
        error = {}

        def consume():
            try:
                transition_result["value"] = ep.nal.worker_consume(
                    WORKER_ACTOR, ep.consume_ref
                )
            except Exception as exc:  # pragma: no cover - diagnostic capture
                error["value"] = exc

        t1 = threading.Thread(target=consume)
        t2 = threading.Thread(target=revoke)
        t1.start()
        t2.start()
        self.assertTrue(entered.wait(5))
        self.assertFalse(revoked.wait(0.05), "revoke bypassed held owner fences")
        release.set()
        t1.join(5)
        t2.join(5)
        self.assertFalse(t1.is_alive() or t2.is_alive(), "lock-order deadlock")
        self.assertNotIn("value", error)
        self.assertEqual(transition_result["value"].status, "WORK_CONSUMED")
        self.assertTrue(revoked.is_set())


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(
        NalAuthorityRevocationProof
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(
        "nal_authority_revocation_validation: "
        f"{result.testsRun - len(result.failures) - len(result.errors)}/"
        f"{result.testsRun} PASS"
    )
    raise SystemExit(0 if result.wasSuccessful() else 1)
