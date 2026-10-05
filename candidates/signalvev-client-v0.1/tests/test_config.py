"""Configuration: bounded, offline, fail-closed, no inline secrets."""
import json
import unittest

from _support import TmpCase, config_doc
from signalvev_client import ConfigError, load_config, parse_config


class ConfigTests(TmpCase):
    def parse(self, **over):
        return parse_config(config_doc(self.tmp, **over), base_dir=self.tmp)

    def test_valid_minimal_and_defaults(self):
        cfg = self.parse()
        self.assertEqual((cfg.node_id, cfg.ttl_seconds, cfg.max_queue), ("node-test", 30, 8))
        self.assertEqual(cfg.resolver_kind, "synthetic_fixture")
        self.assertFalse(cfg.tls_requested)

    def test_public_summary_never_contains_secret_paths_or_contents(self):
        creds = self.tmp / "node.creds"
        creds.write_text("-----BEGIN NATS USER JWT-----\nSUPERSECRETJWT\n")
        cfg = self.parse(nats={"credentials_file": str(creds)})
        text = json.dumps(cfg.public_summary())
        self.assertIn('"credentials_file": "configured"', text)
        self.assertNotIn("SUPERSECRETJWT", text)
        self.assertNotIn("node.creds", text)

    def test_missing_sections_and_unknown_keys_fail_closed(self):
        for doc, code in ((config_doc(self.tmp) | {"nats": None}, "MISSING_SECTION"),
                          (config_doc(self.tmp) | {"extra": {}}, "UNKNOWN_SECTION"),
                          (config_doc(self.tmp, nats={"turbo": True}), "UNKNOWN_KEY")):
            with self.assertRaises(ConfigError) as cm:
                parse_config(doc, base_dir=self.tmp)
            self.assertEqual(cm.exception.code, code)

    def test_inline_secret_keys_and_userinfo_urls_are_refused(self):
        for key in ("password", "token", "nkey_seed", "jwt", "user"):
            with self.assertRaises(ConfigError) as cm:
                self.parse(nats={key: "x"})
            self.assertEqual(cm.exception.code, "INLINE_SECRET_NOT_ALLOWED", key)
        for url in ("nats://user:pw@127.0.0.1:4222", "nats://tok@127.0.0.1:4222"):
            with self.assertRaises(ConfigError) as cm:
                self.parse(nats={"server": url})
            self.assertEqual(cm.exception.code, "INLINE_SECRET_NOT_ALLOWED", url)

    def test_server_must_be_a_plain_nats_or_tls_url(self):
        for url in ("http://127.0.0.1:4222", "127.0.0.1:4222", "nats://", "nats://127.0.0.1:99999", "nats://h/path?x=1", ""):
            with self.assertRaises(ConfigError, msg=url):
                self.parse(nats={"server": url})

    def test_plaintext_to_non_loopback_needs_explicit_opt_in_or_tls(self):
        with self.assertRaises(ConfigError) as cm:
            self.parse(nats={"server": "nats://10.1.2.3:4222"})
        self.assertEqual(cm.exception.code, "PLAINTEXT_NON_LOOPBACK_REFUSED")
        self.assertTrue(self.parse(nats={"server": "nats://10.1.2.3:4222", "allow_plaintext": True}).allow_plaintext)
        self.assertTrue(self.parse(nats={"server": "tls://nats.example.invalid:4222"}).tls_requested)
        self.assertEqual(self.parse(nats={"server": "nats://localhost:4222"}).server, "nats://localhost:4222")
        self.assertEqual(self.parse(nats={"server": "nats://[::1]:4222"}).server, "nats://[::1]:4222")

    def test_bounds(self):
        for section, key, bad in (("nats", "connect_timeout_seconds", 0), ("nats", "flush_timeout_seconds", 999),
                                  ("nats", "max_reconnect_attempts", -1), ("send", "ttl_seconds", 0),
                                  ("send", "ttl_seconds", 3601), ("send", "ttl_seconds", True),
                                  ("listen", "max_queue", 0), ("listen", "max_queue", 10_001)):
            with self.assertRaises(ConfigError, msg=f"{section}.{key}={bad}") as cm:
                self.parse(**{section: {key: bad}})
            self.assertEqual(cm.exception.code, "VALUE_OUT_OF_BOUNDS")

    def test_node_id_and_interest_grammar(self):
        for bad in ("", "has space", "x" * 200, "-lead"):
            with self.assertRaises(ConfigError):
                self.parse(node={"id": bad})
        with self.assertRaises(ConfigError):
            parse_config(config_doc(self.tmp) | {"listen": {"interest": [{"owner_ref": "o"}]}}, base_dir=self.tmp)

    def test_referenced_files_must_exist_and_cert_key_go_together(self):
        with self.assertRaises(ConfigError) as cm:
            self.parse(nats={"credentials_file": "nope.creds"})
        self.assertEqual(cm.exception.code, "FILE_NOT_FOUND")
        cert = self.tmp / "c.pem"
        cert.write_text("x")
        with self.assertRaises(ConfigError) as cm:
            self.parse(nats={"tls_cert_file": str(cert)})
        self.assertEqual(cm.exception.code, "TLS_CERT_KEY_PAIR_INCOMPLETE")
        # check_files=False is the offline-template mode: shape only
        parse_config(config_doc(self.tmp, nats={"credentials_file": "nope.creds"}), base_dir=self.tmp, check_files=False)

    def test_resolver_kinds(self):
        with self.assertRaises(ConfigError):
            self.parse(resolver={"kind": "magic"})
        with self.assertRaises(ConfigError):
            self.parse(resolver={"kind": "factory"})
        with self.assertRaises(ConfigError):
            self.parse(resolver={"kind": "synthetic_fixture", "factory": "a:b"})
        self.assertEqual(self.parse(resolver={"kind": "factory", "factory": "pkg.mod:make"}).resolver_factory, "pkg.mod:make")

    def test_load_config_toml_and_relative_paths(self):
        (self.tmp / "ev").mkdir()
        p = self.tmp / "c.toml"
        p.write_text('[node]\nid = "n1"\n[nats]\nserver = "nats://127.0.0.1:4222"\n[evidence]\ndir = "ev"\n')
        cfg = load_config(p)
        self.assertEqual(cfg.evidence_dir, (self.tmp / "ev").resolve())
        for content, code in (("not [valid", "CONFIG_NOT_VALID_TOML"), ("x" * 70_000, "CONFIG_TOO_LARGE")):
            p.write_text(content)
            with self.assertRaises(ConfigError) as cm:
                load_config(p)
            self.assertEqual(cm.exception.code, code)
        with self.assertRaises(ConfigError) as cm:
            load_config(self.tmp / "missing.toml")
        self.assertEqual(cm.exception.code, "CONFIG_UNREADABLE")

    def test_shipped_example_configs_parse(self):
        import tomllib
        from _support import HERE
        examples = sorted((HERE.parent / "examples").glob("*.toml"))
        self.assertTrue(examples, "examples/*.toml missing")
        for example in examples:
            cfg = parse_config(tomllib.loads(example.read_text(encoding="utf-8")), base_dir=self.tmp, check_files=False)
            self.assertEqual(cfg.server, "nats://127.0.0.1:4222")
            self.assertIsNone(cfg.credentials_file)      # examples carry no credentials

if __name__ == "__main__":
    unittest.main()
