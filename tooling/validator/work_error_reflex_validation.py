"""Changed-risk public metadata witnesses; no D1 replay or runtime audit."""
from __future__ import annotations
import copy
import importlib.util
import json
from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


fixture = load("error_normal_fixture", "tooling/validator/rom_a_normal_path_validation.py")
normal = fixture.normal
reflex = load("error_reflex", "tooling/return-bridge/work_error_reflex.py")


class WorkError(unittest.TestCase):
    def setUp(self):
        self.case = fixture.NormalPath()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.task = self.case.task
        self.returned = self.case.returned
        self.original_sha = self.returned["original_sha256"]

    def event(self):
        return {"schema": reflex.SCHEMA,
          "binding": {**{k: self.task[k] for k in reflex.IDENTITY}, "original_sha256": self.original_sha},
          "profile_binding_sha256": None, "trigger": "FIRST_UNEXPECTED_ERROR",
          "observation_ref": "public-owner:D1-seven-starts-before-active",
          "attempt_ref": "fixture:attempt-7", "reached_stage": "LOCAL_START",
          "effect_state": "NONE_OBSERVED",
          "cause": {"state": "UNKNOWN", "layer": "UNKNOWN", "evidence_ref": None},
          "challenged_premise": {"ref": "fixture:path-assumption", "state": "CHALLENGED"},
          "inherited_countermodel_refs": ["public-owner:D1-working-R6-not-eternal-recipe"],
          "next_step": {"kind": "DIAGNOSE", "justification_ref": "fixture:process-socket-lifecycle",
                        "proof_limit_ref": "fixture:can-distinguish-lifecycle-not-global-root-cause",
                        "attempt_ref": "fixture:attempt-7", "uses_premise_ref": None},
          "retry": None,
          "runtime_evidence": {"code": "PASS", "startup": "FAILED", "operation": "UNKNOWN",
                              "cleanup": "UNKNOWN", "evidence_refs": ["public-owner:D1-current"]}}

    def consume(self, event, name="output"):
        # Ordinary adjacent return encounter, not a test-only direct helper.
        self.case.write_json("work_error.json", event)
        result = normal.prepare(**self.case.kwargs(name))
        receipt = json.loads(Path(result["receipt"]).read_bytes())
        return receipt["work_error_reflex"], result

    def test_code_pass_does_not_close_runtime_and_unknown_environment_not_path_cause(self):
        r, _ = self.consume(self.event())
        self.assertEqual(r["disposition"], "OBSERVE_CHEAP_DISCRIMINATOR")
        self.assertEqual(r["event"]["cause"]["state"], "UNKNOWN")
        self.assertEqual(r["reported_checkpoints"]["code"], "PASS")
        self.assertEqual(r["reported_checkpoints"]["startup"], "FAILED")
        self.assertFalse(r["runtime_closed"])
        self.assertIsNotNone(r["source_learning_encounter"])

    def test_falsified_premise_not_reused(self):
        e = self.event()
        e["challenged_premise"]["state"] = "FALSIFIED"
        e["next_step"]["uses_premise_ref"] = e["challenged_premise"]["ref"]
        with self.assertRaisesRegex(ValueError, "WORK_REFLEX_FALSIFIED_PREMISE_REUSED"):
            self.consume(e)

    def test_pre_unlock_failure_not_blamed_on_unreached_remote(self):
        e = self.event()
        e["cause"] = {"state": "PROVEN", "layer": "REMOTE_IMPORT", "evidence_ref": "fixture:unsupported"}
        with self.assertRaisesRegex(ValueError, "WORK_REFLEX_CAUSE_AT_UNREACHED_LAYER"):
            self.consume(e)

    def test_unknown_effect_blocks_blind_retry_before_sender(self):
        e = self.event()
        e["effect_state"] = "UNKNOWN"
        e["next_step"]["kind"] = "BOUNDED_RETRY"
        r, result = self.consume(e)
        self.assertEqual(r["disposition"], "RECONCILE_SAME_ATTEMPT")
        self.assertFalse(r["next_material_step_qualified"])
        class Sender:
            calls = 0
            def send_selected(self, **kwargs):
                self.calls += 1
                raise AssertionError("should not send")
        sender = Sender()
        with self.assertRaisesRegex(ValueError, "WORK_ERROR_NEXT_MATERIAL_STEP_UNQUALIFIED"):
            normal._dispatch_selected(result, sender)
        self.assertEqual(sender.calls, 0)
        self.assertFalse(Path(result["receipt"]).with_name(".receipt-dispatch-intent.json").exists())

    def test_unknown_matching_reconcile_refuses_actual_dispatch_before_intent(self):
        e = self.event()
        e["effect_state"] = "UNKNOWN"
        e["next_step"]["kind"] = "RECONCILE"
        r, result = self.consume(e)
        self.assertEqual(r["disposition"], "RECONCILE_SAME_ATTEMPT")
        self.assertEqual(r["event"]["next_step"]["attempt_ref"], e["attempt_ref"])
        self.assertFalse(r["next_material_step_qualified"])
        class Sender:
            calls = 0
            def send_selected(self, **kwargs):
                self.calls += 1
                raise AssertionError("unknown effect must not send")
        sender = Sender()
        with self.assertRaisesRegex(ValueError, "WORK_ERROR_NEXT_MATERIAL_STEP_UNQUALIFIED"):
            normal._dispatch_selected(result, sender)
        self.assertEqual(sender.calls, 0)
        self.assertFalse(Path(result["receipt"]).with_name(".receipt-dispatch-intent.json").exists())

    def test_recorded_same_attempt_readback_qualifies_existing_material_path(self):
        e = self.event()
        e["effect_state"] = "RECONCILED"
        e["next_step"]["kind"] = "RECONCILE"
        e["runtime_evidence"]["operation"] = "READBACK"
        e["runtime_evidence"]["evidence_refs"] = ["fixture:owner-same-attempt-readback"]
        r, result = self.consume(e)
        self.assertTrue(r["next_material_step_qualified"])
        self.assertFalse(r["runtime_closed"])
        class Sender:
            calls = 0
            def send_selected(self, **kwargs):
                self.calls += 1
                return {"state": "ACCEPTED", "recipient_ref": kwargs["recipient_ref"],
                        "selected_sha256": kwargs["selected_sha256"], "delivery_ref": "fixture:delivery"}
        sender = Sender()
        delivered = normal._dispatch_selected(result, sender)
        self.assertEqual(sender.calls, 1)
        self.assertEqual(delivered["dispatch"], "SENT_ACCEPTED")
        e["runtime_evidence"]["operation"] = "UNKNOWN"
        r, _ = self.consume(e, "enum-only")
        self.assertFalse(r["next_material_step_qualified"])

    def test_known_quick_fix_stays_cheap(self):
        e = self.event()
        e["cause"] = {"state": "PROVEN", "layer": "LOCAL_ENVIRONMENT", "evidence_ref": "fixture:exact-enum-defect"}
        e["next_step"]["kind"] = "QUICK_FIX"
        r, _ = self.consume(e)
        self.assertEqual(r["disposition"], "DIRECT_KNOWN_FIX")
        self.assertTrue(r["next_material_step_qualified"])
        self.assertIsNone(r["source_learning_encounter"])
        self.assertIsNone(r["solution_escalation"])

    def test_qualified_transient_retry_expiry_and_idempotency(self):
        e = self.event()
        e["next_step"]["kind"] = "BOUNDED_RETRY"
        e["retry"] = {"contract_ref": "fixture:bounded-transient", "evidence_ref": "fixture:qualification",
             "qualified": True, "idempotent": True, "attempt_number": 1,
             "max_attempts": 3, "expires_at": time.time() + 60, "failure_class": "KNOWN_TRANSIENT"}
        r, _ = self.consume(e)
        self.assertEqual(r["disposition"], "QUALIFIED_BOUNDED_TRANSIENT_RETRY")
        self.assertTrue(r["next_material_step_qualified"])
        self.assertIsNone(r["solution_escalation"])
        e["retry"]["expires_at"] = time.time() - 1
        with self.assertRaisesRegex(ValueError, "WORK_REFLEX_RETRY_CONTRACT_UNQUALIFIED_OR_EXPIRED"):
            self.consume(e, "expired")

    def test_unknown_cause_quickfix_not_qualified_by_waiting(self):
        e = self.event()
        e["next_step"]["kind"] = "QUICK_FIX"
        r, _ = self.consume(e)
        self.assertFalse(r["next_material_step_qualified"])
        self.assertEqual(r["disposition"], "OBSERVE_CHEAP_DISCRIMINATOR")

    def test_material_return_and_context_break_preserve_original_challenged_basis(self):
        for trigger in ("MATERIAL_RETURN", "CONTEXT_BREAK"):
            e = self.event()
            e["trigger"] = trigger
            r, result = self.consume(e, trigger)
            self.assertEqual(r["binding"]["original_sha256"], self.original_sha)
            self.assertEqual(r["binding"]["task_revision"], self.task["task_revision"])
            self.assertEqual(r["event"]["challenged_premise"], e["challenged_premise"])
            self.assertEqual(Path(result["selected"]).read_bytes(), self.case.fixture["parent.txt"])

    def test_narrow_diagnosis_active_and_error_stop_cleanup_are_distinct_reports(self):
        e = self.event()
        e["observation_ref"] = "public-owner:D1-E2-9da583-eighth-ACTIVE-R7-read"
        e["reached_stage"] = "REMOTE"
        e["runtime_evidence"] = {"code": "PASS", "startup": "ACTIVE", "operation": "READBACK",
             "cleanup": "ERROR_STOP_CLOSED", "evidence_refs": ["public-owner:D1-R10-refusal-cleanup"]}
        r, _ = self.consume(e)
        self.assertEqual(r["reported_checkpoints"]["startup"], "ACTIVE")
        self.assertEqual(r["reported_checkpoints"]["cleanup"], "ERROR_STOP_CLOSED")
        self.assertFalse(r["runtime_closed"])
        self.assertEqual(r["event"]["cause"]["state"], "UNKNOWN")

    def test_vinkel_only_actual_new_mechanism_trigger(self):
        e = self.event()
        e["next_step"]["kind"] = "STRUCTURAL_CHANGE"
        r, _ = self.consume(e)
        self.assertEqual(r["solution_escalation"]["outcome"], "OBSERVE_FIRST")
        self.assertFalse(r["next_material_step_qualified"])

    def test_task_profile_and_continuation_binding_mismatch_refused(self):
        for field in ("task_revision", "actor_ref", "generation_ref", "original_sha256"):
            e = self.event()
            e["binding"][field] += "-other"
            with self.assertRaisesRegex(ValueError, "WORK_REFLEX_TASK_OR_PROFILE_BINDING_MISMATCH"):
                self.consume(e, field)

    def test_actual_profile_sidecar_bound_and_wrong_profile_hash_refused(self):
        selected = {"source_ref": "fixture:library", "source_revision": "fixture:r1",
                    "primary_coat": "KODEMESTER", "lenses": ["SOURCEINTEGRATOR"],
                    "packages": [], "required_tools": []}
        captured = fixture.capture.capture(self.case.root / "profile-captures",
                    {"schema": fixture.capture.SCHEMA, "task": self.task},
                    self.case.fixture["parent.txt"], profile=selected)
        self.case.captured = captured
        import hashlib
        raw = Path(captured["capture_path"]).with_name("task_local_profile.json").read_bytes()
        e = self.event()
        e["profile_binding_sha256"] = hashlib.sha256(raw).hexdigest()
        r, result = self.consume(e)
        receipt = json.loads(Path(result["receipt"]).read_bytes())
        self.assertEqual(receipt["task_local_profile"]["uptake"]["state"], "NOT_ATTESTED")
        self.assertEqual(r["profile_binding_sha256"], hashlib.sha256(raw).hexdigest())
        e["profile_binding_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "WORK_REFLEX_TASK_OR_PROFILE_BINDING_MISMATCH"):
            self.consume(e, "wrong-profile")
    def test_unknown_mutation_precedes_structural_escalation(self):
        e = self.event()
        e["effect_state"] = "UNKNOWN"
        e["next_step"]["kind"] = "STRUCTURAL_CHANGE"
        r, _ = self.consume(e)
        self.assertEqual(r["disposition"], "RECONCILE_SAME_ATTEMPT")
        self.assertFalse(r["next_material_step_qualified"])
        self.assertIsNone(r["solution_escalation"])
    def test_actual_local_prepare_exception_preserves_refusal_and_attaches_reflex(self):
        with self.assertRaises(ValueError) as caught:
            normal.prepare(capture_record="not-absolute", return_input=self.case.return_file,
                           out_dir=str(self.case.root / "never"))
        self.assertEqual(caught.exception.work_error_reflex["reached_stage"], "LOCAL_PREPARATION")
        self.assertEqual(caught.exception.work_error_reflex["cause"], "UNKNOWN")
        self.assertFalse(caught.exception.work_error_reflex["runtime_closed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
