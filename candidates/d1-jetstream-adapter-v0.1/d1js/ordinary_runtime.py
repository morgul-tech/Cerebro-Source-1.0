"""Ordinary two-family service wrapper, independent of task or message.

Current assignment/lease checks, durable close and expired-ownertruth guards.
Historical per-message recovery selection is not part of this module.
"""
from datetime import datetime,timezone
import hashlib
import sys
from .scope_diagnostics import ScopedOperationRefused,diagnostic
from .pointer import parse_pointer,is_expired
from .production_runtime import cursor_validate,family_from_referent,ProductionRuntimeRefused

READS={'read_pm_owner_d1_outbox_v1','read_room_return_d1_outbox_v1'}
SERVICE={'open_bk11_service_runtime_v1','verify_bk11_service_lease_v1',
    'control_bk11_service_attempt_v1','discover_bk11_backend_obligations_v1',
    'reconcile_bk11_backend_obligation_v1','accept_bk11_backend_delivery_v1',
    'read_bk11_backend_disposition_v1','close_bk11_service_runtime_v1'}
SETTLES={'settle_pm_owner_d1_outbox_v1','settle_room_return_d1_outbox_v1'}


def attempt_ref(op,family):return 'a:'+hashlib.sha256((op+':'+family).encode()).hexdigest()[:32]


class Monitor:
    def __init__(self,inner,mode,admission):
        self.inner=inner;self.mode=mode;self.role=admission['role'];self.admission=admission
        self.original_call=inner._client.call;self.family=None;self.leases={};self.close_attempts=set();self.closed={}
        self.current_runtime=None;self.calls=0;self.open_requests={};self.opened=set()
        self.custody_observations=[]
    def refuse(self,reason,stage,operation,args=None):
        raise ScopedOperationRefused(reason,diagnostic(self.role,self.family,stage,operation,args,reason))
    def need(self,ok,reason,stage,operation,args=None):
        if not ok:self.refuse(reason,stage,operation,args)
    def call(self,name,args):
        self.calls+=1
        self.need(type(name) is str and type(args) is dict and (self.calls<=128 or name=='close_bk11_service_runtime_v1'),
                  'BOUNDED_EXISTING_CALL_REQUIRED','BEFORE_CALL','UNSPECIFIED')
        allowed=SERVICE|READS|(SETTLES if self.mode=='NORMAL' else set())
        self.need(name in allowed,'UNSELECTED_TOOL','BEFORE_CALL',name,args)
        if name=='open_bk11_service_runtime_v1':
            family=args.get('owner_family')
            self.need(type(family) is str and family in ('PM_OWNER','ROOM_RETURN') and family not in self.opened,
                      'EXACT_OWNER_FAMILY','BEFORE_CALL',name)
            self.family=family
            self.need(args.get('assignment_ref')==self.admission['family_plan']['families'][family]['assignment_ref'],
                      'EXACT_ASSIGNED_ROLE_REQUIRED','BEFORE_CALL',name)
            self.opened.add(family)
        elif name in SERVICE:
            lease=args.get('lease_ref');record=self.leases.get(lease)
            self.need(record is not None and args.get('fencing_epoch')==record['fencing_epoch'],
                      'ACTUAL_OWN_CURRENT_LEASE_REQUIRED','BEFORE_CALL',name,args)
            self.family=record['owner_family']
        if name=='close_bk11_service_runtime_v1':
            self.need(args['lease_ref'] not in self.close_attempts,'NO_CLOSE_RETRY','LEASE_CLOSE',name)
            self.close_attempts.add(args['lease_ref'])
        try:answer=self.original_call(name,args)
        except Exception as error:
            if getattr(error,'safe_scope_diagnostic',None) is None:
                error.safe_scope_diagnostic=diagnostic(self.role,self.family,'CONTEXT_CALL',name,args)
            capture=getattr(self.inner._client,'_private_last_response',None)
            if type(capture) is dict:error.private_context_capture=capture
            raise
        if self.mode=='NORMAL' and self.role=='receiver' and (
            name in ('discover_bk11_backend_obligations_v1','accept_bk11_backend_delivery_v1',
                     'read_bk11_backend_disposition_v1') or
            name=='control_bk11_service_attempt_v1' and args.get('phase') in
                ('recovery_receive_begin','recovery_receive_observed','recovery')):
            self.custody_observations.append({'operation':name,'owner_family':self.family,
                                             'arguments':dict(args),'answer':answer})
        if name=='open_bk11_service_runtime_v1':
            self.need(type(answer) is dict and type(answer.get('lease_ref')) is str and
                      answer.get('owner_family')==self.family,'ACTUAL_LEASE_RESULT_REQUIRED','RESULT_CONTRACT',name)
            self.leases[answer['lease_ref']]=dict(answer)
            self.open_requests[answer['lease_ref']]=dict(args)
        if name=='close_bk11_service_runtime_v1':
            self.need(type(answer) is dict and answer.get('closed') is True and
                      answer.get('pending_owner_and_backend_data_preserved') is True,
                      'COMMITTED_LEASE_CLOSE_REQUIRED','LEASE_CLOSE',name)
            self.closed[args['lease_ref']]=dict(answer)
            self.family=None
        return answer


class LeaseClosedFamily:
    def __init__(self,runtime,monitor):self.rt=runtime;self.monitor=monitor;self.last_result=None
    def __getattr__(self,name):return getattr(self.rt,name)
    def close(self,**kwargs):
        primary=sys.exc_info()[1]
        try:self.rt.close(**kwargs)
        except Exception as close_error:
            if primary is not None:
                primary.secondary_close_error=close_error
                raise primary from close_error
            raise
        answer=self.monitor.closed.get(self.rt._lease['lease_ref'])
        self.monitor.need(answer is not None,'OWN_LEASE_CLOSE_UNCONFIRMED','LEASE_CLOSE','close_bk11_service_runtime_v1')
        if self.last_result is not None:self.last_result['lease_close_proof']=answer


class NormalOwnerView:
    def __init__(self,original,runtime,monitor):self.original=original;self.rt=runtime;self.monitor=monitor;self.withheld=[]
    def __getattr__(self,name):return getattr(self.original,name)
    def pending(self,*,after=None,limit=1):
        rows,nxt,enabled=self.original.pending(after=after,limit=limit)
        self.monitor.need(limit==1 and len(rows)<=1,'NORMAL_BOUND_REQUIRED','BEFORE_CALL','UNSPECIFIED')
        if not rows:return rows,nxt,enabled
        row=rows[0]
        try:pointer=parse_pointer(row['pointer'],max_ttl_seconds=self.rt._cfg.pointer_ttl_s)
        except (ValueError,TypeError,KeyError):
            self.monitor.refuse('EXACT_CURRENT_ROW_KEYSET_REQUIRED','NORMAL_EXPIRED_CUSTODY',self.original.READ_TOOL)
        if not is_expired(pointer,datetime.now(timezone.utc)):return rows,nxt,enabled
        item={'message_id':row['message_id'],'pointer_sha256':row['pointer_sha256']}
        self.monitor.need(pointer.message_id==item['message_id'] and pointer.raw_sha256==item['pointer_sha256'],
                          'EXACT_CURRENT_ROW_KEYSET_REQUIRED','NORMAL_EXPIRED_CUSTODY',self.original.READ_TOOL,item)
        # No per-message exemption. The original scoped owner port and the
        # actual server reconciliation below qualify the exact current tuple.
        # Expiry permits neither publication nor execution of old material.
        cursor={'enqueued_at':row['enqueued_at'],'event_id':row.get('owner_event_key',row.get('event_id'))}
        try:cursor_validate({'schema':'bk11-owner-route-keyset/v1',**cursor})
        except (ValueError,TypeError,RuntimeError):
            self.monitor.refuse('EXACT_CURRENT_ROW_KEYSET_REQUIRED','NORMAL_EXPIRED_CUSTODY',self.original.READ_TOOL,item)
        args={**item,'lease_ref':self.rt._lease['lease_ref'],'fencing_epoch':self.rt._lease['fencing_epoch'],
              'attempt_ref':self.rt._admission['attempt_ref'],'operation_ref':self.rt._admission['operation_ref']}
        proof=self.rt._client.call('reconcile_bk11_backend_obligation_v1',args)
        self.monitor.need(proof.get('state')=='MATCHED_DURABLE_BACKEND_DISPOSITION' and
                          all(proof.get(k)==v for k,v in item.items()) and proof.get('republished') is False,
                          'EXPIRED_INTENT_CUSTODY_NOT_PROVEN','NORMAL_EXPIRED_CUSTODY',
                          'reconcile_bk11_backend_obligation_v1',item)
        self.withheld.append({'identity':item,'result':'WITHHELD_EXPIRED_WITH_DURABLE_BACKEND_CUSTODY',
                              'broker_publish':False,'owner_row_unchanged':True,'proof':proof,
                              'actual_row_keyset':cursor,'before_cursor':after,
                              'role':self.monitor.role,'owner_family':self.rt._owner_family,
                              'lease_ref':self.rt._lease['lease_ref'],'fencing_epoch':self.rt._lease['fencing_epoch'],
                              'operation_ref':self.rt._admission['operation_ref'],'attempt_ref':self.rt._admission['attempt_ref']})
        # Ordinary runtime keyset progress after a proven custody disposition,
        # persisted only by the normal controller after its actual attempt proof.
        return [],cursor,enabled


class NormalFamily(LeaseClosedFamily):
    def recover_pending_once(self,**kwargs):
        self.monitor.refuse('EXPLICIT_RECOVERY_SELECTION_REQUIRED','BEFORE_CALL','UNSPECIFIED')
    def publish_pending_once(self,**kwargs):
        result=self.rt.publish_pending_once(**kwargs)
        if isinstance(self.rt._port,NormalOwnerView):result['expired_custody_withheld']=list(self.rt._port.withheld)
        self.last_result=result;return result
    def prepare_current_custody(self,limit=1):
        # A bounded current service-custody pass precedes ordinary broker drain.
        # This is the existing fresh scoped missing-duty path, not old work,
        # old attempt replay or an invented terminal state. Independent backend
        # readback and the existing server attempt guards remain mandatory.
        first=len(self.monitor.custody_observations)
        custody=self.rt.recover_pending_once(limit=limit)
        self.monitor.need(custody.get('completed') is True,
                          'DURABLE_RECEIVER_OBSERVATION_REQUIRED','RESULT_CONTRACT',
                          'control_bk11_service_attempt_v1')
        custody['independent_custody_observations']=self.monitor.custody_observations[first:]
        custody['backend_recovery_next_cursor']=custody.get('next_cursor')
        return custody


def wrap_runtime(inner,admission):
    if admission['role'] not in ('publisher','receiver'):raise ValueError('ORDINARY_ROLE_REQUIRED')
    monitor=Monitor(inner,'NORMAL',admission);inner._client.call=monitor.call
    original_open=inner._open_family
    def guarded_open(family,operation_id,counter,reconcile_attempt_ref=None):
        rt=original_open(family,operation_id,counter,reconcile_attempt_ref)
        monitor.current_runtime=rt
        if admission['role']=='publisher':rt._port=NormalOwnerView(rt._port,rt,monitor)
        return NormalFamily(rt,monitor)
    inner._open_family=guarded_open
    physical=inner._transport;original_publish=physical.publish
    def publish(subject,payload,msg_id,timeout):
        rt=monitor.current_runtime
        monitor.need(rt is not None,'CURRENT_RUNTIME_REQUIRED','BROKER_BOUNDARY','publish')
        pointer=parse_pointer(payload,max_ttl_seconds=rt._cfg.pointer_ttl_s)
        monitor.need(pointer.message_id==msg_id and not is_expired(pointer,datetime.now(timezone.utc)),
                     'NO_EXPIRED_PUBLICATION','BROKER_BOUNDARY','publish',
                     {'message_id':msg_id,'pointer_sha256':pointer.raw_sha256})
        return original_publish(subject,payload,msg_id,timeout)
    physical.publish=publish
    return WholeRuntime(inner,monitor)


class WholeRuntime:
    """Own observed constructor leases are closed once, even before _current exists."""
    def __init__(self,inner,monitor):self.inner=inner;self.monitor=monitor
    def __getattr__(self,name):return getattr(self.inner,name)
    def reconcile_prior_once(self,*,targets,operation_id):
        """Proof-only prior-attempt admission; never discover, send, pull or ACK.

        Targets come from the locked durable role fence, not message constants.
        Only actual server matching proofs and committed own CLOSE qualify.
        Unknown or absent attempts are not assumed safe.
        """
        if type(targets)is not dict or set(targets)!={'PM_OWNER','ROOM_RETURN'}:
            raise ProductionRuntimeRefused('EXACT_TWO_FAMILY_PLAN_REQUIRED')
        results={}
        for family in ('PM_OWNER','ROOM_RETURN'):
            refs=targets[family]
            if type(refs)is not list or len(refs)>128:
                raise ProductionRuntimeRefused('BOUNDED_PRIOR_LINEAGE_REQUIRED')
            from .production_runtime import REF
            if any(type(v)is not str or REF.fullmatch(v) is None for v in refs):
                raise ProductionRuntimeRefused('PRIOR_ATTEMPT_REF_REQUIRED')
            if len(refs)!=len(set(refs)):raise ProductionRuntimeRefused('DUPLICATE_PRIOR_ATTEMPT')
            if not refs:continue
            rt=self.inner._open(family,operation_id)
            try:
                proofs=[]
                for target in refs:
                    rt._guard('prior_attempt_reconciliation')
                    args={k:rt._admission[k] for k in ('assignment_ref','operation_ref','attempt_ref')}
                    args.update(lease_ref=rt._lease['lease_ref'],fencing_epoch=rt._lease['fencing_epoch'],
                                phase='recovery',reconcile_attempt_ref=target)
                    proof=rt._client.call('control_bk11_service_attempt_v1',args)
                    self.monitor.need(type(proof)is dict and
                        proof.get('schema')=='cerebro-bk11-server-attempt-proof/v1' and
                        all(proof.get(k)==args[k] for k in ('assignment_ref','operation_ref','attempt_ref','lease_ref','fencing_epoch')) and
                        proof.get('owner_family')==family and proof.get('reconciled_original_attempt')is True and
                        proof.get('reconciled_attempt_ref')==target,
                        'ORIGINAL_DUTY_RECONCILIATION_PENDING','ORIGINAL_RECONCILE','control_bk11_service_attempt_v1')
                    proofs.append(proof)
                result={'completed':True,'publication_reconciled':True,'original_reconciliation_proofs':proofs,
                        'republished':False,'discovery_entered':False}
                rt.last_result=result;results[family]=result
            finally:self.inner._close_current()
            self.monitor.need(result.get('lease_close_proof') is not None,'OWN_LEASE_CLOSE_UNCONFIRMED',
                              'LEASE_CLOSE','close_bk11_service_runtime_v1')
        return {'completed':True,'family_results':results,'republished':False,'next_by_family':{}}
    def drain_once(self,*,limit=1,operation_id=None,on_family_selection=None,on_family_progress=None):
        if self.monitor.mode!='NORMAL' or self.monitor.role!='receiver':
            return self.inner.drain_once(limit=limit,operation_id=operation_id,on_family_selection=on_family_selection)
        self.monitor.need(type(limit) is int and limit==1,'NORMAL_BOUND_REQUIRED','BEFORE_CALL','UNSPECIFIED')
        self.monitor.need(callable(on_family_progress),'CURRENT_RUNTIME_REQUIRED','BEFORE_CALL','UNSPECIFIED')
        results={};delivery=None;pointer=None;family=None;marker=None
        ingress=self.inner._open('PM_OWNER',operation_id or 'family-receive-ingress')
        try:
            custody=ingress.prepare_current_custody(limit)
            on_family_progress('PM_OWNER','CUSTODY_OBSERVED_OWN_CLOSE_PENDING',{'current_backend_custody':custody})
            ingress._attempt('receive_begin')
            ingress._guard('before_pull');delivery=ingress._transport.fetch_one(ingress._cfg.pull_timeout_s)
            if delivery is None:
                proof=ingress._attempt('receive_observed')
                result={'completed':proof.get('effect_admitted') is True,'outcomes':[],
                        'attempt_proof':proof,'next_cursor':None,'receiver_model_work':False}
            else:
                pointer=ingress.validate_delivery(delivery)
                try:family=family_from_referent(pointer.referent_type)
                except ProductionRuntimeRefused:
                    marker={'schema':'bk11-family-ingress-handoff/v1','message_id':pointer.message_id,
                        'pointer_sha256':pointer.raw_sha256,'owner_family':'UNKNOWN','referent_type':pointer.referent_type,
                        'operation_id':operation_id,'state':'HOLD_UNKNOWN_REFERENT'}
                    self.monitor.need(callable(on_family_selection),'CURRENT_RUNTIME_REQUIRED','BROKER_BOUNDARY','UNSPECIFIED')
                    on_family_selection(marker)
                    proof=ingress._attempt('receive_observed')
                    result={'completed':False,'outcomes':[],'attempt_proof':proof,'next_cursor':None,
                            'receiver_model_work':False,'family_handoff_marker':marker}
                else:
                    marker={'schema':'bk11-family-ingress-handoff/v1','message_id':pointer.message_id,
                        'pointer_sha256':pointer.raw_sha256,'owner_family':family,'referent_type':pointer.referent_type,
                        'operation_id':operation_id}
                    if family=='PM_OWNER':
                        outcome=ingress.receive_delivery(delivery,pointer,begin=False)
                        result={**outcome,'outcomes':[outcome],'next_cursor':None,'receiver_model_work':False}
                    else:
                        self.monitor.need(callable(on_family_selection),'CURRENT_RUNTIME_REQUIRED','BROKER_BOUNDARY','UNSPECIFIED')
                        on_family_selection(marker)
                        proof=ingress._attempt('receive_observed')
                        result={'completed':proof.get('effect_admitted') is True,'outcomes':[],
                                'attempt_proof':proof,'next_cursor':None,'receiver_model_work':False}
            result['current_backend_custody']=custody
            ingress.last_result=result;results['PM_OWNER']=result
            on_family_progress('PM_OWNER','OPERATION_OBSERVED_OWN_CLOSE_PENDING',result)
        finally:self.inner._close_current()
        on_family_progress('PM_OWNER','OWN_CLOSE_CONFIRMED',results['PM_OWNER'])
        if result.get('completed') is not True:
            return {**result,'family_results':results}
        target=self.inner._open('ROOM_RETURN',operation_id or 'family-receive-room')
        try:
            custody=target.prepare_current_custody(limit)
            on_family_progress('ROOM_RETURN','CUSTODY_OBSERVED_OWN_CLOSE_PENDING',{'current_backend_custody':custody})
            if delivery is not None and family=='ROOM_RETURN':
                target._attempt('receive_begin')
                outcome=target.receive_delivery(delivery,pointer,begin=False)
                result={**outcome,'outcomes':[outcome],'next_cursor':None,'receiver_model_work':False,
                        'family_handoff_complete':True,'family_handoff_marker':marker}
            else:
                result={'completed':True,'outcomes':[],'attempt_proof':custody['attempt_proof'],
                        'next_cursor':None,'receiver_model_work':False,'broker_delivery_entered':False}
            result['current_backend_custody']=custody
            target.last_result=result;results['ROOM_RETURN']=result
            on_family_progress('ROOM_RETURN','OPERATION_OBSERVED_OWN_CLOSE_PENDING',result)
        finally:self.inner._close_current()
        on_family_progress('ROOM_RETURN','OWN_CLOSE_CONFIRMED',results['ROOM_RETURN'])
        return {'completed':all(v.get('completed') is True for v in results.values()),
                'family_results':results,'outcomes':[o for v in results.values() for o in v['outcomes']],
                'next_cursor':None,'receiver_model_work':False}
    def close(self):
        failures=[]
        try:self.inner._close_current()
        except Exception as error:failures.append(error)
        for lease,record in self.monitor.leases.items():
            if lease not in self.monitor.close_attempts:
                try:self.monitor.call('close_bk11_service_runtime_v1',
                    {'lease_ref':lease,'fencing_epoch':record['fencing_epoch']})
                except Exception as error:failures.append(error)
        try:self.inner.close()
        except Exception as error:failures.append(error)
        if failures:raise failures[0]
