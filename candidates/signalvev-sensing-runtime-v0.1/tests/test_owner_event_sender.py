"""Owner-event input, D0 envelope bounds, sender ledger, transport adapter, evidence reconstruction."""
from __future__ import annotations

import json

from fixtures import FakeResolver, NOW, OWNER, Rig, RigTestCase, pointer_event, raw_event
from signalvev_sensing import (ACCEPTED, NOT_SENT, UNKNOWN_SEND, CoreNatsAdapter, FakeTransport, NotSentError, SensingError,
                               accept_owner_event, build_frame, canonical, content_fingerprint, reconstruct_edges)
from signalvev_sensing import _reference as ref
from signalvev_sensing.store import JsonlStore, StoreCorrupt


class OwnerEventInput(RigTestCase):
    def test_only_committed_and_read_back_events_are_accepted(self):
        for commit in ({"state": "PENDING", "readback_ref": "rb", "observed_at": "2026-09-21T00:00:00Z"},
                       {"state": "COMMITTED_READBACK", "observed_at": "2026-09-21T00:00:00Z"},
                       {"state": "COMMITTED_READBACK", "readback_ref": "rb"}, None, "committed"):
            with self.assertRaises(SensingError) as cm:
                accept_owner_event(raw_event(commit=commit))
            self.assertEqual(cm.exception.code, "OWNER_EVENT_NOT_COMMITTED")

    def test_uncommitted_event_sends_nothing_and_leaves_no_ledger_trace(self):
        rig = Rig()
        with self.assertRaises(SensingError):
            rig.sender.send(raw_event(commit={"state": "WRITTEN_NOT_READ_BACK", "readback_ref": "rb", "observed_at": "t"}))
        self.assertEqual((len(rig.transport.published), rig.ledger.state("evt-0001")), (0, None))

    def test_identity_basis_provenance_way_home_are_retained(self):
        ev = accept_owner_event(raw_event())
        self.assertEqual((ev.event_id, ev.referent_type, ev.referent_id, ev.owner_seq, ev.revision_before, ev.revision_after,
                          ev.commit_readback_ref, ev.source_ref, ev.way_home),
                         ("evt-0001", "doc", "doc-a", 5, "rev-4", "rev-5", "rb:doc-a:rev-5", "src:doc-a", ("owner:doc-a#section-3",)))

    def test_malformed_events_are_rejected(self):
        cases = [raw_event(owner_seq=0), raw_event(owner_seq=True), raw_event(event_id="bad id"), raw_event(way_home=[]),
                 raw_event(way_home=None), raw_event(change_class="MAYBE"), raw_event(referent={"type": "doc"}),
                 raw_event(revision_basis={"after": ""}), raw_event(delta={"kind": "INLINE", "expected_sha256": "zz", "fields": {"a": 1}}),
                 raw_event(delta={"kind": "INLINE", "expected_sha256": "0" * 64, "fields": {f"k{i}": i for i in range(9)}}),
                 raw_event(delta={"kind": "INLINE", "expected_sha256": "0" * 64, "fields": {"a": {"nested": 1}}}),
                 raw_event(delta={"kind": "INLINE", "expected_sha256": "0" * 64, "fields": {"a": "x" * 65}}),
                 raw_event(delta={"kind": "POINTER", "expected_sha256": "0" * 64}), "not a mapping"]
        for case in cases:
            with self.assertRaises(SensingError, msg=str(case)[:60]):
                accept_owner_event(case)


class D0Bounds(RigTestCase):
    def test_envelope_is_unmodified_v01_and_exact(self):
        built = build_frame(accept_owner_event(raw_event()), now_epoch=NOW, ttl_seconds=30)
        ref.validate_envelope(dict(built.envelope))
        self.assertEqual(sorted(built.envelope), sorted(ref.REQUIRED_FIELDS) if hasattr(ref, "REQUIRED_FIELDS") else sorted(built.envelope))
        self.assertEqual((built.envelope["authority_class"], built.envelope["effect_class"]), ("NONE", "NONE"))

    def test_d0_is_compact_and_never_carries_owner_state_for_pointers(self):
        b = build_frame(accept_owner_event(pointer_event()), now_epoch=NOW, ttl_seconds=30)
        self.assertLessEqual(len(canonical(b.d0)), 1024)
        self.assertEqual(set(b.d0["delta"]), {"kind", "expected_sha256", "ref"})

    def test_oversize_d0_is_refused_never_truncated(self):
        big = raw_event(way_home=[f"owner:very-long-way-home-reference-number-{i:03d}-" + "x" * 60 for i in range(12)])
        with self.assertRaises(SensingError) as cm:
            build_frame(accept_owner_event(big), now_epoch=NOW, ttl_seconds=30)
        self.assertEqual(cm.exception.code, "D0_OVER_BOUND")
        rig = Rig()
        with self.assertRaises(SensingError):
            rig.sender.send(big)
        self.assertEqual(len(rig.transport.published), 0)

    def test_ttl_bounds(self):
        for ttl in (0, -1, 3601, True, 1.5):
            with self.assertRaises(SensingError):
                build_frame(accept_owner_event(raw_event()), now_epoch=NOW, ttl_seconds=ttl)

    def test_fingerprint_ignores_event_id_but_not_content(self):
        a = build_frame(accept_owner_event(raw_event()), now_epoch=NOW).d0
        b = build_frame(accept_owner_event(raw_event(event_id="evt-zz")), now_epoch=NOW).d0
        c = build_frame(accept_owner_event(raw_event(owner_seq=6)), now_epoch=NOW).d0
        self.assertEqual(content_fingerprint(a), content_fingerprint(b))
        self.assertNotEqual(content_fingerprint(a), content_fingerprint(c))


class SenderLedger(RigTestCase):
    def test_accepted_send_is_not_replayed_and_reaches_transport_accepted(self):
        rig = Rig()
        out = rig.sender.send(raw_event())
        self.assertEqual((out.state, out.stages), (ACCEPTED, ("PRODUCED", "TRANSPORT_ACCEPTED")))
        again = rig.sender.send(raw_event())
        self.assertTrue(again.replay_refused)
        self.assertEqual(len(rig.transport.published), 1)

    def test_unknown_send_never_claims_transport_accepted(self):
        rig = Rig(transport=FakeTransport(mode="UNKNOWN"))
        out = rig.sender.send(raw_event())
        self.assertEqual((out.state, out.stages), (UNKNOWN_SEND, ("PRODUCED",)))

    def test_proven_not_sent_may_be_retried(self):
        tr = FakeTransport(mode="NOT_SENT")
        rig = Rig(transport=tr)
        self.assertEqual(rig.sender.send(raw_event()).state, NOT_SENT)
        tr.mode = "OK"
        out = rig.sender.send(raw_event())
        self.assertEqual((out.state, out.replay_refused, len(tr.published)), (ACCEPTED, False, 1))

    def test_event_id_reuse_with_different_content_is_an_error(self):
        rig = Rig()
        rig.sender.send(raw_event())
        with self.assertRaises(SensingError) as cm:
            rig.sender.send(raw_event(owner_seq=99))
        self.assertEqual(cm.exception.code, "EVENT_ID_REUSED_WITH_DIFFERENT_CONTENT")

    def test_failed_outcome_write_leaves_intended_which_is_still_never_replayed(self):
        rig = Rig()
        real = rig.ledger.outcome
        rig.ledger.outcome = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
        with self.assertRaises(OSError):
            rig.sender.send(raw_event())
        rig.ledger.outcome = real
        self.assertEqual(rig.ledger.state("evt-0001"), "INTENDED")
        again = rig.sender.send(raw_event())
        self.assertEqual((again.state, again.replay_refused, len(rig.transport.published)), (UNKNOWN_SEND, True, 1))

    def test_new_owner_event_for_same_referent_is_not_a_replay(self):
        tr = FakeTransport(mode="UNKNOWN", deliver_on_unknown=True)
        rig = Rig(transport=tr)
        rig.sender.send(raw_event())
        out = rig.sender.send(raw_event(event_id="evt-0099", owner_seq=6, revision_basis={"before": "rev-5", "after": "rev-6"}))
        self.assertFalse(out.replay_refused)
        self.assertEqual(len(tr.published), 2)


class CoreNatsAdapterMapping(RigTestCase):
    class Client:
        def __init__(self, publish_exc=None, flush_exc=None):
            self.publish_exc, self.flush_exc, self.sent, self.subs = publish_exc, flush_exc, [], {}

        def publish(self, subject, data):
            if self.publish_exc:
                raise self.publish_exc
            self.sent.append((subject, data))
            for h in self.subs.get(subject, []):
                h(data)

        def flush(self, timeout):
            if self.flush_exc:
                raise self.flush_exc

        def subscribe(self, subject, handler):
            self.subs.setdefault(subject, []).append(handler)

    def test_outcome_mapping_is_conservative(self):
        C = self.Client
        self.assertEqual(CoreNatsAdapter(C()).publish("s", b"x").state, ACCEPTED)
        self.assertEqual(CoreNatsAdapter(C(flush_exc=TimeoutError())).publish("s", b"x").state, UNKNOWN_SEND)
        self.assertEqual(CoreNatsAdapter(C(publish_exc=OSError("boom"))).publish("s", b"x").state, UNKNOWN_SEND)
        self.assertEqual(CoreNatsAdapter(C(publish_exc=NotSentError())).publish("s", b"x").state, NOT_SENT)

    def test_end_to_end_over_injected_client_no_network(self):
        client = self.Client()
        adapter = CoreNatsAdapter(client)
        rig = Rig(transport=adapter)
        out = rig.sender.send(raw_event())
        self.assertEqual(out.state, ACCEPTED)
        self.assertEqual([c.disposition for c in rig.sink.records], ["ACK_READ"])
        self.assertEqual(client.sent[0][0], "cerebro.v1.state.delta")


class StoreRecovery(RigTestCase):
    def fill(self, tmp):
        rig = Rig(tmp=tmp)
        rig.receiver.on_frame(rig.frame_for(raw_event()))
        rig.close()
        return rig

    def test_torn_tail_is_dropped_state_survives_and_file_is_repaired(self):
        import tempfile, pathlib
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="sensing-store-"))
        rig = self.fill(tmp)
        with open(tmp / "cursor.jsonl", "ab") as fh:
            fh.write(b'abcd1234abcd1234 {"kind":"CLAIM","event_id":"evt-torn"')   # killed mid-write, no newline
        rig.boot()
        self.assertTrue(rig.cursor.repaired_tail)
        self.assertIsNone(rig.cursor.get("evt-torn"))
        self.assertEqual(rig.receiver.on_frame(rig.frame_for(raw_event())).disposition, "DUPLICATE")
        rig.boot()
        self.assertFalse(rig.cursor.repaired_tail, "file was repaired in place")

    def test_bad_complete_last_or_middle_line_fails_closed(self):
        import tempfile, pathlib
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="sensing-store-"))
        rig = self.fill(tmp)
        path = tmp / "cursor.jsonl"
        good = path.read_bytes()
        damaged_last = good + b"0000000000000000 {\"kind\":\"CLAIM\"}\n"
        path.write_bytes(damaged_last)
        with self.assertRaises(StoreCorrupt):
            JsonlStore(path, name="receiver-cursor")
        self.assertEqual(path.read_bytes(), damaged_last, "complete corrupt record must not be discarded")
        lines = good.split(b"\n")
        lines[1] = b"0000000000000000 " + lines[1].split(b" ", 1)[1]
        path.write_bytes(b"\n".join(lines))
        with self.assertRaises(StoreCorrupt):
            JsonlStore(path, name="receiver-cursor")

    def test_store_of_wrong_kind_is_refused(self):
        import tempfile, pathlib
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="sensing-store-"))
        self.fill(tmp)
        with self.assertRaises(StoreCorrupt):
            JsonlStore(tmp / "cursor.jsonl", name="sender-ledger")


class EdgeReconstruction(RigTestCase):
    def test_both_sides_complete_chain_is_not_broken(self):
        rig = Rig()
        rig.sender.send(raw_event())
        r = reconstruct_edges("evt-0001", send_ledger=rig.ledger, cursor=rig.cursor)
        self.assertEqual((r["status"], r["first_broken_edge"]), ("IN_PROGRESS_NO_BREAK", None))
        self.assertEqual(r["reached_stages"], ("PRODUCED", "TRANSPORT_ACCEPTED", "DELIVERED", "READ"))

    def test_unknown_send_that_did_arrive_shows_first_broken_edge(self):
        rig = Rig(transport=FakeTransport(mode="UNKNOWN", deliver_on_unknown=True))
        rig.sender.send(raw_event())
        r = reconstruct_edges("evt-0001", send_ledger=rig.ledger, cursor=rig.cursor)
        self.assertEqual((r["status"], r["first_broken_edge"]), ("BROKEN", "TRANSPORT_ACCEPTED"))

    def test_half_view_never_claims_a_break(self):
        rig = Rig()
        rig.sender.send(raw_event())
        self.assertEqual(reconstruct_edges("evt-0001", cursor=rig.cursor)["status"], "PARTIAL_LOCAL_VIEW")
        self.assertEqual(reconstruct_edges("evt-0001", send_ledger=rig.ledger)["first_broken_edge"], None)
