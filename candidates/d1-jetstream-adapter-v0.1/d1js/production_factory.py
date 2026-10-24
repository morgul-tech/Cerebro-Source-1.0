"""D3-compatible actual factory: installation-owned REF map, HTTPS issuer/Context and role-scoped NATS.

No missing-port success, synthetic host, implicit login or credential creation.
Config/custody refs navigate trusted installation input; Context authenticates and
checks real persisted service assignment/lease before any broker connection.
"""
from __future__ import annotations
import hashlib,json,os,stat
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request
from .private_http import private_open
from .production_config import ProductionD1Config,PROFILE
from .production_runtime import FamilyPlanRuntime,ProductionRuntime,ProductionRuntimeRefused,REF
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

TOKEN_REASON_CODES=frozenset(['ACTUAL_INSTALLATION_EPOCH_REQUIRED', 'ACTUAL_ISSUER_TTL_UNQUALIFIED', 'ACTUAL_JWT_EXPIRY_BASIS_REQUIRED', 'CACHED_TOKEN_BINDING_REQUALIFICATION_REQUIRED', 'CLOCK_BASIS_INVALID', 'CLOCK_ROLLBACK_REFUSED', 'CORRUPT_OR_FUTURE_TOKEN_WINDOW', 'CORRUPT_PROFILE_HOLD', 'CORRUPT_TOKEN_WINDOW', 'CREDENTIAL_REF_ROTATION_EPOCH_REQUIRED', 'DURABLE_ATTEMPT_LEDGER_INVALID', 'DURABLE_BINDING_COMMIT_REQUIRED_OWNER_HOLD', 'DURABLE_BUDGET_FIELDS_INVALID', 'EXACT_APPROVED_ROLE_CLIENT_REQUIRED', 'EXACT_ISSUER_AUDIENCE_REQUIRED', 'EXISTING_BUDGET_MUST_NOT_BE_REINITIALIZED', 'EXISTING_LOCK_REQUIRED', 'FOREIGN_ISSUER_CLIENT_AUDIENCE_METADATA', 'FOREIGN_OR_CORRUPT_BINDING_STATE', 'FOREIGN_OR_CORRUPT_ROLE_STATE', 'FOREIGN_REF_OR_UNQUALIFIED_BINDING_CHANGE', 'FUTURE_ATTEMPT_LEDGER_REFUSED', 'FUTURE_OR_INVALID_ISSUER_TIME', 'HOST_BOOT_CLOCK_REQUALIFICATION_REQUIRED', 'HOST_CLOCK_BOOT_BASIS_REQUIRED', 'INITIAL_EFFECTIVE_VALIDITY_BELOW_12H', 'INSTALLATION_EPOCH_ROLLBACK_REFUSED', 'INSTALLED_EXACT_ROLE_STATE_PATH_REQUIRED', 'INSTALLED_TOKEN_LIFECYCLE_PROFILE_REQUIRED', 'INSTALLER_INITIALIZED_DURABLE_ROLE_BUDGET_REQUIRED', 'ISSUER_ACCESS_TOKEN_REQUIRED', 'ISSUER_BEARER_RESPONSE_REQUIRED', 'LINUX_SEARCH_ONLY_ANCESTRY_REQUIRED', 'NO_USABLE_EXISTING_TOKEN_NO_ACQUISITION', 'PERSISTENT_ACQUISITION_COOLDOWN', 'PRIVATE_REGULAR_SINGLE_LINK_0600_REQUIRED', 'PRIVATE_STATE_DUPLICATE_KEY', 'PRIVATE_STATE_NONFINITE', 'PRIVATE_STATE_REPLACED', 'PRIVATE_STATE_SIZE', 'PRIVATE_WRITE_STALLED', 'QUALIFIED_INITIAL_VALIDITY_PROFILE_REQUIRED', 'QUALIFIED_LINUX_OPATH_ROLE_REQUIRED', 'REQUEST_CLOCK_BASIS_INVALID', 'ROLE_ANCESTRY_CUSTODY_REQUIRED', 'ROLE_DIRECTORY_0700_REQUIRED', 'ROLE_DIRECTORY_REPLACED', 'ROLE_LOCK_REPLACED', 'ROLE_PATH_COMPONENT_INVALID', 'ROLE_SINGLEFLIGHT_TIMEOUT', 'ROLE_STORE_ALREADY_OPEN', 'ROLLING_31_DAY_ATTEMPT_ALLOWANCE_EXHAUSTED', 'TOKEN_METADATA_LOCAL_READ_UNAVAILABLE', 'TOKEN_REF_INVALID', 'UNEXPECTED_CLOCK_JUMP_REFUSED', 'UNKNOWN_ISSUER_OUTCOME_ATTEMPT_RETAINED', 'UNKNOWN_TOKEN_FAILURE'])
TOKEN_STAGES=frozenset(['ACQUISITION_GUARDS', 'ACQUISITION_INTENT_COMMIT', 'BINDING_VALIDATE', 'CACHE_COMMIT', 'CACHE_VALIDATE', 'CLOCK_VALIDATE', 'EPOCH_COMMIT', 'ISSUER_CALLBACK_ENTER', 'ISSUER_OPEN', 'ISSUER_PARSE', 'ISSUER_READ', 'ISSUER_REQUEST_BUILD', 'LOCAL_BINDING', 'POST_ISSUER_RESPONSE', 'POST_ISSUER_VALIDATE', 'ROLE_STATE_READ', 'STATE_LOAD', 'STATE_VALIDATE', 'TOKEN_COMMIT', 'TOKEN_LIFECYCLE'])
def safe_token_diagnostic(value):
    if type(value)is not dict or set(value)!={'schema','stage','reason_code','http_status'}:return None
    if any(type(value[k])is not str for k in ('schema','stage','reason_code')):return None
    if value['schema']!='bk11-token-custody-diagnostic/v1' or value['stage'] not in TOKEN_STAGES or value['reason_code'] not in TOKEN_REASON_CODES:return None
    status=value['http_status']
    if status is not None and (type(status)is not int or not 100<=status<=599):return None
    return dict(value)

class PrivateCredentialCustody:
    def __init__(self,mapping:dict,environ=None,opener=private_open):
        self._mapping=mapping;self._env=os.environ if environ is None else environ;self._open=opener

    def token(self,ref:str)->str:
        stage='LOCAL_BINDING'
        issuer_status=None
        try:
            entry=self._mapping[ref]
            client_id=self._env[entry['client_id_env']]
            binding=binding_for(entry,ref,client_id)
            issuer=urlsplit(entry['issuer'])
            if issuer.scheme!='https' or not issuer.hostname or issuer.username or issuer.password or issuer.query or issuer.fragment or issuer.path not in ('','/'):raise ValueError()
            store=ProtectedRoleTokenStore(entry['token_state_dir'],entry['role'])
            def acquire():
                nonlocal stage,issuer_status
                stage='ISSUER_REQUEST_BUILD'
                secret=self._env[entry['client_secret_env']]
                req=Request(entry['issuer'].rstrip('/')+'/oauth/token',method='POST',
                  data=json.dumps({'grant_type':'client_credentials','client_id':client_id,'client_secret':secret,
                                   'audience':entry['resource']}).encode(),headers={'Content-Type':'application/json'})
                try:
                    stage='ISSUER_OPEN'
                    with self._open(req,timeout=5) as response:
                        stage='ISSUER_READ'
                        raw=response.read(65537)
                    if len(raw)>65536:raise ValueError()
                    stage='ISSUER_PARSE'
                    answer=json.loads(raw)
                    stage='POST_ISSUER_RESPONSE'
                    return answer
                except Exception as issuer_error:
                    value=getattr(issuer_error,'code',None)
                    issuer_status=value if type(value)is int and 100<=value<=599 else None
                    raise
            stage='TOKEN_LIFECYCLE'
            def observe_stage(value):
                nonlocal stage
                if type(value)is str and value in TOKEN_STAGES:stage=value
            return TokenLifecycle(store,diagnostic_stage=observe_stage).token(binding,acquire)
        except Exception as error:
            from .private_token_state import TokenCustodyRefused
            reason=error.args[0] if isinstance(error,TokenCustodyRefused) and len(error.args)==1 and type(error.args[0])is str and error.args[0] in TOKEN_REASON_CODES else 'UNKNOWN_TOKEN_FAILURE'
            status=issuer_status
            wrapped=ProductionRuntimeRefused('PRIVATE_ISSUER_TOKEN_CUSTODY_UNAVAILABLE')
            wrapped.safe_token_diagnostic={'schema':'bk11-token-custody-diagnostic/v1','stage':stage,'reason_code':reason,'http_status':status}
            raise wrapped from None

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

def open_runtime(admission:dict,*,operation_id:str|dict|None=None)->ProductionRuntime|FamilyPlanRuntime:
    """Called by the admitted EDGE controller; no credentials/role flags in admission."""
    transport=runtime=client=None
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
        family_plan=admission.get('family_plan')
        family_mode=family_plan is not None
        timeout_s=1.0 if family_mode else 10.0
        client=AuthenticatedContextServiceClient(resource=config_raw['resource'],token_ref=config_raw['context_token_ref'],
                  token_resolver=custody.token,session_ref=config_raw['session_ref'],timeout_s=timeout_s)
        config=ProductionD1Config(enabled=True,server_url=config_raw['server_url']).validate()
        path=custody.nats_file(config_raw['nats_credentials_ref'],role)
        transport=NatsJetStreamTransport(config.server_url,credentials_path=path,runtime_role=role,
          call_timeout=2.0 if family_mode else 10.0,connect_timeout=1.0 if family_mode else 3.0)
        if not family_mode:
            runtime=ProductionRuntime(admission=admission,client=client,transport=transport,config=config)
            transport.connect().verify_runtime(config)
            return runtime
        transport.connect().verify_runtime(config)
        plan_ok=set(family_plan)=={'schema','session_ref','session_binding_id','runtime_config_sha256','families'}
        if not plan_ok or family_plan['schema']!='bk11-edge-family-plan/v1' or family_plan['session_ref']!=config_raw['session_ref']:
            raise ValueError()
        raw_runtime=environment['CEREBRO_BK11_PRODUCTION_RUNTIME_JSON'].encode('utf-8')
        if hashlib.sha256(raw_runtime).hexdigest()!=family_plan['runtime_config_sha256']:
            raise ValueError()
        families=family_plan['families']
        if type(families)is not dict or set(families)!= {'PM_OWNER','ROOM_RETURN'}:raise ValueError()
        for family,item in families.items():
            if type(item)is not dict or set(item)!={'owner_family','role','assignment_ref','session_binding_id',
                'factory_sha256','interpreter_sha256'}:raise ValueError()
            if (item['owner_family']!=family or item['role']!=role or item['session_binding_id']!=family_plan['session_binding_id']
                or item['factory_sha256']!=admission['factory']['sha256']
                or item['interpreter_sha256']!=admission['interpreter']['sha256']
                or REF.fullmatch(item['assignment_ref']) is None):raise ValueError()
        if family_mode:
            if type(operation_id)is not dict or set(operation_id)!= {'PM_OWNER','ROOM_RETURN'}:
                raise ValueError()
            operation_values=[operation_id[family] for family in ('PM_OWNER','ROOM_RETURN')]
            if any(type(value)is not str or REF.fullmatch(value) is None for value in operation_values) \
                or len(set(operation_values))!=2:
                raise ValueError()
        elif operation_id is not None and (type(operation_id)is not str or REF.fullmatch(operation_id) is None):
            raise ValueError()
        def open_family(family,host_operation_id,counter,reconcile_attempt_ref=None):
            item=families[family]
            token=hashlib.sha256(f'{host_operation_id}:{family}'.encode('utf-8')).hexdigest()[:32]
            selected={'profile':PROFILE,'role':role,'owner_family':family,'assignment_ref':item['assignment_ref'],
              'session_binding_id':item['session_binding_id'],'instance_ref':'i:'+token,
              'operation_ref':'o:'+token,'attempt_ref':'a:'+token}
            if reconcile_attempt_ref is not None:selected['reconcile_attempt_ref']=reconcile_attempt_ref
            return ProductionRuntime(admission=selected,client=client,transport=transport,config=config,owns_resources=False)
        runtime=FamilyPlanRuntime(families=families,open_family=open_family,client=client,transport=transport,role=role)
        return runtime
    except Exception:
        if runtime is not None:
            try:runtime.close()
            except Exception:pass
        elif transport is not None:
            try:transport.close()
            except Exception:pass
            if client is not None:
                try:client.close()
                except Exception:pass
        raise ProductionRuntimeRefused('PRODUCTION_FACTORY_PRIVATE_CUSTODY_ASSIGNMENT_OR_RESOURCE_UNAVAILABLE') from None
