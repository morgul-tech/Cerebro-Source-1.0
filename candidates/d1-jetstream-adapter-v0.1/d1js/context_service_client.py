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
        try:
            token=self._resolve(self._ref)
            if type(token) is not str or not token or any(c.isspace() for c in token):
                raise ContextServiceRefused('PRIVATE_ISSUER_TOKEN_UNAVAILABLE')
            self._seq+=1
            payload={'jsonrpc':'2.0','id':self._seq,'method':'tools/call','params':{'name':name,'arguments':args,
                     '_meta':{'openai/session':self._session}}}
            req=Request(self._resource+'/mcp',data=json.dumps(payload,allow_nan=False).encode(),method='POST',
                headers={'Authorization':'Bearer '+token,'Content-Type':'application/json',
                         'Accept':'application/json, text/event-stream','MCP-Protocol-Version':'2025-06-18'})
            with self._open(req,timeout=self._timeout) as response:
                raw=response.read(1048577)
            if len(raw)>1048576:raise ContextServiceRefused('CONTEXT_RESPONSE_BOUND')
            answer=json.loads(raw)
            if answer.get('id')!=self._seq or 'error' in answer:raise ContextServiceRefused('CONTEXT_OPERATION_REFUSED')
            result=answer.get('result')
            if not isinstance(result,dict) or result.get('isError') is True or not isinstance(result.get('structuredContent'),dict):
                raise ContextServiceRefused('CONTEXT_OPERATION_REFUSED')
            return result['structuredContent']
        except ContextServiceRefused:raise
        except Exception:raise ContextServiceRefused('CONTEXT_SERVICE_CALL_UNAVAILABLE') from None

    def close(self):pass
