from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from signalvev_client.local_runtime_admission import (
    admit_local_runtime_session, read_admitted_local_session,
)
from signalvev_client.local_runtime_session_host import (
    LocalRuntimeSessionError, LocalRuntimeSessionHost,
)


class LocalRuntimeAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = self.root / "profile.json"
        self.profile.write_text(json.dumps({"schema": "cerebro-bk07-normal-use-profile/v1",
                                            "enabled": False}), encoding="utf-8")
        self.state = self.root / "state"
        self.now = 100.0
        self.host = LocalRuntimeSessionHost(profile_path=self.profile, state_dir=self.state,
                                            clock=lambda: self.now)
        self.lease = self.host.start()
        self.addCleanup(self.host.stop)
        self.binding = {
            "binding_id": "CSB-1", "binding_fingerprint": "f" * 64,
            "project_revision": 1, "session_revision": 1,
            "session_ref": self.lease["session_ref"],
            "issuer": "https://issuer/", "client_id": "client", "audience": "audience",
            "tenant": "tenant", "workspace": "workspace", "project": "project",
            "principal": "principal", "enabled": True,
        }

    def admit(self, provider=None, expected=None):
        return admit_local_runtime_session(
            profile_path=self.profile, state_dir=self.state,
            expected_binding=expected or self.binding,
            read_provider_binding=provider or (lambda: self.binding),
            clock=lambda: self.now)

    def read(self, provider=None):
        return read_admitted_local_session(
            profile_path=self.profile, state_dir=self.state,
            read_provider_binding=provider or (lambda: self.binding),
            clock=lambda: self.now)

    def test_only_explicit_exact_transition_admits_without_restarting_or_sending(self):
        with self.assertRaisesRegex(LocalRuntimeSessionError, "ADMISSION_NOT_CURRENT"):
            self.read()
        receipt = self.admit()
        self.assertEqual(receipt["session_ref"], self.lease["session_ref"])
        self.assertEqual(receipt["pid"], self.lease["pid"])
        self.assertEqual(self.read()["session"]["state"], "PREPARED_UNBOUND")
        self.assertFalse(json.loads(self.profile.read_text())["enabled"])
        self.assertEqual(sorted(path.name for path in self.state.iterdir()),
                         ["admission.json", "session.json", "session.lock"])

    def test_wrong_or_stale_provider_fails_closed(self):
        for changed in ({"enabled": False}, {"session_ref": "local:wrong"},
                        {"project_revision": 2}, {"session_revision": 2},
                        {"binding_id": "CSB-other"}, {"binding_fingerprint": "0" * 64}):
            with self.subTest(changed=changed):
                provider = lambda: dict(self.binding, **changed)
                with self.assertRaises(LocalRuntimeSessionError):
                    self.admit(provider=provider)
        self.assertFalse((self.state / "admission.json").exists())
        self.admit()
        for changed in ({"enabled": False}, {"session_ref": "local:wrong"},
                        {"project_revision": 2}, {"session_revision": 2},
                        {"binding_id": "CSB-other"}, {"binding_fingerprint": "0" * 64}):
            with self.subTest(read_changed=changed):
                with self.assertRaises(LocalRuntimeSessionError):
                    self.read(provider=lambda: dict(self.binding, **changed))

    def test_wrong_expected_ref_or_revision_does_not_admit(self):
        for changed in ({"session_ref": "local:wrong"}, {"project_revision": 2}):
            with self.assertRaises(LocalRuntimeSessionError):
                self.admit(expected=dict(self.binding, **changed))
        self.assertFalse((self.state / "admission.json").exists())

    def test_currentness_rechecked_after_admission(self):
        self.admit()
        with patch("signalvev_client.local_runtime_session_host._process_start_epoch", return_value=None):
            with self.assertRaisesRegex(LocalRuntimeSessionError, "NOT_CURRENT"):
                self.read()
        self.now = 116.0
        with self.assertRaisesRegex(LocalRuntimeSessionError, "NOT_CURRENT"):
            self.read()

    def test_provider_read_is_mandatory_and_failure_is_closed(self):
        with self.assertRaisesRegex(LocalRuntimeSessionError, "PROVIDER_READ_REQUIRED"):
            admit_local_runtime_session(profile_path=self.profile, state_dir=self.state,
                                        expected_binding=self.binding, read_provider_binding=None,
                                        clock=lambda: self.now)
        with self.assertRaisesRegex(LocalRuntimeSessionError, "PROVIDER_READ_FAILED"):
            self.admit(provider=lambda: (_ for _ in ()).throw(OSError("provider unavailable")))
        self.assertFalse((self.state / "admission.json").exists())
        self.now = 100.0
        self.host.stop()
        with self.assertRaisesRegex(LocalRuntimeSessionError, "NOT_CURRENT"):
            self.read()

    def test_profile_true_alone_is_not_admission_and_stops_host(self):
        self.profile.write_text(json.dumps({"schema": "cerebro-bk07-normal-use-profile/v1",
                                            "enabled": True}), encoding="utf-8")
        with self.assertRaisesRegex(LocalRuntimeSessionError, "MUST_REMAIN_OFF"):
            self.admit()
        with self.assertRaisesRegex(LocalRuntimeSessionError, "MUST_REMAIN_OFF"):
            self.host.heartbeat()
        self.assertFalse((self.state / "admission.json").exists())


if __name__ == "__main__":
    unittest.main()
