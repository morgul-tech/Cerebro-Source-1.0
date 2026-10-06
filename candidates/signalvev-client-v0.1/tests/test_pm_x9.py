"""BK07 focused acceptance tests A-H for the PM-to-X9 composition.

All providers/transports are SYNTHETIC_TEST_ONLY doubles wired through the SAME factory (pm_x9.build_binding) a host
uses. Nothing here is PM truth, a real channel or a NATS server. SIGNALVEV_CLIENT_TEST_TARGET=installed runs these
against the installed wheel (see _support).
"""
from __future__ import annotations

import io
import json
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path

import _support  # noqa: F401  (path setup for the source route; touches nothing for the installed route)
from signalvev_client import pm_x9, pm_x9_cli
from signalvev_client import pm_x9_synthetic as syn
from signalvev_sensing import accept_owner_event, build_frame
from signalvev_sensing.activation import decide_activation
from signalvev_sensing.return_sink import make_closure

x9, pm = pm_x9.x9, pm_x9.pm
RECEIPT = "pm-receipt:bk07-a"
POINTER_SUBJECT = "cerebro.v1.artifact.pointer"


class Base(unittest.TestCase):
    def world(self, **kw) -> syn.SyntheticWorld:
        w = syn.SyntheticWorld(**kw)
        self.addCleanup(w.close)
        return w

    def binding(self, world: syn.SyntheticWorld, *, listen: bool = True, send: bool = True) -> pm_x9.PmX9Binding:
        b = pm_x9.build_binding(world.settings, world.ports())
        self.addCleanup(b.close)
        if listen:
            b.start_listener()
        if send:
            b.open_sender()
        return b

    def deliver(self, world, b, receipt=RECEIPT, *, n=7, **seed):
        facts = world.pm.seed_hint(receipt, n=n, **seed)
        before = len(b.ingress_results())
        sent = b.send_hint(receipt)
        self.assertEqual(sent.state, "TRANSPORT_ACCEPTED", sent)
        self.assertTrue(b.wait_for_ingress(before + 1, 5.0))
        return facts, sent

    @staticmethod
    def frame_for(facts, world, **changes):
        raw = pm_x9.owner_event_to_raw(accept_owner_event({
            "event_id": facts["event_id"], "owner_ref": world.settings.owner_ref, "source_ref": world.settings.owner_ref,
            "referent": {"type": "PM_READY_HINT", "id": facts["referent_id"]}, "owner_seq": facts["owner_seq"],
            "revision_basis": {"after": facts["revision"], "before": facts["revision_before"]},
            "change_class": "SEMANTIC",
            "delta": {"kind": "POINTER", "ref": facts["snapshot_ref"], "expected_sha256": facts["snapshot_sha256"]},
            "commit": {"state": "COMMITTED_READBACK", "readback_ref": "pm-readback:x", "observed_at": "2026-10-05T12:00:00Z"},
            "way_home": [facts["snapshot_ref"], facts["receipt_ref"]]}))
        for k, v in changes.items():
            raw[k] = v
        return build_frame(accept_owner_event(raw), now_epoch=world.clock().timestamp(), ttl_seconds=30).data


# ---------------------------------------------------------------------------------------------- A
class A_WholePath(Base):
    def test_material_route_end_to_end_with_identity_at_every_stage(self):
        w = self.world()
        b = self.binding(w)
        facts, sent = self.deliver(w, b)
        # send stage
        self.assertEqual((sent.event_id, sent.attempt_ref, sent.referent_type, sent.referent_id, sent.owner_seq,
                          sent.revision, sent.packet_sha256),
                         (facts["event_id"], "attempt:synthetic-1", "PM_READY_HINT", facts["referent_id"], 7, "rev:7",
                          syn.PACKET_SHA))
        # wire stage: the D0 that actually crossed the transport
        subject, data = w.broker.published[0]
        d0 = json.loads(data)["d0"]
        self.assertEqual(subject, POINTER_SUBJECT)
        self.assertEqual((d0["event_id"], d0["referent"], d0["owner_seq"], d0["revision_after"],
                          d0["delta"]["expected_sha256"]),
                         (facts["event_id"], {"type": "PM_READY_HINT", "id": facts["referent_id"]}, 7, "rev:7",
                          facts["snapshot_sha256"]))
        self.assertNotIn("claim", json.dumps(d0).lower())        # complete PM state is not in the D0
        # receive + deposit stage
        [dep] = b.sink.records
        self.assertEqual((dep.state, dep.receiver_disposition, dep.event_id, dep.attempt_ref, dep.referent_id,
                          dep.owner_seq, dep.revision, dep.packet_sha256, dep.queued_for_pulse),
                         ("DEPOSITED_READBACK", "ACK_READ", facts["event_id"], "attempt:synthetic-1",
                          facts["referent_id"], 7, "rev:7", syn.PACKET_SHA, True))
        pointer = w.store.read("pointers", facts["event_id"]).record
        self.assertEqual((pointer.referent_type, pointer.referent_id, pointer.claim_ref, pointer.owner_seq,
                          pointer.revision, pointer.packet_sha256, pointer.attempt_id, pointer.way_home),
                         ("PM_READY_HINT", facts["referent_id"], w.settings.claim_ref, 7, "rev:7", syn.PACKET_SHA,
                          "attempt:synthetic-1", (facts["snapshot_ref"], RECEIPT)))
        self.assertNotEqual(pointer.referent_id, pointer.claim_ref)
        # no X9 decision yet: depositing never consumes
        self.assertEqual(w.store.disposition_appends, 0)
        rereads_before = w.pm.reread_calls
        out = b.consume_one(facts["event_id"])
        self.assertEqual((out.state, out.disposition, out.reason), ("DISPOSITION_READBACK", "MATERIAL_ROUTE",
                                                                   "NEW_CURRENT_PM_MATERIAL"))
        self.assertEqual(w.pm.reread_calls, rereads_before + 1)   # X9 made its own fresh PM read
        rec = w.store.read("dispositions", facts["event_id"]).record
        self.assertEqual((rec.event_id, rec.attempt_id, rec.pointer_sha256, rec.owner_revision, rec.work_consumed,
                          rec.effect), (facts["event_id"], "attempt:synthetic-1", pointer.content_sha256, "rev:7",
                                        False, "NONE_CLAIMED"))
        # counts: one PM read + 3 rereads (sender check, receiver resolve, X9 consume), one publish, one append each
        self.assertEqual((w.pm.read_calls, w.pm.reread_calls, len(w.broker.published), w.store.pointer_appends,
                          w.store.disposition_appends), (1, 3, 1, 1, 1))

    def test_receiver_owner_read_runs_off_the_event_loop(self):
        w = self.world()
        b = self.binding(w)
        self.deliver(w, b)
        loop_thread = b.listener._conn.loop_thread_id
        self.assertTrue(b.resolver.thread_ids)
        self.assertNotIn(loop_thread, b.resolver.thread_ids)
        self.assertEqual(set(b.resolver.thread_ids), {b.listener._dispatcher.worker_thread_id})


# ---------------------------------------------------------------------------------------------- B
class B_Superseded(Base):
    def test_superseded_at_receive_time_is_never_deposited(self):
        w = self.world()
        b = self.binding(w)
        w.broker.hold = True
        facts = w.pm.seed_hint(RECEIPT, n=10)                   # rev:10
        self.assertEqual(b.send_hint(RECEIPT).state, "TRANSPORT_ACCEPTED")
        w.pm.supersede(facts["referent_id"], "rev:9", owner_seq=11)   # owner says rev:10 is superseded by "rev:9"
        w.broker.release()
        self.assertTrue(b.wait_for_ingress(1))
        self.assertEqual(b.ingress_results()[0].disposition, "STALE_SUPERSEDED")
        [dep] = b.sink.records
        self.assertEqual(dep.state, "NOT_DEPOSITED")
        self.assertEqual(w.store.pointer_appends, 0)

    def test_superseded_at_consume_time_is_stale_never_material(self):
        for new_rev in ("rev:9", "rev:11", "zzz-opaque"):        # never ordered by the bridge, only owner relation
            with self.subTest(new_rev=new_rev):
                w = self.world()
                b = self.binding(w)
                facts, _ = self.deliver(w, b, n=10)
                w.pm.supersede(facts["referent_id"], new_rev, owner_seq=12)
                out = b.consume_one(facts["event_id"])
                self.assertEqual((out.state, out.disposition, out.reason),
                                 ("DISPOSITION_READBACK", "STALE", "PM_OWNER_SUPERSEDED_HINT"))

    def test_lexically_larger_old_revision_is_still_same_if_owner_says_so(self):
        w = self.world()
        b = self.binding(w)
        facts, _ = self.deliver(w, b, n=10)                     # owner current stays rev:10
        self.assertEqual(b.consume_one(facts["event_id"]).disposition, "MATERIAL_ROUTE")


# ---------------------------------------------------------------------------------------------- C
class C_NoMaterialDelta(Base):
    def test_same_with_already_handled_material(self):
        w = self.world()
        b = self.binding(w)
        facts, _ = self.deliver(w, b, material_sha256="c" * 64)
        before = w.pm.reread_calls
        out = b.consume_one(facts["event_id"], prior_material_sha256="c" * 64)
        self.assertEqual((out.disposition, out.reason), ("NO_MATERIAL_DELTA", "CURRENT_OWNER_HAS_NO_NEW_MATERIAL"))
        self.assertEqual(w.pm.reread_calls, before + 1)
        self.assertEqual(out.record.owner_material_sha256, "c" * 64)

    def test_same_with_material_not_ready(self):
        w = self.world()
        b = self.binding(w)
        facts, _ = self.deliver(w, b)
        w.pm.update_current(facts["referent_id"], ready_state="BIND_AVAILABLE")
        before = w.pm.reread_calls
        out = b.consume_one(facts["event_id"], prior_material_sha256="d" * 64)
        self.assertEqual(out.disposition, "NO_MATERIAL_DELTA")
        self.assertEqual(w.pm.reread_calls, before + 1)


# ---------------------------------------------------------------------------------------------- D
class D_RepeatsAndCollisions(Base):
    def test_repeats_before_and_after_disposition_across_new_instances(self):
        w = self.world()
        b = self.binding(w)
        facts, _ = self.deliver(w, b)
        frame = w.broker.published[0][1]
        again = b.send_hint(RECEIPT)                            # same event: durable ledger refuses a 2nd publish
        self.assertEqual((again.state, again.replay_refused), ("TRANSPORT_ACCEPTED", True))
        self.assertEqual(len(w.broker.published), 1)
        w.broker.inject(POINTER_SUBJECT, frame)                 # same frame again on the same receiver
        self.assertTrue(b.wait_for_ingress(2))
        self.assertEqual(b.ingress_results()[1].disposition, "DUPLICATE")
        # a NEW composition instance (fresh cursor/ledger) against the same provider storage, before disposition
        w2 = w.fork()
        b2 = self.binding(w2, send=False)
        w2.broker.inject(POINTER_SUBJECT, frame)
        self.assertTrue(b2.wait_for_ingress(1))
        self.assertEqual((b2.sink.records[0].state, b2.sink.records[0].reason),
                         ("DEPOSITED_READBACK", "DUPLICATE_SAME_EVENT"))
        self.assertEqual(w.store.pointer_appends, 1)
        first = b.consume_one(facts["event_id"])
        self.assertEqual(first.disposition, "MATERIAL_ROUTE")
        # after disposition: another new instance returns the existing disposition without a new decision
        w3 = w.fork()
        b3 = self.binding(w3, send=False)
        reads = w.pm.reread_calls
        repeat = b3.consume_one(facts["event_id"])
        self.assertEqual((repeat.state, repeat.disposition), ("ALREADY_DISPOSED", "MATERIAL_ROUTE"))
        self.assertEqual(w.pm.reread_calls, reads)              # not re-decided from (cached or fresh) owner state
        self.assertEqual(b2.consume_one(facts["event_id"]).state, "ALREADY_DISPOSED")
        self.assertEqual((w.store.pointer_appends, w.store.disposition_appends), (1, 1))

    def _collide(self, w, frame, **fork_over):
        w2 = w.fork(**fork_over)
        b2 = self.binding(w2, send=False)
        w2.broker.inject(POINTER_SUBJECT, frame)
        self.assertTrue(b2.wait_for_ingress(1))
        return b2.sink.records[0] if b2.sink.records else None, b2

    def test_same_event_with_altered_attempt_referent_revision_or_hash_is_collision(self):
        w = self.world()
        b = self.binding(w)
        facts, _ = self.deliver(w, b)
        frame = w.broker.published[0][1]
        appends = w.store.pointer_appends
        # attempt
        rec, _ = self._collide(w, frame, attempt_ref="attempt:other")
        self.assertEqual((rec.state, rec.reason), ("COLLISION", "EVENT_ID_CONTENT_CONFLICT"))
        # referent: the owner really has a second ready hint, but it reuses the event_id
        other = w.pm.seed_hint("pm-receipt:other", n=8)
        rec, _ = self._collide(w, self.frame_for({**other, "event_id": facts["event_id"]}, w))
        self.assertEqual(rec.state, "COLLISION")
        # revision + content hash: the owner moved on and an event_id is reused for the new revision
        w.pm.supersede(facts["referent_id"], "rev:7b", owner_seq=7)
        moved = {**facts, "revision": "rev:7b", "snapshot_sha256": "e" * 64}
        rec, _ = self._collide(w, self.frame_for(moved, w))
        self.assertEqual(rec.state, "COLLISION")
        self.assertEqual(w.store.pointer_appends, appends)       # exact prior readback decides; no append attempted
        self.assertEqual(len(w.store.pointers), 1)
        self.assertEqual(w.store.disposition_appends, 0)


# ---------------------------------------------------------------------------------------------- E
class E_FailClosed(Base):
    def assertNoSuccess(self, w, out=None):
        if out is not None:
            self.assertNotIn(out.disposition, ("STALE", "NO_MATERIAL_DELTA", "MATERIAL_ROUTE"))
            self.assertIn(out.state, (x9.HOLD, x9.COLLISION, x9.REFINE))
        self.assertEqual(len(w.store.dispositions), 0)

    def test_wrong_principal_or_acl(self):
        w = self.world()
        b = self.binding(w)
        w.producer_channel.authenticated = False
        facts, _ = self.deliver(w, b)
        self.assertEqual(b.sink.records[0].state, x9.REFINE)
        self.assertEqual(w.store.pointer_appends, 0)
        w2 = self.world()
        b2 = self.binding(w2)
        facts, _ = self.deliver(w2, b2)
        w2.x9_channel.principal = "x9:intruder"
        self.assertNoSuccess(w2, b2.consume_one(facts["event_id"]))
        w2.x9_channel.principal = w2.settings.x9_principal
        w2.x9_channel.append_allowed = False
        self.assertNoSuccess(w2, b2.consume_one(facts["event_id"]))

    def test_provider_acl_refuses_producer_writing_a_disposition(self):
        w = self.world()
        rec = x9.X9Disposition(x9.DISPOSITION_SCHEMA, "pm-event:x", "attempt:x", "a" * 64, "rev:1", "b" * 64,
                               "MATERIAL_ROUTE", "FORGED")
        self.assertEqual(w.producer_channel.append_disposition_once(rec), "NOT_SENT")
        self.assertEqual(len(w.store.dispositions), 0)

    def test_wrong_owner_at_send_receive_and_consume(self):
        w = self.world()
        b = self.binding(w)
        w.pm.seed_hint(RECEIPT)
        w.pm.wrong_owner = True
        out = b.send_hint(RECEIPT)
        self.assertEqual((out.state, out.reason), ("PM_EVIDENCE_REJECTED", "PM_OWNER_BINDING_MISMATCH"))
        self.assertEqual(len(w.broker.published), 0)
        w.pm.wrong_owner = False
        facts, _ = self.deliver(w, b, receipt="pm-receipt:b2", n=8)
        w.pm.wrong_owner = True
        out = b.consume_one(facts["event_id"])
        self.assertNoSuccess(w, out)
        self.assertEqual(out.bridge_reason, "PM_REREAD_IDENTITY_MISMATCH")

    def test_forged_auth_and_readback_flags_in_an_incoming_d0_never_reach_the_owner(self):
        w = self.world()
        b = self.binding(w)
        facts = w.pm.seed_hint(RECEIPT)
        frame = json.loads(self.frame_for(facts, w))
        frame["d0"]["authenticated"] = True
        frame["d0"]["readback_verified"] = True
        w.broker.inject(POINTER_SUBJECT, json.dumps(frame, separators=(",", ":"), sort_keys=True).encode())
        self.assertTrue(b.wait_for_ingress(1))
        self.assertEqual(b.ingress_results()[0].disposition, "HOLD_SCHEMA")
        self.assertEqual((b.resolver.calls, w.store.pointer_appends), (0, 0))

    def test_forged_committed_frame_for_an_unknown_referent_holds(self):
        w = self.world()
        b = self.binding(w)
        facts = {"event_id": "pm-event:forged", "referent_id": "pm-ready:never-issued", "owner_seq": 3,
                 "revision": "rev:3", "revision_before": None, "snapshot_ref": "pm-snapshot:forged",
                 "snapshot_sha256": "9" * 64, "receipt_ref": "pm-receipt:forged"}
        w.broker.inject(POINTER_SUBJECT, self.frame_for(facts, w))
        self.assertTrue(b.wait_for_ingress(1))
        self.assertEqual(b.ingress_results()[0].disposition, "HOLD_UNREADABLE")
        self.assertEqual(b.resolver.notes["pm-event:forged"], "PM_OWNER_PORT_UNAVAILABLE")
        self.assertEqual(w.store.pointer_appends, 0)

    def test_split_snapshot_revision(self):
        w = self.world()
        b = self.binding(w)
        w.pm.seed_hint(RECEIPT)
        w.pm.split_snapshot = True
        self.assertEqual(b.send_hint(RECEIPT).reason, "PM_OWNER_JOIN_REVISION_MISMATCH")
        self.assertEqual(len(w.broker.published), 0)
        w.pm.split_snapshot = False
        facts, _ = self.deliver(w, b, receipt="pm-receipt:e2", n=8)
        w.pm.split_snapshot = True
        out = b.consume_one(facts["event_id"])
        self.assertNoSuccess(w, out)
        self.assertEqual(out.bridge_reason, "PM_REREAD_SPLIT_SNAPSHOT")

    def test_changed_packet_hash_at_receive_and_consume(self):
        w = self.world()
        b = self.binding(w)
        facts, _ = self.deliver(w, b)
        w.pm.update_current(facts["referent_id"], packet_sha256="d" * 64)
        out = b.consume_one(facts["event_id"])
        self.assertEqual((out.state, out.reason), (x9.COLLISION, "PM_SAME_REVISION_CONTENT_CONFLICT"))
        self.assertNoSuccess(w, out)
        w2 = self.world()
        b2 = self.binding(w2)
        w2.broker.hold = True
        facts2 = w2.pm.seed_hint(RECEIPT)
        b2.send_hint(RECEIPT)
        w2.pm.update_current(facts2["referent_id"], packet_sha256="d" * 64)
        w2.broker.release()
        self.assertTrue(b2.wait_for_ingress(1))
        self.assertEqual(b2.resolver.notes[facts2["event_id"]], "PM_PACKET_CONTENT_CHANGED_AT_SAME_REVISION")
        self.assertEqual(w2.store.pointer_appends, 0)

    def test_pm_ready_referent_versus_claim_referent_compatibility_extension(self):
        w = self.world()
        b = self.binding(w, listen=False, send=False)
        s = w.settings
        now = w.clock()
        ctx = x9.PointerContext(
            event_id="pm-event:compat", attempt_id=s.attempt_ref, owner_ref=s.owner_ref, referent_type="PM_READY_HINT",
            revision="rev:1", expected_sha256="1" * 64, claim_ref=s.claim_ref, packet_ref=s.packet_ref,
            packet_sha256=s.packet_sha256, queue_ref=s.queue_ref, producer_id=s.producer_principal,
            receiver_ref=s.x9_session_ref, source_cut="pm-cut:1", expires_at="2026-10-05T13:00:00+00:00",
            way_home=("pm-snapshot:1",), referent_id="pm-ready:compat", owner_seq=1)

        def closure(rid):
            return make_closure(event_id=ctx.event_id, owner_ref=ctx.owner_ref, referent_type=ctx.referent_type,
                                referent_id=rid, revision_after=ctx.revision, disposition="ACK_READ",
                                reason="OWNER_READ_MATCHES_EVENT", observed_sha256=ctx.expected_sha256,
                                activation=decide_activation("ACK_READ", "SEMANTIC"), way_home=ctx.way_home)
        # relabelling the event as the claim does NOT satisfy the check once the PM referent is bound
        self.assertEqual(b.producer_ingress.deposit(closure(s.claim_ref), ctx, now=now).reason,
                         "CLOSURE_D0_BINDING_UNPROVEN")
        self.assertEqual(b.producer_ingress.deposit(closure("pm-ready:other"), ctx, now=now).reason,
                         "CLOSURE_D0_BINDING_UNPROVEN")
        self.assertEqual(w.store.pointer_appends, 0)
        ok = b.producer_ingress.deposit(closure("pm-ready:compat"), ctx, now=now)
        self.assertEqual(ok.state, "DEPOSITED_READBACK")
        stored = w.store.read("pointers", ctx.event_id).record
        self.assertEqual((stored.referent_id, stored.claim_ref, stored.owner_seq),
                         ("pm-ready:compat", s.claim_ref, 1))
        # malformed typed extension fields are an invalid context
        self.assertEqual(b.producer_ingress.deposit(closure("pm-ready:compat"), replace(ctx, owner_seq=0), now=now).reason,
                         "INVALID_POINTER_CONTEXT")
        self.assertEqual(b.producer_ingress.deposit(closure("pm-ready:compat"), replace(ctx, referent_id="bad id"),
                                                    now=now).reason, "INVALID_POINTER_CONTEXT")

    def test_legacy_pointer_without_pm_referent_cannot_be_consumed_through_the_hint_reader(self):
        w = self.world()
        b = self.binding(w, listen=False, send=False)
        s = w.settings
        legacy = x9.PointerRecord(x9.SCHEMA, "c" * 32, "pm-event:legacy", s.attempt_ref, s.owner_ref, "PM_READY_HINT",
                                  "rev:1", "1" * 64, s.claim_ref, s.packet_ref, s.packet_sha256, s.queue_ref,
                                  s.producer_principal, s.x9_session_ref, "pm-cut:1", "2026-10-05T13:00:00+00:00",
                                  ("pm-snapshot:1",))
        self.assertEqual(w.store.append_pointer(s.producer_principal, legacy), "ACCEPTED")
        out = b.consume_one("pm-event:legacy")
        self.assertEqual((out.state, out.bridge_reason), (x9.HOLD, "POINTER_LACKS_PM_REFERENT_BINDING"))
        self.assertNoSuccess(w, out)

    def test_malformed_projection_and_wrong_coordinates(self):
        cases = {"drop_projection": "PM_REREAD_PROJECTION_MISSING", "wrong_claim": "PM_REREAD_COORDINATE_MISMATCH"}
        for flag, code in cases.items():
            with self.subTest(flag=flag):
                w = self.world()
                b = self.binding(w)
                facts, _ = self.deliver(w, b)
                setattr(w.pm, flag, True)
                out = b.consume_one(facts["event_id"])
                self.assertEqual(out.bridge_reason, code)
                self.assertNoSuccess(w, out)
        w = self.world()
        b = self.binding(w)
        facts, _ = self.deliver(w, b)
        w.pm.update_current(facts["referent_id"], ready_state="SOMETHING_ELSE")
        self.assertEqual(b.consume_one(facts["event_id"]).bridge_reason, "PM_REREAD_PROJECTION_INVALID")

    def test_unknown_relation_at_receive_and_consume(self):
        w = self.world()
        b = self.binding(w)
        facts, _ = self.deliver(w, b)
        w.pm.force_relation = "UNKNOWN"
        out = b.consume_one(facts["event_id"])
        self.assertEqual((out.state, out.reason), (x9.HOLD, "PM_REVISION_RELATION_UNKNOWN"))
        self.assertNoSuccess(w, out)
        w2 = self.world()
        b2 = self.binding(w2)
        w2.broker.hold = True
        w2.pm.seed_hint(RECEIPT)
        b2.send_hint(RECEIPT)
        w2.pm.force_relation = "UNKNOWN"
        w2.broker.release()
        self.assertTrue(b2.wait_for_ingress(1))
        self.assertEqual(b2.ingress_results()[0].disposition, "HOLD_UNREADABLE")
        self.assertEqual(w2.store.pointer_appends, 0)

    def test_settings_refuse_attestation_flags_and_inline_secrets(self):
        base = vars(syn.synthetic_settings()).copy()
        for bad in ({"authenticated": True}, {"committed_readback": True}, {"api_token": "x"}):
            with self.subTest(bad=bad), self.assertRaises(pm_x9.PmX9ConfigError):
                pm_x9.parse_settings({**base, **bad})
        with self.assertRaises(pm_x9.PmX9ConfigError):
            pm_x9.parse_settings({**base, "x9_principal": base["producer_principal"]})


# ---------------------------------------------------------------------------------------------- F
class F_Uncertainty(Base):
    def test_pm_timeout_or_offline_at_send_and_consume(self):
        w = self.world()
        b = self.binding(w)
        w.pm.seed_hint(RECEIPT)
        for flag in ("timeout", "offline"):
            setattr(w.pm, flag, True)
            out = b.send_hint(RECEIPT)
            self.assertEqual((out.state, out.reason), ("PM_EVIDENCE_REJECTED", "PM_OWNER_PORT_UNAVAILABLE"))
            setattr(w.pm, flag, False)
        self.assertEqual(len(w.broker.published), 0)
        facts, _ = self.deliver(w, b)
        w.pm.timeout = True
        out = b.consume_one(facts["event_id"])
        self.assertEqual((out.state, out.reason, out.bridge_reason),
                         (x9.HOLD, "PM_OWNER_REREAD_UNAVAILABLE", "PM_OWNER_PORT_UNAVAILABLE"))
        self.assertEqual(w.store.disposition_appends, 0)
        w.pm.timeout = False
        self.assertEqual(b.consume_one(facts["event_id"]).disposition, "MATERIAL_ROUTE")
        self.assertEqual(w.store.disposition_appends, 1)

    def test_pm_offline_at_receive_time_holds_without_deposit(self):
        w = self.world()
        b = self.binding(w)
        w.broker.hold = True
        w.pm.seed_hint(RECEIPT)
        b.send_hint(RECEIPT)
        w.pm.offline = True
        w.broker.release()
        self.assertTrue(b.wait_for_ingress(1))
        self.assertEqual(b.ingress_results()[0].disposition, "HOLD_UNREADABLE")
        self.assertEqual(w.store.pointer_appends, 0)

    def test_channel_append_uncertainty_reconciles_once_by_exact_readback(self):
        w = self.world()
        w.store.pointer_mode = "raise_written"
        w.store.disposition_mode = "raise_written"
        b = self.binding(w)
        facts, _ = self.deliver(w, b)
        self.assertEqual((b.sink.records[0].state, b.sink.records[0].reason), ("DEPOSITED_READBACK", "EXACT_EVENT_READBACK"))
        self.assertEqual(b.consume_one(facts["event_id"]).state, "DISPOSITION_READBACK")
        self.assertEqual((w.store.pointer_appends, w.store.disposition_appends), (1, 1))

    def test_absent_readback_stays_hold_with_no_second_append(self):
        w = self.world()
        w.store.pointer_mode = "unknown_lost"
        b = self.binding(w)
        seen = []
        real = b.producer_ingress.deposit

        def spy(closure, context, *, now):
            seen.append((closure, context, now))
            return real(closure, context, now=now)
        b.producer_ingress.deposit = spy
        facts, _ = self.deliver(w, b)
        self.assertEqual((b.sink.records[0].state, b.sink.records[0].reason), (x9.HOLD, "UNKNOWN_SEND_NO_REPLAY"))
        closure, ctx, now = seen[0]
        again = real(closure, ctx, now=now)                       # exact same event/content offered again
        self.assertEqual((again.state, again.reason), (x9.HOLD, "UNKNOWN_SEND_NO_REPLAY"))
        self.assertEqual(w.store.pointer_appends, 1)
        # disposition side
        w2 = self.world()
        w2.store.disposition_mode = "unknown_lost"
        b2 = self.binding(w2)
        facts2, _ = self.deliver(w2, b2)
        self.assertEqual(b2.consume_one(facts2["event_id"]).reason, "DISPOSITION_UNKNOWN_SEND_NO_REPLAY")
        self.assertEqual(b2.consume_one(facts2["event_id"]).reason, "DISPOSITION_UNKNOWN_SEND_NO_REPLAY")
        self.assertEqual(w2.store.disposition_appends, 1)

    def test_mismatched_readback_is_collision_not_delivery(self):
        w = self.world()
        w.store.corrupt_pointer_readback = True
        b = self.binding(w)
        self.deliver(w, b)
        self.assertEqual(b.sink.records[0].state, x9.COLLISION)
        self.assertEqual(w.store.pointer_appends, 1)

    def test_nats_unknown_send_is_never_republished(self):
        w = self.world()
        b = self.binding(w)
        w.broker.script = ["write_then_drop"]
        w.pm.seed_hint(RECEIPT)
        first = b.send_hint(RECEIPT)
        self.assertEqual(first.state, "UNKNOWN_SEND")
        second = b.send_hint(RECEIPT)
        self.assertEqual((second.state, second.replay_refused), ("UNKNOWN_SEND", True))
        self.assertEqual(len(w.broker.published), 1)


# ---------------------------------------------------------------------------------------------- G
class G_LossQueueAndPulse(Base):
    def test_expired_or_lost_d0_reports_owner_pulse_required(self):
        w = self.world()
        b = self.binding(w)
        w.broker.hold = True
        facts = w.pm.seed_hint(RECEIPT)
        b.send_hint(RECEIPT)
        w.clock.advance(60)                                   # beyond the D0 TTL (30 s)
        w.broker.release()
        self.assertTrue(b.wait_for_ingress(1))
        self.assertEqual(b.ingress_results()[0].disposition, "EXPIRED")
        self.assertEqual((b.resolver.calls, w.store.pointer_appends), (0, 0))
        self.assertEqual(b.consume_one(None).reason, "MISSED_D0_OWNER_PULSE_REQUIRED")
        self.assertEqual(b.consume_one(facts["event_id"]).reason, "POINTER_NOT_READBACK")
        lost = self.world()
        bl = self.binding(lost)
        lost.broker.script = ["lose"]
        lost.pm.seed_hint(RECEIPT)
        self.assertEqual(bl.send_hint(RECEIPT).state, "TRANSPORT_ACCEPTED")   # transport evidence only
        time.sleep(0.2)
        self.assertEqual(bl.ingress_results(), [])
        report = bl.pulse(human_conversation_active=False)
        self.assertEqual((report.processed, lost.store.disposition_appends), ((), 0))

    def test_expired_pointer_at_consume(self):
        w = self.world()
        b = self.binding(w)
        facts, _ = self.deliver(w, b)
        w.clock.advance(w.settings.pointer_ttl_seconds + 1)
        self.assertEqual(b.consume_one(facts["event_id"]).reason, "EXPIRED_POINTER_OWNER_PULSE_REQUIRED")
        self.assertEqual(w.store.disposition_appends, 0)

    def test_bounded_receiver_queue_overflow_is_reported_loss(self):
        w = self.world(max_queue=1)
        b = self.binding(w)
        w.broker.hold = True
        for n in (1, 2, 3):
            w.pm.seed_hint(f"pm-receipt:q{n}", n=n)
            self.assertEqual(b.send_hint(f"pm-receipt:q{n}").state, "TRANSPORT_ACCEPTED")
        gate = threading.Event()
        w.pm.gate = gate
        w.broker.release()
        deadline = time.monotonic() + 5
        while b.listener.summary()["frames"]["received"] < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        gate.set()
        frames = b.listener.summary()["frames"]
        self.assertGreaterEqual(frames["dropped_overflow"], 1)
        handled = 3 - frames["dropped_overflow"]
        self.assertTrue(b.wait_for_ingress(handled))
        self.assertEqual(len(b.sink.records), handled)        # dropped frames are never reported delivered or read

    def test_pulse_queue_overflow_and_active_human_defers(self):
        w = self.world(settings=syn.synthetic_settings(max_pending_ingress=1))
        b = self.binding(w)
        f1, _ = self.deliver(w, b, receipt="pm-receipt:p1", n=1)
        f2, _ = self.deliver(w, b, receipt="pm-receipt:p2", n=2)
        self.assertEqual([r.queued_for_pulse for r in b.sink.records], [True, False])
        reads = w.pm.reread_calls
        deferred = b.pulse(human_conversation_active=True)
        self.assertEqual((deferred.deferred, deferred.reason, deferred.pending, deferred.lost_owner_pulse_required),
                         (True, "HUMAN_CONVERSATION_ACTIVE_DEFERRED", 1, 1))
        self.assertEqual((w.pm.reread_calls, w.store.disposition_appends), (reads, 0))   # nothing woken or decided
        done = b.pulse(human_conversation_active=False)
        self.assertEqual([r.event_id for r in done.processed], [f1["event_id"]])
        self.assertEqual(done.processed[0].disposition, "MATERIAL_ROUTE")
        # the overflowed pointer is still in the channel for the ordinary owner pulse to find explicitly
        self.assertEqual(b.consume_one(f2["event_id"]).disposition, "MATERIAL_ROUTE")

    def test_binding_exposes_no_wake_start_or_bind_operation(self):
        public = {n for n in dir(pm_x9.PmX9Binding) if not n.startswith("_")}
        self.assertFalse({n for n in public if any(w in n for w in ("wake", "bind", "schedule", "admit", "claim"))})


# ---------------------------------------------------------------------------------------------- H
class H_FactoryDefaultOffAndModes(Base):
    def test_default_off_fails_before_any_port_action(self):
        settings = pm_x9.parse_settings({})
        self.assertEqual(settings.mode, "OFF")
        w = self.world()
        with self.assertRaises(pm_x9.PmX9Unbound) as cm:
            pm_x9.build_binding(settings, w.ports())
        self.assertEqual(cm.exception.code, "PM_X9_DEFAULT_OFF")
        self.assertEqual((w.pm.read_calls, w.pm.reread_calls, w.store.pointer_appends, len(w.broker.published)),
                         (0, 0, 0, 0))

    def test_production_mode_never_accepts_synthetic_or_missing_ports(self):
        w = self.world()
        prod = replace(w.settings, mode="PRODUCTION")
        with self.assertRaises(pm_x9.PmX9Unbound) as cm:
            pm_x9.build_binding(prod, w.ports())
        statuses = {d["port"]: d["status"] for d in cm.exception.diagnostics}
        self.assertEqual(statuses["pm_port"], "SYNTHETIC_IN_PRODUCTION")
        self.assertEqual(statuses["transport"], "SYNTHETIC_IN_PRODUCTION")
        with self.assertRaises(pm_x9.PmX9Unbound) as cm:
            pm_x9.build_binding(prod, None)
        self.assertTrue(all(d["status"] == "MISSING" for d in cm.exception.diagnostics))
        ports = replace(w.ports(), pm_port=None, connect_fn=None)
        diag = {d["port"]: d["status"] for d in pm_x9.diagnose(prod, ports)}
        self.assertEqual(diag["pm_port"], "MISSING")
        self.assertIn(diag["transport"], ("MISSING", "BOUND"))   # MISSING when nats-py is absent (honest)
        self.assertEqual((w.pm.read_calls, len(w.broker.published)), (0, 0))

    def test_test_mode_accepts_only_labelled_doubles_and_separate_channels(self):
        w = self.world()

        class Unlabelled:
            def read_committed_ready_hint(self, *a, **k): ...
            def reread_ready_hint(self, *a, **k): ...
        with self.assertRaises(pm_x9.PmX9Unbound) as cm:
            pm_x9.build_binding(w.settings, replace(w.ports(), pm_port=Unlabelled()))
        self.assertIn("NOT_SYNTHETIC_IN_TEST_MODE", {d["status"] for d in cm.exception.diagnostics})
        with self.assertRaises(pm_x9.PmX9Unbound) as cm:
            pm_x9.build_binding(w.settings, replace(w.ports(), x9_channel=w.producer_channel))
        self.assertIn("INVALID", {d["status"] for d in cm.exception.diagnostics})

    def _cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = pm_x9_cli.main(list(argv))
        return rc, json.loads(buf.getvalue())

    def test_cli_diagnose_off_unbound_and_production_with_synthetic_factory(self):
        with tempfile.TemporaryDirectory() as td:
            off = Path(td) / "off.toml"
            off.write_text("[pm_x9]\n", encoding="utf-8")
            rc, out = self._cli("diagnose", "--config", str(off))
            self.assertEqual((rc, out["result"]), (3, "PM_X9_DEFAULT_OFF"))
            s = syn.synthetic_settings()
            body = "".join(f'{k} = "{v}"\n' for k, v in vars(s).items()
                           if isinstance(v, str) and k not in ("mode", "ports_factory"))
            prod = Path(td) / "prod.toml"
            prod.write_text('[pm_x9]\nmode = "PRODUCTION"\n' + body, encoding="utf-8")
            rc, out = self._cli("diagnose", "--config", str(prod))
            self.assertEqual((rc, out["result"]), (3, "PM_X9_PORTS_FACTORY_MISSING"))
            prod.write_text('[pm_x9]\nmode = "PRODUCTION"\nports_factory = "signalvev_client.pm_x9_synthetic:make_ports"\n'
                            + body, encoding="utf-8")
            rc, out = self._cli("diagnose", "--config", str(prod))
            self.assertEqual((rc, out["result"]), (3, "PM_X9_PORTS_NOT_BOUND"))
            self.assertIn("SYNTHETIC_IN_PRODUCTION", {d["status"] for d in out["diagnostics"]})
            bad = Path(td) / "bad.toml"
            bad.write_text('[pm_x9]\nmode = "PRODUCTION"\npassword = "x"\n', encoding="utf-8")
            rc, out = self._cli("diagnose", "--config", str(bad))
            self.assertEqual((rc, out["code"]), (2, "INLINE_SECRET_NOT_ALLOWED"))

    def test_cli_selftest_runs_the_same_factory_path(self):
        rc, out = self._cli("selftest")
        self.assertEqual((rc, out["result"], out["label"]), (0, "SELFTEST_PASS_SYNTHETIC_ONLY", "SYNTHETIC_TEST_ONLY"))
        self.assertEqual(out["consume"]["disposition"], "MATERIAL_ROUTE")


class I_InternalIntegration(Base):
    """Only new reconciliation risks; production custody is not simulated as proof."""

    def provider_module(self, name):
        import importlib
        import sys
        root = Path(__file__).resolve().parents[2] / "signalvev-sensing-runtime-v0.1"
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        return importlib.import_module(f"providers.{name}")

    @unittest.skipIf(_support.TARGET == "installed", "source-host PR52 provider is not bundled")
    def test_pr52_reads_canonical_pointer_and_keeps_session_gate(self):
        provider = self.provider_module("x9_session_channel")
        self.assertIs(provider.PointerRecord, x9.PointerRecord)
        self.assertIs(provider.Readback, x9.Readback)
        settings = syn.synthetic_settings()
        w = self.world(settings=settings)
        b = self.binding(w)
        _, sent = self.deliver(w, b)
        readback = w.x9_channel.read_pointer_by_event_id(sent.event_id)

        class Api:
            current = True

            def identity(self):
                return provider.ProviderSessionIdentity(x9.CHANNEL, settings.x9_principal,
                    settings.x9_session_ref, True, self.current, provider.REQUIRED_SCOPES)

            def read_pointer_by_event_id(self, event_id):
                return readback

        api = Api()
        channel = provider.X9SessionChannelPort(api=api, enabled=True)
        self.assertIs(channel.read_pointer_by_event_id(sent.event_id), readback)
        self.assertEqual(channel.append_pointer_once(readback.record), provider.NOT_SENT)
        api.current = False
        self.assertIsNone(channel.read_pointer_by_event_id(sent.event_id))
        self.assertIsNone(provider.X9SessionChannelPort(api=api).read_pointer_by_event_id(sent.event_id))

    @unittest.skipIf(_support.TARGET == "installed", "source-host PR51 provider is not bundled")
    def test_raw_pr51_signature_requires_host_adapter_before_io(self):
        provider = self.provider_module("pm_owner_read")
        w = self.world()
        raw = provider.ServerPmOwnerProvider(owner_ref=w.settings.owner_ref, audience="audience:pm")
        ports = replace(w.ports(), pm_port=raw, pm_reread_port=raw)
        settings = replace(w.settings, mode=pm_x9.MODE_PRODUCTION)
        diagnostics = pm_x9.diagnose(settings, ports)
        for name in ("pm_port", "pm_reread_port"):
            entry = next(d for d in diagnostics if d["port"] == name)
            self.assertEqual(entry["status"], "INVALID")
            self.assertIn("PM_HOST_ADAPTER_REQUIRED", entry["detail"])
        with self.assertRaises(pm_x9.PmX9Unbound):
            pm_x9.build_binding(settings, ports)

    def test_diagnostic_eviction_keeps_totals_waits_and_pending(self):
        from types import SimpleNamespace
        w = self.world()
        b = self.binding(w, listen=False, send=False)
        self.assertTrue(b.inbox.offer("event:pending"))
        total = pm_x9.DIAGNOSTIC_RETENTION * 3
        for i in range(total):
            b._on_result(i)
            b.sink._keep(pm_x9.DepositRecord(f"event:{i}", "NOT_DEPOSITED", "TEST", "HOLD"))
            with self.assertRaises(pm_x9.ResolverUnavailable):
                b.resolver.resolve(SimpleNamespace(owner_ref="wrong", event_id=f"event:{i}"))
        self.assertEqual(b.ingress_results(), list(range(total - pm_x9.DIAGNOSTIC_RETENTION, total)))
        self.assertEqual(len(b.sink.records), pm_x9.DIAGNOSTIC_RETENTION)
        self.assertEqual(len(b.resolver.thread_ids), pm_x9.DIAGNOSTIC_RETENTION)
        self.assertEqual(len(b.resolver.notes), pm_x9.DIAGNOSTIC_RETENTION)
        self.assertEqual((b.ingress_count, b.sink.total_records, b.resolver.calls), (total, total, total))
        self.assertTrue(b.wait_for_ingress(total, timeout=0))
        self.assertFalse(b.wait_for_ingress(total + 1, timeout=0))
        self.assertEqual(b.inbox.snapshot(), ["event:pending"])
        self.assertEqual(b.status()["deposits"], total)

    def test_current_owner_active_hold_reaches_p22_disposition_policy(self):
        for valid, due in ((True, True), (True, False), (False, True)):
            with self.subTest(valid=valid, due=due):
                w = self.world()
                b = self.binding(w)
                facts, sent = self.deliver(w, b)
                hold = {"blocked_edge": "edge:blocked", "first_observed_at": "2026-10-05T14:00:00Z",
                        "previous_first_observed_at": "2026-10-05T14:00:00Z",
                        "owner_ref": w.settings.owner_ref if valid else "owner:other",
                        "next_action": "RETRY_EXISTING_OWNER_EDGE", "next_check": "check:next",
                        "escalation_to": "P22", "next_check_due": due, "orphaned": False}
                w.pm.update_current(facts["referent_id"], active_hold=hold, ready_state="BIND_AVAILABLE")
                result = b.consume_one(sent.event_id,
                    prior_material_sha256=w.pm.current[facts["referent_id"]]["material_sha256"])
                if not valid:
                    self.assertEqual((result.state, result.reason), (x9.HOLD, "PM_ACTIVE_HOLD_BINDING_INVALID"))
                elif due:
                    self.assertEqual(result.disposition, x9.MATERIAL)
                    self.assertEqual(result.record.reason, "ACTIVE_HOLD_ACTION_OR_ESCALATION_DUE")
                else:
                    self.assertEqual(result.disposition, x9.NO_DELTA)


class J_PendingContinuity(Base):
    """C1151 named transient/exception correction only, no live port proof."""

    def test_pm_unavailable_then_restore_next_pulse_acks_once_without_replay(self):
        w = self.world()
        b = self.binding(w)
        facts, sent = self.deliver(w, b)
        w.pm.offline = True
        first = b.pulse(human_conversation_active=False)
        self.assertEqual((first.processed[0].state, first.pending, first.lost_owner_pulse_required),
                         (x9.HOLD, 1, 0))
        self.assertEqual(first.reason, "PULSE_PENDING_RECONCILIATION")
        self.assertEqual(b.inbox.snapshot(), [sent.event_id])
        self.assertEqual(w.store.disposition_appends, 0)
        w.pm.offline = False
        second = b.pulse(human_conversation_active=False)
        self.assertEqual((second.processed[0].state, second.pending), ("DISPOSITION_READBACK", 0))
        self.assertEqual(second.processed[0].event_id, facts["event_id"])
        self.assertEqual((len(w.broker.published), w.store.pointer_appends, w.store.disposition_appends), (1, 1, 1))
        self.assertEqual(b.pulse(human_conversation_active=False).processed, ())
        self.assertEqual(w.store.disposition_appends, 1)

    def test_consume_exception_preserves_current_and_unprocessed_tail(self):
        from unittest.mock import patch
        w = self.world()
        b = self.binding(w)
        _, first = self.deliver(w, b, receipt="pm-receipt:tail1", n=1)
        _, second = self.deliver(w, b, receipt="pm-receipt:tail2", n=2)
        with patch.object(b, "consume_one", side_effect=RuntimeError("injected-consume")) as consume:
            with self.assertRaisesRegex(RuntimeError, "injected-consume"):
                b.pulse(human_conversation_active=False)
            consume.assert_called_once()
        self.assertEqual(b.inbox.snapshot(), [first.event_id, second.event_id])
        self.assertEqual((w.store.disposition_appends, b.inbox.lost_owner_pulse_required), (0, 0))
        resumed = b.pulse(human_conversation_active=False)
        self.assertEqual([r.event_id for r in resumed.processed], [first.event_id, second.event_id])
        self.assertEqual([r.state for r in resumed.processed], ["DISPOSITION_READBACK"] * 2)
        self.assertEqual((resumed.pending, w.store.disposition_appends, len(w.broker.published)), (0, 2, 2))

    def test_ambiguous_disposition_stays_pending_until_exact_late_readback(self):
        from unittest.mock import patch
        w = self.world()
        b = self.binding(w)
        _, sent = self.deliver(w, b)
        w.store.disposition_mode = "unknown_lost"
        with patch.object(w.x9_channel, "append_disposition_once",
                          wraps=w.x9_channel.append_disposition_once) as append:
            first = b.pulse(human_conversation_active=False)
            attempted = append.call_args.args[0]
        self.assertEqual((first.processed[0].reason, first.pending),
                         ("DISPOSITION_UNKNOWN_SEND_NO_REPLAY", 1))
        w.store.disposition_mode = "ok"
        second = b.pulse(human_conversation_active=False)
        self.assertEqual((second.processed[0].reason, second.pending, w.store.disposition_appends),
                         ("DISPOSITION_UNKNOWN_SEND_NO_REPLAY", 1, 1))
        # The provider exposes a late exact receipt; this is a test observation,
        # not a second client append or publication.
        w.store.dispositions[sent.event_id] = (attempted, w.settings.x9_principal, "synthetic-channel:late1")
        third = b.pulse(human_conversation_active=False)
        self.assertEqual((third.processed[0].state, third.pending), ("ALREADY_DISPOSED", 0))
        self.assertEqual((w.store.disposition_appends, len(w.broker.published)), (1, 1))


if __name__ == "__main__":
    unittest.main()
