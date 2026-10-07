from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from signalvev_client.local_runtime_session_host import (
    LocalRuntimeSessionError, LocalRuntimeSessionHost, SCHEMA,
    read_current_local_session,
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
        self.assertEqual(read_current_local_session(profile_path=self.profile,
                         state_dir=self.state_dir, clock=lambda: self.now,
                         expected_session_ref=receipt["session_ref"])["session_ref"],
                         receipt["session_ref"])
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

    def test_dead_or_reused_process_receipt_fails_closed(self):
        host = self.host()
        receipt = host.start()
        try:
            state_path = self.state_dir / "session.json"
            dead = dict(receipt, pid=999999999)
            state_path.write_text(json.dumps(dead), encoding="utf-8")
            with self.assertRaisesRegex(LocalRuntimeSessionError, "NOT_CURRENT"):
                read_current_local_session(profile_path=self.profile,
                                           state_dir=self.state_dir, clock=lambda: self.now)
            reused = dict(receipt, process_start_epoch=0)
            state_path.write_text(json.dumps(reused), encoding="utf-8")
            with self.assertRaisesRegex(LocalRuntimeSessionError, "NOT_CURRENT"):
                read_current_local_session(profile_path=self.profile,
                                           state_dir=self.state_dir, clock=lambda: self.now)
        finally:
            host.stop()

    def test_stale_or_unlocked_lease_fails_closed(self):
        host = self.host()
        host.start()
        try:
            self.now = 116.0
            with self.assertRaisesRegex(LocalRuntimeSessionError, "NOT_CURRENT"):
                read_current_local_session(profile_path=self.profile,
                                           state_dir=self.state_dir, clock=lambda: self.now)
        finally:
            host.stop()
        state_path = self.state_dir / "session.json"
        stopped = json.loads(state_path.read_text(encoding="utf-8"))
        state_path.write_text(json.dumps(dict(stopped, state="PREPARED_UNBOUND")), encoding="utf-8")
        with self.assertRaisesRegex(LocalRuntimeSessionError, "NOT_CURRENT"):
            read_current_local_session(profile_path=self.profile,
                                       state_dir=self.state_dir, clock=lambda: self.now)

    def test_abrupt_process_death_leaves_no_current_session(self):
        process = subprocess.Popen([sys.executable, "-B", "-m",
                                    "signalvev_client.local_runtime_session_host",
                                    "--profile", str(self.profile),
                                    "--state-dir", str(self.state_dir)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            state_path = self.state_dir / "session.json"
            deadline = time.monotonic() + 5
            while not state_path.is_file() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(state_path.is_file(), "host did not start")
            self.assertEqual(read_current_local_session(profile_path=self.profile,
                             state_dir=self.state_dir)["state"], "PREPARED_UNBOUND")
            process.kill()
            process.wait(timeout=5)
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["state"],
                             "PREPARED_UNBOUND")
            with self.assertRaisesRegex(LocalRuntimeSessionError, "NOT_CURRENT"):
                read_current_local_session(profile_path=self.profile, state_dir=self.state_dir)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
