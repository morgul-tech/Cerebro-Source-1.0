"""Receiver hardening beyond the 13 required falsifiers: schema/identity gates, dedupe poisoning, resolver verdicts."""
from __future__ import annotations

import json
import os
import threading

from fixtures import FakeResolver, NOW, OWNER, Rig, RigTestCase, pointer_event, raw_event
from signalvev_sensing import (ACK_READ, CONFLICT_HOLD, DUPLICATE, EXPIRED, HOLD_APPLICABILITY, HOLD_IDENTITY, HOLD_SCHEMA,
                               HOLD_UNREADABLE, NOT_APPLICABLE, STALE_SUPERSEDED, Interest, InterestTable,
                               ResolverResult, SensingError, accept_owner_event, build_frame, canonical, sha256_hex)
from signalvev_sensing import _reference as ref
from signalvev_sensing.d0 import referent_key


def mutated(rig, raw, d0=None, env=None, rehash=True):
    built = build_frame(accept_owner_event(raw), now_epoch=rig.now[0], ttl_seconds=30)
    frame = json.loads(built.data)
    if d0:
        frame["d0"].update(d0)
    if env:
        frame["envelope"].update(env)
    if rehash:
        frame["envelope"]["payload_hash"] = sha256_hex(canonical(frame["d0"]))
    return canonical(frame)


class SchemaAndIdentity(RigTestCase):
    def expect(self, rig, data, disposition, reason_part):
        res = rig.receiver.on_frame(data)
        self.assertEqual(res.disposition, disposition, res.reason)
        self.assertIn(reason_part, res.reason)
        self.assertEqual(len(rig.resolver.calls), 0)
        self.assertEqual(rig.cursor.event_count(), 0, "a rejected frame is never remembered")
        return res

    def test_garbage_and_oversize_frames(self):
        rig = Rig()
        for blob in (b"not json", b"\xff\xfe", b"[]", b"{}", json.dumps({"frame": "x", "envelope": {}, "d0": {}}).encode(),
                     b"x" * 5000):
            self.expect(rig, blob, HOLD_SCHEMA, "")

    def test_unknown_envelope_version_is_typed_hold_no_implicit_upgrade(self):
        rig = Rig()
        self.expect(rig, mutated(rig, raw_event(), env={"schema_version": "2.0.0-draft"}), HOLD_SCHEMA, "UNKNOWN_VERSION")

    def test_unknown_d0_version_is_typed_hold(self):
        rig = Rig()
        self.expect(rig, mutated(rig, raw_event(), d0={"d0_version": "sensing.d0/v9"}), HOLD_SCHEMA, "UNKNOWN_VERSION")

    def test_d0_not_bound_to_envelope_hash(self):
        rig = Rig()
        data = mutated(rig, raw_event(), d0={"owner_seq": 77}, rehash=False)
        self.expect(rig, data, HOLD_SCHEMA, "D0_NOT_BOUND")

    def test_signal_claiming_effect_or_authority_is_refused(self):
        rig = Rig()
        self.expect(rig, mutated(rig, raw_event(), env={"effect_class": "WRITE_POSSIBLE"}), HOLD_SCHEMA, "EFFECT_OR_AUTHORITY")
        self.expect(rig, mutated(rig, raw_event(), env={"authority_class": "OWNER_COMMAND"}), HOLD_SCHEMA, "EFFECT_OR_AUTHORITY")

    def test_extra_or_missing_d0_fields(self):
        rig = Rig()
        self.expect(rig, mutated(rig, raw_event(), d0={"whole_owner_state": {"a": 1}}), HOLD_SCHEMA, "D0_KEYS")

    def test_identity_defects_are_hold_identity(self):
        rig = Rig()
        self.expect(rig, mutated(rig, raw_event(), d0={"way_home": []}), HOLD_IDENTITY, "WAY_HOME_MISSING")
        self.expect(rig, mutated(rig, raw_event(), d0={"owner_seq": 0}), HOLD_IDENTITY, "OWNER_SEQ")
        self.expect(rig, mutated(rig, raw_event(), d0={"owner_ref": "bad owner"}), HOLD_IDENTITY, "OWNER_REF")
        self.expect(rig, mutated(rig, raw_event(), env={"target": {"referent_type": "doc", "referent_id": "other-doc",
                                                                  "state_owner_ref": OWNER}}), HOLD_IDENTITY, "MISMATCH")

    def test_envelope_idempotency_key_must_be_the_registry_scope_of_the_d0(self):
        rig = Rig()
        self.expect(rig, mutated(rig, raw_event(), env={"idempotency_key": "attacker-chosen-key"}), HOLD_IDENTITY,
                    "IDEMPOTENCY_KEY_NOT_REGISTRY_SCOPE")

    def test_hostile_event_id_cannot_crash_ingress_or_recorder(self):
        rig = Rig()
        res = rig.receiver.on_frame(mutated(rig, raw_event(), d0={"event_id": "has space ✓"}))
        self.assertEqual(res.disposition, HOLD_IDENTITY)
        self.assertTrue(rig.recorder.records())

    def test_valid_but_oversize_frame_is_refused_before_parsing_matters(self):
        rig = Rig()
        self.expect(rig, rig.frame_for(raw_event()) + b" " * 5000, HOLD_SCHEMA, "FRAME_OVER_BOUND")

    def test_future_issued_at_is_refused_and_not_remembered(self):
        rig = Rig()
        self.expect(rig, mutated(rig, raw_event(), env={"issued_at": "2026-09-22T00:00:00Z"}), HOLD_SCHEMA, "FUTURE")

    def test_invalid_frames_do_not_poison_dedupe_for_the_real_event(self):
        rig = Rig()
        bad = mutated(rig, raw_event(), d0={"way_home": []})
        rig.receiver.on_frame(bad)
        self.assertEqual(rig.receiver.on_frame(rig.frame_for(raw_event())).disposition, ACK_READ)


class DedupeScopes(RigTestCase):
    def test_same_event_id_with_different_scope_is_conflict_for_event_id_not_a_second_admission(self):
        rig = Rig()
        rig.receiver.on_frame(rig.frame_for(pointer_event()))
        for changed in (pointer_event(delta={"kind": "POINTER", "expected_sha256": "4" * 64, "ref": "owner:doc-a#section-3"}),
                        pointer_event(owner_seq=7, revision_basis={"before": "rev-6", "after": "rev-7"})):
            res = rig.receiver.on_frame(rig.frame_for(changed))
            self.assertEqual((res.disposition, res.reason), (CONFLICT_HOLD, "SAME_EVENT_ID_CHANGED_FINGERPRINT"))
        self.assertEqual(len(rig.resolver.calls), 1)
        self.assertEqual(rig.cursor.get("evt-0002")["owner_seq"], 6, "first admission is never overwritten")

    def test_same_scope_new_event_id_same_content_is_duplicate(self):
        rig = Rig()
        rig.receiver.on_frame(rig.frame_for(raw_event()))
        res = rig.receiver.on_frame(rig.frame_for(raw_event(event_id="evt-other-id")))
        self.assertEqual(res.disposition, DUPLICATE)
        self.assertEqual(len(rig.resolver.calls), 1)

    def test_same_scope_new_event_id_changed_content_is_conflict(self):
        rig = Rig()
        rig.receiver.on_frame(rig.frame_for(raw_event()))
        res = rig.receiver.on_frame(rig.frame_for(raw_event(
            event_id="evt-other-id", delta={"kind": "INLINE", "expected_sha256": "1" * 64, "fields": {"status": "X"}})))
        self.assertEqual((res.disposition, res.reason), (CONFLICT_HOLD, "SAME_IDEMPOTENCY_SCOPE_CHANGED_FINGERPRINT"))

    def test_owner_seq_collision_is_conflict(self):
        rig = Rig()
        rig.receiver.on_frame(rig.frame_for(raw_event()))
        res = rig.receiver.on_frame(rig.frame_for(raw_event(event_id="evt-x", revision_basis={"before": "rev-4", "after": "rev-5b"})))
        self.assertEqual((res.disposition, res.reason), (CONFLICT_HOLD, "OWNER_SEQ_COLLISION"))

    def test_highwater_is_monotonic_a_stale_event_never_lowers_it(self):
        rig = Rig()
        rig.receiver.on_frame(rig.frame_for(raw_event(event_id="e-9", owner_seq=9, revision_basis={"before": "r8", "after": "r9"})))
        a = rig.receiver.on_frame(rig.frame_for(raw_event(event_id="e-5", owner_seq=5, revision_basis={"before": "r4", "after": "r5"})))
        b = rig.receiver.on_frame(rig.frame_for(raw_event(event_id="e-7", owner_seq=7, revision_basis={"before": "r6", "after": "r7"})))
        self.assertEqual((a.disposition, b.disposition), (STALE_SUPERSEDED, STALE_SUPERSEDED))
        self.assertEqual(rig.cursor.highwater(referent_key(OWNER, "doc", "doc-a"))["owner_seq"], 9)

    def test_repeated_conflict_is_returned_once_not_spammed(self):
        rig = Rig()
        rig.receiver.on_frame(rig.frame_for(raw_event()))
        bad = rig.frame_for(raw_event(delta={"kind": "INLINE", "expected_sha256": "2" * 64, "fields": {"status": "X"}}))
        a, b = rig.receiver.on_frame(bad), rig.receiver.on_frame(bad)
        self.assertEqual((a.disposition, b.disposition), (CONFLICT_HOLD, CONFLICT_HOLD))
        self.assertEqual((a.returned, b.returned), (True, False))

    def test_concurrent_duplicate_delivery_makes_exactly_one_resolver_call(self):
        import time
        rig = Rig()
        frame = rig.frame_for(raw_event())
        real_get = rig.cursor.get
        rig.cursor.get = lambda eid: (time.sleep(0.02), real_get(eid))[1]      # widen the check-then-claim window
        results = []
        threads = [threading.Thread(target=lambda: results.append(rig.receiver.on_frame(frame).disposition)) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sorted(results), [ACK_READ] + [DUPLICATE] * 7)
        self.assertEqual(len(rig.resolver.calls), 1)


class ApplicabilityAndResolverVerdicts(RigTestCase):
    def test_applicability_hold_does_no_work(self):
        for policy in (InterestTable([], available=False), type("Boom", (), {"evaluate": lambda *a: 1 / 0})()):
            rig = Rig(interests=policy)
            res = rig.receiver.on_frame(rig.frame_for(raw_event()))
            self.assertEqual((res.disposition, res.applicability), (HOLD_APPLICABILITY, "HOLD"))
            self.assertEqual((len(rig.resolver.calls), rig.cursor.event_count()), (0, 0))

    def test_wildcard_and_specific_interest(self):
        rig = Rig(interests=InterestTable([Interest(OWNER, "doc", "doc-zzz")]))
        self.assertEqual(rig.receiver.on_frame(rig.frame_for(raw_event())).disposition, NOT_APPLICABLE)
        rig2 = Rig(interests=InterestTable([Interest(OWNER, "doc", "doc-a")]))
        self.assertEqual(rig2.receiver.on_frame(rig2.frame_for(raw_event())).disposition, ACK_READ)

    def check(self, resolver, disposition, reason_part, event=None):
        rig = Rig(resolver=resolver)
        res = rig.receiver.on_frame(rig.frame_for(event or raw_event()))
        self.assertEqual(res.disposition, disposition, res.reason)
        self.assertIn(reason_part, res.reason)
        self.assertEqual(len(resolver.calls), 1)
        return rig, res

    def test_wrong_source_is_hold_identity_inline_and_pointer(self):
        self.check(FakeResolver(source_ref="impostor"), HOLD_IDENTITY, "REREAD_SOURCE_MISMATCH")
        self.check(FakeResolver(source_ref="impostor"), HOLD_IDENTITY, "REREAD_SOURCE_MISMATCH", pointer_event())

    def test_wrong_referent_is_hold_identity(self):
        class Wrong(FakeResolver):
            def resolve(self, request):
                r = super().resolve(request)
                return ResolverResult(r.source_ref, "doc", "OTHER", r.current_revision, r.revision_relation, r.observed_sha256, r.grounding)
        self.check(Wrong(), HOLD_IDENTITY, "REFERENT_MISMATCH")

    def test_owner_content_differs_at_same_revision_is_conflict(self):
        self.check(FakeResolver(sha="9" * 64), CONFLICT_HOLD, "DIFFERS_AT_SAME_REVISION")

    def test_unknown_relation_and_bad_result_are_unreadable(self):
        self.check(FakeResolver(relation="UNKNOWN"), HOLD_UNREADABLE, "RELATION_UNKNOWN")
        self.check(FakeResolver(relation="LATER_MAYBE"), HOLD_UNREADABLE, "RELATION_UNKNOWN")
        bad = FakeResolver()
        bad.resolve = lambda req, calls=bad.calls: calls.append(req) or {"truth": "dict is not a ResolverResult"}
        self.check(bad, HOLD_UNREADABLE, "BAD_RESULT")

    def test_grounding_bounds_for_pointer(self):
        self.check(FakeResolver(grounding=None), HOLD_UNREADABLE, "GROUNDING_MISSING", pointer_event())
        self.check(FakeResolver(grounding={"t": "x" * 5000}), HOLD_UNREADABLE, "GROUNDING_OVER_BOUND", pointer_event())

    def test_candidate_bound(self):
        self.check(FakeResolver(candidates=tuple(f"c{i}" for i in range(9))), HOLD_UNREADABLE, "CANDIDATES_OVER_BOUND")

    def test_owner_superseded_does_not_wake_judgment(self):
        rig, res = self.check(FakeResolver(relation="SUPERSEDED", candidates=("a", "b")), STALE_SUPERSEDED, "SUPERSEDED",
                              raw_event(change_class="SEMANTIC"))
        self.assertEqual(res.activation.decision, "DETERMINISTIC_DISPOSITION_COMPLETE")


class ClosureAndReturn(RigTestCase):
    def test_ack_read_is_not_work_consumed_nor_effect(self):
        rig = Rig()
        rig.receiver.on_frame(rig.frame_for(raw_event()))
        c = rig.sink.records[0]
        self.assertEqual((c.disposition, c.receipt_stage, c.work_consumed, c.effect, c.authority, c.is_truth_store),
                         (ACK_READ, "READ", False, "NONE_CLAIMED", "NONE", False))
        self.assertEqual(c.way_home, ("owner:doc-a#section-3",))

    def test_receipt_stages_never_pass_read_and_come_from_v01_vocabulary(self):
        rig = Rig(resolver=FakeResolver(mode="UNAVAILABLE"))
        rig.receiver.on_frame(rig.frame_for(raw_event()))
        rig2 = Rig()
        rig2.receiver.on_frame(rig2.frame_for(raw_event()))
        for c in rig.sink.records + rig2.sink.records:
            self.assertIn(c.receipt_stage, ref.STAGE_PREDECESSORS)
            self.assertIn(c.receipt_stage, ("DELIVERED", "READ"))
        self.assertEqual(rig.sink.records[0].receipt_stage, "DELIVERED", "an unread event was only delivered")

    def test_noise_dispositions_are_not_returned_material_ones_are(self):
        rig = Rig()
        frame = rig.frame_for(raw_event())
        rig.receiver.on_frame(frame)
        rig.receiver.on_frame(frame)                                   # DUPLICATE
        self.assertEqual([c.disposition for c in rig.sink.records], [ACK_READ])

    def test_failing_sink_never_changes_disposition_and_is_not_retried(self):
        rig = Rig()
        calls = []
        rig.sink.deliver = lambda c: (calls.append(c), 1 / 0)
        res = rig.receiver.on_frame(rig.frame_for(raw_event()))
        self.assertEqual((res.disposition, res.returned, res.return_error), (ACK_READ, False, "ZeroDivisionError"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(rig.receiver.on_frame(rig.frame_for(raw_event())).disposition, DUPLICATE)


class ReturnSelection(RigTestCase):
    def test_only_material_dispositions_are_selected_for_return(self):
        from signalvev_sensing import select_for_return
        for d in ("NOT_APPLICABLE", "DUPLICATE", "EXPIRED", "HOLD_SCHEMA"):
            self.assertFalse(select_for_return(d), d)
        for d in ("ACK_READ", "STALE_SUPERSEDED", "HOLD_UNREADABLE", "HOLD_IDENTITY", "CONFLICT_HOLD", "HOLD_APPLICABILITY"):
            self.assertTrue(select_for_return(d), d)


class EvidenceBoundary(RigTestCase):
    def test_no_owner_state_text_in_any_local_file(self):
        rig = Rig()
        rig.sender.send(raw_event(delta={"kind": "INLINE", "expected_sha256": "3" * 64,
                                         "fields": {"status": "SECRETVALUE42"}}))
        rig.receiver.on_frame(rig.frame_for(raw_event(event_id="evt-7", owner_seq=7, revision_basis={"before": "r6", "after": "r7"},
                                                      delta={"kind": "INLINE", "expected_sha256": "3" * 64,
                                                             "fields": {"status": "SECRETVALUE42"}})))
        rig.close()
        blob = b"".join(p.read_bytes() for p in rig.tmp.glob("*.jsonl"))
        self.assertTrue(blob)
        self.assertNotIn(b"SECRETVALUE42", blob)
        self.assertNotIn(b"text-that-stays-home", blob)

    def test_evidence_refuses_free_text(self):
        rig = Rig()
        for bad in ("free text with spaces", "unicode ✓", "x" * 300, {"a": 1}):
            with self.assertRaises(SensingError):
                rig.recorder.record("NOTE", detail=bad)
        with self.assertRaises(SensingError):
            rig.recorder.record("NOTE", **{"Bad-Key": "x"})

    def test_not_applicable_is_counter_only(self):
        rig = Rig(interests=InterestTable([]))
        n = len(rig.recorder.records())
        for _ in range(5):
            rig.receiver.on_frame(rig.frame_for(raw_event()))
        self.assertEqual(len(rig.recorder.records()), n)
        self.assertEqual(rig.recorder.counters["NOT_APPLICABLE"], 5)
        self.assertEqual(os.path.getsize(rig.tmp / "cursor.jsonl") < 400, True)
