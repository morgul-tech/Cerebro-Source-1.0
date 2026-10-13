"""Changed-risk checks for task-local capacity and return-care integration.

All fixtures are synthetic local records. They prove code-path behavior only,
not native reservations, worker identity, profile use, care completion, or Source
publication.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


capture = load("capacity_capture", "tooling/return-bridge/rom_a_dispatch_capture.py")
normal = load("capacity_normal", "tooling/return-bridge/rom_a_normal_path.py")
profile = load("capacity_profile", "tooling/return-bridge/task_local_profile.py")
capacity = load("capacity_care", "tooling/return-bridge/task_capacity_care.py")
sys.path.insert(0, str(ROOT / "candidates/bk05-capsule-tool-v1/src"))
sys.path.insert(0, str(ROOT / "mcp"))
from bk05_capsule.synthetic import positive_fixture  # noqa: E402
from control_resolution_host import BoundControlResolutionHost  # noqa: E402


class CapacityCareFlow(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = positive_fixture()
        manifest = json.loads(self.fixture["parent_manifest.json"])
        self.task = {
            "task_ref": manifest["task_ref"], "task_revision": manifest["parent_revision"],
            "actor_ref": manifest["actor_ref"], "generation_ref": "SYN-GEN",
            "carrier_ref": "SYN-CARRIER", "arc_ref": "SYN-ARC",
            **{name: manifest[name] for name in normal.PARENT_FIELDS},
        }
        self.original = self.fixture["parent.txt"]
        returned = {name: self.task[name] for name in
                    ("actor_ref", "generation_ref", "carrier_ref", "arc_ref",
                     "task_ref", "task_revision")}
        returned["original_sha256"] = manifest["parent_sha256"]
        returned["contract_kind"] = "REPAIR"
        self.return_file = self.write_json("return.json", {
            "schema": normal.RETURN_SCHEMA, "observation_ref": "SYN-OBS-1",
            "return": returned,
        })
        self.profile_value = {
            "source_ref": "fixture:profile-source", "source_revision": "r1",
            "primary_coat": "KODEMESTER", "lenses": ["SOURCEINTEGRATOR"],
            "packages": [], "required_tools": ["source-files", "signed-readback"],
        }
        self.request = {"schema": capture.SCHEMA, "task": self.task}

    def write(self, name: str, raw: bytes) -> str:
        path = self.root / name
        path.write_bytes(raw)
        return str(path)

    def write_json(self, name: str, value: object) -> str:
        return self.write(name, (json.dumps(value, sort_keys=True, separators=(",", ":"),
                                             ensure_ascii=False) + "\n").encode("utf-8"))

    def worker(self, actor: str, generation: str, *, state: str, fit: str,
               ui: str = "UNKNOWN", reservation: str | None = None,
               owner: str | None = None, evidence: str | None = None,
               fit_evidence: str | None = None):
        return {"actor_ref": actor, "generation_ref": generation, "state": state,
                "fit": fit, "reservation_ref": reservation,
                "reservation_owner_ref": owner, "evidence_ref": evidence,
                "fit_evidence_ref": fit_evidence,
                "profile_source_ref": "fixture:worker-profile",
                "profile_source_revision": "worker-r1", "ui_state": ui}

    def observation(self, task=None, workers=None, *, cut_revision="cut-r1", prior=None):
        task = task or self.task
        if workers is None:
            workers = [self.worker(task["actor_ref"], task["generation_ref"],
                                   state="AVAILABLE", fit="FIT", ui="IDLE",
                                   evidence="fixture:available", fit_evidence="fixture:fit")]
        return {"schema": capacity.OBSERVATION_SCHEMA, "cut_ref": "fixture:capacity-cut",
                "cut_revision": cut_revision, "observer_ref": "fixture:owner-observation",
                "task_ref": task["task_ref"], "task_revision": task["task_revision"],
                "workers": workers, "prior_care_readback": prior}

    def capture_capacity(self, *, task=None, observation=None, profile_value=None, root_name="cap"):
        task = task or self.task
        observation = observation or self.observation(task)
        return capture.capture(
            self.root / root_name, {"schema": capture.SCHEMA, "task": task}, self.original,
            profile=profile_value or self.profile_value,
            capacity_observation=capacity.canonical(observation))

    def profile_observation(self, cap_path: str, *, delta="NONE_REPORTED"):
        sidecar = Path(cap_path).with_name("task_local_profile.json")
        return {"schema": profile.SCHEMA + "-observation",
                "binding_sha256": profile.sha(sidecar.read_bytes()),
                **{k: self.task[k] for k in ("task_ref", "task_revision", "actor_ref", "generation_ref")},
                "profile_source_ref": self.profile_value["source_ref"],
                "profile_source_revision": self.profile_value["source_revision"],
                "uptake": {"state": "USED", "evidence_ref": "fixture:uptake"},
                "result": {"state": "DELIVERED", "evidence_ref": "fixture:result"},
                "recipient_use": {"state": "USED", "evidence_ref": "fixture:recipient-use"},
                "relevant_delta": {"state": delta,
                                   "evidence_ref": "fixture:delta" if delta == "REPORTED" else "fixture:none"},
                "available_tools": [{"tool_ref": tool, "state": "AVAILABLE",
                                     "evidence_ref": "fixture:tool:" + tool}
                                    for tool in self.profile_value["required_tools"]]}

    def prepare(self, cap, *, care=None, observation=None, suffix="out"):
        kw = {"capture_record": cap["capture_path"], "return_input": self.return_file,
              "out_dir": str(self.root / suffix)}
        if observation is not None:
            kw["profile_observation"] = self.write_json(suffix + "-profile.json", observation)
        if care is not None:
            kw["capacity_care_observation"] = self.write_json(suffix + "-care.json", care)
        return normal.prepare(**kw)

    def care_report(self, cap, *, state="CARE_PENDING", readback=None):
        bound_path = Path(cap["capacity_binding_path"])
        bound = json.loads(bound_path.read_bytes())
        return {"schema": capacity.CARE_SCHEMA, "care_ref": "fixture:care-episode",
                "state": state, "task_ref": self.task["task_ref"],
                "task_revision": self.task["task_revision"],
                "original_sha256": bound["original_sha256"],
                "profile_binding_sha256": bound["profile_binding_sha256"],
                "delta_ref": "fixture:delta", "caretaker_ref": "fixture:Gartner-owner",
                "next_consumer_ref": "fixture:next-normal-task", "readback": readback}

    def owner_record(self, *, stage, task=None, observation=None, currentness="CURRENT"):
        task = task or self.task
        observation = observation or self.observation(task)
        return {"schema": capacity.OWNER_RECORD_SCHEMA,
                "record_ref": "fixture:owner-record:" + stage.lower(),
                "owner_ref": "fixture:task-owner", "stage": stage,
                "task_ref": task["task_ref"], "task_revision": task["task_revision"],
                "source_ref": "fixture:owner-ledger", "source_revision": "owner-r1",
                "currentness": currentness, "observed_at": "2026-10-13T12:00:00Z",
                "observation": observation, "authority": "NONE",
                "assertion": capacity.OWNER_RECORD_ASSERTION}

    def test_fit_and_reservation_are_distinct_from_ui_state(self):
        target = self.worker(self.task["actor_ref"], self.task["generation_ref"],
                             state="AVAILABLE", fit="FIT", ui="IDLE",
                             evidence="fixture:owner-available", fit_evidence="fixture:full-fit")
        e2 = self.worker("E2", "E2-GEN", state="DEPENDENCY_WAIT", fit="FIT", ui="IDLE",
                         reservation="fixture:E2-whole-context", owner="fixture:E2-task",
                         evidence="fixture:E2-owner-reservation", fit_evidence="fixture:E2-fit")
        signer = self.worker("SIGNER", "SIGNER-GEN", state="AVAILABLE", fit="INPUT_ONLY",
                             ui="NOT_LOADED", evidence="fixture:signer-available",
                             fit_evidence="fixture:signer-only-input")
        obs = self.observation(workers=[target, e2, signer])
        cap = self.capture_capacity(observation=obs)
        bound = json.loads(Path(cap["capacity_binding_path"]).read_bytes())
        self.assertEqual(bound["selected_worker"]["actor_ref"], self.task["actor_ref"])
        self.assertEqual(bound["eligible_workers"], [{"actor_ref": self.task["actor_ref"],
                                                       "generation_ref": self.task["generation_ref"]}])
        self.assertEqual(bound["input_only_workers"], [{"actor_ref": "SIGNER",
                                                         "generation_ref": "SIGNER-GEN"}])
        self.assertEqual(bound["reservation_status"], "CALLER_OBSERVED_NOT_NATIVE_LOCKED")
        capture_record = json.loads(Path(cap["capture_path"]).read_bytes())
        self.assertEqual(capture_record["schema"], capture.SCHEMA + "-receipt")
        self.assertNotIn("capacity", capture_record)
        self.assertEqual(Path(cap["original_path"]).read_bytes(), self.original)

        reserved_task = {**self.task, "actor_ref": "E2", "generation_ref": "E2-GEN",
                         "task_ref": self.task["task_ref"] + "-e2"}
        with self.assertRaisesRegex(ValueError, "CAPACITY_WORKER_RESERVED") as reserved:
            self.capture_capacity(task=reserved_task, observation=self.observation(
                reserved_task, workers=[e2, target, signer]), root_name="reserved")
        self.assertEqual(reserved.exception.details["eligible_candidates"][0]["actor_ref"],
                         self.task["actor_ref"])
        self.assertEqual(reserved.exception.details["next_action"],
                         "RETURN_TO_OWNER_ROUTE_FOR_TASK_REBIND_OR_MORE_EVIDENCE")
        self.assertFalse((self.root / "reserved").exists())

    def test_cross_room_fit_returns_redistribution_without_human_loop(self):
        alternate = self.worker("ALT", "ALT-GEN", state="AVAILABLE", fit="FIT",
                                evidence="fixture:alternate-available", fit_evidence="fixture:alternate-fit")
        requested = {**self.task, "actor_ref": "MISMATCH", "generation_ref": "MISMATCH-GEN"}
        with self.assertRaisesRegex(ValueError, "CAPACITY_REDISTRIBUTION_REQUIRED") as redistribution:
            self.capture_capacity(task=requested, observation=self.observation(
                requested, workers=[alternate]), root_name="redistribution")
        self.assertEqual(redistribution.exception.details["eligible_candidates"][0]["actor_ref"], "ALT")
        self.assertFalse((self.root / "redistribution").exists())

        signer_task = {**self.task, "actor_ref": "SIGNER", "generation_ref": "SIGNER-GEN",
                       "task_ref": self.task["task_ref"] + "-signer"}
        signer_only = self.worker("SIGNER", "SIGNER-GEN", state="AVAILABLE", fit="INPUT_ONLY",
                                  evidence="fixture:signer-available", fit_evidence="fixture:signer-input")
        with self.assertRaisesRegex(ValueError, "CAPACITY_REDISTRIBUTION_REQUIRED"):
            self.capture_capacity(task=signer_task, observation=self.observation(
                signer_task, workers=[signer_only, alternate]), root_name="signer-redistribution")
        self.assertFalse((self.root / "signer-redistribution").exists())

    def test_same_key_capacity_change_is_a_collision_without_rewriting_capture(self):
        cap = self.capture_capacity()
        path = Path(cap["capacity_binding_path"])
        before = path.read_bytes()
        fresh = self.observation(cut_revision="cut-r2")
        with self.assertRaisesRegex(ValueError, "CAPACITY_SIDECAR_COLLISION"):
            self.capture_capacity(observation=fresh)
        self.assertEqual(path.read_bytes(), before)

    def test_existing_capture_cli_accepts_the_additive_capacity_sidecar(self):
        request_path = self.root / "request.json"
        request_path.write_text(json.dumps(self.request, sort_keys=True), encoding="utf-8")
        original_path = self.root / "original.txt"
        original_path.write_bytes(self.original)
        profile_path = self.root / "profile.json"
        profile_path.write_text(json.dumps(self.profile_value, sort_keys=True), encoding="utf-8")
        observation_path = self.root / "capacity.json"
        observation_path.write_bytes(capacity.canonical(self.observation()))
        proc = subprocess.run([
            sys.executable, "-I", "-B", str(ROOT / "tooling/return-bridge/rom_a_dispatch_capture.py"),
            "--request", str(request_path), "--original", str(original_path),
            "--capture-root", str(self.root / "cli-captures"),
            "--profile-binding", str(profile_path), "--capacity-observation", str(observation_path),
        ], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads(proc.stdout)
        self.assertTrue(Path(result["capacity_binding_path"]).is_file())

        reserved_task = {**self.task, "task_ref": self.task["task_ref"] + "-reserved",
                         "actor_ref": "E2", "generation_ref": "E2-GEN"}
        request_path.write_text(json.dumps({"schema": capture.SCHEMA, "task": reserved_task},
                                           sort_keys=True), encoding="utf-8")
        reserved = self.worker("E2", "E2-GEN", state="RESERVED", fit="FIT", ui="IDLE",
                               reservation="fixture:E2-reservation", owner="fixture:E2-task",
                               evidence="fixture:E2-reserved", fit_evidence="fixture:E2-fit")
        alternate = self.worker(self.task["actor_ref"], self.task["generation_ref"],
                                state="AVAILABLE", fit="FIT", evidence="fixture:alternate-ready",
                                fit_evidence="fixture:alternate-fit")
        observation_path.write_bytes(capacity.canonical(self.observation(
            task=reserved_task, workers=[reserved, alternate])))
        proc = subprocess.run([
            sys.executable, "-I", "-B", str(ROOT / "tooling/return-bridge/rom_a_dispatch_capture.py"),
            "--request", str(request_path), "--original", str(original_path),
            "--capture-root", str(self.root / "cli-reserved"),
            "--profile-binding", str(profile_path), "--capacity-observation", str(observation_path),
        ], capture_output=True, text=True, check=False)
        refusal = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(refusal["reason"], "CAPACITY_WORKER_RESERVED")
        self.assertEqual(refusal["capacity_refusal"]["eligible_candidates"][0]["actor_ref"],
                         self.task["actor_ref"])
        self.assertFalse((self.root / "cli-reserved").exists())

    def test_return_care_pending_then_readback_fixture_is_consumed_by_next_profile(self):
        cap = self.capture_capacity()
        profile_obs = self.profile_observation(cap["capture_path"], delta="REPORTED")
        pending = self.care_report(cap)
        result = self.prepare(cap, care=pending, observation=profile_obs, suffix="pending")
        receipt = json.loads(Path(result["receipt"]).read_bytes())
        care = receipt["task_capacity_care"]["return_care"]
        self.assertEqual(care["state"], "CARE_PENDING")
        self.assertEqual(care["caretaker_ref"], "fixture:Gartner-owner")
        self.assertEqual(care["next_consumer_ref"], "fixture:next-normal-task")
        self.assertEqual(receipt["dispatch"]["state"], "NOT_SENT")
        self.assertEqual(receipt["task_capacity_care"]["dispatch_recheck"]["state"],
                         "REQUIRED_BEFORE_INTENT_AND_SENDER")
        self.assertEqual(receipt["authority"], "NONE")
        self.assertEqual(receipt["capture"]["original_sha256"], capacity.sha(self.original))
        self.assertEqual(receipt["return_input"]["sha256"],
                         capacity.sha(Path(self.return_file).read_bytes()))
        self.assertEqual(receipt["adoption"]["task_ref"], self.task["task_ref"])
        self.assertEqual(receipt["effect"], "NONE_CLAIMED")
        self.assertEqual(Path(result["selected"]).read_bytes(), self.original)

        reused = self.care_report(cap, state="REUSED")
        reused_result = self.prepare(cap, care=reused, observation=profile_obs, suffix="reused")
        reused_receipt = json.loads(Path(reused_result["receipt"]).read_bytes())
        self.assertEqual(reused_receipt["task_capacity_care"]["return_care"]["state"], "REUSED")

        readback = {"care_ref": "fixture:care-episode", "delta_ref": "fixture:delta",
                    "source_ref": "fixture:updated-profile",
                    "source_revision": "r2", "source_sha256": "a" * 64,
                    "readback_ref": "fixture:actual-source-readback"}
        done = self.care_report(cap, state="CARE_DONE", readback=readback)
        done_result = self.prepare(cap, care=done, observation=profile_obs, suffix="done")
        done_receipt = json.loads(Path(done_result["receipt"]).read_bytes())
        self.assertEqual(done_receipt["task_capacity_care"]["return_care"]["state"], "CARE_DONE")
        self.assertEqual(done_receipt["task_local_profile"]["recipient_use"]["state"], "USED")

        next_task = {**self.task, "task_ref": self.task["task_ref"] + "-next"}
        next_profile = {**self.profile_value, "source_ref": "fixture:updated-profile",
                        "source_revision": "r2"}
        next_obs = self.observation(task=next_task, prior=readback)
        next_cap = self.capture_capacity(task=next_task, observation=next_obs,
                                         profile_value=next_profile, root_name="next")
        next_bound = json.loads(Path(next_cap["capacity_binding_path"]).read_bytes())
        self.assertEqual(next_bound["care_update_consumption"]["state"],
                         "CONSUMED_BY_ORDINARY_PROFILE_BINDING")
        mismatch = {**next_profile, "source_revision": "r3"}
        with self.assertRaisesRegex(ValueError, "CARE_UPDATED_PROFILE_NOT_CONSUMED"):
            self.capture_capacity(task={**next_task, "task_ref": next_task["task_ref"] + "-bad"},
                                  observation=self.observation(
                                      task={**next_task, "task_ref": next_task["task_ref"] + "-bad"},
                                      prior=readback), profile_value=mismatch, root_name="stale-care")
        self.assertFalse((self.root / "stale-care").exists())

    def test_dispatch_requires_two_fresh_checks_and_holds_changed_reservation(self):
        cap = self.capture_capacity()
        result = self.prepare(cap, suffix="dispatch")
        sent = []

        class Sender:
            def send_selected(self, **kwargs):
                sent.append(kwargs)
                return {"state": "ACCEPTED", "recipient_ref": kwargs["recipient_ref"],
                        "selected_sha256": kwargs["selected_sha256"],
                        "delivery_ref": "fixture:accepted"}

        current = capacity.canonical(self.observation())
        calls = []
        def stable_reader():
            calls.append("read")
            return current
        with self.assertRaisesRegex(ValueError, "CAPACITY_RECHECK_REQUIRED"):
            normal._dispatch_selected(result, Sender())
        self.assertFalse(Path(result["receipt"]).with_name(".receipt-dispatch-intent.json").exists())

        changed_worker = self.worker(self.task["actor_ref"], self.task["generation_ref"],
                                     state="RESERVED", fit="FIT", ui="IDLE",
                                     reservation="fixture:new-reservation", owner="fixture:other-task",
                                     evidence="fixture:reservation-change", fit_evidence="fixture:fit")
        changed = capacity.canonical(self.observation(workers=[changed_worker], cut_revision="cut-r2"))
        sequence = iter([current, changed])
        def changing_reader():
            return next(sequence)
        with self.assertRaisesRegex(ValueError, "CAPACITY_CHANGED_BEFORE_SENDER_NO_RETRY"):
            normal._dispatch_selected(result, Sender(), capacity_reader=changing_reader)
        receipt = json.loads(Path(result["receipt"]).read_bytes())
        self.assertEqual(receipt["dispatch"]["state"], "NOT_SENT_CAPACITY_RECHECK_FAILED")
        self.assertIs(receipt["dispatch"]["sender_called"], False)
        self.assertEqual(receipt["effect"], "NONE_CLAIMED")
        self.assertEqual(sent, [])

        unknown_result = self.prepare(self.capture_capacity(root_name="unknown-dispatch"),
                                      suffix="unknown-dispatch-output")
        unknown_worker = self.worker(self.task["actor_ref"], self.task["generation_ref"],
                                     state="UNKNOWN", fit="FIT", ui="ACTIVE",
                                     evidence=None, fit_evidence="fixture:fit")
        unknown_cut = capacity.canonical(self.observation(workers=[unknown_worker],
                                                           cut_revision="cut-unknown"))
        sequence = iter([current, unknown_cut])
        with self.assertRaisesRegex(ValueError, "CAPACITY_CHANGED_BEFORE_SENDER_NO_RETRY"):
            normal._dispatch_selected(unknown_result, Sender(), capacity_reader=lambda: next(sequence))
        unknown_receipt = json.loads(Path(unknown_result["receipt"]).read_bytes())
        self.assertEqual(unknown_receipt["dispatch"]["capacity_recheck_failure"], "CAPACITY_UNKNOWN")
        with self.assertRaisesRegex(ValueError, "READBACK_MISMATCH"):
            normal._dispatch_selected(unknown_result, Sender(), capacity_reader=lambda: current)
        self.assertEqual(sent, [])

    def test_no_delta_and_legacy_task_keep_low_ceremony_states(self):
        cap = self.capture_capacity()
        none_obs = self.profile_observation(cap["capture_path"], delta="NONE_REPORTED")
        result = self.prepare(cap, observation=none_obs, suffix="none")
        receipt = json.loads(Path(result["receipt"]).read_bytes())
        self.assertEqual(receipt["task_capacity_care"]["return_care"]["state"], "NONE")

        legacy_cap = capture.capture(self.root / "legacy", self.request, self.original)
        legacy_result = normal.prepare(
            capture_record=legacy_cap["capture_path"], return_input=self.return_file,
            out_dir=str(self.root / "legacy-output"))
        legacy_receipt = json.loads(Path(legacy_result["receipt"]).read_bytes())
        self.assertNotIn("task_capacity_care", legacy_receipt)
        self.assertEqual(legacy_receipt["dispatch"]["state"], "NOT_SENT")

    def test_stable_dispatch_rechecks_before_intent_and_sender(self):
        cap = self.capture_capacity()
        result = self.prepare(cap, suffix="stable")
        sent = []
        class Sender:
            def send_selected(self, **kwargs):
                sent.append(kwargs)
                return {"state": "ACCEPTED", "recipient_ref": kwargs["recipient_ref"],
                        "selected_sha256": kwargs["selected_sha256"],
                        "delivery_ref": "fixture:accepted-stable"}
        current = capacity.canonical(self.observation())
        reads = []
        def reader():
            reads.append(True)
            return current
        result = normal._dispatch_selected(result, Sender(), capacity_reader=reader)
        receipt = json.loads(Path(result["receipt"]).read_bytes())
        self.assertEqual(len(reads), 2)
        self.assertEqual(len(sent), 1)
        self.assertEqual(receipt["task_capacity_care"]["dispatch_recheck"]["state"],
                         "PASS_LOCAL_OBSERVATION_BEFORE_INTENT_AND_SENDER")
        self.assertEqual(receipt["recipient_use"]["state"], "NOT_OBSERVED")
        self.assertEqual(receipt["authority"], "NONE")

    def test_nc1_constructors_reject_callback_only_preserve_named_reader_and_none(self):
        """NC1 only: actual constructors reject the frozen-cut shortcut before effects."""
        from unittest.mock import Mock, patch
        import control_context_remote_runtime as runtime
        self.assertEqual(Path(runtime.__file__).resolve(),
                         (ROOT / "mcp/control_context_remote_runtime.py").resolve())
        self.assertEqual(Path(sys.modules[BoundControlResolutionHost.__module__].__file__).resolve(),
                         (ROOT / "mcp/control_resolution_host.py").resolve())

        frozen_cut = capacity.canonical(self.observation())
        callback_reads = []
        def callback():
            callback_reads.append(True)
            return frozen_cut
        sender = Mock()
        persistence = Mock()
        capability = Mock()
        with self.assertRaisesRegex(ValueError, "host-rom-a-capacity-reader-invalid"):
            BoundControlResolutionHost(
                persistence_verifier=persistence, capability_resolver=capability,
                rom_a_selected_dispatcher=sender, rom_a_capacity_reader=callback)
        self.assertEqual(callback_reads, [])
        sender.send_selected.assert_not_called()
        self.assertFalse(list(self.root.rglob(".receipt-dispatch-intent.json")))

        connection = Mock(side_effect=AssertionError("no connection permitted"))
        with patch.object(runtime, "make_psycopg_connection_factory") as make_factory:
            with self.assertRaisesRegex(runtime.ControlContextRemoteRuntimeError,
                                        "runtime-rom-a-capacity-reader-invalid"):
                runtime.assemble_postgres_control_context_remote_runtime_from_connection_factory(
                    config=None, connection_factory=connection, token_verifier=None,
                    resolution_attestation_verifier=None, rom_a_capacity_reader=callback)
            with self.assertRaisesRegex(runtime.ControlContextRemoteRuntimeError,
                                        "runtime-rom-a-capacity-reader-invalid"):
                runtime.assemble_postgres_control_context_remote_runtime(
                    config=None, postgres_dsn="synthetic-not-used", token_verifier=None,
                    resolution_attestation_verifier=None, rom_a_capacity_reader=callback)
            make_factory.assert_not_called()
        connection.assert_not_called()
        self.assertEqual(callback_reads, [])
        sender.send_selected.assert_not_called()

        named = capacity.OwnerCapacityObservationReader(
            dispatch_record=self.root / "owner-dispatch.json",
            uptake_record=self.root / "owner-uptake.json")
        for reader in (named, None):
            host = BoundControlResolutionHost(
                persistence_verifier=persistence, capability_resolver=capability,
                rom_a_selected_dispatcher=sender, rom_a_capacity_reader=reader)
            self.assertIs(host._rom_a_capacity_reader, reader)
            # The connection-factory entry passes the reader guard and reaches
            # its unchanged config guard; no runtime assembly/effect is run.
            with self.assertRaisesRegex(runtime.ControlContextRemoteRuntimeError,
                                        "^runtime-config-required$"):
                runtime.assemble_postgres_control_context_remote_runtime_from_connection_factory(
                    config=None, connection_factory=connection, token_verifier=None,
                    resolution_attestation_verifier=None, rom_a_capacity_reader=reader)
        sender.send_selected.assert_not_called()
        connection.assert_not_called()

    def test_owner_record_reader_drives_dispatch_and_real_uptake_callsite(self):
        cap = self.capture_capacity(root_name="owner-capture")
        dispatch_path = Path(self.write("owner-dispatch.json", capacity.canonical(
            self.owner_record(stage="DISPATCH"))))
        uptake_worker = self.worker(self.task["actor_ref"], self.task["generation_ref"],
                                    state="RESERVED", fit="FIT", ui="IDLE",
                                    reservation="fixture:task-reservation",
                                    owner=self.task["task_ref"], evidence="fixture:reserved",
                                    fit_evidence="fixture:fit")
        uptake_path = Path(self.write("owner-uptake.json", capacity.canonical(
            self.owner_record(stage="UPTAKE", observation=self.observation(
                workers=[uptake_worker], cut_revision="cut-uptake")))))
        reader = capacity.OwnerCapacityObservationReader(
            dispatch_record=dispatch_path, uptake_record=uptake_path)
        context_path = self.write_json("owner-context.json", {
            "content_ref": "fixture:context", "selector_kind": "TAB", "selector": "current"})
        sends = []

        class ContentReader:
            def read_current(self, content_ref, kind, selector):
                return {"content_ref": content_ref, "selector_kind": kind, "selector": selector,
                        "revision": "r1", "authority_ref": "fixture:source", "currentness": "CURRENT",
                        "provider_readback_verified": True, "access_verified": True,
                        "access_scope_ref": "fixture:scope", "provenance_refs": ["fixture:provider"],
                        "parts": {"current": "Current"}}

        class Sender:
            def send_selected(self, **kwargs):
                sends.append(kwargs)
                return {"state": "ACCEPTED", "recipient_ref": kwargs["recipient_ref"],
                        "selected_sha256": kwargs["selected_sha256"],
                        "delivery_ref": "fixture:owner-dispatch"}

        class Persistence:
            def verify(self, **kwargs):
                return {}

        class Capability:
            def is_available(self, **kwargs):
                return False
            def executor(self, **kwargs):
                raise AssertionError("not-used")

        host = BoundControlResolutionHost(
            persistence_verifier=Persistence(), capability_resolver=Capability(),
            bounded_content_provider=ContentReader(), rom_a_selected_dispatcher=Sender(),
            rom_a_capacity_reader=reader)
        result = host.prepare_rom_a_return(
            capture_record=cap["capture_path"], return_input=self.return_file,
            out_dir=str(self.root / "owner-return"), bounded_context=context_path)
        receipt = json.loads(Path(result["receipt"]).read_bytes())
        check = receipt["task_capacity_care"]["dispatch_recheck"]
        self.assertEqual(check["before_intent"]["owner_record"]["record_ref"],
                         "fixture:owner-record:dispatch")
        self.assertEqual(check["before_sender"]["owner_record"]["assertion"],
                         capacity.OWNER_RECORD_ASSERTION)
        self.assertEqual(len(sends), 1)

        uptake = host.accept_rom_a_task_uptake(capture_record=cap["capture_path"])
        self.assertEqual(uptake["owner_record"]["record_ref"], "fixture:owner-record:uptake")
        self.assertEqual(uptake["uptake"]["reservation_owner_ref"], self.task["task_ref"])
        self.assertEqual(uptake["action"], "CALLER_MAY_CONTINUE_TO_EXISTING_START_GATE")
        self.assertEqual(uptake["authority"], "NONE")

        stale_path = Path(self.write("owner-uptake-stale.json", capacity.canonical(
            self.owner_record(stage="UPTAKE", observation=self.observation(
                workers=[uptake_worker]), currentness="UNKNOWN"))))
        stale_reader = capacity.OwnerCapacityObservationReader(
            dispatch_record=dispatch_path, uptake_record=stale_path)
        host._rom_a_capacity_reader = stale_reader
        with self.assertRaisesRegex(ValueError, "CAPACITY_OWNER_RECORD_NOT_CURRENT"):
            host.accept_rom_a_task_uptake(capture_record=cap["capture_path"])

        wrong_owner = self.worker(self.task["actor_ref"], self.task["generation_ref"],
                                  state="RESERVED", fit="FIT", reservation="fixture:other",
                                  owner="fixture:other-task", evidence="fixture:reserved",
                                  fit_evidence="fixture:fit")
        uptake_path.write_bytes(capacity.canonical(self.owner_record(
            stage="UPTAKE", observation=self.observation(workers=[wrong_owner]))))
        host._rom_a_capacity_reader = reader
        with self.assertRaisesRegex(ValueError, "CAPACITY_UPTAKE_RESERVATION_OWNER_MISMATCH"):
            host.accept_rom_a_task_uptake(capture_record=cap["capture_path"])



class OwnerPortDelta(unittest.TestCase):
    """Only the eight Human-requested owner-port delta controls; synthetic inputs."""
    setUp = CapacityCareFlow.setUp
    write = CapacityCareFlow.write
    write_json = CapacityCareFlow.write_json
    worker = CapacityCareFlow.worker
    observation = CapacityCareFlow.observation
    capture_capacity = CapacityCareFlow.capture_capacity

    def port_observation(self, *, phase="PREPARATION", delegation=None, ports=True):
        obs = self.observation()
        obs.update(schema=capacity.OBSERVATION_SCHEMA_V2, work_phase=phase,
                   selected_actor_ref=self.task["actor_ref"], attempt_ref="fixture:current-attempt",
                   input_kind="TASK_ASSIGNMENT")
        for worker in obs["workers"]:
            worker.update(current_work={"state": "NONE", "task_ref": None, "task_revision": None,
                                        "attempt_ref": None, "currentness": "CURRENT", "evidence_ref": "fixture:work-read"},
                          pending_bindings=[], pending_currentness="CURRENT", pending_evidence_ref="fixture:pending-read")
        req = {"port_ref": "fixture:D1-protected-issuer", "function_ref": "fixture:issuer-function",
               "effect_scope": "fixture:existing-READY-release", "owner_ref": "fixture:D1",
               "priority_owner_ref": "fixture:existing-priority-owner",
               "preparation_actor_ref": self.task["actor_ref"],
               "prepared_input_ref": "fixture:prepared-current-input",
               "next_consumer_ref": self.task["actor_ref"]}
        port = {"port_ref": req["port_ref"], "source_ref": "fixture:D1-owner-original",
                "source_revision": "owner-r1", "currentness": "CURRENT", "state": "BUSY",
                "evidence_ref": "fixture:owner-busy", "active_fronts": [
                    {"task_ref": "fixture:front1", "state": "ACTIVE"},
                    {"task_ref": "fixture:front2", "state": "ACTIVE"}],
                "prepared_input": {"input_ref": req["prepared_input_ref"], "source_revision": "input-r1",
                                   "currentness": "CURRENT", "evidence_ref": "fixture:input-readback"},
                "delegation": delegation, "release": None, "consumer_use": None}
        obs["owner_port_contract"] = {"case_ref": "fixture:existing-civilization-case",
            "governing_basis_refs": ["fixture:existing-governing-original"],
            "applicable_control_refs": ["fixture:existing-control"],
            "requirements": [req], "ports": [port] if ports else [], "owner_disposition": []}
        return obs

    def delegation(self):
        return {"delegation_ref": "fixture:existing-qualified-delegation", "owner_ref": "fixture:D1",
                "delegate_ref": self.task["actor_ref"], "task_ref": self.task["task_ref"],
                "effect_scope": "fixture:existing-READY-release", "port_revision": "owner-r1",
                "currentness": "CURRENT", "evidence_ref": "fixture:delegation-readback",
                "governing_basis_refs": ["fixture:existing-governing-original"],
                "applicable_control_refs": ["fixture:existing-control"]}

    def bound(self, obs):
        cap = self.capture_capacity(observation=obs)
        return cap, json.loads(Path(cap["capacity_binding_path"]).read_bytes())

    def stage_record(self, obs, stage):
        return {"schema": capacity.OWNER_RECORD_SCHEMA_V2, "record_ref": "fixture:record:" + stage,
                "owner_ref": "fixture:existing-task-owner", "stage": stage,
                "task_ref": self.task["task_ref"], "task_revision": self.task["task_revision"],
                "source_ref": "fixture:current-owner-route", "source_revision": obs["cut_revision"],
                "currentness": "CURRENT", "observed_at": "2026-10-13T12:00:00Z", "observation": obs,
                "authority": "NONE", "assertion": capacity.OWNER_RECORD_ASSERTION}

    def test_01_free_worker_without_required_port_is_not_relief(self):
        obs = self.port_observation(ports=False)
        _, bound = self.bound(obs)
        assessment = bound["owner_port_assessment"]
        self.assertFalse(assessment["effect_ready"])
        self.assertFalse(assessment["relief_observed"])
        self.assertTrue(assessment["preparation_permitted"])
        legacy = self.capture_capacity(observation=self.observation(), root_name="unrelated-v1")
        legacy_bound = json.loads(Path(legacy["capacity_binding_path"]).read_bytes())
        self.assertEqual(legacy_bound["schema"], capacity.SCHEMA)
        self.assertNotIn("owner_port_assessment", legacy_bound)
        effect = self.port_observation(phase="EFFECT", ports=False)
        with self.assertRaisesRegex(ValueError, "OWNER_PORT_EFFECT_BLOCKED"):
            capacity.require_owner_ports(effect)

    def test_02_same_busy_port_multiple_fronts_existing_case_disposition(self):
        obs = self.port_observation()
        assessment = capacity.owner_port_assessment(obs)
        port = assessment["ports"][0]
        self.assertTrue(port["multiple_front_bottleneck"])
        self.assertEqual(len(port["active_fronts"]), 2)
        self.assertEqual(assessment["case_ref"], "fixture:existing-civilization-case")
        self.assertEqual(port["next_action"], "EXISTING_PRIORITY_OWNER_DISPOSE_EXACT_REST")

    def test_03_preparation_stays_with_room_not_unique_owner(self):
        obs = self.port_observation()
        _, bound = self.bound(obs)
        port = bound["owner_port_assessment"]["ports"][0]
        self.assertEqual(port["preparation_actor_ref"], self.task["actor_ref"])
        self.assertEqual(port["owner_action"], "ONLY_PREPARED_IRREDUCIBLE_ACTION")
        # A1's attributed Human-corrected E1 stacking recurrence motivates this
        # synthetic discriminator; it is not evidence of actual installer relief.
        occupied = self.port_observation()
        occupied["workers"][0]["current_work"].update(
            state="ACTIVE", task_ref="fixture:competing-Context-task",
            task_revision="r1", attempt_ref="fixture:other-attempt")
        occupied["workers"][0]["pending_bindings"] = [{
            "task_ref": "fixture:competing-Context-task", "task_revision": "r1",
            "attempt_ref": "fixture:other-attempt", "state": "DISPATCH_INTENT",
            "currentness": "CURRENT", "evidence_ref": "fixture:pending-original"}]
        with self.assertRaisesRegex(ValueError, "CAPACITY_CURRENT_OR_PENDING_OCCUPIED"):
            self.capture_capacity(observation=occupied, root_name="occupied")
        for currentness in ("STALE", "UNKNOWN"):
            uncertain = self.port_observation()
            uncertain["workers"][0]["pending_currentness"] = currentness
            with self.subTest(pending=currentness), self.assertRaisesRegex(ValueError, "CAPACITY_CURRENT_OR_PENDING_UNKNOWN"):
                self.capture_capacity(observation=uncertain, root_name="uncertain-" + currentness)
        same = self.port_observation()
        worker = same["workers"][0]
        worker.update(state="RUNNING", reservation_ref="fixture:same-existing-reservation",
                      reservation_owner_ref=self.task["task_ref"])
        worker["current_work"].update(state="ACTIVE", task_ref=self.task["task_ref"],
                                      task_revision=self.task["task_revision"], attempt_ref=same["attempt_ref"])
        same["input_kind"] = "MATERIAL_INPUT"
        accepted = self.capture_capacity(observation=same, root_name="material-same")
        self.assertTrue(Path(accepted["capture_path"]).is_file())
        obs["owner_port_contract"]["requirements"][0]["preparation_actor_ref"] = "fixture:D1"
        with self.assertRaisesRegex(ValueError, "OWNER_PORT_DELEGABLE_PREPARATION_ON_UNIQUE_OWNER"):
            capacity.owner_port_assessment(obs)

    def test_04_current_same_scope_delegation_reused_at_dispatch_seam(self):
        obs = self.port_observation(phase="EFFECT", delegation=self.delegation())
        _, bound = self.bound(obs)
        path = Path(self.write_json("dispatch-owner.json", self.stage_record(obs, "DISPATCH")))
        reader = capacity.OwnerCapacityObservationReader(dispatch_record=path,
                                                        uptake_record=self.root / "uptake.json")
        check = normal._capacity_dispatch_read(reader, bound, capacity)
        port = check["owner_ports"]["ports"][0]
        self.assertEqual(port["reason"], "REUSE_CURRENT_SAME_SCOPE_DELEGATION")
        self.assertEqual(port["delegation_ref"], self.delegation()["delegation_ref"])
        self.assertFalse(check["owner_ports"]["relief_observed"])

    def test_05_irreducible_rest_exact_owner_priority_and_next_consumer(self):
        obs = self.port_observation()
        obs["owner_port_contract"]["owner_disposition"] = [{
            "port_ref": "fixture:D1-protected-issuer", "disposition_ref": "fixture:existing-owner-disposition",
            "priority_owner_ref": "fixture:existing-priority-owner", "action": "reuse-current-protected-request",
            "evidence_ref": "fixture:disposition-original"}]
        assessment = capacity.owner_port_assessment(obs)
        port = assessment["ports"][0]
        self.assertEqual(port["owner_ref"], "fixture:D1")
        self.assertEqual(port["next_consumer_ref"], self.task["actor_ref"])
        self.assertEqual(port["priority_owner_ref"], "fixture:existing-priority-owner")
        self.assertIsNotNone(port["owner_disposition"])
        self.assertFalse(assessment["effect_ready"])

    def test_06_stale_or_out_of_scope_delegation_refuses_affected_effect(self):
        obs = self.port_observation(phase="EFFECT", delegation=self.delegation())
        _, bound = self.bound(obs)
        path = self.root / "dispatch-owner.json"
        reader = capacity.OwnerCapacityObservationReader(dispatch_record=path,
                                                        uptake_record=self.root / "uptake.json")
        for field, value in (("currentness", "STALE"), ("port_revision", "owner-old"),
                             ("effect_scope", "fixture:different-effect")):
            changed = json.loads(json.dumps(obs))
            changed["owner_port_contract"]["ports"][0]["delegation"][field] = value
            path.write_bytes(capacity.canonical(self.stage_record(changed, "DISPATCH")))
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "OWNER_PORT_EFFECT_BLOCKED"):
                normal._capacity_dispatch_read(reader, bound, capacity)
        self.assertFalse(list(self.root.rglob(".receipt-dispatch-intent.json")))

    def test_07_host_material_return_requires_release_and_real_consumer_evidence(self):
        from unittest.mock import Mock, patch
        import control_resolution_host as host_module
        obs = self.port_observation()
        cap, _ = self.bound(obs)
        path = Path(self.write_json("material-owner.json", self.stage_record(obs, "MATERIAL_RETURN")))
        reader = capacity.OwnerCapacityObservationReader(
            dispatch_record=self.root / "dispatch.json", uptake_record=self.root / "uptake.json",
            material_return_record=path)
        host = BoundControlResolutionHost(persistence_verifier=Mock(), capability_resolver=Mock(),
                                          rom_a_capacity_reader=reader)
        target = {"content_ref": "fixture:context", "selector_kind": "TAB", "selector": "current"}
        context = self.write_json("context.json", target)
        host.select_bounded_content = Mock(return_value={**target, "selected_text": "fixture:context"})
        import shutil
        scratch = self.root / "integration-source"
        shutil.copytree(ROOT, scratch)
        dependency_root = Path(host_module.control_resolution.__file__).resolve().parents[1]
        shutil.copy2(dependency_root / "tooling/return-bridge/rom_a_return_adoption.py",
                     scratch / "tooling/return-bridge/rom_a_return_adoption.py")
        with patch.object(host_module.control_resolution, "SOURCE_ROOT", scratch):
            first = host.prepare_rom_a_return(capture_record=cap["capture_path"],
                return_input=self.return_file, out_dir=str(self.root / "return-pending"), bounded_context=context)
            self.assertFalse(first["owner_port_return"]["relief_observed"])
            obs["workers"][0]["pending_currentness"] = "UNKNOWN"
            path.write_bytes(capacity.canonical(self.stage_record(obs, "MATERIAL_RETURN")))
            held = host.prepare_rom_a_return(capture_record=cap["capture_path"],
                return_input=self.return_file, out_dir=str(self.root / "return-uptake-held"), bounded_context=context)
            self.assertEqual(held["owner_port_return"]["work_disposition"]["state"], "AFFECTED_UPTAKE_HELD")
            self.assertTrue(held["owner_port_return"]["work_disposition"]["material_return_permitted"])
            obs["workers"][0]["pending_currentness"] = "CURRENT"
            port = obs["owner_port_contract"]["ports"][0]
            req = obs["owner_port_contract"]["requirements"][0]
            port["state"] = "RELEASED"
            port["release"] = {"state": "RELEASED", "evidence_ref": "fixture:owner-release",
                               "owner_ref": req["owner_ref"], "task_ref": self.task["task_ref"],
                               "effect_scope": req["effect_scope"], "port_revision": port["source_revision"]}
            path.write_bytes(capacity.canonical(self.stage_record(obs, "MATERIAL_RETURN")))
            second = host.prepare_rom_a_return(capture_record=cap["capture_path"],
                return_input=self.return_file, out_dir=str(self.root / "return-release-only"), bounded_context=context)
            self.assertFalse(second["owner_port_return"]["relief_observed"])
            port["consumer_use"] = {"state": "USED", "evidence_ref": "fixture:next-consumer-readback",
                                    "consumer_ref": req["next_consumer_ref"], "release_ref": "fixture:owner-release"}
            path.write_bytes(capacity.canonical(self.stage_record(obs, "MATERIAL_RETURN")))
            third = host.prepare_rom_a_return(capture_record=cap["capture_path"],
                return_input=self.return_file, out_dir=str(self.root / "return-with-use"), bounded_context=context)
            self.assertTrue(third["owner_port_return"]["relief_observed"])
            receipt = json.loads(Path(third["receipt"]).read_bytes())
            self.assertEqual(receipt["task_capacity_care"]["owner_port_return"]["dependencies"][0]["dependency_state"],
                             "OWNER_REPORTED_RELEASE_AND_CONSUMER_USE")
            self.assertEqual(receipt["dispatch"]["state"], "NOT_SENT")
            self.assertEqual(receipt["authority"], "NONE")
        # All release/use inputs above are synthetic. This is code-path evidence only.

    def test_08_closed_bk11_boot_fronts_remain_closed_history(self):
        obs = self.port_observation()
        fronts = [{"task_ref": "fixture:closed-BK11-attempt", "state": "CLOSED"},
                  {"task_ref": "fixture:stopped-BOOT-attempt", "state": "STOPPED"},
                  {"task_ref": self.task["task_ref"], "state": "ACTIVE"}]
        obs["owner_port_contract"]["ports"][0]["active_fronts"] = fronts
        before = json.dumps(obs, sort_keys=True)
        result = capacity.owner_port_assessment(obs)
        self.assertEqual(result["ports"][0]["active_fronts"], [self.task["task_ref"]])
        self.assertEqual(json.dumps(obs, sort_keys=True), before)
        self.assertFalse(result["ports"][0]["multiple_front_bottleneck"])
        completed = self.port_observation()
        completed["workers"][0]["current_work"].update(
            state="COMPLETED", task_ref=self.task["task_ref"],
            task_revision=self.task["task_revision"], attempt_ref=completed["attempt_ref"])
        with self.assertRaisesRegex(ValueError, "CAPACITY_CURRENT_ATTEMPT_ALREADY_COMPLETED"):
            self.capture_capacity(observation=completed, root_name="closed-exact-attempt")

if __name__ == "__main__":
    unittest.main()
