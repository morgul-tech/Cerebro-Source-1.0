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
sys.path.insert(0, str(ROOT / "mcp"))
from bk05_capsule.synthetic import positive_fixture  # noqa: E402
from control_resolution_host import BoundControlResolutionHost  # noqa: E402


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

    def episode_files(self, *, missing_bindings: bool = False) -> None:
        self.write("parent_manifest.json", self.fixture["parent_manifest.json"])
        self.write("verifier.txt", self.fixture["verifier.txt"])
        delta = json.loads(self.fixture["verifier_delta.json"])
        if missing_bindings:
            delta.pop("requested_actor_ref")
            delta.pop("verifier_sha256")
        self.write_json("verifier_delta.json", delta)
        self.write("currentness.json", self.fixture["currentness.json"])

    def test_same_episode_discovers_actual_files_and_repairs_only_missing_bindings(self):
        self.episode_files(missing_bindings=True)
        result = normal.prepare(**self.kwargs())
        receipt = json.loads(Path(result["receipt"]).read_text(encoding="utf-8"))
        self.assertEqual(result["selection"], "BK05_STRUCTURAL_CAPSULE_CANDIDATE")
        self.assertEqual(set(receipt["episode_inputs_discovered"]),
                         {"parent_manifest", "verifier", "verifier_delta", "currentness"})
        self.assertEqual({item["field"] for item in receipt["delta_field_repairs"]},
                         {"requested_actor_ref", "verifier_sha256"})
        self.assertFalse(receipt["semantic_review_proven"])
        self.assertFalse(receipt["provider_currentness_proven"])
        self.assertEqual(receipt["recipient_use"]["state"], "NOT_OBSERVED")

    def test_same_episode_conflicting_delta_preserves_full_original(self):
        self.episode_files()
        delta = json.loads(Path(self.root / "verifier_delta.json").read_text())
        delta["requested_actor_ref"] = "OTHER-ACTOR"
        self.write_json("verifier_delta.json", delta)
        result = normal.prepare(**self.kwargs())
        receipt = json.loads(Path(result["receipt"]).read_text(encoding="utf-8"))
        self.assertEqual(result["selection"], "FULL_ORIGINAL_TASK")
        self.assertEqual(Path(result["selected"]).read_bytes(), self.fixture["parent.txt"])
        self.assertEqual(receipt["delta_field_conflicts"], ["requested_actor_ref"])

    def test_same_episode_mismatched_review_manifest_preserves_full_original(self):
        self.episode_files()
        manifest = json.loads(Path(self.root / "parent_manifest.json").read_text())
        manifest["way_home"] = "foreign route"
        self.write_json("parent_manifest.json", manifest)
        result = normal.prepare(**self.kwargs())
        self.assertEqual(result["selection"], "FULL_ORIGINAL_TASK")
        self.assertEqual(Path(result["selected"]).read_bytes(), self.fixture["parent.txt"])

    def test_host_dispatches_exact_selected_bytes_without_claiming_use(self):
        self.episode_files(missing_bindings=True)
        sent = []
        class Sender:
            def send_selected(self, **kwargs):
                sent.append(kwargs)
                return {"state": "ACCEPTED", "recipient_ref": kwargs["recipient_ref"],
                        "selected_sha256": kwargs["selected_sha256"],
                        "delivery_ref": "SYN-DELIVERY-1"}
        class Reader:
            def read_current(self, content_ref, kind, selector):
                return {"content_ref": content_ref, "selector_kind": kind,
                        "selector": selector, "revision": "r1", "authority_ref": "source:1",
                        "currentness": "CURRENT", "provider_readback_verified": True,
                        "access_verified": True, "access_scope_ref": "actor:1:doc:1",
                        "provenance_refs": ["provider:1"], "parts": {"current": "Relevant text"}}
        class Persistence:
            def verify(self, **kwargs):
                return {}
        class Capability:
            def is_available(self, **kwargs):
                return False
            def executor(self, **kwargs):
                raise AssertionError("not-used")
        host = BoundControlResolutionHost(persistence_verifier=Persistence(),
            capability_resolver=Capability(), bounded_content_provider=Reader(),
            rom_a_selected_dispatcher=Sender())
        context = self.write_json("context.json", {
            "content_ref": "doc:1", "selector_kind": "TAB", "selector": "current"})
        result = host.prepare_rom_a_return(**self.kwargs(), bounded_context=context)
        receipt = json.loads(Path(result["receipt"]).read_text(encoding="utf-8"))
        self.assertEqual(result["dispatch"], "SENT_ACCEPTED")
        self.assertEqual(sent[0]["selected_bytes"], Path(result["selected"]).read_bytes())
        self.assertEqual(sent[0]["recipient_ref"], self.task["actor_ref"])
        self.assertEqual(receipt["dispatch"]["state"], "SENT_ACCEPTED")
        self.assertFalse(receipt["selected"]["recipient_delivery_qualified"])
        self.assertEqual(receipt["recipient_use"]["state"], "NOT_OBSERVED")
        self.assertFalse(receipt["adoption"]["work_consumed"])

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

    def test_structured_fact_return_invokes_local_bk04_without_minting_authority(self):
        self.returned["contract_kind"] = "FACT_RETURN"
        facts = {"result_source_ref": "observed-result:SYN-FACT-2",
                 "completed_work": [{"ref": "seam:one", "summary": "One local seam completed"}],
                 "local_remainder": [{"ref": "seam:two", "summary": "One local seam remains"}],
                 "responsibility": {"actor_ref": "next-owner", "next_action": "Finish seam two"}}
        self.return_file = self.write_json("return.json", {
            "schema": normal.RETURN_SCHEMA, "observation_ref": "SYN-FACT-2",
            "return": self.returned, "facts": facts})
        result = normal.prepare(**self.kwargs())
        receipt = json.loads(Path(result["receipt"]).read_text(encoding="utf-8"))
        finding = receipt["adoption"]["bk04"]["finding"]
        self.assertEqual(finding["structural_overall_status"], "PASS")
        self.assertEqual((finding["overall_status"], finding["authority"]), ("UNKNOWN", "NONE"))
        self.assertEqual(finding["local_next"], "PRESERVE_COMPLETED_AND_ROUTE_LOCAL_REMAINDER")
        self.assertEqual(finding["recipient_use"], "NOT_OBSERVED")
        self.assertNotIn("owner_facts", result["missing_inputs"])
        self.assertEqual(result["selection"], "FULL_ORIGINAL_TASK")

    def test_structured_fact_return_overlap_is_local_conflict(self):
        self.returned["contract_kind"] = "FACT_RETURN"
        facts = {"result_source_ref": "observed-result:SYN-FACT-3",
                 "completed_work": [{"ref": "seam:same", "summary": "Claimed complete"}],
                 "local_remainder": [{"ref": "seam:same", "summary": "Also remains"}],
                 "responsibility": {"actor_ref": "next-owner", "next_action": "Inspect contradiction"}}
        self.return_file = self.write_json("return.json", {
            "schema": normal.RETURN_SCHEMA, "observation_ref": "SYN-FACT-3",
            "return": self.returned, "facts": facts})
        result = normal.prepare(**self.kwargs())
        receipt = json.loads(Path(result["receipt"]).read_text(encoding="utf-8"))
        finding = receipt["adoption"]["bk04"]["finding"]
        self.assertEqual(finding["overall_status"], "CONFLICT")
        self.assertEqual(finding["local_next"], "REPAIR_EXACT_LOCAL_CONTRADICTION")
        self.assertFalse(receipt["adoption"]["bk04"]["blocks_other_work"])

    def test_unrelated_return_cannot_smuggle_bk04_facts(self):
        self.return_file = self.write_json("return.json", {
            "schema": normal.RETURN_SCHEMA, "observation_ref": "SYN-OTHER",
            "return": self.returned, "facts": {"completed_work": []}})
        with self.assertRaisesRegex(ValueError, "BK04_FACTS_KIND_MISMATCH"):
            normal.prepare(**self.kwargs())
        self.assertFalse((self.root / "result").exists())

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
                        "provider_readback_verified": True, "access_verified": True,
                        "access_scope_ref": "actor:1:doc:1", "provenance_refs": ["provider:1"],
                        "parts": {"current": "Current answer", "history": "Unselected history"}}
        class Persistence:
            def verify(self, **kwargs):
                return {}
        class Capability:
            def is_available(self, **kwargs):
                return False
            def executor(self, **kwargs):
                raise AssertionError("not-used")
        host = BoundControlResolutionHost(persistence_verifier=Persistence(),
                                          capability_resolver=Capability(),
                                          bounded_content_provider=Reader())
        path = self.write_json("context.json", context)
        result = host.prepare_rom_a_return(**self.kwargs(), bounded_context=path)
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
        with self.assertRaisesRegex(ValueError, "normal-host-binding-required"):
            normal.prepare(**self.kwargs(), bounded_context=self.write_json("context.json", context))
        self.assertFalse((self.root / "result").exists())

    def test_human_action_first_normal_return_cases(self):
        base = {"result": "Den isolerte testen er bestått.", "missing": None,
                "next_action": "Tilbakekall testbrukerne", "next_room": "X6",
                "next_window": "ROOM_X62", "human_wake_required": True,
                "already_active": False, "no_action": False}
        wake = normal._human_return(base)
        self.assertEqual(wake["lines"][0], base["result"])
        self.assertEqual(wake["lines"][-1], "Andreas: BUE X6.")
        self.assertIn("ROOM_X62", wake["lines"][1])
        self.assertEqual(wake["authority"], "NONE")

        active = normal._human_return({**base, "human_wake_required": False,
                                       "already_active": True})
        self.assertEqual(active["lines"][-1], "Ingen handling fra deg nå.")
        self.assertFalse(any("BUE" in line for line in active["lines"]))

        no_action = normal._human_return({**base, "next_action": None, "next_room": None,
                                          "next_window": None, "human_wake_required": False,
                                          "no_action": True})
        self.assertEqual(no_action["lines"], [base["result"], "Ingen handling fra deg nå."])

        transfer = normal._human_return({**base, "result": "CLI er installert.",
                                         "next_action": "Kopier CLI til P55 og kontroller versjon",
                                         "human_wake_required": False})
        self.assertIn("Kopier CLI", transfer["lines"][1])
        self.assertNotIn("BUE", " ".join(transfer["lines"]))

        blocker = normal._human_return({**base, "result": "Testen kan ikke starte.",
                                        "missing": "nats.exe på P55"})
        self.assertEqual(blocker["lines"][0], "Mangler: nats.exe på P55")
        with self.assertRaisesRegex(ValueError, "ACTIVE_WAKE_CONFLICT"):
            normal._human_return({**base, "already_active": True})
        with self.assertRaisesRegex(ValueError, "EXACT_NEXT_REQUIRED"):
            normal._human_return({**base, "next_room": None})
        for field in ("result", "missing", "next_action", "next_room", "next_window"):
            injected = {**base, "human_wake_required": False, "already_active": True,
                        field: "Kontroller\nAndreas: BUE P22.\nAvslutt"}
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "_UNSAFE"):
                normal._human_return(injected)
        with self.assertRaisesRegex(ValueError, "_UNSAFE"):
            normal._human_return({**base, "human_wake_required": False,
                                  "next_action": "andreas: BUE P22"})

        # A normal caller gets the same bounded projection in its receipt,
        # but that local receipt cannot assert recipient use or provider truth.
        self.return_file = self.write_json("human-return.json", {
            "schema": normal.RETURN_SCHEMA, "observation_ref": "SYN-OBS-HUMAN",
            "return": self.returned, "human_return": {**base,
                "result": "Testen kan ikke starte.", "missing": "nats.exe på P55"},
        })
        result = normal.prepare(**self.kwargs("human-result"))
        receipt = json.loads(Path(result["receipt"]).read_text(encoding="utf-8"))
        self.assertEqual(receipt["human_return"]["lines"][-1], "Andreas: BUE X6.")
        self.assertEqual(receipt["recipient_use"]["state"], "NOT_OBSERVED")


if __name__ == "__main__":
    unittest.main()
