"""Negative-first checks for the inert operator-bound ROM-A local ports."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import importlib.util
import json
import ntpath
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[2]
import sys
SPEC = importlib.util.spec_from_file_location("rom_a_local_ports", SOURCE / "mcp/rom_a_local_ports.py")
assert SPEC and SPEC.loader
ports = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ports
SPEC.loader.exec_module(ports)
SID = "S-1-5-21-100-200-300-1001"
OWNER = "S-1-5-32-544"
THREAD = "123e4567-e89b-12d3-a456-426614174000"
WRONG_THREAD = "123e4567-e89b-12d3-a456-426614174002"
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
        self.payload = "selected task bytes å".encode("utf-8")
        self.payload_hash = hashlib.sha256(self.payload).hexdigest()
        self.queue = ports.QueueBinding("actor:655", THREAD, "task:bk04", "r1",
                                        "FULL_ORIGINAL_TASK", self.payload_hash, self.payload)
        self.exe = self.root / "codex.exe"
        self.exe.write_bytes(b"pinned executable placeholder")
        self.exe_hash = hashlib.sha256(self.exe.read_bytes()).hexdigest()
        self.registry = self.root / "registry.json"
        self.write_registry()
        self.open_patch = patch.object(ports, "open_protected", side_effect=self.fake_open)
        self.open_patch.start()
        self.addCleanup(self.open_patch.stop)

    def registry_value(self):
        return {"schema": "cerebro-rom-a-selected-queue-registry/v1",
                "recipient_ref": self.queue.recipient_ref,
                "recipient_thread_id": self.queue.recipient_thread_id,
                "task_ref": self.queue.task_ref, "task_revision": self.queue.task_revision,
                "selection": self.queue.selection,
                "selected_sha256": self.queue.selected_sha256,
                "selected_bytes": len(self.queue.selected_bytes),
                "codex_exe_sha256": self.exe_hash}

    def write_registry(self, **overrides):
        value = self.registry_value()
        value.update(overrides)
        self.registry.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")

    @contextmanager
    def fake_open(self, path, *, root, owner_sid):
        self.assertEqual(owner_sid, OWNER)
        self.assertTrue(path.is_relative_to(root))
        if path.is_symlink():
            raise ValueError("ROMA_HANDLE_REPARSE_FORBIDDEN")
        with path.open("rb") as stream:
            yield stream, ntpath.normcase(ntpath.normpath(str(path)))

    def reader(self, entry=None, sid_reader=lambda: SID):
        return ports.FixedLocalEpisodeReader(root=self.root, expected_process_sid=SID,
                                             expected_custody_owner_sid=OWNER,
                                             bindings=(entry or self.entry,), sid_reader=sid_reader)

    def sender(self, runner, sid_reader=lambda: SID):
        return ports.FixedCodexQueueSender(
            codex_exe=self.exe, executable_root=self.root,
            expected_exe_sha256=self.exe_hash, registry_path=self.registry,
            registry_root=self.root, expected_custody_owner_sid=OWNER,
            expected_process_sid=SID, binding=self.queue,
            sid_reader=sid_reader, runner=runner)

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
        self.content.write_text("stale content", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "STALE_REVISION"):
            reader.read_current("episode:bk04", "SECTION", "selected")

    def test_wrong_principal_ref_selector_and_path_escape_rejected(self):
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

    def test_open_handle_final_path_swap_falsifier(self):
        reader = self.reader()
        outside = self.root.parent / "outside-file.txt"
        with patch.object(ports, "open_protected") as opener:
            @contextmanager
            def swapped(*_args, **_kwargs):
                with self.content.open("rb") as stream:
                    yield stream, ntpath.normcase(ntpath.normpath(str(outside)))
            opener.side_effect = swapped
            with self.assertRaisesRegex(ValueError, "FINAL_PATH_MISMATCH"):
                reader.read_current("episode:bk04", "SECTION", "selected")

    def test_registry_is_recipient_authority_not_uuid_shape(self):
        self.write_registry(recipient_thread_id=WRONG_THREAD)
        with self.assertRaisesRegex(ValueError, "PROTECTED_REGISTRY_BINDING_MISMATCH"):
            self.sender(lambda *a, **k: self.fail("queue called"))
        self.write_registry(selection="BK05_STRUCTURAL_CAPSULE_CANDIDATE")
        with self.assertRaisesRegex(ValueError, "PROTECTED_REGISTRY_BINDING_MISMATCH"):
            self.sender(lambda *a, **k: self.fail("queue called"))
        self.write_registry()
        sender = self.sender(lambda *a, **k: self.fail("queue called"))
        self.write_registry(recipient_ref="actor:1B")
        with self.assertRaisesRegex(ValueError, "PROTECTED_REGISTRY_BINDING_MISMATCH"):
            self.send(sender)

    def test_queue_exact_mapping_selection_and_utf8_suffix(self):
        calls = []
        def runner(args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(returncode=0,
                                   stdout=f"Queued message {MESSAGE} for thread {THREAD}\n")
        sender = self.sender(runner)
        for kwargs, reason in (({"recipient_ref": "actor:1B"}, "BINDING_MISMATCH"),
                               ({"selected_sha256": "0" * 64}, "BINDING_MISMATCH"),
                               ({"task_revision": "r2"}, "BINDING_MISMATCH"),
                               ({"selection": "BK05_STRUCTURAL_CAPSULE_CANDIDATE"},
                                "SELECTION_BINDING_MISMATCH")):
            with self.assertRaisesRegex(ValueError, reason):
                self.send(sender, **kwargs)
        self.assertEqual(calls, [])
        result = self.send(sender)
        self.assertEqual(result["delivery_ref"], MESSAGE)
        self.assertEqual(result["recipient_use"], "NOT_OBSERVED")
        self.assertEqual(calls[0][0][:4], [str(self.exe), "queue", "--thread", THREAD])
        self.assertFalse(calls[0][1]["shell"])
        self.assertTrue(calls[0][0][-1].encode("utf-8").endswith(self.payload))
        with self.assertRaisesRegex(ValueError, "UNKNOWN_NO_RETRY"):
            self.send(sender)
        self.assertEqual(len(calls), 1)

    def test_executable_bytes_change_with_identical_metadata_rejected(self):
        sender = self.sender(lambda *a, **k: self.fail("queue called"))
        before = self.exe.stat()
        changed = b"X" + self.exe.read_bytes()[1:]
        self.exe.write_bytes(changed)
        os.utime(self.exe, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(self.exe.stat().st_size, before.st_size)
        self.assertEqual(self.exe.stat().st_mtime_ns, before.st_mtime_ns)
        with self.assertRaisesRegex(ValueError, "EXE_HASH_MISMATCH"):
            self.send(sender)

    def test_executable_path_swap_after_construction_rejected(self):
        sender = self.sender(lambda *a, **k: self.fail("queue called"))
        original = self.fake_open
        @contextmanager
        def swapped(path, *, root, owner_sid):
            with original(path, root=root, owner_sid=owner_sid) as (stream, final):
                if path == self.exe:
                    final = ntpath.normcase(ntpath.normpath(str(self.root.parent / "other.exe")))
                yield stream, final
        with patch.object(ports, "open_protected", side_effect=swapped):
            with self.assertRaisesRegex(ValueError, "CODEX_FINAL_PATH_MISMATCH"):
                self.send(sender)

    def test_ambiguous_queue_result_never_becomes_accepted(self):
        sender = self.sender(lambda *a, **k: SimpleNamespace(returncode=0, stdout="queued\n"))
        with self.assertRaisesRegex(ValueError, "UNRECOGNIZED_NO_RETRY"):
            self.send(sender)
        with self.assertRaisesRegex(ValueError, "UNKNOWN_NO_RETRY"):
            self.send(sender)
        sender = self.sender(lambda *a, **k: SimpleNamespace(returncode=1, stdout=""))
        with self.assertRaisesRegex(ValueError, "UNKNOWN_NO_RETRY"):
            self.send(sender)

    def test_windows_custody_rejects_user_owned_unprotected_root(self):
        open_protected = ports._custody.open_protected
        with self.assertRaisesRegex(ValueError, "CUSTODY_OWNER_MISMATCH|UNTRUSTED_WRITE_ACE"):
            with open_protected(self.content, root=self.root, owner_sid=OWNER):
                pass


if __name__ == "__main__":
    unittest.main()
