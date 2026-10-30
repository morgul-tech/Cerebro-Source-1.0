"""Inert reader-boundary tests; producer stub is not runtime or recipient proof."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from test_rom_e_material_return import ChangedSeam, entry, write_json


class A7Reader(unittest.TestCase):
    setUp = ChangedSeam.setUp
    run_entry = ChangedSeam.run_entry
    manifest = ChangedSeam.manifest
    repair = ChangedSeam.repair

    def material(self):
        binding = {**{k: self.task[k] for k in ("task_ref", "task_revision", "actor_ref",
                   "generation_ref", "carrier_ref", "arc_ref")},
                   "original_sha256": entry._sha(self.original.read_bytes())}
        context = {"schema": "cerebro-learning-context/v1", "sanitized_failure": "synthetic failure",
                   "challenged_premise": "UNKNOWN", "considered_decision": "inspect",
                   "epistemic_state": "HYPOTHESIS", "contract_ref": "synthetic contract",
                   "edition_ref": "UNKNOWN", "applicability": "synthetic",
                   "original_ref": "synthetic original", "tags": ["synthetic"]}
        item = {"ref": "example", "failure_family": "synthetic", "observation": "failure",
                "application": "inspect first", "exception": "UNKNOWN", "next_action": "inspect",
                "scope": "synthetic", "evidence_refs": ["example"],
                "evidence_state": "ATTRIBUTED_OBSERVATION", "causal_state": "OWNER_DIAGNOSIS_NOT_PROVEN",
                "recurrence": "UNKNOWN", "counterevidence": ["UNKNOWN"], "authority": "EVIDENCE_ONLY",
                "record_revision": "a" * 64, "validity": {"state": "CURRENT", "as_of": "UNKNOWN", "scope": "synthetic"},
                "source_ref": "synthetic source", "source_revision": "UNKNOWN"}
        size = len(entry._canonical(item)) - 1
        encounter = {"schema": "cerebro-source-learning-encounter/v1", "status": "SELECTED",
                     "items": [item], "omissions": [], "source_catalog_sha256": "b" * 64,
                     "authority": "EVIDENCE_ONLY", "provider_readback": "NOT_CLAIMED",
                     "native_or_work_authority": "NONE", "projection_utf8_bytes": size}
        event = {"schema": "cerebro-work-error-reflex/v2", "binding": binding,
                 "observation_ref": "synthetic observation", "attempt_ref": "synthetic attempt",
                 "learning_context": context}
        material = {"schema": "cerebro-learning-material/v1", "binding": binding,
                    "observation_ref": event["observation_ref"], "attempt_ref": event["attempt_ref"],
                    "learning_context": context, "source_learning_encounter": encounter, "authority": "EVIDENCE_ONLY"}
        return {"schema": "cerebro-work-error-reflex/v2-assessment", "event": event,
                "source_learning_encounter": encounter, "learning_material": material,
                "next_material_step_qualified": True}

    def run_stubbed(self, assessment, **options):
        write_json(self.root / "work_error.json", {"synthetic": True})
        original_load = entry._load
        def load(name):
            module = original_load(name)
            if name == "rom_a_normal_path":
                original_module = module._module
                def select(modname, path):
                    if modname == "work_reflex_consumer":
                        class Stub:
                            @staticmethod
                            def evaluate(*args, **kwargs):
                                return copy.deepcopy(assessment)
                        return Stub
                    return original_module(modname, path)
                module._module = select
            return module
        with patch.object(entry, "_load", side_effect=load):
            return self.run_entry(**options)

    def test_selected_bytes_include_original_and_advice_before_manifest(self):
        result = self.run_stubbed(self.material())
        manifest = self.manifest(result)
        out = Path(result["manifest"]).parent
        payload = (out / manifest["selected"]).read_bytes()
        self.assertEqual(manifest["selection"], "FULL_ORIGINAL_WITH_LEARNING")
        self.assertIn(self.original.read_bytes(), payload)
        self.assertIn(b'"application": "inspect first"', payload)
        self.assertIn("control/learning_material.json", manifest["files"])
        self.assertIn(manifest["selected"], manifest["files"])
        (out / "control/learning_material.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "OUTPUT_CONFLICT"):
            self.run_stubbed(self.material())

    def test_wrong_binding_and_malformed_item_are_rejected(self):
        for mutate in ("binding", "item"):
            assessment = self.material()
            if mutate == "binding":
                assessment["learning_material"]["binding"]["actor_ref"] = "WRONG"
            else:
                assessment["learning_material"]["source_learning_encounter"]["items"][0]["extra"] = "unbound"
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                self.run_stubbed(assessment)

    def test_noncheap_v2_cannot_drop_material(self):
        assessment = self.material()
        assessment["learning_material"] = None
        assessment["disposition"] = "OBSERVE_CHEAP_DISCRIMINATOR"
        with self.assertRaisesRegex(ValueError, "LEARNING_MATERIAL_MISSING"):
            self.run_stubbed(assessment)

    def test_new_learning_forces_full_even_when_old_held_basis_matches(self):
        files = self.repair()
        result = self.run_stubbed(self.material(), held_basis_reader=lambda *_: (
            "synthetic current", files["parent.txt"], files["verifier.txt"]))
        manifest = self.manifest(result)
        self.assertTrue(manifest["held_basis"]["exact"])
        self.assertEqual(manifest["selection"], "FULL_ORIGINAL_WITH_LEARNING")
        self.assertIn(b"inspect first", (Path(result["manifest"]).parent / manifest["selected"]).read_bytes())

    def test_sender_sees_bound_advice_and_requires_manifest(self):
        result = self.run_stubbed(self.material())
        out = Path(result["manifest"]).parent
        control = out / "control"
        normal = entry._load("rom_a_normal_path")
        sent = []
        class Sender:
            def send_selected(self, **kw):
                sent.append(kw["selected_bytes"])
                return {"state": "ACCEPTED", "recipient_ref": kw["recipient_ref"],
                        "selected_sha256": kw["selected_sha256"], "delivery_ref": "synthetic"}
        selected = {"receipt": str(control / "receipt.json"),
                    "selected": str(control / "original_with_learning.txt")}
        manifest_raw = Path(result["manifest"]).read_bytes()
        Path(result["manifest"]).unlink()
        with self.assertRaises(ValueError):
            normal._dispatch_selected(selected, Sender())
        self.assertEqual(sent, [])
        Path(result["manifest"]).write_bytes(manifest_raw)
        normal._dispatch_selected(selected, Sender())
        self.assertEqual(len(sent), 1)
        self.assertIn(b'"application": "inspect first"', sent[0])


if __name__ == "__main__":
    unittest.main()
