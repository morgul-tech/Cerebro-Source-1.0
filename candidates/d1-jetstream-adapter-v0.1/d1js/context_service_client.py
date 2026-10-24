"""Actual HTTPS MCP client; private bearer resolution is installation-owned.

This class's marker is adapter provenance only. OAuth and current assignments are
verified at the Context server on every operation, never inferred from that marker.
"""
from __future__ import annotations
import json
from urllib.parse import urlsplit
from urllib.request import Request
from .private_http import private_open
from typing import Callable,Any

DIAG_STAGES=frozenset({'UNSPECIFIED','TOKEN_RESOLVE','BUILD_REQUEST','HTTP_OPEN','HTTP_READ','JSON_PARSE','RPC_ENVELOPE','RESULT_CONTRACT'})
DIAG_REASONS=frozenset({'QUALIFIED_CONTEXT_CUSTODY_INPUT_REQUIRED','CONTEXT_CALL_TIMEOUT_INVALID','PRIVATE_ISSUER_TOKEN_UNAVAILABLE','CONTEXT_RESPONSE_BOUND','CONTEXT_OPERATION_REFUSED','CONTEXT_SERVICE_CALL_UNAVAILABLE'})
DIAG_TOOLS=frozenset({'UNSPECIFIED','open_bk11_service_runtime_v1','verify_bk11_service_lease_v1','control_bk11_service_attempt_v1','discover_bk11_backend_obligations_v1','reconcile_bk11_backend_obligation_v1','accept_bk11_backend_delivery_v1','read_bk11_backend_disposition_v1','close_bk11_service_runtime_v1','read_room_return_d1_outbox_v1','settle_room_return_d1_outbox_v1','read_pm_owner_d1_outbox_v1','settle_pm_owner_d1_outbox_v1'})
TOKEN_REASON_CODES=frozenset(['ACTUAL_INSTALLATION_EPOCH_REQUIRED', 'ACTUAL_ISSUER_TTL_UNQUALIFIED', 'ACTUAL_JWT_EXPIRY_BASIS_REQUIRED', 'CACHED_TOKEN_BINDING_REQUALIFICATION_REQUIRED', 'CLOCK_BASIS_INVALID', 'CLOCK_ROLLBACK_REFUSED', 'CORRUPT_OR_FUTURE_TOKEN_WINDOW', 'CORRUPT_PROFILE_HOLD', 'CORRUPT_TOKEN_WINDOW', 'CREDENTIAL_REF_ROTATION_EPOCH_REQUIRED', 'DURABLE_ATTEMPT_LEDGER_INVALID', 'DURABLE_BINDING_COMMIT_REQUIRED_OWNER_HOLD', 'DURABLE_BUDGET_FIELDS_INVALID', 'EXACT_APPROVED_ROLE_CLIENT_REQUIRED', 'EXACT_ISSUER_AUDIENCE_REQUIRED', 'EXISTING_BUDGET_MUST_NOT_BE_REINITIALIZED', 'EXISTING_LOCK_REQUIRED', 'FOREIGN_ISSUER_CLIENT_AUDIENCE_METADATA', 'FOREIGN_OR_CORRUPT_BINDING_STATE', 'FOREIGN_OR_CORRUPT_ROLE_STATE', 'FOREIGN_REF_OR_UNQUALIFIED_BINDING_CHANGE', 'FUTURE_ATTEMPT_LEDGER_REFUSED', 'FUTURE_OR_INVALID_ISSUER_TIME', 'HOST_BOOT_CLOCK_REQUALIFICATION_REQUIRED', 'HOST_CLOCK_BOOT_BASIS_REQUIRED', 'INITIAL_EFFECTIVE_VALIDITY_BELOW_12H', 'INSTALLATION_EPOCH_ROLLBACK_REFUSED', 'INSTALLED_EXACT_ROLE_STATE_PATH_REQUIRED', 'INSTALLED_TOKEN_LIFECYCLE_PROFILE_REQUIRED', 'INSTALLER_INITIALIZED_DURABLE_ROLE_BUDGET_REQUIRED', 'ISSUER_ACCESS_TOKEN_REQUIRED', 'ISSUER_BEARER_RESPONSE_REQUIRED', 'LINUX_SEARCH_ONLY_ANCESTRY_REQUIRED', 'NO_USABLE_EXISTING_TOKEN_NO_ACQUISITION', 'PERSISTENT_ACQUISITION_COOLDOWN', 'PRIVATE_REGULAR_SINGLE_LINK_0600_REQUIRED', 'PRIVATE_STATE_DUPLICATE_KEY', 'PRIVATE_STATE_NONFINITE', 'PRIVATE_STATE_REPLACED', 'PRIVATE_STATE_SIZE', 'PRIVATE_WRITE_STALLED', 'QUALIFIED_INITIAL_VALIDITY_PROFILE_REQUIRED', 'QUALIFIED_LINUX_OPATH_ROLE_REQUIRED', 'REQUEST_CLOCK_BASIS_INVALID', 'ROLE_ANCESTRY_CUSTODY_REQUIRED', 'ROLE_DIRECTORY_0700_REQUIRED', 'ROLE_DIRECTORY_REPLACED', 'ROLE_LOCK_REPLACED', 'ROLE_PATH_COMPONENT_INVALID', 'ROLE_SINGLEFLIGHT_TIMEOUT', 'ROLE_STORE_ALREADY_OPEN', 'ROLLING_31_DAY_ATTEMPT_ALLOWANCE_EXHAUSTED', 'TOKEN_METADATA_LOCAL_READ_UNAVAILABLE', 'TOKEN_REF_INVALID', 'UNEXPECTED_CLOCK_JUMP_REFUSED', 'UNKNOWN_ISSUER_OUTCOME_ATTEMPT_RETAINED', 'UNKNOWN_TOKEN_FAILURE'])
TOKEN_STAGES=frozenset(['ACQUISITION_GUARDS', 'ACQUISITION_INTENT_COMMIT', 'BINDING_VALIDATE', 'CACHE_COMMIT', 'CACHE_VALIDATE', 'CLOCK_VALIDATE', 'EPOCH_COMMIT', 'ISSUER_CALLBACK_ENTER', 'ISSUER_OPEN', 'ISSUER_PARSE', 'ISSUER_READ', 'ISSUER_REQUEST_BUILD', 'LOCAL_BINDING', 'POST_ISSUER_RESPONSE', 'POST_ISSUER_VALIDATE', 'ROLE_STATE_READ', 'STATE_LOAD', 'STATE_VALIDATE', 'TOKEN_COMMIT', 'TOKEN_LIFECYCLE'])
def safe_token_diagnostic(value):
    if type(value)is not dict or set(value)!={'schema','stage','reason_code','http_status'}:return None
    if any(type(value[k])is not str for k in ('schema','stage','reason_code')):return None
    if value['schema']!='bk11-token-custody-diagnostic/v1' or value['stage'] not in TOKEN_STAGES or value['reason_code'] not in TOKEN_REASON_CODES:return None
    status=value['http_status']
    if status is not None and (type(status)is not int or not 100<=status<=599):return None
    return dict(value)

def safe_context_diagnostic(value):
    if type(value) is not dict or set(value) not in ({'schema','stage','operation','owner_family','reason_code','http_status'},{'schema','stage','operation','owner_family','reason_code','http_status','token_diagnostic'}) or value.get('schema')!='bk11-context-call-diagnostic/v1':return None
    if any(type(value[k])is not str for k in ('schema','stage','operation','owner_family','reason_code')):return None
    if value['stage'] not in DIAG_STAGES or value['operation'] not in DIAG_TOOLS or value['reason_code'] not in DIAG_REASONS:return None
    if value['owner_family'] not in ('UNSPECIFIED','PM_OWNER','ROOM_RETURN'):return None
    status=value['http_status']
    if status is not None and (type(status)is not int or not 100<=status<=599):return None
    if 'token_diagnostic' in value and (value['stage']!='TOKEN_RESOLVE' or safe_token_diagnostic(value['token_diagnostic']) is None):return None
    return dict(value)

class ContextServiceRefused(RuntimeError):
    pass

class AuthenticatedContextServiceClient:
    D1_SERVICE_OWNED_CONTEXT_CLIENT=True
    def __init__(self,*,resource:str,token_ref:str,token_resolver:Callable[[str],str],session_ref:str,
                 opener:Callable[...,Any]=private_open,timeout_s:float=10.0):
        u=urlsplit(resource)
        if u.scheme!='https' or not u.hostname or u.username or u.password or u.query or u.fragment \
                or u.path not in ('','/') or not token_ref or not session_ref or not callable(token_resolver):
            raise ContextServiceRefused('QUALIFIED_CONTEXT_CUSTODY_INPUT_REQUIRED')
        if type(timeout_s) not in (int,float) or not 0<timeout_s<=30:
            raise ContextServiceRefused('CONTEXT_CALL_TIMEOUT_INVALID')
        self._resource=resource.rstrip('/');self._ref=token_ref;self._resolve=token_resolver
        self._session=session_ref;self._open=opener;self._timeout=timeout_s;self._seq=0

    def call(self,name:str,args:dict)->dict:
        stage='TOKEN_RESOLVE'
        operation=name if name in DIAG_TOOLS else 'UNSPECIFIED'
        family=args.get('owner_family') if type(args)is dict else None
        family=family if family in ('PM_OWNER','ROOM_RETURN') else 'UNSPECIFIED'
        try:
            token=self._resolve(self._ref)
            if type(token) is not str or not token or any(c.isspace() for c in token):
                raise ContextServiceRefused('PRIVATE_ISSUER_TOKEN_UNAVAILABLE')
            stage='BUILD_REQUEST'
            self._seq+=1
            payload={'jsonrpc':'2.0','id':self._seq,'method':'tools/call','params':{'name':name,'arguments':args,
                     '_meta':{'openai/session':self._session}}}
            req=Request(self._resource+'/mcp',data=json.dumps(payload,allow_nan=False).encode(),method='POST',
                headers={'Authorization':'Bearer '+token,'Content-Type':'application/json',
                         'Accept':'application/json, text/event-stream','MCP-Protocol-Version':'2025-06-18'})
            stage='HTTP_OPEN'
            with self._open(req,timeout=self._timeout) as response:
                stage='HTTP_READ'
                raw=response.read(1048577)
            if len(raw)>1048576:raise ContextServiceRefused('CONTEXT_RESPONSE_BOUND')
            stage='JSON_PARSE'
            answer=json.loads(raw)
            stage='RPC_ENVELOPE'
            if answer.get('id')!=self._seq or 'error' in answer:raise ContextServiceRefused('CONTEXT_OPERATION_REFUSED')
            stage='RESULT_CONTRACT'
            result=answer.get('result')
            if not isinstance(result,dict) or result.get('isError') is True or not isinstance(result.get('structuredContent'),dict):
                raise ContextServiceRefused('CONTEXT_OPERATION_REFUSED')
            return result['structuredContent']
        except ContextServiceRefused as error:
            reason=str(error) if str(error) in DIAG_REASONS else 'CONTEXT_SERVICE_CALL_UNAVAILABLE'
            error.safe_diagnostic={'schema':'bk11-context-call-diagnostic/v1','stage':stage,'operation':operation,'owner_family':family,'reason_code':reason,'http_status':None}
            raise
        except Exception as error:
            status=getattr(error,'code',None)
            status=status if type(status)is int and 100<=status<=599 else None
            wrapped=ContextServiceRefused('CONTEXT_SERVICE_CALL_UNAVAILABLE')
            wrapped.safe_diagnostic={'schema':'bk11-context-call-diagnostic/v1','stage':stage,'operation':operation,'owner_family':family,'reason_code':'CONTEXT_SERVICE_CALL_UNAVAILABLE','http_status':status}
            inner=safe_token_diagnostic(getattr(error,'safe_token_diagnostic',None))
            if stage=='TOKEN_RESOLVE' and inner is not None:wrapped.safe_diagnostic['token_diagnostic']=inner
            raise wrapped from None

    def close(self):pass
