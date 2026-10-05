"""Focused OFFLINE bridge proofs. FakeBroker is not a live private NATS server.

Runs with the already-installed signalvev-client when
SIGNALVEV_CLIENT_TEST_TARGET=installed; no original package suites are rerun.
"""
from __future__ import annotations

import sys
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "providers"))
sys.path.insert(0, str(HERE.parents[1] / "signalvev-client-v0.1" / "tests"))
from _support import FakeBroker, NOW, OWNER, POINTER_SHA, TmpCase, config_doc, raw_event
from signalvev_client import EvidenceBusyError, parse_config
from signalvev_sensing import JsonlStore, ResolverResult, accept_owner_event, build_frame
from room_carrier_ingress import (CASE, C3, SERVER, Episode, RoomBridgeError, _RoomSink, _append, _entry, _latest,
                                 _records, finish_app_request, make_room_listener, prepare_app_request)


class ExactOwner:
    """Offline owner double, NOT a production credential or authenticated Drive reader."""
    def __init__(self):
        self.calls = []
        self.mode = "SAME"

    def resolve(self, req):
        self.calls.append(req)
        if self.mode == "RAISE":
            raise RuntimeError("offline simulated owner failure")
        return ResolverResult(req.owner_ref, req.referent_type, req.referent_id,
                              req.expected_revision if self.mode == "SAME" else "new-revision",
                              self.mode, req.expected_sha256, grounding={"test": "local-only"},
                              owner_seq=req.owner_seq)


class RoomBridgeTests(TmpCase):
    def setUp(self):
        super().setUp()
        self.event = raw_event(event_id="room-event-01", source_ref="src:doc-a",
                               delta={"kind": "POINTER", "expected_sha256": POINTER_SHA, "ref": "src:doc-a"},
                               way_home=[CASE, "owner:doc-a#section-3"])
        self.frame = build_frame(accept_owner_event(self.event), now_epoch=NOW, ttl_seconds=30)
        self.ep = Episode(CASE, "room-event-01", OWNER, "src:doc-a", "doc", "doc-a", "rev-5",
                          POINTER_SHA, self.frame.envelope["payload_hash"], 5, int(NOW) + 30,
                          "pm:test-only-effect-binding",
                          ("review:test:revision:hash", "return:test:revision:hash"))
        self.cfg = parse_config(config_doc(self.tmp,
            nats={"server": "nats://" + SERVER, "allow_plaintext": True},
            resolver={"kind": "factory", "factory": "test_owner:factory"},
            listen={"interest": [{"owner_ref": OWNER, "referent_type": "doc", "referent_id": "doc-a"}]}))
        self.resolver = ExactOwner()
        self.broker = FakeBroker()
        self.clients = []
        self.results = []
        self.completed = threading.Event()
        self.addCleanup(self.stop)

    def stop(self):
        for client in self.clients:
            client.stop()
        self.clients.clear()

    def listen(self, ep=None):
        client = make_room_listener(self.cfg, ep or self.ep, resolver=self.resolver, enabled=True)
        # TEST-ONLY fake nats-py boundary on a genuine installed ListenClient.
        async def connect(**kwargs):
            nc = await self.broker.connect(**kwargs)
            nc.connected_url.netloc = SERVER
            return nc
        client._connect_fn = connect
        client._clock = lambda: NOW
        client._user_on_result = lambda r: (self.results.append(r), self.completed.set())
        self.clients.append(client)
        client.start()
        return client

    def receive(self, frame=None):
        self.completed.clear()
        frame = frame or self.frame
        self.broker.deliver(frame.subject, frame.data)
        self.assertTrue(self.completed.wait(3), "offline callback did not finish")
        return self.results[-1]

    def pending(self):
        self.listen()
        result = self.receive()
        self.assertEqual((result.disposition, result.return_error), ("ACK_READ", None))
        self.stop()

    def rows(self):
        return _records(self.cfg.evidence_dir / "closures.jsonl", "closure-log")

    def stage(self):
        return _latest(self.rows(), self.ep.event_id)["stage"]

    def prepare(self, *, state="idle", ep=None, now=None):
        return prepare_app_request(self.cfg, ep or self.ep, resolver=self.resolver,
                                   app_status={"thread_id": C3, "state": state},
                                   now=int(NOW) if now is None else now, enabled=True)

    def test_installed_callback_pending_then_actual_pinned_app_request(self):
        self.pending()
        self.assertEqual(self.stage(), "RECEIVE_PENDING")
        pending = _latest(self.rows(), self.ep.event_id)
        self.assertEqual((pending["origin"], pending["transport_server"]), ("INSTALLED_LISTEN_CALLBACK", SERVER))
        for state in ("active", "unavailable", "asleep"):
            self.assertEqual(self.prepare(state=state)["result"], "PENDING")
        self.assertEqual(len(self.resolver.calls), 1)  # only receiver's own read, no busy reread
        request = self.prepare()
        self.assertEqual(request["result"], "APP_REQUEST")
        self.assertEqual(request["arguments"]["threadId"], C3)
        self.assertIn(self.ep.event_id, request["arguments"]["prompt"])
        self.assertIn(self.ep.revision, request["arguments"]["prompt"])
        self.assertIn(self.ep.sha256, request["arguments"]["prompt"])
        self.assertIn("RECEIVED_AWAIT_WORK_BIND", request["arguments"]["prompt"])
        self.assertEqual(self.stage(), "APP_INTENT")
        self.assertTrue(all(r.get("work_consumed", False) is False for r in self.rows()))
        self.assertEqual(self.broker.publish_calls, [])  # no sender exists in this test

    def test_duplicate_does_not_make_a_second_pending_or_wake_after_restart(self):
        self.listen()
        self.receive()
        self.assertEqual(self.receive().disposition, "DUPLICATE")
        self.stop()
        self.listen()
        self.assertEqual(self.receive().disposition, "DUPLICATE")
        self.stop()
        self.assertEqual(sum(r.get("stage") == "RECEIVE_PENDING" for r in self.rows()), 1)
        request = self.prepare()
        self.assertEqual(finish_app_request(self.cfg, self.ep, request_sha256=request["request_sha256"],
                         outcome="APP_ACCEPT", tool_result_sha256="a" * 64, enabled=True), "APP_ACCEPT")
        self.assertEqual(self.prepare(), {"result": "NO_REPLAY", "stage": "APP_ACCEPT"})
        self.assertFalse(_latest(self.rows(), self.ep.event_id)["work_consumed"])

    def test_uncertain_app_acceptance_and_crash_after_intent_never_replay(self):
        self.pending()
        request = self.prepare()
        self.assertEqual(self.prepare(), {"result": "NO_REPLAY", "stage": "APP_INTENT"})
        self.assertEqual(finish_app_request(self.cfg, self.ep, request_sha256=request["request_sha256"],
                         outcome="UNKNOWN_APP_ACCEPT", tool_result_sha256="b" * 64, enabled=True), "UNKNOWN_APP_ACCEPT")
        self.assertEqual(self.prepare(), {"result": "NO_REPLAY", "stage": "UNKNOWN_APP_ACCEPT"})
        self.assertEqual(sum(r.get("stage") == "APP_INTENT" for r in self.rows()), 1)

    def test_wrong_event_payload_hash_and_same_event_collision_block_the_bridge(self):
        self.listen(replace(self.ep, d0_sha256="0" * 64))
        self.receive()
        self.assertEqual(self.stage(), "HOLD_RECEIVER_CONFLICT")
        self.stop()
        with self.assertRaisesRegex(RoomBridgeError, "HOST_BINDING_CHANGED"):
            self.prepare()  # changing host episode cannot adopt old evidence

    def test_changed_payload_after_pending_prevents_any_app_request(self):
        self.listen()
        self.receive()
        changed = dict(self.event, source_ref="src:different")
        frame = build_frame(accept_owner_event(changed), now_epoch=NOW, ttl_seconds=30)
        self.assertEqual(self.receive(frame).disposition, "CONFLICT_HOLD")
        self.stop()
        self.assertEqual(self.prepare(), {"result": "NO_REPLAY", "stage": "HOLD_RECEIVER_CONFLICT"})

    def test_fresh_owner_change_and_expiry_refuse_before_app_intent(self):
        self.pending()
        self.resolver.mode = "SUPERSEDED"
        self.assertEqual(self.prepare()["result"], "HOLD_STALE_OR_UNREADABLE")
        self.assertEqual(self.stage(), "HOLD_STALE_OR_UNREADABLE")
        self.assertFalse(any(r.get("stage") == "APP_INTENT" for r in self.rows()))

    def test_expiry_while_asleep_does_not_reread_or_wake(self):
        self.pending()
        before = len(self.resolver.calls)
        self.assertEqual(self.prepare(state="asleep", now=self.ep.expires_at)["result"], "HOLD_EXPIRED")
        self.assertEqual(len(self.resolver.calls), before)

    def test_expiry_during_slow_owner_read_does_not_create_app_intent(self):
        self.pending()
        with patch("room_carrier_ingress.time.monotonic", side_effect=(100, 131)):
            self.assertEqual(self.prepare()["result"], "HOLD_EXPIRED")
        self.assertFalse(any(r.get("stage") == "APP_INTENT" for r in self.rows()))

    def test_manual_closure_missing_real_receiver_records_is_refused(self):
        self.listen()
        from signalvev_sensing.activation import decide_activation
        from signalvev_sensing.return_sink import make_closure
        closure = make_closure(event_id=self.ep.event_id, owner_ref=OWNER, referent_type="doc", referent_id="doc-a",
             revision_after="rev-5", disposition="ACK_READ", reason="OWNER_READ_MATCHES_EVENT",
             observed_sha256=POINTER_SHA, activation=decide_activation("ACK_READ", "MECHANICAL"),
             way_home=(CASE,))
        with self.assertRaisesRegex(RoomBridgeError, "RECEIVER_READBACK_INCOMPLETE"):
            self.clients[-1]._extra_sink.deliver(closure)
        self.assertIsNone(_latest(self.rows(), self.ep.event_id))
        self.assertEqual(self.broker.publish_calls, [])

    def test_active_receiver_and_unbound_carrier_or_authority_are_refused(self):
        self.listen()
        self.receive()
        with self.assertRaises(EvidenceBusyError):
            self.prepare()
        self.stop()
        with self.assertRaisesRegex(RoomBridgeError, "WRONG_APP_CARRIER"):
            prepare_app_request(self.cfg, self.ep, resolver=self.resolver, now=int(NOW), enabled=True,
                                app_status={"thread_id": "wrong-thread", "state": "idle"})
        with self.assertRaisesRegex(RoomBridgeError, "DEFAULT_OFF"):
            make_room_listener(self.cfg, self.ep, resolver=self.resolver)
        with self.assertRaisesRegex(RoomBridgeError, "QUALIFIED_PRIVATE_CONFIG_REQUIRED"):
            make_room_listener(replace(self.cfg, resolver_kind="synthetic_fixture"), self.ep,
                               resolver=self.resolver, enabled=True)
        with self.assertRaisesRegex(RoomBridgeError, "EXACT_ARTIFACT_INTEREST_REQUIRED"):
            make_room_listener(replace(self.cfg, interests=self.cfg.interests * 2), self.ep,
                               resolver=self.resolver, enabled=True)
        with self.assertRaises(TypeError):
            Episode(**{**self.ep.__dict__, "carrier_ref": "wrong-thread"})

    def test_wrong_tool_result_binding_cannot_create_an_acceptance_receipt(self):
        self.pending()
        request = self.prepare()
        with self.assertRaisesRegex(RoomBridgeError, "WRONG_APP_REQUEST_RECEIPT"):
            finish_app_request(self.cfg, self.ep, request_sha256="f" * 64, outcome="APP_ACCEPT",
                               tool_result_sha256="a" * 64, enabled=True)
        with self.assertRaisesRegex(RoomBridgeError, "INVALID_APP_OUTCOME"):
            finish_app_request(self.cfg, self.ep, request_sha256=request["request_sha256"], outcome="ACTOR_CONSUME",
                               tool_result_sha256="a" * 64, enabled=True)
        self.assertEqual(self.stage(), "APP_INTENT")

    def test_manual_pending_without_cursor_or_closure_never_creates_app_intent(self):
        # Reproduces X5's uncovered path with checksum-valid evidence and NO broker.
        _append(self.cfg.evidence_dir / "closures.jsonl", _entry(self.ep, "RECEIVE_PENDING",
                closure_id="manual-no-broker", origin="INSTALLED_LISTEN_CALLBACK", transport_server=SERVER,
                receiver_owner_read=True, actor_source_read=False))
        with self.assertRaisesRegex(RoomBridgeError, "RECEIVER_READBACK_INCOMPLETE"):
            self.prepare()
        self.assertEqual(self.broker.delivered, [])
        self.assertEqual(self.broker.connects, [])
        self.assertEqual(self.resolver.calls, [])
        self.assertFalse(any(r.get("stage") == "APP_INTENT" for r in self.rows()))

    def test_pending_revalidates_original_receiver_chain_under_lock(self):
        self.pending()  # genuine installed-client callback via the OFFLINE broker double
        cursor_path = self.cfg.evidence_dir / "cursor.jsonl"
        closure_path = self.cfg.evidence_dir / "closures.jsonl"
        original_cursor = cursor_path.read_bytes()
        original_closures = closure_path.read_bytes()
        calls_before = len(self.resolver.calls)

        def rewrite(path, name, records):
            path.unlink()  # only this test's disposable temporary file
            store = JsonlStore(path, name=name)
            try:
                for row in records:
                    if row.get("kind") != "HEADER":
                        store.append(row)
            finally:
                store.close()

        cases = ("missing_cursor", "missing_final", "missing_closure", "claim_hash", "claim_seq",
                 "latest_final", "latest_closure", "closure_revision", "closure_hash", "closure_authority",
                 "pending_origin", "pending_source", "pending_closure")
        for case in cases:
            with self.subTest(case=case):
                cursor_path.write_bytes(original_cursor)
                closure_path.write_bytes(original_closures)
                cursor = _records(cursor_path, "receiver-cursor")
                closures = self.rows()
                claim = next(r for r in cursor if r.get("kind") == "CLAIM")
                final = next(r for r in cursor if r.get("kind") == "FINAL")
                closure = next(r for r in closures if r.get("kind") == "CLOSURE")
                pending = _latest(closures, self.ep.event_id)
                if case == "missing_final":
                    cursor = [r for r in cursor if r.get("kind") != "FINAL"]
                elif case == "missing_closure":
                    closures = [r for r in closures if r.get("kind") != "CLOSURE"]
                elif case == "claim_hash":
                    claim["d0_hash"] = "0" * 64
                elif case == "claim_seq":
                    claim["owner_seq"] += 1
                elif case == "latest_final":
                    cursor.append({**final, "disposition": "HOLD_UNREADABLE", "reason": "LATER_OWNER_HOLD"})
                elif case == "latest_closure":
                    closures.append({**closure, "closure_id": "later-conflict", "disposition": "CONFLICT_HOLD",
                                     "reason": "SAME_EVENT_ID_CHANGED_FINGERPRINT", "receipt_stage": "DELIVERED"})
                elif case == "closure_revision":
                    closure["revision_after"] = "other-revision"
                elif case == "closure_hash":
                    closure["observed_sha256"] = "0" * 64
                elif case == "closure_authority":
                    closure["authority"] = "SOURCE_WRITE"
                elif case == "pending_origin":
                    pending["origin"] = "MANUAL"
                elif case == "pending_source":
                    pending["source_ref"] = "src:other"
                elif case == "pending_closure":
                    pending["closure_id"] = "manual-no-broker"
                rewrite(cursor_path, "receiver-cursor", cursor)
                rewrite(closure_path, "closure-log", closures)
                if case == "missing_cursor":
                    cursor_path.unlink()
                with self.assertRaisesRegex(RoomBridgeError, "RECEIVER_READBACK_(INCOMPLETE|CONFLICT)"):
                    self.prepare()
                self.assertFalse(any(r.get("stage") == "APP_INTENT" for r in self.rows()))
                self.assertEqual(len(self.resolver.calls), calls_before)
        # Restore the authentic chain: the correction must retain the matching path.
        cursor_path.write_bytes(original_cursor)
        closure_path.write_bytes(original_closures)
        self.assertEqual(self.prepare()["result"], "APP_REQUEST")
        self.assertEqual(self.prepare(), {"result": "NO_REPLAY", "stage": "APP_INTENT"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
