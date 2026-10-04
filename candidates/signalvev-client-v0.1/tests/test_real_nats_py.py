"""The REAL nats-py client over REAL loopback sockets, against a protocol STUB (tests/protocol_stub.py).

This exercises the binding's assumptions about nats-py itself (pending_size=0 publish rules, flush semantics, subscribe
callback shape, drain/close, reconnect). It is NOT a broker roundtrip: DISPOSABLE_NATS_ROUNDTRIP is a separate claim."""
import importlib.util
import json
import threading
import time
import unittest

from _support import INLINE_SHA, NOW, TmpCase, config_doc, fixture_entry, raw_event, synthetic_fixture_doc
from protocol_stub import ProtocolStub
from signalvev_client import ListenClient, SendClient, SyntheticOwnerResolver, health, parse_config

HAVE_NATS = importlib.util.find_spec("nats") is not None
SUBJECT = "cerebro.v1.state.delta"


@unittest.skipUnless(HAVE_NATS, "nats-py is not installed in this interpreter")
class RealClientTests(TmpCase):
    def setUp(self):
        super().setUp()
        self.stub = ProtocolStub()
        self.port = self.stub.start()
        self.addCleanup(self.stub.stop)
        self.results = []
        self.clients = []
        self.addCleanup(self._close_all)

    def _close_all(self):
        for c in self.clients:
            c.close() if hasattr(c, "close") else c.stop()

    def cfg(self, name, **nats):
        doc = config_doc(self.tmp, nats={"server": f"nats://127.0.0.1:{self.port}", "flush_timeout_seconds": 1.0,
                                         "connect_timeout_seconds": 2.0, "max_reconnect_attempts": 50,
                                         "reconnect_wait_seconds": 0.1, **nats},
                         evidence={"dir": str(self.tmp / name)})
        return parse_config(doc, base_dir=self.tmp)

    def listener(self, name="rx", entry=None):
        resolver = SyntheticOwnerResolver.from_dict(synthetic_fixture_doc(entry or fixture_entry()))
        lc = ListenClient(self.cfg(name), resolver=resolver, clock=lambda: NOW, on_result=self.results.append)
        self.clients.append(lc)
        lc.start()
        return lc

    def sender(self, name="tx"):
        sc = SendClient(self.cfg(name), clock=lambda: NOW)
        self.clients.append(sc)
        sc.connect()
        return sc

    def wait_for(self, cond, timeout=5.0):
        end = time.monotonic() + timeout
        while not cond() and time.monotonic() < end:
            time.sleep(0.02)
        return cond()

    def test_health_connect_does_a_real_server_roundtrip(self):
        rep = health(self.cfg("hx"), connect=True)
        self.assertTrue(rep["ok"], rep)
        self.assertEqual(rep["transport"]["result"], "CONNECTED_AND_SERVER_ROUNDTRIP_OK")
        self.assertEqual(rep["transport"]["server"], f"127.0.0.1:{self.port}")
        self.assertEqual(rep["claims"]["peer_receiver_liveness"], "NOT_PROVEN")

    def test_real_roundtrip_two_clients_into_the_existing_receiver(self):
        lc = self.listener()
        sc = self.sender()
        out = sc.send(raw_event())
        self.assertEqual((out.state, out.stages), ("ACCEPTED", ("PRODUCED", "TRANSPORT_ACCEPTED")))
        self.assertTrue(self.wait_for(lambda: len(self.results) == 1), "frame never reached the receiver")
        self.assertEqual((self.results[0].disposition, self.results[0].reason), ("ACK_READ", "OWNER_READ_MATCHES_EVENT"))
        subjects = [s for s, _ in self.stub.pubs]
        self.assertEqual(subjects, [SUBJECT])
        self.assertEqual(json.loads(self.stub.pubs[0][1])["d0"]["event_id"], "evt-0001")     # exact frame on the wire
        lc.stop()
        sc.close()
        self.assertFalse([t for t in threading.enumerate() if t.name in ("signalvev-nats-loop", "signalvev-dispatch")])

    def test_publish_while_disconnected_writes_nothing_and_is_not_sent(self):
        sc = self.sender()
        self.stub.stop()                                          # broker outage; allow_reconnect=False => connection ends
        self.assertTrue(self.wait_for(lambda: sc.status()["state"] != "CONNECTED"))
        out = sc.send(raw_event())
        self.assertEqual((out.state, out.reason), ("NOT_SENT", "CLIENT_PROVED_NOT_WRITTEN"))
        self.assertEqual(self.stub.pubs, [])
        # a proven-unsent event may be retried on a fresh connection; nothing was buffered in the meantime
        self.port = self.stub.start(self.port)
        sc2 = self.sender("tx2")
        self.assertEqual(sc2.send(raw_event()).state, "ACCEPTED")
        self.assertEqual(len(self.stub.pubs), 1)

    def test_flush_timeout_is_unknown_and_the_library_never_resends_it(self):
        self.stub.pong_enabled = False
        sc = self.sender()
        out = sc.send(raw_event())
        self.assertEqual((out.state, out.reason), ("UNKNOWN_SEND", "FLUSH_FAILED_SERVER_RECEIPT_UNPROVEN"))
        self.assertEqual(len(self.stub.pubs), 1)                  # the PUB did reach the wire once (ambiguous to the sender)
        self.stub.pong_enabled = True
        self.stub.drop_connections()                              # a break + any library reconnect/flush logic ...
        time.sleep(0.8)
        self.assertEqual(len(self.stub.pubs), 1)                  # ... must not write it a second time
        again = sc.send(raw_event())
        self.assertEqual((again.state, again.replay_refused), ("UNKNOWN_SEND", True))
        self.assertEqual(len(self.stub.pubs), 1)

    def test_lost_hint_is_accepted_at_transport_but_never_read(self):
        self.listener()
        self.stub.swallow_pubs = True                             # broker accepted it, nobody receives it
        out = self.sender().send(raw_event())
        self.assertEqual(out.state, "ACCEPTED")
        time.sleep(0.4)
        self.assertEqual(self.results, [])

    def test_listener_reconnects_and_resubscribes_after_a_broker_outage(self):
        lc = self.listener()
        self.stub.drop_connections()
        self.assertTrue(self.wait_for(lambda: self.stub.connect_count >= 2), "listener did not reconnect")
        time.sleep(0.3)
        self.sender().send(raw_event())
        self.assertTrue(self.wait_for(lambda: len(self.results) == 1), "re-subscription did not happen")
        self.assertEqual(self.results[0].disposition, "ACK_READ")
        self.assertGreaterEqual(lc.status()["counters"]["reconnects"], 1)

    def test_stop_drains_and_leaves_no_threads(self):
        lc = self.listener()
        lc.stop()
        lc.stop()
        self.assertFalse([t for t in threading.enumerate() if t.name.startswith("signalvev-")])


if __name__ == "__main__":
    unittest.main()
