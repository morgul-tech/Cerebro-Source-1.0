"""Negative-first checks for the inert, operator-bound ROM-A local ports."""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("rom_a_local_ports", SOURCE / "mcp/rom_a_local_ports.py")
assert SPEC and SPEC.loader
import sys
ports = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ports
SPEC.loader.exec_module(ports)
SID = "S-1-5-21-100-200-300-1001"
THREAD = "123e4567-e89b-12d3-a456-426614174000"
MESSAGE = "123e4567-e89b-12d3-a456-426614174001"


class FixedPortsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.content = self.root / "current.txt"
        self.content.write_text("real bounded text", encoding="utf-8")
        self.digest = hashlib.sha256(self.content.read_bytes()).hexdigest()
        self.entry = ports.LocalContentBinding("episode:bk04", "current.txt", self.digest)
        self.payload = b"selected task bytes"
        self.payload_hash = hashlib.sha256(self.payload).hexdigest()
        self.queue = ports.QueueBinding("actor:655", THREAD, "task:bk04", "r1",
                                        self.payload_hash, self.payload)
        self.exe = self.root / "codex.exe"
        self.exe.write_bytes(b"pinned executable placeholder")

    def reader(self, entry=None, sid_reader=lambda: SID):
        return ports.FixedLocalEpisodeReader(root=self.root, expected_process_sid=SID,
                                             bindings=(entry or self.entry,), sid_reader=sid_reader,
                                             owner_reader=lambda _: SID)

    def sender(self, runner, sid_reader=lambda: SID):
        return ports.FixedCodexQueueSender(codex_exe=self.exe, expected_process_sid=SID,
                                           binding=self.queue, sid_reader=sid_reader, runner=runner)

    def send(self, sender, **overrides):
        kwargs = dict(recipient_ref="actor:655", selected_bytes=self.payload,
                      selected_sha256=self.payload_hash, selection="FULL_ORIGINAL_TASK",
                      task_ref="task:bk04", task_revision="r1")
        kwargs.update(overrides)
        return sender.send_selected(**kwargs)

    def test_current_exact_one_part_and_stale_rejected(self):
        reader = self.reader()
        value = reader.read_current("episode:bk04", "SECTION", "selected")
        self.assertEqual(value["parts"], {"selected": "real bounded text"})
        self.assertEqual(value["revision"], "sha256:" + self.digest)
        self.assertEqual(value["currentness"], "CURRENT")
        self.content.write_text("stale content", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "STALE_REVISION"):
            reader.read_current("episode:bk04", "SECTION", "selected")

    def test_wrong_principal_ref_selector_and_escape_rejected(self):
        with self.assertRaisesRegex(ValueError, "WRONG_PRINCIPAL"):
            self.reader(sid_reader=lambda: "S-1-5-18")
        reader = self.reader()
        for ref, kind, selector in (("other", "SECTION", "selected"),
                                    ("episode:bk04", "TAB", "selected"),
                                    ("episode:bk04", "SECTION", "other")):
            with self.assertRaisesRegex(ValueError, "UNREGISTERED"):
                reader.read_current(ref, kind, selector)
        with self.assertRaisesRegex(ValueError, "RELATIVE_PATH"):
            self.reader(ports.LocalContentBinding("episode:bk04", "../escape", self.digest))
        with self.assertRaisesRegex(ValueError, "ROOT_CUSTODY_MISMATCH"):
            ports.FixedLocalEpisodeReader(root=self.root, expected_process_sid=SID,
                                          bindings=(self.entry,), sid_reader=lambda: SID,
                                          owner_reader=lambda _: "S-1-5-18")
        owned = self.reader()
        with patch.object(owned, "_owner_reader",
                          side_effect=lambda path: SID if path == self.root else "S-1-5-18"):
            with self.assertRaisesRegex(ValueError, "CONTENT_CUSTODY_MISMATCH"):
                owned.read_current("episode:bk04", "SECTION", "selected")
        link = self.root / "link.txt"
        try:
            link.symlink_to(self.content)
        except (OSError, NotImplementedError):
            self.skipTest("symlink privilege unavailable")
        with self.assertRaisesRegex(ValueError, "REPARSE"):
            self.reader(ports.LocalContentBinding("episode:bk04", "link.txt", self.digest)).read_current(
                "episode:bk04", "SECTION", "selected")

    def test_concurrent_file_change_rejected(self):
        reader = self.reader()
        actual = ports.os.fstat
        def changed(fd):
            stat = actual(fd)
            return SimpleNamespace(st_dev=stat.st_dev, st_ino=stat.st_ino,
                                   st_size=stat.st_size, st_mtime_ns=stat.st_mtime_ns + 1)
        with patch.object(ports.os, "fstat", side_effect=changed):
            with self.assertRaisesRegex(ValueError, "CONCURRENT_CONTENT_CHANGE"):
                reader.read_current("episode:bk04", "SECTION", "selected")

    def test_queue_exact_binding_safe_args_and_no_second_attempt(self):
        calls = []
        def runner(args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(returncode=0,
                                   stdout=f"Queued message {MESSAGE} for thread {THREAD}\n")
        sender = self.sender(runner)
        with self.assertRaisesRegex(ValueError, "BINDING_MISMATCH"):
            self.send(sender, recipient_ref="actor:1B")
        with self.assertRaisesRegex(ValueError, "BINDING_MISMATCH"):
            self.send(sender, selected_sha256="0" * 64)
        with self.assertRaisesRegex(ValueError, "BINDING_MISMATCH"):
            self.send(sender, task_revision="r2")
        self.assertEqual(calls, [])
        result = self.send(sender)
        self.assertEqual(result["delivery_ref"], MESSAGE)
        self.assertEqual(result["recipient_use"], "NOT_OBSERVED")
        self.assertEqual(calls[0][0][:4], [str(self.exe.resolve()), "queue", "--thread", THREAD])
        self.assertFalse(calls[0][1]["shell"])
        self.assertIn("selected task bytes", calls[0][0][-1])
        with self.assertRaisesRegex(ValueError, "UNKNOWN_NO_RETRY"):
            self.send(sender)
        self.assertEqual(len(calls), 1)

    def test_ambiguous_queue_result_never_becomes_accepted(self):
        sender = self.sender(lambda *a, **k: SimpleNamespace(returncode=0, stdout="queued\n"))
        with self.assertRaisesRegex(ValueError, "UNRECOGNIZED_NO_RETRY"):
            self.send(sender)
        with self.assertRaisesRegex(ValueError, "UNKNOWN_NO_RETRY"):
            self.send(sender)
        sender = self.sender(lambda *a, **k: SimpleNamespace(returncode=1, stdout=""))
        with self.assertRaisesRegex(ValueError, "UNKNOWN_NO_RETRY"):
            self.send(sender)

    def test_executable_change_and_runtime_principal_change_rejected(self):
        sender = self.sender(lambda *a, **k: self.fail("queue called"))
        self.exe.write_bytes(b"changed executable")
        with self.assertRaisesRegex(ValueError, "EXE_CHANGED"):
            self.send(sender)
        with patch.object(sender, "_sid_reader", return_value="S-1-5-18"):
            with self.assertRaisesRegex(ValueError, "WRONG_PRINCIPAL"):
                self.send(sender)


if __name__ == "__main__":
    unittest.main()
