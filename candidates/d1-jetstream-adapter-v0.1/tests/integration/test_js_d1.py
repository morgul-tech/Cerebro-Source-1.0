"""REAL-broker integration tests (nats-server + nats-py). Run ONLY through fixture/run_jetstream_tests.py, which owns
the disposable server. Real transport evidence: stream storage, PubAck/Nats-Msg-Id dedupe, explicit ACK/NAK,
redelivery, max-delivery advisory, DiscardNew capacity, MaxAge expiry, server restart on the same file store.
Synthetic semantics: owner/outbox/return ledger, safe-boundary host/lease, receiver journal, fault injection."""
from __future__ import annotations

import json
import subprocess
import sys
import time
import unittest
from pathlib import Path

CANDIDATE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(CANDIDATE))

from d1js.composition import END_TO_END_UNPROVEN, RECEIVER, D1Fixture  # noqa: E402
from d1js.config import D1Config  # noqa: E402
from d1js.faults import CRASH_EXIT_CODE, Faults  # noqa: E402
from d1js.nats_transport import NatsJetStreamTransport  # noqa: E402
from d1js.pointer import canonical, sha256_hex  # noqa: E402
from fixture import runtime  # noqa: E402

FAST = dict(ack_wait_s=1.0, hold_nak_delay_s=0.2, pull_timeout_s=0.5)


def require_runtime() -> None:
    if runtime.BROKER is None:
        raise unittest.SkipTest("UNRUN: real broker only via fixture/run_jetstream_tests.py")


class JsCase(unittest.TestCase):
    overrides: dict = {}

    def setUp(self) -> None:
        require_runtime()
        self.broker = runtime.BROKER
        self.state = Path(runtime.WORK) / "state" / self.id().split(".")[-1]
        self.state.mkdir(parents=True)
        self.cfg_over = {**FAST, **self.overrides}
        self.transport = NatsJetStreamTransport(self.broker.url, name="d1js-test").connect()
        self.addCleanup(self.transport.close)
        self.transport._cfg = D1Config(enabled=True, server_url=self.broker.url, **self.cfg_over).validate()
        self.transport.fixture_reset_stream("X2_D1_TEST")
        self.faults = Faults()
        self.fx = self.make_fixture()

    def make_fixture(self, **over) -> D1Fixture:
        cfg = D1Config(enabled=True, server_url=self.broker.url, **{**self.cfg_over, **over}).validate()
        return D1Fixture(gateway_root=runtime.GATEWAY_ROOT, state_dir=self.state, transport=self.transport,
                         config=cfg, faults=self.faults)

    def child(self, cmd: str, *, faults: dict | None = None, key: str = "evt-syn-1") -> tuple[int, dict]:
        args = [runtime.PYTHON, "-B", "-m", "d1js.child", "--state", str(self.state), "--url", self.broker.url,
                "--gateway-root", str(runtime.GATEWAY_ROOT), "--cmd", cmd, "--config", json.dumps(self.cfg_over),
                "--owner-event-key", key]
        if faults:
            args += ["--faults", json.dumps(faults)]
        p = subprocess.run(args, cwd=str(CANDIDATE), capture_output=True, text=True, timeout=120)
        log = Path(runtime.WORK) / "logs" / "children.log"
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(f"\n### {self.id()} {cmd} faults={faults} rc={p.returncode}\n{p.stdout}{p.stderr}")
        out = json.loads(p.stdout.strip().splitlines()[-1]) if p.returncode == 0 and p.stdout.strip() else {}
        return p.returncode, out

    def publish(self, **kw) -> dict:
        facts = self.fx.commit_hint(**kw)
        self.assertEqual(self.fx.publisher.publish(facts["message_id"]), "PUBLISHED")
        return facts

    def wait(self, seconds: float) -> None:
        time.sleep(seconds)

    def pending(self) -> dict:
        return self.transport.consumer_state()


class A_OwnerPubAckLostWakeDrainAck(JsCase):
    def test_a(self):
        wakes = []

        def lost_wake(message_id, seq):
            wakes.append((message_id, seq))
            raise ConnectionError("synthetic wake lost")
        self.fx.publisher._wake = lost_wake
        facts = self.publish()
        before = self.fx.owner.event("evt-syn-1")
        row = self.fx.owner.outbox(facts["message_id"])[0]
        self.assertEqual((row["state"], row["stream"], row["stream_seq"], row["duplicate"]),
                         ("PUBLISHED", "X2_D1_TEST", 1, 0))                     # PubAck recorded before wake
        self.assertEqual(len(wakes), 1)                                        # wake attempted and lost
        self.assertEqual(self.pending()["num_pending"], 1)                     # ingress kept by the broker
        rep = self.fx.receiver.drain_once()                                    # later safe idle drain
        o = rep.outcomes[0]
        self.assertEqual((o.gateway.disposition, o.d1_disposition, o.transit_state, o.unresolved_obligation),
                         ("SELECTED_RETURN_CLOSED", "CONSUMED_SELECTED_RETURN", "ACKED", "SETTLED_FOR_RECEIVER"))
        self.assertEqual(o.delivery_id, f"X2_D1_TEST/X2_D1_RECEIVER/{RECEIVER}/s1/c1/d1")
        term = self.fx.journal.terminal("X2_D1_TEST", 1)
        self.assertEqual((term["d1_disposition"], term["receipt_ref"], term["pointer_sha256"]),
                         ("CONSUMED_SELECTED_RETURN", o.receipt_ref, sha256_hex(facts["pointer"])))
        st = self.pending()
        self.assertEqual((st["num_pending"], st["num_ack_pending"]), (0, 0))
        self.assertEqual(self.fx.owner.event("evt-syn-1"), before)
        self.assertEqual(self.fx.owner.materializations(), 1)
        self.assertEqual(self.fx.reconcile_once(), [])
        m = self.fx.metrics()
        for key in ("pending_obligations", "oldest_open_obligation_age_s", "redeliveries", "puback_latency_ms",
                    "duplicate_pubacks", "conflict_rejects", "active_turn_deferrals", "drain_latency_ms", "expired",
                    "max_delivery", "capacity_rejects", "owner_reconciliation_misses"):
            self.assertIn(key, m)
        self.assertEqual((m["pending_obligations"], m["puback_latency_ms"]["n"], m["materializations"]), (0, 1, 1))
        self.assertIn("no token", m["unit_note"])
        (Path(runtime.WORK) / "evidence" / "metrics_case_a.json").write_text(json.dumps(m, indent=1, default=str))


class B_SafeBoundary(JsCase):
    def test_b_active_turn_and_race_before_pull(self):
        self.publish()
        self.fx.host.begin_human_turn()
        self.assertEqual(self.fx.receiver.drain_once().reason, "DEFER_UNSAFE_BOUNDARY")
        self.assertEqual(self.pending()["num_pending"], 1)
        self.fx.host.end_human_turn()
        self.fx.host.hooks["before_pull"] = self.fx.host.begin_human_turn      # turn starts right before pull
        rep = self.fx.receiver.drain_once()
        self.assertEqual(rep.outcomes[0].gateway.disposition, "HOLD_PULL_UNAVAILABLE")
        self.assertEqual(self.pending()["num_pending"], 1)                     # nothing pulled
        del self.fx.host.hooks["before_pull"]
        self.broker.restart()                                                  # turn stays active across reconnect
        self.wait(1.5)
        self.assertEqual(self.fx.receiver.drain_once().reason, "DEFER_UNSAFE_BOUNDARY")
        self.fx.host.end_human_turn()
        rep = self.fx.receiver.drain_once()
        self.assertEqual(rep.outcomes[0].d1_disposition, "CONSUMED_SELECTED_RETURN")
        self.assertEqual(self.fx.owner.materializations(), 1)

    def test_b_lease_lost_mid_processing_noack_no_selected_work(self):
        self.publish()
        self.faults.on("human", self.fx.host.begin_human_turn)
        self.faults.set("owner.between_read_and_append", "call:human")
        o = self.fx.receiver.drain_once().outcomes[0]
        self.assertEqual((o.gateway.disposition, o.transit_state), ("HOLD_RETURN_NOT_CLOSED", "PENDING_NOACK"))
        self.assertEqual(self.fx.owner.materializations(), 0)
        self.assertIn("return.insert", self.fx.host.lease_losses)
        self.fx.host.end_human_turn()
        self.wait(0.5)
        rep = self.fx.receiver.drain_once()
        self.assertEqual(rep.outcomes[0].d1_disposition, "CONSUMED_SELECTED_RETURN")
        self.assertEqual(self.fx.owner.materializations(), 1)


class C_RestartAndCrash(JsCase):
    def test_c_broker_and_process_restart_pointer_pending_then_one_materialization(self):
        rc, out = self.child("commit_publish")
        self.assertEqual((rc, list(out["publish"].values())), (0, ["PUBLISHED"]))
        self.broker.stop()
        self.broker.start()                                                    # same port + same file store
        self.wait(1.5)
        self.assertEqual(self.pending()["num_pending"], 1)                     # pointer survived the restart
        rc, out = self.child("drain")
        self.assertEqual(out["drain"]["outcomes"][0]["d1"], "CONSUMED_SELECTED_RETURN")
        rc, out2 = self.child("drain")
        self.assertEqual(out2["drain"]["reason"], "QUIET")
        self.assertEqual(out2["materializations"], 1)

    def test_c_crash_after_append_before_readback(self):
        self.child("commit_publish")
        rc, _ = self.child("drain", faults={"return.after_commit_before_readback": "exit"})
        self.assertEqual(rc, CRASH_EXIT_CODE)
        self.assertEqual(self.fx.owner.materializations(), 1)                 # committed, unconfirmed, un-ACKed
        self.wait(1.5)                                                         # ack_wait -> redelivery
        rc, out = self.child("drain")
        o = out["drain"]["outcomes"][0]
        self.assertEqual((o["d1"], o["num_delivered"], out["materializations"]), ("CONSUMED_SELECTED_RETURN", 2, 1))

    def test_c_crash_after_disposition_before_ack(self):
        self.child("commit_publish")
        rc, _ = self.child("drain", faults={"ack.before_send": "exit"})
        self.assertEqual(rc, CRASH_EXIT_CODE)
        term = self.fx.journal.terminal("X2_D1_TEST", 1)
        self.assertEqual(term["d1_disposition"], "CONSUMED_SELECTED_RETURN")  # disposition durable, ACK not sent
        self.wait(1.5)
        rc, out = self.child("drain")
        o = out["drain"]["outcomes"][0]
        self.assertEqual((o["port_event"], o["d1"], o["receipt_ref"], o["transit"]),
                         ("RECOVERED", "CONSUMED_SELECTED_RETURN", term["receipt_ref"], "ACKED"))
        self.assertEqual(out["materializations"], 1)


class D_PubAckAmbiguity(JsCase):
    def test_d_lost_puback_reconciles_by_lookup_no_blind_publish(self):
        facts = self.fx.commit_hint()
        self.faults.set("publish.after_send_before_puback", "raise_once")
        self.assertEqual(self.fx.publisher.publish(facts["message_id"]), "UNKNOWN_PENDING")
        self.broker.stop()                                                     # lookup unavailable
        self.assertEqual(self.fx.publisher.publish(facts["message_id"]), "RECONCILE_HOLD")
        self.broker.start()
        self.wait(1.5)
        self.assertEqual(self.fx.publisher.reconcile_publication(facts["message_id"]), "PUBLISHED")
        self.assertEqual(self.transport.stream_state()["messages"], 1)         # never a second publish
        self.assertEqual(self.fx.publisher.publish_calls, 1)
        self.assertEqual(self.fx.owner.outbox(facts["message_id"])[0]["last_reason"], "RECONCILED_BY_LOOKUP")

    def test_d_lookup_incomplete_stays_hold(self):
        fx = self.make_fixture(lookup_bound=1)
        for key in ("evt-a", "evt-b"):
            f = fx.commit_hint(owner_event_key=key)
            fx.publisher.publish(f["message_id"])
        f = fx.commit_hint(owner_event_key="evt-c")
        self.faults.set("publish.after_send_before_puback", "raise_once")
        self.assertEqual(fx.publisher.publish(f["message_id"]), "UNKNOWN_PENDING")
        self.assertEqual(fx.publisher.reconcile_publication(f["message_id"]), "RECONCILE_HOLD")

    def test_d_same_id_changed_bytes_is_conflict(self):
        facts = self.fx.commit_hint()
        foreign = facts["pointer"].replace(b'"kind":"HINT"', b'"kind":"DECISION"')
        ack = self.transport.publish("cerebro.test.d1.pointer", foreign, facts["message_id"], 2.0)
        self.assertFalse(ack.duplicate)
        self.assertEqual(self.fx.publisher.publish(facts["message_id"]), "CONFLICT_HOLD")
        self.assertEqual(self.fx.journal.counters().get("conflict_rejects"), 1)

    def test_d_duplicate_after_window_and_live_replay_vs_redelivery(self):
        self.cfg_over["duplicate_window_s"] = 1.0
        self.transport.fixture_reset_stream("X2_D1_TEST")
        fx = self.make_fixture(duplicate_window_s=1.0)
        facts = fx.commit_hint()
        fx.publisher.publish(facts["message_id"])
        dup = self.transport.publish("cerebro.test.d1.pointer", facts["pointer"], facts["message_id"], 2.0)
        self.assertEqual((dup.duplicate, dup.seq), (True, 1))                 # inside the broker window
        self.assertEqual(fx.receiver.drain_once().outcomes[0].d1_disposition, "CONSUMED_SELECTED_RETURN")
        self.wait(1.3)                                                         # broker window expired
        replay = self.transport.publish("cerebro.test.d1.pointer", facts["pointer"], facts["message_id"], 2.0)
        self.assertEqual((replay.duplicate, replay.seq), (False, 2))           # live external replay = new seq
        o = fx.receiver.drain_once().outcomes[0]
        self.assertEqual((o.d1_disposition, o.stream_seq, o.num_delivered), ("CONSUMED_SELECTED_RETURN", 2, 1))
        self.assertEqual(fx.owner.materializations(), 1)                       # owner/return idempotency holds
        seqs = [t["stream_seq"] for t in fx.journal.terminal_for_message(facts["message_id"])]
        self.assertEqual(seqs, [1, 2])


class E_StaleForgedCorruptFence(JsCase):
    def test_e_stale_newer(self):
        self.publish()
        self.publish()                                                         # owner amended: N -> N+1
        rep = self.fx.receiver.drain_once()
        self.assertEqual([o.d1_disposition for o in rep.outcomes if o.d1_disposition],
                         ["STALE", "CONSUMED_SELECTED_RETURN"])
        self.assertEqual(self.fx.journal.terminal("X2_D1_TEST", 1)["d1_disposition"], "STALE")

    def test_e_forged_route_scope_receiver(self):
        f = self.fx.commit_hint(pointer_overrides={"project_ref": "project-forged"})
        self.fx.publisher.publish(f["message_id"])
        o = self.fx.receiver.drain_once().outcomes[0]
        self.assertEqual((o.gateway.disposition, o.transit_state), ("HOLD_ROUTE_UNVERIFIED", "PENDING_NOACK"))
        self.assertIsNone(self.fx.journal.terminal("X2_D1_TEST", 1))         # never retired as NOT_APPLICABLE
        self.transport.fixture_reset_stream("X2_D1_TEST")
        fx = self.make_fixture()
        g = fx.commit_hint(owner_event_key="evt-recv", receiver_ref="receiver-other")
        raw = g["pointer"]
        doc = json.loads(raw)
        doc["receiver_ref"] = "receiver-forged"                                # altered after the owner commit
        doc.pop("body_sha256")
        doc["body_sha256"] = sha256_hex(canonical(doc))
        self.transport.publish("cerebro.test.d1.pointer", canonical(doc), doc["message_id"], 2.0)
        o = fx.receiver.drain_once().outcomes[0]
        self.assertIn(o.d1_disposition, ("REJECT_IDENTITY", "HOLD_ROUTE_UNVERIFIED"))
        self.assertNotEqual(o.d1_disposition, "NOT_APPLICABLE")

    def test_e_other_receiver_owner_verified_vs_forged(self):
        from d1js.pointer import build_pointer, parse_pointer
        g = self.fx.commit_hint(owner_event_key="evt-other", receiver_ref="receiver-other")
        self.fx.publisher.publish(g["message_id"])
        forged = build_pointer(tenant_ref="tenant-syn-1", workspace_ref="workspace-syn-1", project_ref="project-syn-1",
                               owner_ref="PROJECT_ENGINE", owner_event_key="evt-other", owner_revision=1,
                               event_fingerprint="b" * 64, referent_type="doc", referent_id="doc-syn-1",
                               receiver_ref="receiver-third", kind="HINT", created_at=self.fx.clock(), ttl_seconds=120)
        self.transport.publish("cerebro.test.d1.pointer", forged, parse_pointer(forged, max_ttl_seconds=120).message_id,
                               2.0)
        rep = self.fx.receiver.drain_once(max_items=2)
        self.assertEqual([(o.d1_disposition, o.transit_state) for o in rep.outcomes],
                         [("NOT_APPLICABLE", "ACKED"), ("HOLD_ROUTE_UNVERIFIED", "PENDING_NOACK")])

    def test_e_corrupt_digest_schema_and_ttl_rejects_are_journaled_before_ack(self):
        f = self.fx.commit_hint()
        corrupt = f["pointer"].replace(b'"owner_revision":1', b'"owner_revision":7')
        self.transport.publish("cerebro.test.d1.pointer", corrupt, f["message_id"], 2.0)
        self.transport.publish("cerebro.test.d1.pointer", b'{"authenticated":true,"committed":true}', "junk-1", 2.0)
        rep = self.fx.receiver.drain_once()
        ds = [o.d1_disposition for o in rep.outcomes if o.d1_disposition]
        self.assertEqual(ds, ["REJECT_DIGEST", "REJECT_SCHEMA"])
        for seq in (1, 2):
            self.assertIsNotNone(self.fx.journal.terminal("X2_D1_TEST", seq))
        self.assertTrue(all(o.transit_state == "ACKED" for o in rep.outcomes if o.d1_disposition))
        rows = {r["message_id"]: r["reason"] for r in self.fx.reconcile_once(publish_unqueued=False)}
        self.assertEqual(rows[f["message_id"]], "UNQUEUED")                    # the real obligation stays visible
        self.assertEqual(self.fx.metrics()["owner_reconciliation_misses"], 1)  # junk id unknown to the owner

    def test_e_ttl_expired(self):
        fx = self.make_fixture(pointer_ttl_s=1)
        f = fx.commit_hint()
        fx.publisher.publish(f["message_id"])
        self.wait(1.3)
        o = fx.receiver.drain_once().outcomes[0]
        self.assertEqual((o.d1_disposition, o.transit_state), ("EXPIRED", "ACKED"))
        self.assertEqual(fx.reconcile_once()[0]["reason"], "NEGATIVE_DISPOSITION_OBLIGATION_OPEN:EXPIRED")

    def test_e_pre_append_fences(self):
        for same in (False, True):
            with self.subTest(same_revision=same):
                self.transport.fixture_reset_stream("X2_D1_TEST")
                state = self.state / f"fence-{same}"
                fx = D1Fixture(gateway_root=runtime.GATEWAY_ROOT, state_dir=state, transport=self.transport,
                               config=D1Config(enabled=True, server_url=self.broker.url, **self.cfg_over),
                               faults=Faults())
                f = fx.commit_hint()
                fx.publisher.publish(f["message_id"])
                fx.faults.on("move", lambda fx=fx, same=same: fx.owner.touch_head(
                    "tenant-syn-1", "workspace-syn-1", "project-syn-1", same_revision=same))
                fx.faults.set("owner.between_read_and_append", "call:move")
                o = fx.receiver.drain_once().outcomes[0]
                self.assertEqual((o.gateway.disposition, o.transit_state), ("HOLD_RETURN_NOT_CLOSED", "PENDING_NOACK"))
                self.assertEqual(fx.receiver.return_mapping.last_result["reason"],
                                 "project-owner-head-revision-or-event-changed")
                self.assertEqual(fx.owner.materializations(), 0)
                self.assertIsNone(fx.journal.terminal("X2_D1_TEST", 1))


class F_Coalescing(JsCase):
    def test_f(self):
        for _ in range(3):
            self.publish(kind="HINT")
        rep = self.fx.receiver.drain_once()
        self.assertEqual([o.d1_disposition for o in rep.outcomes if o.d1_disposition],
                         ["STALE", "STALE", "CONSUMED_SELECTED_RETURN"])
        self.assertEqual(self.fx.receiver.owner_reader.rereads, 2)
        self.assertEqual([self.fx.journal.terminal("X2_D1_TEST", s)["d1_disposition"] for s in (1, 2, 3)],
                         ["STALE", "STALE", "CONSUMED_SELECTED_RETURN"])               # per-pointer dispositions kept
        self.transport.fixture_reset_stream("X2_D1_TEST")
        fx = D1Fixture(gateway_root=runtime.GATEWAY_ROOT, state_dir=self.state / "decisions", transport=self.transport,
                       config=D1Config(enabled=True, server_url=self.broker.url, **self.cfg_over), faults=Faults())
        for _ in range(3):
            g = fx.commit_hint(kind="DECISION")
            fx.publisher.publish(g["message_id"])
        fx.receiver.drain_once()
        self.assertEqual(fx.receiver.owner_reader.rereads, 3)                          # never coalesced
        self.assertIsNone(fx.journal.counters().get("coalesced"))


class G_CapacityExpiryMaxDelivery(JsCase):
    def test_g_capacity_msgs_bytes_size(self):
        for over, n_ok in (({"max_msgs": 2}, 2), ({"max_bytes": 2000}, 2), ({"max_msg_size": 512}, 0)):
            with self.subTest(over=over):
                self.transport.fixture_reset_stream("X2_D1_TEST")
                fx = D1Fixture(gateway_root=runtime.GATEWAY_ROOT, state_dir=self.state / json.dumps(over)[2:12],
                               transport=self.transport,
                               config=D1Config(enabled=True, server_url=self.broker.url, **{**self.cfg_over, **over}),
                               faults=Faults())
                states = []
                for key in ("evt-1", "evt-2", "evt-3"):
                    f = fx.commit_hint(owner_event_key=key)
                    states.append(fx.publisher.publish(f["message_id"]))
                self.assertEqual(states.count("PUBLISHED"), n_ok)
                self.assertEqual(states[-1], "REJECTED_CAPACITY")
                self.assertEqual(self.transport.stream_state()["messages"], n_ok)        # old items kept
                self.assertEqual(len(fx.owner.outbox()), 3)
                reasons = [r["reason"] for r in fx.reconcile_once()]
                self.assertIn("CAPACITY_REJECTED_OPEN", reasons)

    def test_g_max_age_expiry_while_offline_survives_restart(self):
        self.cfg_over.update(max_age_s=2.0, pointer_ttl_s=2, duplicate_window_s=1.0)
        self.transport.fixture_reset_stream("X2_D1_TEST")
        rc, out = self.child("commit_publish")
        self.assertEqual(list(out["publish"].values()), ["PUBLISHED"])
        self.wait(3.0)                                                         # receiver offline; MaxAge removes bytes
        self.assertEqual(self.transport.stream_state()["messages"], 0)
        rc, out = self.child("reconcile")                                      # new adapter process
        self.assertEqual(out["reconcile"][0]["reason"], "EXPIRED_UNCLOSED")

    def test_g_max_delivery_advisory_and_restart_reconciliation(self):
        self.publish()
        self.faults.set("owner.read_current", "raise_always")
        for _ in range(5):
            self.fx.receiver.drain_once()
            self.wait(0.4)
        self.wait(1.2)
        self.fx.receiver.drain_once()
        self.faults.clear()
        self.assertGreaterEqual(len(self.fx.journal.advisories()), 1)          # real broker advisory, journaled
        rc, out = self.child("reconcile")                                      # restart: durable bookkeeping
        self.assertEqual(out["reconcile"][0]["reason"], "MAX_DELIVERY_EXHAUSTED")
        self.assertEqual(self.fx.owner.materializations(), 0)
        self.assertEqual(self.fx.receiver.drain_once().reason, "QUIET")      # no endless retry


class H_SeamFailures(JsCase):
    def test_h_journal_write_failure_then_resume(self):
        self.publish()
        self.faults.set("journal.write", "raise_once")
        o = self.fx.receiver.drain_once().outcomes[0]
        self.assertEqual(o.transit_state, "NOACK_DISPOSITION_UNPROVEN")
        self.assertNotEqual(o.unresolved_obligation, "SETTLED_FOR_RECEIVER")
        self.wait(1.3)
        o = self.fx.receiver.drain_once().outcomes[0]
        self.assertEqual((o.d1_disposition, o.transit_state, o.num_delivered), ("CONSUMED_SELECTED_RETURN", "ACKED", 2))
        self.assertEqual(self.fx.owner.materializations(), 1)

    def test_h_return_append_failure_and_source_unavailable(self):
        self.publish()
        self.faults.set("return.append", "raise_once")
        self.assertEqual(self.fx.receiver.drain_once(max_items=1).outcomes[0].gateway.disposition,
                         "HOLD_RETURN_NOT_CLOSED")
        self.faults.set("owner.read_current", "raise_once")
        self.wait(0.4)
        self.assertEqual(self.fx.receiver.drain_once(max_items=1).outcomes[0].gateway.disposition,
                         "HOLD_OWNER_CURRENTNESS_UNKNOWN")
        self.wait(0.4)
        o = self.fx.receiver.drain_once(max_items=1).outcomes[0]
        self.assertEqual(o.d1_disposition, "CONSUMED_SELECTED_RETURN")
        self.assertEqual(self.fx.owner.materializations(), 1)

    def test_h_ack_lost(self):
        self.publish()
        self.faults.set("ack.before_send", "raise_once")                       # ACK never reached the broker
        o = self.fx.receiver.drain_once().outcomes[0]
        self.assertEqual((o.gateway.transit_state, o.transit_state), ("ACK_UNKNOWN", "ACK_UNKNOWN"))
        self.wait(1.3)
        o = self.fx.receiver.drain_once().outcomes[0]
        self.assertEqual((o.port_event, o.transit_state, o.num_delivered), ("RECOVERED", "ACKED", 2))
        self.assertEqual(self.fx.owner.materializations(), 1)


class I_OwnerCommitBeforeEnqueue(JsCase):
    def test_i_with_outbox_reconciles(self):
        rc, _ = self.child("commit_publish", faults={"publish.before_send": "exit"})
        self.assertEqual(rc, CRASH_EXIT_CODE)
        self.assertEqual(self.transport.stream_state()["messages"], 0)
        rc, out = self.child("reconcile")
        self.assertEqual(out["reconcile"][0]["reason"], "UNQUEUED_RECONCILED_PUBLISH:PUBLISHED")
        rc, out = self.child("drain")
        self.assertEqual(out["drain"]["outcomes"][0]["d1"], "CONSUMED_SELECTED_RETURN")
        self.assertEqual(self.fx.end_to_end_claim(), "OWNER_OUTBOX_COVERS_EVERY_COMMITTED_EVENT")

    def test_i_without_outbox_reports_unproven(self):
        self.fx.commit_hint(with_outbox=False)                                 # committed, crash before enqueue
        self.assertEqual(self.fx.reconcile_once(), [])                         # nothing the transport can recover
        self.assertEqual(self.transport.stream_state()["messages"], 0)
        self.assertEqual(self.fx.end_to_end_claim(), END_TO_END_UNPROVEN)



# ---------------------------------------------------------------------------------------------------------------------
# BK11 correction r1.1 (X2_RETURN_REVIEW 2026-10-06): publisher crash recovery, bounded lookup, default-off.
class _CountingTransport:
    def __init__(self, inner) -> None:
        self._inner, self.calls = inner, []

    def __getattr__(self, name):
        target = getattr(self._inner, name)
        if not callable(target):
            return target

        def wrapped(*a, **kw):
            self.calls.append(name)
            return target(*a, **kw)
        return wrapped


class J_PublisherCorrection(JsCase):
    overrides = {"duplicate_window_s": 1.0, "publish_timeout_s": 0.5, "claim_stale_s": 3.0}

    def outbox(self) -> dict:
        return self.fx.owner.outbox()[0]

    def test_j1_child_exit_after_broker_store_before_puback_lookup_recovery_after_window(self):
        rc, _ = self.child("commit_publish", faults={"publish.after_send_before_puback": "exit"})
        self.assertEqual(rc, CRASH_EXIT_CODE)                                  # real process death
        st = self.transport.stream_state()
        self.assertEqual((st["messages"], st["last_seq"]), (1, 1))             # the broker stored it
        self.assertEqual((self.outbox()["state"], self.outbox()["attempts"]), ("SEND_CLAIMED", 1))
        self.wait(1.3)                                                         # broker duplicate window passed
        rc, out = self.child("publish")                                        # restarted adapter process
        mid = self.outbox()["message_id"]
        self.assertEqual((rc, out["publish"][mid], out["publish_calls"]), (0, "PUBLISHED", 0))
        self.assertGreaterEqual(out["lookup_calls"], 1)
        st = self.transport.stream_state()
        self.assertEqual((st["messages"], st["last_seq"]), (1, 1))             # no blind second publication
        row = self.outbox()
        self.assertEqual((row["state"], row["stream_seq"], row["last_reason"]), ("PUBLISHED", 1,
                                                                                  "RECONCILED_BY_LOOKUP"))
        rc, out = self.child("drain")
        self.assertEqual(out["drain"]["outcomes"][0]["d1"], "CONSUMED_SELECTED_RETURN")
        self.assertEqual(out["materializations"], 1)

    def test_j2_child_exit_after_claim_before_send_recovers_proven_absence(self):
        rc, _ = self.child("commit_publish", faults={"publish.after_claim_before_send": "exit"})
        self.assertEqual(rc, CRASH_EXIT_CODE)
        self.assertEqual(self.transport.stream_state()["messages"], 0)
        self.assertEqual(self.outbox()["state"], "SEND_CLAIMED")
        rc, out = self.child("reconcile")                                      # claim younger than claim_stale_s
        self.assertEqual((out["reconcile"][0]["reason"], out["publish_calls"]), ("PUBLICATION_SEND_CLAIMED", 0))
        self.assertEqual(self.transport.stream_state()["messages"], 0)
        self.wait(3.2)                                                         # claim now stale
        rc, out = self.child("reconcile")
        self.assertEqual((out["reconcile"][0]["reason"], out["publish_calls"]), ("PUBLICATION_PUBLISHED", 1))
        st = self.transport.stream_state()
        self.assertEqual((st["messages"], st["last_seq"]), (1, 1))
        self.assertEqual((self.outbox()["attempts"], self.outbox()["last_reason"]), (2, "PUBACK"))
        rc, out = self.child("drain")
        self.assertEqual(out["drain"]["outcomes"][0]["d1"], "CONSUMED_SELECTED_RETURN")

    def test_j3_sparse_stream_lookup_is_bounded_and_unresolved(self):
        fx = self.make_fixture(lookup_bound=2)
        for key in ("evt-a", "evt-b", "evt-c", "evt-d"):
            self.assertEqual(fx.publisher.publish(fx.commit_hint(owner_event_key=key)["message_id"]), "PUBLISHED")
        for seq in (2, 3):                                                     # real deletions -> sparse span
            self.assertTrue(self.transport._call(self.transport._js.delete_msg("X2_D1_TEST", seq), 5))
        st = self.transport.stream_state()
        self.assertEqual((st["messages"], st["first_seq"], st["last_seq"]), (2, 1, 4))   # messages <= bound
        f = fx.commit_hint(owner_event_key="evt-x")
        self.faults.set("publish.after_claim_before_send", "raise_once")
        self.assertEqual(fx.publisher.publish(f["message_id"]), "UNKNOWN_PENDING")
        calls = []
        orig = self.transport.get_msg
        self.transport.get_msg = lambda seq: (calls.append(seq), orig(seq))[1]
        try:
            self.assertEqual(fx.publisher.reconcile_publication(f["message_id"]), "RECONCILE_HOLD")
            self.assertEqual(fx.publisher.publish(f["message_id"]), "RECONCILE_HOLD")     # still no retry
        finally:
            del self.transport.get_msg
        self.assertLessEqual(len(calls), 2)
        row = fx.owner.outbox(f["message_id"])[0]
        self.assertIn("incomplete: span=4", row["last_reason"])
        st = self.transport.stream_state()
        self.assertEqual((st["messages"], st["last_seq"]), (2, 4))             # nothing published
        self.assertEqual(fx.publisher.publish_calls, 4)

    def test_j4_default_off_existing_stream_zero_broker_calls(self):
        self.publish()                                                         # real pre-existing stream + message
        before_stream, before_cons = self.transport.stream_state(), self.pending()
        counting = _CountingTransport(self.transport)
        off_state = self.state / "off"
        off = D1Fixture(gateway_root=runtime.GATEWAY_ROOT, state_dir=off_state, transport=counting,
                        config=D1Config(), faults=Faults())
        new = off.commit_hint(owner_event_key="evt-new")["message_id"]
        unk = off.commit_hint(owner_event_key="evt-unk")["message_id"]
        clm = off.commit_hint(owner_event_key="evt-clm")["message_id"]
        off.owner.outbox_update(unk, state="UNKNOWN_PENDING")
        off.owner.outbox_update(clm, state="SEND_CLAIMED", claim_token="x", claimed_at="2026-10-06T00:00:00Z")
        rows_before = off.owner.outbox()
        for mid in (new, unk, clm):
            self.assertEqual(off.publisher.publish(mid), "D1_DISABLED_NO_BROKER_ACTIVITY")
            self.assertEqual(off.publisher.reconcile_publication(mid), "D1_DISABLED_NO_BROKER_ACTIVITY")
        self.assertEqual(len(off.reconcile_once()), 3)
        self.assertEqual(off.metrics()["stream"], None)
        self.assertEqual(off.receiver.drain_once().reason, "DEFAULT_OFF")
        self.assertEqual(counting.calls, [])                                   # zero transport/broker calls
        self.assertEqual(off.owner.outbox(), rows_before)
        self.assertEqual(self.transport.stream_state(), before_stream)
        self.assertEqual(self.pending(), before_cons)
        on = D1Fixture(gateway_root=runtime.GATEWAY_ROOT, state_dir=off_state, transport=self.transport,
                       config=D1Config(enabled=True, server_url=self.broker.url, **self.cfg_over), faults=Faults())
        self.assertEqual(on.publisher.publish(new), "PUBLISHED")              # enabled keeps intended behaviour
        self.assertEqual(self.transport.stream_state()["last_seq"], 2)


if __name__ == "__main__":
    unittest.main()
