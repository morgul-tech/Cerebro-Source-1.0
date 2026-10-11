"""CAP26 G: the REAL existing SensingReceiver composed with ContextSliceOwnerResolver over the local pinned provider.

Uses the existing test helpers (Rig, pointer_event, raw_event, OWNER) and the supplied receiver class unchanged.
"""
from __future__ import annotations

from context_slice_support import FULL_SHA, ORIGINAL, SLICE_SHA, TmpRoot, binding, sha
from fixtures import OWNER, Rig, RigTestCase, pointer_event, raw_event
from signalvev_sensing import (ACK_READ, CONFLICT_HOLD, HOLD_UNREADABLE, MAX_GROUNDING_BYTES, STALE_SUPERSEDED,
                               ResolveRequest, ResolverUnavailable, canonical)
from signalvev_sensing.context_slice import ContextSliceResolver, Selector
from signalvev_sensing.context_slice_owner import ContextSliceOwnerResolver, PointerBinding
from signalvev_sensing.d0 import referent_key

POINTER = "owner:doc-a#section-3"
SUBJECT = "actor:sensing-receiver"


class G_ReceiverComposition(TmpRoot, RigTestCase):
    def setUp(self):
        TmpRoot.setUp(self)
        self.bindings = [binding(content_ref="fixture:doc-a", owner_ref=OWNER, revision="rev-6", owner_seq=6,
                                 authorized_subjects=[SUBJECT], way_home=POINTER)]
        self.write_registry()
        self.adapter = self.make_adapter()
        self.rig = Rig(resolver=self.adapter)

    def tearDown(self):
        RigTestCase.tearDown(self)

    def make_adapter(self, *, selector=None, extra=()):
        sel = selector or Selector(start=774, end=990, expected_slice_sha256=SLICE_SHA)
        b = PointerBinding(pointer_ref=POINTER, owner_ref=OWNER, referent_type="doc", referent_id="doc-a",
                           content_ref="fixture:doc-a", way_home=POINTER, selector=sel)
        return ContextSliceOwnerResolver(self.resolver(), subject_ref=SUBJECT, bindings=[b, *extra])

    def event(self, **over):
        delta = {"kind": "POINTER", "expected_sha256": over.pop("expected_sha256", FULL_SHA),
                 "ref": over.pop("ref", POINTER)}
        return pointer_event(delta=delta, **over)

    def ingest(self, ev):
        return self.rig.receiver.on_frame(self.rig.frame_for(ev))

    def assert_source_unchanged(self):
        self.assertEqual(sha((self.root / "original.txt").read_bytes()), FULL_SHA)

    # ---------------------------------------------------------------- positive
    def test_g_pointer_ground_exact_authorized_fresh_slice_is_ack_read(self):
        res = self.ingest(self.event())
        self.assertEqual((res.disposition, res.reason, res.resolver_calls), (ACK_READ, "OWNER_READ_MATCHES_EVENT", 1))
        sr = self.adapter.last_result
        self.assertEqual((sr.outcome, sr.slice_sha256, sr.verified_content_sha256), ("SLICE_READY", SLICE_SHA,
                                                                                      FULL_SHA))
        self.assertNotEqual(SLICE_SHA, FULL_SHA)                       # event hash = FULL source, never the slice
        self.assertEqual((self.provider.stats.content_opens, self.provider.stats.describes), (1, 2))
        hw = self.rig.cursor.highwater(referent_key(OWNER, "doc", "doc-a"))
        self.assertEqual(hw["owner_seq"], 6)                          # provider-attested seq confirmed by receiver
        self.assert_source_unchanged()

    def test_g_grounding_is_bounded_and_carries_slice_identity(self):
        req = ResolveRequest(event_id="e1", owner_ref=OWNER, referent_type="doc", referent_id="doc-a",
                             depth="POINTER_GROUND", pointer_ref=POINTER, expected_revision="rev-6",
                             expected_sha256=FULL_SHA, owner_seq=6)
        out = self.adapter.resolve(req)
        g = out.grounding
        self.assertLessEqual(len(canonical(dict(g))), MAX_GROUNDING_BYTES)
        self.assertEqual((g["slice_sha256"], g["selector"]["start"], g["way_home"], g["original_ref"]),
                         (SLICE_SHA, 774, POINTER, "fixture:doc-a"))
        self.assertEqual(g["slice_text"].encode(), ORIGINAL[774:990])
        self.assertEqual((out.observed_sha256, out.source_ref, out.owner_seq), (FULL_SHA, OWNER, 6))

    def test_g_owner_seq_only_when_attested_no_invented_highwater(self):
        self.bindings[0]["owner_seq"] = None
        self.write_registry()
        res = self.ingest(self.event())
        self.assertEqual(res.disposition, ACK_READ)
        self.assertIsNone(self.rig.cursor.highwater(referent_key(OWNER, "doc", "doc-a")))

    def test_g_revision_check_reads_metadata_only(self):
        req = ResolveRequest(event_id="e2", owner_ref=OWNER, referent_type="doc", referent_id="doc-a",
                             depth="REVISION_CHECK", pointer_ref=None, expected_revision="rev-6",
                             expected_sha256=FULL_SHA, owner_seq=6)
        out = self.adapter.resolve(req)
        self.assertIsNone(out.grounding)
        self.assertEqual((out.revision_relation, out.observed_sha256, out.current_revision), ("SAME", FULL_SHA,
                                                                                               "rev-6"))
        self.assertEqual((self.provider.stats.content_opens, self.provider.stats.read_calls,
                          self.provider.stats.describes), (0, 0, 1))
        # through the real receiver: an INLINE event's digest is not the file digest -> observable conflict, 0 reads
        res = self.ingest(raw_event(revision_basis={"before": "rev-5", "after": "rev-6"}, owner_seq=6))
        self.assertEqual((res.disposition, res.reason), (CONFLICT_HOLD, "OWNER_CONTENT_DIFFERS_AT_SAME_REVISION"))
        self.assertEqual(self.provider.stats.content_opens, 0)

    # ---------------------------------------------------------------- negatives: no receiver accept, source unchanged
    def test_g_superseded_maps_to_existing_stale(self):
        self.bindings[0].update(revision="rev-7", superseded_revisions=["rev-6"])
        self.write_registry()
        res = self.ingest(self.event())
        self.assertEqual((res.disposition, res.reason), (STALE_SUPERSEDED, "OWNER_REPORTS_REVISION_SUPERSEDED"))
        self.assertEqual(self.provider.stats.content_opens, 0)
        self.assert_source_unchanged()

    def test_g_unknown_relation_fails_closed(self):
        self.bindings[0]["head_relation"] = "UNKNOWN"
        self.write_registry()
        res = self.ingest(self.event())
        self.assertEqual((res.disposition, res.reason), (HOLD_UNREADABLE, "REVISION_RELATION_UNKNOWN"))
        self.assertEqual(self.provider.stats.content_opens, 0)

    def test_g_wrong_full_hash_is_observable_conflict_not_fabricated_same(self):
        res = self.ingest(self.event(expected_sha256="4" * 64))
        self.assertEqual((res.disposition, res.reason), (CONFLICT_HOLD, "OWNER_CONTENT_DIFFERS_AT_SAME_REVISION"))
        self.assertEqual(self.provider.stats.content_opens, 0)
        self.assertEqual(self.rig.sink.records[-1].observed_sha256, FULL_SHA)   # owner's real digest is visible

    def test_g_attested_owner_seq_disagreeing_is_conflict(self):
        self.bindings[0]["owner_seq"] = 7
        self.write_registry()
        res = self.ingest(self.event())
        self.assertEqual((res.disposition, res.reason), (CONFLICT_HOLD, "OWNER_SEQ_DIFFERS_AT_SAME_REVISION"))

    def test_g_fail_closed_negatives_hold_unreadable(self):
        cases = []
        # wrong slice hash in the binding
        self.adapter = self.make_adapter(selector=Selector(start=774, end=990, expected_slice_sha256="5" * 64))
        self.rig = Rig(resolver=self.adapter)
        cases.append(("slice", self.ingest(self.event(event_id="evt-s"))))
        # unknown pointer ref -> no provider call at all
        self.adapter = self.make_adapter()
        self.rig = Rig(resolver=self.adapter)
        describes = self.provider.stats.describes
        cases.append(("ref", self.ingest(self.event(event_id="evt-r", ref="owner:doc-a#unknown"))))
        self.assertEqual(self.provider.stats.describes, describes)
        # pointer bound to doc-a but event referent doc-b
        self.rig = Rig(resolver=self.adapter)
        cases.append(("referent", self.ingest(self.event(event_id="evt-x", referent={"type": "doc", "id": "doc-b"}))))
        # subject no longer authorized (fresh receiver state: no dedupe interference)
        self.bindings[0]["authorized_subjects"] = ["actor:other"]
        self.write_registry()
        self.rig = Rig(resolver=self.adapter)
        cases.append(("subject", self.ingest(self.event(event_id="evt-d"))))
        for name, res in cases:
            self.assertEqual((name, res.disposition, res.reason), (name, HOLD_UNREADABLE,
                                                                   "RESOLVER_UNAVAILABLE_OR_FAILED"))
        self.assertEqual(self.provider.stats.content_opens, 1)              # only the wrong-slice case opened
        self.assert_source_unchanged()

    def test_g_source_altered_never_acks(self):
        target = self.root / "original.txt"

        def tamper(_):
            data = bytearray(target.read_bytes())
            data[800] ^= 1
            target.write_bytes(bytes(data))
        self.provider.on("before_open", tamper)
        res = self.ingest(self.event())
        self.assertEqual(res.disposition, HOLD_UNREADABLE)
        self.assertIsNone(self.adapter.last_result.slice_text)

    def test_g_adapter_rejects_wrong_depth_or_identity_directly(self):
        req = ResolveRequest(event_id="e3", owner_ref="owner:other", referent_type="doc", referent_id="doc-a",
                             depth="POINTER_GROUND", pointer_ref=POINTER, expected_revision="rev-6",
                             expected_sha256=FULL_SHA, owner_seq=6)
        with self.assertRaises(ResolverUnavailable):
            self.adapter.resolve(req)
        req = ResolveRequest(event_id="e4", owner_ref=OWNER, referent_type="doc", referent_id="doc-a",
                             depth="REVISION_CHECK", pointer_ref=POINTER, expected_revision="rev-6",
                             expected_sha256=FULL_SHA, owner_seq=6)
        with self.assertRaises(ResolverUnavailable):
            self.adapter.resolve(req)
        self.assertEqual(self.provider.stats.describes, 0)
