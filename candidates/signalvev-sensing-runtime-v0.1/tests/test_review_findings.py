"""Regression tests for the findings of the independent adversarial review (H1-H3, M1-M4, L1-L4), each proven first."""
from __future__ import annotations

import json
import tempfile
import threading
import time
from pathlib import Path

from fixtures import FakeResolver, NOW, OWNER, Rig, RigTestCase, pointer_event, raw_event
from signalvev_sensing import (ACCEPTED, ACK_READ, CONFLICT_HOLD, DUPLICATE, HOLD_IDENTITY, HOLD_SCHEMA, HOLD_UNREADABLE,
                               NOT_APPLICABLE, NOT_SENT, UNKNOWN_SEND, FakeTransport, FlightRecorder, Interest, InterestTable,
                               NotSentError, ResolverResult, SendLedger, TransportResult, accept_owner_event, build_frame)
from signalvev_sensing.cursor import DedupeCursor
from signalvev_sensing.d0 import referent_key
from signalvev_sensing.store import JsonlStore, StoreCorrupt


def wire(frame: dict) -> bytes:
    """Serialize like a hostile peer would: plain JSON, ascii escapes (so lone surrogates are expressible)."""
    return json.dumps(frame, ensure_ascii=True, separators=(",", ":")).encode()


def frame_dict(rig, raw=None):
    return json.loads(build_frame(accept_owner_event(raw or raw_event()), now_epoch=rig.now[0], ttl_seconds=30).data)


class H1SenderReplay(RigTestCase):
    class Raising:
        def __init__(self, exc):
            self.exc, self.calls = exc, 0

        def publish(self, subject, data):
            self.calls += 1
            raise self.exc

        def subscribe(self, subject, handler):
            pass

    def test_publish_that_raises_is_unknown_send_and_never_replayed(self):
        tr = self.Raising(RuntimeError("link flapped mid-write"))
        rig = Rig(transport=tr)
        first = rig.sender.send(raw_event())
        self.assertEqual((first.state, first.replay_refused), (UNKNOWN_SEND, False))
        self.assertEqual(rig.ledger.state("evt-0001"), UNKNOWN_SEND)
        self.assertTrue(rig.sender.send(raw_event()).replay_refused)
        self.assertEqual(tr.calls, 1)

    def test_publish_raising_not_sent_error_is_retryable(self):
        tr = self.Raising(NotSentError())
        rig = Rig(transport=tr)
        self.assertEqual(rig.sender.send(raw_event()).state, NOT_SENT)
        rig.sender.send(raw_event())
        self.assertEqual(tr.calls, 2)

    def test_concurrent_sends_of_one_event_publish_once(self):
        class Slow(FakeTransport):
            def publish(self, subject, data):
                time.sleep(0.03)
                return super().publish(subject, data)
        tr = Slow()
        rig = Rig(transport=tr)
        out = []
        ts = [threading.Thread(target=lambda: out.append(rig.sender.send(raw_event()))) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(tr.published), 1)
        self.assertEqual(sorted(o.replay_refused for o in out), [False] + [True] * 5)


class H2HostileFrames(RigTestCase):
    def assert_typed_hold(self, rig, data, allowed=(HOLD_SCHEMA, HOLD_IDENTITY)):
        before = (len(rig.resolver.calls), rig.cursor.event_count())
        res = rig.receiver.on_frame(data)                      # must not raise
        self.assertIn(res.disposition, allowed, res.reason)
        self.assertEqual((len(rig.resolver.calls), rig.cursor.event_count()), before, "no reread, nothing remembered")
        return res

    def test_wrong_types_unicode_depth_nan_and_duplicate_keys_never_raise(self):
        rig = Rig()
        def with_(path_fn):
            f = frame_dict(rig)
            path_fn(f)
            return wire(f)
        cases = {
            "change_class list": with_(lambda f: f["d0"].update(change_class=["MECHANICAL"])),
            "change_class dict": with_(lambda f: f["d0"].update(change_class={"a": 1})),
            "message_type list": with_(lambda f: f["envelope"].update(message_type=["STATE_DELTA"])),
            "effect_class list": with_(lambda f: f["envelope"].update(effect_class=["NONE"])),
            "subject dict": with_(lambda f: f["envelope"].update(subject={"x": 1})),
            "lone surrogate referent id": with_(lambda f: f["d0"]["referent"].update(id="doc-\ud800")),
            "lone surrogate inline value": with_(lambda f: f["d0"]["delta"]["fields"].update(status="\udfff")),
            "lone surrogate event id": with_(lambda f: f["d0"].update(event_id="evt-\ud800")),
            "deep nesting": b"[" * 3000,
            "deep nesting object": b'{"frame":' + b"[" * 3000,
            "NaN literal": wire(frame_dict(rig)).replace(b'"owner_seq":5', b'"owner_seq":NaN'),
            "Infinity ttl": wire(frame_dict(rig)).replace(b'"ttl_seconds":30', b'"ttl_seconds":Infinity'),
            "duplicate keys": wire(frame_dict(rig)).replace(b'"owner_seq":5', b'"owner_seq":5,"owner_seq":6'),
            "huge int": wire(frame_dict(rig)).replace(b'"owner_seq":5', b'"owner_seq":' + b"9" * 5000),
            "None frame": None,
        }
        for name, data in cases.items():
            with self.subTest(name):
                self.assert_typed_hold(rig, data)

    def test_legitimate_long_ids_are_processed_not_crashed(self):
        owner, rid = "o" * 120, "d" * 150
        rig = Rig(interests=InterestTable([Interest(owner, "doc", None)]))
        raw = raw_event(owner_ref=owner, referent={"type": "doc", "id": rid})
        res = rig.receiver.on_frame(rig.frame_for(raw))
        self.assertEqual(res.disposition, ACK_READ)
        self.assertEqual(rig.receiver.on_frame(rig.frame_for(raw)).disposition, DUPLICATE)

    def test_malformed_resolver_results_become_typed_holds_not_stuck_claims(self):
        class Weird(FakeResolver):
            def __init__(self, **kw):
                super().__init__()
                self.kw = kw

            def resolve(self, request):
                self.calls.append(request)
                base = dict(source_ref=request.owner_ref, referent_type=request.referent_type, referent_id=request.referent_id,
                            current_revision="r", revision_relation="SAME", observed_sha256=request.expected_sha256,
                            grounding={"t": "x"}, candidates=())
                base.update(self.kw)
                return ResolverResult(**base)
        for kw in ({"candidates": (1, "a")}, {"candidates": ("a b", "c")}, {"observed_sha256": None},
                   {"grounding": {"x": float("nan")}}, {"current_revision": 7}, {"revision_relation": ["SAME"]}):
            for ev in ((pointer_event(),) if "grounding" in kw else (raw_event(), pointer_event())):
                rig = Rig(resolver=Weird(**kw))
                res = rig.receiver.on_frame(rig.frame_for(ev))
                self.assertEqual(res.disposition, HOLD_UNREADABLE, (kw, res.reason))
                self.assertEqual(rig.cursor.get(ev["event_id"])["disposition"], HOLD_UNREADABLE, "no stuck PENDING claim")
                self.assertEqual(len(rig.sink.records), 1, "the hold is told to the owner, not silently lost")


class H3HighwaterPoisoning(RigTestCase):
    def test_forged_huge_owner_seq_cannot_suppress_the_genuine_event(self):
        class Fussy(FakeResolver):
            def resolve(self, request):
                r = super().resolve(request)
                if request.event_id == "forged":
                    return ResolverResult(r.source_ref, r.referent_type, r.referent_id, r.current_revision, "SAME", "9" * 64)
                return r
        rig = Rig(resolver=Fussy())
        forged = rig.receiver.on_frame(rig.frame_for(raw_event(event_id="forged", owner_seq=10 ** 15,
                                                              revision_basis={"before": "x", "after": "forged-rev"},
                                                              delta={"kind": "INLINE", "expected_sha256": "8" * 64, "fields": {"a": 1}})))
        self.assertEqual(forged.disposition, CONFLICT_HOLD)
        self.assertIsNone(rig.cursor.highwater(referent_key(OWNER, "doc", "doc-a")), "unconfirmed signal sets no highwater")
        genuine = rig.receiver.on_frame(rig.frame_for(raw_event(owner_seq=6, revision_basis={"before": "rev-5", "after": "rev-6"})))
        self.assertEqual(genuine.disposition, ACK_READ)
        self.assertEqual(len(rig.resolver.calls), 2)

    def test_highwater_only_from_confirmed_reads_and_survives_restart(self):
        rig = Rig()
        rig.receiver.on_frame(rig.frame_for(raw_event(owner_seq=9, revision_basis={"before": "a", "after": "b"}, event_id="e9")))
        rig.boot()
        self.assertEqual(rig.cursor.highwater(referent_key(OWNER, "doc", "doc-a"))["owner_seq"], 9)


class OutOfOrderCompletion(RigTestCase):
    def test_highwater_never_decreases_when_reads_complete_out_of_order(self):
        gate = threading.Event()

        class Gated(FakeResolver):
            def resolve(self, request):
                if request.event_id == "e-5":
                    gate.wait(5)
                return super().resolve(request)
        rig = Rig(resolver=Gated())
        low = rig.frame_for(raw_event(event_id="e-5", owner_seq=5, revision_basis={"before": "r4", "after": "r5"}))
        high = rig.frame_for(raw_event(event_id="e-9", owner_seq=9, revision_basis={"before": "r8", "after": "r9"}))
        t = threading.Thread(target=lambda: rig.receiver.on_frame(low))
        t.start()
        while not rig.cursor.get("e-5"):
            time.sleep(0.005)
        self.assertEqual(rig.receiver.on_frame(high).disposition, ACK_READ)       # completes FIRST
        gate.set()
        t.join(3)
        self.assertEqual(rig.cursor.get("e-5")["disposition"], ACK_READ)          # completes SECOND with the lower seq
        self.assertEqual(rig.cursor.highwater(referent_key(OWNER, "doc", "doc-a"))["owner_seq"], 9)


class M1ClosureLeak(RigTestCase):
    def test_free_text_from_resolver_never_reaches_closures_or_files(self):
        class Leaky(FakeResolver):
            def __init__(self, **kw):
                super().__init__()
                self.kw = kw

            def resolve(self, request):
                self.calls.append(request)
                base = dict(source_ref=request.owner_ref, referent_type=request.referent_type, referent_id=request.referent_id,
                            current_revision="r", revision_relation="SUPERSEDED", observed_sha256="a" * 64, grounding={"t": "x"},
                            candidates=())
                base.update(self.kw)
                return ResolverResult(**base)
        leaks = [{"candidates": ("TOP-SECRET owner state " * 100, "b")}, {"observed_sha256": "TOP-SECRET free text"},
                 {"observed_sha256": "TOP-SECRET free text", "revision_relation": "SAME"}]
        for kw in leaks:
            rig = Rig(resolver=Leaky(**kw))
            res = rig.receiver.on_frame(rig.frame_for(raw_event(change_class="SEMANTIC")))
            self.assertEqual(res.disposition, HOLD_UNREADABLE)
            rig.close()
            blob = json.dumps([c.as_dict() for c in rig.sink.records]).encode() + b"".join(p.read_bytes() for p in rig.tmp.glob("*.jsonl"))
            self.assertNotIn(b"TOP-SECRET", blob)


class M2TtlBound(RigTestCase):
    def test_receiver_bounds_ttl_like_the_sender(self):
        rig = Rig()
        for ttl in (3601, 10 ** 30, 2 ** 63):
            f = frame_dict(rig)
            f["envelope"]["ttl_seconds"] = ttl
            res = rig.receiver.on_frame(wire(f))
            self.assertEqual((res.disposition, res.reason), (HOLD_SCHEMA, "TTL_OVER_BOUND"))
        self.assertEqual(len(rig.resolver.calls), 0)


class M3RecoveredHoldIsToldOnce(RigTestCase):
    def test_surfaced_flag_survives_restart(self):
        rig = Rig()
        ev = accept_owner_event(raw_event())
        b = build_frame(ev, now_epoch=NOW, ttl_seconds=30)
        from signalvev_sensing.d0 import idempotency_hash
        rig.cursor.claim(event_id=ev.event_id, fingerprint=b.fingerprint, d0_hash=b.envelope["payload_hash"],
                         idem_key=idempotency_hash(b.d0), referent_key=referent_key(OWNER, "doc", "doc-a"), owner_seq=5)
        rig.boot()
        self.assertEqual(rig.receiver.on_frame(rig.frame_for(raw_event())).disposition, HOLD_UNREADABLE)
        rig.boot()
        self.assertEqual(rig.receiver.on_frame(rig.frame_for(raw_event())).disposition, DUPLICATE)
        self.assertEqual(len(rig.resolver.calls), 0)


class M4NoHeadOfLineBlocking(RigTestCase):
    def test_duplicate_is_answered_while_the_owner_read_is_still_in_flight(self):
        started, release = threading.Event(), threading.Event()

        class Blocking(FakeResolver):
            def resolve(self, request):
                started.set()
                release.wait(5)
                return super().resolve(request)
        rig = Rig(resolver=Blocking())
        frame = rig.frame_for(raw_event())
        t = threading.Thread(target=lambda: rig.receiver.on_frame(frame))
        t.start()
        self.assertTrue(started.wait(2))
        done = []
        o = threading.Thread(target=lambda: done.append(rig.receiver.on_frame(frame).disposition))
        o.start()
        o.join(1.0)
        self.assertEqual(done, [DUPLICATE], "duplicate answered while the owner read is still in flight")
        release.set()
        t.join(2)
        self.assertEqual(len(rig.resolver.calls), 1)

    def test_not_applicable_is_not_blocked_by_in_flight_owner_read(self):
        started, release = threading.Event(), threading.Event()

        class Blocking(FakeResolver):
            def resolve(self, request):
                started.set()
                release.wait(5)
                return super().resolve(request)
        rig = Rig(resolver=Blocking(), interests=InterestTable([Interest(OWNER, "doc", "doc-a")]))
        t = threading.Thread(target=lambda: rig.receiver.on_frame(rig.frame_for(raw_event())))
        t.start()
        self.assertTrue(started.wait(2))
        got = []
        o = threading.Thread(target=lambda: got.append(rig.receiver.on_frame(rig.frame_for(
            raw_event(event_id="e-irrelevant", referent={"type": "doc", "id": "not-mine"}))).disposition))
        o.start()
        o.join(1.0)
        self.assertEqual(got, [NOT_APPLICABLE])
        release.set()
        t.join(2)


class LowFindings(RigTestCase):
    def test_L1_hold_schema_is_recorder_only_never_a_sink_write(self):
        rig = Rig()
        f = frame_dict(rig)
        f["envelope"]["issued_at"] = "2026-09-22T00:00:00Z"
        self.assertEqual(rig.receiver.on_frame(wire(f)).disposition, HOLD_SCHEMA)
        self.assertEqual(rig.sink.records, [])

    def test_L2_floods_degrade_to_counters(self):
        rec = FlightRecorder(None, max_per_kind=5)
        for _ in range(300):
            rec.record("INGRESS_REJECT", disposition="HOLD_SCHEMA")
        self.assertEqual((len(rec.records()), rec.counters["INGRESS_REJECT_SUPPRESSED"]), (1 + 5, 295))
        rig = Rig()
        frame = rig.frame_for(raw_event())
        rig.receiver.on_frame(frame)
        before = len(rig.recorder.records())
        for _ in range(50):
            rig.receiver.on_frame(frame)
        self.assertEqual((len(rig.recorder.records()), rig.recorder.counters[DUPLICATE]), (before, 50))

    def test_L3_same_event_id_different_provenance_is_a_conflict(self):
        rig = Rig()
        rig.receiver.on_frame(rig.frame_for(raw_event()))
        res = rig.receiver.on_frame(rig.frame_for(raw_event(way_home=["owner:somewhere-else#x"])))
        self.assertEqual(res.disposition, CONFLICT_HOLD)

    def test_L4_envelope_principal_must_be_the_declared_owner(self):
        rig = Rig()
        f = frame_dict(rig)
        f["envelope"]["source"]["principal_ref"] = "someone-else"
        res = rig.receiver.on_frame(wire(f))
        self.assertEqual((res.disposition, res.reason), (HOLD_IDENTITY, "ENVELOPE_PRINCIPAL_NOT_OWNER"))


class N1ForgedSeqOnRealRevision(RigTestCase):
    def forged(self):
        return raw_event(event_id="forged", owner_seq=10 ** 15)                      # real revision rev-5 + real sha, fake seq

    def test_owner_refutes_the_forged_seq_and_scope_is_not_held_hostage(self):
        rig = Rig(resolver=FakeResolver(seq=5))                                        # the owner's true seq is 5
        res = rig.receiver.on_frame(rig.frame_for(self.forged()))
        self.assertEqual((res.disposition, res.reason), (CONFLICT_HOLD, "OWNER_SEQ_DIFFERS_AT_SAME_REVISION"))
        self.assertIsNone(rig.cursor.highwater(referent_key(OWNER, "doc", "doc-a")))
        genuine = rig.receiver.on_frame(rig.frame_for(raw_event()))
        self.assertEqual(genuine.disposition, ACK_READ, "the genuine event is still read (not stale, not scope-conflicted)")
        self.assertEqual(rig.cursor.highwater(referent_key(OWNER, "doc", "doc-a"))["owner_seq"], 5)

    def test_resolver_without_seq_leaves_the_local_stale_hint_inert(self):
        rig = Rig(resolver=FakeResolver(seq=None))
        rig.receiver.on_frame(rig.frame_for(raw_event(event_id="e9", owner_seq=9, revision_basis={"before": "a", "after": "b"})))
        self.assertIsNone(rig.cursor.highwater(referent_key(OWNER, "doc", "doc-a")), "unconfirmed seq never sets a highwater")
        res = rig.receiver.on_frame(rig.frame_for(raw_event(event_id="e5", owner_seq=5, revision_basis={"before": "r4", "after": "r5"})))
        self.assertEqual(len(rig.resolver.calls), 2, "no local short-cut without an owner-confirmed seq: the owner judges")

    def test_bad_owner_seq_shape_is_a_bad_result(self):
        for bad in (0, -1, True, 1.5, "5"):
            rig = Rig(resolver=FakeResolver(seq=bad))
            self.assertEqual(rig.receiver.on_frame(rig.frame_for(raw_event())).disposition, HOLD_UNREADABLE, bad)


class N2IoFailures(RigTestCase):
    @staticmethod
    def fail_kind(store, kind):
        real = store.append
        def append(rec):
            if rec.get("kind") == kind:
                raise OSError("disk full")
            return real(rec)
        store.append = append

    def test_finalize_write_failure_keeps_the_verdict_counts_the_failure_and_recovers_conservatively(self):
        rig = Rig()
        self.fail_kind(rig.cursor._store, "FINAL")
        res = rig.receiver.on_frame(rig.frame_for(raw_event()))
        self.assertEqual((res.disposition, res.evidence_error, res.returned), (ACK_READ, "CURSOR_WRITE_FAILED", True))
        self.assertEqual(rig.cursor.get("evt-0001")["disposition"], ACK_READ)
        self.assertEqual(rig.receiver.on_frame(rig.frame_for(raw_event())).disposition, DUPLICATE)
        self.assertEqual(len(rig.resolver.calls), 1)
        rig.boot()                                                                     # FINAL never reached the file
        self.assertEqual(rig.cursor.get("evt-0001")["reason"], "RECOVERED_PENDING_OUTCOME_UNKNOWN")

    def test_claim_write_failure_refuses_the_work(self):
        rig = Rig()
        self.fail_kind(rig.cursor._store, "CLAIM")
        res = rig.receiver.on_frame(rig.frame_for(raw_event()))
        self.assertEqual((res.disposition, res.reason), (HOLD_UNREADABLE, "CURSOR_UNWRITABLE_NO_WORK_DONE"))
        self.assertEqual((len(rig.resolver.calls), rig.cursor.event_count()), (0, 0))

    def test_failing_recorder_never_changes_a_verdict_or_raises(self):
        rig = Rig()
        rig.recorder.record = lambda *a, **k: (_ for _ in ()).throw(OSError("recorder broken"))
        self.assertEqual(rig.receiver.on_frame(rig.frame_for(raw_event())).disposition, ACK_READ)
        self.assertEqual(rig.receiver.on_frame(b"garbage").disposition, HOLD_SCHEMA)
        self.assertEqual(rig.receiver.on_frame(rig.frame_for(raw_event())).disposition, DUPLICATE)
        self.assertGreater(rig.recorder.counters["RECORDER_WRITE_FAILED"], 0)


class N3SenderReentrancy(RigTestCase):
    def test_a_handler_that_sends_another_event_does_not_deadlock(self):
        holder = {}

        class Reentrant(FakeResolver):
            fired = False

            def resolve(self, request):
                if not Reentrant.fired:
                    Reentrant.fired = True
                    holder["nested"] = holder["rig"].sender.send(raw_event(event_id="evt-nested", owner_seq=2,
                                                                          revision_basis={"before": "r1", "after": "r2"}))
                    holder["same"] = holder["rig"].sender.send(raw_event())            # same event, still in flight
                return super().resolve(request)
        rig = Rig(resolver=Reentrant())
        holder["rig"] = rig
        t = threading.Thread(target=lambda: holder.setdefault("outer", rig.sender.send(raw_event())))
        t.start()
        t.join(3)
        self.assertFalse(t.is_alive(), "send() must not deadlock when a delivery handler re-enters send()")
        self.assertEqual(holder["nested"].state, ACCEPTED)
        self.assertEqual((holder["same"].state, holder["same"].replay_refused), ("IN_FLIGHT", True))
        self.assertEqual(holder["outer"].state, ACCEPTED)
        self.assertEqual(len(rig.transport.published), 2)


class N3bSendersOverlap(RigTestCase):
    def test_sends_of_different_events_are_not_serialized_behind_a_slow_publish(self):
        barrier = threading.Barrier(2, timeout=2)

        class Meeting(FakeTransport):
            def publish(self, subject, data):
                barrier.wait()                       # only passes if BOTH publishes are in flight at once
                return super().publish(subject, data)
        rig = Rig(transport=Meeting())
        out = []
        evs = [raw_event(), raw_event(event_id="evt-b", owner_seq=2, revision_basis={"before": "r1", "after": "r2"})]
        ts = [threading.Thread(target=lambda e=e: out.append(rig.sender.send(e).state)) for e in evs]
        [t.start() for t in ts]
        [t.join(5) for t in ts]
        self.assertEqual(sorted(out), [ACCEPTED, ACCEPTED])


class N4OldFormatStores(RigTestCase):
    def test_incompatible_records_and_schema_fail_closed_with_a_typed_error(self):
        import tempfile, pathlib
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="sensing-old-"))
        old = JsonlStore(tmp / "cursor.jsonl", name="receiver-cursor")
        old.append({"kind": "CLAIM", "event_id": "e1", "fingerprint": "a" * 64, "idem_key": "k", "referent_key": "r", "owner_seq": 1})
        old.close()
        from signalvev_sensing import DedupeCursor
        with self.assertRaises(StoreCorrupt):
            DedupeCursor(tmp / "cursor.jsonl")
        import signalvev_sensing.store as store_mod
        saved = store_mod.STORE_SCHEMA
        store_mod.STORE_SCHEMA = "sensing.store/v0.0"
        try:
            JsonlStore(tmp / "other.jsonl", name="receiver-cursor").close()
        finally:
            store_mod.STORE_SCHEMA = saved
        with self.assertRaises(StoreCorrupt):
            DedupeCursor(tmp / "other.jsonl")


class N4bCompleteRecordIntegrity(RigTestCase):
    @staticmethod
    def corrupt_last_complete_line(path: Path) -> bytes:
        lines = path.read_bytes().splitlines(keepends=True)
        assert len(lines) >= 2 and lines[-1].endswith(b"\n")
        lines[-1] = (b"0" if lines[-1][:1] != b"0" else b"1") + lines[-1][1:]
        damaged = b"".join(lines)
        path.write_bytes(damaged)
        return damaged

    def test_corrupt_complete_sender_intent_fails_closed_on_restart(self):
        with tempfile.TemporaryDirectory(prefix="sensing-intent-integrity-") as td:
            path = Path(td) / "sender.jsonl"
            ledger = SendLedger(path)
            ledger.intend("evt-0001", "a" * 64)
            ledger.close()
            damaged = self.corrupt_last_complete_line(path)
            with self.assertRaises(StoreCorrupt):
                SendLedger(path)
            self.assertEqual(path.read_bytes(), damaged, "corrupt complete INTENT must not be truncated")

    def test_corrupt_complete_receiver_claim_fails_closed_on_restart(self):
        with tempfile.TemporaryDirectory(prefix="sensing-claim-integrity-") as td:
            path = Path(td) / "cursor.jsonl"
            cursor = DedupeCursor(path)
            cursor.claim(event_id="evt-0001", fingerprint="a" * 64, d0_hash="b" * 64,
                         idem_key="c" * 64, referent_key="owner:doc", owner_seq=1)
            cursor.close()
            damaged = self.corrupt_last_complete_line(path)
            with self.assertRaises(StoreCorrupt):
                DedupeCursor(path)
            self.assertEqual(path.read_bytes(), damaged, "corrupt complete CLAIM must not be truncated")

    def test_incomplete_tail_is_still_repaired_conservatively(self):
        with tempfile.TemporaryDirectory(prefix="sensing-torn-tail-") as td:
            path = Path(td) / "sender.jsonl"
            ledger = SendLedger(path)
            ledger.intend("evt-0001", "a" * 64)
            ledger.close()
            with path.open("ab") as stream:
                stream.write(b"incomplete-without-newline")
            recovered = SendLedger(path)
            try:
                self.assertTrue(recovered._store.repaired_tail)
                self.assertEqual(recovered.state("evt-0001"), UNKNOWN_SEND)
            finally:
                recovered.close()


class N5FloodBounds(RigTestCase):
    def test_conflict_flood_is_bounded_in_cursor_records_and_closures(self):
        rig = Rig()
        rig.receiver.on_frame(rig.frame_for(raw_event()))
        base = len(rig.cursor._store.records())
        for i in range(200):
            res = rig.receiver.on_frame(rig.frame_for(raw_event(delta={"kind": "INLINE", "expected_sha256": f"{i:064x}",
                                                                     "fields": {"a": i}})))
            self.assertEqual(res.disposition, CONFLICT_HOLD)
        self.assertLessEqual(len(rig.cursor._store.records()) - base, 4)
        self.assertLessEqual(len(rig.sink.records), 1 + 4)

    def test_recorder_budget_bounds_the_file_across_restarts(self):
        import tempfile, pathlib
        path = pathlib.Path(tempfile.mkdtemp(prefix="sensing-rec-")) / "r.jsonl"
        for _ in range(3):
            rec = FlightRecorder(path, max_per_kind=5)
            for _ in range(10):
                rec.record("INGRESS_REJECT", disposition="HOLD_SCHEMA")
            rec.close()
        self.assertEqual(len(FlightRecorder(path, max_per_kind=5).records()), 1 + 5)

    def test_reject_flood_has_a_smaller_budget_than_admitted_evidence(self):
        self.assertLess(FlightRecorder.MAX_BY_KIND["INGRESS_REJECT"], FlightRecorder.MAX_PER_KIND)
