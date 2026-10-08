"""Negative-first checks for the inert operator-bound ROM-A local ports."""

from __future__ import annotations

from contextlib import contextmanager
import base64
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
from unittest.mock import Mock


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
        self.association = self.root / "association.json"
        self.association.write_text(json.dumps({"schema": "cerebro-rom-a-existing-thread-association/v1",
            "episode_ref": "episode:bk04", "recipient_ref": self.queue.recipient_ref,
            "recipient_thread_id": self.queue.recipient_thread_id,
            "evidence_ref": "existing-normal-episode:fixture"}), encoding="utf-8")
        self.write_registry()
        self.open_patch = patch.object(ports, "open_protected", side_effect=self.fake_open)
        self.open_patch.start()
        self.addCleanup(self.open_patch.stop)

    def registry_value(self):
        return {"schema": "cerebro-rom-a-selected-queue-registry/v2",
                "episode_ref": "episode:bk04",
                "recipient_ref": self.queue.recipient_ref,
                "recipient_thread_id": self.queue.recipient_thread_id,
                "task_ref": self.queue.task_ref, "task_revision": self.queue.task_revision,
                "selection": self.queue.selection,
                "selected_sha256": self.queue.selected_sha256,
                "selected_bytes_b64": base64.b64encode(self.queue.selected_bytes).decode("ascii"),
                "codex_exe": str(self.exe), "executable_root": str(self.root),
                "codex_exe_sha256": self.exe_hash,
                "association_receipt": self.association.name,
                "association_sha256": hashlib.sha256(self.association.read_bytes()).hexdigest()}

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
            registry_path=self.registry, episode_ref="episode:bk04",
            registry_root=self.root, expected_custody_owner_sid=OWNER,
            expected_process_sid=SID,
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
        for bad in (r"..\escape", r"child\..\current.txt", r"C:\other.txt",
                    r"\\server\share\file", "current.txt:stream", r"\\?\C:\other"):
            with self.assertRaisesRegex(ValueError, "RELATIVE_PATH"):
                self.reader(ports.LocalContentBinding("episode:bk04", bad, self.digest))

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
        with self.assertRaisesRegex(ValueError, "ASSOCIATION_RECEIPT_MISMATCH"):
            self.sender(lambda *a, **k: self.fail("queue called"))
        self.write_registry(selection="BK05_STRUCTURAL_CAPSULE_CANDIDATE")
        self.sender(lambda *a, **k: self.fail("queue called"))
        self.write_registry()
        sender = self.sender(lambda *a, **k: self.fail("queue called"))
        self.write_registry(recipient_ref="actor:1B")
        with self.assertRaisesRegex(ValueError, "ASSOCIATION_RECEIPT_MISMATCH"):
            self.send(sender)
        self.write_registry()
        with self.assertRaises(TypeError):
            ports.FixedCodexQueueSender(registry_path=self.registry,
                registry_root=self.root, expected_custody_owner_sid=OWNER,
                expected_process_sid=SID, episode_ref="episode:bk04", binding=self.queue)

    def test_protected_registry_and_association_drift_rejected(self):
        sender = self.sender(lambda *a, **k: self.fail("queue called"))
        self.association.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "ASSOCIATION_RECEIPT_DRIFT"):
            self.send(sender)
        self.write_registry()
        with self.assertRaisesRegex(ValueError, "ASSOCIATION_RECEIPT_MISMATCH"):
            self.sender(lambda *a, **k: self.fail("queue called"))

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

    def test_windows_custody_lexical_and_intermediate_ancestor_falsifiers(self):
        custody = ports._custody
        self.assertFalse(custody._can_mutate_protected_path(0x00000004, ancestor=True))
        self.assertTrue(custody._can_mutate_protected_path(0x00000004, ancestor=False))
        for right in (0x00000040, 0x00010000, 0x00040000, 0x00080000):
            self.assertTrue(custody._can_mutate_protected_path(right, ancestor=True))
        for bad in (r"C:\Program Files\safe\..\escape.txt", r"C:\safe\file.txt:ads",
                    r"\\server\share\file.txt", r"\\?\C:\safe\file.txt",
                    r"C:relative\file.txt"):
            with self.assertRaisesRegex(ValueError, "LOCAL_PATH_FORM_INVALID"):
                custody._safe_local_path(Path(bad))
        root = Path(r"C:\Program Files\Cerebro\episode")
        path = root / "current.txt"
        kernel = Mock()
        kernel.CreateFileW.return_value = 123
        inspected = []
        def inspect(_handle, expected, _owner, _kernel, _advapi, *, ancestor=False):
            inspected.append((str(expected), ancestor))
            if str(expected).lower() == r"c:\program files\cerebro":
                raise ValueError("ROMA_FINAL_PATH_MISMATCH")
        with patch.object(custody, "_apis", return_value=(kernel, Mock())), \
             patch.object(custody, "_inspect", side_effect=inspect):
            with self.assertRaisesRegex(ValueError, "FINAL_PATH_MISMATCH"):
                with custody.open_protected(path, root=root, owner_sid=OWNER):
                    pass
        self.assertTrue(any(value.lower() == r"c:\program files\cerebro"
                            for value, _ in inspected))
        self.assertFalse(any(value.lower() == str(path).lower() for value, _ in inspected))


if __name__ == "__main__":
    unittest.main()
