"""Exact CB16 synthetic wire fixture; never contacts the selected live endpoint."""
from __future__ import annotations

import json
import re
import shutil
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from support import CANARY_BODY, ServerCase, jbody
from cerebrobase.postkasse import PostkasseError
from cerebrobase.preflight import preflight

PILOT, ADMIN = "room-fixture-marianne-pilot", "room-fixture-andreas-admin"
S_PILOT, S_ADMIN = "cb16-synth-pilot", "cb16-synth-andreas-admin"
SCOPES = ["postkassa:read", "postkassa:send", "contacts:read"]


class Fixture(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        self.messages, self.posts, self.dedupe = {}, [], {}
        self.next_post_500, self.bad_contact = False, False
        super().__init__(("127.0.0.1", 0), FixtureHandler)


class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return

    def _room(self):
        return {"Bearer pilot-secret": S_PILOT, "Bearer admin-secret": S_ADMIN}.get(
            self.headers.get("Authorization"))

    def _send(self, status, obj):
        raw = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _new(self, sender, recipient, body, *, reply_to=None):
        mid = f"synthetic-{len(self.server.messages) + 1}"
        msg = {"message_id": mid, "sender_room_id": sender, "recipient_room_id": recipient,
               "payload_type": body["type"], "payload": body["payload"], "payload_hash": "synthetic-hash",
               "state": "ACCEPTED_PROTO", "created_at": "2026-10-06T00:00:00Z", "expires_at": None,
               "dedupe_key": body["dedupe_key"], "thread_id": reply_to or mid, "reply_to": reply_to,
               "protocol_version": "proto-postkassa/0.1", "room_bucket": recipient, "guards": {}}
        self.server.messages[mid] = msg
        return msg

    def do_GET(self):
        room = self._room()
        if room is None:
            return self._send(403, {"code": "AUTH_REJECTED"})
        if self.path == "/v1/me":
            return self._send(200, {"room_id": room, "scopes": SCOPES})
        if self.path == "/v1/contacts":
            peer = S_ADMIN if room == S_PILOT else S_PILOT
            alias = "andreas_admin" if room == S_PILOT else "pilot"
            if self.server.bad_contact:
                peer = "unpaired-server-room"
            return self._send(200, {"contacts": [{"alias": alias, "room_id": peer, "state": "ALLOWED"}]})
        if self.path.startswith("/v1/postkassa?limit="):
            return self._send(200, {"messages": [m for m in self.server.messages.values()
                                                if room in (m["sender_room_id"], m["recipient_room_id"])]})
        match = re.fullmatch(r"/v1/postkassa/([A-Za-z0-9_-]+)", self.path)
        msg = self.server.messages.get(match.group(1)) if match else None
        return self._send(200, {"message": msg}) if msg and room in (
            msg["sender_room_id"], msg["recipient_room_id"]) else self._send(404, {"code": "NOT_FOUND"})

    def do_POST(self):
        room = self._room()
        if room is None:
            return self._send(403, {"code": "AUTH_REJECTED"})
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/v1/postkassa":
            alias = "andreas_admin" if room == S_PILOT else "pilot"
            if body.get("recipient") != alias or "sender_room_id" in body or body.get("type") != "TEXT":
                return self._send(400, {"code": "INVALID"})
            key = (room, body["dedupe_key"])
            if key in self.server.dedupe:
                old_body, msg = self.server.dedupe[key]
                if old_body != body:
                    return self._send(409, {"code": "DEDUPE_CONFLICT"})
                return self._send(200, {"deduped": True, "message": msg})
            peer = S_ADMIN if room == S_PILOT else S_PILOT
            msg = self._new(room, peer, body)
            self.server.posts.append((self.path, body))
            self.server.dedupe[key] = (body, msg)
            if self.server.next_post_500:
                self.server.next_post_500 = False
                return self._send(500, {"code": "SERVER_ERROR"})
            return self._send(201, {"deduped": False, "message": msg})
        reply = re.fullmatch(r"/v1/postkassa/([A-Za-z0-9_-]+)/reply", self.path)
        original = self.server.messages.get(reply.group(1)) if reply else None
        if original and original["recipient_room_id"] == room:
            if body.get("type") != "REPLY":
                return self._send(400, {"code": "INVALID"})
            msg = self._new(room, original["sender_room_id"], body, reply_to=original["message_id"])
            self.server.posts.append((self.path, body))
            return self._send(201, {"deduped": False, "message": msg})
        ack = re.fullmatch(r"/v1/postkassa/([A-Za-z0-9_-]+)/ack", self.path)
        msg = self.server.messages.get(ack.group(1)) if ack else None
        if msg and msg["recipient_room_id"] == room:
            msg["state"] = "ACKED_BY_RECIPIENT_PROTO"
            self.server.posts.append((self.path, body))
            return self._send(200, {"state": "ACKED_BY_RECIPIENT_PROTO", "deduped": False, "guards": {}})
        return self._send(404, {"code": "NOT_FOUND"})


class PostkasseAdapterTest(ServerCase):
    def setUp(self):
        self.fixture = Fixture()
        self.fixture_thread = threading.Thread(target=self.fixture.serve_forever, daemon=True)
        self.fixture_thread.start()
        self.addCleanup(self.fixture.server_close)
        self.addCleanup(self.fixture.shutdown)
        self.reader_root = Path(tempfile.mkdtemp(prefix="cb16-reader-"))
        self.addCleanup(shutil.rmtree, self.reader_root, True)
        self.reader = self.reader_root / "reader.json"
        self.reader.write_text(json.dumps({S_PILOT: "pilot-secret", S_ADMIN: "admin-secret"}), encoding="utf-8")
        self.spec = {"enabled": True, "endpoint": f"http://127.0.0.1:{self.fixture.server_address[1]}",
                     "credential_reader_ref": "cb16_reader",
                     "rooms": {PILOT: {"server_room_id": S_PILOT, "contacts": {"andreas_admin": S_ADMIN}},
                               ADMIN: {"server_room_id": S_ADMIN, "contacts": {"pilot": S_PILOT}}}}
        self.env_over = {"secret_refs": {"cb16_reader": "file:" + str(self.reader)},
                         "integrations": {"postkasse": self.spec}}
        super().setUp()

    def test_exact_wire_round_trip_and_own_sent_role(self):
        pilot = self.logged_in("fixture-marianne-pilot")
        admin = self.logged_in("fixture-andreas-admin")
        st, _, page = pilot.get(f"/rom/{PILOT}/postkasse")
        self.assertEqual(st, 200)
        self.assertIn(b"Ny melding", page)
        st, _, _ = pilot.post(f"/rom/{PILOT}/postkasse", {"csrf": pilot.csrf(), "recipient": "andreas_admin",
                            "tekst": CANARY_BODY, "dedupe_key": "send-1"})
        self.assertEqual(st, 303)
        path, wire = self.fixture.posts[0]
        self.assertEqual(path, "/v1/postkassa")
        self.assertEqual(wire, {"recipient": "andreas_admin", "type": "TEXT", "payload": {"text": CANARY_BODY},
                                "dedupe_key": "send-1"})
        st, _, page = pilot.get(f"/rom/{PILOT}/postkasse/synthetic-1")
        self.assertEqual(st, 200)
        self.assertIn(CANARY_BODY.encode(), page)
        self.assertNotIn(b"Bekreft mottatt", page)
        st, _, body = pilot.post(f"/rom/{PILOT}/postkasse/synthetic-1/ack", {"csrf": pilot.csrf()})
        self.assertEqual(st, 404)
        self.assertEqual(jbody(body)["error"], "NOT_FOUND")
        st, _, _ = pilot.post(f"/rom/{PILOT}/postkasse/synthetic-1/reply",
                              {"csrf": pilot.csrf(), "tekst": "not a reply", "dedupe_key": "own-reply"})
        self.assertEqual(st, 400)
        st, _, page = admin.get(f"/rom/{ADMIN}/postkasse/synthetic-1")
        self.assertEqual(st, 200)
        self.assertIn(CANARY_BODY.encode(), page)
        st, _, _ = admin.post(f"/rom/{ADMIN}/postkasse/synthetic-1/ack", {"csrf": admin.csrf()})
        self.assertEqual(st, 303)
        st, _, _ = admin.post(f"/rom/{ADMIN}/postkasse/synthetic-1/reply",
                              {"csrf": admin.csrf(), "tekst": "synthetic reply", "dedupe_key": "reply-1"})
        self.assertEqual(st, 303)
        self.assertEqual(self.fixture.posts[-1][0], "/v1/postkassa/synthetic-1/reply")
        st, _, page = pilot.get(f"/rom/{PILOT}/postkasse/synthetic-2")
        self.assertEqual(st, 200)
        self.assertIn(b"synthetic reply", page)
        self.assertNotIn(CANARY_BODY, self.log_text())
        self.assertNotIn("pilot-secret", self.log_text())

    def test_foreign_unpaired_csrf_and_duplicate_bindings(self):
        pilot = self.logged_in("fixture-marianne-pilot")
        st, _, _ = pilot.get(f"/rom/{ADMIN}/postkasse")
        self.assertEqual(st, 404)
        st, _, _ = pilot.post(f"/rom/{PILOT}/postkasse", {"csrf": pilot.csrf(), "recipient": "unknown",
                            "tekst": "no", "dedupe_key": "bad-1"})
        self.assertEqual(st, 400)
        st, _, _ = pilot.post(f"/rom/{PILOT}/postkasse", {"csrf": pilot.csrf(), "recipient": "andreas_admin",
                            "room_id": ADMIN, "tekst": "no", "dedupe_key": "bad-2"})
        self.assertEqual(st, 400)
        st, _, _ = pilot.post(f"/rom/{PILOT}/postkasse", {"csrf": "wrong", "recipient": "andreas_admin",
                            "tekst": "no", "dedupe_key": "bad-3"})
        self.assertEqual(st, 403)
        self.assertEqual(self.fixture.posts, [])
        self.app.postkasse.spec = {**self.spec, "rooms": {**self.spec["rooms"], ADMIN: {
            "server_room_id": S_PILOT, "contacts": {"pilot": S_PILOT}}}}
        st, _, body = pilot.get(f"/rom/{PILOT}/postkasse")
        self.assertEqual(st, 503)
        self.assertEqual(jbody(body)["error"], "POSTKASSE_ROOM_UNPAIRED")
        self.assertEqual(self.fixture.posts, [])

    def test_unpaired_inbound_hidden_and_contact_mismatch(self):
        pilot = self.logged_in("fixture-marianne-pilot")
        self.fixture.messages["foreign-1"] = {"message_id": "foreign-1", "sender_room_id": "unpaired-room",
            "recipient_room_id": S_PILOT, "payload_type": "TEXT", "payload": {"text": "FOREIGN_PRIVATE_BODY"},
            "state": "ACCEPTED_PROTO", "protocol_version": "proto-postkassa/0.1"}
        st, _, page = pilot.get(f"/rom/{PILOT}/postkasse")
        self.assertEqual(st, 200)
        self.assertNotIn(b"foreign-1", page)
        self.assertNotIn(b"FOREIGN_PRIVATE_BODY", page)
        st, _, body = pilot.get(f"/rom/{PILOT}/postkasse/foreign-1")
        self.assertEqual(st, 404)
        self.assertNotIn(b"FOREIGN_PRIVATE_BODY", body)
        st, _, _ = pilot.post(f"/rom/{PILOT}/postkasse/foreign-1/ack", {"csrf": pilot.csrf()})
        self.assertEqual(st, 404)
        self.fixture.bad_contact = True
        st, _, body = pilot.get(f"/rom/{PILOT}/postkasse")
        self.assertEqual(st, 503)
        self.assertEqual(jbody(body)["error"], "POSTKASSE_CONTACT_MISMATCH")

    def test_uncertain_5xx_never_retries_and_dedupe_conflict(self):
        pilot = self.logged_in("fixture-marianne-pilot")
        self.fixture.next_post_500 = True
        st, _, body = pilot.post(f"/rom/{PILOT}/postkasse", {"csrf": pilot.csrf(),
            "recipient": "andreas_admin", "tekst": "may-commit", "dedupe_key": "uncertain-1"})
        self.assertEqual(st, 503)
        self.assertEqual(jbody(body)["error"], "POSTKASSE_OUTCOME_UNKNOWN")
        self.assertEqual(len(self.fixture.posts), 1)
        st, _, body = pilot.post(f"/rom/{PILOT}/postkasse", {"csrf": pilot.csrf(),
            "recipient": "andreas_admin", "tekst": "different", "dedupe_key": "uncertain-1"})
        self.assertEqual(st, 409)
        self.assertEqual(jbody(body)["error"], "POSTKASSE_CONFLICT")
        self.assertEqual(len(self.fixture.posts), 1)

    def test_exact_retry_200_and_duplicate_reader_token_rejected(self):
        pilot = self.logged_in("fixture-marianne-pilot")
        form = {"csrf": pilot.csrf(), "recipient": "andreas_admin", "tekst": "same",
                "dedupe_key": "same-1"}
        self.assertEqual(pilot.post(f"/rom/{PILOT}/postkasse", form)[0], 303)
        self.assertEqual(pilot.post(f"/rom/{PILOT}/postkasse", form)[0], 303)
        self.assertEqual(len(self.fixture.posts), 1)
        self.reader.write_text(json.dumps({S_PILOT: "same-token", S_ADMIN: "same-token"}), encoding="utf-8")
        st, _, body = pilot.get(f"/rom/{PILOT}/postkasse")
        self.assertEqual(st, 503)
        self.assertEqual(jbody(body)["error"], "POSTKASSE_CREDENTIAL_UNRESOLVED")
        self.assertEqual(len(self.fixture.posts), 1)

    def test_read_only_qualification_rejects_missing_membership(self):
        object.__setattr__(self.cfg, "environment", "STAGING")
        con = self.db()
        con.execute("UPDATE memberships SET status='REVOKED' WHERE room_id=?", (PILOT,))
        con.close()
        result = preflight(self.cfg, check_listener=False)
        item = next(x for x in result["items"] if x["name"] == "integration:postkasse")
        self.assertEqual(item["code"], "POSTKASSE_MEMBERSHIP_UNQUALIFIED")
        self.assertEqual(self.fixture.posts, [])

    def test_read_only_qualification_and_exact_private_http_allowlist(self):
        object.__setattr__(self.cfg, "environment", "STAGING")
        result = preflight(self.cfg, check_listener=False)
        item = next(x for x in result["items"] if x["name"] == "integration:postkasse")
        self.assertEqual(item["status"], "INSTALLED_TESTED")
        self.assertEqual(self.fixture.posts, [])
        self.fixture.bad_contact = True
        result = preflight(self.cfg, check_listener=False)
        item = next(x for x in result["items"] if x["name"] == "integration:postkasse")
        self.assertEqual(item["code"], "POSTKASSE_CONTACT_MISMATCH")
        self.fixture.bad_contact = False
        self.app.postkasse.spec = {**self.spec, "endpoint": "http://100.77.125.87:18788"}
        self.assertEqual(self.app.postkasse._address(), ("http", "100.77.125.87", 18788))
        self.app.postkasse.spec = {**self.spec, "endpoint": "http://100.77.125.88:18788"}
        with self.assertRaises(PostkasseError):
            self.app.postkasse._address()
        object.__setattr__(self.cfg, "environment", "DEV")
        self.app.postkasse.spec = {**self.spec, "endpoint": "http://100.77.125.87:18788"}
        with self.assertRaises(PostkasseError):
            self.app.postkasse._address()
