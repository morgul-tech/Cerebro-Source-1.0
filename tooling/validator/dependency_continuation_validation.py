#!/usr/bin/env python3
"""One deterministic normal-host risk batch for SF57/SF60 continuation."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mcp"))

from control_resolution_host import BoundControlResolutionHost  # noqa: E402


class Persistence:
    def verify(self, **_kwargs):
        return {}


class Capability:
    def is_available(self, **_kwargs):
        return False

    def executor(self, **_kwargs):
        raise AssertionError("unused")


class Reader:
    def __init__(self):
        payload = b"existing authorized task bytes\n"
        self.current = {
            "provider_readback_verified": True, "currentness": "CURRENT",
            "task": {
                "task_ref": "TASK-1", "task_revision": "TREV-1", "actor_ref": "ACTOR-1",
                "authority_ref": "AUTH-1", "authority_state": "AUTHORIZED",
                "scope_ref": "existing scope", "progress_state": "WAITING_DEPENDENCY",
                "paused": False, "revoked": False, "dependency_refs": ["DEP-1"],
                "all_dependencies_resolved": True,
                "other_unresolved_gates": [], "selected_bytes": payload,
                "selected_sha256": hashlib.sha256(payload).hexdigest(),
                "preflight_request": {"stage": "MATERIAL_AUTHORIZE", "material": True,
                                      "commitment_target": "TASK-1",
                                      "resolved_scope": "existing scope"},
            },
            "dependency": {"dependency_ref": "DEP-1", "dependency_revision": "DREV-1",
                           "source_ref": "SOURCE-1", "disposition": "SOURCE_AFTER_DEPENDENCY",
                           "state": "RESOLVED", "applicable": True},
        }
        self.reads = 0
        self.reservations = []
        self.records = []
        self.change_on_second_read = False

    def read_current(self, *, task_ref, dependency_ref):
        assert (task_ref, dependency_ref) == ("TASK-1", "DEP-1")
        self.reads += 1
        if self.change_on_second_read and self.reads == 2:
            self.current["dependency"]["dependency_revision"] = "DREV-2"
        return copy.deepcopy(self.current)

    def reserve_reconsideration(self, **kwargs):
        self.reservations.append(kwargs)
        return {"state": "RESERVED", "basis_fingerprint": kwargs["basis_fingerprint"],
                "task_revision": kwargs["task_revision"],
                "dependency_revision": kwargs["dependency_revision"]}

    def record_reconsideration_result(self, **kwargs):
        self.records.append(kwargs)
        self.current["prior_reconsideration"] = {
            "basis_fingerprint": kwargs["basis_fingerprint"], "result": kwargs["result"]}
        return {"state": "RECORDED", "basis_fingerprint": kwargs["basis_fingerprint"]}


class Sender:
    def __init__(self):
        self.sent = []

    def send_selected(self, **kwargs):
        self.sent.append(kwargs)
        return {"state": "ACCEPTED", "recipient_ref": kwargs["recipient_ref"],
                "selected_sha256": kwargs["selected_sha256"], "delivery_ref": "DELIVERY-1"}


def host(reader, sender):
    return BoundControlResolutionHost(
        persistence_verifier=Persistence(), capability_resolver=Capability(),
        authorized_task_dependency_reader=reader, rom_a_selected_dispatcher=sender)


class DependencyContinuationTests(unittest.TestCase):
    def setUp(self):
        self.reader, self.sender = Reader(), Sender()
        self.host = host(self.reader, self.sender)
        self.resolve_patch = patch(
            "control_resolution_host.material_commitment_preflight.resolve",
            return_value={"result": "PASS", "receipt": {"schema": "test-receipt"}})
        self.consume_patch = patch(
            "control_resolution_host.material_commitment_preflight.consume",
            return_value={"result": "PASS"})
        self.resolve_patch.start(); self.consume_patch.start()
        self.addCleanup(self.resolve_patch.stop); self.addCleanup(self.consume_patch.stop)

    def run_once(self):
        return self.host.reconsider_authorized_task_dependency("TASK-1", "DEP-1", root=ROOT)

    def test_positive_and_same_basis_quiet_reuse(self):
        first = self.run_once()
        self.assertEqual(first["action"], "CONTINUATION_SENT")
        self.assertEqual((first["task_ref"], first["dependency_ref"], first["dependency_source_ref"]),
                         ("TASK-1", "DEP-1", "SOURCE-1"))
        self.assertEqual(first["authority_added"], "NONE")
        self.assertEqual(first["recipient_use"], "NOT_PROVEN")
        self.assertEqual(len(self.sender.sent), 1)
        self.assertEqual(self.sender.sent[0]["selection"], "DEPENDENCY_CONTINUATION")
        second = self.run_once()
        self.assertEqual(second["action"], "QUIET_REUSE")
        self.assertEqual(len(self.sender.sent), 1)
        self.assertEqual(len(self.reader.reservations), 1)

    def test_negative_batch_no_unwanted_consumer_effect(self):
        cases = (
            ("paused", lambda c: c["task"].update(paused=True), "HOLD"),
            ("revoked", lambda c: c["task"].update(revoked=True), "HOLD"),
            ("other-gate", lambda c: c["task"].update(other_unresolved_gates=["GATE-2"]), "PRECISE_REPAIR"),
            ("other-dependency", lambda c: c["task"].update(all_dependencies_resolved=False), "PRECISE_REPAIR"),
            ("refall", lambda c: c["dependency"].update(state="REOPENED"), "HOLD"),
            ("unresolved", lambda c: c["dependency"].update(state="UNRESOLVED"), "HOLD"),
            ("non-applicable", lambda c: c["dependency"].update(applicable=False), "HOLD"),
            ("started", lambda c: c["task"].update(progress_state="STARTED"), "QUIET_REUSE"),
            ("done", lambda c: c["task"].update(progress_state="DONE"), "QUIET_REUSE"),
            ("unknown-authority", lambda c: c["task"].update(authority_state="UNKNOWN"), "HOLD"),
        )
        for name, change, expected in cases:
            with self.subTest(name=name):
                self.reader, self.sender = Reader(), Sender()
                self.host = host(self.reader, self.sender)
                change(self.reader.current)
                self.assertEqual(self.run_once()["action"], expected)
                self.assertEqual(self.sender.sent, [])
                self.assertEqual(self.reader.reservations, [])

    def test_stale_or_wrong_target_fails_before_consumer(self):
        self.reader.change_on_second_read = True
        self.assertEqual(self.run_once()["reason"], "TASK_OR_DEPENDENCY_CHANGED_DURING_PREFLIGHT")
        self.assertEqual(self.sender.sent, [])
        self.assertEqual(self.reader.reservations, [])
        self.reader, self.sender = Reader(), Sender()
        self.host = host(self.reader, self.sender)
        self.reader.current["task"]["dependency_refs"] = ["OTHER-DEP"]
        with self.assertRaisesRegex(ValueError, "authorized-task-dependency-not-owned-by-task"):
            self.run_once()
        self.assertEqual(self.sender.sent, [])

    def test_preflight_or_reservation_refusal_never_dispatches(self):
        with patch("control_resolution_host.material_commitment_preflight.consume",
                   return_value={"result": "BLOCK"}):
            self.assertEqual(self.run_once()["reason"], "MATERIAL_PREFLIGHT_NOT_CURRENT")
        self.assertEqual(self.sender.sent, [])
        self.assertEqual(self.reader.reservations, [])
        with patch.object(self.reader, "reserve_reconsideration",
                          return_value={"state": "ALREADY_RESERVED"}):
            self.assertEqual(self.run_once()["reason"],
                             "RECONSIDERATION_NOT_EXCLUSIVELY_RESERVED")
        self.assertEqual(self.sender.sent, [])
        self.assertEqual(self.reader.reservations, [])

    def test_uncertain_prior_and_missing_consumer_fail_closed(self):
        first = self.run_once()
        self.assertEqual(first["action"], "CONTINUATION_SENT")
        self.reader.current["prior_reconsideration"]["result"] = "SEND_UNCERTAIN"
        self.assertEqual(self.run_once()["reason"], "SAME_BASIS_UNCERTAIN_OR_UNRESOLVED")
        self.assertEqual(len(self.sender.sent), 1)
        fresh = Reader()
        no_consumer = host(fresh, None)
        self.assertEqual(no_consumer.reconsider_authorized_task_dependency(
            "TASK-1", "DEP-1", root=ROOT)["reason"], "NORMAL_CONSUMER_UNBOUND")


if __name__ == "__main__":
    unittest.main()
