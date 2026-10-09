"""F. bounded body / rate / methods; Norwegian labels never claim real login, private recovery, saved files,
encryption or operative unqualified tools. (Visual 320px/desktop check: tests/visual_check.py.)"""
from __future__ import annotations

import re
import unittest

from support import ServerCase, jbody

FORBIDDEN_CLAIMS = ("Lås opp med din nøkkel", "Ingen andre har tilgang", "heller ikke Cerebrobase", "kryptert",
                    "gjenopprettet", "Filen er lagret", "nye meldinger", "Operativ")


class F_Limits(ServerCase):
    env_over = {"limits": {"max_body_bytes": 512, "rate_per_minute": 30, "max_header_bytes": 8192}}

    def test_oversize_body_and_missing_length_and_media_type(self):
        a = self.logged_in("fixture-andreas-admin")
        st, _, body = a.post("/admin/verktoy/fixture.echo", {"csrf": a.csrf(), "tekst": "x" * 2000})
        self.assertEqual((st, jbody(body)["error"]), (413, "BODY_TOO_LARGE"))
        st, _, body = a.request("POST", "/logg-ut", raw_body=b"csrf=x", headers={"Content-Type": "text/plain",
                                                                                "Origin": a.origin})
        self.assertEqual(st, 415)
        st, _, body = a.request("PUT", "/rom")
        self.assertEqual(st, 405)

    def test_rate_limit_bounded(self):
        c = self.client()
        statuses = [c.get("/health")[0] for _ in range(40)]
        self.assertIn(429, statuses)
        self.assertLessEqual(statuses.count(200), 31)
        self.assertEqual(statuses[-1], 429)


class F_Labels(ServerCase):
    def test_labels_are_honest(self):
        anon = self.client()
        _, _, login = anon.get("/logg-inn")
        t = login.decode()
        self.assertIn("ikke ekte innlogging", t)
        self.assertIn("Testidentitet: Andreas (ADMIN)", t)
        a = self.logged_in("fixture-andreas-admin")
        m = self.logged_in("fixture-marianne-pilot")
        pages = [t, a.get("/rom/room-fixture-andreas-admin")[2].decode(), a.get("/admin")[2].decode(),
                 m.get("/rom/room-fixture-marianne-pilot")[2].decode()]
        for p in pages:
            for claim in FORBIDDEN_CLAIMS:
                self.assertNotIn(claim, p)
            self.assertIn('lang="nb"', p)
        self.assertIn("Ikke tilgjengelig ennå", pages[1])
        self.assertIn("Ingen filer er lagret", pages[3])
        for p in pages:                                            # every form control is labelled/named
            for inp in re.findall(r"<input [^>]*type=\"text\"[^>]*>", p):
                ident = re.search(r'id="([^"]+)"', inp).group(1)
                self.assertIn(f'for="{ident}"', p)


if __name__ == "__main__":
    unittest.main()
