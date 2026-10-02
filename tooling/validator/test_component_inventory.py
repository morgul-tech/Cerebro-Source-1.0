"""Focused fixtures for the root README's stabilized-component count."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from tooling.validator.canonical_foundation import classify_components

SOURCE_ROOT = Path(__file__).resolve().parents[2]
INVENTORY = Path("standards/source-component-inventory.yaml")


class ComponentInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory(prefix="cerebro-component-inventory-")
        target = Path(scratch.name).resolve()
        if not target.is_relative_to(Path(tempfile.gettempdir()).resolve()):
            raise AssertionError("temporary fixture escaped its intended directory")
        self.addCleanup(scratch.cleanup)
        self.root = target
        for source in SOURCE_ROOT.rglob("component.yaml"):
            if "history" in {part.lower() for part in source.relative_to(SOURCE_ROOT).parts}:
                continue
            dest = self.root / source.relative_to(SOURCE_ROOT)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, dest)
        dest = self.root / INVENTORY
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SOURCE_ROOT / INVENTORY, dest)

    def test_current_inventory_is_19_stable_19_candidates_1_canary(self) -> None:
        classes, errors = classify_components(self.root)
        self.assertEqual(errors, [])
        self.assertEqual((len(classes["stable"]), len(classes["candidates"]),
                          len(classes["bounded_canaries"])), (19, 19, 1))

    def test_candidate_classification_uses_manifest_semantics_not_path_prefix(self) -> None:
        path = self.root / "tooling/experimental/component.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump({"schema": "cerebro-component/v1", "component": {
            "id": "experimental", "type": "candidate", "status": "isolated-implementation-candidate",
            "authority": "NONE"}}), encoding="utf-8")
        classes, errors = classify_components(self.root)
        self.assertEqual(errors, [])
        self.assertEqual((len(classes["stable"]), len(classes["candidates"])), (19, 20))
        self.assertIn("tooling/experimental/component.yaml", classes["candidates"])
        path.write_text(yaml.safe_dump({"schema": "cerebro-component/v1", "component": {
            "id": "experimental", "type": "tooling", "status": "active-source"}}), encoding="utf-8")
        _, errors = classify_components(self.root)
        self.assertIn("SOURCE_COMPONENT_UNCLASSIFIED:tooling/experimental/component.yaml", errors)

    def test_named_canary_is_validated_separately(self) -> None:
        rel = "tooling/canary/v0.1-lifecycle-receipt/component.yaml"
        path = self.root / rel
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        doc["component"]["authority"] = "WRITE"
        path.write_text(yaml.safe_dump(doc), encoding="utf-8")
        classes, errors = classify_components(self.root)
        self.assertEqual(len(classes["stable"]), 19)
        self.assertIn("SOURCE_BOUNDED_CANARY_CLASS_DRIFT:" + rel, errors)

    def test_missing_stable_component_fails_instead_of_shrinking_count(self) -> None:
        rel = "engines/context/component.yaml"
        (self.root / rel).unlink()
        _, errors = classify_components(self.root)
        self.assertIn("SOURCE_COMPONENT_INVENTORY_MISSING:" + rel, errors)


if __name__ == "__main__":
    unittest.main()
