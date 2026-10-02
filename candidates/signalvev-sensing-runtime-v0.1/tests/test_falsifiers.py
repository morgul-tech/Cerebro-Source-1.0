"""The 13 REQUIRED falsifiers of the SIGNALVEV_SENSING_RUNTIME_V01 work order, one test each (F01..F13)."""
from __future__ import annotations

import json
import unittest

from fixtures import (RigTestCase, FakeResolver, INLINE_SHA, NOW, OWNER, POINTER_SHA, Rig, pointer_event, raw_event)
from signalvev_sensing import (ACK_READ, CONFLICT_HOLD, DETERMINISTIC, DUPLICATE, EXPIRED, HOLD_UNREADABLE,
                               JUDGMENT_REQUIRED, NOT_APPLICABLE, STALE_SUPERSEDED, FakeTransport, Interest,
                               InterestTable, SendLedger, SensingError, SensingSender, accept_owner_event, build_frame,
                               canonical)
from signalvev_sensing.d0 import idempotency_hash, referent_key


class RequiredFalsifiers(RigTestCase):
    def test_F00_default_memory_sender_cannot_publish(self):
        transport = FakeTransport()
        with self.assertRaisesRegex(SensingError, "DURABLE_SEND_LEDGER_REQUIRED"):
            SensingSender(transport=transport, ledger=SendLedger())
        self.assertEqual(transport.published, [])

    def test_F00b_intent_write_failure_prevents_publish(self):
        rig = Rig()

        def fail_intent(*args):
            raise OSError("injected intent write failure")

        rig.ledger.intend = fail_intent
        with self.assertRaises(OSError):
            rig.sender.send(raw_event())
        self.assertEqual(rig.transport.published, [])

    def test_F00c_outcome_write_failure_recovers_as_unknown_no_replay(self):
        rig = Rig()

        def fail_outcome(*args):
            raise OSError("injected outcome write failure")

        rig.ledger.outcome = fail_outcome
        with self.assertRaises(OSError):
            rig.sender.send(raw_event())
        self.assertEqual(len(rig.transport.published), 1)
        rig.boot()
        again = rig.sender.send(raw_event())
        self.assertEqual(again.state, "UNKNOWN_SEND")
        self.assertTrue(again.replay_refused)
        self.assertEqual(len(rig.transport.published), 1)

    def test_F01_irrelevant_receiver_is_not_applicable_zero_reread_zero_wake(self):
        rig = Rig(interests=InterestTable([Interest("owner:someone-else", "doc", None)]))
        res = rig.receiver.on_frame(rig.frame_for(raw_event()))
        self.assertEqual(res.disposition, NOT_APPLICABLE)
        self.assertEqual(res.applicability, "NOT_APPLICABLE")
        self.assertEqual(len(rig.resolver.calls), 0, "NOT_APPLICABLE must not trigger owner reread")
        self.assertEqual(res.activation.decision, DETERMINISTIC)   # no JUDGMENT_REQUIRED => no model-wake
        self.assertEqual(rig.sink.records, [], "NOT_APPLICABLE is not materially returned")
        self.assertEqual(rig.cursor.event_count(), 0, "an irrelevant receiver keeps no per-event state")

    def test_F01b_not_applicable_even_for_semantic_change(self):
        rig = Rig(interests=InterestTable([]))
        res = rig.receiver.on_frame(rig.frame_for(raw_event(change_class="SEMANTIC")))
        self.assertEqual((res.disposition, res.activation.decision), (NOT_APPLICABLE, DETERMINISTIC))
        self.assertEqual(len(rig.resolver.calls), 0)

    def test_F02_duplicate_event_no_second_work(self):
        rig = Rig()
        frame = rig.frame_for(raw_event())
        first = rig.receiver.on_frame(frame)
        second = rig.receiver.on_frame(frame)
        self.assertEqual(first.disposition, ACK_READ)
        self.assertEqual(second.disposition, DUPLICATE)
        self.assertEqual(len(rig.resolver.calls), 1, "no second owner reread")
        self.assertEqual(len(rig.sink.records), 1, "no second closure / return")

    def test_F03_same_event_id_changed_fingerprint_is_conflict_hold(self):
        rig = Rig()
        rig.receiver.on_frame(rig.frame_for(raw_event()))
        tampered = raw_event(delta={"kind": "INLINE", "expected_sha256": "0" * 64, "fields": {"status": "READY", "n": 3}})
        res = rig.receiver.on_frame(rig.frame_for(tampered))
        self.assertEqual(res.disposition, CONFLICT_HOLD)
        self.assertEqual(len(rig.resolver.calls), 1, "conflict must not trigger more work")
        original = build_frame(accept_owner_event(raw_event()), now_epoch=NOW, ttl_seconds=30)
        self.assertEqual(rig.cursor.get("evt-0001")["fingerprint"], original.fingerprint,
                         "the stored fingerprint is still the FIRST admission's, not the conflicting frame's")
        self.assertEqual(rig.cursor.get("evt-0001")["disposition"], ACK_READ, "first admitted record is never overwritten")

    def test_F04_stale_revision_is_stale_superseded(self):
        rig = Rig()
        rig.receiver.on_frame(rig.frame_for(raw_event(event_id="evt-new", owner_seq=9,
                                                      revision_basis={"before": "rev-8", "after": "rev-9"})))
        old = rig.receiver.on_frame(rig.frame_for(raw_event(event_id="evt-old", owner_seq=5)))
        self.assertEqual(old.disposition, STALE_SUPERSEDED)
        self.assertEqual(len(rig.resolver.calls), 1, "stale detected locally: no reread for the stale event")

    def test_F04b_owner_reports_superseded_on_reread(self):
        rig = Rig(resolver=FakeResolver(relation="SUPERSEDED"))
        res = rig.receiver.on_frame(rig.frame_for(raw_event()))
        self.assertEqual(res.disposition, STALE_SUPERSEDED)
        self.assertEqual(len(rig.resolver.calls), 1)

    def test_F05_applicable_exact_event_exactly_one_resolver_call(self):
        rig = Rig()
        res = rig.receiver.on_frame(rig.frame_for(raw_event()))
        self.assertEqual((res.disposition, res.resolver_calls), (ACK_READ, 1))
        self.assertEqual(len(rig.resolver.calls), 1)
        res2 = rig.receiver.on_frame(rig.frame_for(pointer_event()))
        self.assertEqual((res2.disposition, res2.resolver_calls), (ACK_READ, 1))
        self.assertEqual(len(rig.resolver.calls), 2)

    def test_F06_resolver_unavailable_is_hold_unreadable_and_not_retried(self):
        for mode in ("UNAVAILABLE", "BOOM"):
            rig = Rig(resolver=FakeResolver(mode=mode))
            res = rig.receiver.on_frame(rig.frame_for(raw_event()))
            self.assertEqual(res.disposition, HOLD_UNREADABLE, mode)
            self.assertEqual(len(rig.resolver.calls), 1, "no hidden retry")
            self.assertEqual(rig.receiver.on_frame(rig.frame_for(raw_event())).disposition, DUPLICATE)
            self.assertEqual(len(rig.resolver.calls), 1)

    def test_F07_expired_event_no_downstream_work(self):
        rig = Rig()
        frame = rig.frame_for(raw_event())
        rig.now[0] = NOW + 31
        res = rig.receiver.on_frame(frame)
        self.assertEqual(res.disposition, EXPIRED)
        self.assertEqual(len(rig.resolver.calls), 0)
        self.assertEqual(rig.sink.records, [])
        self.assertEqual(rig.cursor.event_count(), 0, "an expired frame must not poison dedupe")
        rig.now[0] = NOW + 1                      # same event, fresh frame within TTL is still admissible
        self.assertEqual(rig.receiver.on_frame(rig.frame_for(raw_event())).disposition, ACK_READ)

    def test_F08_unknown_send_no_replay_even_across_restart(self):
        tr = FakeTransport(mode="UNKNOWN", deliver_on_unknown=True)
        rig = Rig(transport=tr)
        out = rig.sender.send(raw_event())
        self.assertEqual(out.state, "UNKNOWN_SEND")
        self.assertEqual(len(tr.published), 1)
        again = rig.sender.send(raw_event())
        self.assertTrue(again.replay_refused)
        self.assertEqual(again.state, "UNKNOWN_SEND")
        rig.boot()                                  # process restart
        after = rig.sender.send(raw_event())
        self.assertTrue(after.replay_refused)
        self.assertEqual(len(tr.published), 1, "transport must never see a second publish for an UNKNOWN_SEND event")

    def test_F08b_crash_between_intent_and_outcome_is_unknown_send(self):
        rig = Rig()
        ev = accept_owner_event(raw_event())
        rig.ledger.intend(ev.event_id, build_frame(ev, now_epoch=NOW, ttl_seconds=30).fingerprint)  # crash right after the write-ahead intent
        rig.boot()
        self.assertEqual(rig.ledger.state(ev.event_id), "UNKNOWN_SEND")
        self.assertTrue(rig.sender.send(raw_event()).replay_refused)
        self.assertEqual(len(rig.transport.published), 0)

    def test_F09_restart_cursor_recovery_preserves_dedupe(self):
        rig = Rig()
        frame = rig.frame_for(raw_event())
        self.assertEqual(rig.receiver.on_frame(frame).disposition, ACK_READ)
        rig.boot()
        res = rig.receiver.on_frame(frame)
        self.assertEqual(res.disposition, DUPLICATE)
        self.assertEqual(len(rig.resolver.calls), 1)
        stale = rig.receiver.on_frame(rig.frame_for(raw_event(event_id="evt-older", owner_seq=2,
                                                                     revision_basis={"before": "rev-1", "after": "rev-2"})))
        self.assertEqual(stale.disposition, STALE_SUPERSEDED, "highwater survives restart too")

    def test_F09b_pending_claim_at_crash_is_recovered_as_hold_not_replayed(self):
        rig = Rig()
        ev = accept_owner_event(raw_event())
        built = build_frame(ev, now_epoch=NOW, ttl_seconds=30)
        rig.cursor.claim(event_id=ev.event_id, fingerprint=built.fingerprint, d0_hash=built.envelope["payload_hash"],
                         idem_key=idempotency_hash(built.d0), referent_key=referent_key(OWNER, "doc", "doc-a"), owner_seq=5)
        rig.boot()                                  # crash between claim and finalize
        rec = rig.cursor.get(ev.event_id)
        self.assertEqual((rec["disposition"], rec["reason"]), (HOLD_UNREADABLE, "RECOVERED_PENDING_OUTCOME_UNKNOWN"))
        told = rig.receiver.on_frame(rig.frame_for(raw_event()))            # redelivery surfaces the hold ONCE ...
        self.assertEqual((told.disposition, told.reason, told.returned), (HOLD_UNREADABLE, "RECOVERED_PENDING_OUTCOME_UNKNOWN", True))
        self.assertEqual(rig.receiver.on_frame(rig.frame_for(raw_event())).disposition, DUPLICATE)   # ... then dedupes
        self.assertEqual(len(rig.resolver.calls), 0, "never a second resolver run for a crashed claim")

    def test_F10_inline_small_delta_fixture(self):
        rig = Rig()
        ev = accept_owner_event(raw_event())
        built = build_frame(ev, now_epoch=NOW, ttl_seconds=30)
        d0 = json.loads(built.data)["d0"]
        self.assertEqual(d0["delta"]["kind"], "INLINE")
        self.assertLessEqual(len(canonical(d0)), 1024, "D0 is compact")
        self.assertEqual(json.loads(built.data)["envelope"]["subject"], "cerebro.v1.state.delta")
        res = rig.receiver.on_frame(built.data)
        self.assertEqual(res.disposition, ACK_READ)
        self.assertEqual(rig.resolver.calls[0].depth, "REVISION_CHECK", "inline delta needs only the cheapest owner check")
        self.assertEqual(rig.resolver.calls[0].expected_sha256, INLINE_SHA)

    def test_F11_pointer_plus_resolver_fixture(self):
        rig = Rig()
        built = build_frame(accept_owner_event(pointer_event()), now_epoch=NOW, ttl_seconds=30)
        wire = json.loads(built.data)
        self.assertEqual(wire["envelope"]["subject"], "cerebro.v1.artifact.pointer")
        self.assertNotIn("fields", wire["d0"]["delta"], "pointer carries no state, only the way to it")
        res = rig.receiver.on_frame(built.data)
        self.assertEqual(res.disposition, ACK_READ)
        req = rig.resolver.calls[0]
        self.assertEqual((req.depth, req.pointer_ref, req.expected_sha256),
                         ("POINTER_GROUND", "owner:doc-a#section-3", POINTER_SHA))
        self.assertNotIn("text-that-stays-home", json.dumps(rig.sink.records[0].as_dict()), "grounding text stays home")

    def test_F12_deterministic_case_never_judgment_required(self):
        rig = Rig()
        res = rig.receiver.on_frame(rig.frame_for(raw_event(change_class="MECHANICAL")))
        self.assertEqual((res.disposition, res.activation.decision), (ACK_READ, DETERMINISTIC))
        self.assertNotEqual(res.activation.decision, JUDGMENT_REQUIRED)
        for mode in ("UNAVAILABLE",):
            r2 = Rig(resolver=FakeResolver(mode=mode))
            self.assertEqual(r2.receiver.on_frame(r2.frame_for(raw_event())).activation.decision, DETERMINISTIC)

    def test_F13_ambiguous_semantic_case_is_judgment_required_without_choosing(self):
        for cands in (("opt-b", "opt-a"), ("opt-a", "opt-b")):
            rig = Rig(resolver=FakeResolver(candidates=cands))
            res = rig.receiver.on_frame(rig.frame_for(raw_event(change_class="SEMANTIC")))
            self.assertEqual(res.disposition, ACK_READ)
            act = res.activation
            self.assertEqual(act.decision, JUDGMENT_REQUIRED)
            self.assertEqual(act.candidates, ("opt-a", "opt-b"), "listed in neutral sorted order, never ranked")
            fields = set(act.as_dict())
            self.assertFalse(fields & {"chosen", "answer", "selected", "preferred", "recommendation", "winner"})
            self.assertFalse(act.wake_bound, "no wake mechanism is bound by this runtime")
            self.assertEqual(rig.sink.records[0].work_consumed, False)

    def test_F13b_semantic_change_without_candidates_still_needs_judgment(self):
        rig = Rig()
        res = rig.receiver.on_frame(rig.frame_for(raw_event(change_class="SEMANTIC")))
        self.assertEqual(res.activation.decision, JUDGMENT_REQUIRED)
        self.assertEqual(res.activation.candidates, ())


if __name__ == "__main__":
    unittest.main()
