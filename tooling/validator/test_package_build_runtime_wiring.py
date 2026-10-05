"""C1171 affected wiring only. Offline token/grant doubles confer no authority."""
import ast
import asyncio
import copy
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'mcp'))
sys.path.insert(0, str(ROOT / 'tooling/context'))
import control_context_remote_runtime as runtime
import control_context_remote_service as service
import control_context_mcp_sdk as sdk
from package_build_verifier_port import PackageBuildVerifierPort

NOW = 2000000000.0
RESOURCE, ISSUER = 'https://control.example.invalid', 'https://issuer.example.invalid'


class Response:
    def __init__(self, value, status_code=200, headers=None):
        self.value, self.status_code, self.headers = value, status_code, headers or {}


class Route:
    def __init__(self, path, endpoint, methods):
        self.path, self.endpoint, self.methods = path, endpoint, methods


class OfflineApp:
    async def __call__(self, *args):
        raise AssertionError('no network server in these tests')


class OfflineServer:
    def streamable_http_app(self, **kwargs):
        app = OfflineApp()
        app.routes = kwargs['custom_starlette_routes']
        return app


class Token:
    def verify(self, token):
        return service.VerifiedBearerToken(signature_verified=token != 'bad', claims={
            'iss': ISSUER, 'aud': RESOURCE, 'exp': NOW+100,
            'scope': 'package_build:verify' if token != 'state-only' else 'project_state:read',
            'sub': 'PRINCIPAL', 'cerebro_tenant': 'TENANT', 'cerebro_workspace': 'WORKSPACE'})


class WiringTests(unittest.TestCase):
    def setUp(self):
        self.config = runtime.ControlContextRemoteRuntimeConfig(service=service.RemoteMcpServiceConfig(
            resource=RESOURCE, authorization_servers=(ISSUER,), resource_documentation=RESOURCE),
            allowed_hosts=('testserver',))
        self.request = {'schema': 'cerebro-package-build-verification/v1', 'grant_ref': 'offline-grant',
            'grant_revision': '1', 'nonce': 'c'*48, 'request_sha256': 'd'*64,
            'binding': {k: 'offline-'+k for k in ('claim','packet','queue','actor','source_base',
                'current_main_commit','candidate_commit','candidate_tree','target_bytes_sha256','qualification_report_sha256')}}
        self.request['binding']['effect'] = 'RUN_ONLY_PACKAGE_BUILD'
        self.grant = {'currentness': 'CURRENT', 'revoked': False, 'result': 'PASS', 'revision': '1',
            'expires_at': NOW+60, 'tenant_ref': 'TENANT', 'workspace_ref': 'WORKSPACE',
            'principal_ref': 'PRINCIPAL', 'binding': copy.deepcopy(self.request['binding']), 'request_sha256': 'd'*64}
        self.reads = []
        def reader(ref, identity):
            self.reads.append((ref, identity))
            return copy.deepcopy(self.grant)
        self.port = PackageBuildVerifierPort(provider='OFFLINE_TEST_ONLY', grant_reader=reader, clock=lambda: NOW)
        self.attestor = SimpleNamespace(verify=lambda **kwargs: None)
        self.connection = lambda: self.fail('wiring must not open database')
        self.addCleanup(patch.stopall)
        patch.object(sdk, '_sdk_modules', return_value=(None,None,SimpleNamespace,Response,Route,None)).start()
        patch.object(sdk, 'create_official_mcp_server', return_value=OfflineServer()).start()

    def assemble(self, port=None):
        return runtime.assemble_postgres_control_context_remote_runtime_from_connection_factory(
            config=self.config, connection_factory=self.connection, token_verifier=Token(),
            resolution_attestation_verifier=self.attestor, package_build_verifier=port, clock=lambda: NOW)

    def call(self, assembled, token='good', request=None):
        route = next(r for r in assembled.app.app.routes if r.path == '/control/package-build/verify')
        self.assertEqual(route.methods, ['POST'])
        async def stream():
            yield json.dumps(self.request if request is None else request).encode()
        headers = {} if token is None else {'authorization': 'Bearer '+token}
        return asyncio.run(route.endpoint(SimpleNamespace(stream=stream, headers=headers)))

    def test_composed_endpoint_default_off_scope_auth_and_fresh_grant_refusal(self):
        off = self.assemble()
        self.assertNotIn('package_build:verify', off.service.protected_resource_metadata()['scopes_supported'])
        self.assertEqual(self.call(off).value['result'], 'HOLD')
        self.assertEqual(self.reads, [])
        bound = self.assemble(self.port)
        self.assertIn('package_build:verify', bound.service.protected_resource_metadata()['scopes_supported'])
        for token, status in ((None,401), ('bad',401), ('state-only',403)):
            response = self.call(bound, token)
            self.assertEqual(response.status_code, status)
            self.assertIn('package_build:verify', response.headers['WWW-Authenticate'])
        self.assertEqual(self.reads, [])
        response = self.call(bound)
        self.assertEqual(response.value['result'], 'PASS')
        self.assertIn('package_build:verify', self.reads[-1][1].scopes)
        current_grant = copy.deepcopy(self.grant)
        for change in ({'revoked': True}, {'currentness': 'STALE'}, {'principal_ref': 'OTHER'},
                       {'binding': {'effect': 'WORKER_ATTACH'}}):
            self.grant = {**copy.deepcopy(current_grant), **change}
            self.assertEqual(self.call(bound).value['result'], 'HOLD')
        self.assertEqual(len(self.reads), 5)  # fresh server read on every authorized request
        caller = {**self.request, 'grant': {'result': 'PASS'}}
        self.assertEqual(self.call(bound, request=caller).value['result'], 'HOLD')
        self.assertEqual(len(self.reads), 5)

    def test_dsn_constructor_forwards_exact_port_without_database_effect(self):
        with patch.object(runtime, 'make_psycopg_connection_factory', return_value=self.connection) as factory:
            assembled = runtime.assemble_postgres_control_context_remote_runtime(config=self.config,
                postgres_dsn='OFFLINE_NOT_A_CREDENTIAL', token_verifier=Token(),
                resolution_attestation_verifier=self.attestor, package_build_verifier=self.port, clock=lambda: NOW)
        factory.assert_called_once()
        self.assertEqual(self.call(assembled).value['result'], 'PASS')

    if (ROOT/'deployment/app.py').exists():
        def test_deploy_bootstrap_forwards_port_and_uses_runtime_scope_discovery(self):
            # Execute only the actual bootstrap function, avoiding environment/secret/JWKS import effects.
            tree = ast.parse((ROOT/'deployment/app.py').read_text())
            fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_build_app')
            received = []
            def assemble(**kwargs):
                received.append(kwargs)
                return SimpleNamespace(app=OfflineApp(), service=self.assemble(kwargs['package_build_verifier']).service)
            wrapper = lambda app, **kwargs: SimpleNamespace(app=app, **kwargs)
            namespace = {'Any': object, '_public_resource': lambda: (RESOURCE,'testserver'),
                '_required': lambda name: ISSUER, 'os': SimpleNamespace(environ={}), '_csv': lambda name: [],
                'ControlContextRemoteRuntimeConfig': runtime.ControlContextRemoteRuntimeConfig,
                'JwksBearerTokenVerifier': lambda **kwargs: Token(), 'HmacControlResolutionAttestor': lambda **kwargs: self.attestor,
                '_attestation_secret': lambda: b'x'*32, '_runtime_dsn': lambda: 'OFFLINE',
                '_standing_worker_grant': lambda: {'scope': 'WORKER_ATTACH'},
                'assemble_postgres_control_context_remote_runtime': assemble, 'OAuthBootstrapMiddleware': wrapper}
            exec(compile(ast.Module(body=[fn], type_ignores=[]), '<actual-bootstrap-function>', 'exec'), namespace)
            off = namespace['_build_app']()
            self.assertNotIn('package_build:verify', off.protected_resource_metadata['scopes_supported'])
            bound = namespace['_build_app'](package_build_verifier=self.port)
            self.assertIs(received[-1]['package_build_verifier'], self.port)
            self.assertEqual(received[-1]['standing_worker_grant'], {'scope': 'WORKER_ATTACH'})
            self.assertIn('package_build:verify', bound.protected_resource_metadata['scopes_supported'])


if __name__ == '__main__':
    unittest.main()
