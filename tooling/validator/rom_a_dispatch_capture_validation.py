"""Focused changed-risk checks for exact producer-boundary dispatch capture."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mcp"))
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


class OwnerEpisodeAdapter(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name) / "capture"
        request = {"schema": capture_module.SCHEMA, "task": {
            "task_ref": "TASK-1", "task_revision": "TREV-1", "actor_ref": "ACTOR-1",
            "generation_ref": "GEN-1", "carrier_ref": "CARRIER-1", "arc_ref": "ARC-1",
            "source_head": "a" * 40, "effect_class": "NONE", "privacy_class": "SYNTHETIC",
            "live_scope": "SCOPE-1", "authority_class": "NONE", "return_target": "A1",
            "way_home": "A1-RETURN", "allowed_paths": ["one.py"],
            "required_invariants": ["one task"], "stop_edges": ["authority change"],
        }}
        self.raw = b"original task bytes\n"
        self.capture = capture_module.capture(root, request, self.raw)
        self.identity = {"owner_ref": "A1", "authenticated": True, "currentness": "CURRENT"}
        self.mandate = {
            "owner_ref": "A1", "mandate_ref": "HUMAN-MANDATE-1", "revision": "MREV-1",
            "currentness": "CURRENT", "authenticated_readback": True,
            "human_authorized": True,
            "human_provenance_ref": "HUMAN-DECISION-1", "task_ref": "TASK-1",
            "task_revision": "TREV-1", "actor_ref": "ACTOR-1", "scope_ref": "SCOPE-1",
            "progress_state": "WAITING_DEPENDENCY", "paused": False, "revoked": False,
            "dependency_refs": ["DEP-1"], "all_dependencies_resolved": True,
            "other_unresolved_gates": [],
            "preflight_request": {"stage": "MATERIAL_AUTHORIZE", "material": True,
                                  "commitment_target": "TASK-1", "resolved_scope": "SCOPE-1"},
        }
        self.dependency = {"dependency_ref": "DEP-1", "revision": "DREV-1",
                           "source_ref": "SOURCE-1", "currentness": "CURRENT",
                           "readback_verified": True,
                           "disposition": "SOURCE_AFTER_DEPENDENCY",
                           "state": "RESOLVED", "applicable": True}

        class Reader:
            def __init__(self, value):
                self.value = value

            def read_current(self, **_kwargs):
                return dict(self.value)

        self.port = capture_module.BoundRomAOwnerEpisodePort(
            capture_record=Path(self.capture["capture_path"]),
            owner_identity_reader=Reader(self.identity),
            mandate_reader=Reader(self.mandate), dependency_reader=Reader(self.dependency))

    def provision(self, **overrides):
        return self.port.provision(**{
            "task_ref": "TASK-1", "task_revision": "TREV-1", "dependency_ref": "DEP-1",
            "dependency_revision": "DREV-1", "mandate_ref": "HUMAN-MANDATE-1",
            **overrides,
        })

    def basis(self):
        from control_resolution_host import _authorized_dependency_basis
        return _authorized_dependency_basis(
            self.port.read_current(task_ref="TASK-1", dependency_ref="DEP-1"),
            "TASK-1", "DEP-1")[2]

    def test_legacy_capture_ineligible_until_owner_provisions_exact_mandate(self):
        with self.assertRaisesRegex(ValueError, "OWNER_EPISODE_NOT_PROVISIONED"):
            self.port.read_current(task_ref="TASK-1", dependency_ref="DEP-1")
        with self.assertRaisesRegex(ValueError, "EXACT_MANDATE_OR_DEPENDENCY_MISMATCH"):
            self.provision(dependency_revision="FORGED-REV")
        self.identity["authenticated"] = False
        with self.assertRaisesRegex(ValueError, "CURRENT_HUMAN_AND_DEPENDENCY_READBACK_REQUIRED"):
            self.provision()
        self.identity["authenticated"] = True
        self.mandate["authenticated_readback"] = False
        with self.assertRaisesRegex(ValueError, "CURRENT_HUMAN_AND_DEPENDENCY_READBACK_REQUIRED"):
            self.provision()
        self.mandate["authenticated_readback"] = True
        self.dependency["readback_verified"] = False
        with self.assertRaisesRegex(ValueError, "CURRENT_HUMAN_AND_DEPENDENCY_READBACK_REQUIRED"):
            self.provision()
        self.dependency["readback_verified"] = True
        self.assertEqual(self.provision()["state"], "PROVISIONED_READBACK")
        current = self.port.read_current(task_ref="TASK-1", dependency_ref="DEP-1")
        self.assertTrue(current["owner_readback_verified"])
        self.assertFalse(current["provider_verified_claim"])
        self.assertEqual(current["task"]["selected_bytes"], self.raw)
        self.assertEqual(current["task"]["authority_ref"], "HUMAN-DECISION-1")

    def test_reservation_is_per_episode_cas_and_same_basis_cannot_reset(self):
        self.provision()
        basis = self.basis()
        reserved = self.port.reserve_reconsideration(
            task_ref="TASK-1", task_revision="TREV-1", dependency_ref="DEP-1",
            dependency_revision="DREV-1", basis_fingerprint=basis)
        self.assertEqual(reserved["state"], "RESERVED")
        self.assertEqual(self.port.reserve_reconsideration(
            task_ref="TASK-1", task_revision="TREV-1", dependency_ref="DEP-1",
            dependency_revision="DREV-1", basis_fingerprint=basis)["state"],
            "STALE_OR_ALREADY_RESERVED")
        self.assertEqual(self.port.record_reconsideration_result(
            task_ref="TASK-1", dependency_ref="DEP-1", basis_fingerprint=basis,
            result="SENT_ACCEPTED", delivery_ref="DELIVERY-1")["state"], "RECORDED")
        self.assertEqual(self.port.read_current(task_ref="TASK-1", dependency_ref="DEP-1")
                         ["prior_reconsideration"]["result"], "SENT_ACCEPTED")
        with self.assertRaisesRegex(ValueError, "SAME_BASIS_RESERVATION_CANNOT_RESET"):
            self.provision(expected_episode_revision=3)

    def test_mandate_or_dependency_staleness_blocks_readback(self):
        self.provision()
        self.mandate["revision"] = "MREV-2"
        with self.assertRaisesRegex(ValueError, "STALE_MANDATE_OR_DEPENDENCY"):
            self.port.read_current(task_ref="TASK-1", dependency_ref="DEP-1")
        self.mandate["revision"] = "MREV-1"
        self.dependency["revision"] = "DREV-2"
        with self.assertRaisesRegex(ValueError, "STALE_MANDATE_OR_DEPENDENCY"):
            self.port.read_current(task_ref="TASK-1", dependency_ref="DEP-1")

    def test_restart_and_fault_batch_keeps_uncertain_reservation_non_retryable(self):
        self.provision()
        args = {"task_ref": "TASK-1", "task_revision": "TREV-1",
                "dependency_ref": "DEP-1", "dependency_revision": "DREV-1",
                "basis_fingerprint": self.basis()}
        state_path = Path(self.capture["capture_path"]).parent / "owner-state.json"
        before = state_path.read_bytes()
        with mock.patch.object(capture_module.os, "fsync", side_effect=OSError("sync failed")):
            with self.assertRaisesRegex(OSError, "sync failed"):
                self.port.reserve_reconsideration(**args)
        self.assertEqual(state_path.read_bytes(), before)
        self.assertIsNone(self.port.read_current(task_ref="TASK-1", dependency_ref="DEP-1")
                          ["prior_reconsideration"])
        self.assertEqual(self.port.reserve_reconsideration(**args)["state"], "RESERVED")

        restarted = capture_module.BoundRomAOwnerEpisodePort(
            capture_record=Path(self.capture["capture_path"]),
            owner_identity_reader=self.port.owner_identity_reader,
            mandate_reader=self.port.mandate_reader,
            dependency_reader=self.port.dependency_reader)
        prior = restarted.read_current(task_ref="TASK-1", dependency_ref="DEP-1")
        self.assertEqual(prior["prior_reconsideration"]["result"], "RESERVED_UNCERTAIN")
        self.assertEqual(restarted.reserve_reconsideration(**args)["state"],
                         "STALE_OR_ALREADY_RESERVED")
        with self.assertRaisesRegex(ValueError, "SAME_BASIS_RESERVATION_CANNOT_RESET"):
            restarted.provision(task_ref="TASK-1", task_revision="TREV-1",
                                dependency_ref="DEP-1", dependency_revision="DREV-1",
                                mandate_ref="HUMAN-MANDATE-1", expected_episode_revision=2)

        with mock.patch.object(capture_module.os, "fsync", side_effect=OSError("result sync failed")):
            with self.assertRaisesRegex(OSError, "result sync failed"):
                restarted.record_reconsideration_result(
                    task_ref="TASK-1", dependency_ref="DEP-1", basis_fingerprint=args["basis_fingerprint"],
                    result="SENT_ACCEPTED", delivery_ref="DELIVERY-1")
        again = capture_module.BoundRomAOwnerEpisodePort(
            capture_record=Path(self.capture["capture_path"]),
            owner_identity_reader=self.port.owner_identity_reader,
            mandate_reader=self.port.mandate_reader,
            dependency_reader=self.port.dependency_reader)
        self.assertEqual(again.read_current(task_ref="TASK-1", dependency_ref="DEP-1")
                         ["prior_reconsideration"]["result"], "RESERVED_UNCERTAIN")
        self.assertEqual(again.reserve_reconsideration(**args)["state"],
                         "STALE_OR_ALREADY_RESERVED")

    def test_reservation_refuses_pause_revocation_and_refall_at_same_revision(self):
        for field, value, target in (("paused", True, self.mandate),
                                     ("revoked", True, self.mandate),
                                     ("state", "REOPENED", self.dependency)):
            with self.subTest(field=field):
                state_path = self.port.episode / "owner-state.json"
                revision = json.loads(state_path.read_text())["episode_revision"] if state_path.exists() else 0
                self.provision(expected_episode_revision=revision)
                basis = self.basis()
                original = target[field]
                target[field] = value
                try:
                    result = self.port.reserve_reconsideration(
                        task_ref="TASK-1", task_revision="TREV-1", dependency_ref="DEP-1",
                        dependency_revision="DREV-1", basis_fingerprint=basis)
                    self.assertEqual(result["state"], "CURRENT_OWNER_BASIS_OR_GATE_CHANGED")
                    self.assertIsNone(self.port.read_current(task_ref="TASK-1", dependency_ref="DEP-1")
                                      ["prior_reconsideration"])
                finally:
                    target[field] = original

    def test_normal_host_constructor_binds_exact_owner_episode_and_selected_sender(self):
        from control_resolution_host import BoundControlResolutionHost

        class Persistence:
            def verify(self, **_kwargs):
                return {}

        class Capability:
            def is_available(self, **_kwargs):
                return False

            def executor(self, **_kwargs):
                raise AssertionError("unused")

        class Sender:
            def __init__(self):
                self.count = 0

            def send_selected(self, **kwargs):
                self.count += 1
                return {"state": "ACCEPTED", "recipient_ref": kwargs["recipient_ref"],
                        "selected_sha256": kwargs["selected_sha256"],
                        "delivery_ref": "SYNTHETIC-DELIVERY-1"}

        sender = Sender()
        host = BoundControlResolutionHost.for_rom_a_owner_episode(
            capture_record=Path(self.capture["capture_path"]),
            owner_identity_reader=self.port.owner_identity_reader,
            human_mandate_reader=self.port.mandate_reader,
            dependency_reader=self.port.dependency_reader,
            selected_sender=sender, persistence_verifier=Persistence(),
            capability_resolver=Capability())
        host._authorized_task_dependency_reader.provision(
            task_ref="TASK-1", task_revision="TREV-1", dependency_ref="DEP-1",
            dependency_revision="DREV-1", mandate_ref="HUMAN-MANDATE-1")
        with (mock.patch("control_resolution_host.material_commitment_preflight.resolve",
                         return_value={"result": "PASS", "receipt": {"schema": "test"}}),
              mock.patch("control_resolution_host.material_commitment_preflight.consume",
                         return_value={"result": "PASS"})):
            first = host.reconsider_authorized_task_dependency("TASK-1", "DEP-1")
            second = host.reconsider_authorized_task_dependency("TASK-1", "DEP-1")
        self.assertEqual(first["reason"], "OWNER_FENCED_DISPATCH_UNBOUND")
        self.assertEqual(second["reason"], "OWNER_FENCED_DISPATCH_UNBOUND")
        self.assertEqual(sender.count, 0)


if __name__ == "__main__":
    unittest.main()
