"""GUARDS for the client package. The offline core keeps its own (unchanged, unweakened) guards; these confine the
NEW network/execution surface to one narrow file and keep the out-of-scope list out of the code."""
import ast
import socket
import unittest
from pathlib import Path

from _support import TARGET, FakeBroker, TmpCase, config_doc, fixture_entry, raw_event, synthetic_fixture_doc
import signalvev_client
from signalvev_client import ListenClient, SendClient, SyntheticOwnerResolver, parse_config

PKG = Path(signalvev_client.__file__).resolve().parent
FILES = sorted(p for p in PKG.glob("*.py"))


def imports_of(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module


def only_in(names, allowed_file):
    hits = {}
    for f in FILES:
        found = {i for i in imports_of(ast.parse(f.read_text(encoding="utf-8"))) if i.split(".")[0] in names}
        if found:
            hits[f.name] = found
    return hits, {allowed_file}


class StaticGuards(unittest.TestCase):
    def test_network_event_loop_and_tls_only_in_the_binding_file(self):
        for names in ({"nats"}, {"asyncio"}, {"ssl"}, {"concurrent"}):
            hits, allowed = only_in(names, "nats_binding.py")
            self.assertLessEqual(set(hits), allowed, f"{names} escaped the binding boundary: {hits}")
        hits, _ = only_in({"nats", "asyncio"}, "")
        self.assertEqual(set(hits), {"nats_binding.py"})        # ... and it really is there

    def test_forbidden_imports_everywhere(self):
        forbidden = {"socket", "subprocess", "http", "requests", "httpx", "aiohttp", "websockets", "urllib3", "pickle", "marshal",
                     "multiprocessing", "sched", "smtplib", "ftplib", "sqlite3", "ctypes", "openai", "anthropic", "paramiko", "asyncssh"}
        for f in FILES:
            bad = {i for i in imports_of(ast.parse(f.read_text(encoding="utf-8"))) if i.split(".")[0] in forbidden}
            self.assertFalse(bad, f"{f.name}: {bad}")
            self.assertFalse([i for i in imports_of(ast.parse(f.read_text(encoding="utf-8"))) if i.startswith("urllib.request")])

    def test_environment_signals_and_file_locking_are_confined(self):
        for f in FILES:
            src = f.read_text(encoding="utf-8")
            if f.name != "_bootstrap.py":
                for w in ("os.environ", "getenv", "putenv"):
                    self.assertNotIn(w, src, f"{f.name} touches the environment")
            if f.name != "cli.py":
                self.assertNotIn("import signal", src, f"{f.name}")
            if f.name != "lock.py":
                self.assertNotIn("fcntl", src)
                self.assertNotIn("msvcrt", src)

    def test_no_dynamic_exec_sleep_process_or_daemon_constructs(self):
        for f in FILES:
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    fn = node.func
                    name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                    self.assertNotIn(name, {"eval", "exec", "compile", "__import__", "system", "popen", "Popen", "fork", "spawn",
                                            "sleep", "write_text", "write_bytes"}, f"{f.name}: {name}()")
                    for kw in node.keywords:
                        if kw.arg == "daemon":
                            self.assertFalse(isinstance(kw.value, ast.Constant) and kw.value.value is True,
                                             f"{f.name}: daemon thread")
                if isinstance(node, ast.While):
                    test = node.test
                    self.assertFalse(isinstance(test, ast.Constant) and test.value is True and f.name not in ("dispatch.py",),
                                     f"{f.name}: unbounded 'while True' loop")

    def test_importlib_only_for_the_operator_named_resolver_factory(self):
        for f in FILES:
            if "import_module" in f.read_text(encoding="utf-8"):
                # session.py: operator-named resolver factory; pm_x9.py: operator-named ports_factory (same pattern)
                self.assertIn(f.name, {"session.py", "pm_x9.py"})

    def test_out_of_scope_capabilities_are_absent_from_executable_code(self):
        banned = ("jetstream", "js.", "request_many", "wildcard", "daemon", "systemd", "scheduler", "autowake", "bind_worker",
                  "start_worker", "admit_claim", "release_claim", "chatgpt", "openai", "anthropic", "drive")
        for f in FILES:
            tree = ast.parse(f.read_text(encoding="utf-8"))
            docs = {id(n.body[0].value) for n in ast.walk(tree)
                    if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.body
                    and isinstance(n.body[0], ast.Expr) and isinstance(getattr(n.body[0], "value", None), ast.Constant)}
            atoms = []
            for n in ast.walk(tree):
                if isinstance(n, ast.Name):
                    atoms.append(n.id)
                elif isinstance(n, ast.Attribute):
                    atoms.append(n.attr)
                elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    atoms.append(n.name)
                elif isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
                    atoms.append(n.value)
            for atom in atoms:
                for w in banned:
                    self.assertNotIn(w, atom.lower(), f"{f.name}: {w!r} in {atom!r}")
            for n in ast.walk(tree):                                  # no wildcard / request-reply API usage
                if isinstance(n, ast.Attribute):
                    self.assertNotIn(n.attr, {"request", "jetstream", "jsm", "new_inbox"}, f"{f.name}.{n.attr}")

    def test_subjects_are_only_the_two_registered_literals(self):
        from signalvev_client.nats_binding import ALLOWED_SUBJECTS
        self.assertEqual(ALLOWED_SUBJECTS, {"cerebro.v1.state.delta", "cerebro.v1.artifact.pointer"})

    def test_reference_logic_is_not_reimplemented_in_the_client(self):
        allsrc = "\n".join(f.read_text(encoding="utf-8") for f in FILES)
        for needle in ("class ReceiptTrail", "def validate_envelope(", "class SensingReceiver", "class SensingSender",
                       "class DedupeCursor", "class SendLedger", "def build_frame(", "def decode_frame(", "def resolve_state_delta_truth("):
            self.assertNotIn(needle, allsrc)

    def test_the_client_never_imports_core_internals_it_would_have_to_copy(self):
        for f in FILES:
            for mod in imports_of(ast.parse(f.read_text(encoding="utf-8"))):
                self.assertFalse(mod.startswith("signalvev_reference"), f"{f.name}: reference modules come only via the core bridge")

    def test_core_package_has_no_network_or_event_loop_import(self):
        import signalvev_sensing
        core = Path(signalvev_sensing.__file__).resolve().parent
        for f in core.glob("*.py"):
            bad = {i.split(".")[0] for i in imports_of(ast.parse(f.read_text(encoding="utf-8")))} & {"nats", "asyncio", "socket", "ssl"}
            self.assertFalse(bad, f"core {f.name} imports {bad}")


class DynamicGuard(TmpCase):
    def test_fake_broker_roundtrip_runs_with_dns_and_outbound_connect_blocked(self):
        def blocked(*a, **k):
            raise AssertionError("real network use in a fake-broker test")
        saved = (socket.create_connection, socket.getaddrinfo)
        socket.create_connection = socket.getaddrinfo = blocked
        try:
            broker, results = FakeBroker(), []
            cfg = parse_config(config_doc(self.tmp), base_dir=self.tmp)
            resolver = SyntheticOwnerResolver.from_dict(synthetic_fixture_doc(fixture_entry()))
            with ListenClient(cfg, resolver=resolver, connect_fn=broker.connect, clock=lambda: 1_790_000_000.0,
                              on_result=results.append) as lc:
                cfg2 = parse_config(config_doc(self.tmp, evidence={"dir": str(self.tmp / "ev2")}), base_dir=self.tmp)
                with SendClient(cfg2, connect_fn=broker.connect, clock=lambda: 1_790_000_000.0) as sc:
                    self.assertEqual(sc.send(raw_event()).state, "ACCEPTED")
                self.assertEqual(lc.wait(duration=5, max_frames=1), "MAX_FRAMES_REACHED")
            self.assertEqual(results[0].disposition, "ACK_READ")
        finally:
            socket.create_connection, socket.getaddrinfo = saved

    def test_target_is_reported(self):
        self.assertIn(TARGET, ("source", "installed"))
        if TARGET == "installed":                                  # the suite must be exercising the INSTALLED bytes
            self.assertIn("site-packages", Path(signalvev_client.__file__).as_posix())


if __name__ == "__main__":
    unittest.main()
