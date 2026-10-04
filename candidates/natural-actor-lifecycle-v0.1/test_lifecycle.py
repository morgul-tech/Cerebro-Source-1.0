"""NAL-01 local falsifiers. In-memory synthetic evidence only; no provider/PG/live call."""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
import pathlib
import sys
import threading
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "signalvev-sensing-runtime-v0.1" / "src"))
from lifecycle import (BINDING_SCHEMA, TERMINAL_SCHEMA, Hold, Lifecycle, MemoryStore,
                       UnavailableEvidenceReader, digest)
from signalvev_sensing.owner_event import accept_owner_event
from signalvev_sensing.d0 import build_frame


class FixtureEvidence:
    def __init__(self):
        self.rows = {}
        self._owner_lock = threading.RLock()

    def seed(self, kind, ref, **fields):
        row = {"evidence_ref": ref, "provider_revision": "fixture-provider-r1", **fields}
        with self._owner_lock:
            self.rows[(kind, ref)] = row
        return row

    def read(self, kind, ref):
        with self._owner_lock:
            return copy.deepcopy(self.rows.get((kind, ref)))

    def revoke(self, binding_ref):
        with self._owner_lock:
            self.rows[("OWNER_BINDING", binding_ref)]["revoked"] = True

    def owner_commit_fence(self, binding_ref, *, cursor=None):
        return FixtureOwnerFence(self, binding_ref)


class FixtureOwnerFence:
    """Synthetic owner-side serialization, not a production authority adapter."""

    def __init__(self, evidence, binding_ref):
        self.evidence, self.binding_ref = evidence, binding_ref
        self.entered = False

    def __enter__(self):
        self.evidence._owner_lock.acquire()
        self.entered = True
        return self

    def __exit__(self, *_):
        self.entered = False
        self.evidence._owner_lock.release()

    def assert_current(self, expected):
        if not self.entered:
            raise Hold("OWNER_COMMIT_FENCE_UNAVAILABLE")
        row = self.evidence.rows.get(("OWNER_BINDING", self.binding_ref))
        if row != expected or row.get("current") is not True or row.get("revoked") is not False:
            raise Hold("OWNER_BINDING_CHANGED_BEFORE_COMMIT")


class Episode(unittest.TestCase):
    def setUp(self):
        self.evidence = FixtureEvidence()
        self.store = MemoryStore()
        self.nal = Lifecycle(self.store, self.evidence, enabled=True)
        self.actor, self.generation = "actor-A", "gen-1"

    def assert_hold(self, code, fn):
        with self.assertRaises(Hold) as caught:
            fn()
        self.assertEqual(caught.exception.code, code)

    def ready(self, birth_role=None, claim_ref=None):
        self.evidence.seed("ROLE_READY", "ready-1", actor_ref=self.actor,
                           generation_ref=self.generation, pre_role_lifecycle="READY_UNBOUND",
                           birth_role=birth_role, attached_role="WORKER", claim_ref=claim_ref,
                           source_revision="source-1", role_projection_fingerprint="profile-1",
                           role_attach_receipt="attach-1")
        return self.nal.register_worker_ready(self.actor, self.generation, "ready-1")

    def task(self, suffix="1"):
        ref = "admitted-" + suffix
        self.evidence.seed("ADMITTED_TASK", ref, actor_ref=self.actor,
                           generation_ref=self.generation, actor_role="WORKER",
                           source_revision="source-1", role_projection_fingerprint="profile-1",
                           task_ref="task-" + suffix, attempt_ref="attempt-" + suffix,
                           claim_ref="claim-" + suffix, packet_ref="packet-" + suffix,
                           queue_ref="queue-" + suffix, task_payload_sha256=digest(["task", suffix]),
                           admission_receipt="owner-task-receipt-" + suffix,
                           owner_revision="owner-task-r" + suffix, admitted=True)
        return self.nal.admit_task(self.actor, ref)

    def binding(self, suffix="1", *, binding_ref=None, supersedes=None, role="WORKER",
                current=True, revoked=False):
        ref = binding_ref or "binding-" + suffix
        task = self.store.read(self.actor)["current_task"]
        core = {"schema": BINDING_SCHEMA, "binding_ref": ref,
                "semantic_ref": "semantic:fixture:" + suffix, "actor_ref": self.actor,
                "generation_ref": self.generation, "task_ref": task["task_ref"],
                "attempt_ref": task["attempt_ref"], "claim_ref": task["claim_ref"],
                "packet_ref": task["packet_ref"], "queue_ref": task["queue_ref"],
                "actor_role": role, "source_revision": "source-1",
                "role_projection_fingerprint": "profile-1",
                "task_payload_sha256": task["task_payload_sha256"],
                "owner_revision": "owner-binding-r" + ref,
                "provider_revision": "fixture-provider-r1",
                "admission_receipt": task["admission_receipt"], "supersedes": supersedes}
        self.evidence.seed("OWNER_BINDING", ref, **core, current=current, revoked=revoked,
                           canonical_fingerprint=digest(core), issue_receipt="owner-binding-receipt-" + ref)
        return ref

    def worker(self, kind, ref, **extra):
        task = self.store.read(self.actor)["current_task"]
        fields = {"actor_ref": self.actor, "generation_ref": self.generation,
                  "attempt_ref": task["attempt_ref"], "claim_ref": task["claim_ref"],
                  "packet_ref": task["packet_ref"], "queue_ref": task["queue_ref"],
                  "binding_ref": task["binding_ref"], "receipt_ref": "worker-receipt-" + ref}
        self.evidence.seed(kind, ref, **fields, **extra)
        return ref

    def prepared(self, suffix="1"):
        if self.store.read(self.actor) is None:
            self.ready()
        self.task(suffix)
        ref = self.binding(suffix)
        self.nal.issue_binding(self.actor, ref)
        return ref

    def started(self, suffix="1"):
        ref = self.prepared(suffix)
        self.nal.worker_consume(self.actor, self.worker("WORKER_CONSUME", "consume-" + suffix))
        self.nal.worker_start(self.actor, self.worker("WORKER_START", "start-" + suffix))
        return ref

    def terminal(self, suffix="1", terminal_ref=None, result="PASS", terminal_digest=None):
        ref = terminal_ref or "terminal-" + suffix
        evref = "terminal-evidence-" + suffix
        self.worker("WORKER_TERMINAL", evref, schema=TERMINAL_SCHEMA, terminal_ref=ref,
                    terminal_digest=terminal_digest or digest(["terminal", suffix]), result=result)
        return self.nal.worker_terminal(self.actor, evref)

    def admission(self, suffix="1", terminal_ref=None, **changes):
        ref = terminal_ref or "terminal-" + suffix
        terminal = self.store.read(self.actor)["terminals"][ref]
        fields = {"actor_ref": self.actor, "generation_ref": self.generation,
                  "terminal_ref": ref, "terminal_digest": terminal["terminal_digest"],
                  "binding_ref": terminal["binding_ref"], "claim_ref": terminal["claim_ref"],
                  "packet_ref": terminal["packet_ref"], "queue_ref": terminal["queue_ref"],
                  "result": terminal["result"], "admitted": True}
        fields.update(changes)
        e_ref = "pm-admission-" + suffix + "-" + str(len(self.evidence.rows))
        self.evidence.seed("PM_ADMISSION", e_ref, **fields)
        return e_ref

    def closed(self, suffix="1", result="PASS"):
        self.started(suffix)
        self.terminal(suffix, result=result)
        admission_ref = self.admission(suffix)
        return self.nal.admit_release_once(self.actor, "terminal-" + suffix, admission_ref)

    def test_default_off_and_missing_trusted_reader(self):
        self.assert_hold("DEFAULT_OFF", lambda: Lifecycle(self.store).register_worker_ready(
            self.actor, self.generation, "ready-1"))
        self.assert_hold("TRUSTED_EVIDENCE_READ_UNAVAILABLE", lambda: Lifecycle(
            self.store, UnavailableEvidenceReader(), enabled=True).register_worker_ready(
                self.actor, self.generation, "ready-1"))

    def test_role_null_zero_claim_birth_and_task_semantics_order(self):
        self.assert_hold("ROLE_NULL_BIRTH_OR_ZERO_CLAIM_READBACK_REQUIRED",
                         lambda: self.ready(birth_role="WORKER"))
        self.assert_hold("ROLE_NULL_BIRTH_OR_ZERO_CLAIM_READBACK_REQUIRED",
                         lambda: self.ready(claim_ref="premature"))
        self.assertEqual(self.ready().status, "WORKER_READY_ZERO_CLAIM")
        self.assertIsNone(self.store.read(self.actor)["current_task"])
        self.task()
        self.assert_hold("CONSUME_ORDER_OR_BINDING_REQUIRED", lambda: self.nal.worker_consume(
            self.actor, self.worker("WORKER_CONSUME", "too-early")))
        ref = self.binding()
        self.nal.issue_binding(self.actor, ref)
        self.assert_hold("START_REQUIRES_CONSUME", lambda: self.nal.worker_start(
            self.actor, self.worker("WORKER_START", "start-before-consume")))

    def test_local_episode_receipt_outbox_and_replay(self):
        result = self.closed()
        self.assertEqual(result.status, "TASK_CLOSED_RECEIPTED_OUTBOXED")
        state = self.store.read(self.actor)
        self.assertEqual(state["current_task"]["status"], "CLOSED")
        self.assertFalse(state["reuse_ready"])
        self.assertEqual(len(state["receipts"]), 1)
        pending = self.nal.pending_outbox(self.actor)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["receipt_ref"], result.receipt["receipt_ref"])
        self.assertEqual(pending[0]["schema"], "cerebro.nal.owner-event-intent/v1")
        projected = self.nal.project_owner_event(self.actor, pending[0]["event_ref"],
                                                 observed_at="2026-10-03T12:00:00Z")
        parsed = accept_owner_event(projected)
        self.assertEqual(parsed.commit_readback_ref, result.receipt["receipt_ref"])
        self.assertEqual(parsed.event_id, pending[0]["event_ref"])
        frame = build_frame(parsed, now_epoch=1_791_025_200.0)
        self.assertEqual(frame.d0["event_id"], pending[0]["event_ref"])
        replay = self.nal.admit_release_once(self.actor, "terminal-1", self.admission())
        self.assertEqual(replay.status, "REPLAY_SAME_RECEIPT")
        self.assertEqual(replay.receipt, result.receipt)
        self.assertEqual(len(self.nal.pending_outbox(self.actor)), 1)
        self.assertEqual(self.nal.admit_task(self.actor, "admitted-1").status, "REPLAY_TASK_ADMISSION")
        self.assertEqual(self.nal.issue_binding(self.actor, "binding-1").status, "REPLAY_BINDING_ISSUE")
        self.assertEqual(self.nal.worker_consume(self.actor, "consume-1").status, "REPLAY_CONSUME")
        self.assertEqual(self.nal.worker_start(self.actor, "start-1").status, "REPLAY_START")

    def test_same_terminal_identity_replay_and_refine_closure(self):
        self.started()
        self.terminal(result="REFINE")
        task = self.store.read(self.actor)["current_task"]
        self.worker("WORKER_TERMINAL", "second-provider-readback", schema=TERMINAL_SCHEMA,
                    terminal_ref="terminal-1", terminal_digest=digest(["terminal", "1"]),
                    result="REFINE")
        self.assertEqual(self.nal.worker_terminal(self.actor, "second-provider-readback").status,
                         "REPLAY_TERMINAL")
        closed = self.nal.admit_release_once(self.actor, "terminal-1", self.admission(result="REFINE"))
        self.assertEqual(closed.receipt["task_result"], "REFINE")
        self.assertEqual(task["claim_ref"], closed.receipt["claim_ref"])
        self.assertFalse(self.store.read(self.actor)["reuse_ready"])

    def test_wrong_role_stale_revoked_and_superseded_binding(self):
        self.ready()
        self.task()
        wrong = self.binding(binding_ref="wrong", role="RESEARCHER")
        self.assert_hold("OWNER_BINDING_STALE_WRONG_ROLE_OR_FINGERPRINT",
                         lambda: self.nal.issue_binding(self.actor, wrong))
        stale = self.binding(binding_ref="stale")
        self.evidence.rows[("OWNER_BINDING", stale)]["canonical_fingerprint"] = "0" * 64
        self.assert_hold("OWNER_BINDING_STALE_WRONG_ROLE_OR_FINGERPRINT",
                         lambda: self.nal.issue_binding(self.actor, stale))
        revoked = self.binding(binding_ref="revoked", revoked=True)
        self.assert_hold("OWNER_BINDING_READBACK_REQUIRED",
                         lambda: self.nal.issue_binding(self.actor, revoked))
        first = self.binding(binding_ref="first")
        self.nal.issue_binding(self.actor, first)
        second = self.binding(binding_ref="second", supersedes=first)
        self.nal.supersede_binding(self.actor, second)
        self.assertEqual(self.nal.supersede_binding(self.actor, second).status,
                         "REPLAY_BINDING_SUPERSESSION")
        self.assertEqual(self.store.read(self.actor)["current_task"]["binding_ref"], second)
        self.assertEqual(self.store.read(self.actor)["supersessions"][first]["new_binding_ref"], second)
        self.evidence.rows[("OWNER_BINDING", second)]["revoked"] = True
        self.assert_hold("OWNER_BINDING_READBACK_REQUIRED", lambda: self.nal.worker_consume(
            self.actor, self.worker("WORKER_CONSUME", "consume-revoked")))

    def test_send_is_not_consume_start_or_terminal(self):
        self.prepared()
        self.assert_hold("START_REQUIRES_CONSUME", lambda: self.nal.worker_start(
            self.actor, self.worker("WORKER_START", "sent-only")))
        self.assert_hold("TERMINAL_REQUIRES_START", lambda: self.terminal())
        self.assertEqual(self.store.read(self.actor)["current_task"]["status"], "ADMITTED")

    def test_changed_terminal_binding_and_old_t_cannot_second_effect_f2(self):
        first = self.closed().receipt
        self.assert_hold("CONFLICT_TERMINAL_ADMISSION_BINDING", lambda: self.nal.admit_release_once(
            self.actor, "terminal-1", self.admission(binding_ref="other-binding")))
        self.evidence.seed("RESET_READBACK", "reset-1", actor_ref=self.actor,
                           generation_ref=self.generation, terminal_ref="terminal-1",
                           closed_capabilities=True, next_epoch=1)
        self.nal.reset_for_reuse(self.actor, "reset-1")
        self.started("2")
        # Old T is globally first-use bound, independent of the current F2.
        replay = self.nal.admit_release_once(self.actor, "terminal-1", self.admission(
            suffix="1", terminal_ref="terminal-1"))
        self.assertEqual(replay.receipt, first)
        self.assertEqual(self.store.read(self.actor)["current_task"]["status"], "STARTED")
        self.assert_hold("CONFLICT_TERMINAL_OCCURRENCE", lambda: self.terminal(
            suffix="2", terminal_ref="terminal-1", terminal_digest=digest("changed")))
        self.assertEqual(len(self.store.read(self.actor)["receipts"]), 1)

    def test_changed_digest_binder_claim_packet_queue_refused_before_close(self):
        self.started()
        self.terminal()
        for change in ({"terminal_digest": digest("other")}, {"binding_ref": "other"},
                       {"claim_ref": "other"}, {"packet_ref": "other"}, {"queue_ref": "other"}):
            with self.subTest(change=change):
                ref = self.admission(**change)
                self.assert_hold("CONFLICT_TERMINAL_ADMISSION_BINDING",
                                 lambda: self.nal.admit_release_once(self.actor, "terminal-1", ref))
                self.assertEqual(len(self.store.read(self.actor)["receipts"]), 0)

    def test_competing_close_only_one_commit(self):
        self.started()
        self.terminal()
        a, b = self.admission(), self.admission()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda ref: self.nal.admit_release_once(
                self.actor, "terminal-1", ref), (a, b)))
        self.assertEqual(sorted(x.status for x in results),
                         ["REPLAY_SAME_RECEIPT", "TASK_CLOSED_RECEIPTED_OUTBOXED"])
        self.assertEqual(len(self.store.read(self.actor)["receipts"]), 1)

    def test_revoke_first_and_interleaved_revoke_zero_effect(self):
        binding_ref = self.started()
        self.terminal()
        admission_ref = self.admission()
        self.evidence.revoke(binding_ref)
        self.assert_hold("OWNER_BINDING_CHANGED_BEFORE_COMMIT", lambda: self.nal.admit_release_once(
            self.actor, "terminal-1", admission_ref))
        state = self.store.read(self.actor)
        self.assertEqual(state["current_task"]["status"], "TERMINAL")
        self.assertEqual((len(state["receipts"]), len(state["outbox"])), (0, 0))

        # The frozen O7 shape mutates the fixture at MemoryStore.before_commit.
        # It bypasses the fixture's lock, so the guarded commit assertion must
        # still reject that synthetic interleaving before any local effect.
        self.evidence.rows[("OWNER_BINDING", binding_ref)]["revoked"] = False
        self.store.before_commit = lambda: self.evidence.revoke(binding_ref)
        self.assert_hold("OWNER_BINDING_CHANGED_BEFORE_COMMIT", lambda: self.nal.admit_release_once(
            self.actor, "terminal-1", admission_ref))
        self.store.before_commit = None
        state = self.store.read(self.actor)
        self.assertEqual(state["current_task"]["status"], "TERMINAL")
        self.assertEqual((len(state["receipts"]), len(state["outbox"])), (0, 0))

    def test_close_first_then_revoke_keeps_one_effect_and_replay(self):
        binding_ref = self.started()
        self.terminal()
        admission_ref = self.admission()
        first = self.nal.admit_release_once(self.actor, "terminal-1", admission_ref)
        self.evidence.revoke(binding_ref)
        replay = self.nal.admit_release_once(self.actor, "terminal-1", admission_ref)
        self.assertEqual(replay.status, "REPLAY_SAME_RECEIPT")
        self.assertEqual(replay.receipt, first.receipt)
        state = self.store.read(self.actor)
        self.assertEqual((len(state["receipts"]), len(state["outbox"])), (1, 1))

    def test_owner_revoke_waits_for_inflight_close_commit(self):
        binding_ref = self.started()
        self.terminal()
        admission_ref = self.admission()
        at_commit, permit_commit = threading.Event(), threading.Event()
        revoking_started, revoked = threading.Event(), threading.Event()

        def pause_before_commit():
            at_commit.set()
            if not permit_commit.wait(2):
                raise RuntimeError("test commit permit timeout")

        def revoke():
            revoking_started.set()
            self.evidence.revoke(binding_ref)
            revoked.set()

        self.store.before_commit = pause_before_commit
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                closing = pool.submit(self.nal.admit_release_once, self.actor,
                                      "terminal-1", admission_ref)
                self.assertTrue(at_commit.wait(2))
                revoking = pool.submit(revoke)
                self.assertTrue(revoking_started.wait(2))
                self.assertFalse(revoked.wait(0.05))
                permit_commit.set()
                self.assertEqual(closing.result(timeout=2).status,
                                 "TASK_CLOSED_RECEIPTED_OUTBOXED")
                revoking.result(timeout=2)
        finally:
            permit_commit.set()
            self.store.before_commit = None
        self.assertTrue(revoked.is_set())
        state = self.store.read(self.actor)
        self.assertEqual((len(state["receipts"]), len(state["outbox"])), (1, 1))

    def test_missing_owner_fence_holds_without_effect(self):
        self.started()
        self.terminal()
        admission_ref = self.admission()

        class ReaderWithoutFence:
            def read(_, kind, ref):
                return self.evidence.read(kind, ref)

        nal = Lifecycle(self.store, ReaderWithoutFence(), enabled=True)
        self.assert_hold("OWNER_COMMIT_FENCE_UNAVAILABLE", lambda: nal.admit_release_once(
            self.actor, "terminal-1", admission_ref))
        state = self.store.read(self.actor)
        self.assertEqual((len(state["receipts"]), len(state["outbox"])), (0, 0))

    def test_crash_before_and_after_commit_recover_without_second_effect(self):
        self.started()
        self.terminal()
        ref = self.admission()
        self.store.before_commit = lambda: (_ for _ in ()).throw(RuntimeError("before-commit"))
        with self.assertRaises(RuntimeError):
            self.nal.admit_release_once(self.actor, "terminal-1", ref)
        self.assertEqual(len(self.store.read(self.actor)["receipts"]), 0)
        self.store.before_commit = None
        self.store.after_commit = lambda: (_ for _ in ()).throw(RuntimeError("after-commit"))
        with self.assertRaises(RuntimeError):
            self.nal.admit_release_once(self.actor, "terminal-1", ref)
        self.store.after_commit = None
        self.assertEqual(len(self.store.read(self.actor)["receipts"]), 1)
        self.assertEqual(self.nal.admit_release_once(self.actor, "terminal-1", ref).status,
                         "REPLAY_SAME_RECEIPT")
        self.assertEqual(len(self.nal.pending_outbox(self.actor)), 1)

    def test_outbox_recovery_and_closed_task_not_actor_reusable(self):
        self.closed()
        self.assert_hold("ACTOR_NOT_REUSABLE_OR_TASK_ACTIVE", lambda: self.task("2"))
        event = self.nal.pending_outbox(self.actor)[0]
        self.evidence.seed("OUTBOX_DELIVERY", "delivery-1", event_ref=event["event_ref"],
                           delivery_receipt="delivery-receipt-1")
        self.assertEqual(self.nal.acknowledge_outbox(self.actor, event["event_ref"], "delivery-1").status,
                         "OUTBOX_DELIVERED_READBACK")
        self.assertEqual(self.nal.pending_outbox(self.actor), [])
        self.evidence.seed("RESET_READBACK", "reset-bad", actor_ref=self.actor,
                           generation_ref=self.generation, terminal_ref="terminal-1",
                           closed_capabilities=False, next_epoch=1)
        self.assert_hold("RESET_READBACK_MISMATCH", lambda: self.nal.reset_for_reuse(
            self.actor, "reset-bad"))
        self.evidence.seed("RESET_READBACK", "reset-good", actor_ref=self.actor,
                           generation_ref=self.generation, terminal_ref="terminal-1",
                           closed_capabilities=True, next_epoch=1)
        self.assertEqual(self.nal.reset_for_reuse(self.actor, "reset-good").status,
                         "ACTOR_REUSABLE_AFTER_RESET")
        self.assertEqual(self.nal.reset_for_reuse(self.actor, "reset-good").status, "REPLAY_RESET")
        self.assert_hold("TASK_REF_ALREADY_CLOSED", lambda: self.task("1"))
        self.assertEqual(self.task("2").status, "TASK_ADMITTED")


if __name__ == "__main__":
    unittest.main()
