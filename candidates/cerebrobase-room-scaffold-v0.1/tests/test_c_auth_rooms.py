"""C. server-side identity/role/membership: admin and pilot shells, direct-URL and room_id manipulation in both
directions, forged headers/fields, missing/expired/revoked session, revoked membership, capability, logout,
CSRF and foreign Origin."""
from __future__ import annotations

import unittest

from support import ServerCase, jbody

ADMIN_ROOM, PILOT_ROOM = "room-fixture-andreas-admin", "room-fixture-marianne-pilot"


class C_RoomsAndRoles(ServerCase):
    def test_andreas_admin_shell_and_own_namespace(self):
        c = self.logged_in("fixture-andreas-admin")
        st, h, _ = c.get("/rom")
        self.assertEqual((st, h["Location"]), (303, f"/rom/{ADMIN_ROOM}"))
        st, _, body = c.get(f"/rom/{ADMIN_ROOM}")
        self.assertEqual(st, 200)
        self.assertIn("Adminrom".encode(), body)
        st, _, body = c.get("/admin")
        self.assertEqual(st, 200)
        self.assertIn("Admininngang".encode(), body)
        st, _, body = c.get(f"/api/rom/{ADMIN_ROOM}/filer")
        self.assertEqual((st, jbody(body)["error"]), (503, "CAPABILITY_UNAVAILABLE"))   # own namespace, no fake files

    def test_marianne_pilot_shell_only(self):
        c = self.logged_in("fixture-marianne-pilot")
        st, h, _ = c.get("/rom")
        self.assertEqual(h["Location"], f"/rom/{PILOT_ROOM}")
        st, _, body = c.get(f"/rom/{PILOT_ROOM}")
        self.assertEqual(st, 200)
        self.assertIn("Pilotrom".encode(), body)
        self.assertNotIn(b"Admininngang</a>", body)
        self.assertEqual(c.get("/admin")[0], 403)
        self.assertEqual(c.get("/admin/drift.json")[0], 403)
        st, _, body = c.post("/admin/verktoy/fixture.echo", {"csrf": c.csrf(), "tekst": "x"})
        self.assertEqual((st, jbody(body)["code"]), (403, "ROLE_REQUIRED"))

    def test_foreign_room_id_both_directions_and_no_existence_oracle(self):
        a = self.logged_in("fixture-andreas-admin")
        m = self.logged_in("fixture-marianne-pilot")
        for client, foreign in ((a, PILOT_ROOM), (m, ADMIN_ROOM)):
            st_f, _, body_f = client.get(f"/rom/{foreign}")
            st_n, _, body_n = client.get("/rom/room-does-not-exist")
            self.assertEqual((st_f, st_n), (404, 404))
            strip = lambda b: b.replace(foreign.encode(), b"").replace(b"room-does-not-exist", b"")  # noqa: E731
            self.assertEqual(strip(body_f), strip(body_n))                       # identical denial
            for api in ("filer", "speil"):
                st, _, body = client.get(f"/api/rom/{foreign}/{api}")
                self.assertEqual((st, jbody(body)["error"]), (404, "NOT_FOUND"))  # admin cannot enumerate pilot
        st, _, body = a.get(f"/rom/{PILOT_ROOM}")
        self.assertNotIn(b"Marianne", body)

    def test_forged_headers_and_fields_are_ignored(self):
        m = self.logged_in("fixture-marianne-pilot")
        forged = {"X-Account-Id": "acc-fixture-andreas-admin", "X-Role": "ADMIN", "X-Room-Id": ADMIN_ROOM,
                  "X-Forwarded-User": "fixture-andreas-admin"}
        self.assertEqual(m.get("/admin", headers=forged)[0], 403)
        self.assertEqual(m.get(f"/rom/{ADMIN_ROOM}", headers=forged)[0], 404)
        st, _, body = m.post("/admin/verktoy/fixture.echo", {"csrf": m.csrf(), "role": "ADMIN", "account_id":
                                                             "acc-fixture-andreas-admin", "room_id": ADMIN_ROOM})
        self.assertEqual(st, 403)
        anon = self.client()
        self.assertEqual(anon.get("/admin", headers=forged)[1].get("Location"), "/logg-inn")

    def test_missing_expired_revoked_session_and_logout(self):
        anon = self.client()
        self.assertEqual(anon.get(f"/rom/{PILOT_ROOM}")[0], 303)
        self.assertEqual(anon.get(f"/api/rom/{PILOT_ROOM}/filer")[0], 401)
        a = self.logged_in("fixture-andreas-admin")
        token = a.cookies["cb_session"]
        self.clock.t += self.cfg.session_ttl_seconds + 1                         # expired
        self.assertEqual(a.get("/admin")[0], 303)
        self.clock.t -= self.cfg.session_ttl_seconds + 1
        self.assertEqual(a.get("/admin")[0], 200)
        st, h, _ = a.post("/logg-ut", {"csrf": a.csrf()})
        self.assertEqual((st, h["Location"]), (303, "/logg-inn"))
        stolen = self.client()
        stolen.cookies["cb_session"] = token                                     # the old token is dead at once
        self.assertEqual(stolen.get("/admin")[0], 303)
        self.assertEqual(stolen.get("/admin/drift.json")[0], 401)

    def test_revoked_membership_disabled_account_and_missing_capability(self):
        a = self.logged_in("fixture-andreas-admin")
        con = self.db()
        con.execute("DELETE FROM capabilities WHERE capability='tool:fixture.echo'")
        st, _, body = a.post("/admin/verktoy/fixture.echo", {"csrf": a.csrf(), "tekst": "x"})
        self.assertEqual(st, 403)
        self.assertIn("CAPABILITY_REQUIRED".encode(), body)
        con.execute("UPDATE memberships SET status='REVOKED' WHERE room_id=?", (ADMIN_ROOM,))
        self.assertEqual(a.get(f"/rom/{ADMIN_ROOM}")[0], 404)
        self.assertEqual(a.get("/admin")[0], 403)                               # role alone is not membership
        m = self.logged_in("fixture-marianne-pilot")
        con.execute("UPDATE accounts SET status='DISABLED' WHERE id='acc-fixture-marianne-pilot'")
        self.assertEqual(m.get(f"/rom/{PILOT_ROOM}")[0], 303)
        con.close()

    def test_membership_is_not_role(self):
        con = self.db()
        con.execute("INSERT INTO memberships VALUES ('acc-fixture-marianne-pilot', ?, 1, 'MEMBER', 'ACTIVE')",
                    (ADMIN_ROOM,))
        con.close()
        m = self.logged_in("fixture-marianne-pilot")
        self.assertEqual(m.get(f"/rom/{ADMIN_ROOM}")[0], 200)                   # member of the room ...
        self.assertEqual(m.get("/admin")[0], 403)                                # ... but no ADMIN role

    def test_csrf_and_foreign_origin_rejected_on_mutations(self):
        a = self.logged_in("fixture-andreas-admin")
        st, _, body = a.post("/admin/verktoy/fixture.echo", {"csrf": "wrong", "tekst": "x"})
        self.assertEqual((st, jbody(body)["error"]), (403, "CSRF_REJECTED"))
        st, _, body = a.post("/admin/verktoy/fixture.echo", {"csrf": a.csrf()}, headers={"Origin": "https://evil.test"})
        self.assertEqual((st, jbody(body)["error"]), (403, "ORIGIN_REJECTED"))
        st, _, _ = a.post("/logg-ut", {"csrf": a.csrf()}, headers={"Referer": "https://evil.test/x"}, send_origin=False)
        self.assertEqual(st, 403)
        self.assertEqual(a.get("/admin")[0], 200)                               # logout was not performed
        anon = self.client()
        anon.get("/logg-inn")
        st, _, _ = anon.post("/logg-inn", {"identity": "fixture-andreas-admin", "prelogin": "forged"})
        self.assertEqual(st, 403)
        anon2 = self.client()
        st, _, _ = anon2.post("/logg-inn", {"identity": "fixture-andreas-admin", "prelogin": "x"})
        self.assertEqual(st, 403)                                                # no prelogin cookie
        self.assertNotIn("cb_session", anon2.cookies)

    def test_cookie_flags(self):
        c = self.client()
        c.get("/logg-inn")
        pre = c.cookies["cb_prelogin"]
        import http.client
        from urllib.parse import urlencode
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/logg-inn", body=urlencode({"identity": "fixture-andreas-admin", "prelogin": pre}),
                     headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": self.origin,
                              "Cookie": f"cb_prelogin={pre}"})
        r = conn.getresponse()
        r.read()
        sess = [v for k, v in r.getheaders() if k.lower() == "set-cookie" and v.startswith("cb_session=")][0]
        self.assertIn("HttpOnly", sess)
        self.assertIn("SameSite=Strict", sess)
        self.assertIn(f"Max-Age={self.cfg.session_ttl_seconds}", sess)
        self.assertNotIn("Secure", sess)                                         # HTTP loopback: explicit no-Secure
        conn.close()


if __name__ == "__main__":
    unittest.main()
