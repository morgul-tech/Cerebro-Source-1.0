"""The controlled executor and the ambiguity model: T2 (execution half), T3, T9, T10, T11 + necessary extras."""
import dataclasses
import threading
import unittest

from _world import ART_NEW, TARGET, World, default_spec
from controlled_effect_executor import (COMMITTED_READBACK, DENIED, FENCED, IN_FLIGHT, NO_COMMIT, UNKNOWN_EFFECT,
                                        ControlledEffectExecutor, LedgerBasisError, ProviderAck, verify_provenance)


class HappyPathAndFence(unittest.TestCase):
    def setUp(self):
        self.w = World()

    def test_T2_fenced_exact_batch_runs_at_most_once_even_after_a_later_revoke(self):
        spec, receipt = self.w.fence()
        self.w.owner.revoke("SYNTH-DELEG-1")  # fence-before-revoke: the admitted exact batch may still proceed ...
        first = self.w.executor.execute(receipt, spec)
        self.assertEqual((first.state, first.attempts_started), (COMMITTED_READBACK, 1))
        self.assertEqual(self.w.provider.apply_call_count, 1)  # ... exactly once
        for _ in range(3):
            again = self.w.executor.execute(receipt, spec)
            self.assertEqual((again.state, again.replayed, again.attempts_started), (COMMITTED_READBACK, True, 1))
            self.assertEqual(again.reason_code, "NOT_FENCED_NO_NEW_ATTEMPT")
        self.assertEqual((self.w.provider.apply_call_count, self.w.provider.mutation_count), (1, 1))
        snap = self.w.provider.snapshot(TARGET)
        self.assertEqual((snap["version"], snap["artifact_version"], snap["applied"], snap["ops"]),
                         ("4", ART_NEW, (receipt.admission_ref,), ("SYNTH-OP-SET", "SYNTH-OP-TAG")))

    def test_T3_exact_replay_gives_the_same_result_identity_and_no_second_provider_call(self):
        spec, receipt, run = self.w.run()
        replay_admission = self.w.admit(spec)
        self.assertEqual((replay_admission.receipt, replay_admission.state), (receipt, COMMITTED_READBACK))
        again = self.w.executor.execute(replay_admission.receipt, spec)
        self.assertEqual((again.admission_ref, again.attempt_ref, again.batch_digest),
                         (run.admission_ref, run.attempt_ref, run.batch_digest))
        self.assertEqual(self.w.provider.apply_call_count, 1)

    def test_committed_state_carries_the_original_admission_and_attempt_identity(self):
        spec, receipt, run = self.w.run()
        self.assertEqual((run.admission_ref, run.batch_digest), (receipt.admission_ref, spec.digest))
        self.assertTrue(run.attempt_ref.startswith("ATT-"))
        self.assertFalse(run.automatic_retry_allowed)
        self.assertFalse(run.owner_action_required)

    def test_an_admission_past_its_bound_expiry_is_not_started(self):
        spec, receipt = self.w.fence()
        self.w.clock.advance(3600)  # delegation expiry (bound into the batch) passes before the start
        res = self.w.executor.execute(receipt, spec)
        self.assertEqual((res.state, res.reason_code, self.w.provider.apply_call_count),
                         (DENIED, "ADMISSION_EXPIRED_BEFORE_START", 0))
        self.assertEqual(self.w.store.state_of(receipt.admission_ref), FENCED)  # not consumed, not started

    def test_forged_or_foreign_receipts_never_reach_the_provider(self):
        spec, receipt = self.w.fence()
        tampered = dataclasses.replace(receipt, actor_generation=99)  # fingerprint no longer matches
        self.assertEqual(self.w.executor.execute(tampered, spec).reason_code, "RECEIPT_FORGED_OR_MALFORMED")
        resealed = type(receipt).seal(**{k: v for k, v in dataclasses.asdict(receipt).items()
                                          if k not in ("admission_ref", "receipt_fingerprint")} | {"actor_generation": 99})
        self.assertTrue(resealed.verify())  # internally consistent, but not an admission the fence ever minted
        self.assertEqual(self.w.executor.execute(resealed, spec).reason_code, "ADMISSION_NOT_FOUND")
        wrong_spec = default_spec(artifact_version="SYNTH-ARTIFACT-V3")
        self.assertEqual(self.w.executor.execute(receipt, wrong_spec).reason_code, "RECEIPT_SPEC_MISMATCH")
        self.assertEqual(self.w.executor.execute("ADM-X", spec).reason_code, "RECEIPT_FORGED_OR_MALFORMED")
        self.assertEqual(self.w.provider.apply_call_count, 0)

    def test_concurrent_execute_of_one_admission_calls_the_provider_once(self):
        spec, receipt = self.w.fence()
        results, barrier = [], threading.Barrier(8)

        def go():
            barrier.wait()
            results.append(self.w.executor.execute(receipt, spec))

        ts = [threading.Thread(target=go) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(self.w.provider.apply_call_count, 1)
        self.assertEqual(self.w.provider.mutation_count, 1)
        self.assertEqual({r.state for r in results} - {COMMITTED_READBACK, IN_FLIGHT}, set())


class Ambiguity(unittest.TestCase):
    def setUp(self):
        self.w = World()

    def lost_response(self, mode="commit_lose_response"):
        self.w.provider.apply_script[:] = [mode]
        return self.w.run()

    def test_T9_response_lost_after_commit_is_unknown_then_readback_resolves_without_a_second_mutation(self):
        spec, receipt, run = self.lost_response("commit_lose_response")
        self.assertEqual((run.state, run.reason_code, run.owner_action_required), (UNKNOWN_EFFECT,
                         "PROVIDER_RESPONSE_NOT_RECEIVED", True))
        self.assertEqual(self.w.provider.mutation_count, 1)  # the synthetic state DID change
        for _ in range(3):  # automatic retry is denied: execute never calls the provider again
            retry = self.w.executor.execute(receipt, spec)
            self.assertEqual((retry.state, retry.automatic_retry_allowed), (UNKNOWN_EFFECT, False))
        self.assertEqual(self.w.provider.apply_call_count, 1)
        res = self.w.executor.reconcile(receipt.admission_ref)
        self.assertEqual((res.state, res.reason_code), (COMMITTED_READBACK, "READBACK_MATCHES_INTENDED_STATE"))
        self.assertEqual((res.admission_ref, res.attempt_ref), (run.admission_ref, run.attempt_ref))  # original identity
        self.assertEqual((self.w.provider.apply_call_count, self.w.provider.mutation_count), (1, 1))

    def test_T10_response_lost_with_proven_no_commit_returns_to_the_owner_without_a_retry_policy(self):
        spec, receipt, run = self.lost_response("fail_before_commit")
        self.assertEqual(run.state, UNKNOWN_EFFECT)
        self.assertEqual(self.w.provider.mutation_count, 0)
        res = self.w.executor.reconcile(receipt.admission_ref)
        self.assertEqual((res.state, res.reason_code, res.owner_action_required, res.automatic_retry_allowed),
                         (NO_COMMIT, "READBACK_PROVES_NO_COMMIT", True, False))
        # NO_COMMIT is terminal for this admission: no state, no method, and no replay re-runs it
        for _ in range(3):
            self.assertEqual(self.w.executor.execute(receipt, spec).state, NO_COMMIT)
        self.assertEqual(self.w.admit(spec).state, NO_COMMIT)  # exact replay of the key returns the result, no call
        self.assertEqual(self.w.provider.apply_call_count, 1)
        # a retry is a NEW batch with a NEW idempotency key that the owner must admit afresh
        new = default_spec(idempotency_key="SYNTH-IDEM-RETRY-BY-OWNER")
        self.w.approve(new)
        self.assertNotEqual(self.w.admit(new).receipt.admission_ref, receipt.admission_ref)

    def test_T11_unresolved_readback_keeps_unknown_effect_and_never_retries(self):
        spec, receipt, run = self.lost_response("commit_lose_response")
        for bad in ("unavailable", "non_authoritative", "wrong_target"):
            with self.subTest(readback=bad):
                self.w.provider.readback_script[:] = [bad]
                res = self.w.executor.reconcile(receipt.admission_ref)
                self.assertEqual((res.state, res.owner_action_required), (UNKNOWN_EFFECT, True))
                self.assertEqual(self.w.executor.execute(receipt, spec).state, UNKNOWN_EFFECT)
        self.assertEqual(self.w.provider.apply_call_count, 1)
        self.assertEqual(self.w.provider.mutation_count, 1)
        # and a later authoritative readback still resolves it
        self.assertEqual(self.w.executor.reconcile(receipt.admission_ref).state, COMMITTED_READBACK)

    def test_no_commit_cannot_be_claimed_from_an_incomplete_history(self):
        spec, receipt, run = self.lost_response("fail_before_commit")  # nothing was committed ...
        self.w.provider.readback_script[:] = ["history_incomplete"]  # ... but the provider cannot prove its history
        res = self.w.executor.reconcile(receipt.admission_ref)
        self.assertEqual((res.state, res.reason_code), (UNKNOWN_EFFECT, "READBACK_HISTORY_INCOMPLETE"))
        self.assertEqual(self.w.provider.apply_call_count, 1)

    def test_provider_precondition_rejection_is_also_treated_as_no_response_not_as_proof(self):
        self.w.provider._state[TARGET]["version"] = "9"  # target moved: precondition "3" no longer holds
        spec, receipt, run = self.w.run()
        self.assertEqual(run.state, UNKNOWN_EFFECT)
        self.assertEqual(self.w.executor.reconcile(receipt.admission_ref).state, NO_COMMIT)
        self.assertEqual(self.w.provider.mutation_count, 0)

    def test_an_ack_is_not_proof_when_readback_is_unavailable(self):
        self.w.provider.readback_script[:] = ["unavailable"]
        spec, receipt, run = self.w.run()
        self.assertEqual((run.state, run.reason_code), (UNKNOWN_EFFECT, "READBACK_MALFORMED"))
        self.assertEqual(self.w.executor.reconcile(receipt.admission_ref).state, COMMITTED_READBACK)
        self.assertEqual(self.w.provider.apply_call_count, 1)

    def test_an_ack_contradicted_by_complete_readback_is_ambiguous_not_no_commit(self):
        self.w.provider.apply_script[:] = ["ack_without_commit"]
        spec, receipt, run = self.w.run()
        self.assertEqual((run.state, run.reason_code), (UNKNOWN_EFFECT, "ACK_CONTRADICTED_BY_READBACK"))
        self.assertEqual(self.w.executor.reconcile(receipt.admission_ref).state, UNKNOWN_EFFECT)  # stays HOLD
        self.assertEqual(self.w.provider.mutation_count, 0)

    def test_applied_correlation_with_a_different_artifact_version_is_not_a_match(self):
        self.w.provider.apply_script[:] = ["commit_lose_response"]
        spec, receipt, run = self.w.run()
        self.w.provider._state[TARGET]["artifact_version"] = "SYNTH-ARTIFACT-SOMETHING-ELSE"
        res = self.w.executor.reconcile(receipt.admission_ref)
        self.assertEqual((res.state, res.reason_code), (UNKNOWN_EFFECT, "READBACK_APPLIED_BUT_ARTIFACT_MISMATCH"))

    def test_the_attempt_compare_and_set_itself_admits_only_one_start(self):
        spec, receipt = self.w.fence()
        key = self.w.executor._key
        first = self.w.store.begin_attempt(receipt.admission_ref, spec.digest, writer_key=key)
        self.assertTrue(first.startswith("ATT-"))
        self.assertIsNone(self.w.store.begin_attempt(receipt.admission_ref, spec.digest, writer_key=key))
        self.assertEqual(self.w.store.attempts_started(receipt.admission_ref), 1)

    def test_a_malformed_ack_is_unknown_effect(self):
        self.w.provider.apply_script[:] = ["commit_bad_ack"]
        spec, receipt, run = self.w.run()
        self.assertEqual((run.state, run.reason_code), (UNKNOWN_EFFECT, "PROVIDER_ACK_MALFORMED"))
        self.assertEqual(self.w.executor.reconcile(receipt.admission_ref).state, COMMITTED_READBACK)

    def test_an_interrupted_attempt_stays_in_flight_and_is_never_called_twice(self):
        class Crash(BaseException):
            pass

        spec, receipt = self.w.fence()
        real = self.w.provider.adapter.apply

        def crash(grant):
            real(grant)  # the provider committed ...
            raise Crash()  # ... and the process died before any result was recorded

        self.w.executor._provider = type("P", (), {"apply": staticmethod(crash)})()
        with self.assertRaises(Crash):
            self.w.executor.execute(receipt, spec)
        self.assertEqual(self.w.store.state_of(receipt.admission_ref), IN_FLIGHT)
        again = self.w.executor.execute(receipt, spec)
        self.assertEqual((again.state, self.w.provider.apply_call_count), (IN_FLIGHT, 1))
        self.assertEqual(self.w.executor.reconcile(receipt.admission_ref).reason_code, "RECONCILE_NOT_APPLICABLE")
        # no fixed in-flight deadline exists; the OWNER decides when an attempt is declared lost
        lost = self.w.executor.declare_in_flight_lost(receipt.admission_ref, owner_reason_ref="SYNTH-OWNER-DECISION-1")
        self.assertEqual(lost.state, UNKNOWN_EFFECT)
        self.assertEqual(self.w.executor.reconcile(receipt.admission_ref).state, COMMITTED_READBACK)
        self.assertEqual(self.w.provider.apply_call_count, 1)

    def test_a_call_still_running_in_this_executor_cannot_be_declared_lost(self):
        spec, receipt = self.w.fence()
        real, seen = self.w.provider.adapter, {}

        class Hook:
            def apply(inner, grant):
                seen["during"] = self.w.executor.declare_in_flight_lost(receipt.admission_ref,
                                                                       owner_reason_ref="SYNTH-OWNER-EARLY")
                seen["state_during"] = self.w.store.state_of(receipt.admission_ref)
                return real.apply(grant)

        self.w.executor._provider = Hook()
        final = self.w.executor.execute(receipt, spec)
        self.assertEqual((seen["during"].state, seen["during"].reason_code, seen["state_during"]),
                         (IN_FLIGHT, "DECLARE_LOST_REFUSED_CALL_STILL_ACTIVE", IN_FLIGHT))
        self.assertEqual((final.state, self.w.provider.apply_call_count), (COMMITTED_READBACK, 1))

    def test_a_reconcile_racing_inside_execute_never_raises_and_never_recalls_the_provider(self):
        spec, receipt = self.w.fence()
        ex, real = self.w.executor, self.w.provider.readback
        seen = {}

        class Racing:
            def read_target_state(inner, target):
                if "inner" not in seen:
                    seen["inner"] = ex.reconcile(receipt.admission_ref)  # another caller resolves first
                return real.read_target_state(target)

        ex._readback = Racing()
        outer = ex.execute(receipt, spec)  # must return a result, not raise LedgerBasisError
        self.assertEqual(seen["inner"].state, COMMITTED_READBACK)
        self.assertEqual((outer.state, outer.reason_code), (COMMITTED_READBACK, "CONCURRENT_RESOLUTION_ALREADY_RECORDED"))
        self.assertEqual((self.w.provider.apply_call_count, self.w.provider.mutation_count), (1, 1))
        self.assertEqual(verify_provenance(self.w.store.provenance(receipt.admission_ref)), ())

    def test_declare_lost_only_applies_to_in_flight(self):
        spec, receipt = self.w.fence()
        res = self.w.executor.declare_in_flight_lost(receipt.admission_ref, owner_reason_ref="SYNTH-OWNER-DECISION-1")
        self.assertEqual((res.state, res.reason_code), (FENCED, "DECLARE_LOST_NOT_APPLICABLE"))

    def test_every_ambiguity_path_leaves_an_intact_provenance_chain(self):
        for mode in ("commit_ok", "commit_lose_response", "fail_before_commit"):
            with self.subTest(mode=mode):
                w = World()
                w.provider.apply_script[:] = [mode]
                spec, receipt, run = w.run()
                w.executor.reconcile(receipt.admission_ref)
                self.assertEqual(verify_provenance(w.store.provenance(receipt.admission_ref)), ())


if __name__ == "__main__":
    unittest.main()
