"""Focused changed-risk checks for exact producer-boundary dispatch capture."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "tooling/return-bridge/rom_a_dispatch_capture.py"
spec = importlib.util.spec_from_file_location("rom_a_dispatch_capture", MODULE)
assert spec is not None and spec.loader is not None
capture_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture_module)
ADOPTION = ROOT / "tooling/return-bridge/rom_a_return_adoption.py"
adoption_spec = importlib.util.spec_from_file_location("rom_a_return_adoption_capture_check", ADOPTION)
assert adoption_spec is not None and adoption_spec.loader is not None
adoption = importlib.util.module_from_spec(adoption_spec)
adoption_spec.loader.exec_module(adoption)


class DispatchCapture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "capture"
        self.request = {
            "schema": capture_module.SCHEMA,
            "task": {
                "task_ref": "SYN-TASK-1", "task_revision": "rev-2",
                "actor_ref": "SYN-ACTOR", "generation_ref": "SYN-GEN",
                "carrier_ref": "SYN-CARRIER", "arc_ref": "SYN-ARC",
                "source_head": "a" * 40, "effect_class": "NONE",
                "privacy_class": "SYNTHETIC", "live_scope": "LOCAL",
                "authority_class": "NONE", "return_target": "SYN-OWNER",
                "way_home": "SYN-OWNER-RETURN", "allowed_paths": ["one.py"],
                "required_invariants": ["raw bytes retained"],
                "stop_edges": ["foreign actor"],
            },
        }
        self.raw = "første\r\nlinje\nAndré\n".encode("utf-8")

    def test_raw_bytes_readback_without_newline_or_unicode_normalization(self):
        result = capture_module.capture(self.root, self.request, self.raw)
        self.assertEqual(result["state"], "CAPTURED_READBACK")
        self.assertEqual(Path(result["original_path"]).read_bytes(), self.raw)
        self.assertEqual(result["receipt"]["original_sha256"], hashlib.sha256(self.raw).hexdigest())
        self.assertEqual(result["receipt"]["original_bytes"], len(self.raw))
        self.assertEqual(result["receipt"]["semantic_review"]["status"], "UNREVIEWED")
        self.assertEqual(result["receipt"]["provenance"]["status"], "UNVERIFIED")
        self.assertFalse(result["receipt"]["recipient_read_or_use_proven"])

    def test_publish_receipt_does_not_claim_crash_durability_and_fsync_failure_refuses(self):
        with mock.patch.object(capture_module.os, "fsync", side_effect=OSError("sync failed")):
            with self.assertRaisesRegex(OSError, "sync failed"):
                capture_module.capture(self.root, self.request, self.raw)
        self.assertFalse(list(self.root.glob("capture-*")))
        result = capture_module.capture(self.root, self.request, self.raw)
        self.assertEqual(result["state"], "CAPTURED_READBACK")
        self.assertEqual(result["publish_durability"],
                         "LOCAL_READBACK_ONLY_CRASH_DURABILITY_UNPROVEN")
        self.assertEqual(result["receipt"]["publish_durability"], result["publish_durability"])
        reused = capture_module.capture(self.root, self.request, self.raw)
        self.assertEqual(reused["state"], "EXACT_REUSE")
        self.assertEqual(reused["publish_durability"], result["publish_durability"])

    def test_exact_same_record_reuses_and_same_key_divergence_collides(self):
        first = capture_module.capture(self.root, self.request, self.raw)
        again = capture_module.capture(self.root, self.request, self.raw)
        self.assertEqual(again["state"], "EXACT_REUSE")
        self.assertEqual(first["capture_path"], again["capture_path"])
        with self.assertRaisesRegex(ValueError, "COLLISION"):
            capture_module.capture(self.root, self.request, self.raw + b"!")
        changed = json.loads(json.dumps(self.request))
        changed["task"]["stop_edges"] = ["different stop edge"]
        with self.assertRaisesRegex(ValueError, "COLLISION"):
            capture_module.capture(self.root, changed, self.raw)

    def test_explicit_new_revision_is_distinct(self):
        first = capture_module.capture(self.root, self.request, self.raw)
        new = json.loads(json.dumps(self.request))
        new["task"]["task_revision"] = "rev-3"
        second = capture_module.capture(self.root, new, self.raw)
        self.assertNotEqual(first["capture_path"], second["capture_path"])

    def test_provenance_and_review_are_never_promoted_to_authority(self):
        self.request["provenance"] = {"status": "CALLER_ASSERTED", "original_dispatch_ref": "SYN-ORIGINAL"}
        self.request["semantic_review"] = {"status": "OWNER_REVIEWED", "receipt_ref": "SYN-REVIEW"}
        receipt = capture_module.capture(self.root, self.request, self.raw)["receipt"]
        self.assertEqual(receipt["provenance"]["assertion"], "CALLER_SUPPLIED_NOT_VERIFIED")
        self.assertEqual(receipt["semantic_review"]["assertion"], "CALLER_SUPPLIED_NOT_VERIFIED")
        self.assertEqual(receipt["authority"], "NONE")
        self.assertEqual(receipt["capture_stage"], "LOCAL_CAPTURE_TIMING_NOT_PROVIDER_VERIFIED")

    def test_review_and_provenance_cannot_claim_evidence_without_refs(self):
        self.request["semantic_review"] = {"status": "OWNER_REVIEWED", "receipt_ref": ""}
        with self.assertRaisesRegex(ValueError, "REVIEW_RECEIPT_REF_REQUIRED"):
            capture_module.capture(self.root, self.request, self.raw)
        self.request["semantic_review"] = {"status": "UNREVIEWED", "receipt_ref": ""}
        self.request["provenance"] = {"status": "CALLER_ASSERTED", "original_dispatch_ref": ""}
        with self.assertRaisesRegex(ValueError, "ORIGINAL_DISPATCH_REF_REQUIRED"):
            capture_module.capture(self.root, self.request, self.raw)

    def test_review_claim_without_original_provenance_is_refused(self):
        self.request["semantic_review"] = {"status": "OWNER_REVIEWED", "receipt_ref": "SYN-REVIEW"}
        with self.assertRaisesRegex(ValueError, "PROVENANCE_REQUIRED_FOR_REVIEW"):
            capture_module.capture(self.root, self.request, self.raw)

    def test_invalid_bytes_and_missing_stop_edge_fail_before_capture(self):
        with self.assertRaisesRegex(ValueError, "ORIGINAL_NON_UTF8"):
            capture_module.capture(self.root, self.request, b"\xff")
        self.request["task"]["stop_edges"] = []
        with self.assertRaisesRegex(ValueError, "STOP_EDGES_REQUIRED"):
            capture_module.capture(self.root, self.request, self.raw)
        self.assertFalse(self.root.exists())

    def test_existing_capture_corruption_is_not_reused(self):
        first = capture_module.capture(self.root, self.request, self.raw)
        Path(first["original_path"]).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "COLLISION"):
            capture_module.capture(self.root, self.request, self.raw)

    def test_existing_adoption_uses_captured_full_original_on_identity_change(self):
        result = capture_module.capture(self.root, self.request, self.raw)
        task = {**result["receipt"]["task"], "original_path": result["original_path"],
                "original_sha256": result["receipt"]["original_sha256"]}
        returned = {name: task[name] for name in
                    ("actor_ref", "generation_ref", "carrier_ref", "arc_ref", "task_ref", "task_revision",
                     "original_sha256")}
        returned["actor_ref"] = "FOREIGN-ACTOR"
        finding, selected = adoption.evaluate({"schema": adoption.SCHEMA,
                                                "task": task, "return": returned})
        self.assertEqual(finding["selection"], "FULL_ORIGINAL_TASK")
        self.assertEqual(selected, self.raw)
        self.assertFalse(finding["work_consumed"])

    def test_missing_captured_original_never_becomes_exact_original(self):
        result = capture_module.capture(self.root, self.request, self.raw)
        Path(result["original_path"]).unlink()
        with self.assertRaisesRegex(ValueError, "CAPTURE_READBACK_UNAVAILABLE"):
            capture_module.capture(self.root, self.request, self.raw)


if __name__ == "__main__":
    unittest.main()
