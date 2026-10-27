"""Affected ordinary caller seam; external Codex tool is explicitly inert."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

BRIDGE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("capacity_dispatch_entry", BRIDGE / "rom_e_material_return.py")
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)


class OrdinaryDispatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ordinary-dispatch-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.task = dict(task_ref="fixture:whole-component", task_revision="r1", actor_ref="D1",
                         generation_ref="g1", carrier_ref="chat-D1", arc_ref="fixture:arc",
                         source_head="b" * 40, effect_class="LOCAL_PREPARATION",
                         privacy_class="SYNTHETIC", live_scope="no external effect",
                         authority_class="NONE", return_target="PM", way_home="CURRENT_PM",
                         allowed_paths=["own-component"], required_invariants=["one writer"],
                         stop_edges=["actual authority boundary"])
        self.write("request.json", {"schema": "cerebro-rom-a-dispatch-capture/v1", "task": self.task})
        (self.root / "original.txt").write_bytes(b"One complete component.\n")
        self.calls = []
        self.observations = {}

    def write(self, name, value):
        path = self.root / name
        path.write_bytes(entry._canonical(value))
        return str(path)

    def worker(self, actor, busy=False, own=False):
        return dict(actor_ref=actor, generation_ref="g1", state="RUNNING" if busy else "AVAILABLE",
                    fit="FIT", reservation_ref="fixture:reservation" if busy else None,
                    reservation_owner_ref=self.task["task_ref"] if own else "fixture:other" if busy else None,
                    evidence_ref="fixture:owner-current:" + actor, fit_evidence_ref="fixture:fit:" + actor,
                    profile_source_ref="fixture:profile:" + actor, profile_source_revision="r1",
                    ui_state="IDLE", current_work=dict(state="ACTIVE" if busy else "NONE",
                    task_ref=self.task["task_ref"] if own else "fixture:other" if busy else None,
                    task_revision="r1" if busy else None, attempt_ref="fixture:attempt" if own else "other" if busy else None,
                    currentness="CURRENT", evidence_ref="fixture:current-work:" + actor),
                    pending_bindings=[], pending_currentness="CURRENT", pending_evidence_ref="fixture:pending:" + actor)

    def setup_candidates(self, *, busy=True, own=False, kind="TASK_ASSIGNMENT"):
        workers = [self.worker("D1", busy, own), self.worker("D3")]
        candidates = []
        for actor in ("D1", "D3"):
            profile = dict(source_ref="fixture:profile:" + actor, source_revision="r1",
                           primary_coat="producer", lenses=[], packages=[], required_tools=[])
            obs = dict(schema="cerebro-task-capacity-care/v2-observation", cut_ref="fixture:cut",
                       cut_revision="r1", observer_ref="fixture:PM-own-observation",
                       task_ref=self.task["task_ref"], task_revision="r1", attempt_ref="fixture:attempt",
                       input_kind=kind, selected_actor_ref=actor, work_phase="PREPARATION",
                       workers=workers, prior_care_readback=None,
                       owner_port_contract=dict(case_ref="fixture:existing-work",
                         governing_basis_refs=["fixture:order"], applicable_control_refs=["fixture:scope"],
                         requirements=[dict(port_ref="fixture:owner-port", function_ref="fixture:publication",
                           effect_scope="no live effect", owner_ref="fixture:publication-owner",
                           priority_owner_ref="PM", preparation_actor_ref=actor,
                           prepared_input_ref="fixture:component", next_consumer_ref="fixture:publication-owner")],
                         ports=[], owner_disposition=[]))
            self.observations[actor] = obs
            path = self.write(actor + "-capacity.json", obs)
            candidates.append(dict(actor_ref=actor, generation_ref="g1", carrier_ref="chat-" + actor,
                                   profile=profile, capacity_observation=path))
        self.write("candidates.json", candidates)
        return candidates

    def send(self, **args):
        self.calls.append(args)
        return {"content": [{"type": "text", "text": json.dumps({"threadId": args["threadId"]})}],
                "isError": False}

    def reader(self, drift=False):
        outer = self
        class Reader:
            count = 0
            def read_dispatch(self, bound):
                self.count += 1
                actor = bound["task"]["actor_ref"]
                obs = copy.deepcopy(outer.observations[actor])
                if drift and self.count == 2:
                    obs["workers"] = [outer.worker("D1", True), outer.worker("D3", True)]
                return {"provenance": {"owner_ref": "fixture:PM", "record_ref": "fixture:actual-owner-cut"},
                        "observation_raw": entry._canonical(obs)}
        return Reader()

    def dispatch(self, reader=None, send=None):
        sender = entry.CodexThreadSender(send or self.send, {"D1": "chat-D1", "D3": "chat-D3"})
        return entry.dispatch_task(request=str(self.root / "request.json"),
            original=str(self.root / "original.txt"), candidates=str(self.root / "candidates.json"),
            capture_root=str(self.root / "captures"), dispatch_root=str(self.root / "dispatch"),
            sender=sender, capacity_reader=reader or self.reader())

    def test_busy_known_actor_selects_available_profile_and_actual_host_arguments(self):
        self.setup_candidates()
        result = self.dispatch()
        self.assertEqual(result["dispatch"], "SENT_ACCEPTED")
        self.assertEqual(self.calls, [dict(threadId="chat-D3", prompt="One complete component.\n")])
        receipt = json.loads(Path(result["receipt"]).read_bytes())
        capture = Path(receipt["capture_ref"])
        self.assertEqual(json.loads(capture.read_bytes())["task"]["actor_ref"], "D3")
        self.assertEqual(json.loads(capture.with_name("task_local_profile.json").read_bytes())["profile"]["source_ref"], "fixture:profile:D3")
        self.assertEqual(receipt["selection_basis"]["rejected"][0]["actor_ref"], "D1")
        self.assertEqual(receipt["task_capacity_care"]["dispatch_recheck"]["state"], "PASS_LOCAL_OBSERVATION_BEFORE_INTENT_AND_SENDER")

    def test_same_attempt_continuation_keeps_busy_owner_and_never_sends_twice(self):
        self.setup_candidates(own=True, kind="SAME_TASK_CONTINUATION")
        first = self.dispatch()
        second = self.dispatch()
        self.assertEqual(self.calls[0]["threadId"], "chat-D1")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(second["dispatch"], "EXACT_SENT_REUSE")
        self.assertEqual(first["delivery_ref"], second["delivery_ref"])

    def test_short_mechanical_action_is_one_complete_send(self):
        self.setup_candidates(busy=False)
        (self.root / "original.txt").write_bytes(b"Publish the reviewed commit through existing route.\n")
        self.dispatch()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["prompt"], "Publish the reviewed commit through existing route.\n")

    def test_current_pending_drift_blocks_before_actual_tool(self):
        self.setup_candidates()
        with self.assertRaisesRegex(ValueError, "CAPACITY_CHANGED_BEFORE_SENDER_NO_RETRY"):
            self.dispatch(reader=self.reader(drift=True))
        with self.assertRaises(ValueError):
            self.dispatch()
        self.assertEqual(self.calls, [])

    def test_unknown_tool_outcome_is_not_redispatched(self):
        self.setup_candidates()
        def unknown(**args):
            self.calls.append(args)
            raise TimeoutError("synthetic external boundary")
        with self.assertRaises(TimeoutError):
            self.dispatch(send=unknown)
        with self.assertRaises(ValueError):
            self.dispatch()
        self.assertEqual(len(self.calls), 1)

    def test_cli_presend_actually_forwards_profile_and_capacity(self):
        candidates = self.setup_candidates(busy=False)
        profile = self.write("profile.json", candidates[0]["profile"])
        result = subprocess.run([sys.executable, "-I", "-B", str(BRIDGE / "rom_e_material_return.py"),
            "presend", "--request", str(self.root / "request.json"), "--original", str(self.root / "original.txt"),
            "--capture-root", str(self.root / "cli-captures"), "--profile", profile,
            "--capacity-observation", candidates[0]["capacity_observation"]], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        path = Path(json.loads(result.stdout)["capture_path"])
        self.assertTrue(path.with_name("task_local_profile.json").is_file())
        self.assertTrue(path.with_name("task_capacity_care.json").is_file())
        self.assertEqual(self.calls, [])

    def test_selected_carrier_must_match_actual_host_thread_binding(self):
        candidates = self.setup_candidates()
        candidates[1]["carrier_ref"] = "wrong-chat"
        self.write("candidates.json", candidates)
        with self.assertRaisesRegex(ValueError, "HOST_THREAD_CARRIER_BINDING_MISMATCH"):
            self.dispatch()
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
