"""Focused integration checks for the ordinary Rom A return selection seam."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
ADAPTER = ROOT / "tooling/return-bridge/rom_a_return_adoption.py"
spec = importlib.util.spec_from_file_location("rom_a_return_adoption", ADAPTER)
assert spec is not None and spec.loader is not None
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)
sys.path.insert(0, str(ROOT / "candidates/bk05-capsule-tool-v1/src"))
sys.path.insert(0, str(ROOT / "candidates/bk04-episode-verifier-v1"))
from bk05_capsule.synthetic import positive_fixture  # noqa: E402
from test_bk04_consumer import owner_binding, owner_fixture  # noqa: E402


class NormalReturn(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        files = positive_fixture()
        self.paths = {}
        for name, raw in files.items():
            path = self.root / name
            path.write_bytes(raw)
            self.paths[name] = str(path)
        self.bundle = {
            "schema": adapter.SCHEMA,
            "task": {"actor_ref": "SYN-ACTOR-WRITER", "arc_ref": "SYN-ARC-1",
                     "task_ref": "SYN-TASK-0001", "original_path": self.paths["parent.txt"],
                     "original_sha256": hashlib.sha256(files["parent.txt"]).hexdigest(),
                     **{k: json.loads(files["parent_manifest.json"])[k] for k in
                        ("effect_class", "privacy_class", "live_scope", "authority_class",
                         "return_target", "way_home", "required_invariants", "stop_edges", "source_head")},
                     "allowed_paths": json.loads(files["parent_manifest.json"])["allowed_paths"]},
            "return": {"actor_ref": "SYN-ACTOR-WRITER", "arc_ref": "SYN-ARC-1",
                       "task_ref": "SYN-TASK-0001",
                       "original_sha256": hashlib.sha256(files["parent.txt"]).hexdigest()},
            "bk05": {"parent_manifest_path": self.paths["parent_manifest.json"],
                     "verifier_path": self.paths["verifier.txt"],
                     "verifier_delta_path": self.paths["verifier_delta.json"],
                     "currentness_path": self.paths["currentness.json"]},
        }

    def test_qualified_same_arc_selects_existing_capsule_with_no_authority(self):
        finding, selected = adapter.evaluate(self.bundle)
        self.assertEqual(finding["selection"], "BK05_STRUCTURAL_CAPSULE_CANDIDATE")
        self.assertEqual(finding["bk05"]["decision"], "LOCAL_CAPSULE_CANDIDATE")
        self.assertEqual(json.loads(selected)["parent"]["task_ref"], "SYN-TASK-0001")
        self.assertEqual((finding["authority"], finding["effect"]), ("NONE", "NONE_CLAIMED"))

    def test_changed_actor_and_stale_source_keep_exact_full_original(self):
        original = Path(self.paths["parent.txt"]).read_bytes()
        self.bundle["return"]["actor_ref"] = "SYN-OTHER-ACTOR"
        finding, selected = adapter.evaluate(self.bundle)
        self.assertEqual((finding["selection"], selected), ("FULL_ORIGINAL_TASK", original))
        self.assertIn("ACTOR_ARC_OR_ORIGINAL_CHANGED", finding["bk05"]["reasons"])
        self.bundle["return"]["actor_ref"] = "SYN-ACTOR-WRITER"
        current = Path(self.paths["currentness.json"])
        data = json.loads(current.read_bytes())
        data["observed_source_head"] = "different-source-head"
        current.write_text(json.dumps(data), encoding="utf-8")
        finding, selected = adapter.evaluate(self.bundle)
        self.assertEqual((finding["selection"], selected), ("FULL_ORIGINAL_TASK", original))
        self.assertEqual(finding["bk05"]["decision"], "HOLD_STALE")
        self.assertTrue(any(r["code"] == "STALE_SOURCE_HEAD" for r in finding["bk05"]["reasons"]))

    def test_bk04_local_unknown_does_not_block_same_arc_capsule(self):
        owner = owner_fixture()
        raw, binding = owner_binding(owner)
        facts = self.root / "owner-facts.json"
        bind = self.root / "owner-binding.json"
        facts.write_bytes(raw)
        bind.write_text(json.dumps(binding), encoding="utf-8")
        self.bundle["bk04"] = {"owner_facts_path": str(facts), "binding_path": str(bind)}
        self.bundle["return"]["contract_kind"] = "BK04_OWNER_FACTS"
        finding, _ = adapter.evaluate(self.bundle)
        self.assertEqual(finding["bk04"]["state"], "UNKNOWN")
        self.assertFalse(finding["bk04"]["blocks_other_work"])
        self.assertEqual(finding["selection"], "BK05_STRUCTURAL_CAPSULE_CANDIDATE")

    def test_irrelevant_return_skips_bk04_and_scope_loss_falls_back(self):
        self.bundle["bk04"] = {"owner_facts_path": "C:/not-read.json", "binding_path": "C:/not-read.json"}
        finding, _ = adapter.evaluate(self.bundle)
        self.assertEqual(finding["bk04"]["state"], "SKIPPED_NOT_ELIGIBLE")
        self.bundle["task"]["stop_edges"] = ["lost original stop edge"]
        finding, selected = adapter.evaluate(self.bundle)
        self.assertEqual(finding["selection"], "FULL_ORIGINAL_TASK")
        self.assertEqual(selected, Path(self.paths["parent.txt"]).read_bytes())


if __name__ == "__main__":
    unittest.main()
