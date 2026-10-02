"""GUARD tests: the order's NO-lists, checked statically (AST over src) and dynamically (network/process blocked)."""
from __future__ import annotations

import ast
import socket
import subprocess
import unittest
from pathlib import Path

from fixtures import SRC, FakeResolver, NOW, Rig, RigTestCase, pointer_event, raw_event

PKG = SRC / "signalvev_sensing"
FILES = sorted(PKG.glob("*.py"))
FORBIDDEN_IMPORTS = {"socket", "ssl", "subprocess", "http", "urllib", "urllib3", "requests", "httpx", "aiohttp", "asyncio",
                     "nats", "openai", "anthropic", "ctypes", "multiprocessing", "sched", "smtplib", "ftplib", "sqlite3",
                     "shelve", "dbm", "pickle", "marshal", "tempfile", "shutil"}
FORBIDDEN_BARE_CALLS = {"eval", "exec", "compile", "__import__"}
FORBIDDEN_ATTR_CALLS = {"system", "popen", "sleep", "Popen", "run", "fork", "spawn"}


def imports_of(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module.split(".")[0]


class StaticGuards(unittest.TestCase):
    def test_sources_exist(self):
        self.assertGreaterEqual(len(FILES), 14)

    def test_no_network_process_model_or_datastore_imports(self):
        for f in FILES:
            bad = set(imports_of(ast.parse(f.read_text()))) & FORBIDDEN_IMPORTS
            self.assertFalse(bad, f"{f.name} imports {bad}")

    def test_no_dynamic_exec_sleep_or_process_calls(self):
        for f in FILES:
            for node in ast.walk(ast.parse(f.read_text())):
                if isinstance(node, ast.Call):
                    fn = node.func
                    if isinstance(fn, ast.Attribute):
                        self.assertNotIn(fn.attr, FORBIDDEN_ATTR_CALLS, f"{f.name}: forbidden call .{fn.attr}()")
                    else:
                        self.assertNotIn(getattr(fn, "id", ""), FORBIDDEN_BARE_CALLS, f"{f.name}: forbidden call")

    def test_no_loops_that_could_be_a_daemon_or_scheduler(self):
        for f in FILES:
            for node in ast.walk(ast.parse(f.read_text())):
                if isinstance(node, ast.While):
                    self.fail(f"{f.name}: while-loop (no daemon/scheduler/poller allowed in v0.1)")

    def test_only_the_evidence_store_opens_files_and_only_reference_bridge_reads_env(self):
        for f in FILES:
            src = f.read_text()
            opens = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "open"]
            self.assertTrue(not opens or f.name == "store.py", f"{f.name} opens files")
            for w in ("write_text", "write_bytes", "os.environ", "getenv"):
                self.assertTrue(w not in src or f.name == "_reference.py" and w in ("os.environ",), f"{f.name} uses {w}")

    def test_no_credentials_no_model_identifiers_no_jetstream_in_code(self):
        """Executable code only (identifiers + non-docstring string literals); prose may NAME what is excluded."""
        words = ("password", "passwd", "api_key", "apikey", "bearer", "token", "chatgpt", "openai", "anthropic", "jetstream",
                 "nats://", "tls://", "ws://", "credential")
        for f in FILES:
            tree = ast.parse(f.read_text())
            docs = {id(n.body[0].value) for n in ast.walk(tree)
                    if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef)) and n.body
                    and isinstance(n.body[0], ast.Expr) and isinstance(getattr(n.body[0], "value", None), ast.Constant)}
            atoms = []
            for n in ast.walk(tree):
                if isinstance(n, ast.Name):
                    atoms.append(n.id)
                elif isinstance(n, ast.Attribute):
                    atoms.append(n.attr)
                elif isinstance(n, (ast.FunctionDef, ast.ClassDef)):
                    atoms.append(n.name)
                elif isinstance(n, ast.arg):
                    atoms.append(n.arg)
                elif isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
                    atoms.append(n.value)
            for atom in atoms:
                for w in words:
                    self.assertNotIn(w, atom.lower(), f"{f.name}: {w!r} in {atom!r}")

    def test_receipt_and_truth_logic_is_imported_not_reimplemented(self):
        allsrc = "\n".join(f.read_text() for f in FILES)
        for needle in ("class ReceiptTrail", "STAGE_PREDECESSORS =", "def validate_envelope(", "def evaluate_schema_compatibility(",
                       "def resolve_state_delta_truth(", "class FirstBrokenEdgeRecorder", "RECEIPT_LADDER ="):
            self.assertNotIn(needle, allsrc)
        ref = (PKG / "_reference.py").read_text()
        for needle in ("ReceiptTrail", "validate_envelope", "evaluate_schema_compatibility", "resolve_state_delta_truth",
                       "FirstBrokenEdgeRecorder", "load_registry"):
            self.assertIn(needle, ref)

    def test_activation_module_is_pure(self):
        self.assertTrue(set(imports_of(ast.parse((PKG / "activation.py").read_text()))) <= {"__future__", "dataclasses", "typing"})

    def test_package_only_touches_the_validator_dir_via_the_bridge(self):
        for f in FILES:
            if f.name != "_reference.py":
                self.assertNotIn("sys.path", f.read_text())


class DynamicGuards(RigTestCase):
    def test_full_scenario_runs_with_network_and_processes_blocked(self):
        def blocked(*a, **k):
            raise AssertionError("network/process use is forbidden in this package")
        saved = (socket.socket, socket.create_connection, subprocess.Popen)
        socket.socket = socket.create_connection = subprocess.Popen = blocked
        try:
            rig = Rig()
            self.assertEqual(rig.sender.send(raw_event()).state, "ACCEPTED")
            self.assertEqual(rig.receiver.on_frame(rig.frame_for(pointer_event())).disposition, "ACK_READ")
            self.assertEqual(rig.receiver.on_frame(rig.frame_for(raw_event(change_class="SEMANTIC", event_id="e9", owner_seq=9,
                                                                           revision_basis={"before": "a", "after": "b"}))).activation.decision,
                             "JUDGMENT_REQUIRED")
        finally:
            socket.socket, socket.create_connection, subprocess.Popen = saved

    def test_judgment_required_is_only_a_label_no_hook_exists_to_wake_anything(self):
        rig = Rig()
        res = rig.receiver.on_frame(rig.frame_for(raw_event(change_class="SEMANTIC")))
        self.assertEqual(res.activation.decision, "JUDGMENT_REQUIRED")
        self.assertFalse([a for a in dir(rig.receiver) if "wake" in a.lower() or "model" in a.lower() or "llm" in a.lower()])
        self.assertFalse(res.activation.wake_bound)

    def test_resolver_is_never_called_for_noise(self):
        rig = Rig()
        frame = rig.frame_for(raw_event())
        for _ in range(5):
            rig.receiver.on_frame(frame)
        self.assertEqual(len(rig.resolver.calls), 1)


if __name__ == "__main__":
    unittest.main()
