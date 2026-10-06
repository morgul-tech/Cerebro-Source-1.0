"""One bounded PR79/81/82 normal-entry and CLI-boundary integration check.

Synthetic-only. It verifies that the composed Source path consumes the preserved PR73 capture
and typed return file directly, while keeping the exact-original fallback and all authority/use
ceilings. It makes no provider call or external write.
"""

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
CAPTURE_PATH = ROOT / "tooling/return-bridge/rom_a_dispatch_capture.py"
NORMAL_CLI = ROOT / "tooling/return-bridge/rom_a_return_adoption.py"
REVIEW_CLI = ROOT / "tooling/return-bridge/rom_a_review_input.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("MODULE_NOT_IMPORTABLE")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


capture = load_module("rom_a_integration_capture", CAPTURE_PATH)
review_input = load_module("rom_a_integration_review_input", REVIEW_CLI)


class CombinedNormalEntry(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="roma-pr79-81-82-integration-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.original = b"Exact synthetic dispatch\r\nkeep bytes, spacing and newline\n"
        self.task = {
            "task_ref": "SYN-PR79-81-82-TASK", "task_revision": "rev-1",
            "actor_ref": "SYN-ACTOR", "generation_ref": "SYN-GENERATION",
            "carrier_ref": "SYN-CARRIER", "arc_ref": "SYN-ARC",
            "source_head": "a" * 40, "effect_class": "NONE", "privacy_class": "SYNTHETIC",
            "live_scope": "LOCAL", "authority_class": "NONE", "return_target": "SYN-OWNER",
            "way_home": "SYN-RETURN", "allowed_paths": ["example.py"],
            "required_invariants": ["exact original fallback"], "stop_edges": ["identity changed"],
        }
        self.captured = capture.capture(
            self.root / "captures", {"schema": capture.SCHEMA, "task": self.task}, self.original)
        capture_record = json.loads(Path(self.captured["capture_path"]).read_text(encoding="utf-8"))
        parent_manifest = {
            "task_ref": self.task["task_ref"], "parent_revision": self.task["task_revision"],
            "parent_sha256": hashlib.sha256(self.original).hexdigest(),
            **{name: self.task[name] for name in (
                "actor_ref", "effect_class", "privacy_class", "live_scope", "authority_class",
                "allowed_paths", "required_invariants", "stop_edges", "return_target", "way_home", "source_head")},
            "semantic_review": {"status": "OWNER_REVIEWED", "receipt_ref": "SYN-CURRENT-PARENT-REVIEW"},
        }
        self.parent_manifest = self.root / "parent_manifest.json"
        self.parent_manifest.write_text(json.dumps(parent_manifest, sort_keys=True) + "\n", encoding="utf-8")
        self.return_input = self.root / "return.json"
        self.return_input.write_text(json.dumps({
            "schema": "cerebro-rom-a-return-observation/v1", "observation_ref": "SYN-ACTUAL-RETURN-FILE",
            "return": {
                **{name: self.task[name] for name in (
                    "actor_ref", "generation_ref", "carrier_ref", "arc_ref", "task_ref", "task_revision")},
                "original_sha256": hashlib.sha256(self.original).hexdigest(), "contract_kind": "REPAIR",
            },
        }, sort_keys=True) + "\n", encoding="utf-8")
        self.capture_record = Path(self.captured["capture_path"])
        self.assertEqual(capture_record["semantic_review"]["status"], "UNREVIEWED")

    def test_normal_cli_consumes_preserved_capture_and_typed_return_without_bundle_rebuild(self):
        output = self.root / "normal-output"
        completed = subprocess.run([
            sys.executable, "-B", str(NORMAL_CLI), "--capture-record", str(self.capture_record),
            "--return-input", str(self.return_input), "--parent-manifest", str(self.parent_manifest),
            "--out-dir", str(output),
        ], cwd=ROOT, capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        result = json.loads(completed.stdout)
        receipt = json.loads(Path(result["receipt"]).read_text(encoding="utf-8"))
        self.assertEqual(result["selection"], "FULL_ORIGINAL_TASK")
        self.assertEqual(Path(result["selected"]).read_bytes(), self.original)
        self.assertEqual(receipt["capture"]["semantic_review"]["status"], "UNREVIEWED")
        self.assertEqual(receipt["parent_manifest"]["basis"], "SUPPLIED_REVIEW_NOT_AUTHENTICATED_HERE")
        self.assertEqual(receipt["return_input"]["authority"], "CALLER_SUPPLIED_NOT_PROVIDER_VERIFIED")
        self.assertIn("verifier", receipt["missing_inputs"])
        self.assertIn("verifier_delta", receipt["missing_inputs"])
        self.assertEqual(receipt["authority"], "NONE")
        self.assertFalse(receipt["semantic_review_proven"])
        self.assertFalse(receipt["provider_currentness_proven"])
        self.assertFalse(receipt["adoption"]["work_consumed"])
        self.assertEqual(receipt["recipient_use"]["state"], "NOT_OBSERVED")

    def test_review_input_cli_imports_with_normal_path_and_keeps_exact_gap(self):
        evidence = self.root / "empty-evidence"
        evidence.mkdir()
        output = self.root / "review-gap"
        completed = subprocess.run([
            sys.executable, "-B", str(REVIEW_CLI), "--evidence-dir", str(evidence),
            "--source-root", str(ROOT), "--out-dir", str(output),
        ], cwd=ROOT, capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        result = json.loads(completed.stdout)
        gap = json.loads((output / "gap.json").read_text(encoding="utf-8"))
        self.assertEqual(result["state"], "EXACT_INPUT_GAP")
        self.assertEqual(gap["authority"], "NONE")
        self.assertEqual(gap["host_binding"], "NONE")
        self.assertIn("NO_CAPSULE", gap["limits"])
        self.assertFalse((output / "capsule.json").exists())

    def test_combined_review_import_preserves_supplied_actor_and_verifier_conflicts(self):
        verifier_hash = "b" * 64
        supplied = {"requested_actor_ref": "REVIEWER-SUPPLIED-ACTOR", "verifier_sha256": "c" * 64,
                    "required_test_delta": ["synthetic focused check"]}
        before = json.dumps(supplied, sort_keys=True)
        preserved, repairs, conflicts = review_input._bind_missing_producer_fields(
            supplied, "EXPECTED-ACTOR", verifier_hash)
        self.assertEqual(json.dumps(supplied, sort_keys=True), before)
        self.assertEqual(preserved, supplied)
        self.assertFalse(repairs)
        self.assertEqual({row["field"] for row in conflicts}, {"requested_actor_ref", "verifier_sha256"})


if __name__ == "__main__":
    unittest.main()
