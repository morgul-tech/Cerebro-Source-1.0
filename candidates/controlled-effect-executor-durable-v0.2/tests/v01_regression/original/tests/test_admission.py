"""The atomic one-use admission fence: T1, T2(admission half), T3, T4-T8, T14 + narrowly necessary extras."""
import dataclasses
import threading
import unittest
from contextlib import contextmanager

from _world import ART_NEW, OPS, T0, World, default_spec
from controlled_effect_executor import (DENIED, FENCED, DelegationView, Operation, TargetScopeEntry)


class AdmissionDenials(unittest.TestCase):
    def setUp(self):
        self.w = World()

    def assertDenied(self, decision, reason):
        self.assertEqual((decision.state, decision.reason_code), (DENIED, reason))
        self.assertIsNone(decision.receipt)
        self.assertEqual(self.w.provider.apply_call_count, 0)
        self.assertEqual(self.w.store.denials()[-1][0], reason)
        self.assertEqual(len(self.w.store._records), 0)  # no admission exists, so there is nothing to execute

    def test_T1_revoke_before_fence_is_denied_with_zero_provider_calls(self):
        self.w.owner.revoke("SYNTH-DELEG-1")
        self.assertDenied(self.w.admit(), "DELEGATION_REVOKED")
        self.assertEqual(self.w.provider.mutation_count, 0)

    def test_T4_stale_delegation_revision(self):
        self.w.owner.amend("SYNTH-DELEG-1")  # owner is now revision 2; the caller still presents revision 1
        self.assertDenied(self.w.admit(), "STALE_DELEGATION_REVISION")

    def test_T4_future_revision_is_not_current_either(self):
        self.assertDenied(self.w.admit(default_spec(delegation_revision=9)), "DELEGATION_REVISION_MISMATCH")

    def test_T5_wrong_actor(self):
        spec = default_spec(actor_ref="SYNTH-ACTOR-2")
        self.assertDenied(self.w.admit(spec), "ACTOR_MISMATCH")

    def test_T5_wrong_actor_generation(self):
        spec = default_spec(actor_generation=3)
        self.assertDenied(self.w.admit(spec), "ACTOR_GENERATION_MISMATCH")
        self.w.owner.amend("SYNTH-DELEG-1", actor_generation=5)  # actor generation moved on at the owner
        self.assertDenied(self.w.admit(default_spec(delegation_revision=2)), "ACTOR_GENERATION_MISMATCH")

    def test_T6_wrong_operation_scope(self):
        spec = default_spec(operations=OPS + (Operation.of("SYNTH-OP-FORBIDDEN"),))
        self.w.approve(spec)
        self.assertDenied(self.w.admit(spec), "OPERATION_OUT_OF_SCOPE")

    def test_T6_wrong_target(self):
        spec = default_spec(target_identity="SYNTH-TARGET-B")
        self.w.approve(spec)
        self.assertDenied(self.w.admit(spec), "TARGET_OUT_OF_SCOPE")

    def test_T6_wrong_artifact_version(self):
        spec = default_spec(artifact_version="SYNTH-ARTIFACT-V9")
        self.w.approve(spec)
        self.assertDenied(self.w.admit(spec), "VERSION_OUT_OF_SCOPE")

    def test_T7_expired_by_owner_status(self):
        self.w.owner.expire("SYNTH-DELEG-1")
        self.assertDenied(self.w.admit(default_spec(delegation_revision=2)), "DELEGATION_EXPIRED")

    def test_T7_expired_by_clock(self):
        self.w.clock.advance(3600)
        self.assertDenied(self.w.admit(), "DELEGATION_EXPIRED")

    def test_owner_expiry_by_clock_denies_even_when_the_caller_binds_no_expiry(self):
        self.w.clock.advance(3600)  # the owner's view has expired by the clock; the caller binds no expiry at all
        self.assertDenied(self.w.admit(default_spec(delegation_expiry=None)), "DELEGATION_EXPIRED")

    def test_expiry_the_caller_binds_must_equal_the_owner_expiry(self):
        self.assertDenied(self.w.admit(default_spec(delegation_expiry=T0 + 7200)), "DELEGATION_EXPIRY_MISMATCH")
        self.assertDenied(self.w.admit(default_spec(delegation_expiry=None)), "DELEGATION_EXPIRY_MISMATCH")

    def test_T8_forged_or_mismatched_digest(self):
        spec = default_spec()
        other = default_spec(idempotency_key="SYNTH-IDEM-OTHER")
        for forged in (other.digest, "0" * 64, "", spec.digest.upper(), spec.operations_digest):
            with self.subTest(forged=forged[:12]):
                self.assertDenied(self.w.store.admit(spec, forged), "DIGEST_MISMATCH")

    def test_a_profile_style_hash_is_not_a_batch_identity(self):
        # X3 correction: no profile hash is a valid batch-admission hash by itself.
        self.assertDenied(self.w.store.admit(default_spec(), "18db16ddcd61f6202f8449193f1f7c7942d96f31391efabab8f8e72c9ad53aff"),
                          "DIGEST_MISMATCH")

    def test_unknown_delegation(self):
        spec = default_spec(delegation_ref="SYNTH-DELEG-404")
        self.w.approve(spec)
        self.assertDenied(self.w.admit(spec), "DELEGATION_NOT_FOUND")

    def test_human_approval_fixture_must_exist_be_approved_and_bind_the_exact_batch(self):
        spec = default_spec(human_approval_ref="SYNTH-APPROVAL-404")
        self.assertDenied(self.w.admit(spec), "APPROVAL_NOT_FOUND")
        self.w.owner.withdraw_approval("SYNTH-APPROVAL-1")
        self.assertDenied(self.w.admit(), "APPROVAL_NOT_ACTIVE")
        w = World()
        w.approve(default_spec(), operations_digest=default_spec(operations=OPS[:1]).operations_digest)
        self.assertEqual(w.admit().reason_code, "APPROVAL_BINDING_MISMATCH")
        w = World()
        w.approve(default_spec(), artifact_version="SYNTH-ARTIFACT-V9")
        self.assertEqual(w.admit().reason_code, "APPROVAL_BINDING_MISMATCH")

    def test_non_synthetic_authority_refs_are_refused_by_the_reference_store(self):
        for field in ("delegation_ref", "actor_ref", "human_approval_ref", "target_identity"):
            with self.subTest(field=field):
                spec = default_spec(**{field: "prod-" + field})
                self.assertDenied(self.w.admit(spec), "NON_SYNTHETIC_REF_REFUSED")

    def test_a_denied_request_creates_no_admission_and_consumes_no_key(self):
        self.w.owner.revoke("SYNTH-DELEG-1")
        self.assertEqual(self.w.admit().reason_code, "DELEGATION_REVOKED")
        self.assertIsNone(self.w.store.state_of("ADM-ANYTHING"))
        # the owner later issues a fresh ACTIVE revision; the SAME idempotency key is still free for a new spec
        self.w.owner.amend("SYNTH-DELEG-1", status="ACTIVE")
        decision = self.w.admit(default_spec(delegation_revision=3))
        self.assertEqual(decision.state, FENCED)


class AdmissionFence(unittest.TestCase):
    def setUp(self):
        self.w = World()

    def test_T2_fence_before_revoke_one_receipt_and_a_later_revoke_mints_nothing_new(self):
        spec, receipt = self.w.fence()
        self.w.owner.revoke("SYNTH-DELEG-1")
        self.assertEqual(self.w.store.state_of(receipt.admission_ref), FENCED)
        again = self.w.admit(spec)  # exact replay after revoke returns the SAME admission, never a second one
        self.assertEqual((again.state, again.replayed, again.receipt), (FENCED, True, receipt))
        # and every NEW admission is now denied
        new = default_spec(idempotency_key="SYNTH-IDEM-2", delegation_revision=3)
        self.w.approve(new)
        self.assertEqual(self.w.admit(new).reason_code, "DELEGATION_REVOKED")
        self.assertEqual(len(self.w.store._records), 1)

    def test_T3_exact_replay_returns_the_same_admission_identity(self):
        spec, receipt = self.w.fence()
        for _ in range(3):
            replay = self.w.admit(spec)
            self.assertEqual((replay.receipt, replay.replayed, replay.reason_code), (receipt, True, "EXACT_REPLAY"))
        self.assertEqual(len(self.w.store._records), 1)
        self.assertEqual(self.w.provider.apply_call_count, 0)

    def test_the_receipt_binds_delegation_batch_actor_and_owner_currentness(self):
        spec, r = self.w.fence()
        self.assertTrue(r.verify())
        self.assertEqual((r.batch_digest, r.delegation_ref, r.delegation_revision, r.actor_ref, r.actor_generation,
                          r.approval_ref, r.idempotency_key, r.target_identity, r.artifact_version),
                         (spec.digest, "SYNTH-DELEG-1", 1, "SYNTH-ACTOR-1", 4, "SYNTH-APPROVAL-1", "SYNTH-IDEM-1",
                          "SYNTH-TARGET-A", ART_NEW))
        self.assertGreater(r.delegation_currentness, 0)
        self.assertTrue(r.admission_ref.startswith("ADM-"))

    def test_same_idempotency_key_with_a_different_batch_is_denied_not_replayed(self):
        self.w.fence()
        other = default_spec(artifact_version="SYNTH-ARTIFACT-V3")
        decision = self.w.admit(other)
        self.assertEqual((decision.state, decision.reason_code), (DENIED, "IDEMPOTENCY_KEY_CONFLICT"))

    def test_T14_a_different_lawful_batch_needs_a_distinct_admission(self):
        spec_a, receipt_a = self.w.fence()
        spec_b = default_spec(idempotency_key="SYNTH-IDEM-B", operations=(OPS[0],))
        self.w.approve(spec_b)
        receipt_b = self.w.admit(spec_b).receipt
        self.assertNotEqual(receipt_a.admission_ref, receipt_b.admission_ref)
        self.assertNotEqual(receipt_a.batch_digest, receipt_b.batch_digest)
        # a completed batch cannot be replayed as the new one: A's receipt does not match B's spec, and vice versa
        run_a = self.w.executor.execute(receipt_a, spec_a)
        self.assertEqual(run_a.state, "COMMITTED_READBACK")
        wrong = self.w.executor.execute(receipt_a, spec_b)
        self.assertEqual((wrong.state, wrong.reason_code), (DENIED, "RECEIPT_SPEC_MISMATCH"))
        self.assertEqual(self.w.provider.apply_call_count, 1)

    def test_concurrent_identical_admissions_mint_exactly_one_receipt(self):
        spec = default_spec()
        decisions, barrier = [], threading.Barrier(8)

        def go():
            barrier.wait()
            decisions.append(self.w.store.admit(spec, spec.digest))

        threads = [threading.Thread(target=go) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual({d.receipt for d in decisions}, {decisions[0].receipt})
        self.assertEqual(sum(1 for d in decisions if not d.replayed), 1)
        self.assertEqual(len(self.w.store._records), 1)

    def test_a_revoke_arriving_between_the_owner_read_and_the_insert_is_ordered_after_the_fence(self):
        """Deterministic version of the race: a revoke is fired from INSIDE the owner section, after the delegation
        was read and before the admission is inserted. The section makes it wait; the fence is stamped with the owner
        currentness it read (nothing moved), and the revoke lands strictly after."""
        w, spec, box = World(), default_spec(), {}
        orig_atomic = w.owner.atomic_read

        @contextmanager
        def hooked():
            with orig_atomic() as snap:
                real = snap.read_approval

                def read_approval(ref):
                    approval = real(ref)
                    if "thread" not in box:
                        box["thread"] = threading.Thread(
                            target=lambda: box.__setitem__("seq", w.owner.revoke("SYNTH-DELEG-1")))
                        box["thread"].start()
                        box["thread"].join(0.3)  # it blocks on the owner section; times out while we still hold it
                    return approval

                snap.read_approval = read_approval
                yield snap

        w.owner.atomic_read = hooked
        decision = w.store.admit(spec, spec.digest)
        box["thread"].join()
        self.assertEqual(decision.state, FENCED, decision)
        self.assertEqual(decision.receipt.owner_currentness_at_fence, decision.receipt.delegation_currentness)
        self.assertLess(decision.receipt.owner_currentness_at_fence, box["seq"])
        self.assertEqual(w.owner.read_for_test("SYNTH-DELEG-1").status, "REVOKED")

    def test_an_owner_without_a_section_is_still_caught_by_the_currentness_compare_and_set(self):
        w, spec = World(), default_spec()
        owner = w.owner

        class Unlocked:  # an owner whose atomic_read gives NO exclusion: only the optimistic compare protects the fence
            moved = False

            def read_delegation(self, ref):
                view = owner._delegations.get(ref)
                return None if view is None else dataclasses.replace(view, owner_currentness=owner._seq)

            def read_approval(self, ref):
                approval = owner._approvals.get(ref)
                if not self.moved:
                    self.moved = True
                    owner.revoke("SYNTH-DELEG-1")  # owner state moves right after the delegation read
                return approval

            def currentness(self):
                return owner._seq

        @contextmanager
        def lockless():
            yield Unlocked()

        owner.atomic_read = lockless
        decision = w.store.admit(spec, spec.digest)
        self.assertEqual((decision.state, decision.reason_code), (DENIED, "OWNER_MOVED_DURING_ADMISSION"))
        self.assertEqual(len(w.store._records), 0)

    def test_revoke_races_admission_every_outcome_is_linearizable(self):
        """The fence's reason to exist: revoke is ordered strictly before or after the admission, never in between.
        FENCED  <=> the admission was stamped with an owner currentness earlier than the revoke.
        DENIED(DELEGATION_REVOKED) <=> the revoke came first. Either way: zero provider calls unless FENCED+executed once."""
        for i in range(60):
            w = World()
            spec = default_spec()
            out, barrier = {}, threading.Barrier(2)

            def admit():
                barrier.wait()
                out["d"] = w.store.admit(spec, spec.digest)

            def revoke():
                barrier.wait()
                out["seq"] = w.owner.revoke("SYNTH-DELEG-1")

            ts = [threading.Thread(target=admit), threading.Thread(target=revoke)]
            [t.start() for t in ts]
            [t.join() for t in ts]
            d = out["d"]
            if d.state == FENCED:
                self.assertLess(d.receipt.delegation_currentness, out["seq"])
                res = w.executor.execute(d.receipt, spec)  # admitted first: the exact batch may proceed, once
                self.assertEqual((res.state, w.provider.apply_call_count), ("COMMITTED_READBACK", 1))
            else:
                self.assertEqual(d.reason_code, "DELEGATION_REVOKED")
                self.assertEqual(w.provider.apply_call_count, 0)
                self.assertEqual(w.provider.mutation_count, 0)


if __name__ == "__main__":
    unittest.main()
