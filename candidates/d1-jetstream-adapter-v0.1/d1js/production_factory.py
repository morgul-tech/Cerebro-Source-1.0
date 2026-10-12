"""D3-compatible actual factory: installation-owned REF map, HTTPS issuer/Context and role-scoped NATS.

No missing-port success, synthetic host, implicit login or credential creation.
Config/custody refs navigate trusted installation input; Context authenticates and
checks real persisted service assignment/lease before any broker connection.
"""
from __future__ import annotations
import json,os,stat
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request
from .private_http import private_open
from .production_config import ProductionD1Config,PROFILE
from .production_runtime import ProductionRuntime,ProductionRuntimeRefused,REF
from .context_service_client import AuthenticatedContextServiceClient
from .nats_transport import NatsJetStreamTransport
from .private_token_state import ProtectedRoleTokenStore
from .token_lifecycle import TokenLifecycle,binding_for

def private_environment(admission):
    if 'private_env_file' not in admission:return os.environ
    try:
        path=Path(admission['private_env_file'])
        if not path.is_absolute() or not hasattr(os,'geteuid'):raise ValueError()
        for p in [path,*path.parents]:
            if p.is_symlink() or p.is_junction():raise ValueError()
            s=p.stat()
            if s.st_uid not in (0,os.geteuid()) or stat.S_IMODE(s.st_mode)&0o022:raise ValueError()
        info=path.stat()
        if info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)&0o077 or info.st_size>65536:raise ValueError()
        result=json.loads(path.read_bytes())
        required={'CEREBRO_BK11_PRODUCTION_RUNTIME_JSON','CEREBRO_BK11_CREDENTIAL_REFS_JSON',
          'CEREBRO_BK11_AUTH0_CLIENT_ID','CEREBRO_BK11_AUTH0_CLIENT_SECRET','CEREBRO_BK11_NATS_CREDS_PATH'}
        if type(result)is not dict or set(result)!=required or not all(type(v)is str and v for v in result.values()):raise ValueError()
        return result
    except Exception:raise ProductionRuntimeRefused('ROLE_PRIVATE_INSTALLED_ENV_CUSTODY_UNAVAILABLE') from None

class PrivateCredentialCustody:
    def __init__(self,mapping:dict,environ=None,opener=private_open):
        self._mapping=mapping;self._env=os.environ if environ is None else environ;self._open=opener

    def token(self,ref:str)->str:
        try:
            entry=self._mapping[ref]
            client_id=self._env[entry['client_id_env']]
            binding=binding_for(entry,ref,client_id)
            issuer=urlsplit(entry['issuer'])
            if issuer.scheme!='https' or not issuer.hostname or issuer.username or issuer.password or issuer.query or issuer.fragment or issuer.path not in ('','/'):raise ValueError()
            store=ProtectedRoleTokenStore(entry['token_state_dir'],entry['role'])
            def acquire():
                secret=self._env[entry['client_secret_env']]
                req=Request(entry['issuer'].rstrip('/')+'/oauth/token',method='POST',
                  data=json.dumps({'grant_type':'client_credentials','client_id':client_id,'client_secret':secret,
                                   'audience':entry['resource']}).encode(),headers={'Content-Type':'application/json'})
                with self._open(req,timeout=5) as response:raw=response.read(65537)
                if len(raw)>65536:raise ValueError()
                return json.loads(raw)
            return TokenLifecycle(store).token(binding,acquire)
        except Exception:raise ProductionRuntimeRefused('PRIVATE_ISSUER_TOKEN_CUSTODY_UNAVAILABLE') from None

    def nats_file(self,ref:str,role:str)->str:
        try:
            entry=self._mapping[ref]
            if set(entry)!={'kind','role','path_env'} or entry['kind']!='NATS_USER_CREDS_FILE' or entry['role']!=role:raise ValueError()
            path=Path(self._env[entry['path_env']])
            if not path.is_absolute() or not path.is_file():raise ValueError()
            for p in [path,*path.parents]:
                if p.is_symlink() or p.is_junction():raise ValueError()
                ancestor=p.stat()
                if hasattr(os,'geteuid') and (ancestor.st_uid not in (0,os.geteuid()) or stat.S_IMODE(ancestor.st_mode)&0o022):raise ValueError()
            s=path.stat()
            if not hasattr(os,'geteuid') or s.st_uid!=os.geteuid() or stat.S_IMODE(s.st_mode)&0o077:raise ValueError()
            return str(path)
        except Exception:raise ProductionRuntimeRefused('ROLE_OWNED_PRIVATE_NATS_CREDENTIAL_FILE_UNAVAILABLE') from None

def initialize_role_token_state(admission:dict)->dict:
    """Explicit admitted installer step under actual nonroot role; no issuer request.

    Existing/corrupt budgets are never reset. D2/D3 qualify paths/owner/epoch first.
    """
    try:
        environment=private_environment(admission)
        config=json.loads(environment['CEREBRO_BK11_PRODUCTION_RUNTIME_JSON'])
        mapping=json.loads(environment['CEREBRO_BK11_CREDENTIAL_REFS_JSON'])
        ref=config['context_token_ref'];entry=mapping[ref]
        if config['role']!=admission['role'] or entry['role']!=config['role']:raise ValueError()
        binding=binding_for(entry,ref,environment[entry['client_id_env']])
        TokenLifecycle(ProtectedRoleTokenStore(entry['token_state_dir'],entry['role'])).initialize(binding)
        return {'initialized_empty_role_budget':True,'issuer_requested':False,'authority':'NONE'}
    except Exception:raise ProductionRuntimeRefused('ROLE_TOKEN_STATE_INSTALLATION_REFUSED') from None

def open_runtime(admission:dict)->ProductionRuntime:
    """Called by the admitted EDGE controller; no credentials/role flags in admission."""
    transport=runtime=None
    try:
        environment=private_environment(admission)
        config_raw=json.loads(environment['CEREBRO_BK11_PRODUCTION_RUNTIME_JSON'])
        credential_map=json.loads(environment['CEREBRO_BK11_CREDENTIAL_REFS_JSON'])
        if set(config_raw)!={'profile','role','resource','session_ref','context_token_ref','nats_credentials_ref','server_url'} \
            or config_raw['profile']!=PROFILE or config_raw['role']!=admission.get('role'):raise ValueError()
        role=config_raw['role']
        if role not in ('publisher','receiver'):raise ValueError()
        for k in ('context_token_ref','nats_credentials_ref'):
            if REF.fullmatch(config_raw[k]) is None:raise ValueError()
        custody=PrivateCredentialCustody(credential_map,environ=environment)
        if credential_map[config_raw['context_token_ref']].get('role')!=role:raise ValueError()
        if credential_map[config_raw['context_token_ref']]['resource']!=config_raw['resource']:raise ValueError()
        client=AuthenticatedContextServiceClient(resource=config_raw['resource'],token_ref=config_raw['context_token_ref'],
                  token_resolver=custody.token,session_ref=config_raw['session_ref'])
        config=ProductionD1Config(enabled=True,server_url=config_raw['server_url']).validate()
        path=custody.nats_file(config_raw['nats_credentials_ref'],role)
        transport=NatsJetStreamTransport(config.server_url,credentials_path=path,runtime_role=role)
        runtime=ProductionRuntime(admission=admission,client=client,transport=transport,config=config)
        transport.connect().verify_runtime(config)
        return runtime
    except Exception:
        if runtime is not None:
            try:runtime.close()
            except Exception:pass
        elif transport is not None:
            try:transport.close()
            except Exception:pass
        raise ProductionRuntimeRefused('PRODUCTION_FACTORY_PRIVATE_CUSTODY_ASSIGNMENT_OR_RESOURCE_UNAVAILABLE') from None
