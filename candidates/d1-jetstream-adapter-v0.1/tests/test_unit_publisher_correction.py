"""BK11 correction r1.1 (X2_RETURN_REVIEW 2026-10-06), SYNTHETIC transport: the three publisher findings.

1 crash recovery without blind republication (durable claim before any transport effect),
2 actually bounded lookup (calls and elapsed work, sparse spans), 3 default-off => zero broker activity.
Real-broker counterparts: tests/integration/test_js_d1.py class J_PublisherCorrection.
"""
from __future__ import annotations

import unittest

from _support import FakeClock, TmpCase, reference_roots, unit_config

from d1js.composition import D1Fixture
from d1js.config import D1Config, D1ConfigError
from d1js.faults import Faults
from d1js.publisher import CLAIM_NOT_DURABLE, D1_DISABLED
from d1js.transport import InMemoryStreamTransport


class CountingTransport:
    """Records every transport method call by name, then delegates (SYNTHETIC)."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls: list[str] = []

    def __getattr__(self, name):
        target = getattr(self._inner, name)
        if not callable(target):
            return target

        def wrapped(*a, **kw):
            self.calls.append(name)
            return target(*a, **kw)
        return wrapped


class _Controlled(BaseException):
    """Catchless termination at a seam (like SystemExit): not an Exception, so no handler in the code catches it."""


class PublisherCorrection(TmpCase):
    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self.inner = InMemoryStreamTransport(clock=self.clock.epoch)
        self.transport = CountingTransport(self.inner)

    def fixture(self, faults=None, state="state", **over):
        gw_root, _ = reference_roots()
        return D1Fixture(gateway_root=gw_root, state_dir=self.tmp / state, transport=self.transport,
                         config=unit_config(**over), faults=faults or Faults(), clock=self.clock)

    def die_at(self, faults: Faults, point: str) -> None:
        def death():
            raise _Controlled(point)
        faults.on("death", death)
        faults.set(point, "call:death")

    # ---------------------------------------------------------------- finding 1
    def test_1_terminate_after_store_before_puback_recovers_by_lookup_after_window(self):
        faults = Faults()
        fx = self.fixture(faults, duplicate_window_s=1)
        mid = fx.commit_hint()["message_id"]
        self.die_at(faults, "publish.after_send_before_puback")
        with self.assertRaises(_Controlled):
            fx.publisher.publish(mid)
        row = fx.owner.outbox(mid)[0]
        self.assertEqual((row["state"], row["attempts"], self.inner.last_seq), ("SEND_CLAIMED", 1, 1))
        self.assertIsNotNone(row["claim_token"])
        self.clock.advance(2)                                        # beyond the 1 s broker duplicate window
        re = self.fixture(Faults(), duplicate_window_s=1)            # restart on the same durable state
        self.transport.calls.clear()
        self.assertEqual(re.publisher.publish(mid), "PUBLISHED")
        self.assertEqual(self.inner.last_seq, 1)                     # one stored message, no second publication
        self.assertNotIn("publish", self.transport.calls)
        self.assertGreaterEqual(re.publisher.lookup_calls, 1)
        row = re.owner.outbox(mid)[0]
        self.assertEqual((row["stream_seq"], row["last_reason"]), (1, "RECONCILED_BY_LOOKUP"))

    def test_1_terminate_after_claim_before_send_recovers_proven_absence_once(self):
        faults = Faults()
        fx = self.fixture(faults)
        mid = fx.commit_hint()["message_id"]
        self.die_at(faults, "publish.after_claim_before_send")
        with self.assertRaises(_Controlled):
            fx.publisher.publish(mid)
        self.assertEqual((fx.owner.outbox(mid)[0]["state"], self.inner.last_seq), ("SEND_CLAIMED", 0))
        self.assertNotIn("publish", self.transport.calls)
        re = self.fixture(Faults())
        # absent, but the claim is younger than claim_stale_s: it may still be in flight -> no race, no change
        self.assertEqual(re.publisher.publish(mid), "SEND_CLAIMED")
        self.assertEqual((re.publisher.publish_calls, self.inner.last_seq), (0, 0))
        self.clock.advance(re.config.claim_stale_s + 0.1)
        self.assertEqual(re.publisher.publish(mid), "PUBLISHED")     # complete lookup proved absence
        self.assertEqual((re.publisher.publish_calls, self.inner.last_seq), (1, 1))
        row = re.owner.outbox(mid)[0]
        self.assertEqual((row["attempts"], row["stream_seq"]), (2, 1))
        self.assertEqual(re.journal.counters().get("proven_absent_republish"), 1)

    def test_1_claim_write_failure_means_zero_transport_calls(self):
        faults = Faults()
        fx = self.fixture(faults)
        mid = fx.commit_hint()["message_id"]
        faults.set("owner.outbox_transition", "raise_once")
        self.transport.calls.clear()
        self.assertEqual(fx.publisher.publish(mid), CLAIM_NOT_DURABLE)
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(fx.owner.outbox(mid)[0]["state"], "INTENDED")
        self.assertEqual(fx.publisher.publish(mid), "PUBLISHED")      # the store works again: one publication

    def test_1_concurrent_publishers_cannot_both_claim(self):
        faults = Faults()
        a = self.fixture(faults)
        b = self.fixture(Faults())                                    # second process, same owner store
        mid = a.commit_hint()["message_id"]
        seen = {}

        def b_runs_while_a_holds_claim():
            seen["b"] = b.publisher.publish(mid)
        faults.on("b", b_runs_while_a_holds_claim)
        faults.set("publish.after_claim_before_send", "call:b")
        self.assertEqual(a.publisher.publish(mid), "PUBLISHED")
        self.assertEqual(seen["b"], "SEND_CLAIMED")                   # B saw A's live claim, sent nothing
        self.assertEqual((b.publisher.publish_calls, a.publisher.publish_calls, self.inner.last_seq), (0, 1, 1))
        # Direct compare-and-set: of two claims on the same (state, token) exactly one wins.
        mid2 = a.commit_hint(owner_event_key="evt-2")["message_id"]
        wins = [a.owner.outbox_transition(mid2, from_state="INTENDED", from_token=None, state="SEND_CLAIMED",
                                          claim_token=t) for t in ("t1", "t2")]
        self.assertEqual(wins, [True, False])

    def test_1_closed_conflict_capacity_and_exact_identity_preserved(self):
        fx = self.fixture(max_msgs=1)
        f = fx.commit_hint()
        self.assertEqual(fx.publisher.publish(f["message_id"]), "PUBLISHED")
        self.assertEqual(fx.publisher.publish(f["message_id"]), "PUBLISHED")         # closed: no transport call
        self.assertEqual(self.inner.last_seq, 1)
        stored = self.inner.get_msg(1)
        self.assertEqual((stored[0], stored[1]["Nats-Msg-Id"]), (f["pointer"], f["message_id"]))
        big = fx.commit_hint(owner_event_key="evt-big")                              # stream full (MaxMsgs 1)
        self.assertEqual(fx.publisher.publish(big["message_id"]), "REJECTED_CAPACITY")
        self.assertEqual(fx.publisher.publish(big["message_id"]), "REJECTED_CAPACITY")  # not recoverable/retried

    # ---------------------------------------------------------------- finding 2
    def _unknown(self, fx, key):
        f = fx.commit_hint(owner_event_key=key)
        fx.faults.set("publish.after_claim_before_send", "raise_once")              # not sent, outcome unknown
        self.assertEqual(fx.publisher.publish(f["message_id"]), "UNKNOWN_PENDING")
        return f["message_id"]

    def test_2_sparse_span_beyond_budget_is_bounded_unresolved_hold(self):
        fx = self.fixture(lookup_bound=2)
        mid = self._unknown(fx, "evt-sparse")
        get_calls = []
        self.inner.stream_state = lambda **kw: {"messages": 1, "first_seq": 1, "last_seq": 1000, "bytes": 1}
        self.inner.get_msg = lambda seq, **kw: (get_calls.append(seq) or None)
        self.transport.calls.clear()
        self.assertEqual(fx.publisher.reconcile_publication(mid), "RECONCILE_HOLD")
        self.assertLessEqual(len(get_calls), 2)
        self.assertNotIn("publish", self.transport.calls)
        row = fx.owner.outbox(mid)[0]
        self.assertEqual(row["state"], "RECONCILE_HOLD")                             # durable, visible
        self.assertIn("incomplete: span=1000", row["last_reason"])
        self.assertEqual(fx.publisher.publish(mid), "RECONCILE_HOLD")                # still no retry
        self.assertNotIn("publish", self.transport.calls)

    def test_2_elapsed_work_budget_and_shared_budget_across_rows(self):
        fx = self.fixture(lookup_bound=4, lookup_time_budget_s=0.05)
        ids = [self._unknown(fx, f"evt-{i}") for i in range(3)]
        for i in range(3):                                                           # span 3 in the stream
            self.inner.publish("cerebro.test.d1.pointer", b"{}", f"other-{i}", 1.0)
        get_calls = []
        orig = self.inner.get_msg

        def slow(seq, **kw):
            get_calls.append(seq)
            import time as _t
            _t.sleep(0.03)
            return orig(seq)
        self.inner.get_msg = slow
        self.assertEqual(fx.publisher.reconcile_publication(ids[0]), "RECONCILE_HOLD")   # time budget hit
        self.assertLessEqual(len(get_calls), 2)
        self.assertIn("budget exhausted", fx.owner.outbox(ids[0])[0]["last_reason"])
        self.inner.get_msg = lambda seq, **kw: (get_calls.append(seq) or orig(seq))
        get_calls.clear()
        fx2 = self.fixture(state="state", lookup_bound=4)
        rows = fx2.reconcile_once()                                  # one shared budget of 4 calls for 3 rows
        self.assertLessEqual(len(get_calls), 4)
        states = [r["outbox_state"] for r in rows]
        # the first row covered by the budget is complete-but-absent with a young claim (unchanged, no send);
        # the rows the remaining budget cannot cover become durable RECONCILE_HOLD
        self.assertTrue(set(states) <= {"RECONCILE_HOLD", "UNKNOWN_PENDING"}, states)
        self.assertGreaterEqual(states.count("RECONCILE_HOLD"), 2)
        self.assertEqual(self.inner.last_seq, 3)                                     # nothing republished

    def test_2_complete_lookup_semantics_kept(self):
        fx = self.fixture()
        mid = self._unknown(fx, "evt-absent")
        self.clock.advance(fx.config.claim_stale_s + 0.1)
        self.assertEqual(fx.publisher.reconcile_publication(mid), "PUBLISHED")       # proven absence -> one send
        self.assertEqual(self.inner.last_seq, 1)
        f = fx.commit_hint(owner_event_key="evt-lost-ack")
        self.inner.lose_puback = True
        self.assertEqual(fx.publisher.publish(f["message_id"]), "UNKNOWN_PENDING")
        self.inner.lose_puback = False
        self.assertEqual(fx.publisher.reconcile_publication(f["message_id"]), "PUBLISHED")   # exact match
        self.assertEqual(self.inner.last_seq, 2)
        g = fx.commit_hint(owner_event_key="evt-changed")
        self.inner.publish("cerebro.test.d1.pointer", g["pointer"] + b" ", g["message_id"], 1.0)
        fx.owner.outbox_update(g["message_id"], state="UNKNOWN_PENDING")
        self.assertEqual(fx.publisher.reconcile_publication(g["message_id"]), "CONFLICT_HOLD")  # changed digest

    def test_2_empty_real_stream_shape_first_seq_zero_proves_absence_without_lookup(self):
        fx = self.fixture()
        mid = self._unknown(fx, "evt-empty")
        self.clock.advance(fx.config.claim_stale_s + 0.1)
        self.inner.stream_state = lambda **kw: {"messages": 0, "first_seq": 0, "last_seq": 0, "bytes": 0}   # nats shape
        self.transport.calls.clear()
        self.assertEqual(fx.publisher.reconcile_publication(mid), "PUBLISHED")
        self.assertEqual((self.transport.calls.count("get_msg"), self.inner.last_seq), (0, 1))

    # ---------------------------------------------------------------- finding 3
    def test_3_default_off_zero_broker_activity_on_every_entry(self):
        on = self.fixture()                                          # pre-existing stream with a message
        on.commit_hint(owner_event_key="evt-existing")
        self.assertEqual(on.publisher.publish(on.owner.outbox()[0]["message_id"]), "PUBLISHED")
        gw_root, _ = reference_roots()
        off = D1Fixture(gateway_root=gw_root, state_dir=self.tmp / "off", transport=self.transport,
                        config=D1Config(), faults=Faults(), clock=self.clock)
        new = off.commit_hint(owner_event_key="evt-new")["message_id"]           # owner commit keeps its intent
        unk = off.commit_hint(owner_event_key="evt-unk")["message_id"]
        clm = off.commit_hint(owner_event_key="evt-clm")["message_id"]
        off.owner.outbox_update(unk, state="UNKNOWN_PENDING")
        off.owner.outbox_update(clm, state="SEND_CLAIMED", claim_token="x", claimed_at="2026-10-06T11:00:00Z")
        before = off.owner.outbox()
        self.transport.calls.clear()
        for mid in (new, unk, clm):
            self.assertEqual(off.publisher.publish(mid), D1_DISABLED)
            self.assertEqual(off.publisher.reconcile_publication(mid), D1_DISABLED)
        rows = off.reconcile_once()
        self.assertTrue(all(r["reason"].startswith("D1_DISABLED_OBLIGATION_OPEN:") for r in rows))
        m = off.metrics()
        self.assertEqual((m["stream"], m["consumer"], m["pending_obligations"]), (None, None, 3))
        self.assertEqual(off.receiver.drain_once().reason, "DEFAULT_OFF")
        self.assertEqual(self.transport.calls, [])                                  # zero transport calls
        self.assertEqual((off.publisher.publish_calls, off.publisher.lookup_calls), (0, 0))
        self.assertEqual(off.owner.outbox(), before)                                # no publication-state change
        self.assertEqual(self.inner.last_seq, 1)
        # enabled=True on the same owner state keeps the intended behaviour
        en = D1Fixture(gateway_root=gw_root, state_dir=self.tmp / "off", transport=self.transport,
                       config=unit_config(), faults=Faults(), clock=self.clock)
        self.assertEqual(en.publisher.publish(new), "PUBLISHED")
        self.assertEqual(self.inner.last_seq, 2)

    def test_3_config_bounds(self):
        with self.assertRaises(D1ConfigError):
            unit_config(publish_timeout_s=5.0, claim_stale_s=5.0)
        with self.assertRaises(D1ConfigError):
            unit_config(lookup_time_budget_s=0)
        self.assertFalse(D1Config().enabled)


if __name__ == "__main__":
    unittest.main()
