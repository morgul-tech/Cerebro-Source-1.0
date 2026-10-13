"""Selected D owner-record/normal-consumer seam, synthetic metadata only."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


fixture = load("resume_normal_fixture", "tooling/validator/rom_a_normal_path_validation.py")
normal = fixture.normal


class WorkResume(unittest.TestCase):
    def setUp(self):
        self.case = fixture.NormalPath(); self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.task = self.case.task

    def record(self):
        return {"schema": "cerebro-owner-selected-work-resume/v1",
            "binding": {**{k:self.task[k] for k in
                ("task_ref","task_revision","actor_ref","generation_ref","carrier_ref","arc_ref")},
                "original_sha256": self.case.returned["original_sha256"]},
            "owner_selection_ref": "fixture:D2-current-broker-order",
            "current_task": {"task_ref":self.task["task_ref"],"task_revision":self.task["task_revision"],
                "owner_ref":"fixture:D2","next_action_ref":"fixture:broker-read","attempt_ref":"fixture:same-attempt",
                "evidence_refs":["fixture:owner-current-order"],"closed_arcs":["fixture:CLOSED-Liv"]},
            "received_order":{"kind":"NO_NEW_ORDER","selection_ref":None,"task_ref":None,
                "task_revision":None,"owner_ref":None,"original_ref":None},
            "current_blocks":[{"task_ref":self.task["task_ref"],"task_revision":self.task["task_revision"],
                "state":"CURRENT","superseded_by":None},{"task_ref":"fixture:CLOSED-Liv",
                "task_revision":"fixture:old","state":"CLOSED","superseded_by":None}],
            "inspection":{"intent":"STATUS","authorization_ref":None,"case_ref":None},
            "cases":[], "learning":{"consumer_ref":"D2:own-AGENTS-current-before-material-action",
                "method_source_ref":"standards/development/implementation-learning-loop.yaml",
                "source_revision":self.task["source_head"],"profile_binding_sha256":None,
                "known_countermodel_refs":["fixture:known-source-reflex-F1"],
                "next_choice_ref":"fixture:broker-read","use_state":"NOT_ATTESTED","use_evidence_ref":None},
            "fact_delta":None}

    def prepare(self, record, name="result"):
        path=self.case.write_json(name+"-resume.json",record)
        r=normal.prepare(**self.case.kwargs(name),resume_input=path)
        return json.loads(Path(r["receipt"]).read_bytes()),r

    def test_explicit_inspection_preserves_one_case_scope_period_owner_criterion_before_dispatch(self):
        r=self.record()
        case={"case_ref":"P23-D-BK11-DAY-POSTMORTEM-20261013-001",
              "scope_ref":"fixture:room-D-workmode","period_ref":"fixture:day-cut",
              "analysis_owner_ref":"AD1/P23","finish_criterion_ref":"fixture:correction-and-followup",
              "state":"ACTIVE","pending_obligations":["fixture:finished-criterion-observation"]}
        r["cases"]=[case]
        r["inspection"]={"intent":"EXPLICIT_AUTHORIZED_ROOM_INSPECTION",
                         "authorization_ref":"fixture:Human-original","case_ref":case["case_ref"]}
        receipt,result=self.prepare(r)
        self.assertEqual(receipt["work_resume"]["preserved_cases"],[case])
        class Sender:
            calls=0
            def send_selected(inner,**kw):
                inner.calls+=1
                actual=json.loads(Path(result["receipt"]).read_bytes())
                self.assertEqual(actual["work_resume"]["preserved_cases"],[case])
                return {"state":"ACCEPTED","recipient_ref":kw["recipient_ref"],
                        "selected_sha256":kw["selected_sha256"],"delivery_ref":"fixture:delivery"}
        s=Sender();normal._dispatch_selected(result,s);self.assertEqual(s.calls,1)
        r["cases"]=[]
        with self.assertRaisesRegex(ValueError,"RESUME_INSPECTION_EXISTING_CASE_BIND_REQUIRED"):
            self.prepare(r,"missing-case")

    def test_ordinary_status_no_invented_case(self):
        receipt,_=self.prepare(self.record())
        self.assertEqual(receipt["work_resume"]["preserved_cases"],[])
        self.assertEqual(receipt["work_resume"]["inspection"]["intent"],"STATUS")

    def test_owner_selected_broker_and_closed_liv_no_provider_or_old_task_selection(self):
        receipt,_=self.prepare(self.record())
        resume=receipt["work_resume"]
        self.assertEqual(resume["current_task"]["task_ref"],self.task["task_ref"])
        self.assertEqual(resume["current_task"]["attempt_ref"],"fixture:same-attempt")
        self.assertIn("fixture:CLOSED-Liv",resume["current_task"]["closed_arcs"])
        self.assertFalse(resume["actual_worker_use_attested"])
        r=self.record();r["current_blocks"][1]["state"]="CURRENT"
        with self.assertRaisesRegex(ValueError,"RESUME_CONTRADICTORY_CURRENT_BLOCK"):
            self.prepare(r,"conflict")
        self.assertFalse((self.case.root/"conflict").exists())

    def test_genuine_new_owner_liv_order_selectable_with_new_capture_not_from_summary(self):
        old=self.task["task_ref"]
        self.task={**self.task,"task_ref":"fixture:NEW-Human-Liv","task_revision":"fixture:new"}
        self.case.captured=fixture.capture.capture(self.case.root/"new-capture",
               {"schema":fixture.capture.SCHEMA,"task":self.task},self.case.fixture["parent.txt"])
        returned={**self.case.returned,"task_ref":self.task["task_ref"],"task_revision":self.task["task_revision"]}
        self.case.return_file=self.case.write_json("new-return.json",
             {"schema":normal.RETURN_SCHEMA,"observation_ref":"fixture:new-return","return":returned})
        r=self.record();r["owner_selection_ref"]="fixture:new-Human-original"
        r["current_task"]["closed_arcs"]=[old,"fixture:CLOSED-Liv"]
        r["received_order"]={"kind":"OWNER_SELECTED_NEW_ORDER","selection_ref":r["owner_selection_ref"],
           "task_ref":self.task["task_ref"],"task_revision":self.task["task_revision"],
           "owner_ref":r["current_task"]["owner_ref"],"original_ref":"fixture:genuine-Human-order"}
        receipt,_=self.prepare(r)
        self.assertEqual(receipt["work_resume"]["current_task"]["task_ref"],"fixture:NEW-Human-Liv")

    def test_explicit_d_entry_cli_consumes_owner_record_then_mock_effect(self):
        r=self.record()
        path=self.case.write_json("D2-owner-selected-resume.json",r)
        out=self.case.root/"d-entry-output"
        command=[sys.executable,"-B",str(ROOT/"tooling/return-bridge/rom_a_normal_path.py"),
          "--capture-record",self.case.captured["capture_path"],"--return-input",self.case.return_file,
          "--resume-input",path,"--out-dir",str(out)]
        proc=subprocess.run(command,capture_output=True,text=True)
        self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr)
        result=json.loads(proc.stdout)
        receipt=json.loads(Path(result["receipt"]).read_bytes())
        self.assertTrue(receipt["work_resume"]["local_record_consumed"])
        self.assertEqual(receipt["work_resume"]["learning"]["known_countermodel_refs"],["fixture:known-source-reflex-F1"])
        class Sender:
            calls=0
            def send_selected(inner,**kw):
                inner.calls+=1
                return {"state":"ACCEPTED","recipient_ref":kw["recipient_ref"],
                        "selected_sha256":kw["selected_sha256"],"delivery_ref":"fixture:d-entry"}
        sender=Sender();normal._dispatch_selected(result,sender)
        self.assertEqual(sender.calls,1)
        # This executes the prepared Source CLI seam, NOT an actual D chat/AGENTS installation.

    def test_resume_unknown_effect_same_attempt_zero_intent_known_fix_short(self):
        errors=load("resume_error_fixture","tooling/validator/work_error_reflex_validation.py")
        c=errors.WorkError();c.setUp();self.addCleanup(c.doCleanups)
        self.case=c.case;self.task=c.task
        r=self.record();r["current_task"]["attempt_ref"]="fixture:attempt-7"
        e=c.event();e["effect_state"]="UNKNOWN";e["next_step"]["kind"]="RECONCILE"
        self.case.write_json("work_error.json",e)
        receipt,result=self.prepare(r)
        self.assertEqual(receipt["work_error_reflex"]["disposition"],"RECONCILE_SAME_ATTEMPT")
        self.assertFalse(receipt["work_error_reflex"]["next_material_step_qualified"])
        class Sender:
            calls=0
            def send_selected(inner,**kw):inner.calls+=1;raise AssertionError("no send")
        s=Sender()
        with self.assertRaisesRegex(ValueError,"WORK_ERROR_NEXT_MATERIAL_STEP_UNQUALIFIED"):
            normal._dispatch_selected(result,s)
        self.assertEqual(s.calls,0)
        self.assertFalse(Path(result["receipt"]).with_name(".receipt-dispatch-intent.json").exists())
        e["effect_state"]="NONE_OBSERVED"
        e["cause"]={"state":"PROVEN","layer":"LOCAL_ENVIRONMENT","evidence_ref":"fixture:exact-known-defect"}
        e["next_step"]["kind"]="QUICK_FIX"
        self.case.write_json("work_error.json",e)
        receipt,_=self.prepare(r,"known")
        self.assertEqual(receipt["work_error_reflex"]["disposition"],"DIRECT_KNOWN_FIX")
        self.assertIsNone(receipt["work_error_reflex"]["source_learning_encounter"])

    def test_composed_resume_capacity_consumer_and_unknown_preintent(self):
        capfixture=load("resume_capacity_fixture","tooling/validator/task_capacity_care_validation.py")
        capcase=capfixture.CapacityCareFlow();capcase.setUp();self.addCleanup(capcase.doCleanups)
        # Same fixture task; use actual E3 capture/reader implementations, not rebuilt capacity logic.
        captured=capcase.capture_capacity()
        self.case.captured=captured
        import hashlib
        profile_raw=Path(captured["capture_path"]).with_name("task_local_profile.json").read_bytes()
        record=self.record()
        record["learning"]["profile_binding_sha256"]=hashlib.sha256(profile_raw).hexdigest()
        record["current_task"]["attempt_ref"]="fixture:attempt-7"
        record["cases"]=[{"case_ref":"P23-D-BK11-DAY-POSTMORTEM-20261013-001",
          "scope_ref":"fixture:room-D","period_ref":"fixture:day-cut","analysis_owner_ref":"AD1/P23",
          "finish_criterion_ref":"fixture:followup","state":"ACTIVE","pending_obligations":["fixture:actual-use"]}]
        receipt,result=self.prepare(record,"composed")
        self.assertIn("task_capacity_care",receipt)
        self.assertEqual(receipt["work_resume"]["preserved_cases"],record["cases"])
        dispatch_path=Path(capcase.write_json("dispatch-owner.json",capcase.owner_record(stage="DISPATCH")))
        reader=capfixture.capacity.OwnerCapacityObservationReader(
              dispatch_record=dispatch_path,uptake_record=capcase.root/"uptake-NOT-PROVIDED.json")
        class CountReader:
            calls=0
            def read_dispatch(inner,binding):
                inner.calls+=1;return reader.read_dispatch(binding)
        class Sender:
            calls=0
            def send_selected(inner,**kw):
                inner.calls+=1
                return {"state":"ACCEPTED","recipient_ref":kw["recipient_ref"],
                  "selected_sha256":kw["selected_sha256"],"delivery_ref":"fixture:composed"}
        counted=CountReader();sender=Sender()
        normal._dispatch_selected(result,sender,capacity_reader=counted)
        self.assertEqual(counted.calls,2)
        self.assertEqual(sender.calls,1)
        # UNKNOWN reconciliation remains ahead of E3 capacity reads and any new intent.
        errors=load("composed_error_fixture","tooling/validator/work_error_reflex_validation.py")
        errcase=errors.WorkError();errcase.setUp();self.addCleanup(errcase.doCleanups)
        event=errcase.event()
        event["profile_binding_sha256"]=hashlib.sha256(profile_raw).hexdigest()
        event["effect_state"]="UNKNOWN";event["next_step"]["kind"]="RECONCILE"
        self.case.write_json("work_error.json",event)
        receipt,result=self.prepare(record,"composed-unknown")
        counted=CountReader();sender=Sender()
        with self.assertRaisesRegex(ValueError,"WORK_ERROR_NEXT_MATERIAL_STEP_UNQUALIFIED"):
            normal._dispatch_selected(result,sender,capacity_reader=counted)
        self.assertEqual(counted.calls,0);self.assertEqual(sender.calls,0)
        self.assertFalse(Path(result["receipt"]).with_name(".receipt-dispatch-intent.json").exists())


    def test_new_store_connections_evidence_replaces_stale_premise_no_client_inference(self):
        r=self.record()
        r["fact_delta"]={"premise_ref":"fixture:old-store-assumption",
            "supersedes_evidence_ref":"fixture:old-snapshot","current_evidence_ref":"fixture:new-owner-store-read",
            "store_state":"OWNER_REPORTED_CURRENT","connections":1,"subscriptions":65}
        receipt,_=self.prepare(r)
        facts=receipt["work_resume"]["fact_delta"]
        self.assertEqual(facts["current_evidence_ref"],"fixture:new-owner-store-read")
        self.assertEqual(facts["subscriptions"],65)
        self.assertIsNone(facts["active_clients"])
        errors=load("fresh_fact_error_fixture","tooling/validator/work_error_reflex_validation.py")
        c=errors.WorkError();c.setUp();self.addCleanup(c.doCleanups)
        self.case=c.case;self.task=c.task
        r=self.record();r["current_task"]["attempt_ref"]="fixture:attempt-7"
        r["fact_delta"]={"premise_ref":"fixture:old-store-assumption",
            "supersedes_evidence_ref":"fixture:old-snapshot","current_evidence_ref":"fixture:new-owner-store-read",
            "store_state":"OWNER_REPORTED_CURRENT","connections":1,"subscriptions":65}
        e=c.event();e["next_step"]["uses_premise_ref"]="fixture:old-store-assumption"
        self.case.write_json("work_error.json",e)
        with self.assertRaisesRegex(ValueError,"RESUME_STALE_PREMISE_AFTER_FRESH_FACT"):
            self.prepare(r,"stale-choice")
        self.assertFalse((self.case.root/"stale-choice").exists())



    def test_exact_current_pair_historical_refuses_before_output_intent_sender(self):
        class Sender:
            calls = 0
            def send_selected(inner, **kw):
                inner.calls += 1
                raise AssertionError("unexpected sender call")
        for state in ("CLOSED", "SUPERSEDED"):
            with self.subTest(state=state):
                r = self.record()
                r["current_task"]["closed_arcs"].append(self.task["task_ref"])
                r["current_blocks"].append({
                    "task_ref": self.task["task_ref"], "task_revision": self.task["task_revision"],
                    "state": state, "superseded_by":
                        r["owner_selection_ref"] if state == "SUPERSEDED" else None})
                sender = Sender()
                name = "historical-current-" + state.lower()
                with self.assertRaisesRegex(ValueError, "RESUME_CURRENT_PAIR_MARKED_HISTORICAL"):
                    _, result = self.prepare(r, name)
                    normal._dispatch_selected(result, sender)
                self.assertEqual(sender.calls, 0)
                self.assertFalse((self.case.root / name).exists())
                self.assertFalse(list(self.case.root.rglob(".receipt-dispatch-intent.json")))

    def test_same_task_ref_new_revision_preserves_closed_history_and_machine_order(self):
        old_revision = self.task["task_revision"]
        self.task = {**self.task, "task_revision": "fixture:new-machine-revision"}
        self.case.captured = fixture.capture.capture(self.case.root / "revision-capture",
            {"schema": fixture.capture.SCHEMA, "task": self.task}, self.case.fixture["parent.txt"])
        returned = {**self.case.returned, "task_revision": self.task["task_revision"]}
        self.case.return_file = self.case.write_json("revision-return.json",
            {"schema": normal.RETURN_SCHEMA, "observation_ref": "fixture:new-return", "return": returned})
        r = self.record()
        r["current_task"]["closed_arcs"].append(self.task["task_ref"])
        old = {"task_ref": self.task["task_ref"], "task_revision": old_revision,
               "state": "CLOSED", "superseded_by": None}
        r["current_blocks"].append(old)
        r["received_order"] = {"kind": "OWNER_SELECTED_NEW_ORDER",
            "selection_ref": r["owner_selection_ref"], "task_ref": self.task["task_ref"],
            "task_revision": self.task["task_revision"], "owner_ref": r["current_task"]["owner_ref"],
            "original_ref": "fixture:qualified-machine-order"}
        receipt, _ = self.prepare(r, "new-revision")
        self.assertTrue(receipt["work_resume"]["next_material_step_qualified"])
        self.assertIn(old, receipt["work_resume"]["history_blocks"])
        self.assertEqual(receipt["work_resume"]["received_order"]["original_ref"],
                         "fixture:qualified-machine-order")
        self.assertEqual(receipt["work_resume"]["authority"], "NONE")


if __name__=="__main__":
    unittest.main(verbosity=2)
