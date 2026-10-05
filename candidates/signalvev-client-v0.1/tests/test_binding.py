"""Real-client adapter mapping (nats-py facade) against a fake async client of the same shape."""
import threading
import time
import unittest

from _support import FakeBroker, TmpCase, config_doc
from signalvev_client import BindingError, ConnectError, parse_config
from signalvev_client.dispatch import Dispatcher
from signalvev_client.nats_binding import LISTEN, PROBE, SEND, NatsPyConnection, build_connect_kwargs
from signalvev_sensing import ACCEPTED, NOT_SENT, UNKNOWN_SEND, CoreNatsAdapter
from signalvev_sensing.transport import NotSentError

FRAME = b'{"frame":"x"}'
SUBJECT = "cerebro.v1.state.delta"


class BindingTests(TmpCase):
    def setUp(self):
        super().setUp()
        self.cfg = parse_config(config_doc(self.tmp, nats={"flush_timeout_seconds": 0.2}), base_dir=self.tmp)
        self.broker = FakeBroker()
        self.conns = []

    def tearDown(self):
        for c in self.conns:
            c.close()
        super().tearDown()

    def conn(self, role=SEND, cfg=None, dispatcher=None):
        c = NatsPyConnection(cfg or self.cfg, role=role, dispatcher=dispatcher, connect_fn=self.broker.connect)
        self.conns.append(c)
        c.connect()
        return c

    # ------------------------------------------------------------ connect options
    def test_send_role_never_buffers_or_reconnects(self):
        kw = build_connect_kwargs(self.cfg, SEND)
        self.assertIs(kw["allow_reconnect"], False)
        self.assertEqual((kw["max_reconnect_attempts"], kw["pending_size"]), (0, 0))
        self.assertEqual(kw["servers"], ["nats://127.0.0.1:4222"])
        self.assertEqual(kw["name"], "signalvev-client:node-test:send")
        self.assertIs(build_connect_kwargs(self.cfg, PROBE)["allow_reconnect"], False)

    def test_listen_role_may_reconnect_but_still_never_buffers_publishes(self):
        kw = build_connect_kwargs(self.cfg, LISTEN)
        self.assertIs(kw["allow_reconnect"], True)
        self.assertEqual((kw["max_reconnect_attempts"], kw["pending_size"]), (2, 0))

    def test_credentials_are_passed_as_a_path_only_and_tls_context_is_built(self):
        creds = self.tmp / "n.creds"
        creds.write_text("SECRET-CONTENT")
        cfg = parse_config(config_doc(self.tmp, nats={"credentials_file": str(creds), "server": "tls://127.0.0.1:4222"}),
                           base_dir=self.tmp)
        kw = build_connect_kwargs(cfg, SEND)
        self.assertEqual(kw["user_credentials"], str(creds))
        self.assertNotIn("SECRET-CONTENT", repr({k: v for k, v in kw.items() if k != "tls"}))
        self.assertEqual(type(kw["tls"]).__name__, "SSLContext")
        self.assertNotIn("user_credentials", build_connect_kwargs(self.cfg, SEND))
        self.assertNotIn("tls", build_connect_kwargs(self.cfg, SEND))

    # ------------------------------------------------------------ publish / flush outcome mapping
    def test_publish_and_flush_ok_is_transport_accepted_only(self):
        c = self.conn()
        res = CoreNatsAdapter(c, flush_timeout=0.2).publish(SUBJECT, FRAME)
        self.assertEqual((res.state, res.reason), (ACCEPTED, "SERVER_FLUSH_OK"))
        self.assertEqual(self.broker.publish_calls, [(SUBJECT, FRAME)])

    def test_library_refusals_before_write_are_proven_not_sent(self):
        for refusal in ("refuse_closed", "refuse_buffer"):
            c = self.conn()
            self.broker.script = [refusal]
            self.assertEqual(CoreNatsAdapter(c, flush_timeout=0.2).publish(SUBJECT, FRAME).state, NOT_SENT, refusal)

    def test_disconnected_connection_publishes_nothing_and_is_not_sent(self):
        c = self.conn()
        self.broker.make_nc_drop()
        before = len(self.broker.publish_calls)
        self.assertEqual(CoreNatsAdapter(c, flush_timeout=0.2).publish(SUBJECT, FRAME).state, NOT_SENT)
        self.assertEqual(len(self.broker.publish_calls), before)      # not even handed to the library
        self.assertEqual(c.status()["state"], "DISCONNECTED")

    def test_write_errors_flush_timeouts_and_hangs_are_unknown_not_not_sent(self):
        c = self.conn()
        ad = CoreNatsAdapter(c, flush_timeout=0.2)
        self.broker.script = ["oserror"]
        self.assertEqual(ad.publish(SUBJECT, FRAME).state, UNKNOWN_SEND)
        self.broker.script = ["write_then_drop"]                      # delivered AND errored: still ambiguous
        self.assertEqual(ad.publish(SUBJECT, FRAME).state, UNKNOWN_SEND)
        self.broker.flush_error = True
        res = ad.publish(SUBJECT, FRAME)                              # published, server round-trip failed
        self.assertEqual((res.state, res.reason), (UNKNOWN_SEND, "FLUSH_FAILED_SERVER_RECEIPT_UNPROVEN"))
        self.broker.flush_error = False
        self.broker.script = ["hang"]
        self.assertEqual(ad.publish(SUBJECT, FRAME).state, UNKNOWN_SEND)
        self.assertGreaterEqual(c.status()["counters"]["publish_errors"], 2)

    def test_role_and_subject_enforcement(self):
        s = self.conn(SEND)
        with self.assertRaises(NotSentError):
            s.publish("cerebro.v1.other", FRAME)
        with self.assertRaises(NotSentError):
            s.publish(SUBJECT, bytearray(FRAME))
        with self.assertRaises(BindingError):
            s.subscribe(SUBJECT, lambda d: None)
        d = Dispatcher(max_queue=2)
        listener = self.conn(LISTEN, dispatcher=d)
        with self.assertRaises(NotSentError):
            listener.publish(SUBJECT, FRAME)
        for bad in ("cerebro.v1.>", "cerebro.v1.*", "cerebro.v1.state.delta.extra", ">"):
            with self.assertRaises(BindingError, msg=bad):
                listener.subscribe(bad, lambda data: None)
        self.assertEqual(self.broker.publish_calls, [])

    # ------------------------------------------------------------ lifecycle
    def test_connect_failure_is_typed_redacted_and_leaves_no_thread(self):
        self.broker.refuse_connect = True
        c = NatsPyConnection(self.cfg, role=SEND, connect_fn=self.broker.connect)
        with self.assertRaises(ConnectError) as cm:
            c.connect()
        self.assertEqual(cm.exception.code, "CONNECT_FAILED")
        self.assertEqual(c.state(), "CLOSED")
        self.assertFalse([t for t in threading.enumerate() if t.name == "signalvev-nats-loop"])
        with self.assertRaises(BindingError):
            c.connect()                                               # single-use

    def test_close_is_idempotent_stops_the_loop_thread_and_blocks_publish(self):
        c = self.conn()
        c.close()
        c.close()
        self.assertEqual(c.state(), "CLOSED")
        self.assertFalse([t for t in threading.enumerate() if t.name == "signalvev-nats-loop"])
        with self.assertRaises(NotSentError):
            c.publish(SUBJECT, FRAME)

    def test_status_is_honest_and_secret_free(self):
        c = self.conn()
        st = c.status()
        self.assertEqual((st["state"], st["role"], st["server"]), ("CONNECTED", "SEND", "127.0.0.1:4222"))
        self.assertNotIn("password", repr(st).lower())

    def test_missing_nats_py_is_a_typed_binding_error(self):
        import sys
        real = sys.modules.get("nats")
        sys.modules["nats"] = None                                    # makes `import nats` raise ImportError
        try:
            c = NatsPyConnection(self.cfg, role=SEND)
            with self.assertRaises(BindingError) as cm:
                c.connect()
            self.assertEqual(cm.exception.code, "NATS_PY_NOT_INSTALLED")
        finally:
            if real is None:
                sys.modules.pop("nats", None)
            else:
                sys.modules["nats"] = real


    def test_credentials_file_without_nkeys_fails_early_and_typed(self):
        import sys
        import types
        creds = self.tmp / "n.creds"
        creds.write_text("x")
        cfg = parse_config(config_doc(self.tmp, nats={"credentials_file": str(creds)}), base_dir=self.tmp)
        saved = {k: sys.modules.get(k, "ABSENT") for k in ("nats", "nkeys")}
        sys.modules["nats"] = types.SimpleNamespace(connect=lambda **kw: None, __spec__=None)
        sys.modules["nkeys"] = None
        try:
            with self.assertRaises(BindingError) as cm:
                NatsPyConnection(cfg, role=SEND).connect()
            self.assertEqual(cm.exception.code, "NKEYS_NOT_INSTALLED")
        finally:
            for k, v in saved.items():
                if v == "ABSENT":
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v

    # ------------------------------------------------------------ receive path execution model
    def test_frames_are_handled_off_the_event_loop_in_arrival_order(self):
        seen, done = [], threading.Event()

        def handler(data):
            seen.append((threading.get_ident(), data))
            if len(seen) == 3:
                done.set()
            return None
        d = Dispatcher(max_queue=8)
        d.start()
        listener = self.conn(LISTEN, dispatcher=d)
        listener.subscribe(SUBJECT, handler)
        for i in range(3):
            self.broker.deliver(SUBJECT, f"m{i}".encode())
        self.assertTrue(done.wait(3))
        self.assertEqual([x[1] for x in seen], [b"m0", b"m1", b"m2"])
        self.assertNotIn(listener.loop_thread_id, {x[0] for x in seen})
        self.assertEqual({x[0] for x in seen}, {d.worker_thread_id})
        listener.close()
        self.assertTrue(d.stop())

    def test_full_queue_drops_and_counts_never_blocks_the_loop(self):
        gate, started = threading.Event(), threading.Event()

        def slow(data):
            started.set()
            gate.wait(5)
        d = Dispatcher(max_queue=1)
        d.start()
        listener = self.conn(LISTEN, dispatcher=d)
        listener.subscribe(SUBJECT, slow)
        self.broker.deliver(SUBJECT, b"a")
        self.assertTrue(started.wait(3))
        for i in range(5):
            self.broker.deliver(SUBJECT, f"x{i}".encode())
        deadline = time.monotonic() + 3
        while d.counters["received"] < 6 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(d.counters["received"], 6)
        self.assertEqual(d.counters["dropped_overflow"], 4)           # 1 queued, 4 dropped, 1 in the handler
        gate.set()
        listener.close()
        self.assertTrue(d.stop())


if __name__ == "__main__":
    unittest.main()
