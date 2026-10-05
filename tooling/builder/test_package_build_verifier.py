"""New port/binder tests only; synthetic owner and token data, no package/PG/network effects."""
import copy
import asyncio
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT / "mcp", ROOT / "tooling/context"):
    sys.path.insert(0, str(path))
from package_build_verifier import PackageBuildVerifier, VerifierError, canonical
from package_build_verifier_port import PackageBuildVerifierPort, PackageBuildPortError
from control_context_remote_service import (ControlContextRemoteMcpService, RemoteMcpServiceConfig,
    VerifiedBearerToken, RemoteMcpAuthenticationError)
from control_context_tools import ControlContextToolError
import standard_delivery_package as builder
import control_context_mcp_sdk as sdk


class PortTests(unittest.TestCase):
    def setUp(self):
        self.declared = {"authorization": {"claim": "C1", "packet": "P1", "queue": "Q1",
            "actor": "ACTOR", "effect": "RUN_ONLY_PACKAGE_BUILD", "pm_bound": True,
            "qualification_report_sha256": "a" * 64},
            "source": {"base_commit": builder.EXACT35_BASE, "current_main_commit": "b" * 40,
                       "candidate_commit": builder.EXACT35_CANDIDATE, "candidate_tree": builder.EXACT35_TREE},
            "identity": {"profile": builder.EXACT35_PROFILE, "target_bytes_sha256": "c" * 64},
            "output": {"root": "synthetic-output"}}
        self.config = {"URL": "https://control.invalid/control/package-build/verify", "TOKEN": "synthetic",
                       "PROVIDER": "CURRENT_PM", "GRANT": "G1", "REVISION": "R1"}
        self.claims = {"iss": "https://issuer.invalid", "aud": "https://control.invalid", "exp": 200,
            "scope": "package_build:verify", "sub": "PM-BUILDER", "cerebro_tenant": "T1", "cerebro_workspace": "W1"}
        self.reader_calls = []
        self.grant_change = {}
        def reader(ref, identity):
            self.reader_calls.append((ref, identity.principal_ref))
            return {"result": "PASS", "revision": "R1", "currentness": "CURRENT", "revoked": False,
                    "expires_at": 120, "tenant_ref": "T1", "workspace_ref": "W1", "principal_ref": "PM-BUILDER",
                    "binding": copy.deepcopy(self.expected_binding),
                    "request_sha256": hashlib.sha256(canonical(self.declared)).hexdigest(), **self.grant_change}
        self.port = PackageBuildVerifierPort(provider="CURRENT_PM", grant_reader=reader, clock=lambda: 100)
        verifier = SimpleNamespace(verify=lambda token: VerifiedBearerToken(self.claims, token == "synthetic"))
        self.service = ControlContextRemoteMcpService(
            config=RemoteMcpServiceConfig(resource="https://control.invalid", authorization_servers=("https://issuer.invalid",),
                resource_documentation="https://control.invalid/docs", clock_skew_seconds=0),
            tools=SimpleNamespace(dispatch=lambda *a: None), token_verifier=verifier,
            readiness_probe=lambda: True, clock=lambda: 100, package_build_verifier=self.port)
        def transport(request):
            self.last_request = copy.deepcopy(request)
            return self.service.verify_run_only_package_build(args=request, headers={"Authorization": "Bearer synthetic"})
        self.client = PackageBuildVerifier(self.config, clock=lambda: 100, transport=transport)
        # Generate independently owned expected grant binding before exercising server.
        self.expected_binding = {"effect": "RUN_ONLY_PACKAGE_BUILD", "claim": "C1", "packet": "P1", "queue": "Q1", "actor": "ACTOR",
            "source_base": builder.EXACT35_BASE, "current_main_commit": "b" * 40,
            "candidate_commit": builder.EXACT35_CANDIDATE, "candidate_tree": builder.EXACT35_TREE,
            "target_bytes_sha256": "c" * 64, "qualification_report_sha256": "a" * 64}

    def test_authenticated_server_client_and_fresh_reread(self):
        self.assertEqual(self.client(self.declared)["result"], "PASS")
        self.assertEqual(self.client(self.declared)["current_main_commit"], "b" * 40)
        self.assertEqual(len(self.reader_calls), 2)
        self.grant_change["revoked"] = True
        with self.assertRaises(PackageBuildPortError):
            self.client(self.declared)

    def test_authority_never_from_caller_fields(self):
        self.grant_change["result"] = "HOLD"
        with self.assertRaises(PackageBuildPortError):
            self.client(self.declared)
        self.service._package_build_verifier = None
        with self.assertRaises(ControlContextToolError):
            self.client(self.declared)
        with patch.dict("os.environ", {}, clear=True), self.assertRaises(VerifierError):
            PackageBuildVerifier.from_environment()

    def test_oauth_signature_scope_issuer_audience_expiry(self):
        self.client(self.declared)
        for key, value in (("scope", "project_state:read"), ("iss", "https://wrong.invalid"),
                           ("aud", "https://wrong.invalid"), ("exp", 99)):
            with self.subTest(key=key), patch.dict(self.claims, {key: value}):
                with self.assertRaises(RemoteMcpAuthenticationError):
                    self.client(self.declared)
        with self.assertRaises(RemoteMcpAuthenticationError):
            self.service.verify_run_only_package_build(args=self.last_request, headers={"Authorization": "Bearer forged"})

    def test_owner_revision_currentness_identity_and_execution_scope(self):
        for key, value in (("revision", "old"), ("currentness", "STALE"), ("expires_at", 99),
                           ("principal_ref", "other"), ("tenant_ref", "other"), ("workspace_ref", "other"),
                           ("request_sha256", "d" * 64), ("binding", {})):
            with self.subTest(key=key):
                self.grant_change = {key: value}
                with self.assertRaises(PackageBuildPortError):
                    self.client(self.declared)
        self.grant_change = {}
        changed = copy.deepcopy(self.declared)
        changed["output"]["root"] = "different-execution"
        with self.assertRaises(PackageBuildPortError):
            self.client(changed)

    def test_client_rejects_forged_stale_replayed_response_and_redirect_url(self):
        good = self.client(self.declared)
        for change in ({"nonce": "old"}, {"provider": "caller"}, {"revoked": True},
                       {"expires_at": 99}, {"issued_at": 80}, {"expires_at": 200}, {"binding": {}}):
            with self.subTest(change=change):
                def forged(request):
                    return {**good, **request, **change}
                self.client.transport = forged
                with self.assertRaises(VerifierError):
                    self.client(self.declared)
        with self.assertRaises(VerifierError):
            PackageBuildVerifier({**self.config, "URL": "http://control.invalid/control/package-build/verify"})

    def test_normal_cli_passes_bound_callback_without_package_effect(self):
        self.declared["input"] = "synthetic"
        def no_build(declared, root, files, target, **kwargs):
            return kwargs["trusted_authority_check"](declared)
        with patch.object(sys, "argv", ["builder", "build", "--input", "fixture.json"]), \
             patch.object(builder, "load_declared_input", return_value=self.declared), \
             patch.object(builder, "exact35_candidate", return_value=(Path("synthetic"), [], "c" * 64)), \
             patch.object(builder.PackageBuildVerifier, "from_environment", return_value=self.client), \
             patch.object(builder, "write_bundle_atomically_outside_source", side_effect=no_build), \
             patch("builtins.print"):
            self.assertEqual(builder.main(), 0)
        # Grant reader must authorize full declared bytes, rather than receipt pm_bound/hash fixtures.
        self.assertEqual(self.last_request["request_sha256"], hashlib.sha256(canonical(self.declared)).hexdigest())

    def test_registered_http_route_refuses_revoked_and_oversize_requests(self):
        self.client(self.declared)
        captured = {}
        class Route:
            def __init__(self, path, endpoint, **kwargs):
                self.path, self.endpoint = path, endpoint
        class Response:
            def __init__(self, value, status_code=200, **kwargs):
                self.value, self.status_code = value, status_code
        class Server:
            def streamable_http_app(self, **kwargs):
                captured.update(kwargs)
                return lambda *a: None
        class Request:
            headers = {"Authorization": "Bearer synthetic"}
            def __init__(self, raw):
                self.raw = raw
            async def stream(self):
                yield self.raw
        with patch.object(sdk, "_sdk_modules", return_value=(None, None, lambda **k: None, Response, Route, None)), \
             patch.object(sdk, "create_official_mcp_server", return_value=Server()):
            sdk.create_streamable_http_app(self.service)
        route = next(r for r in captured["custom_starlette_routes"] if r.path == "/control/package-build/verify")
        self.assertEqual(asyncio.run(route.endpoint(Request(canonical(self.last_request)))).status_code, 200)
        self.grant_change["revoked"] = True
        self.assertEqual(asyncio.run(route.endpoint(Request(canonical(self.last_request)))).status_code, 403)
        self.assertEqual(asyncio.run(route.endpoint(Request(b"x" * 65537))).status_code, 413)

    def test_historical_parent_cannot_be_reported_as_current_main(self):
        declared = copy.deepcopy(self.declared)
        declared["source"].update(root=str(ROOT), repository=builder.REPOSITORY, branch="main")
        def command(root, *args):
            if args[1:] == ("rev-parse", "--show-toplevel"):
                return str(ROOT)
            if "get-url" in args:
                return "https://github.com/morgul-tech/Cerebro-Source-1.0.git"
            if "symbolic-ref" in args:
                return "main"
            if "ls-remote" in args:
                return "b" * 40 + " refs/heads/main"
            return "b" * 40
        declared["source"]["current_main_commit"] = builder.EXACT35_BASE
        with patch.object(builder, "command", side_effect=command), self.assertRaisesRegex(
                builder.BuilderError, "main currentness mismatch"):
            builder.exact35_candidate(declared)


if __name__ == "__main__":
    unittest.main()
