"""Private synthetic service contract; no historical/live Postkassa endpoint is contacted."""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from support import CANARY_BODY, ServerCase, jbody

PILOT = "room-fixture-marianne-pilot"
ADMIN = "room-fixture-andreas-admin"


class Fixture(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        self.messages = {}
        self.posts = []
        super().__init__(("127.0.0.1", 0), FixtureHandler)


class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return

    def _room(self):
        return {"Bearer pilot-secret": PILOT, "Bearer admin-secret": ADMIN}.get(
            self.headers.get("Authorization"))

    def _send(self, status, obj):
        raw = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        room = self._room()
        if room is None:
            return self._send(403, {})
        if self.path == "/v1/me":
            return self._send(200, {"room_id": room})
        if self.path == "/v1/contacts":
            return self._send(200, {"contacts": [{"room_id": ADMIN if room == PILOT else PILOT}]})
        if self.path.startswith("/v1/postkassa?limit="):
            return self._send(200, {"messages": [m for m in self.server.messages.values()
                                                if m["recipient_room_id"] == room]})
        match = re.fullmatch(r"/v1/postkassa/([A-Za-z0-9_-]+)", self.path)
        m = self.server.messages.get(match.group(1)) if match else None
        return self._send(200, m) if m and m["recipient_room_id"] == room else self._send(404, {})

    def do_POST(self):
        room = self._room()
        if room is None:
            return self._send(403, {})
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/v1/postkassa":
            if body.get("recipient") != (ADMIN if room == PILOT else PILOT):
                return self._send(403, {})
            mid = f"synthetic-{len(self.server.messages) + 1}"
            m = {"message_id": mid, "sender_room_id": room, "recipient_room_id": body["recipient"],
                 "payload": body["payload"], "delivery_state": "DELIVERED"}
            self.server.messages[mid] = m
            self.server.posts.append((self.path, body))
            return self._send(201, m)
        reply = re.fullmatch(r"/v1/postkassa/([A-Za-z0-9_-]+)/reply", self.path)
        original = self.server.messages.get(reply.group(1)) if reply else None
        if original and original["recipient_room_id"] == room:
            mid = f"synthetic-{len(self.server.messages) + 1}"
            m = {"message_id": mid, "sender_room_id": room,
                 "recipient_room_id": original["sender_room_id"], "payload": body["payload"],
                 "delivery_state": "DELIVERED"}
            self.server.messages[mid] = m
            self.server.posts.append((self.path, body))
            return self._send(201, m)
        match = re.fullmatch(r"/v1/postkassa/([A-Za-z0-9_-]+)/ack", self.path)
        m = self.server.messages.get(match.group(1)) if match else None
        if not m or m["recipient_room_id"] != room:
            return self._send(404, {})
        m["delivery_state"] = "ACKED"
        self.server.posts.append((self.path, body))
        return self._send(200, {"message_id": m["message_id"]})


class PostkasseAdapterTest(ServerCase):
    def setUp(self):
        self.fixture = Fixture()
        self.fixture_thread = threading.Thread(target=self.fixture.serve_forever, daemon=True)
        self.fixture_thread.start()
        self.addCleanup(self.fixture.server_close)
        self.addCleanup(self.fixture.shutdown)
        os.environ["CB16_PILOT_SECRET"] = "pilot-secret"
        os.environ["CB16_ADMIN_SECRET"] = "admin-secret"
        self.addCleanup(os.environ.pop, "CB16_PILOT_SECRET", None)
        self.addCleanup(os.environ.pop, "CB16_ADMIN_SECRET", None)
        self.env_over = {
            "secret_refs": {"pilot": "env:CB16_PILOT_SECRET", "admin": "env:CB16_ADMIN_SECRET"},
            "integrations": {"postkasse": {"enabled": True,
                "endpoint": f"http://127.0.0.1:{self.fixture.server_address[1]}",
                "rooms": {PILOT: {"server_room_id": PILOT, "credential_ref": "pilot",
                                  "contacts": {"ADMIN": ADMIN}},
                          ADMIN: {"server_room_id": ADMIN, "credential_ref": "admin",
                                  "contacts": {"PILOT": PILOT}}}}}}
        super().setUp()

    def test_round_trip_and_private_boundaries(self):
        pilot = self.logged_in("fixture-marianne-pilot")
        admin = self.logged_in("fixture-andreas-admin")
        st, _, page = pilot.get(f"/rom/{PILOT}/postkasse")
        self.assertEqual(st, 200)
        self.assertIn(b"Ny melding", page)
        st, _, body = pilot.post(f"/rom/{PILOT}/postkasse", {"csrf": pilot.csrf(), "recipient": "ADMIN",
                              "tekst": CANARY_BODY, "dedupe_key": secrets.token_urlsafe(18)})
        self.assertEqual(st, 303, body)
        self.assertEqual(len(self.fixture.messages), 1)
        path, wire = self.fixture.posts[0]
        self.assertEqual(path, "/v1/postkassa")
        self.assertNotIn("sender_room_id", wire)
        self.assertEqual(wire["recipient"], ADMIN)
        st, _, page = admin.get(f"/rom/{ADMIN}/postkasse")
        self.assertEqual(st, 200)
        self.assertIn(b"synthetic-1", page)
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
        self.assertNotIn("synthetic reply", self.log_text())

    def test_foreign_unpaired_tamper_and_csrf_fail_closed(self):
        pilot = self.logged_in("fixture-marianne-pilot")
        admin = self.logged_in("fixture-andreas-admin")
        st, _, _ = pilot.get(f"/rom/{ADMIN}/postkasse")
        self.assertEqual(st, 404)
        st, _, _ = pilot.post(f"/rom/{ADMIN}/postkasse", {"csrf": pilot.csrf(), "recipient": "PILOT",
                              "tekst": "bad", "dedupe_key": "bad-1"})
        self.assertEqual(st, 404)
        st, _, _ = pilot.post(f"/rom/{PILOT}/postkasse", {"csrf": pilot.csrf(), "recipient": "UNKNOWN",
                              "tekst": "bad", "dedupe_key": "bad-2"})
        self.assertEqual(st, 400)
        st, _, _ = pilot.post(f"/rom/{PILOT}/postkasse", {"csrf": pilot.csrf(), "recipient": "ADMIN",
                              "room_id": ADMIN, "tekst": "bad", "dedupe_key": "bad-3"})
        self.assertEqual(st, 400)
        st, _, _ = pilot.post(f"/rom/{PILOT}/postkasse", {"csrf": "wrong", "recipient": "ADMIN",
                              "tekst": "bad", "dedupe_key": "bad-4"})
        self.assertEqual(st, 403)
        self.assertEqual(self.fixture.posts, [])
        pilot.post(f"/rom/{PILOT}/postkasse", {"csrf": pilot.csrf(), "recipient": "ADMIN",
                                                    "tekst": "secret body", "dedupe_key": "good-1"})
        st, _, body = pilot.get(f"/rom/{PILOT}/postkasse/synthetic-1")
        self.assertEqual(st, 404)
        self.assertNotIn(b"secret body", body)
        st, _, body = admin.get(f"/rom/{ADMIN}/postkasse/missing")
        self.assertEqual(st, 404)
        self.assertEqual(jbody(body)["error"], "NOT_FOUND")

    def test_disabled_default_and_unqualified_endpoint(self):
        pilot = self.logged_in("fixture-marianne-pilot")
        self.app.postkasse.spec = {"enabled": False}
        st, _, page = pilot.get(f"/rom/{PILOT}")
        self.assertEqual(st, 200)
        self.assertIn(b"Ikke tilgjengelig", page)
        st, _, body = pilot.get(f"/rom/{PILOT}/postkasse")
        self.assertEqual(st, 503)
        self.assertEqual(jbody(body)["error"], "POSTKASSE_DISABLED")
        self.app.postkasse.spec = {**self.env_over["integrations"]["postkasse"],
                                   "endpoint": "http://postkassa.example:8788"}
        st, _, body = pilot.get(f"/rom/{PILOT}/postkasse")
        self.assertEqual(st, 503)
        self.assertEqual(jbody(body)["error"], "POSTKASSE_ENDPOINT_UNQUALIFIED")

    def test_service_identity_mismatch_and_untrusted_body(self):
        pilot = self.logged_in("fixture-marianne-pilot")
        self.app.postkasse.spec = {**self.env_over["integrations"]["postkasse"],
                                   "rooms": {PILOT: {"server_room_id": "wrong-room",
                                                     "credential_ref": "pilot", "contacts": {"ADMIN": ADMIN}}}}
        st, _, body = pilot.post(f"/rom/{PILOT}/postkasse", {"csrf": pilot.csrf(), "recipient": "ADMIN",
                              "tekst": "must-not-send", "dedupe_key": "mismatch-1"})
        self.assertEqual(st, 503)
        self.assertEqual(jbody(body)["error"], "POSTKASSE_ROOM_MISMATCH")
        self.assertEqual(self.fixture.posts, [])
        self.app.postkasse.spec = self.env_over["integrations"]["postkasse"]
        admin = self.logged_in("fixture-andreas-admin")
        html = '<script>alert("receipt is not authority")</script>'
        pilot.post(f"/rom/{PILOT}/postkasse", {"csrf": pilot.csrf(), "recipient": "ADMIN",
                                                "tekst": html, "dedupe_key": "escape-1"})
        st, _, page = admin.get(f"/rom/{ADMIN}/postkasse/synthetic-1")
        self.assertEqual(st, 200)
        self.assertNotIn(b"<script>", page)
        self.assertIn(b"&lt;script&gt;", page)
        self.assertNotIn(html, self.log_text())
