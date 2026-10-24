"""Exact D3 R5 facade. Host operation IDs identify attempts, never business events."""
from __future__ import annotations
import hashlib,uuid
from .production_config import PROFILE
from .production_factory import open_runtime as core_open
from .production_runtime import CURSOR_SCHEMA,REF,ProductionRuntimeRefused

def attempt_ref(operation_id):
    if type(operation_id)is not str or REF.fullmatch(operation_id)is None:
        raise ProductionRuntimeRefused('HOST_OPERATION_REFERENCE_REQUIRED')
    return 'bk11attempt:'+hashlib.sha256(operation_id.encode()).hexdigest()

class EdgeRuntimeAdapter:
    def __init__(self,admission,*,core_factory=core_open):
        self._admission=dict(admission);self._open=core_factory;self._runtime=None
        for key in ['assignment_ref','instance_ref']:
            if type(admission.get(key))is not str or REF.fullmatch(admission[key])is None:
                raise ProductionRuntimeRefused('INSTALLED_SERVICE_REFERENCE_REQUIRED')
        if admission.get('role')not in ('publisher','receiver'):
            raise ProductionRuntimeRefused('INSTALLED_SERVICE_ROLE_REQUIRED')

    def _start(self,operation_id,reconcile_id=None):
        if self._runtime is not None:self._runtime.close();self._runtime=None
        args={k:self._admission[k] for k in ['assignment_ref','instance_ref','role']}
        if 'private_env_file' in self._admission:args['private_env_file']=self._admission['private_env_file']
        args.update(profile=PROFILE,operation_ref=operation_id,attempt_ref=attempt_ref(operation_id))
        if reconcile_id is not None:args['reconcile_attempt_ref']=attempt_ref(reconcile_id)
        self._runtime=self._open(args)
        return self._runtime

    def _cursor(self,value):
        if value is None:return None
        if type(value)is not dict or set(value)!={'enqueued_at','event_id'}:
            raise ProductionRuntimeRefused('D3_CURSOR_FIELDS')
        return {'schema':CURSOR_SCHEMA,**value}

    def _result(self,result):
        result=dict(result);nxt=result.get('next_cursor')
        if nxt is not None:
            if type(nxt)is not dict or set(nxt)!={'schema','enqueued_at','event_id'} or nxt['schema']!=CURSOR_SCHEMA:
                raise ProductionRuntimeRefused('CORE_CURSOR_FIELDS')
            result['next_cursor']={k:nxt[k] for k in ['enqueued_at','event_id']}
        return result

    def publish_pending_once(self,*,limit,after=None,operation_id):
        if self._admission['role']!='publisher':raise ProductionRuntimeRefused('RECEIVER_PUBLISH_DENIED')
        return self._result(self._start(operation_id).publish_pending_once(limit=limit,after=self._cursor(after)))

    def drain_once(self,*,limit,operation_id):
        if self._admission['role']!='receiver':raise ProductionRuntimeRefused('PUBLISHER_DRAIN_DENIED')
        return self._result(self._start(operation_id).drain_once(limit=limit))

    def recover_pending_once(self,*,limit,after=None,reconcile_operation):
        if type(reconcile_operation)is not dict or set(reconcile_operation)!={'operation_id','action'}:
            raise ProductionRuntimeRefused('D3_ORIGINAL_OPERATION_REQUIRED')
        original=reconcile_operation['operation_id'];target=attempt_ref(original)
        recovery='bk11recovery:'+uuid.uuid4().hex
        result=self._result(self._start(recovery,original).recover_pending_once(limit=limit,after=self._cursor(after)))
        proof=result.get('attempt_proof',{})
        matched=(result.get('publication_reconciled')is True and proof.get('reconciled_original_attempt')is True
                 and proof.get('reconciled_attempt_ref')==target)
        result['publication_reconciled']=matched
        result['reconciled_operation_id']=original if matched else None
        return result

    def close(self):
        if self._runtime is not None:
            try:self._runtime.close()
            finally:self._runtime=None

def open_runtime(admission,*,operation_id=None):
    if admission.get('family_plan') is not None:
        from .production_factory import open_runtime as open_family_runtime
        from .ordinary_runtime import wrap_runtime
        inner=open_family_runtime(admission,operation_id=operation_id)
        try:return wrap_runtime(inner,admission)
        except Exception:
            inner.close()
            raise
    return EdgeRuntimeAdapter(admission)
