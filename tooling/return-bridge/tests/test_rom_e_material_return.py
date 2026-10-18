"""Changed E entry seam only. Inert synthetic episodes, never normal-use evidence."""
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

BRIDGE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("rom_e_entry_test", BRIDGE / "rom_e_material_return.py")
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)


def write_json(path, value):
    path.write_bytes(entry._canonical(value))


class ChangedSeam(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="e-entry-seam-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.request = self.root / "request.json"
        self.original = self.root / "original.txt"
        self.original.write_bytes(b"SYNTHETIC E task: produce inert material; no provider or host effect.\n")
        self.task = dict(task_ref="SYN-E-MATERIAL", task_revision="rev-test",
                         actor_ref="SYN-E2-PRODUCER", generation_ref="SYN-GEN",
                         carrier_ref="SYN-CARRIER", arc_ref="SYN-ARC", source_head="e" * 40,
                         effect_class="LOCAL_ONLY", privacy_class="SYNTHETIC_PUBLIC",
                         live_scope="NONE", authority_class="NONE", return_target="SYN-E1-OWNER",
                         way_home="SYN existing owner route", allowed_paths=["adapter.py"],
                         required_invariants=["no external effects"], stop_edges=["new authority required"])
        self.request_obj = {"schema": "cerebro-rom-a-dispatch-capture/v1", "task": self.task}
        write_json(self.request, self.request_obj)
        self.cap = entry.presend(request=str(self.request), original=str(self.original),
                                 capture_root=str(self.root / "captures"))
        self.capture = self.cap["capture_path"]
        self.observation = self.root / "return.json"
        self.obs = {"schema": "cerebro-rom-a-return-observation/v1", "observation_ref": "SYN-final-1",
                    "return": {**{k: self.task[k] for k in (
                        "task_ref", "task_revision", "actor_ref", "generation_ref", "carrier_ref", "arc_ref")},
                        "original_sha256": entry._sha(self.original.read_bytes()), "contract_kind": "FACT_RETURN"},
                    "facts": {"result_source_ref": "SYN-result", "completed_work": [{"ref": "SYN-done", "summary": "inert result"}],
                              "local_remainder": [], "responsibility": {"actor_ref": "SYN-E1-OWNER", "next_action": "dispose result"}}}
        write_json(self.observation, self.obs)
        self.outroot = self.root / "results"

    def run_entry(self, **options):
        return entry.finalize(capture_record=self.capture, return_input=str(self.observation),
                              output_root=str(self.outroot), **options)

    def manifest(self, result):
        return json.loads(Path(result["manifest"]).read_bytes())

    def cli(self, *args, script=None):
        return subprocess.run([sys.executable, "-I", "-B", str(script or BRIDGE / "rom_e_material_return.py"), *args],
                              capture_output=True, text=True, timeout=20)

    def repair(self):
        # Existing synthetic DATA only, not an old E1/core test campaign.
        sys.path.insert(0, str(entry.ROOT / "candidates/bk05-capsule-tool-v1/src"))
        from bk05_capsule import synthetic
        files = synthetic.positive_fixture()
        manifest = json.loads(files["parent_manifest.json"])
        self.original.write_bytes(files["parent.txt"])
        self.task = {k: manifest[k] for k in (
            "task_ref", "actor_ref", "effect_class", "privacy_class", "live_scope", "authority_class",
            "allowed_paths", "required_invariants", "stop_edges", "return_target", "way_home", "source_head")}
        self.task.update(actor_ref="SYN-E2-PRODUCER", task_ref="SYN-E-REPAIR", task_revision=manifest["parent_revision"],
                         generation_ref="SYN-GEN", carrier_ref="SYN-CARRIER", arc_ref="SYN-ARC")
        manifest.update(actor_ref=self.task["actor_ref"], task_ref=self.task["task_ref"])
        delta = json.loads(files["verifier_delta.json"])
        delta["requested_actor_ref"] = self.task["actor_ref"]
        write_json(self.request, {"schema": "cerebro-rom-a-dispatch-capture/v1", "task": self.task})
        self.cap = entry.presend(request=str(self.request), original=str(self.original),
                                 capture_root=str(self.root / "repair-captures"))
        self.capture = self.cap["capture_path"]
        self.obs = {"schema": "cerebro-rom-a-return-observation/v1", "observation_ref": "SYN-repair-1",
                    "return": {**{k: self.task[k] for k in (
                        "task_ref", "task_revision", "actor_ref", "generation_ref", "carrier_ref", "arc_ref")},
                        "original_sha256": entry._sha(files["parent.txt"]), "contract_kind": "REFINE"}}
        write_json(self.observation, self.obs)
        write_json(self.root / "parent_manifest.json", manifest)
        write_json(self.root / "verifier_delta.json", delta)
        (self.root / "verifier.txt").write_bytes(files["verifier.txt"])
        (self.root / "currentness.json").write_bytes(files["currentness.json"])
        return files

    def test_cli_presend_finalize_and_exact_repeat(self):
        pre = self.cli("presend", "--request", str(self.request), "--original", str(self.original),
                       "--capture-root", str(self.root / "captures"))
        self.assertEqual(pre.returncode, 0, pre.stdout + pre.stderr)
        self.assertEqual(json.loads(pre.stdout)["state"], "EXACT_REUSE")
        args = ("finalize", "--capture-record", self.capture, "--return-input", str(self.observation),
                "--output-root", str(self.outroot))
        first = self.cli(*args)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        result = json.loads(first.stdout)
        manifest = self.manifest(result)
        out = Path(result["manifest"]).parent
        receipt = json.loads((out / manifest["control_receipt"]).read_bytes())
        self.assertEqual(receipt["adoption"]["bk04"]["finding"]["structural_overall_status"], "PASS")
        self.assertEqual(receipt["adoption"]["bk04"]["state"], "UNKNOWN")
        self.assertEqual(receipt["adoption"]["actor_ref"], "SYN-E2-PRODUCER")
        self.assertEqual(manifest["recipient_use"], "NOT_OBSERVED")
        self.assertEqual((out / manifest["selected"]).read_bytes(), self.original.read_bytes())
        repeat = self.cli(*args)
        self.assertEqual(repeat.returncode, 0, repeat.stdout)
        self.assertEqual(json.loads(repeat.stdout)["state"], "EXACT_REUSE")

    def test_missing_incomplete_and_conflicting_facts_never_publish(self):
        cases = [None, {}, {**self.obs["facts"], "local_remainder": [{"ref": "SYN-done", "summary": "conflict"}]}]
        for facts in cases:
            with self.subTest(facts=facts):
                obj = copy.deepcopy(self.obs)
                if facts is None:
                    obj.pop("facts")
                else:
                    obj["facts"] = facts
                write_json(self.observation, obj)
                with self.assertRaises(ValueError):
                    self.run_entry()
                self.assertFalse(self.outroot.exists() and list(self.outroot.iterdir()))
        self.assertTrue(Path(self.capture).exists())

    def test_every_identity_and_hash_mismatch_refused(self):
        for key in self.obs["return"]:
            if key == "contract_kind":
                continue
            with self.subTest(key=key):
                obj = copy.deepcopy(self.obs)
                obj["return"][key] = "wrong"
                write_json(self.observation, obj)
                with self.assertRaisesRegex(ValueError, "MISMATCH"):
                    self.run_entry()

    def test_presend_divergent_original_same_key_conflicts(self):
        self.original.write_bytes(b"DIVERGENT SYNTHETIC original\n")
        with self.assertRaisesRegex(ValueError, "COLLISION"):
            entry.presend(request=str(self.request), original=str(self.original), capture_root=str(self.root / "captures"))

    def test_repair_without_held_basis_uses_full_with_review(self):
        files = self.repair()
        result = self.run_entry()
        manifest = self.manifest(result)
        out = Path(result["manifest"]).parent
        control = json.loads((out / manifest["control_receipt"]).read_bytes())
        self.assertEqual(control["selected"]["selection"], "BK05_STRUCTURAL_CAPSULE_CANDIDATE")
        self.assertEqual(manifest["selection"], "FULL_ORIGINAL_TASK")
        self.assertEqual((out / manifest["selected"]).read_bytes(), files["parent.txt"])
        self.assertEqual((out / "inputs/episode/verifier.txt").read_bytes(), files["verifier.txt"])

    def test_qualified_in_process_held_bytes_reconstruct(self):
        files = self.repair()
        def reader(task, original_sha, review_sha):
            self.assertEqual(task, self.task)
            self.assertEqual(original_sha, entry._sha(files["parent.txt"]))
            self.assertEqual(review_sha, entry._sha(files["verifier.txt"]))
            return "SYN-owner-qualified-current-not-real-memory-proof", files["parent.txt"], files["verifier.txt"]
        result = self.run_entry(held_basis_reader=reader)
        manifest = self.manifest(result)
        self.assertEqual(manifest["selection"], "BK05_STRUCTURAL_CAPSULE_CANDIDATE")
        self.assertEqual(manifest["recipient_use"], "NOT_OBSERVED")
        self.assertEqual(manifest["authority"], "NONE")

    def test_bool_cannot_enable_short_and_wrong_held_bytes_fall_back(self):
        self.repair()
        with self.assertRaisesRegex(ValueError, "NOT_FLAG"):
            self.run_entry(held_basis_reader=True)
        result = self.run_entry(held_basis_reader=lambda *args: ("SYN-current", b"wrong", b"wrong"))
        self.assertEqual(self.manifest(result)["selection"], "FULL_ORIGINAL_TASK")

    def test_stale_basis_never_becomes_full_sendable_reserve(self):
        self.repair()
        current = json.loads((self.root / "currentness.json").read_bytes())
        current["observed_source_head"] = "f" * 40
        write_json(self.root / "currentness.json", current)
        (self.root / "verifier_delta.json").unlink()  # Still stale when other review input is absent.
        with self.assertRaisesRegex(ValueError, "STALE_BASIS"):
            self.run_entry()

    def test_wrong_review_hash_refuses_and_cleans_own_output(self):
        self.repair()
        (self.root / "verifier.txt").write_bytes(b"Divergent SYNTHETIC review\n")
        with self.assertRaisesRegex(ValueError, "BK05_INVALID"):
            self.run_entry()
        self.assertFalse(list(self.outroot.iterdir()))
        self.assertTrue((self.root / "verifier.txt").exists())

    def test_same_key_different_input_and_corrupt_output_refuse(self):
        result = self.run_entry()
        self.obs["facts"]["completed_work"][0]["summary"] = "divergent"
        write_json(self.observation, self.obs)
        with self.assertRaisesRegex(ValueError, "IDEMPOTENCY_CONFLICT"):
            self.run_entry()
        self.obs["facts"]["completed_work"][0]["summary"] = "inert result"
        write_json(self.observation, self.obs)
        out = Path(result["manifest"]).parent
        (out / "original_task.txt").write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "OUTPUT_CONFLICT"):
            self.run_entry()

    def test_incomplete_prior_output_is_retained_for_same_attempt(self):
        key = entry._sha(entry._canonical([self.task["task_ref"], self.task["task_revision"], self.obs["observation_ref"]]))
        incomplete = self.outroot / ("return-" + key)
        incomplete.mkdir(parents=True)
        (incomplete / "incomplete.txt").write_bytes(b"SYN retained evidence")
        with self.assertRaisesRegex(ValueError, "OUTPUT_INCOMPLETE"):
            self.run_entry()
        self.assertTrue((incomplete / "incomplete.txt").exists())

    def test_cli_missing_dependency_and_invalid_input_are_sanitized(self):
        fake = self.root / "fake/tooling/return-bridge/rom_e_material_return.py"
        fake.parent.mkdir(parents=True)
        shutil.copyfile(BRIDGE / "rom_e_material_return.py", fake)
        missing = self.cli("presend", "--request", str(self.request), "--original", str(self.original),
                           "--capture-root", str(self.root / "unused"), script=fake)
        self.assertEqual(missing.returncode, 2)
        self.assertEqual(json.loads(missing.stdout)["state"], "REFUSED")
        self.observation.write_bytes(b'{"PRIVATE-SENTINEL":')
        bad = self.cli("finalize", "--capture-record", self.capture, "--return-input", str(self.observation),
                       "--output-root", str(self.outroot))
        self.assertEqual(bad.returncode, 2)
        self.assertNotIn("PRIVATE-SENTINEL", bad.stdout + bad.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
