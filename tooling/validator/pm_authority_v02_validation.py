#!/usr/bin/env python3
"""P1603 PM authority v0.2 local default-off acceptance/negative suite.

Synthetic owner fixtures exercise contract mechanics only. They do not install,
mint, or prove a real Human mandate, PM delegation, provider binding, claim,
effect, boot, Worker, or production owner.
"""
from __future__ import annotations

import copy
import threading
import unittest
from dataclasses import dataclass
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "mcp"), str(ROOT / "tooling/validator")]

from worker_context_auth import (  # noqa: E402
    BoundPmAuthorityV02,
    BoundWorkerPolicyResolver,
    PM_BIND_RECEIPT_SCHEMA,
    PM_DELEGATION_SCHEMA,
    PM_EFFECT_OUTCOME_SCHEMA,
    PM_EFFECT_RECEIPT_SCHEMA,
    PM_RIGHTS_SCHEMA,
    PM_TASK_AUTHORITY_SCHEMA,
    UNKNOWN_COMMIT,
    WorkerContextAuthError,
    _canonical_sha256,
)


WORKER_GENERATION = "CEREBRO-BOOT-WORKER-P1603"
WORKER_ACTOR = "WORKER-P1603"
SOURCE = "c80a5399d78c5a244f3e70db3a6f3a5ec501af0e"
METHOD = "a" * 64
PM_ACTOR = "PROJECT_MANAGER_C1A05B39"
PM_GENERATION = "CEREBRO-BOOT-PM-C1A05B39"
MANDATE = "HUMAN-PM-MANDATE-V02"
DELEGATION = "PM-DELEGATION-V02-C1A05B39"
ADMIN_GRANT = "HUMAN-ADMIN-PM-BIND-001"


def make_rights(*, renewal=True):
    value = {
        "schema": PM_RIGHTS_SCHEMA,
        "mandate_ref": MANDATE,
        "semantic_revision": 2,
        "grantor_role": "HUMAN",
        "human_decision_refs": ["PM8852", "PM9532"],
        "operations": ["CLAIM_BIND", "CLAIM_RELEASE"],
        "operation_scopes": {
            "CLAIM_BIND": ["TASK_EXECUTE"],
            "CLAIM_RELEASE": ["CLAIM_CLOSE"],
        },
        "held_operations": [],
        "validity": {
            "not_before_owner_revision": 10,
            "expires_after_owner_revision": None,
        },
        "renewal_rule_ref": (
            "SAME_ACTOR_SAME_GENERATION_TECHNICAL_RENEWAL"
            if renewal else "NO_AUTOMATIC_RENEWAL"
        ),
        "current": True,
        "revoked": False,
        "readback_verified": True,
        "owner_revision": 10,
        "readback_ref": "HUMAN-RIGHTS-READBACK-10",
    }
    value["basis_sha256"] = _canonical_sha256(BoundPmAuthorityV02._rights_basis(value))
    return value


class SharedAuthorityState:
    def __init__(self, *, renewal=True):
        self.lock = threading.RLock()
        self.rights = make_rights(renewal=renewal)
        self.delegation = None
        self.bind_calls = []
        self.effect_results = {}
        self.effect_apply_count = 0
        self.effect_progress_revision = 0
        self.raise_after_commit = False
        self.force_unknown_readback = False
        self.prove_no_commit = {}

    def rights_current(self, **kwargs):
        return copy.deepcopy(self.rights)

    def delegation_current(self, **kwargs):
        if self.delegation is None:
            raise RuntimeError("delegation absent")
        return copy.deepcopy(self.delegation)

    def bind_or_renew(self, **kwargs):
        with self.lock:
            self.bind_calls.append(copy.deepcopy(kwargs))
            action = kwargs["action"]
            current_rev = 20 if self.delegation is None else self.delegation["owner_revision"] + 1
            fence_rev = 30 if self.delegation is None else self.delegation["fence_revision"] + 1
            delegation_ref = (
                DELEGATION
                if self.delegation is None
                or kwargs["pm_generation_ref"] == self.delegation["pm_generation_ref"]
                else "PM-DELEGATION-SUCCESSOR"
            )
            authorized_by = getattr(self, "authorized_by_role", "HUMAN_ADMIN_OWNER")
            self.delegation = {
                "schema": PM_DELEGATION_SCHEMA,
                "delegation_ref": delegation_ref,
                "current": True,
                "revoked": False,
                "readback_verified": True,
                "pm_actor_ref": kwargs["pm_actor_ref"],
                "pm_generation_ref": kwargs["pm_generation_ref"],
                "mandate_ref": kwargs["mandate_ref"],
                "mandate_semantic_revision": kwargs["mandate_semantic_revision"],
                "authorized_by_role": authorized_by,
                "administrative_grant_ref": ADMIN_GRANT,
                "operations": ["CLAIM_BIND", "CLAIM_RELEASE"],
                "scopes": ["TASK_EXECUTE", "CLAIM_CLOSE"],
                "renewal_rule_ref": self.rights["renewal_rule_ref"],
                "owner_revision": current_rev,
                "fence_revision": fence_rev,
                "readback_ref": f"DELEGATION-READBACK-{current_rev}",
            }
            return {
                "schema": PM_BIND_RECEIPT_SCHEMA,
                "action": action,
                "authorized_by_role": authorized_by,
                "mandate_ref": kwargs["mandate_ref"],
                "mandate_semantic_revision": kwargs["mandate_semantic_revision"],
                "mandate_basis_sha256": kwargs["mandate_basis_sha256"],
                "pm_actor_ref": kwargs["pm_actor_ref"],
                "pm_generation_ref": kwargs["pm_generation_ref"],
                "administrative_grant_ref": ADMIN_GRANT,
                "delegation_ref": delegation_ref,
                "owner_revision": current_rev,
                "readback_verified": True,
                "readback_ref": f"BIND-READBACK-{current_rev}",
                "succession_ref": kwargs.get("succession_ref"),
            }

    def hold_current(
        self,
        *,
        basis,
        basis_fingerprint,
        expected_mandate_owner_revision,
        expected_delegation_owner_revision,
        expected_delegation_fence_revision,
    ):
        owner = self

        class Fence:
            def __enter__(self):
                owner.lock.acquire()
                return self

            def __exit__(self, *_):
                owner.lock.release()
                return False

            def assert_current(self):
                rights = owner.rights
                delegation = owner.delegation
                if (
                    rights["current"] is not True
                    or rights["revoked"] is not False
                    or rights["owner_revision"] != expected_mandate_owner_revision
                    or delegation is None
                    or delegation["current"] is not True
                    or delegation["revoked"] is not False
                    or delegation["owner_revision"] != expected_delegation_owner_revision
                    or delegation["fence_revision"] != expected_delegation_fence_revision
                    or basis_fingerprint != _canonical_sha256(basis)
                ):
                    raise WorkerContextAuthError("pm-effect-owner-fence-stale")

        return Fence()

    def apply_once(self, *, basis, basis_fingerprint):
        with self.lock:
            idem = basis["idempotency_key"]
            if idem in self.effect_results:
                return copy.deepcopy(self.effect_results[idem])
            self.effect_apply_count += 1
            self.effect_progress_revision += 1
            receipt = {
                "schema": PM_EFFECT_RECEIPT_SCHEMA,
                "state": "COMMITTED",
                "decision_ref": basis["decision_ref"],
                "task_ref": basis["task_ref"],
                "idempotency_key": idem,
                "basis_fingerprint": basis_fingerprint,
                "result_ref": f"RESULT-{idem}",
                "owner_revision": 100 + self.effect_progress_revision,
                "readback_verified": True,
                "readback_ref": f"EFFECT-READBACK-{self.effect_progress_revision}",
            }
            self.effect_results[idem] = receipt
            if self.raise_after_commit:
                raise OSError("simulated lost response after commit")
            return copy.deepcopy(receipt)

    def read_outcome(self, *, decision_ref, idempotency_key):
        if self.force_unknown_readback:
            return {
                "schema": PM_EFFECT_OUTCOME_SCHEMA,
                "decision_ref": decision_ref,
                "idempotency_key": idempotency_key,
                "basis_fingerprint": self._basis_for_result(idempotency_key),
                "state": "UNKNOWN",
                "readback_verified": True,
            }
        if idempotency_key in self.effect_results:
            receipt = self.effect_results[idempotency_key]
            return {
                "schema": PM_EFFECT_OUTCOME_SCHEMA,
                "decision_ref": decision_ref,
                "idempotency_key": idempotency_key,
                "basis_fingerprint": receipt["basis_fingerprint"],
                "state": "COMMITTED",
                "readback_verified": True,
                "receipt": copy.deepcopy(receipt),
            }
        if idempotency_key in self.prove_no_commit:
            return {
                "schema": PM_EFFECT_OUTCOME_SCHEMA,
                "decision_ref": decision_ref,
                "idempotency_key": idempotency_key,
                "basis_fingerprint": self.prove_no_commit[idempotency_key],
                "state": "NO_COMMIT",
                "readback_verified": True,
            }
        raise RuntimeError("outcome unavailable")

    def _basis_for_result(self, idempotency_key):
        if idempotency_key in self.effect_results:
            return self.effect_results[idempotency_key]["basis_fingerprint"]
        return getattr(self, "last_basis_fingerprint", "0" * 64)


class Reader:
    def __init__(self, call):
        self.read_current = call


class AdminOwner:
    def __init__(self, state):
        self.state = state

    def bind_or_renew(self, **kwargs):
        return self.state.bind_or_renew(**kwargs)


class EffectOwner:
    def __init__(self, state):
        self.state = state

    def hold_current(self, **kwargs):
        self.state.last_basis_fingerprint = kwargs["basis_fingerprint"]
        return self.state.hold_current(**kwargs)

    def apply_once(self, **kwargs):
        return self.state.apply_once(**kwargs)

    def read_outcome(self, **kwargs):
        return self.state.read_outcome(**kwargs)


class WorkerOwners:
    """Existing WORKER owner readers, extended only with v0.2 task fields."""

    def __init__(self):
        self.worker_mandate_current = True
        self.task_current = True
        self.task_ref = "TASK-D1"
        self.decision_ref = "DECISION-D1"
        self.idempotency_key = "IDEMPOTENCY-D1"
        self.task_revision = 1
        self.task_sha256 = "1" * 64
        self.operation = "CLAIM_BIND"
        self.authority_scope = ["TASK_EXECUTE"]
        self.effect_target_ref = "CLAIM-TARGET-D1"
        self.pm_generation_ref = PM_GENERATION
        self.delegation_ref = DELEGATION

    def mandate(self, **kwargs):
        return {
            "schema": "cerebro.worker-pm-parent-mandate/v1",
            "current": self.worker_mandate_current,
            "revoked": not self.worker_mandate_current,
            "grantor_role": "HUMAN",
            "grantee_role": "PROJECT_MANAGER",
            "generation_ref": WORKER_GENERATION,
            "actor_ref": WORKER_ACTOR,
            "source_revision": SOURCE,
            "scopes": ["WORKER_ROLE_ATTACH", "WORKER_TASK_ISSUE"],
            "allowed_task_scopes": ["TASK_EXECUTE"],
            "mandate_ref": "WORKER-PARENT-MANDATE",
            "owner_revision": 3,
            "readback_ref": "WORKER-MANDATE-RB",
        }

    def task(self, **kwargs):
        return {
            "schema": "cerebro.worker-pm-task-decision/v1",
            "current": self.task_current,
            "revoked": not self.task_current,
            "issued_by_role": "PROJECT_MANAGER",
            "generation_ref": WORKER_GENERATION,
            "actor_ref": WORKER_ACTOR,
            "task_ref": kwargs["task_ref"],
            "source_revision": SOURCE,
            "method_fingerprint": METHOD,
            "mandate_ref": "WORKER-PARENT-MANDATE",
            "mandate_revision": 3,
            "scope": ["TASK_EXECUTE"],
            "claim_ref": "PM-CLAIM-V02",
            "packet_ref": "PM-PACKET-V02",
            "queue_ref": "PM-QUEUE-V02",
            "decision_ref": self.decision_ref,
            "owner_revision": 8,
            "frontier_ref": "PM-FRONTIER-V02",
            "readback_verified": True,
            "readback_ref": "PM-TASK-RB-V02",
            "authority_schema": PM_TASK_AUTHORITY_SCHEMA,
            "authority_mandate_ref": MANDATE,
            "authority_mandate_revision": 2,
            "delegation_ref": self.delegation_ref,
            "pm_actor_ref": PM_ACTOR,
            "pm_generation_ref": self.pm_generation_ref,
            "task_revision": self.task_revision,
            "task_sha256": self.task_sha256,
            "operation": self.operation,
            "authority_scope": copy.deepcopy(self.authority_scope),
            "idempotency_key": self.idempotency_key,
            "effect_target_ref": self.effect_target_ref,
        }


class WorkerClaimReader:
    def __init__(self, owners):
        self.owners = owners

    def read_current_task(self, **kwargs):
        return self.owners.task(**kwargs)


class MandateReader:
    def __init__(self, owners):
        self.owners = owners

    def read_current(self, **kwargs):
        return self.owners.mandate(**kwargs)


class PmAuthorityV02Proof(unittest.TestCase):
    def setUp(self):
        self.state = SharedAuthorityState()
        self.worker_owners = WorkerOwners()
        self.worker_policy = BoundWorkerPolicyResolver(
            human_intent_reader=object(),
            mandate_reader=MandateReader(self.worker_owners),
            claim_reader=WorkerClaimReader(self.worker_owners),
            enabled=True,
        )
        self.pm = BoundPmAuthorityV02(
            self.worker_policy,
            Reader(self.state.rights_current),
            Reader(self.state.delegation_current),
            AdminOwner(self.state),
            EffectOwner(self.state),
            enabled=True,
        )
        self.bind = self.pm.bind_or_renew_pm_generation(
            mandate_ref=MANDATE,
            pm_actor_ref=PM_ACTOR,
            pm_generation_ref=PM_GENERATION,
        )

    def snapshot(self, *, task_ref=None, revision=None, sha=None):
        return self.pm.read_current_consumer_authority(
            mandate_ref=MANDATE,
            pm_actor_ref=PM_ACTOR,
            pm_generation_ref=PM_GENERATION,
            worker_generation_ref=WORKER_GENERATION,
            worker_actor_ref=WORKER_ACTOR,
            source_revision=SOURCE,
            method_fingerprint=METHOD,
            task_ref=task_ref or self.worker_owners.task_ref,
            expected_task_revision=revision or self.worker_owners.task_revision,
            expected_task_sha256=sha or self.worker_owners.task_sha256,
        )

    def test_default_off_and_unbound_ports_fail_closed(self):
        disabled = BoundPmAuthorityV02(
            self.worker_policy, None, None, None, None, enabled=False
        )
        with self.assertRaisesRegex(WorkerContextAuthError, "default-off"):
            disabled.read_current_rights(MANDATE)

    def test_administrative_bootstrap_is_distinct_from_pm_self_grant(self):
        self.assertEqual(self.bind["bind_receipt"]["authorized_by_role"], "HUMAN_ADMIN_OWNER")
        self.assertEqual(self.bind["delegation"]["mandate_ref"], MANDATE)
        self.assertEqual(len(self.state.bind_calls), 1)

        bad_state = SharedAuthorityState()
        bad_state.authorized_by_role = "PROJECT_MANAGER"
        bad = BoundPmAuthorityV02(
            self.worker_policy,
            Reader(bad_state.rights_current),
            Reader(bad_state.delegation_current),
            AdminOwner(bad_state),
            EffectOwner(bad_state),
            enabled=True,
        )
        with self.assertRaisesRegex(WorkerContextAuthError, "self-grant"):
            bad.bind_or_renew_pm_generation(
                mandate_ref=MANDATE,
                pm_actor_ref=PM_ACTOR,
                pm_generation_ref=PM_GENERATION,
            )

    def test_exact_task_revision_hash_generation_scope_and_currentness_required(self):
        self.snapshot()
        with self.assertRaisesRegex(WorkerContextAuthError, "task-basis"):
            self.snapshot(revision=2)
        with self.assertRaisesRegex(WorkerContextAuthError, "task-basis"):
            self.snapshot(sha="f" * 64)

        self.worker_owners.pm_generation_ref = "OLD-PM-GENERATION"
        with self.assertRaisesRegex(WorkerContextAuthError, "task-basis"):
            self.snapshot()
        self.worker_owners.pm_generation_ref = PM_GENERATION

        self.worker_owners.authority_scope = ["OUTSIDE"]
        with self.assertRaisesRegex(WorkerContextAuthError, "scope"):
            self.snapshot()
        self.worker_owners.authority_scope = ["TASK_EXECUTE"]

        self.state.delegation["revoked"] = True
        self.state.delegation["current"] = False
        with self.assertRaisesRegex(WorkerContextAuthError, "delegation-not-current"):
            self.snapshot()

    def test_one_held_operation_class_does_not_expand_hold_to_other_class(self):
        self.state.rights["held_operations"] = ["CLAIM_RELEASE"]
        self.worker_owners.operation = "CLAIM_RELEASE"
        self.worker_owners.authority_scope = ["CLAIM_CLOSE"]
        with self.assertRaisesRegex(WorkerContextAuthError, "operation-class-held"):
            self.snapshot()

        self.worker_owners.operation = "CLAIM_BIND"
        self.worker_owners.authority_scope = ["TASK_EXECUTE"]
        allowed = self.snapshot()
        self.assertEqual(allowed["task"]["operation"], "CLAIM_BIND")

    def test_revoked_human_rights_stop_consumer_before_effect(self):
        self.state.rights["current"] = False
        self.state.rights["revoked"] = True
        with self.assertRaisesRegex(WorkerContextAuthError, "not-current-human-owned"):
            self.snapshot()
        self.assertEqual(self.state.effect_apply_count, 0)

    def test_authoritative_no_commit_is_distinct_from_unknown_and_does_not_retry(self):
        snap = self.snapshot()
        idem = snap["immutable_basis"]["idempotency_key"]
        self.state.prove_no_commit[idem] = snap["basis_fingerprint"]
        result = self.pm.reconcile_unknown(snap)
        self.assertEqual(result["result"], "NO_COMMIT_PROVEN")
        self.assertEqual(self.state.effect_apply_count, 0)

    def test_revoke_before_commit_denies_zero_new_effect(self):
        snap = self.snapshot()
        self.state.delegation["current"] = False
        self.state.delegation["revoked"] = True
        with self.assertRaisesRegex(WorkerContextAuthError, "fence-stale"):
            self.pm.apply_current_task_once(snap)
        self.assertEqual(self.state.effect_apply_count, 0)
        self.assertEqual(self.state.effect_results, {})

    def test_commit_before_revoke_retains_one_receipt_and_exact_replay(self):
        snap = self.snapshot()
        first = self.pm.apply_current_task_once(snap)
        self.assertEqual(first["result"], "APPLIED")
        receipt = copy.deepcopy(first["receipt"])
        self.assertEqual(self.state.effect_apply_count, 1)

        self.state.delegation["current"] = False
        self.state.delegation["revoked"] = True
        reconciled = self.pm.reconcile_unknown(snap)
        self.assertEqual(reconciled["result"], "APPLIED_RECONCILED")
        self.assertEqual(reconciled["receipt"], receipt)
        self.assertEqual(self.state.effect_apply_count, 1)

    def test_ambiguous_commit_is_unknown_then_reconciles_without_retry(self):
        snap = self.snapshot()
        self.state.raise_after_commit = True
        unknown = self.pm.apply_current_task_once(snap)
        self.assertEqual(unknown["result"], UNKNOWN_COMMIT)
        self.assertEqual(self.state.effect_apply_count, 1)
        self.state.raise_after_commit = False
        resolved = self.pm.reconcile_unknown(snap)
        self.assertEqual(resolved["result"], "APPLIED_RECONCILED")
        self.assertEqual(self.state.effect_apply_count, 1)

    def test_immutable_basis_is_separate_from_mutable_progress_and_fence_revision(self):
        snap = self.snapshot()
        before = snap["basis_fingerprint"]
        self.pm.apply_current_task_once(snap)
        self.state.effect_progress_revision += 50

        renewal = self.pm.bind_or_renew_pm_generation(
            mandate_ref=MANDATE,
            pm_actor_ref=PM_ACTOR,
            pm_generation_ref=PM_GENERATION,
            previous_pm_generation_ref=PM_GENERATION,
        )
        self.assertGreater(
            renewal["delegation"]["owner_revision"],
            snap["delegation"]["owner_revision"],
        )
        refreshed = self.snapshot()
        self.assertEqual(refreshed["basis_fingerprint"], before)

    def test_same_actor_rollover_reuses_semantic_mandate_and_does_not_replay_d1(self):
        d1 = self.snapshot()
        first = self.pm.apply_current_task_once(d1)
        self.assertEqual(first["result"], "APPLIED")
        d1_receipt = first["receipt"]
        human_refs = copy.deepcopy(self.state.rights["human_decision_refs"])
        semantic_revision = self.state.rights["semantic_revision"]

        # Technical renewal after chat rollover; no new Human semantic decision.
        renewed = self.pm.bind_or_renew_pm_generation(
            mandate_ref=MANDATE,
            pm_actor_ref=PM_ACTOR,
            pm_generation_ref=PM_GENERATION,
            previous_pm_generation_ref=PM_GENERATION,
        )
        self.assertEqual(self.state.rights["human_decision_refs"], human_refs)
        self.assertEqual(self.state.rights["semantic_revision"], semantic_revision)
        self.assertEqual(renewed["bind_receipt"]["action"], "RENEW")

        # New coordinator instance models same-actor process/chat rollover.
        restarted = BoundPmAuthorityV02(
            self.worker_policy,
            Reader(self.state.rights_current),
            Reader(self.state.delegation_current),
            AdminOwner(self.state),
            EffectOwner(self.state),
            enabled=True,
        )
        def restarted_snapshot():
            return restarted.read_current_consumer_authority(
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

        d1_after_rollover = restarted_snapshot()
        replay = restarted.apply_current_task_once(d1_after_rollover)
        self.assertEqual(replay["result"], "APPLIED")
        self.assertEqual(replay["receipt"], d1_receipt)
        self.assertEqual(self.state.effect_apply_count, 1)

        # A distinct next lawful operation gets a new decision/idempotency.
        self.worker_owners.task_ref = "TASK-D2"
        self.worker_owners.decision_ref = "DECISION-D2"
        self.worker_owners.idempotency_key = "IDEMPOTENCY-D2"
        self.worker_owners.task_revision = 2
        self.worker_owners.task_sha256 = "2" * 64
        self.worker_owners.effect_target_ref = "CLAIM-TARGET-D2"
        d2 = restarted_snapshot()
        second = restarted.apply_current_task_once(d2)
        self.assertEqual(second["result"], "APPLIED")
        self.assertEqual(self.state.effect_apply_count, 2)
        self.assertNotEqual(second["receipt"]["result_ref"], d1_receipt["result_ref"])

    def test_renewal_requires_rule_and_new_generation_requires_explicit_succession(self):
        no_rule_state = SharedAuthorityState(renewal=False)
        no_rule = BoundPmAuthorityV02(
            self.worker_policy,
            Reader(no_rule_state.rights_current),
            Reader(no_rule_state.delegation_current),
            AdminOwner(no_rule_state),
            EffectOwner(no_rule_state),
            enabled=True,
        )
        no_rule.bind_or_renew_pm_generation(
            mandate_ref=MANDATE,
            pm_actor_ref=PM_ACTOR,
            pm_generation_ref=PM_GENERATION,
        )
        with self.assertRaisesRegex(WorkerContextAuthError, "renewal-rule-absent"):
            no_rule.bind_or_renew_pm_generation(
                mandate_ref=MANDATE,
                pm_actor_ref=PM_ACTOR,
                pm_generation_ref=PM_GENERATION,
                previous_pm_generation_ref=PM_GENERATION,
            )

        with self.assertRaisesRegex(WorkerContextAuthError, "explicit-succession"):
            self.pm.bind_or_renew_pm_generation(
                mandate_ref=MANDATE,
                pm_actor_ref=PM_ACTOR,
                pm_generation_ref="PM-NEW-GENERATION",
                previous_pm_generation_ref=PM_GENERATION,
            )


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(PmAuthorityV02Proof)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(
        "pm_authority_v02_validation: "
        f"{result.testsRun - len(result.failures) - len(result.errors)}/{result.testsRun} PASS"
    )
    raise SystemExit(0 if result.wasSuccessful() else 1)
