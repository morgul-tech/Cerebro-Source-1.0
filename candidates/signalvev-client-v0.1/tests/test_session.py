"""Send -> (fake private broker) -> listen -> EXISTING receiver path, plus C1/C2 and lifecycle.

The broker is an in-process stand-in for the nats-py module. These are adapter/unit proofs of routing and semantics;
they are NOT real-transport proof (see the disposable-NATS roundtrip script for that)."""
import json
import threading
import time
import unittest
from pathlib import Path

from _support import (INLINE_SHA, NOW, OWNER, POINTER_SHA, FakeBroker, TmpCase, config_doc, fixture_entry, raw_event,
                      synthetic_fixture_doc)
from signalvev_client import (EvidenceBusyError, ListenClient, SendClient, SyntheticOwnerResolver, parse_config)
from signalvev_client.errors import ConfigError, ConnectError
from signalvev_sensing import canonical, sha256_hex

SHA6 = sha256_hex(canonical({"status": "READY", "n": 6}))


def pointer_event(**over):
    base = raw_event(event_id="evt-0002", owner_seq=6, revision_basis={"before": "rev-5", "after": "rev-6"},
                     delta={"kind": "POINTER", "expected_sha256": POINTER_SHA, "ref": "owner:doc-a#section-3"},
                     commit={"state": "COMMITTED_READBACK", "readback_ref": "rb:doc-a:rev-6", "observed_at": "2026-09-21T00:00:01Z"})
    base.update(over)
    return base


def inline6(**over):
    base = raw_event(event_id="evt-0006", owner_seq=6, revision_basis={"before": "rev-5", "after": "rev-6"},
                     delta={"kind": "INLINE", "expected_sha256": SHA6, "fields": {"status": "READY", "n": 6}},
                     commit={"state": "COMMITTED_READBACK", "readback_ref": "rb:doc-a:rev-6", "observed_at": "2026-09-21T00:00:02Z"})
    base.update(over)
    return base


class Rig(TmpCase):
    def setUp(self):
        super().setUp()
        self.broker = FakeBroker()
        self.results = []
        self.clock_now = [NOW]
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            c.close() if hasattr(c, "close") else c.stop()
        super().tearDown()

    def cfg(self, **over):
        return parse_config(config_doc(self.tmp, nats={"flush_timeout_seconds": 0.3}, **over), base_dir=self.tmp)

    def listener(self, *entries, cfg=None, resolver=None):
        resolver = resolver or SyntheticOwnerResolver.from_dict(synthetic_fixture_doc(*entries))
        client = ListenClient(cfg or self.cfg(), resolver=resolver, connect_fn=self.broker.connect,
                              clock=lambda: self.clock_now[0], on_result=self.results.append)
        self.clients.append(client)
        client.start()
        self.resolver = resolver
        return client

    def sender(self, cfg=None):
        client = SendClient(cfg or self.cfg(), connect_fn=self.broker.connect, clock=lambda: NOW)
        self.clients.append(client)
        client.connect()
        return client

    def wait_results(self, n, timeout=5):
        deadline = time.monotonic() + timeout
        while len(self.results) < n and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertGreaterEqual(len(self.results), n, "frames did not reach the receiver")

    def closures(self):
        p = self.tmp / "evidence" / "closures.jsonl"
        return [json.loads(line.split(" ", 1)[1]) for line in p.read_text().splitlines()[1:]] if p.exists() else []


class RoutingTests(Rig):
    def test_inline_event_reaches_existing_receiver_and_closes_ack_read(self):
        lc = self.listener(fixture_entry())
        out = self.sender().send(raw_event())
        self.assertEqual((out.state, out.stages), ("ACCEPTED", ("PRODUCED", "TRANSPORT_ACCEPTED")))
        self.wait_results(1)
        res = self.results[0]
        self.assertEqual((res.disposition, res.reason, res.resolver_calls), ("ACK_READ", "OWNER_READ_MATCHES_EVENT", 1))
        self.assertEqual(self.resolver.calls, 1)
        (rec,) = self.closures()
        self.assertEqual((rec["disposition"], rec["work_consumed"], rec["effect"], rec["authority"], rec["is_truth_store"]),
                         ("ACK_READ", False, "NONE_CLAIMED", "NONE", False))
        self.assertIs(rec["wake_bound"], False)
        self.assertEqual(lc.status()["claims"]["delivered"], "NOT_PROVEN")
        self.assertEqual(lc.status()["resolver"], "SYNTHETIC_FIXTURE_NOT_OWNER_TRUTH")

    def test_pointer_event_uses_pointer_ground_depth(self):
        self.listener(fixture_entry(current_revision="rev-6", owner_seq=6, sha256=POINTER_SHA))
        self.sender().send(pointer_event())
        self.wait_results(1)
        self.assertEqual(self.results[0].disposition, "ACK_READ")

    def test_duplicate_frame_is_deduped_by_the_existing_cursor_and_survives_restart(self):
        lc = self.listener(fixture_entry())
        self.sender().send(raw_event())
        self.wait_results(1)
        frame = self.broker.delivered[0][1]
        self.broker.deliver("cerebro.v1.state.delta", frame)
        self.wait_results(2)
        self.assertEqual([r.disposition for r in self.results], ["ACK_READ", "DUPLICATE"])
        self.assertEqual(self.resolver.calls, 1)
        lc.stop()
        lc2 = self.listener(fixture_entry())                       # restart: same evidence dir
        self.broker.deliver("cerebro.v1.state.delta", frame)
        self.wait_results(3)
        self.assertEqual(self.results[-1].disposition, "DUPLICATE")
        self.assertEqual(self.resolver.calls, 0)                    # new resolver object: never asked again
        self.assertTrue(lc2.status()["by_disposition"])

    def test_expired_frame_is_closed_expired_without_owner_reread(self):
        self.listener(fixture_entry())
        self.sender().send(raw_event())
        self.clock_now[0] = NOW + 31
        self.broker.deliver("cerebro.v1.state.delta", self.broker.delivered[0][1])    # a late copy
        self.wait_results(2)
        self.assertEqual(self.results[-1].disposition, "EXPIRED")

    def test_not_applicable_frames_cost_nothing(self):
        cfg = self.cfg(listen={"interest": [{"owner_ref": "owner:someone-else", "referent_type": "doc"}]})
        self.listener(fixture_entry(), cfg=cfg)
        self.sender().send(raw_event())
        self.wait_results(1)
        self.assertEqual((self.results[0].disposition, self.results[0].resolver_calls), ("NOT_APPLICABLE", 0))
        self.assertEqual(self.closures(), [])

    def test_hostile_garbage_never_raises_and_never_reaches_the_owner(self):
        self.listener(fixture_entry())
        for junk in (b"", b"\xff\xfe", b"{", b"[]", b'{"frame":"sensing.frame/v0.1"}', b"x" * 9000):
            self.broker.deliver("cerebro.v1.state.delta", junk)
        self.wait_results(6)
        self.assertTrue(all(r.disposition == "HOLD_SCHEMA" for r in self.results))
        self.assertEqual(self.resolver.calls, 0)


class OwnerTruthAndLossTests(Rig):
    def test_c1_old_hint_cannot_override_newer_owner_truth(self):
        self.listener(fixture_entry(current_revision="rev-7", owner_seq=7, sha256=sha256_hex(b"newer")))
        self.sender().send(raw_event())                            # an OLD hint (seq 5) about a referent now at seq 7
        self.wait_results(1)
        res = self.results[0]
        self.assertEqual((res.disposition, res.reason), ("STALE_SUPERSEDED", "OWNER_REPORTS_REVISION_SUPERSEDED"))
        (rec,) = self.closures()
        self.assertEqual((rec["disposition"], rec["work_consumed"], rec["effect"]), ("STALE_SUPERSEDED", False, "NONE_CLAIMED"))
        self.assertNotIn("ACK_READ", [r.disposition for r in self.results])

    def test_c1_reordered_hints_newer_first_then_older_is_stale_without_second_reread(self):
        self.listener(fixture_entry(current_revision="rev-6", owner_seq=6, sha256=SHA6))
        s = self.sender()
        s.send(inline6())
        self.wait_results(1)
        self.assertEqual(self.results[0].disposition, "ACK_READ")
        s.send(raw_event())                                        # the delayed older announcement arrives last
        self.wait_results(2)
        self.assertEqual((self.results[1].disposition, self.results[1].reason),
                         ("STALE_SUPERSEDED", "LOCAL_OWNER_SEQ_BEHIND_CONFIRMED_HIGHWATER"))
        self.assertEqual(self.resolver.calls, 1)

    def test_owner_content_mismatch_is_conflict_hold_not_ack(self):
        self.listener(fixture_entry(sha256=sha256_hex(b"different-bytes")))
        self.sender().send(raw_event())
        self.wait_results(1)
        self.assertEqual(self.results[0].disposition, "CONFLICT_HOLD")

    def test_unreadable_owner_is_typed_hold_never_ack(self):
        self.listener()                                            # fixture without the referent
        self.sender().send(raw_event())
        self.wait_results(1)
        self.assertEqual(self.results[0].disposition, "HOLD_UNREADABLE")

    def test_c2_unknown_send_is_never_blindly_replayed_even_across_restart(self):
        s = self.sender()
        self.broker.flush_error = True
        out = s.send(raw_event())
        self.assertEqual((out.state, out.replay_refused), ("UNKNOWN_SEND", False))
        self.broker.flush_error = False
        calls = len(self.broker.publish_calls)
        again = s.send(raw_event())
        self.assertEqual((again.state, again.replay_refused, again.reason), ("UNKNOWN_SEND", True, "REPLAY_REFUSED_NO_SECOND_PUBLISH"))
        s.close()
        s2 = self.sender()                                         # process restart, same evidence dir
        self.assertEqual(s2.send(raw_event()).replay_refused, True)
        self.assertEqual(len(self.broker.publish_calls), calls)    # nothing was written a second time

    def test_c2_ambiguous_write_then_error_may_arrive_once_but_is_recorded_unknown_and_not_replayed(self):
        self.listener(fixture_entry())
        s = self.sender()
        self.broker.script = ["write_then_drop"]
        self.assertEqual(s.send(raw_event()).state, "UNKNOWN_SEND")
        self.wait_results(1)                                       # it DID arrive, the sender could not know
        self.assertEqual(s.send(raw_event()).replay_refused, True)
        self.assertEqual(len([c for c in self.broker.publish_calls]), 1)

    def test_c2_lost_hint_leaves_no_false_delivery_liveness_or_effect(self):
        lc = self.listener(fixture_entry())
        s = self.sender()
        self.broker.script = ["lose"]                              # broker outage: flush fine, nothing delivered
        out = s.send(raw_event())
        self.assertEqual(out.state, "ACCEPTED")                    # transport evidence only ...
        time.sleep(0.3)
        self.assertEqual(self.results, [])                         # ... and the receiver never saw it
        self.assertEqual(self.closures(), [])
        st = lc.status()
        self.assertEqual((st["claims"]["delivered"], st["claims"]["peer_receiver_liveness"], st["claims"]["effect"]),
                         ("NOT_PROVEN", "NOT_PROVEN", "NONE_CLAIMED"))
        self.assertEqual(self.resolver.calls, 0)
        # no inferred worker START / bind / wake artefact: only the evidence files of a receiver exist, all empty of work
        names = sorted(p.name for p in (self.tmp / "evidence").iterdir())
        self.assertTrue(set(names) <= {"cursor.jsonl", "recorder.jsonl", "closures.jsonl", "receiver.lock",
                                       "sender.lock", "sender.jsonl"}, names)
        self.assertNotIn("event_id", (self.tmp / "evidence" / "cursor.jsonl").read_text())   # no frame was ever claimed/handled
        self.assertEqual(self.closures(), [])

    def test_c2_receiver_outage_hint_sent_while_no_listener_is_lost_not_queued(self):
        s = self.sender()
        self.assertEqual(s.send(raw_event()).state, "ACCEPTED")    # nobody subscribed: core NATS drops it
        self.listener(fixture_entry())
        time.sleep(0.3)
        self.assertEqual(self.results, [])                         # no JetStream/inbox: lawful D0 loss

    def test_c2_listener_connection_loss_ends_the_wait_with_a_typed_reason(self):
        lc = self.listener(fixture_entry())
        for nc in self.broker.connects:
            nc.is_closed, nc.is_connected = True, False
        self.assertEqual(lc.wait(duration=3), "CONNECTION_LOST")


class LifecycleTests(Rig):
    def test_two_senders_or_two_listeners_cannot_share_an_evidence_dir(self):
        self.sender()
        with self.assertRaises(EvidenceBusyError):
            SendClient(self.cfg(), connect_fn=self.broker.connect).connect()
        self.listener(fixture_entry())
        with self.assertRaises(EvidenceBusyError):
            ListenClient(self.cfg(), resolver=self.resolver, connect_fn=self.broker.connect).start()

    def test_failed_connect_releases_the_lock_and_leaves_no_threads(self):
        self.broker.refuse_connect = True
        with self.assertRaises(ConnectError):
            SendClient(self.cfg(), connect_fn=self.broker.connect).connect()
        with self.assertRaises(ConnectError):
            ListenClient(self.cfg(), resolver=SyntheticOwnerResolver.from_dict(synthetic_fixture_doc()),
                         connect_fn=self.broker.connect).start()
        self.broker.refuse_connect = False
        self.sender()                                              # lock was released
        self.assertFalse([t for t in threading.enumerate() if t.name == "signalvev-dispatch"])

    def test_listen_needs_an_interest_and_stop_is_clean_and_idempotent(self):
        with self.assertRaises(ConfigError):
            ListenClient(self.cfg(listen={"interest": []}), resolver=None, connect_fn=self.broker.connect).start()
        lc = self.listener(fixture_entry())
        lc.stop()
        lc.stop()
        self.assertFalse([t for t in threading.enumerate() if t.name in ("signalvev-dispatch", "signalvev-nats-loop")])

    def test_stop_lets_already_received_frames_finish(self):
        lc = self.listener(fixture_entry())
        self.sender().send(raw_event())
        lc.stop()                                                  # drain: the received frame is still processed
        self.assertEqual(len(self.results), 1)

    def test_invalid_owner_event_is_rejected_before_any_publish(self):
        s = self.sender()
        bad = raw_event()
        bad["commit"] = {"state": "WRITTEN_NOT_READ_BACK"}
        from signalvev_sensing.model import SensingError
        with self.assertRaises(SensingError):
            s.send(bad)
        self.assertEqual(self.broker.publish_calls, [])


if __name__ == "__main__":
    unittest.main()
