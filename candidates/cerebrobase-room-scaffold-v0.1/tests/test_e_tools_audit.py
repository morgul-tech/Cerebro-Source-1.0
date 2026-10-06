"""E. tool registry: fixture tool only with explicit scope; pilot/admin-without-scope/disabled deny; unqualified
external tools never invoke; audit, errors and logs carry no canary body, session, CSRF or secret bytes."""
from __future__ import annotations

import unittest

from support import CANARY_BODY, ServerCase, jbody
from cerebrobase import tools

ADMIN_ROOM = "room-fixture-andreas-admin"


class E_Tools(ServerCase):
    def test_fixture_tool_allowed_with_explicit_scope_and_redacted_receipt(self):
        a = self.logged_in("fixture-andreas-admin")
        st, _, body = a.post("/admin/verktoy/fixture.echo", {"csrf": a.csrf(), "tekst": CANARY_BODY})
        self.assertEqual(st, 200)
        self.assertIn("Testverktøy kjørt (syntetisk)".encode(), body)
        self.assertNotIn(CANARY_BODY.encode(), body)
        con = self.db()
        rows = [dict(r) for r in con.execute("SELECT * FROM audit_events WHERE kind='TOOL_INVOKE'")]
        con.close()
        self.assertEqual([(r["outcome"], r["tool_id"], r["room_id"]) for r in rows], [("OK", "fixture.echo", ADMIN_ROOM)])
        self.assertEqual(set(rows[0]), {"seq", "event_id", "at", "kind", "outcome", "account_id", "room_id", "tool_id"})

    def test_denials_pilot_no_scope_disabled_unqualified(self):
        calls = []
        orig = tools.FixtureEchoAdapter.invoke
        tools.FixtureEchoAdapter.invoke = lambda self_, p: calls.append(1) or orig(self_, p)
        self.addCleanup(setattr, tools.FixtureEchoAdapter, "invoke", orig)
        m = self.logged_in("fixture-marianne-pilot")
        st, _, body = m.post("/admin/verktoy/fixture.echo", {"csrf": m.csrf(), "tekst": CANARY_BODY})
        self.assertEqual((st, jbody(body)["code"]), (403, "ROLE_REQUIRED"))
        a = self.logged_in("fixture-andreas-admin")
        for tid in ("postkasse.lese", "signalvev.status", "drive.navigasjon"):
            st, _, body = a.post(f"/admin/verktoy/{tid}", {"csrf": a.csrf()})
            self.assertEqual(st, 409)
            self.assertIn(b"TOOL_UNQUALIFIED", body)
        st, _, body = a.post("/admin/verktoy/shell.exec", {"csrf": a.csrf()})
        self.assertEqual(st, 404)
        con = self.db()
        con.execute("DELETE FROM capabilities WHERE capability='tool:fixture.echo'")
        st, _, body = a.post("/admin/verktoy/fixture.echo", {"csrf": a.csrf(), "tekst": CANARY_BODY})
        self.assertIn(b"CAPABILITY_REQUIRED", body)
        con.execute("INSERT INTO capabilities VALUES ('acc-fixture-andreas-admin', ?, 'tool:fixture.echo', 1)",
                    (ADMIN_ROOM,))
        object.__setattr__(self.cfg, "capabilities", {"tools": {"fixture.echo": False}})
        st, _, body = a.post("/admin/verktoy/fixture.echo", {"csrf": a.csrf(), "tekst": CANARY_BODY})
        self.assertIn(b"TOOL_DISABLED_BY_CONFIG", body)
        self.assertEqual(calls, [])                                              # nothing invoked on any denial
        outcomes = [r[0] for r in con.execute("SELECT outcome FROM audit_events WHERE kind='TOOL_INVOKE'")]
        con.close()
        self.assertTrue(all(o.startswith("DENIED:") for o in outcomes))
        self.assertEqual(len(outcomes), 7)

    def test_ui_marks_unqualified_tools_unavailable(self):
        a = self.logged_in("fixture-andreas-admin")
        _, _, body = a.get("/admin")
        text = body.decode()
        self.assertEqual(text.count("Ikke kvalifisert — utilgjengelig"), 3)
        self.assertNotIn('action="/admin/verktoy/postkasse.lese"', text)

    def test_no_canary_session_csrf_in_audit_logs_errors(self):
        a = self.logged_in("fixture-andreas-admin")
        a.post("/admin/verktoy/fixture.echo", {"csrf": a.csrf(), "tekst": CANARY_BODY})
        _, _, err = a.post("/admin/verktoy/fixture.echo", {"csrf": "bad", "tekst": CANARY_BODY})   # typed error
        _, _, page = a.post("/admin/verktoy/postkasse.lese", {"csrf": a.csrf(), "tekst": CANARY_BODY})
        self.assertNotIn(CANARY_BODY.encode(), page)          # own page carries the user's CSRF by design, no body
        token, csrf = a.cookies["cb_session"], a.cookies["cb_csrf"]
        con = self.db()
        dump = "\n".join(str(tuple(r)) for t in ("audit_events", "sessions")
                         for r in con.execute(f"SELECT * FROM {t}"))
        con.close()
        a.get(f"/rom/{ADMIN_ROOM}?token={CANARY_BODY}")                          # query strings never logged
        logs = self.log_text()
        self.assertTrue(logs)
        for blob in (dump, logs, err.decode()):
            for secret in (CANARY_BODY, token, csrf):
                self.assertNotIn(secret, blob)
        _, _, health = self.client().get("/health")
        self.assertNotIn(token.encode(), health)


if __name__ == "__main__":
    unittest.main()
