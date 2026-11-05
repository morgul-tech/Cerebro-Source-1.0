"""Focused normal app composition regression; no database, network or real actor.

Runs the real deployment.app:app (there is no deployment/entry.py in r3).
The snapshot sentinel marks entry into the unchanged SQL reader, not SQL success.
P24 separately owns Linux UID/GID and enabled-public-resolver reproduction.
"""
import base64
from dataclasses import replace
import importlib
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

root = Path(sys.argv[1]).resolve()
expected = sys.argv[2]  # predecessor or corrected
sys.path.insert(0, str(root))
for key in list(os.environ):
    if key.startswith('CEREBRO_'):
        del os.environ[key]
os.environ.update({
    'CEREBRO_RESOURCE_URL':'https://composition.invalid',
    'CEREBRO_OAUTH_ISSUER':'https://issuer.composition.invalid',
    'CEREBRO_RUNTIME_DATABASE_URL':'postgresql://fixture@127.0.0.1:1/unused',
    'CEREBRO_PM_ADMISSION_PROFILE':'PM_OWNER_ADMISSION_V1',
    'CEREBRO_ATTESTATION_SECRET_B64':base64.urlsafe_b64encode(b'composition-fixture-only-32-bytes!').decode(),
})

def no_effect(event, args):
    if event in ('socket.connect', 'socket.bind', 'subprocess.Popen'):
        raise AssertionError('Unexpected external effect: '+event)
sys.addaudithook(no_effect)

deployment = importlib.import_module('deployment.app')
runtime = deployment.app._context_runtime
tools = runtime.service._tools
from control_context_tools import VerifiedMcpIdentity, McpToolCallContext
from control_context_state_postgres import PostgresControlContextStatePort
from pm_admission_owner import PostgresPmAdmissionOwner, PmAdmissionRefused
import pm_own_profile_read
from control_context_remote_runtime import assemble_postgres_control_context_remote_runtime_from_connection_factory as assemble

identity = VerifiedMcpIdentity('fixture-tenant','fixture-workspace','fixture-principal',
                              frozenset({'project_state:read'}),True)
context = McpToolCallContext(identity, {'openai/session':'synthetic-composition-session'})
owner = tools._pm_admission_owner
assert type(owner) is PostgresPmAdmissionOwner
assert tools._state_port is runtime.state_port
assert runtime.pm_lifecycle_verifier._state_port is runtime.state_port
assert runtime.pm_lifecycle_verifier._profile_verifier is owner
definitions = runtime.service.list_tools()
assert sum(x['name']=='read_own_pm_profile_v1' for x in definitions) == 1

def read(ctx=context, args=None):
    result=tools.dispatch('read_own_pm_profile_v1', {} if args is None else args, ctx)
    return result['structuredContent']

snapshot_calls=[]
def snapshot_sentinel(actual_owner, who):
    assert actual_owner is owner
    assert who['session_ref']=='chatgpt:synthetic-composition-session'
    snapshot_calls.append(True)
    raise PmAdmissionRefused('OWN_DECISION_UNKNOWN')

with patch.object(pm_own_profile_read, '_snapshot', snapshot_sentinel):
    result=read()
    if expected=='predecessor':
        assert owner.state_port is not tools._state_port
        assert result['reason']=='PROFILE_OWNER_NOT_BOUND' and not snapshot_calls
        print(json.dumps({'state':'PREDECESSOR_NORMAL_APP_REPRODUCED', 'result':result}))
        sys.exit(0)
    assert owner.state_port is tools._state_port
    assert owner.state_port._connection_factory is deployment.app._context_connection_factory
    assert result['reason']=='OWN_DECISION_UNKNOWN' and len(snapshot_calls)==1
    assert read(McpToolCallContext(replace(identity, token_verified=False),context.request_meta))['reason']=='AUTHENTICATION_OR_READ_SCOPE_REQUIRED'
    assert read(McpToolCallContext(replace(identity, scopes=frozenset()),context.request_meta))['reason']=='AUTHENTICATION_OR_READ_SCOPE_REQUIRED'
    assert read(McpToolCallContext(identity,{}))['reason']=='HOST_SESSION_MISSING_OR_INVALID'
    assert read(args={'unexpected':True})['reason']=='EMPTY_ARGUMENTS_REQUIRED'
    assert len(snapshot_calls)==1
    original_port=owner.state_port
    try:
        owner.state_port=PostgresControlContextStatePort(original_port._connection_factory)
        assert read()['reason']=='PROFILE_OWNER_NOT_BOUND'
        assert len(snapshot_calls)==1
    finally:
        owner.state_port=original_port

factory=deployment.app._context_connection_factory
def reassemble(verifier):
    return assemble(config=runtime.config, connection_factory=factory,
                    token_verifier=deployment.JwksBearerTokenVerifier(jwks_url='https://issuer.composition.invalid/jwks', algorithms=['RS256']),
                    resolution_attestation_verifier=tools._resolution_attestation_verifier,
                    pm_profile_verifier=verifier)

def assert_rejected(verifier, reason):
    try:
        reassemble(verifier)
    except Exception as error:
        assert str(error)==reason, (type(error).__name__,str(error))
    else:
        raise AssertionError('Expected constructor refusal')

foreign_factory=lambda: (_ for _ in ()).throw(AssertionError('No DB'))
assert_rejected(PostgresPmAdmissionOwner(PostgresControlContextStatePort(foreign_factory)),
                'runtime-PM-admission-connection-factory-mismatch')
class ForeignPort:
    def _transaction(self, **kwargs):
        raise AssertionError('No DB')
assert_rejected(PostgresPmAdmissionOwner(ForeignPort()), 'runtime-PM-admission-state-port-invalid')
os.environ.pop('CEREBRO_PM_ADMISSION_PROFILE')
profile_off=deployment._build_app()._context_runtime
assert profile_off.service._tools._pm_admission_owner is None
assert profile_off.state_port is profile_off.service._tools._state_port
assert profile_off.pm_lifecycle_verifier is None
print(json.dumps({'state':'CORRECTED_NORMAL_APP_COMPOSITION_REGRESSION_PASS',
 'normal_entry':'deployment.app:app', 'shared_owner_service_lifecycle_port':True,
 'registered_reader_dispatched':True,'reader_reached_unchanged_snapshot_boundary':True,
 'authentication_scope_session_arguments_and_identity_guards_retained':True,
 'foreign_factory_and_invalid_port_rejected':True, 'profile_disabled_normal_entry_pass':True,
 'actual_sql_or_oauth_verification_or_linux_uid_proof':False,
 'public_resolution_enabled_in_fixture':False,'network_database_or_candidate_actions':False}))
