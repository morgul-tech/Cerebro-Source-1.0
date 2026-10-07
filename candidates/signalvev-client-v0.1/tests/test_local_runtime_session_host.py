from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from signalvev_client.local_runtime_session_host import (
    LocalRuntimeSessionError, LocalRuntimeSessionHost, SCHEMA,
)


class LocalRuntimeSessionHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = self.root / "profile.json"
        self.profile.write_text(json.dumps({"schema": "cerebro-bk07-normal-use-profile/v1",
                                            "enabled": False}), encoding="utf-8")
        self.state_dir = self.root / "state"
        self.now = 100.0

    def host(self):
        return LocalRuntimeSessionHost(profile_path=self.profile,
                                       state_dir=self.state_dir, clock=lambda: self.now)

    def test_stable_within_process_and_new_reference_after_restart(self):
        first = self.host()
        receipt = first.start()
        self.assertEqual(receipt["schema"], SCHEMA)
        self.assertTrue(receipt["session_ref"].startswith("local:"))
        self.assertEqual(receipt["state"], "PREPARED_UNBOUND")
        self.now = 105.0
        self.assertEqual(first.heartbeat()["session_ref"], receipt["session_ref"])
        self.assertEqual(json.loads((self.state_dir / "session.json").read_text())["heartbeat_at_epoch"], 105.0)
        first.stop()
        self.assertEqual(json.loads((self.state_dir / "session.json").read_text())["state"], "STOPPED")
        second = self.host()
        try:
            self.assertNotEqual(second.start()["session_ref"], receipt["session_ref"])
        finally:
            second.stop()

    def test_single_owner_lock(self):
        first = self.host()
        first.start()
        try:
            with self.assertRaisesRegex(LocalRuntimeSessionError, "ALREADY_RUNNING"):
                self.host().start()
        finally:
            first.stop()

    def test_profile_enable_fails_closed_without_listening(self):
        host = self.host()
        host.start()
        try:
            self.profile.write_text(json.dumps({"schema": "cerebro-bk07-normal-use-profile/v1",
                                                "enabled": True}), encoding="utf-8")
            with self.assertRaisesRegex(LocalRuntimeSessionError, "MUST_REMAIN_OFF"):
                host.heartbeat()
        finally:
            host.stop()
        self.assertEqual(json.loads((self.state_dir / "session.json").read_text())["state"], "STOPPED")


if __name__ == "__main__":
    unittest.main()
