"""BK11 durable PM-owner outbox: ONE local integration batch over the ACTUAL selected Context SQL/schema/transaction
path (PostgreSQL 16, RLS, non-superuser runtime role), a real local nats-server JetStream and nats-py, the reused D1
publisher/receiver and the pinned historical gateway. Cases 1-9 of the assignment + SQL privilege/RLS policy.
Deterministic faults are synthetic and labelled; DB and broker round trips are real.
"""
from __future__ import annotations

import base64
import threading
import time
import unittest

import psycopg
import psycopg.types.json

from _owner_support import (PROJECT, RECEIVER_REF, ROUTE_REF, TENANT, WORKSPACE, ContextToolClient,
                            CountingTransport, OwnerCase, install_route)
from d1js.faults import CRASH_EXIT_CODE, Faults
from d1js.internal_binding import (CONTEXT_OWNER_MODE, RECEIVE_ONLY_DENIED, InternalBinding,
                                   InternalBindingRefused, compose_context_publisher, receive_only_publish)
from d1js.owner_protocol import OwnerPortRejected, require_owner_port
from d1js.pointer import message_identity, parse_pointer
from fixture import owner_runtime as rt
from fixture.context_pg import PRINCIPALS, SESSIONS


class C1_CommitCrashRollbackDefaultOff(OwnerCase):
    route_kwargs = None                                         # start with NO installation route (default OFF)

    def test_default_off_rollback_and_crash_after_commit_discovery(self):
        # default OFF: no route -> unchanged commit, no intent
        r0 = self.commit("pm-ready:c1-a", "idem:c1-a")
        install_route(self.pg, enqueue=False, drain=False)
        r1 = self.commit("pm-ready:c1-b", "idem:c1-b")
        self.assertEqual(self.intents(), [])
        install_route(self.pg, enqueue=True, drain=True)
        self.commit("pm-ready:c1-c", "idem:c1-c", ready="BIND_AVAILABLE")      # no material => no obligation
        self.assertEqual(self.intents(), [])
        # rollback: fault after the intent insert, inside the same transaction -> NEITHER commit nor intent
        receipts_before = self.sql("SELECT count(*) FROM cerebro_pm_owner_receipts")[0][0]
        head_before = self.sql("SELECT next_seq FROM cerebro_pm_owner_heads")[0][0]

        def boom():
            raise RuntimeError("SYNTHETIC_FAULT_AFTER_ENQUEUE_BEFORE_COMMIT")
        self.fx.d1.faults["after_enqueue_before_commit"] = boom
        with self.assertRaises(Exception):
            self.commit("pm-ready:c1-d", "idem:c1-d")
        self.fx.d1.faults.clear()
        self.assertEqual(self.sql("SELECT count(*) FROM cerebro_pm_owner_receipts")[0][0], receipts_before)
        self.assertEqual(self.sql("SELECT next_seq FROM cerebro_pm_owner_heads")[0][0], head_before)
        self.assertEqual(self.intents(), [])
        # authenticated owner commit in a CHILD process that dies right after COMMIT (before any enqueue/drain)
        rc, out = self.child({"cmd": "commit_then_die",
                              "commit_args": self.commit_args("pm-ready:c1-e", "idem:c1-e")})
        self.assertEqual(rc, CRASH_EXIT_CODE)
        record = out["record"]
        self.assertEqual(self.stream()["messages"], 0)
        # restart: a NEW publisher composition discovers the exact intent (bounded scoped listing)
        pub, t = self.publisher()
        rows, nxt, drain = pub.port.pending(limit=8)
        self.assertEqual((len(rows), nxt, drain), (1, None, True))
        row = rows[0]
        doc = parse_pointer(row["pointer"], max_ttl_seconds=120)          # the Source strict parser
        self.assertEqual((doc.owner_event_key, doc.owner_revision, doc.event_fingerprint, doc.referent_id,
                          doc.receiver_ref), (record["event_id"], record["owner_seq"], record["snapshot_sha256"],
                                              record["referent_id"], RECEIVER_REF))
        self.assertEqual(row["message_id"], message_identity(TENANT, WORKSPACE, PROJECT, RECEIVER_REF,
                                                             record["event_id"], record["owner_seq"]))
        pm = row["mapping_payload"]["pm"]
        self.assertEqual({k: pm[k] for k in ("event_id", "receipt_ref", "commit_ref", "revision_after", "owner_seq")},
                         {k: record[k] for k in ("event_id", "receipt_ref", "commit_ref", "revision_after",
                                                 "owner_seq")})
        # same idempotency key => same receipt and the same single intent (replay, no second obligation)
        replay = self.fx.invoke("pm", "commit_pm_owner_ready", self.commit_args("pm-ready:c1-e", "idem:c1-e"))
        self.assertTrue(replay["structuredContent"]["replayed"])
        self.assertEqual(replay["structuredContent"]["record"]["receipt_ref"], record["receipt_ref"])
        self.assertEqual(len(self.intents()), 1)
        # semantic conflict under the same key refuses
        with self.assertRaises(Exception):
            self.fx.invoke("pm", "commit_pm_owner_ready",
                           self.commit_args("pm-ready:c1-e", "idem:c1-e", material="changed"))
        self.assertEqual(t.total(), 0)                                   # discovery made zero broker calls
        self.ev.update(r0=r0["receipt_ref"], r1=r1["receipt_ref"], crash_child_rc=rc,
                       discovered=row["message_id"], mapping=row["mapping_payload"])


class C2_ReceiverOfflineReconnect(OwnerCase):
    def test_receiver_offline_then_bounded_reconnect_delivery(self):
        rec = self.commit("pm-ready:c2", "idem:c2")
        self.deposit(rec)
        pub, t = self.publisher()
        res = pub.publish_pending_once()
        self.assertEqual([r["result"] for r in res], ["PUBLISHED"])
        intent = self.intents()[0]
        self.assertEqual((intent[3], intent[4]), ("PUBLISHED", 1))
        self.broker.restart()                                             # receiver offline across a broker restart
        self._reconnect_admin()
        self.assertEqual(self.stream()["messages"], 1)                    # file store survived the restart
        recv = self.receiver("late")
        out = self.drain(recv)
        self.assertEqual(len(out), 1, out)
        o = out[0]
        self.assertEqual((o["gateway"], o["d1"], o["transit"], o["obligation"]),
                         ("SELECTED_RETURN_CLOSED", "CONSUMED_SELECTED_RETURN", "ACKED", "SETTLED_FOR_RECEIVER"))
        disp = self.dispositions()
        self.assertEqual([(d[0], d[1]) for d in disp], [(rec["event_id"], "MATERIAL_ROUTE")])
        got = self.admin_t.get_msg(1, timeout=5)
        row = pub.port.outbox(intent[0])[0]
        self.assertEqual(got[0], row["pointer"])                          # exact canonical bytes delivered
        self.assertEqual(got[1].get("Nats-Msg-Id"), intent[0])
        self.assertEqual(recv._t.consumer_state()["num_ack_pending"], 0)
        self.ev.update(outcome=o, disposition=disp, broker_starts=self.broker.starts)

    def _reconnect_admin(self) -> None:
        from d1js.nats_transport import NatsJetStreamTransport
        self.admin_t.close()
        self.admin_t = NatsJetStreamTransport(self.broker.url, name="owner-test-admin2").connect()
        self.addCleanup(self.admin_t.close)
        self.admin_t._cfg = self.cfg


class C3_PubAckLostBeyondDedupe(OwnerCase):
    cfg_over = {"duplicate_window_s": 1.0}

    def test_stored_but_puback_lost_restart_beyond_window_reconciles_by_lookup(self):
        rec = self.commit("pm-ready:c3", "idem:c3")
        self.deposit(rec)
        rc, _ = self.child({"cmd": "publish", "url": self.broker.url, "config": {**self.cfg_over,
                                                                                  "ack_wait_s": 1.0},
                            "faults": {"publish.after_send_before_puback": "exit"}})
        self.assertEqual(rc, CRASH_EXIT_CODE)
        self.assertEqual(self.intents()[0][3], "SEND_CLAIMED")
        self.assertEqual(self.stream()["messages"], 1)
        time.sleep(1.3)                                                    # beyond the broker duplicate window
        pub, t = self.publisher()
        res = pub.publish_pending_once()
        self.assertEqual([r["result"] for r in res], ["PUBLISHED"])
        self.assertEqual((pub.publisher.publish_calls, t.calls.get("publish", 0)), (0, 0))   # no blind 2nd publish
        self.assertGreaterEqual(pub.publisher.lookup_calls, 1)
        st = self.stream()
        self.assertEqual((st["messages"], st["last_seq"]), (1, 1))
        intent = self.intents()[0]
        self.assertEqual((intent[3], intent[4], intent[10]), ("PUBLISHED", 1, "RECONCILED_BY_LOOKUP"))
        out = self.drain(self.receiver())
        self.assertEqual(out[0]["d1"], "CONSUMED_SELECTED_RETURN")
        self.assertEqual(len(self.dispositions()), 1)
        self.ev.update(child_rc=rc, lookup_calls=pub.publisher.lookup_calls, stream=st)


class C4_DuplicateRedeliveryConcurrentClaim(OwnerCase):
    def test_concurrent_claim_one_send_and_redelivery_one_disposition(self):
        rec = self.commit("pm-ready:c4", "idem:c4")
        self.deposit(rec)
        pubs = [self.publisher(client=ContextToolClient(self.fx, "d1pub")) for _ in range(2)]
        results: list = [None, None]
        barrier = threading.Barrier(2)

        def run(i):
            barrier.wait()
            results[i] = pubs[i][0].publish_pending_once()
        ths = [threading.Thread(target=run, args=(i,)) for i in range(2)]
        for th in ths:
            th.start()
        for th in ths:
            th.join(60)
        sends = sum(p.publisher.publish_calls for p, _ in pubs)
        self.assertEqual(sends, 1, results)
        self.assertEqual(self.stream()["messages"], 1)
        intent = self.intents()[0]
        self.assertEqual((intent[3], intent[5]), ("PUBLISHED", 1))
        # receiver 1: disposition durable + read back, but the ACK never reaches the broker (synthetic fault)
        f1 = Faults({"ack.before_send": "raise_once"})
        out1 = self.drain(self.receiver("r1", faults=f1), max_items=1)
        self.assertEqual(out1[0]["transit"], "ACK_UNKNOWN")
        d_after_first = self.dispositions()
        self.assertEqual(len(d_after_first), 1)
        time.sleep(1.3)                                                     # ack_wait elapses -> redelivery
        # receiver 2: a FRESH receiver journal (no local memory) -> Context returns the same exact disposition
        out2 = self.drain(self.receiver("r2"))
        self.assertEqual((out2[0]["d1"], out2[0]["transit"], out2[0]["num_delivered"]),
                         ("CONSUMED_SELECTED_RETURN", "ACKED", 2))
        self.assertEqual(self.dispositions(), d_after_first)                 # one logical disposition, same row
        # a duplicate publish attempt of the settled intent is a no-op
        again = pubs[0][0].publisher.publish(intent[0])
        self.assertEqual(again, "PUBLISHED")
        self.assertEqual(self.stream()["messages"], 1)
        self.ev.update(results=results, first=out1, second=out2, dispositions=d_after_first)


class C5_FailedDispositionWriteThenRecovery(OwnerCase):
    def test_failed_durable_disposition_write_noack_pending_then_exact_recovery(self):
        rec = self.commit("pm-ready:c5", "idem:c5")
        self.deposit(rec)
        pub, _ = self.publisher()
        pub.publish_pending_once()
        self.sql("REVOKE INSERT ON cerebro_x9_channel_records FROM cerebro_runtime", fetch=False)
        try:
            out = self.drain(self.receiver("r1"), max_items=1)
        finally:
            self.sql("GRANT INSERT ON cerebro_x9_channel_records TO cerebro_runtime", fetch=False)
        self.assertEqual((out[0]["gateway"], out[0]["transit"]), ("HOLD_RETURN_NOT_CLOSED", "PENDING_NOACK"), out)
        self.assertEqual(self.dispositions(), [])
        time.sleep(0.4)
        out2 = self.drain(self.receiver("r1"))
        self.assertEqual((out2[0]["d1"], out2[0]["transit"]), ("CONSUMED_SELECTED_RETURN", "ACKED"), out2)
        self.assertEqual([(d[0], d[1]) for d in self.dispositions()], [(rec["event_id"], "MATERIAL_ROUTE")])
        # readback failure at the client boundary (synthetic): NOACK, nothing ACKed, later exact recovery
        rec2 = self.commit("pm-ready:c5b", "idem:c5b")
        self.deposit(rec2)
        pub.publish_pending_once()
        client = ContextToolClient(self.fx, "x9")
        recv = self.receiver("r3", client=client)
        orig = client.call
        calls = {"n": 0}

        def flaky(tool, args):
            if tool == "consume_x9_pointer":
                calls["n"] += 1
                if calls["n"] == 2:                                          # the independent readback
                    raise ConnectionError("SYNTHETIC_READBACK_UNAVAILABLE")
            return orig(tool, args)
        client.call = flaky
        out3 = self.drain(recv, max_items=1)
        self.assertEqual(out3[0]["transit"], "PENDING_NOACK", out3)
        client.call = orig
        time.sleep(0.4)
        out4 = self.drain(recv)
        self.assertEqual((out4[0]["d1"], out4[0]["transit"]), ("CONSUMED_SELECTED_RETURN", "ACKED"), out4)
        self.assertEqual(len(self.dispositions()), 2)
        self.ev.update(write_failure=out, recovered=out2, readback_failure=out3, readback_recovered=out4)


class C6_StaleAndIdentityRejections(OwnerCase):
    cfg_over = {"max_deliver": 8}
    def test_stale_revision_forged_pointer_revoked_expired_wrong_tenant_and_receiver(self):
        r1 = self.commit("pm-ready:c6", "idem:c6-1")
        self.deposit(r1)
        r2 = self.commit("pm-ready:c6", "idem:c6-2", expected=r1["receipt_ref"], material="newer")
        self.deposit(r2)
        pub, _ = self.publisher()
        self.assertEqual([r["result"] for r in pub.publish_pending_once()], ["PUBLISHED", "PUBLISHED"])
        recv = self.receiver("r1")
        out = self.drain(recv)
        self.assertEqual([(o["gateway"], o["d1"], o["transit"]) for o in out],
                         [("STALE_SUPERSEDED", "STALE", "ACKED"),
                          ("SELECTED_RETURN_CLOSED", "CONSUMED_SELECTED_RETURN", "ACKED")], out)
        self.assertEqual([(d[0], d[1]) for d in self.dispositions()],
                         [(r1["event_id"], "STALE"), (r2["event_id"], "MATERIAL_ROUTE")])
        # forged pointer: strict schema/digest/identity valid, but never owner-committed -> HOLD, nothing disposed
        from d1js.pointer import build_pointer
        from datetime import datetime, timezone
        forged = build_pointer(tenant_ref=TENANT, workspace_ref=WORKSPACE, project_ref=PROJECT,
                               owner_ref="owner:syn-pm", owner_event_key="pm-event:forged", owner_revision=9,
                               event_fingerprint="f" * 64, referent_type="PM_READY_HINT",
                               referent_id="pm-ready:c6", receiver_ref=RECEIVER_REF, kind="HINT",
                               created_at=datetime.now(timezone.utc), ttl_seconds=120)
        fid = message_identity(TENANT, WORKSPACE, PROJECT, RECEIVER_REF, "pm-event:forged", 9)
        self.admin_t.publish(self.cfg.subject, forged, fid, 2.0)              # an unowned injector, not the owner
        out = self.drain(recv, max_items=1)
        self.assertEqual((out[0]["gateway"], out[0]["transit"]), ("HOLD_OWNER_CURRENTNESS_UNKNOWN",
                                                                   "PENDING_NOACK"), out)
        self.assertEqual(len(self.dispositions()), 2)
        # remove the forged message so later items are reachable (fixture-only stream surgery)
        self.admin_t._call(self.admin_t._js.delete_msg(self.cfg.stream, 3), 5)
        # revoked receiver session (persisted tuple advanced) / expired token / wrong tenant -> fail closed
        r3 = self.commit("pm-ready:c6c", "idem:c6-3")
        self.deposit(r3)
        pub.publish_pending_once()
        self.sql("UPDATE cerebro_control_session_bindings SET session_revision=session_revision+1 "
                 "WHERE session_ref=%s", (SESSIONS["x9"],), fetch=False)
        out = self.drain(recv, max_items=1)
        self.assertEqual(out[0]["transit"], "PENDING_NOACK", out)
        self.sql("UPDATE cerebro_control_session_bindings SET session_revision=session_revision-1 "
                 "WHERE session_ref=%s", (SESSIONS["x9"],), fetch=False)
        expired = self.tokens.token(PRINCIPALS["x9"], "project_state:read project_state:transition",
                                    now=time.time() - 3600)
        out_exp = self.drain(self.receiver("rexp", client=ContextToolClient(self.fx, "x9", token=expired)),
                             max_items=1)
        wrong = self.tokens.token(PRINCIPALS["x9"], "project_state:read project_state:transition",
                                  extra={"cerebro_tenant": "tenant:other"})
        time.sleep(0.4)
        out_wrong = self.drain(self.receiver("rwrong", client=ContextToolClient(self.fx, "x9", token=wrong)),
                               max_items=1)
        for o in (out_exp[0], out_wrong[0]):
            self.assertEqual(o["transit"], "PENDING_NOACK", o)
        self.assertEqual(len(self.dispositions()), 2)
        time.sleep(0.4)
        out_ok = self.drain(recv, max_items=1)
        self.assertEqual(out_ok[0]["d1"], "CONSUMED_SELECTED_RETURN", out_ok)
        # Wrong receiver has no X9 disposition; X2 holds and leaves the delivery unacked.
        self.sql("""INSERT INTO cerebro_pm_owner_d1_routes SELECT tenant_ref,workspace_ref,project_ref,owner_ref,
            'route:syn-other',  'x9-receiver:syn-other',delivery_class,subject,stream,x9_config_ref,
            'assignment:other',publisher_principal_ref,publisher_consumer_ref,publisher_session_ref,
            publisher_session_binding_id,pointer_ttl_seconds,binding_revision,true,false,source_ref
            FROM cerebro_pm_owner_d1_routes WHERE route_ref=%s""", (ROUTE_REF,), fetch=False)
        r4 = self.commit("pm-ready:c6d", "idem:c6-4")
        other = self.sql("SELECT message_id, pointer FROM cerebro_pm_owner_d1_outbox WHERE route_ref='route:syn-other'")
        self.assertEqual(len(other), 1)
        self.admin_t.publish(self.cfg.subject, bytes(other[0][1]), other[0][0], 2.0)
        out_na = self.drain(recv, max_items=1)
        self.assertEqual((out_na[0]["d1"], out_na[0]["transit"]),
                         ("HOLD_OWNER_DISPOSITION_UNPROVEN:NO_X9_DISPOSITION_FOR_NOT_APPLICABLE", "PENDING_NOACK"), out_na)
        # another principal cannot use the receiver resolution
        with self.assertRaises(Exception):
            ContextToolClient(self.fx, "other").call("resolve_d1_pointer_v1", {
                "op": "resolve", "message_id": other[0][0], "pointer_sha256": "0" * 64})
        self.ev.update(r1=r1["event_id"], r2=r2["event_id"], r4=r4["event_id"], dispositions=self.dispositions())


class C6b_SameIdentityOtherHash(OwnerCase):
    def test_same_event_and_revision_with_other_fingerprint_holds_and_publisher_conflicts(self):
        from datetime import datetime, timezone
        from d1js.pointer import build_pointer
        rec = self.commit("pm-ready:c6b", "idem:c6b")
        self.deposit(rec)
        row = self.intents()[0]
        forged = build_pointer(tenant_ref=TENANT, workspace_ref=WORKSPACE, project_ref=PROJECT,
                               owner_ref="owner:syn-pm", owner_event_key=rec["event_id"],
                               owner_revision=rec["owner_seq"], event_fingerprint="e" * 64,
                               referent_type="PM_READY_HINT", referent_id=rec["referent_id"],
                               receiver_ref=RECEIVER_REF, kind="HINT", created_at=datetime.now(timezone.utc),
                               ttl_seconds=120)
        self.assertEqual(row[0], message_identity(TENANT, WORKSPACE, PROJECT, RECEIVER_REF, rec["event_id"],
                                                  rec["owner_seq"]))
        self.admin_t.publish(self.cfg.subject, forged, row[0], 2.0)        # same Nats-Msg-Id, other owner hash
        out = self.drain(self.receiver("r1"), max_items=1)
        self.assertEqual(out[0]["transit"], "PENDING_NOACK", out)
        self.assertEqual(self.dispositions(), [])
        pub, t = self.publisher()
        res = pub.publish_pending_once()
        self.assertEqual([r["result"] for r in res], ["CONFLICT_HOLD"], res)  # broker dedupe hides other bytes
        self.assertEqual(self.intents()[0][3], "CONFLICT_HOLD")
        self.assertEqual(self.stream()["messages"], 1)
        self.ev.update(hold=out, publisher=res)


class C7_ReceiveOnlyAndRuntimeReadOnly(OwnerCase):
    def test_receive_only_denies_publish_runtime_readonly_cannot_mutate_or_claim(self):
        recv = self.receiver("ro")
        before = self.stream()
        self.assertEqual(receive_only_publish(recv), RECEIVE_ONLY_DENIED)
        self.assertEqual(recv._t.denied_publishes, 1)
        self.assertEqual(self.stream(), before)
        with self.assertRaises(InternalBindingRefused):
            compose_context_publisher(binding=InternalBinding(enabled=True, owner_mode=CONTEXT_OWNER_MODE,
                                                              authorization_ref="authz:x", role="receiver_only",
                                                              receiver_ref=RECEIVER_REF, tenant_ref=TENANT,
                                                              workspace_ref=WORKSPACE, project_ref=PROJECT),
                                      config=self.cfg, transport=None, client=ContextToolClient(self.fx, "x9"))
        rec = self.commit("pm-ready:c7", "idem:c7")
        mid = self.intents()[0][0]
        # existing local-runtime M2M credential (resolve-only) refused by the unchanged authenticator
        m2m = self.tokens.token(self.fx.runtime_client_id + "@clients", "project_state:read project_state:transition",
                                extra={"azp": self.fx.runtime_client_id, "gty": "client-credentials"})
        for tool, args in (("settle_pm_owner_d1_outbox_v1", {"message_id": mid, "from_state": "INTENDED",
                                                             "from_token": None,
                                                             "fields": {"state": "SEND_CLAIMED"}}),
                           ("read_pm_owner_d1_outbox_v1", {"op": "list_pending"}),
                           ("resolve_d1_pointer_v1", {"op": "resolve", "message_id": mid,
                                                      "pointer_sha256": "0" * 64})):
            res = self.fx.invoke("d1pub", tool, args, token=m2m)
            self.assertTrue(res.get("isError"), (tool, res))
        # read-only scope token for the publisher cannot settle
        ro = self.tokens.token(PRINCIPALS["d1pub"], "project_state:read")
        self.assertTrue(self.fx.invoke("d1pub", "settle_pm_owner_d1_outbox_v1", {
            "message_id": mid, "from_state": "INTENDED", "from_token": None, "fields": {"state": "SEND_CLAIMED"}},
            token=ro).get("isError"))
        # the X9 receiver / PM principals cannot claim the publisher's obligations
        for who in ("x9", "pm"):
            with self.assertRaises(Exception):
                ContextToolClient(self.fx, who).call("settle_pm_owner_d1_outbox_v1", {
                    "message_id": mid, "from_state": "INTENDED", "from_token": None,
                    "fields": {"state": "SEND_CLAIMED", "claim_token": "t", "claimed_at": "x", "attempts": 1}})
        # the dedicated read-only DB role cannot mutate or insert (actual PG privilege denial)
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            self.sql("UPDATE cerebro_pm_owner_d1_outbox SET last_reason='x'", who="readonly", fetch=False)
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            self.sql("DELETE FROM cerebro_pm_owner_d1_outbox", who="readonly", fetch=False)
        self.assertEqual(self.intents()[0][3], "INTENDED")
        self.assertEqual(self.stream(), before)
        self.ev.update(intent=mid, receipt=rec["receipt_ref"])


class C8_MigrationRollbackDefaultOff(OwnerCase):
    def test_migration_repeat_newer_refusal_drift_rollback_retention_default_off(self):
        from control_context_state_postgres import MigrationDriftError, apply_postgres_migrations
        again = apply_postgres_migrations(self.pg.factory("migrator"))
        self.assertEqual(again["applied"], [])
        self.assertIn("0009-pm-owner-d1-outbox", again["already_applied"])
        self.assertEqual(self.migration["applied"][-1], "0009-pm-owner-d1-outbox")
        # drift of the governed migration bytes fails closed (existing runner)
        import json as _json
        import shutil
        import tempfile
        from pathlib import Path
        from control_context_state_postgres import DEFAULT_MANIFEST
        with tempfile.TemporaryDirectory() as d:
            for f in Path(DEFAULT_MANIFEST).parent.iterdir():
                if f.suffix in (".sql", ".json"):
                    shutil.copy(f, d)
            p = Path(d) / "control_context_state_postgres_0009_pm_owner_d1_outbox.sql"
            p.write_text(p.read_text() + "\n-- drift\n")
            with self.assertRaises(MigrationDriftError):
                apply_postgres_migrations(self.pg.factory("migrator"), Path(d) / DEFAULT_MANIFEST.name)
        rec = self.commit("pm-ready:c8", "idem:c8")
        self.assertEqual(len(self.intents()), 1)
        # newer outbox schema version: enqueue refuses (whole commit rolls back) and the publisher read refuses
        self.sql("UPDATE cerebro_pm_owner_d1_outbox_meta SET outbox_schema_version=2", fetch=False)
        with self.assertRaises(Exception):
            self.commit("pm-ready:c8b", "idem:c8b")
        pub, t = self.publisher()
        with self.assertRaises(Exception):
            pub.port.pending(limit=4)
        self.sql("UPDATE cerebro_pm_owner_d1_outbox_meta SET outbox_schema_version=1", fetch=False)
        self.assertEqual(len(self.intents()), 1)
        # safe rollback = the shipped disable script (migration/owner role); pending lineage retained
        disable = (Path(rt.CONTEXT_ROOT) / "deployment" / "proposals" /
                   "bk11_pm_owner_d1_outbox_disable.sql").read_text()
        self.sql(disable, who="migrator", fetch=False)
        res = pub.publish_pending_once()
        self.assertEqual([r["result"] for r in res], ["DRAIN_DISABLED_RETAINED"])
        self.assertEqual(t.total(), 0)
        with self.assertRaises(Exception):
            pub.port.outbox_transition(self.intents()[0][0], from_state="INTENDED", from_token=None,
                                       state="SEND_CLAIMED", claim_token="t", claimed_at="2026-10-09T00:00:00.0Z",
                                       attempts=1)
        self.commit("pm-ready:c8c", "idem:c8c")
        self.assertEqual(len(self.intents()), 1)                           # disabled enqueue: no new obligation
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):      # runtime role has no DELETE grant
            self.sql("DELETE FROM cerebro_pm_owner_d1_outbox", who="runtime", fetch=False)
        with self.assertRaises(psycopg.errors.ObjectNotInPrerequisiteState):
            self.sql("DELETE FROM cerebro_pm_owner_d1_outbox", fetch=False)   # even a superuser hits the trigger
        self.assertEqual(self.intents()[0][3], "INTENDED")
        # default OFF publisher config: zero broker calls
        from d1js.config import D1Config
        cfg_off = D1Config(enabled=False).validate()
        from d1js.internal_binding import ContextPublisher
        from d1js.publisher import OwnerOutboxPublisher
        counting = CountingTransport(t.inner)
        cp = ContextPublisher(pub.port, OwnerOutboxPublisher(owner=pub.port, transport=counting, config=cfg_off,
                                                             faults=Faults()), cfg_off)
        self.assertEqual(cp.publish_pending_once(), [{"result": "D1_DISABLED_NO_BROKER_ACTIVITY"}])
        self.assertEqual(cp.publisher.publish(self.intents()[0][0]), "D1_DISABLED_NO_BROKER_ACTIVITY")
        self.assertEqual(counting.total(), 0)
        with self.assertRaises(InternalBindingRefused):
            compose_context_publisher(binding=InternalBinding(), config=self.cfg, transport=t,
                                      client=ContextToolClient(self.fx, "d1pub"))
        install_route(self.pg, enqueue=True, drain=True)                     # re-enable: retained row drains
        self.deposit(rec)
        self.assertEqual([r["result"] for r in pub.publish_pending_once()], ["PUBLISHED"])
        self.ev.update(again=again, receipt=rec["receipt_ref"])


class C9_LookupDeadlineHold(OwnerCase):
    def test_lookup_deadline_exhaustion_holds_with_zero_retry(self):
        rec = self.commit("pm-ready:c9", "idem:c9")
        rc, _ = self.child({"cmd": "publish", "url": self.broker.url, "config": {},
                            "faults": {"publish.after_claim_before_send": "exit"}})
        self.assertEqual(rc, CRASH_EXIT_CODE)
        self.assertEqual(self.intents()[0][3], "SEND_CLAIMED")
        time.sleep(5.2)                                                      # claim is stale (claim_stale_s=5)
        pub, t = self.publisher(lookup_time_budget_s=0.000001)
        res = pub.publish_pending_once()
        self.assertEqual([r["result"] for r in res], ["RECONCILE_HOLD"], res)
        self.assertEqual((pub.publisher.publish_calls, t.calls.get("publish", 0)), (0, 0))
        row = self.intents()[0]
        self.assertEqual(row[3], "RECONCILE_HOLD")
        self.assertIn("lookup:", row[10])
        self.assertEqual(self.stream()["messages"], 0)
        pub2, t2 = self.publisher()                                          # normal budget: proven absent, ONE send
        self.assertEqual([r["result"] for r in pub2.publish_pending_once()], ["PUBLISHED"])
        self.assertEqual(pub2.publisher.publish_calls, 1)
        self.assertEqual(self.intents()[0][5], 2)                            # attempts: crash claim + proven retry
        self.deposit(rec)
        self.assertEqual(self.drain(self.receiver())[0]["d1"], "CONSUMED_SELECTED_RETURN")
        self.ev.update(hold_reason=row[10])


class P_SqlPrivilegeAndRls(OwnerCase):
    def test_actual_pg_rls_trigger_column_and_rollback_policy(self):
        rec = self.commit("pm-ready:p", "idem:p")
        mid, ev = self.intents()[0][0], rec["event_id"]
        with self.pg.connect("runtime") as db:
            def scoped(actor: str | None, principal: str = PRINCIPALS["d1pub"]):
                db.execute("SELECT set_config('cerebro.tenant_ref', %s, true)", (TENANT,))
                db.execute("SELECT set_config('cerebro.workspace_ref', %s, true)", (WORKSPACE,))
                db.execute("SELECT set_config('cerebro.principal_ref', %s, true)", (principal,))
                db.execute("SELECT set_config('cerebro.d1_actor', %s, true)", (actor or "",))
            row = self.sql("SELECT * FROM cerebro_pm_owner_d1_outbox", who="admin")[0]
            cols = [c for c in ("tenant_ref", "workspace_ref", "project_ref", "owner_ref", "route_ref", "receiver_ref",
                                "delivery_class", "binding_revision", "event_id", "receipt_ref", "commit_ref",
                                "referent_id", "revision_after", "owner_seq", "snapshot_sha256", "packet_sha256",
                                "message_id", "pointer", "pointer_sha256", "mapping_payload", "mapping_sha256",
                                "created_at", "expires_at")]
            base = dict(zip(cols, row[:len(cols)]))
            base["mapping_payload"] = psycopg.types.json.Jsonb(base["mapping_payload"])
            ins = ("INSERT INTO cerebro_pm_owner_d1_outbox (" + ",".join(cols) + ",state) VALUES (" +
                   ",".join(["%s"] * len(cols)) + ",'INTENDED')")
            # 1) insert without the PM-commit actor -> RLS violation
            scoped(None)
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                db.execute(ins, tuple({**base, "event_id": "pm-event:x1", "message_id": "d1:" + "1" * 40}.values()))
            db.rollback()
            # 2) insert as PM-commit actor but for a receiver the route does not name -> RLS violation
            #    (discriminates the table-qualified EXISTS: an unqualified column would bind to the route itself)
            scoped("pm_owner_commit")
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                db.execute(ins, tuple({**base, "event_id": "pm-event:x2", "message_id": "d1:" + "2" * 40,
                                       "receiver_ref": "x9-receiver:not-routed"}.values()))
            db.rollback()
            # 3) settle without the publisher actor -> RLS hides the row (0 rows)
            scoped(None)
            cur = db.execute("UPDATE cerebro_pm_owner_d1_outbox SET last_reason='x' WHERE message_id=%s", (mid,))
            self.assertEqual(cur.rowcount, 0)
            db.rollback()
            # 4) publisher actor rewriting identity bytes -> immutable trigger
            scoped("d1_publisher")
            with self.assertRaises(psycopg.errors.ObjectNotInPrerequisiteState):
                db.execute("UPDATE cerebro_pm_owner_d1_outbox SET pointer=pointer||'x'::bytea WHERE message_id=%s",
                           (mid,))
            db.rollback()
            # 5) illegal transition INTENDED -> PUBLISHED -> trigger
            scoped("d1_publisher")
            with self.assertRaises(psycopg.errors.ObjectNotInPrerequisiteState):
                db.execute("UPDATE cerebro_pm_owner_d1_outbox SET state='PUBLISHED', stream='X', stream_seq=1 "
                           "WHERE message_id=%s", (mid,))
            db.rollback()
            # 6) a different principal (not the route's publisher) -> 0 rows
            scoped("d1_publisher", principal=PRINCIPALS["other"])
            cur = db.execute("UPDATE cerebro_pm_owner_d1_outbox SET last_reason='x' WHERE message_id=%s", (mid,))
            self.assertEqual(cur.rowcount, 0)
            db.rollback()
            # 7) a legal settle inside a transaction that then rolls back leaves no trace
            scoped("d1_publisher")
            cur = db.execute("UPDATE cerebro_pm_owner_d1_outbox SET state='SEND_CLAIMED', claim_token='t', "
                             "claimed_at='x', attempts=1 WHERE message_id=%s AND state='INTENDED'", (mid,))
            self.assertEqual(cur.rowcount, 1)
            db.rollback()
        self.assertEqual(self.intents()[0][3], "INTENDED")
        # 8) installation role binding (column privileges) -> identity column UPDATE denied by privilege
        from pathlib import Path
        binding = (Path(rt.CONTEXT_ROOT) / "deployment" / "proposals" /
                   "bk11_pm_owner_d1_outbox_role_binding.sql").read_text()
        self.sql("SELECT set_config('cerebro.install_runtime_role', 'cerebro_runtime', false);" + binding,
                 fetch=False)
        with self.pg.connect("runtime") as db:
            db.execute("SELECT set_config('cerebro.tenant_ref', %s, true)", (TENANT,))
            db.execute("SELECT set_config('cerebro.workspace_ref', %s, true)", (WORKSPACE,))
            db.execute("SELECT set_config('cerebro.principal_ref', %s, true)", (PRINCIPALS["d1pub"],))
            db.execute("SELECT set_config('cerebro.d1_actor', 'd1_publisher', true)")
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                db.execute("UPDATE cerebro_pm_owner_d1_outbox SET receipt_ref='x' WHERE message_id=%s", (mid,))
            db.rollback()
        # the normal service path still works under the binding
        pub, _ = self.publisher()
        self.deposit(rec)
        self.assertEqual([r["result"] for r in pub.publish_pending_once()], ["PUBLISHED"])
        # the owner port refuses caller-owned substitutes
        for bad in (lambda m: [], {"outbox": None}, object(), "SELECT 1"):
            with self.assertRaises(OwnerPortRejected):
                require_owner_port(bad)
        self.ev.update(event=ev, message_id=mid)


if __name__ == "__main__":
    unittest.main()
