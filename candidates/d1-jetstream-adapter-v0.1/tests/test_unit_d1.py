"""Unit tests (no broker): pointer/config contracts and the composition semantics over the SYNTHETIC in-memory
transport. These are NOT broker evidence; real JetStream behaviour is exercised by fixture/run_jetstream_tests.py."""
from __future__ import annotations

import json
import unittest
from dataclasses import replace

from _support import FakeClock, TmpCase, reference_roots, unit_config
from d1js.composition import END_TO_END_UNPROVEN, RECEIVER, D1Fixture
from d1js.config import D1Config, D1ConfigError
from d1js.faults import Faults
from d1js.pointer import PointerReject, build_pointer, canonical, parse_pointer, sha256_hex
from d1js.reference_root import (GATEWAY_PINS, ReferenceRootError, check_project_return_root, load_gateway)
from d1js.transport import InMemoryStreamTransport


class PointerAndConfig(TmpCase):
    def raw(self, **over):
        clock = FakeClock()
        fields = dict(tenant_ref="t", workspace_ref="w", project_ref="p", owner_ref="PROJECT_ENGINE",
                      owner_event_key="evt-1", owner_revision=3, event_fingerprint="a" * 64, referent_type="doc",
                      referent_id="d1", receiver_ref="r1", kind="HINT", created_at=clock(), ttl_seconds=60)
        fields.update(over)
        return build_pointer(**fields)

    def test_roundtrip_identity_and_digest(self):
        raw = self.raw()
        p = parse_pointer(raw, max_ttl_seconds=120)
        self.assertEqual((p.owner_revision, p.raw_sha256, p.receiver_ref), (3, sha256_hex(raw), "r1"))
        self.assertTrue(p.message_id.startswith("d1:"))
        self.assertEqual(parse_pointer(self.raw(), max_ttl_seconds=120).message_id, p.message_id)   # stable id
        self.assertNotEqual(parse_pointer(self.raw(owner_revision=4), max_ttl_seconds=120).message_id, p.message_id)

    def test_strict_rejects(self):
        raw = self.raw()
        doc = json.loads(raw)
        cases = []
        bad = dict(doc); bad["extra"] = 1; cases.append((canonical(bad), "REJECT_SCHEMA"))
        bad = dict(doc); bad["owner_revision"] = True; cases.append((canonical(bad), "REJECT_SCHEMA"))
        bad = dict(doc); bad["class"] = "D0"; cases.append((canonical(bad), "REJECT_SCHEMA"))
        bad = dict(doc); bad["project_ref"] = "p-forged"; cases.append((canonical(bad), "REJECT_DIGEST"))
        cases.append((raw.replace(b'"t"', b'"t", "t":"x"', 1), "REJECT_SCHEMA"))
        cases.append((b"{not json", "REJECT_SCHEMA"))
        bad = dict(doc); bad["message_id"] = "d1:" + "0" * 40
        bad.pop("body_sha256"); bad["body_sha256"] = sha256_hex(canonical({k: v for k, v in bad.items() if k != "body_sha256"}))
        cases.append((canonical(bad), "REJECT_IDENTITY"))
        for data, code in cases:
            with self.subTest(code=code), self.assertRaises(PointerReject) as cm:
                parse_pointer(data, max_ttl_seconds=120)
            self.assertEqual(cm.exception.code, code)
        with self.assertRaises(PointerReject) as cm:
            parse_pointer(raw, max_ttl_seconds=30)
        self.assertEqual(cm.exception.code, "REJECT_TTL_SHAPE")

    def test_config_default_off_and_bounds(self):
        self.assertFalse(D1Config().enabled)
        D1Config().validate()
        for over in ({"subject": "cerebro.test.>"}, {"stream": "OTHER"}, {"max_ack_pending": 2},
                     {"pointer_ttl_s": 121}, {"discard": "old"}, {"pull_batch": 2}, {"duplicate_window_s": 700},
                     {"enabled": True, "server_url": "nats://edge.example:4222"}):
            with self.subTest(over=over), self.assertRaises(D1ConfigError):
                D1Config(**over).validate()

    def test_reference_root_is_hash_pinned(self):
        gw_root, pr_root = reference_roots()
        self.assertTrue(hasattr(load_gateway(gw_root), "D1HybridGateway"))
        check_project_return_root(pr_root)
        bad = self.tmp / "gw"
        bad.mkdir()
        for name in GATEWAY_PINS:
            (bad / name).write_bytes((gw_root / name).read_bytes())
        (bad / "gateway.py").write_bytes((gw_root / "gateway.py").read_bytes() + b"\n# edited\n")
        with self.assertRaises(ReferenceRootError):
            load_gateway(bad)


class Composition(TmpCase):
    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self.faults = Faults()
        self.transport = InMemoryStreamTransport(clock=self.clock.epoch)

    def fixture(self, **over):
        gw_root, _ = reference_roots()
        return D1Fixture(gateway_root=gw_root, state_dir=self.tmp / "state", transport=self.transport,
                         config=unit_config(**over), faults=self.faults, clock=self.clock)

    def publish(self, fx, **kw):
        facts = fx.commit_hint(**kw)
        self.assertEqual(fx.publisher.publish(facts["message_id"]), "PUBLISHED")
        return facts

    def test_default_off_never_connects_or_pulls(self):
        gw_root, _ = reference_roots()
        fx = D1Fixture(gateway_root=gw_root, state_dir=self.tmp / "off", transport=self.transport,
                       config=D1Config(), faults=self.faults, clock=self.clock)
        self.assertIsNone(fx.ensure_info)
        self.assertEqual(fx.receiver.drain_once().reason, "DEFAULT_OFF")

    def test_a_selected_return_then_ack_after_readback(self):
        fx = self.fixture()
        facts = self.publish(fx)
        before = fx.owner.event("evt-syn-1")
        rep = fx.receiver.drain_once()
        first = rep.outcomes[0]
        self.assertEqual(first.gateway.disposition, "SELECTED_RETURN_CLOSED")          # original status retained
        self.assertEqual((first.d1_disposition, first.transit_state, first.unresolved_obligation),
                         ("CONSUMED_SELECTED_RETURN", "ACKED", "SETTLED_FOR_RECEIVER"))
        self.assertTrue(first.delivery_id.startswith("X2_D1_TEST/X2_D1_RECEIVER/receiver-syn-1/s1/c1/d1"))
        self.assertEqual(fx.owner.materializations(), 1)
        self.assertEqual(fx.owner.event("evt-syn-1"), before)                          # owner truth unchanged
        self.assertEqual(fx.reconcile_once(), [])

    def test_b_active_turn_defers_and_lease_loss_mid_processing_noacks(self):
        fx = self.fixture()
        self.publish(fx)
        fx.host.begin_human_turn()
        self.assertEqual(fx.receiver.drain_once().reason, "DEFER_UNSAFE_BOUNDARY")
        fx.host.end_human_turn()
        fx.faults.on("human_starts", fx.host.begin_human_turn)
        fx.faults.set("owner.between_read_and_append", "call:human_starts")   # turn starts mid-processing
        rep = fx.receiver.drain_once()
        self.assertEqual(rep.outcomes[0].gateway.disposition, "HOLD_RETURN_NOT_CLOSED")
        self.assertEqual(fx.owner.materializations(), 0)
        fx.host.end_human_turn()
        self.clock.advance(2)
        rep = fx.receiver.drain_once()
        self.assertEqual(rep.outcomes[0].d1_disposition, "CONSUMED_SELECTED_RETURN")
        self.assertEqual(fx.owner.materializations(), 1)

    def test_e_stale_newer_and_fence(self):
        fx = self.fixture()
        old = self.publish(fx)
        new = self.publish(fx)                       # same event key amended: revision N+1
        rep = fx.receiver.drain_once()
        ds = [o.d1_disposition for o in rep.outcomes if o.d1_disposition]
        self.assertEqual(ds, ["STALE", "CONSUMED_SELECTED_RETURN"])
        self.assertEqual(fx.owner.materializations(), 1)

    def test_e_pre_append_fence_revision_and_same_revision_other_event(self):
        for same in (False, True):
            with self.subTest(same_revision=same):
                td = self.tmp / f"fence-{same}"
                gw_root, _ = reference_roots()
                faults = Faults()
                fx = D1Fixture(gateway_root=gw_root, state_dir=td, transport=InMemoryStreamTransport(clock=self.clock.epoch),
                               config=unit_config(), faults=faults, clock=self.clock)
                facts = fx.commit_hint()
                fx.publisher.publish(facts["message_id"])
                faults.on("move", lambda fx=fx, same=same: fx.owner.touch_head("tenant-syn-1", "workspace-syn-1",
                                                                              "project-syn-1", same_revision=same))
                faults.set("owner.between_read_and_append", "call:move")
                rep = fx.receiver.drain_once()
                o = rep.outcomes[0]
                self.assertEqual((o.gateway.disposition, o.transit_state), ("HOLD_RETURN_NOT_CLOSED", "PENDING_NOACK"))
                self.assertEqual(fx.owner.materializations(), 0)
                self.assertEqual(fx.receiver.return_mapping.last_result["reason"],
                                 "project-owner-head-revision-or-event-changed")

    def test_f_coalescing_only_compatible_hints(self):
        fx = self.fixture()
        for _ in range(3):
            self.publish(fx, kind="HINT")
        rep = fx.receiver.drain_once()
        self.assertEqual([o.d1_disposition for o in rep.outcomes if o.d1_disposition],
                         ["STALE", "STALE", "CONSUMED_SELECTED_RETURN"])
        self.assertEqual(fx.receiver.owner_reader.rereads, 2)        # one saved
        self.assertEqual(fx.journal.counters().get("coalesced"), 1)
        fx2_dir = self.tmp / "nocoalesce"
        gw_root, _ = reference_roots()
        fx2 = D1Fixture(gateway_root=gw_root, state_dir=fx2_dir, transport=InMemoryStreamTransport(clock=self.clock.epoch),
                        config=unit_config(), faults=Faults(), clock=self.clock)
        for _ in range(3):
            f = fx2.commit_hint(kind="DECISION")
            fx2.publisher.publish(f["message_id"])
        rep = fx2.receiver.drain_once()
        self.assertEqual(fx2.receiver.owner_reader.rereads, 3)       # never coalesced
        self.assertIsNone(fx2.journal.counters().get("coalesced"))

    def test_h_journal_or_return_failures_never_ack(self):
        fx = self.fixture()
        self.publish(fx)
        self.faults.set("journal.readback", "raise_once")
        o = fx.receiver.drain_once().outcomes[0]
        self.assertEqual((o.gateway.transit_state, o.transit_state), ("ACK_UNKNOWN", "NOACK_DISPOSITION_UNPROVEN"))
        self.assertTrue(o.unresolved_obligation.startswith("OPEN"))
        self.clock.advance(2)
        o = fx.receiver.drain_once().outcomes[0]
        self.assertEqual((o.d1_disposition, o.transit_state, o.port_event), ("CONSUMED_SELECTED_RETURN", "ACKED", "RECOVERED"))
        self.assertEqual(fx.owner.materializations(), 1)

    def test_h_return_readback_failure_then_recovery_same_receipt(self):
        fx = self.fixture()
        self.publish(fx)
        self.faults.set("return.readback", "raise_once")
        o = fx.receiver.drain_once().outcomes[0]
        self.assertEqual((o.gateway.disposition, o.transit_state), ("HOLD_RETURN_NOT_CLOSED", "PENDING_NOACK"))
        self.assertEqual(fx.owner.materializations(), 1)                 # committed, not confirmed
        self.clock.advance(2)
        o = fx.receiver.drain_once().outcomes[0]
        self.assertEqual(o.d1_disposition, "CONSUMED_SELECTED_RETURN")
        self.assertEqual(fx.owner.materializations(), 1)                 # same receipt, no second materialization

    def test_h_ack_lost_after_send_redelivery_absent_and_no_rematerialization(self):
        fx = self.fixture()
        self.publish(fx)
        self.faults.set("ack.after_send", "raise_once")
        o = fx.receiver.drain_once().outcomes[0]
        self.assertEqual(o.gateway.transit_state, "ACK_UNKNOWN")
        self.clock.advance(2)
        self.assertEqual(fx.receiver.drain_once().reason, "QUIET")
        self.assertEqual(fx.owner.materializations(), 1)

    def test_e_rejects_are_journaled_before_ack_and_obligation_stays_open(self):
        fx = self.fixture()
        f = fx.commit_hint(pointer_overrides={"project_ref": "project-forged"})   # outbox has forged-route bytes
        fx.publisher.publish(f["message_id"])
        o = fx.receiver.drain_once().outcomes[0]
        self.assertEqual(o.gateway.disposition, "HOLD_ROUTE_UNVERIFIED")
        self.assertEqual(o.transit_state, "PENDING_NOACK")
        # expired pointer: disposition + readback before ACK, obligation visible
        fx2 = D1Fixture(gateway_root=reference_roots()[0], state_dir=self.tmp / "exp",
                        transport=InMemoryStreamTransport(clock=self.clock.epoch), config=unit_config(pointer_ttl_s=5),
                        faults=Faults(), clock=self.clock)
        f2 = fx2.commit_hint()
        fx2.publisher.publish(f2["message_id"])
        self.clock.advance(6)
        o = fx2.receiver.drain_once().outcomes[0]
        self.assertEqual((o.d1_disposition, o.transit_state), ("EXPIRED", "ACKED"))
        self.assertIsNotNone(fx2.journal.terminal("X2_D1_TEST", 1))
        self.assertEqual(fx2.reconcile_once()[0]["reason"], "NEGATIVE_DISPOSITION_OBLIGATION_OPEN:EXPIRED")

    def test_e_other_receiver_needs_owner_verified_full_pointer(self):
        fx = self.fixture()
        genuine = fx.commit_hint(owner_event_key="evt-other", receiver_ref="receiver-other")
        fx.publisher.publish(genuine["message_id"])
        forged = build_pointer(tenant_ref="tenant-syn-1", workspace_ref="workspace-syn-1", project_ref="project-syn-1",
                               owner_ref="PROJECT_ENGINE", owner_event_key="evt-other", owner_revision=1,
                               event_fingerprint="b" * 64, referent_type="doc", referent_id="doc-syn-1",
                               receiver_ref="receiver-third", kind="HINT", created_at=self.clock(), ttl_seconds=120)
        fid = parse_pointer(forged, max_ttl_seconds=120).message_id
        self.transport.publish("cerebro.test.d1.pointer", forged, fid, 1.0)    # internally consistent, not owner's
        rep = fx.receiver.drain_once(max_items=2)
        self.assertEqual([o.d1_disposition for o in rep.outcomes], ["NOT_APPLICABLE", "HOLD_ROUTE_UNVERIFIED"])
        self.assertEqual([o.transit_state for o in rep.outcomes], ["ACKED", "PENDING_NOACK"])
        self.assertIsNone(fx.journal.terminal("X2_D1_TEST", 2))

    def test_d_unknown_puback_reconciles_by_lookup_and_conflict_on_changed_bytes(self):
        fx = self.fixture()
        f = fx.commit_hint()
        self.transport.lose_puback = True
        self.assertEqual(fx.publisher.publish(f["message_id"]), "UNKNOWN_PENDING")
        self.transport.lose_puback = False
        self.transport.fail["lookup"] = "raise"
        self.assertEqual(fx.publisher.publish(f["message_id"]), "RECONCILE_HOLD")   # never blind
        del self.transport.fail["lookup"]
        self.assertEqual(fx.publisher.reconcile_publication(f["message_id"]), "PUBLISHED")
        self.assertEqual(len(self.transport.msgs), 1)
        # same id, changed bytes
        self.assertEqual(fx.owner.outbox_add(f["message_id"], "evt-syn-1", 1, RECEIVER, b"{}", "x", "y"), "CONFLICT")

    def test_i_owner_commit_before_enqueue_crash(self):
        fx = self.fixture()
        f = fx.commit_hint()                                    # committed + intent, never published (crash)
        rows = fx.reconcile_once()
        self.assertEqual(rows[0]["reason"], "UNQUEUED_RECONCILED_PUBLISH:PUBLISHED")
        self.assertEqual(fx.end_to_end_claim(), "OWNER_OUTBOX_COVERS_EVERY_COMMITTED_EVENT")
        fx.commit_hint(owner_event_key="evt-no-outbox", with_outbox=False)
        self.assertEqual(fx.end_to_end_claim(), END_TO_END_UNPROVEN)

    def test_g_capacity_rejects_new_and_keeps_old(self):
        fx = self.fixture(max_msgs=1)
        a = self.publish(fx, owner_event_key="evt-a")
        b = fx.commit_hint(owner_event_key="evt-b")
        self.assertEqual(fx.publisher.publish(b["message_id"]), "REJECTED_CAPACITY")
        self.assertEqual(len(self.transport.msgs), 1)
        reasons = {r["owner_event_key"]: r["reason"] for r in fx.reconcile_once()}
        self.assertEqual(reasons["evt-b"], "CAPACITY_REJECTED_OPEN")
        self.assertEqual(reasons["evt-a"], "PENDING_IN_STREAM")

    def test_g_max_delivery_becomes_owner_visible(self):
        fx = self.fixture()
        self.publish(fx)
        self.faults.set("owner.read_current", "raise_always")
        for _ in range(4):
            fx.receiver.drain_once()
            self.clock.advance(2)
        self.faults.clear()
        rows = fx.reconcile_once()
        self.assertEqual(rows[0]["reason"], "MAX_DELIVERY_EXHAUSTED")
        self.assertEqual(fx.owner.materializations(), 0)


if __name__ == "__main__":
    unittest.main()
