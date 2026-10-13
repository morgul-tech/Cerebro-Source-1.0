"""Affected offline capture/consumer tests; fixture reports are not runtime attestations."""
from __future__ import annotations
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tooling/return-bridge"))


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


legacy = load("normal_profile_fixture", "tooling/validator/rom_a_normal_path_validation.py")
capture, normal = legacy.capture, legacy.normal
profile = load("profile_test", "tooling/return-bridge/task_local_profile.py")


class ProfileFlow(unittest.TestCase):
    def setUp(self):
        self.old = legacy.NormalPath()
        self.old.setUp()
        self.addCleanup(self.old.doCleanups)
        self.root = self.old.root
        self.task = copy.deepcopy(self.old.task)
        self.selected = {"source_ref": "fixture:library", "source_revision": "fixture:r1",
                         "primary_coat": "KODEMESTER", "lenses": ["SOURCEARKITEKT"],
                         "packages": [], "required_tools": ["source-files"]}
        self.cap = capture.capture(self.root / "profile-captures",
                    {"schema": capture.SCHEMA, "task": self.task},
                    self.old.fixture["parent.txt"], profile=self.selected)
        self.sidecar = Path(self.cap["capture_path"]).with_name("task_local_profile.json")
        self.bound = json.loads(self.sidecar.read_bytes())

    def observe(self):
        return {"schema": profile.SCHEMA + "-observation",
                "binding_sha256": profile.sha(self.sidecar.read_bytes()),
                **{k: self.task[k] for k in ("task_ref", "task_revision", "actor_ref", "generation_ref")},
                "profile_source_ref": self.selected["source_ref"],
                "profile_source_revision": self.selected["source_revision"],
                "uptake": {"state": "USED", "evidence_ref": "fixture:actual-uptake"},
                "result": {"state": "DELIVERED", "evidence_ref": "fixture:result"},
                "recipient_use": {"state": "USED", "evidence_ref": "fixture:recipient-read"},
                "relevant_delta": {"state": "REPORTED", "evidence_ref": "fixture:delta-care"},
                "available_tools": [{"tool_ref": "source-files", "state": "AVAILABLE",
                                     "evidence_ref": "fixture:tool-observation"}]}

    def run_normal(self, observation=None, suffix="output"):
        kw = {"capture_record": self.cap["capture_path"], "return_input": self.old.return_file,
              "out_dir": str(self.root / suffix)}
        if observation is not None:
            kw["profile_observation"] = self.old.write_json(suffix + "-observation.json", observation)
        result = normal.prepare(**kw)
        return json.loads(Path(result["receipt"]).read_bytes()), result

    def test_v1_original_receipt_and_refusal_unchanged(self):
        self.assertEqual(Path(self.cap["capture_path"]).read_bytes(),
                         Path(self.old.captured["capture_path"]).read_bytes())
        self.assertEqual(Path(self.cap["original_path"]).read_bytes(), self.old.fixture["parent.txt"])
        request = {"schema": capture.SCHEMA, "task": {**self.task, "profile": self.selected}}
        with self.assertRaisesRegex(ValueError, "TASK_FIELDS_INVALID"):
            capture.capture(self.root / "bad", request, b"original")
        self.assertFalse((self.root / "bad").exists())

    def test_proposed_does_not_become_loaded_or_used(self):
        receipt, result = self.run_normal()
        value = receipt["task_local_profile"]
        self.assertEqual(value["uptake"]["state"], "NOT_ATTESTED")
        self.assertEqual(value["recipient_use"]["state"], "NOT_OBSERVED")
        self.assertEqual(receipt["recipient_use"]["state"], "NOT_OBSERVED")
        self.assertFalse(value["identity_attested"])
        self.assertEqual(Path(result["selected"]).read_bytes(), self.old.fixture["parent.txt"])

    def test_actual_report_consumed_without_grant_or_attestation(self):
        receipt, _ = self.run_normal(self.observe())
        value = receipt["task_local_profile"]
        self.assertEqual(value["uptake"]["state"], "USED")
        self.assertEqual(value["recipient_use"]["state"], "USED")
        self.assertEqual(value["relevant_delta"]["evidence_ref"], "fixture:delta-care")
        self.assertEqual(value["authority"], "NONE")
        self.assertFalse(value["tools_attested"])
        self.assertFalse(value["recipient_use_attested"])

    def test_same_worker_two_tasks_switch_profile_without_birth(self):
        task = {**self.task, "task_ref": self.task["task_ref"] + "-second"}
        second = {**self.selected, "primary_coat": "SERVERARKITEKT", "source_revision": "fixture:r2"}
        c = capture.capture(self.root / "second", {"schema": capture.SCHEMA, "task": task},
                            b"second original", profile=second)
        b = json.loads(Path(c["capture_path"]).with_name("task_local_profile.json").read_bytes())
        self.assertEqual(b["task"]["actor_ref"], self.bound["task"]["actor_ref"])
        self.assertEqual(b["task"]["generation_ref"], self.bound["task"]["generation_ref"])
        self.assertNotEqual(b["profile"], self.bound["profile"])
        self.assertEqual(b["authority"], "NONE")

    def test_wrong_task_worker_generation_and_profile_revision_refused(self):
        for field in ("task_ref", "task_revision", "actor_ref", "generation_ref",
                      "profile_source_ref", "profile_source_revision", "binding_sha256"):
            with self.subTest(field=field):
                o = self.observe()
                o[field] += "-wrong"
                with self.assertRaisesRegex(ValueError, "PROFILE_OBSERVATION_BINDING_MISMATCH"):
                    self.run_normal(o, field)
                self.assertFalse((self.root / field).exists())

    def test_missing_used_uptake_evidence_refused(self):
        o = self.observe()
        o["uptake"]["evidence_ref"] = None
        with self.assertRaisesRegex(ValueError, "PROFILE_TEXT_REQUIRED"):
            self.run_normal(o)

    def test_required_unavailable_tool_is_precise_local_refusal(self):
        o = self.observe()
        o["available_tools"][0]["state"] = "UNAVAILABLE"
        with self.assertRaisesRegex(ValueError, "^PROFILE_REQUIRED_TOOL_UNAVAILABLE:source-files$"):
            self.run_normal(o)
        self.assertFalse((self.root / "output").exists())

    def test_unknown_required_tool_cannot_support_used(self):
        o = self.observe()
        o["available_tools"] = []
        with self.assertRaisesRegex(ValueError, "^PROFILE_REQUIRED_TOOL_NOT_ATTESTED:source-files$"):
            self.run_normal(o)

    def test_unknowns_preserved_loaded_separate_from_used(self):
        o = self.observe()
        o["uptake"]["state"] = "LOADED"
        o["available_tools"] = [{"tool_ref": "source-files", "state": "NOT_ATTESTED",
                                "evidence_ref": None}]
        o["result"] = o["recipient_use"] = o["relevant_delta"] = {"state": "NOT_OBSERVED", "evidence_ref": None}
        receipt, _ = self.run_normal(o)
        self.assertEqual(receipt["task_local_profile"]["uptake"]["state"], "LOADED")
        self.assertEqual(receipt["task_local_profile"]["recipient_use"]["state"], "NOT_OBSERVED")

    def test_sidecar_scope_or_byte_manipulation_refused(self):
        value = copy.deepcopy(self.bound)
        value["task"]["effect_class"] = "NEW_GRANT"
        self.sidecar.write_bytes(profile.canonical(value))
        with self.assertRaisesRegex(ValueError, "PROFILE_CAPTURE_BINDING_MISMATCH"):
            self.run_normal()
        self.sidecar.write_bytes(profile.canonical(self.bound) + b" ")
        with self.assertRaisesRegex(ValueError, "PROFILE_CAPTURE_BINDING_MISMATCH"):
            self.run_normal()

    def test_history_not_retrofitted_and_profile_collision_refused(self):
        with self.assertRaisesRegex(ValueError, "PROFILE_SIDECAR_COLLISION"):
            capture.capture(self.root / "captures", {"schema": capture.SCHEMA, "task": self.task},
                            self.old.fixture["parent.txt"], profile=self.selected)
        changed = {**self.selected, "source_revision": "fixture:wrong"}
        with self.assertRaisesRegex(ValueError, "PROFILE_SIDECAR_COLLISION"):
            capture.capture(self.root / "profile-captures",
                            {"schema": capture.SCHEMA, "task": self.task},
                            self.old.fixture["parent.txt"], profile=changed)

    def test_duplicate_keys_refused(self):
        with self.assertRaisesRegex(ValueError, "PROFILE_DUPLICATE_KEY"):
            profile.decode(b'{"source_ref":"one","source_ref":"two"}')

    def test_ordinary_cli_dispatch_and_normal_consumer(self):
        req = self.old.write_json("request.json", {"schema": capture.SCHEMA, "task": self.task})
        prof = self.old.write_json("profile.json", self.selected)
        original = self.old.write("original.txt", self.old.fixture["parent.txt"])
        command = [sys.executable, "-B", str(ROOT / "tooling/return-bridge/rom_a_dispatch_capture.py"),
                   "--request", req, "--original", original, "--capture-root", str(self.root / "cli"),
                   "--profile-binding", prof]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = json.loads(result.stdout)
        self.assertTrue(Path(output["profile_binding_path"]).is_file())
        result = subprocess.run([sys.executable, "-B", str(ROOT / "tooling/return-bridge/rom_a_normal_path.py"),
                   "--capture-record", output["capture_path"], "--return-input", self.old.return_file,
                   "--out-dir", str(self.root / "cli-normal")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipt = json.loads(Path(json.loads(result.stdout)["receipt"]).read_bytes())
        self.assertEqual(receipt["task_local_profile"]["uptake"]["state"], "NOT_ATTESTED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
