"""C1156 new risk tests only. All sessions/proofs are labelled OFFLINE doubles.
No original artifact bytes, credentials, Google calls or installed qualification.
"""
import json
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

import _support  # noqa: F401
from signalvev_client import drive_owner_resolver as d
from signalvev_client.session import load_resolver
from signalvev_client.config import parse_config
from signalvev_sensing.resolver import ResolveRequest, ResolverUnavailable

BODY = b"offline first line\noffline selected line\nnot selected\n"
SHA = d.hashlib.sha256(BODY).hexdigest()
NOW = 2000.0


class Response:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status
        self.closed = False

    def iter_content(self, chunk_size):
        yield self.body

    def close(self):
        self.closed = True


class Session:
    SYNTHETIC_TEST_ONLY = True

    def __init__(self):
        self.calls = []
        self.responses = []
        self.status = 200
        self.principal = "permission:reader"
        self.revision = d.REVISION
        self.version = "99999"  # deliberately not the owner's seq7
        self.body = BODY
        self.allowed = True

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url == d.API + "/about":
            payload = {"user": {"permissionId": self.principal}}
        elif "/revisions/" in url:
            payload = None
        else:
            payload = {"id": d.FILE_ID, "mimeType": "text/markdown", "headRevisionId": self.revision,
                       "version": self.version, "trashed": False, "capabilities": {"canDownload": self.allowed}}
        response = Response(self.body if payload is None else json.dumps(payload).encode(), self.status)
        self.responses.append(response)
        return response


class ProofPort:
    SYNTHETIC_TEST_ONLY = True

    def __init__(self):
        self.calls = 0
        self.change = {}
        self.after = None

    def confirm_current(self, request, observation, *, nonce):
        self.calls += 1
        proof = d.OwnerConfirmation(nonce, d._fingerprint(asdict(request)), d._fingerprint(asdict(observation)),
            request.owner_ref, "owner-proof:host-custody", "owner-receipt:fresh", "SAME", 7, NOW, NOW + 15)
        if self.after:
            self.after()
        return replace(proof, **self.change)


class DriveOwnerResolverTests(unittest.TestCase):
    def setUp(self):
        self.pin = patch.object(d, "SOURCE_SHA256", SHA)
        self.pin.start()
        self.addCleanup(self.pin.stop)
        self.addCleanup(d.configure_host, None)
        self.scope = d.DriveScope("owner:C2", "ARTIFACT", "artifact:original", "drive:original",
                                  sha256=SHA, line_count=2)
        self.session, self.proof = Session(), ProofPort()
        self.binding = d.HostBinding(self.scope, Path.cwd() / "unused-host-credential.json",
                                     "permission:reader", "owner-proof:host-custody", self.proof, True)
        self.resolver = d.DriveOwnerResolver(self.binding,
            d.GoogleDriveReadProvider(self.session, self.binding.principal_permission_id), clock=lambda: NOW)
        self.request = ResolveRequest("event:case", self.scope.owner_ref, self.scope.referent_type,
            self.scope.referent_id, "POINTER_GROUND", self.scope.pointer_ref, self.scope.revision, SHA, 7)

    def test_factory_is_default_off_on_existing_load_resolver_seam(self):
        cfg = self.factory_cfg()
        d.configure_host(None)
        with self.assertRaisesRegex(Exception, "RESOLVER_FACTORY"):
            load_resolver(cfg)
        self.assertEqual(self.session.calls, [])

    def test_missing_owner_proof_refuses_before_read(self):
        resolver = d.DriveOwnerResolver(replace(self.binding, owner_confirmation=None),
            d.GoogleDriveReadProvider(self.session, self.binding.principal_permission_id))
        with self.assertRaisesRegex(ResolverUnavailable, "OWNER_CONFIRMATION_PORT_UNBOUND"):
            resolver.resolve(self.request)
        self.assertEqual(self.session.calls, [])

    def test_runtime_factory_rejects_labelled_fixture_proof_before_credentials(self):
        cfg = self.factory_cfg()
        d.configure_host(self.binding)
        with patch.object(d, "_authorized_session") as credential:
            with self.assertRaisesRegex(ResolverUnavailable, "OWNER_CONFIRMATION_PORT_UNBOUND"):
                d.create_resolver(cfg)
            credential.assert_not_called()

    def factory_cfg(self):
        return parse_config(_support.config_doc(Path.cwd(),
            resolver={"kind": "factory", "factory": "signalvev_client.drive_owner_resolver:create_resolver"},
            listen={"interest": [{"owner_ref": self.scope.owner_ref, "referent_type": self.scope.referent_type,
                                   "referent_id": self.scope.referent_id}]}), check_files=False)

    def test_exact_trusted_read_selects_original_and_owner_seq_not_drive_version(self):
        result = self.resolver.resolve(self.request)
        self.assertEqual((result.current_revision, result.observed_sha256, result.owner_seq), (d.REVISION, SHA, 7))
        self.assertEqual(result.grounding["text"], "offline first line\noffline selected line")
        self.assertEqual(result.grounding["owner_receipt_ref"], "owner-receipt:fresh")
        self.assertEqual(self.proof.calls, 1)
        self.assertTrue(all(r.closed for r in self.session.responses))
        self.assertTrue(all(u.startswith(d.API + "/") and kw["allow_redirects"] is False
                            for u, kw in self.session.calls))
        self.assertEqual(sum("/revisions/" in u for u, _ in self.session.calls), 1)

    def test_auth_denied_unavailable_or_redirect_has_no_grounding(self):
        for status in (401, 403, 302, 503):
            with self.subTest(status=status):
                self.session.status = status
                with self.assertRaisesRegex(ResolverUnavailable, "DRIVE_AUTH_OR_ACCESS_UNAVAILABLE"):
                    self.resolver.resolve(self.request)
        with patch.object(self.session, "get", side_effect=TimeoutError("private-token-must-not-leak")):
            with self.assertRaisesRegex(ResolverUnavailable, "^DRIVE_READ_UNAVAILABLE$"):
                self.resolver.resolve(self.request)
        self.assertEqual(self.proof.calls, 0)

    def test_wrong_principal_or_acl_refuses(self):
        self.session.principal = "permission:other"
        with self.assertRaisesRegex(ResolverUnavailable, "DRIVE_PRINCIPAL_MISMATCH"):
            self.resolver.resolve(self.request)
        self.session.principal, self.session.allowed = self.binding.principal_permission_id, False
        with self.assertRaisesRegex(ResolverUnavailable, "DRIVE_SOURCE_ACCESS_DENIED"):
            self.resolver.resolve(self.request)

    def test_wrong_referent_revision_or_hash_never_selects_alternate(self):
        for change in ({"referent_id": "artifact:other"}, {"pointer_ref": "https://evil.test"},
                       {"expected_revision": "other"}, {"expected_sha256": "f" * 64}):
            with self.subTest(change=change):
                with self.assertRaisesRegex(ResolverUnavailable, "EXACT_REFERENT_BINDING_MISMATCH"):
                    self.resolver.resolve(replace(self.request, **change))
        self.assertEqual(self.session.calls, [])
        self.session.revision = "different-head"
        with self.assertRaisesRegex(ResolverUnavailable, "ORIGINAL_REVISION_NO_LONGER_CURRENT"):
            self.resolver.resolve(self.request)
        self.session.revision, self.session.body = d.REVISION, b"altered payload"
        with self.assertRaisesRegex(ResolverUnavailable, "ORIGINAL_HASH_MISMATCH"):
            self.resolver.resolve(self.request)
        self.assertEqual(self.proof.calls, 0)

    def test_stale_or_wrong_owner_confirmation_refuses(self):
        for change in ({"issued_at": NOW - 50}, {"expires_at": NOW}, {"nonce": "old"},
                       {"owner_seq": 99999}, {"relation": "SUPERSEDED"}, {"owner_ref": "owner:other"},
                       {"observation_sha256": "f" * 64}, {"request_sha256": "f" * 64},
                       {"proof_ref": "owner-proof:other"}):
            with self.subTest(change=change):
                self.proof.change = change
                with self.assertRaisesRegex(ResolverUnavailable, "OWNER_CONFIRMATION_STALE_OR_MISMATCHED"):
                    self.resolver.resolve(self.request)

    def test_changed_source_after_owner_confirmation_refuses(self):
        self.proof.after = lambda: setattr(self.session, "version", "100000")
        with self.assertRaisesRegex(ResolverUnavailable, "DRIVE_CHANGED_AFTER_OWNER_CONFIRMATION"):
            self.resolver.resolve(self.request)

    def test_credential_origin_and_missing_reference_refuse_without_sdk(self):
        with self.assertRaisesRegex(ResolverUnavailable, "DRIVE_CREDENTIAL_REFERENCE_UNAVAILABLE"):
            d._authorized_session(Path("relative.json"))
        with tempfile.TemporaryDirectory() as temp:
            ref = Path(temp) / "host-credential.json"
            ref.write_text(json.dumps({"type": "authorized_user", "token_uri": "https://evil.test/token"}))
            with self.assertRaisesRegex(ResolverUnavailable, "DRIVE_CREDENTIAL_ORIGIN_REFUSED"):
                d._authorized_session(ref)

    def test_bounded_source_and_provider_failure_never_reach_owner_proof(self):
        self.session.body = b"x" * (d.MAX_SOURCE_BYTES + 1)
        with self.assertRaisesRegex(ResolverUnavailable, "DRIVE_READ_BOUND_EXCEEDED"):
            self.resolver.resolve(self.request)
        self.assertEqual(self.proof.calls, 0)
        self.assertTrue(all(r.closed for r in self.session.responses))

    def test_owner_confirmation_unavailable_refuses_without_private_error(self):
        with patch.object(self.proof, "confirm_current", side_effect=RuntimeError("private-proof-details")):
            with self.assertRaisesRegex(ResolverUnavailable, "^OWNER_CONFIRMATION_UNAVAILABLE$"):
                self.resolver.resolve(self.request)


if __name__ == "__main__":
    unittest.main()
