"""Focused checks for PR73 capture -> BK04/BK05 local normal return selection."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


def module(name: str, relative: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


capture = module("capture_normal_test", "tooling/return-bridge/rom_a_dispatch_capture.py")
normal = module("normal_test", "tooling/return-bridge/rom_a_normal_path.py")
sys.path.insert(0, str(ROOT / "candidates/bk05-capsule-tool-v1/src"))
from bk05_capsule.synthetic import positive_fixture  # noqa: E402


class NormalPath(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.fixture = positive_fixture()
        manifest = json.loads(self.fixture["parent_manifest.json"])
        self.task = {
            "task_ref": manifest["task_ref"], "task_revision": manifest["parent_revision"],
            "actor_ref": manifest["actor_ref"], "generation_ref": "SYN-GEN",
            "carrier_ref": "SYN-CARRIER", "arc_ref": "SYN-ARC",
            **{name: manifest[name] for name in normal.PARENT_FIELDS},
        }
        request = {"schema": capture.SCHEMA, "task": self.task}
        self.captured = capture.capture(self.root / "captures", request, self.fixture["parent.txt"])
        self.returned = {name: self.task[name] for name in
                         ("actor_ref", "generation_ref", "carrier_ref", "arc_ref",
                          "task_ref", "task_revision")}
        self.returned["original_sha256"] = manifest["parent_sha256"]
        self.returned["contract_kind"] = "REPAIR"
        self.return_file = self.write_json("return.json", {
            "schema": normal.RETURN_SCHEMA, "observation_ref": "SYN-OBS-1",
            "return": self.returned,
        })

    def write(self, name: str, raw: bytes) -> str:
        path = self.root / name
        path.write_bytes(raw)
        return str(path)

    def write_json(self, name: str, value: object) -> str:
        return self.write(name, (json.dumps(value, sort_keys=True) + "\n").encode())

    def kwargs(self, name: str = "result") -> dict[str, str]:
        return {"capture_record": self.captured["capture_path"],
                "return_input": self.return_file, "out_dir": str(self.root / name)}

    def bk05_files(self) -> dict[str, str]:
        return {
            "parent_manifest": self.write("manifest.json", self.fixture["parent_manifest.json"]),
            "verifier": self.write("verifier.txt", self.fixture["verifier.txt"]),
            "verifier_delta": self.write("delta.json", self.fixture["verifier_delta.json"]),
            "currentness": self.write("currentness.json", self.fixture["currentness.json"]),
        }

    def test_missing_producer_inputs_keeps_exact_full_original_and_marks_gap(self):
        result = normal.prepare(**self.kwargs())
        receipt = json.loads(Path(result["receipt"]).read_text(encoding="utf-8"))
        self.assertEqual(result["selection"], "FULL_ORIGINAL_TASK")
        self.assertEqual(Path(result["selected"]).read_bytes(), self.fixture["parent.txt"])
        self.assertIn("qualified_parent_review_manifest", result["missing_inputs"])
        self.assertIn("verifier_delta", result["missing_inputs"])
        self.assertEqual(receipt["parent_manifest"]["basis"], "DERIVED_UNREVIEWED")
        self.assertEqual(receipt["recipient_use"]["state"], "NOT_OBSERVED")
        self.assertFalse(receipt["selected"]["recipient_delivery_qualified"])
        self.assertFalse(receipt["semantic_review_proven"])
        self.assertFalse(receipt["provider_currentness_proven"])
        self.assertEqual(receipt["authority"], "NONE")

    def test_local_capsule_is_structural_only_without_recipient_use(self):
        result = normal.prepare(**self.kwargs(), **self.bk05_files())
        receipt = json.loads(Path(result["receipt"]).read_text(encoding="utf-8"))
        self.assertEqual(result["selection"], "BK05_STRUCTURAL_CAPSULE_CANDIDATE")
        self.assertEqual(receipt["adoption"]["bk05"]["decision"], "LOCAL_CAPSULE_CANDIDATE")
        self.assertEqual(receipt["recipient_use"]["state"], "NOT_OBSERVED")
        self.assertEqual(receipt["authority"], "NONE")
        self.assertFalse(receipt["selected"]["recipient_delivery_qualified"])
        self.assertEqual(receipt["selected"]["sha256"],
                         hashlib.sha256(Path(result["selected"]).read_bytes()).hexdigest())

    def test_new_actor_preserves_full_original_even_with_complete_delta(self):
        self.returned["actor_ref"] = "SYN-NEW-ACTOR"
        self.return_file = self.write_json("return.json", {
            "schema": normal.RETURN_SCHEMA, "observation_ref": "SYN-OBS-2", "return": self.returned})
        result = normal.prepare(**self.kwargs(), **self.bk05_files())
        self.assertEqual(result["selection"], "FULL_ORIGINAL_TASK")
        self.assertEqual(Path(result["selected"]).read_bytes(), self.fixture["parent.txt"])

    def test_new_task_kind_preserves_full_original_even_with_complete_delta(self):
        self.returned["contract_kind"] = "NEW_TASK"
        self.return_file = self.write_json("return.json", {
            "schema": normal.RETURN_SCHEMA, "observation_ref": "SYN-NEW-1", "return": self.returned})
        result = normal.prepare(**self.kwargs(), **self.bk05_files())
        self.assertEqual(result["selection"], "FULL_ORIGINAL_TASK")
        self.assertEqual(Path(result["selected"]).read_bytes(), self.fixture["parent.txt"])

    def test_stale_currentness_preserves_full_original(self):
        files = self.bk05_files()
        observed = json.loads(self.fixture["currentness.json"])
        observed["observed_parent_revision"] = "stale"
        files["currentness"] = self.write_json("currentness.json", observed)
        result = normal.prepare(**self.kwargs(), **files)
        self.assertEqual(result["selection"], "FULL_ORIGINAL_TASK")
        self.assertEqual(Path(result["selected"]).read_bytes(), self.fixture["parent.txt"])

    def test_fact_return_runs_bk04_at_relevant_return_without_global_stop(self):
        self.returned["contract_kind"] = "FACT_RETURN"
        self.return_file = self.write_json("return.json", {
            "schema": normal.RETURN_SCHEMA, "observation_ref": "SYN-FACT-1", "return": self.returned})
        result = normal.prepare(**self.kwargs())
        receipt = json.loads(Path(result["receipt"]).read_text(encoding="utf-8"))
        self.assertEqual(receipt["adoption"]["bk04"]["state"], "UNKNOWN")
        self.assertFalse(receipt["adoption"]["bk04"]["blocks_other_work"])
        self.assertIn("owner_facts", result["missing_inputs"])
        self.assertEqual(result["selection"], "FULL_ORIGINAL_TASK")

    def test_tampered_capture_and_duplicate_return_refused_without_output(self):
        original = Path(self.captured["original_path"])
        original.write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "CAPTURE_READBACK_MISMATCH"):
            normal.prepare(**self.kwargs())
        self.assertFalse((self.root / "result").exists())
        original.write_bytes(self.fixture["parent.txt"])
        self.return_file = self.write("return.json", b'{"schema":"x","schema":"y"}')
        with self.assertRaisesRegex(ValueError, "DUPLICATE_JSON_KEY"):
            normal.prepare(**self.kwargs())
        self.assertFalse((self.root / "result").exists())

    def test_parent_manifest_must_match_captured_scope(self):
        files = self.bk05_files()
        manifest = json.loads(self.fixture["parent_manifest.json"])
        manifest["way_home"] = "foreign route"
        files["parent_manifest"] = self.write_json("manifest.json", manifest)
        with self.assertRaisesRegex(ValueError, "PARENT_MANIFEST_CAPTURE_MISMATCH"):
            normal.prepare(**self.kwargs(), **files)

    def test_existing_result_is_not_overwritten(self):
        kwargs = self.kwargs()
        first = normal.prepare(**kwargs)
        before = Path(first["receipt"]).read_bytes()
        with self.assertRaisesRegex(ValueError, "OUTPUT_TARGET_INVALID"):
            normal.prepare(**kwargs)
        self.assertEqual(Path(first["receipt"]).read_bytes(), before)

    def test_cli_normal_invocation_emits_receipt_without_use_claim(self):
        kwargs = self.kwargs()
        completed = subprocess.run([
            sys.executable, "-B", str(ROOT / "tooling/return-bridge/rom_a_return_adoption.py"),
            "--capture-record", kwargs["capture_record"], "--return-input", kwargs["return_input"],
            "--out-dir", kwargs["out_dir"],
        ], capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        emitted = json.loads(completed.stdout)
        self.assertEqual(emitted["recipient_use"], "NOT_OBSERVED")
        self.assertEqual(Path(emitted["selected"]).read_bytes(), self.fixture["parent.txt"])

    def test_bounded_context_normal_return_exposes_only_selected_tab(self):
        context = {"content_ref": "doc:1", "selector_kind": "TAB", "selector": "current"}
        class Reader:
            def read_current(self, content_ref, kind, selector):
                return {"content_ref": content_ref, "selector_kind": kind, "selector": selector,
                        "revision": "r1", "authority_ref": "source:1", "currentness": "CURRENT",
                        "provider_readback_verified": True, "provenance_refs": ["provider:1"],
                        "parts": {"current": "Current answer", "history": "Unselected history"}}
        path = self.write_json("context.json", context)
        result = normal.prepare(**self.kwargs(), bounded_context=path, bounded_provider_reader=Reader())
        receipt = json.loads(Path(result["receipt"]).read_text(encoding="utf-8"))
        selected = Path(result["receipt"]).parent / receipt["bounded_context"]["file"]
        self.assertEqual(selected.read_text(encoding="utf-8"), "Current answer")
        self.assertNotIn("Unselected history", receipt["bounded_context"].__str__())
        self.assertEqual(receipt["recipient_use"]["state"], "NOT_OBSERVED")

    def test_caller_claimed_bounded_context_cannot_create_return(self):
        context = {"content_ref": "doc:1", "revision": "r1", "authority_ref": "source:1",
                   "selector_kind": "TAB", "selector": "current", "currentness": "STALE",
                   "provider_readback_verified": True, "provenance_refs": ["provider:1"],
                   "parts": {"current": "Stale answer"}}
        with self.assertRaisesRegex(ValueError, "trusted-provider-reader-required"):
            normal.prepare(**self.kwargs(), bounded_context=self.write_json("context.json", context))
        self.assertFalse((self.root / "result").exists())


if __name__ == "__main__":
    unittest.main()
