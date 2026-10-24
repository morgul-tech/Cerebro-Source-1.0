"""Production service runtime; no synthetic host, implicit credentials or scheduler.

Context custody endpoints are a required real server boundary, not local flags.
Missing/unregistered/fenced assignment fails closed. D3 owns machine lifecycle and
must bind exact operation/attempt refs before calling this factory or effects.
"""
from __future__ import annotations
import hashlib,json,re
from datetime import datetime
from typing import Any
from .production_config import ProductionD1Config,PROFILE
from .context_owner_port import ContextOwnerOutboxPort,RoomReturnContextOwnerOutboxPort
from .context_service_client import AuthenticatedContextServiceClient
from .publisher import OwnerOutboxPublisher
from .faults import Faults
from .pointer import parse_pointer

REF=re.compile(r'\A[A-Za-z0-9][A-Za-z0-9_.:@/#=-]{0,159}\Z')
CURSOR_SCHEMA='bk11-owner-route-keyset/v1'
class ProductionRuntimeRefused(RuntimeError):pass
def need(ok:bool,code:str):
    if not ok:raise ProductionRuntimeRefused(code)

def family_from_referent(referent_type:str)->str:
    """Use only the two existing owner referents; unknown kinds never default to PM."""
    need(type(referent_type) is str,'OWNER_FAMILY_REFERENT_REQUIRED')
    families={'PM_READY_HINT':'PM_OWNER','ROOM_RETURN':'ROOM_RETURN'}
    need(referent_type in families,'OWNER_FAMILY_REFERENT_UNKNOWN')
    return families[referent_type]

def cursor_validate(value:Any)->dict|None:
    if value is None:return None
    need(type(value) is dict and set(value)=={'schema','enqueued_at','event_id'},'SCOPED_CURSOR_SCHEMA_REQUIRED')
    need(value['schema']==CURSOR_SCHEMA and type(value['event_id']) is str and REF.fullmatch(value['event_id']) is not None,'SCOPED_CURSOR_ID')
    need(type(value['enqueued_at']) is str and len(value['enqueued_at'])<=64,'SCOPED_CURSOR_TIME')
    try:stamp=datetime.fromisoformat(value['enqueued_at'].replace('Z','+00:00'));need(stamp.tzinfo is not None,'SCOPED_CURSOR_TIME')
    except ValueError:raise ProductionRuntimeRefused('SCOPED_CURSOR_TIME') from None
    return {'enqueued_at':value['enqueued_at'],'event_id':value['event_id']}
def cursor_wrap(value):
    if value is None:return None
    wrapped={'schema':CURSOR_SCHEMA,**value};cursor_validate(wrapped);return wrapped

class ServiceLeasedTransport:
    """Fresh server fencing check at each broker effect; no atomic broker lease claim."""
    def __init__(self,inner,guard):self._inner=inner;self._guard=guard
    def publish(self,*args,**kwargs):self._guard('broker.publish');return self._inner.publish(*args,**kwargs)
    def fetch_one(self,*args,**kwargs):self._guard('broker.pull');return self._inner.fetch_one(*args,**kwargs)
    def ack(self,*args,**kwargs):self._guard('broker.ack');return self._inner.ack(*args,**kwargs)
    def nak(self,*args,**kwargs):self._guard('broker.nak');return self._inner.nak(*args,**kwargs)
    def __getattr__(self,name):return getattr(self._inner,name)

class ServiceLeasedOwnerPort(ContextOwnerOutboxPort):
    def __init__(self,client,guard):super().__init__(client);self._guard=guard
    def outbox(self,*args,**kwargs):self._guard('owner.read');return super().outbox(*args,**kwargs)
    def outbox_transition(self,*args,**kwargs):self._guard('owner.settle');return super().outbox_transition(*args,**kwargs)
    def pending(self,*args,**kwargs):self._guard('owner.pending');return super().pending(*args,**kwargs)
    def recovery_candidates(self,*args,**kwargs):self._guard('owner.recovery');return super().recovery_candidates(*args,**kwargs)

class ServiceLeasedRoomReturnOwnerPort(RoomReturnContextOwnerOutboxPort):
    def __init__(self,client,guard):super().__init__(client);self._guard=guard
    def outbox(self,*args,**kwargs):self._guard('owner.read');return super().outbox(*args,**kwargs)
    def outbox_transition(self,*args,**kwargs):self._guard('owner.settle');return super().outbox_transition(*args,**kwargs)
    def pending(self,*args,**kwargs):self._guard('owner.pending');return super().pending(*args,**kwargs)
    def recovery_candidates(self,*args,**kwargs):self._guard('owner.recovery');return super().recovery_candidates(*args,**kwargs)

class ProductionRuntime:
    def __init__(self,*,admission:dict,client:AuthenticatedContextServiceClient,transport:Any,config:ProductionD1Config,
                 owns_resources:bool=True):
        need(type(config) is ProductionD1Config and config.validate().enabled is True,'EXPLICIT_ENABLED_PRODUCTION_CONFIG_REQUIRED')
        need(type(client) is AuthenticatedContextServiceClient,'ACTUAL_CONTEXT_SERVICE_CLIENT_REQUIRED')
        need(type(admission) is dict and admission.get('profile')==PROFILE,'PRODUCTION_ADMISSION_PROFILE_REQUIRED')
        for name in ['assignment_ref','instance_ref','operation_ref','attempt_ref']:
            need(type(admission.get(name)) is str and REF.fullmatch(admission[name]) is not None,'NONSECRET_ADMISSION_REF_REQUIRED')
        need(admission.get('role') in ('publisher','receiver'),'ROLE_REQUIRED')
        self._admission=dict(admission);self._client=client;self._transport=transport;self._cfg=config;self._closed=False
        self._owns_resources=owns_resources
        requested_family=admission.get('owner_family','PM_OWNER')
        need(requested_family in ('PM_OWNER','ROOM_RETURN'),'ADMISSION_OWNER_FAMILY_REQUIRED')
        self._lease=self._client.call('open_bk11_service_runtime_v1',
          {k:admission[k] for k in ['assignment_ref','instance_ref','operation_ref','attempt_ref']} | {'owner_family':requested_family})
        need(self._lease.get('schema')=='cerebro-bk11-service-lease/v1'
          and self._lease.get('assignment_ref')==admission['assignment_ref'] and self._lease.get('role')==admission['role']
          and type(self._lease.get('fencing_epoch')) is int and self._lease['fencing_epoch']>0
          and type(self._lease.get('lease_ref')) is str and REF.fullmatch(self._lease['lease_ref']) is not None,
          'SERVER_CURRENT_ASSIGNMENT_LEASE_REQUIRED')
        self._owner_family=self._lease.get('owner_family')
        need(self._owner_family in ('PM_OWNER','ROOM_RETURN'),'SERVER_OWNER_FAMILY_REQUIRED')
        need(requested_family==self._owner_family,'OWNER_FAMILY_ADMISSION_MISMATCH')
        if 'session_binding_id' in admission:
            need(self._lease.get('session_binding_id')==admission['session_binding_id'],
                 'OWNER_FAMILY_SESSION_BINDING_MISMATCH')
        else:
            need(requested_family=='PM_OWNER','ROOM_RETURN_SESSION_BINDING_PIN_REQUIRED')
        self._guard('open')
        self._transport=ServiceLeasedTransport(transport,self._guard)
        port_type=ServiceLeasedRoomReturnOwnerPort if self._owner_family=='ROOM_RETURN' else ServiceLeasedOwnerPort
        self._port=port_type(client,self._guard)
        self._publisher=OwnerOutboxPublisher(owner=self._port,transport=self._transport,config=config,faults=Faults()) if admission['role']=='publisher' else None

    def _guard(self,seam:str):
        need(not self._closed,'RUNTIME_CLOSED')
        got=self._client.call('verify_bk11_service_lease_v1',{'lease_ref':self._lease['lease_ref'],
          'fencing_epoch':self._lease['fencing_epoch'],'assignment_ref':self._admission['assignment_ref'],'seam':seam})
        need(got.get('schema')=='cerebro-bk11-service-lease-validation/v1' and got.get('valid') is True
          and got.get('assignment_ref')==self._admission['assignment_ref']
          and got.get('owner_family')==self._owner_family
          and got.get('lease_ref')==self._lease['lease_ref'] and got.get('fencing_epoch')==self._lease['fencing_epoch'],
          'SERVER_LEASE_FENCED_OR_CURRENT_ASSIGNMENT_UNKNOWN')

    def _attempt(self,phase:str,obligations=None):
        self._guard(phase)
        args={k:self._admission[k] for k in ['assignment_ref','operation_ref','attempt_ref']}
        args.update({'lease_ref':self._lease['lease_ref'],'fencing_epoch':self._lease['fencing_epoch'],'phase':phase})
        if obligations is not None:
            args['obligations']=[{'message_id':r['message_id'],'pointer_sha256':r['pointer_sha256']} for r in obligations]
        if phase=='recovery' and self._admission.get('reconcile_attempt_ref'):
            args['reconcile_attempt_ref']=self._admission['reconcile_attempt_ref']
        proof=self._client.call('control_bk11_service_attempt_v1',args)
        need(proof.get('schema')=='cerebro-bk11-server-attempt-proof/v1'
          and all(proof.get(k)==self._admission[k] for k in ['assignment_ref','operation_ref','attempt_ref'])
          and proof.get('owner_family')==self._owner_family
          and proof.get('lease_ref')==self._lease['lease_ref'] and proof.get('fencing_epoch')==self._lease['fencing_epoch'],
          'MATCHING_SERVER_ATTEMPT_PROOF_REQUIRED')
        if phase!='recovery' and not phase.endswith('_observed'):
            need(proof.get('effect_admitted') is True,'SERVER_RECONCILIATION_REQUIRED_BEFORE_EFFECT')
        return proof

    def publish_pending_once(self,*,limit:int=8,after=None):
        need(self._publisher is not None,'RECEIVER_PUBLISH_DENIED');need(type(limit) is int and 1<=limit<=8,'BOUNDED_WORK_REQUIRED')
        self._guard('publisher_discovery');rows,nxt,drain=self._port.pending(after=cursor_validate(after),limit=limit)
        self._attempt('publish_begin',rows)
        outcomes=[];budget=self._publisher.new_budget()
        if drain:
            for row in rows:
                self._guard('before_owner_claim_and_send');outcomes.append({'message_id':row['message_id'],'state':self._publisher.publish(row['message_id'],budget=budget)})
        proof=self._attempt('publish_observed')
        return {'completed':drain and proof.get('effect_admitted') is True,'next_cursor':cursor_wrap(nxt),'outcomes':outcomes,'attempt_proof':proof,
                'publication_reconciled':False,'receiver_work':'NOT_CLAIMED'}

    def recover_pending_once(self,*,limit:int=8,after=None):
        need(type(limit) is int and 1<=limit<=8,'BOUNDED_WORK_REQUIRED');self._attempt('recovery')
        if self._admission['role']=='receiver':
            page=self._client.call('discover_bk11_backend_obligations_v1',{'lease_ref':self._lease['lease_ref'],
              'fencing_epoch':self._lease['fencing_epoch'],'limit':limit,'after':cursor_validate(after)})
            need(page.get('schema')=='cerebro-bk11-backend-discovery/v1' and type(page.get('rows'))is list
              and len(page['rows'])<=limit,'BOUNDED_SERVER_BACKEND_DISCOVERY_REQUIRED')
            rows=page['rows'];self._attempt('recovery_receive_begin',rows);outcomes=[]
            for row in rows:
                readback=self._accept_readback(row['message_id'],row['pointer_sha256'])
                outcomes.append({'message_id':row['message_id'],'backend_disposition':readback['status'],
                  'source':'FRESH_SCOPED_OWNERTRUTH','model_wake':False})
            observed=self._attempt('recovery_receive_observed')
            proof=self._attempt('recovery')
            return {'completed':observed.get('effect_admitted') is True,'next_cursor':cursor_wrap(page.get('next')),'attempt_proof':proof,
              'outcomes':outcomes,'republished':False,
              'publication_reconciled':proof.get('reconciled_original_attempt') is True
                and proof.get('reconciled_attempt_ref')==self._admission.get('reconcile_attempt_ref')
                and self._admission.get('reconcile_attempt_ref') is not None,'downstream_work':'PENDING_NOT_MODEL_EXECUTED'}
        rows,nxt,drain=self._port.recovery_candidates(after=cursor_validate(after),limit=limit)
        outcomes=[]
        for row in rows:
            self._guard('recovery_ownertruth')
            got=self._client.call('reconcile_bk11_backend_obligation_v1',{'lease_ref':self._lease['lease_ref'],
              'fencing_epoch':self._lease['fencing_epoch'],'message_id':row['message_id'],'pointer_sha256':row['pointer_sha256'],
              'attempt_ref':self._admission['attempt_ref'],'operation_ref':self._admission['operation_ref']})
            need(got.get('message_id')==row['message_id'] and got.get('pointer_sha256')==row['pointer_sha256'],'RECOVERY_IDENTITY_MISMATCH')
            outcomes.append({'message_id':row['message_id'],'state':got.get('state','UNRESOLVED')})
        proof=self._attempt('recovery')
        return {'completed':True,'next_cursor':cursor_wrap(nxt),'outcomes':outcomes,'attempt_proof':proof,
                'publication_reconciled':proof.get('reconciled_original_attempt') is True
                    and proof.get('reconciled_attempt_ref')==self._admission.get('reconcile_attempt_ref')
                    and self._admission.get('reconcile_attempt_ref') is not None,'republished':False}

    def drain_once(self,*,limit:int=8):
        need(self._admission['role']=='receiver','PUBLISHER_DRAIN_DENIED');need(type(limit) is int and 1<=limit<=8,'BOUNDED_WORK_REQUIRED')
        self._attempt('receive_begin');outcomes=[]
        for _ in range(limit):
            self._guard('before_pull');delivery=self._transport.fetch_one(self._cfg.pull_timeout_s)
            if delivery is None:break
            pointer=parse_pointer(delivery.data,max_ttl_seconds=self._cfg.pointer_ttl_s)
            need(delivery.headers.get('Nats-Msg-Id')==pointer.message_id,'DELIVERY_MESSAGE_ID_MISMATCH')
            need(family_from_referent(pointer.referent_type)==self._owner_family,'CROSS_FAMILY_DELIVERY_REQUIRES_ROLE_INGRESS')
            self._attempt('receive_item',[{'message_id':pointer.message_id,'pointer_sha256':pointer.raw_sha256}])
            readback=self._accept_readback(pointer.message_id,pointer.raw_sha256)
            self._guard('before_ack');self._transport.ack(delivery)
            outcomes.append({'message_id':pointer.message_id,'backend_disposition':readback['status'],'ack':'TRANSIT_ONLY','model_wake':False})
        proof=self._attempt('receive_observed')
        return {'completed':proof.get('effect_admitted') is True,'next_cursor':None,'outcomes':outcomes,'attempt_proof':proof,'receiver_model_work':False}

    def _accept_readback(self,message_id,pointer_sha256):
            self._guard('before_backend_custody')
            args={'lease_ref':self._lease['lease_ref'],'fencing_epoch':self._lease['fencing_epoch'],
                  'message_id':message_id,'pointer_sha256':pointer_sha256}
            written=self._client.call('accept_bk11_backend_delivery_v1',args)
            readback=self._client.call('read_bk11_backend_disposition_v1',args)
            need(written==readback and readback.get('schema')=='cerebro-bk11-backend-disposition/v1'
              and readback.get('message_id')==message_id and readback.get('pointer_sha256')==pointer_sha256
              and readback.get('owner_family')==self._owner_family
              and readback.get('receiver_assignment_ref')==self._admission['assignment_ref']
              and readback.get('delivery_responsibility')=='DURABLE_BACKEND_CUSTODY'
              and readback.get('downstream_work')=='PENDING_NOT_MODEL_EXECUTED','DURABLE_SERVER_DISPOSITION_READBACK_REQUIRED_BEFORE_ACK')
            return readback

    def validate_delivery(self,delivery):
        pointer=parse_pointer(delivery.data,max_ttl_seconds=self._cfg.pointer_ttl_s)
        need(delivery.headers.get('Nats-Msg-Id')==pointer.message_id,'DELIVERY_MESSAGE_ID_MISMATCH')
        return pointer

    def receive_delivery(self,delivery,pointer,*,begin:bool=True):
        need(family_from_referent(pointer.referent_type)==self._owner_family,'CROSS_FAMILY_DELIVERY_REFUSED')
        need(delivery.headers.get('Nats-Msg-Id')==pointer.message_id,'DELIVERY_MESSAGE_ID_MISMATCH')
        if begin:self._attempt('receive_begin')
        self._attempt('receive_item',[{'message_id':pointer.message_id,'pointer_sha256':pointer.raw_sha256}])
        readback=self._accept_readback(pointer.message_id,pointer.raw_sha256)
        self._guard('before_ack');self._transport.ack(delivery)
        proof=self._attempt('receive_observed')
        return {'completed':proof.get('effect_admitted') is True,'message_id':pointer.message_id,
          'owner_family':self._owner_family,'backend_disposition':readback['status'],'ack':'TRANSIT_ONLY','model_wake':False}

    def close(self,*,close_resources:bool|None=None):
        if self._closed:return
        if close_resources is None:close_resources=self._owns_resources
        try:
            self._client.call('close_bk11_service_runtime_v1',{'lease_ref':self._lease['lease_ref'],
                'fencing_epoch':self._lease['fencing_epoch']})
        finally:
            self._closed=True
            if close_resources:
                try:self._transport.close()
                finally:self._client.close()


class FamilyPlanRuntime:
    """Serial owner-family leases over one existing role client and shared pull consumer."""
    def __init__(self,*,families:dict,open_family,client,transport,role:str,ingress_writer=None):
        need(set(families)=={'PM_OWNER','ROOM_RETURN'},'EXACT_TWO_FAMILY_PLAN_REQUIRED')
        self._families=families;self._open_family=open_family;self._client=client;self._transport=transport
        self._role=role;self._ingress_writer=ingress_writer;self._current=None;self._closed=False;self._counter=0

    def _operation_id(self,family,value):
        if type(value) is dict:return value.get(family)
        return value

    @staticmethod
    def attempt_ref_for(operation_id,family):
        token=hashlib.sha256(f'{operation_id}:{family}'.encode('utf-8')).hexdigest()[:32]
        return 'a:'+token

    def _open(self,family,operation_id,reconcile_attempt_ref=None):
        need(not self._closed and self._current is None,'FAMILY_RUNTIME_OVERLAP_OR_CLOSED')
        self._counter+=1
        operation_id=self._operation_id(family,operation_id)
        need(type(operation_id)is str and REF.fullmatch(operation_id) is not None,'FAMILY_OPERATION_ID_REQUIRED')
        self._current=self._open_family(family,operation_id,self._counter,reconcile_attempt_ref)
        need(self._current._owner_family==family,'SERVER_FAMILY_SELECTION_MISMATCH')
        return self._current

    def _close_current(self):
        if self._current is None:return
        current,self._current=self._current,None
        current.close(close_resources=False)

    def recover_pending_once(self,*,limit:int=1,after=None,reconcile_operation=None,
                             reconcile_attempt_refs=None,operation_id=None):
        need(type(limit)is int and 1<=limit<=1,'FAMILY_TICK_LIMIT_ONE_REQUIRED')
        outcomes={};next_by_family={};proofs=[]
        previous=reconcile_operation if type(reconcile_operation)is dict else {}
        prior_refs=reconcile_attempt_refs if type(reconcile_attempt_refs)is dict else {}
        for family in ('PM_OWNER','ROOM_RETURN'):
            current_id=self._operation_id(family,operation_id)
            previous_id=previous.get(family)
            prior_attempt=prior_refs.get(family)
            if prior_attempt is None and type(previous_id)is str:
                prior_attempt=self.attempt_ref_for(previous_id,family)
            need(prior_attempt is None or (type(prior_attempt)is str and REF.fullmatch(prior_attempt) is not None),
                 'FAMILY_RECONCILE_ATTEMPT_REF_INVALID')
            rt=self._open(family,current_id or 'family-recovery',prior_attempt)
            try:
                result=rt.recover_pending_once(limit=limit,after=(after or {}).get(family) if type(after)is dict else after)
                outcomes[family]=result;next_by_family[family]=result.get('next_cursor');proofs.append(result.get('attempt_proof',{}))
            finally:self._close_current()
        complete=all(type(v) is dict and v.get('completed') is True for v in outcomes.values())
        return {'completed':complete,'next_by_family':next_by_family,'family_results':outcomes,
          'publication_reconciled':complete and all(p.get('reconciled_original_attempt') is True for p in proofs),
          'reconciled_operation_id':operation_id,'republished':False,'downstream_work':'PENDING_NOT_MODEL_EXECUTED'}

    def publish_pending_once(self,*,limit:int=1,after=None,operation_id=None):
        need(self._role=='publisher','RECEIVER_PUBLISH_DENIED')
        need(type(limit)is int and 1<=limit<=1,'FAMILY_TICK_LIMIT_ONE_REQUIRED')
        results={};next_by_family={}
        for family in ('PM_OWNER','ROOM_RETURN'):
            rt=self._open(family,operation_id or 'family-publish')
            try:
                result=rt.publish_pending_once(limit=limit,after=(after or {}).get(family) if type(after)is dict else after)
                results[family]=result;next_by_family[family]=result.get('next_cursor')
            finally:self._close_current()
        return {'completed':all(v.get('completed') is True for v in results.values()),
          'family_results':results,'next_by_family':next_by_family,'publication_reconciled':False,'republished':False}

    def drain_once(self,*,limit:int=1,operation_id=None,on_family_selection=None):
        need(self._role=='receiver','PUBLISHER_DRAIN_DENIED')
        need(type(limit)is int and 1<=limit<=1,'FAMILY_TICK_LIMIT_ONE_REQUIRED')
        ingress=self._open('PM_OWNER',operation_id or 'family-receive-ingress')
        try:
            ingress._attempt('receive_begin')
            ingress._guard('before_pull');delivery=ingress._transport.fetch_one(ingress._cfg.pull_timeout_s)
            if delivery is None:
                proof=ingress._attempt('receive_observed')
                return {'completed':proof.get('effect_admitted') is True,'outcomes':[],'attempt_proof':proof,
                  'next_cursor':None,'receiver_model_work':False}
            pointer=ingress.validate_delivery(delivery)
            try:family=family_from_referent(pointer.referent_type)
            except ProductionRuntimeRefused:
                marker={'schema':'bk11-family-ingress-handoff/v1','message_id':pointer.message_id,
                  'pointer_sha256':pointer.raw_sha256,'owner_family':'UNKNOWN','referent_type':pointer.referent_type,
                  'operation_id':operation_id,'state':'HOLD_UNKNOWN_REFERENT'}
                need(callable(on_family_selection),'FAMILY_INGRESS_DURABLE_HOLD_REQUIRED')
                on_family_selection(marker)
                proof=ingress._attempt('receive_observed')
                return {'completed':False,'outcomes':[],'attempt_proof':proof,'next_cursor':None,
                  'receiver_model_work':False,'family_handoff_marker':marker}
            marker={'schema':'bk11-family-ingress-handoff/v1','message_id':pointer.message_id,
              'pointer_sha256':pointer.raw_sha256,'owner_family':family,'referent_type':pointer.referent_type,
              'operation_id':operation_id}
            if family!='PM_OWNER':
                need(callable(on_family_selection),'FAMILY_HANDOFF_DURABLE_MARKER_REQUIRED')
                on_family_selection(marker)
            if family=='PM_OWNER':
                outcome=ingress.receive_delivery(delivery,pointer,begin=False)
                return {**outcome,'outcomes':[outcome],'next_cursor':None,'receiver_model_work':False}
            ingress._attempt('receive_observed')
        except Exception:
            raise
        finally:
            self._close_current()
        target=self._open(family,operation_id or 'family-receive-room')
        try:
            target._attempt('receive_begin')
            outcome=target.receive_delivery(delivery,pointer,begin=False)
            return {**outcome,'outcomes':[outcome],'next_cursor':None,'receiver_model_work':False,
              'family_handoff_complete':True,'family_handoff_marker':marker}
        finally:self._close_current()

    def close(self):
        if self._closed:return
        try:self._close_current()
        finally:
            self._closed=True
            try:self._transport.close()
            finally:self._client.close()
