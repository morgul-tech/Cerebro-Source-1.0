"""Authenticated Context-owned backend custody and service fencing, never a native actor claim.

Requires real schema + installation-owned service assignments. No fixture fallback.
An HTTP/MCP host supplies its freshly verified identity and current transport session.
Runtime service role is read from protected rows, never accepted from caller flags.
"""
from __future__ import annotations
import json,re,secrets
from datetime import datetime
from contextlib import contextmanager
from typing import Any
from pm_owner_atomic import _row

PROFILE='BK11_CONTEXT_EDGE_BACKEND_PRODUCTION/v1'
REF=re.compile(r'\A[A-Za-z0-9][A-Za-z0-9_.:@/#=-]{0,159}\Z')
MID=re.compile(r'\Ad1:[a-f0-9]{40}\Z');HASH=re.compile(r'\A[a-f0-9]{64}\Z')
class Bk11CustodyRefused(RuntimeError):pass
def need(ok,code):
 if not ok:raise Bk11CustodyRefused(code)
def reference(value):need(type(value)is str and REF.fullmatch(value)is not None,'REFERENCE_INVALID');return value

class ContextBk11ServiceCustody:
 def __init__(self,state_port:Any):self._state=state_port

 @contextmanager
 def _transaction(self,identity,session,*,write,args,require_drain=True):
  identity.validate()
  need(('project_state:transition' if write else 'project_state:read') in identity.state_scopes,'SERVICE_SCOPE_REQUIRED')
  need(isinstance(session,dict) and type(session.get('session_ref'))is str,'CURRENT_SERVICE_SESSION_REQUIRED')
  try:
   with self._state._transaction(tenant_ref=identity.tenant_ref,workspace_ref=identity.workspace_ref,
     principal_ref=identity.principal_ref,read_only=not write,repeatable_read=not write,passthrough_errors=(Bk11CustodyRefused,)) as cursor:
    if write:
     # Installation must acquire this same transaction lock before changing binding.
     # No runtime UPDATE privilege is granted on installation-owned assignment rows.
     lock_key=json.dumps([identity.tenant_ref,identity.workspace_ref,identity.principal_ref,identity.consumer_ref,session['session_ref']],separators=(',',':'))
     cursor.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(lock_key,))
    if 'lease_ref' in args:
     reference(args['lease_ref'])
     cursor.execute('''SELECT a.*, now() AS server_now FROM cerebro_bk11_service_assignments a
       JOIN cerebro_bk11_service_leases l ON l.assignment_ref=a.assignment_ref
       WHERE a.tenant_ref=%s AND a.workspace_ref=%s AND a.principal_ref=%s AND a.consumer_ref=%s
        AND a.session_ref=%s AND l.lease_ref=%s LIMIT 2''',
       (identity.tenant_ref,identity.workspace_ref,identity.principal_ref,identity.consumer_ref,
        session['session_ref'],args['lease_ref']))
     assignment=cursor.fetchone(); ambiguous=cursor.fetchone()
     need(assignment is not None and ambiguous is None,'SERVICE_LEASE_ASSIGNMENT_AMBIGUOUS_OR_MISSING')
    else:
     family=args.get('owner_family','PM_OWNER')
     need(family in ('PM_OWNER','ROOM_RETURN'),'SERVICE_OWNER_FAMILY_INVALID')
     reference(args.get('assignment_ref'))
     cursor.execute('''SELECT *, now() AS server_now FROM cerebro_bk11_service_assignments WHERE tenant_ref=%s
       AND workspace_ref=%s AND principal_ref=%s AND consumer_ref=%s AND session_ref=%s
       AND assignment_ref=%s AND owner_family=%s LIMIT 2''',
       (identity.tenant_ref,identity.workspace_ref,identity.principal_ref,identity.consumer_ref,
        session['session_ref'],args['assignment_ref'],family))
     assignment=cursor.fetchone(); ambiguous=cursor.fetchone()
     need(assignment is not None and ambiguous is None,'SERVICE_ASSIGNMENT_FAMILY_OR_REFERENCE_MISMATCH')
    need(assignment is not None and assignment['enabled'] is True and assignment['profile']==PROFILE
      and assignment['expires_at']>assignment['server_now'],'SERVICE_ASSIGNMENT_UNAVAILABLE_OR_EXPIRED')
    current=self._state._load_session(cursor,tenant_ref=identity.tenant_ref,workspace_ref=identity.workspace_ref,
      principal_ref=identity.principal_ref,consumer_ref=identity.consumer_ref,session_ref=assignment['session_ref'],for_update=False,required=False)
    project=self._state._load_project(cursor,tenant_ref=identity.tenant_ref,workspace_ref=identity.workspace_ref,
      project_ref=assignment['project_ref'],required=False)
    need(current is not None and project is not None and project['project_status']=='ACTIVE'
      and current['project_ref']==assignment['project_ref'] and current['project_revision']==project['revision']
      and current['session_binding_id']==assignment['session_binding_id'],'CURRENT_SERVICE_ASSIGNMENT_STALE')
    cursor.execute('''SELECT *, now() AS server_now FROM cerebro_pm_owner_d1_routes WHERE tenant_ref=%s AND workspace_ref=%s
     AND owner_ref=%s AND route_ref=%s''',
     (assignment['tenant_ref'],assignment['workspace_ref'],assignment['owner_ref'],assignment['route_ref']))
    route=_row(cursor)
    need(route is not None and route['project_ref']==assignment['project_ref'] and route['receiver_ref']==assignment['receiver_ref'],
     'SERVICE_ROUTE_SCOPE_MISMATCH')
    need(assignment.get('owner_family','PM_OWNER') in ('PM_OWNER','ROOM_RETURN'), 'SERVICE_OWNER_FAMILY_INVALID')
    if assignment.get('owner_family','PM_OWNER')=='ROOM_RETURN':
     need(type(assignment.get('room_owner_ref')) is str and REF.fullmatch(assignment['room_owner_ref']) is not None,
      'ROOM_RETURN_OWNER_BINDING_REQUIRED')
     need((route['binding_revision'],route['pointer_ttl_seconds'])==
       (assignment['route_binding_revision'],assignment['pointer_ttl_seconds']),
       'ROOM_RETURN_TRANSPORT_BINDING_STALE')
    need(not require_drain or route['drain_enabled'] is True,'SERVICE_ROUTE_DRAIN_DISABLED')
    yield cursor,assignment
  except Bk11CustodyRefused:raise
  except Exception:raise Bk11CustodyRefused('PROTECTED_SERVICE_CUSTODY_UNAVAILABLE') from None

 def _lease(self,cursor,a,args,*,write=False):
  need(args.get('assignment_ref',a['assignment_ref'])==a['assignment_ref'],'ASSIGNMENT_MISMATCH')
  cursor.execute('SELECT *, now() AS server_now FROM cerebro_bk11_service_leases WHERE assignment_ref=%s'+(' FOR SHARE' if write else ''),(a['assignment_ref'],))
  lease=_row(cursor)
  need(lease is not None and lease['lease_ref']==args.get('lease_ref') and type(args.get('fencing_epoch'))is int
   and lease['fencing_epoch']==args['fencing_epoch'] and lease['assignment_revision']==a['assignment_revision']
   and lease['expires_at']>lease['server_now'],'SERVICE_LEASE_FENCED_OR_EXPIRED')
  return lease

 def open_runtime(self,args,identity,session):
  required={'assignment_ref','instance_ref','operation_ref','attempt_ref'}
  need(required<=set(args) and set(args)<=required|{'owner_family'},'OPEN_EXACT_FIELDS')
  for key in required:reference(args[key])
  family=args.get('owner_family','PM_OWNER')
  need(family in ('PM_OWNER','ROOM_RETURN'),'OPEN_OWNER_FAMILY_INVALID')
  with self._transaction(identity,session,write=True,args=args) as (c,a):
   need(args['assignment_ref']==a['assignment_ref'],'ASSIGNMENT_MISMATCH')
   c.execute('SELECT * FROM cerebro_bk11_operation_attempts WHERE assignment_ref=%s AND attempt_ref=%s FOR UPDATE',
     (a['assignment_ref'],args['attempt_ref']))
   prior=_row(c)
   need(prior is None or prior['operation_ref']==args['operation_ref'],'ATTEMPT_OPERATION_REBIND_DENIED')
   # Protected transaction lock above serializes creation even before a lease exists.
   c.execute('SELECT *, now() AS server_now FROM cerebro_bk11_service_leases WHERE assignment_ref=%s FOR UPDATE',(a['assignment_ref'],));old=_row(c)
   need(old is None or old['expires_at']<=old['server_now'],'ACTIVE_SERVICE_INSTANCE_LEASE_HELD')
   lease_ref='bk11lease:'+secrets.token_hex(20);epoch=1 if old is None else old['fencing_epoch']+1
   c.execute('''INSERT INTO cerebro_bk11_service_leases(assignment_ref,lease_ref,instance_ref,fencing_epoch,assignment_revision,expires_at)
    VALUES(%s,%s,%s,%s,%s,least(%s::timestamptz,now()+%s*interval '1 second'))
    ON CONFLICT(assignment_ref) DO UPDATE SET lease_ref=excluded.lease_ref,instance_ref=excluded.instance_ref,
    fencing_epoch=excluded.fencing_epoch,assignment_revision=excluded.assignment_revision,expires_at=excluded.expires_at''',
    (a['assignment_ref'],lease_ref,args['instance_ref'],epoch,a['assignment_revision'],a['expires_at'],a['lease_ttl_s']))
   c.execute('''INSERT INTO cerebro_bk11_operation_attempts(assignment_ref,operation_ref,attempt_ref,phase,outcome)
    VALUES(%s,%s,%s,'OPEN','NO_EFFECT_ADMITTED') ON CONFLICT(assignment_ref,attempt_ref) DO NOTHING''',
    (a['assignment_ref'],args['operation_ref'],args['attempt_ref']))
  return {'schema':'cerebro-bk11-service-lease/v1','assignment_ref':a['assignment_ref'],'role':a['service_role'],
   'owner_family':a['owner_family'],
   'session_binding_id':a['session_binding_id'],
   'lease_ref':lease_ref,'fencing_epoch':epoch,'authority':'AUTHENTICATED_CONTEXT_SERVICE_ASSIGNMENT_NOT_NATIVE_ACTOR'}

 def verify_lease(self,args,identity,session):
  need(set(args)=={'assignment_ref','lease_ref','fencing_epoch','seam'},'VERIFY_EXACT_FIELDS');reference(args['seam'])
  with self._transaction(identity,session,write=False,args=args) as (c,a):lease=self._lease(c,a,args)
  return {'schema':'cerebro-bk11-service-lease-validation/v1','valid':True,'assignment_ref':a['assignment_ref'],
   'owner_family':a['owner_family'],'session_binding_id':a['session_binding_id'],
   'lease_ref':lease['lease_ref'],'fencing_epoch':lease['fencing_epoch']}

 def close_runtime(self,args,identity,session):
  need(set(args)=={'lease_ref','fencing_epoch'},'CLOSE_EXACT_FIELDS')
  with self._transaction(identity,session,write=True,args=args,require_drain=False) as (c,a):
   lease=self._lease(c,a,args,write=True)
   c.execute('UPDATE cerebro_bk11_service_leases SET expires_at=now() WHERE assignment_ref=%s AND lease_ref=%s AND fencing_epoch=%s',
     (a['assignment_ref'],lease['lease_ref'],lease['fencing_epoch']))
  return {'closed':True,'pending_owner_and_backend_data_preserved':True}

 def _obligation(self,c,a,args):
  need(type(args.get('message_id'))is str and MID.fullmatch(args['message_id'])is not None
   and type(args.get('pointer_sha256'))is str and HASH.fullmatch(args['pointer_sha256'])is not None,'OBLIGATION_IDENTITY_INVALID')
  if a.get('owner_family','PM_OWNER')=='ROOM_RETURN':
   c.execute('''SELECT *,expires_at::timestamptz AS pointer_expiry_at,
     owner_event_key AS event_id FROM cerebro_room_return_d1_outbox WHERE tenant_ref=%s AND workspace_ref=%s
     AND owner_ref=%s AND route_owner_ref=%s AND route_ref=%s AND receiver_ref=%s
     AND message_id=%s AND pointer_sha256=%s''',
     (a['tenant_ref'],a['workspace_ref'],a['room_owner_ref'],a['owner_ref'],a['route_ref'],a['receiver_ref'],
      args['message_id'],args['pointer_sha256']))
   row=_row(c);need(row is not None,'SCOPED_ROOM_RETURN_OBLIGATION_NOT_FOUND');return row
  c.execute('''SELECT *,expires_at::timestamptz AS pointer_expiry_at FROM cerebro_pm_owner_d1_outbox WHERE tenant_ref=%s AND workspace_ref=%s AND owner_ref=%s
   AND route_ref=%s AND receiver_ref=%s AND message_id=%s AND pointer_sha256=%s''',
   (a['tenant_ref'],a['workspace_ref'],a['owner_ref'],a['route_ref'],a['receiver_ref'],args['message_id'],args['pointer_sha256']))
  row=_row(c);need(row is not None,'SCOPED_OWNER_OBLIGATION_NOT_FOUND');return row

 def _disposition(self,c,a,args):
  service_role=a.get('service_role')
  need(service_role in ('publisher','receiver'),'DURABLE_DISPOSITION_SERVICE_ROLE_REQUIRED')
  c.execute('''SELECT * FROM cerebro_bk11_backend_dispositions WHERE tenant_ref=%s AND workspace_ref=%s
   AND route_ref=%s AND receiver_ref=%s AND message_id=%s AND owner_family=%s''',
   (a['tenant_ref'],a['workspace_ref'],a['route_ref'],a['receiver_ref'],args['message_id'],a['owner_family']))
  row=_row(c)
  if row is None:return None
  need(row['pointer_sha256']==args['pointer_sha256'] and row['owner_family']==a['owner_family'],
   'DURABLE_DISPOSITION_IDENTITY_CONFLICT')
  if service_role=='receiver':
   need(row['receiver_assignment_ref']==a['assignment_ref'],'DURABLE_DISPOSITION_IDENTITY_CONFLICT')
  else:
   # The scoped immutable receipt FK and receiver-only insert policy attest this identity.
   reference(row['receiver_assignment_ref'])
  if a.get('owner_family','PM_OWNER')=='ROOM_RETURN':
   owner=self._obligation(c,a,args)
   need(row['owner_event_id']==owner['event_id'] and row['owner_seq']==owner['owner_seq']
    ,'ROOM_RETURN_BACKEND_OWNER_READBACK_MISMATCH')
  return {'schema':'cerebro-bk11-backend-disposition/v1','message_id':row['message_id'],'pointer_sha256':row['pointer_sha256'],
   'status':row['status'],'owner_family':row['owner_family'],'receiver_assignment_ref':row['receiver_assignment_ref'],'owner_event_id':row['owner_event_id'],
   'owner_seq':row['owner_seq'],'delivery_responsibility':'DURABLE_BACKEND_CUSTODY','downstream_work':row['downstream_work']}

 def accept_delivery(self,args,identity,session):
  need(set(args)=={'lease_ref','fencing_epoch','message_id','pointer_sha256'},'ACCEPT_EXACT_FIELDS')
  with self._transaction(identity,session,write=True,args=args) as (c,a):
   self._lease(c,a,args,write=True);need(a['service_role']=='receiver','BACKEND_RECEIVER_SERVICE_REQUIRED');owner=self._obligation(c,a,args)
   # Original delivery duty persists. Expiry never grants action on old facts or rewrites TTL.
   c.execute('SELECT now() AS server_now');now=_row(c)['server_now']
   status='EXPIRED_NEEDS_OWNER_DISPOSITION' if owner['pointer_expiry_at']<=now else 'ACCEPTED_PENDING_WORK'
   c.execute('''INSERT INTO cerebro_bk11_backend_dispositions(tenant_ref,workspace_ref,route_ref,receiver_ref,message_id,
    pointer_sha256,owner_event_id,owner_seq,status,receiver_assignment_ref,owner_family) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    ON CONFLICT(tenant_ref,workspace_ref,route_ref,receiver_ref,message_id,owner_family) DO NOTHING''',
    (a['tenant_ref'],a['workspace_ref'],a['route_ref'],a['receiver_ref'],owner['message_id'],owner['pointer_sha256'],
     owner['event_id'],owner['owner_seq'],status,a['assignment_ref'],a['owner_family']))
   result=self._disposition(c,a,args)
  return result  # Returned only after commit; client performs independent readback before ACK.

 def read_disposition(self,args,identity,session):
  need(set(args)=={'lease_ref','fencing_epoch','message_id','pointer_sha256'},'READ_EXACT_FIELDS')
  with self._transaction(identity,session,write=False,args=args) as (c,a):self._lease(c,a,args);self._obligation(c,a,args);result=self._disposition(c,a,args)
  return result if result is not None else {'schema':'cerebro-bk11-disposition-unresolved/v1','pending':True}

 def discover_backend_obligations(self,args,identity,session):
  """Receiver-owned fresh ownertruth recovery; no pointer required, no old facts acted on."""
  need(set(args)=={'lease_ref','fencing_epoch','limit','after'},'DISCOVERY_EXACT_FIELDS')
  need(type(args['limit']) is int and 1<=args['limit']<=8,'DISCOVERY_BOUND_REQUIRED')
  after=args['after'];values=[]
  if after is not None:
   need(type(after)is dict and set(after)=={'enqueued_at','event_id'},'DISCOVERY_CURSOR_FIELDS')
   reference(after['event_id']);need(type(after['enqueued_at'])is str and len(after['enqueued_at'])<=64,'DISCOVERY_CURSOR_TIME')
   try:stamp=datetime.fromisoformat(after['enqueued_at'].replace('Z','+00:00'))
   except ValueError:raise Bk11CustodyRefused('DISCOVERY_CURSOR_TIME') from None
   need(stamp.tzinfo is not None,'DISCOVERY_CURSOR_TIME')
   values=[after['enqueued_at'],after['event_id']]
  with self._transaction(identity,session,write=False,args=args) as (c,a):
   self._lease(c,a,args);need(a['service_role']=='receiver','BACKEND_RECEIVER_SERVICE_REQUIRED')
   family=a.get('owner_family','PM_OWNER')
   table='cerebro_room_return_d1_outbox' if family=='ROOM_RETURN' else 'cerebro_pm_owner_d1_outbox'
   owner_col='owner_ref'
   route_owner_clause=' AND o.route_owner_ref=%s' if family=='ROOM_RETURN' else ''
   event_col='owner_event_key' if family=='ROOM_RETURN' else 'event_id'
   cursor_col='owner_event_key' if family=='ROOM_RETURN' else 'event_id'
   keyset=(' AND (o.enqueued_at,o.'+cursor_col+')>(%s::timestamptz,%s)') if after is not None else ''
   route_owner=(a['owner_ref'],) if family=='ROOM_RETURN' else ()
   c.execute(('''SELECT o.message_id,o.pointer_sha256,o.enqueued_at,o.'''+event_col+''' AS event_id FROM '''+table+''' o
    WHERE o.tenant_ref=%s AND o.workspace_ref=%s AND o.'''+owner_col+'''=%s AND o.route_ref=%s AND o.receiver_ref=%s'''+route_owner_clause+'''
    AND NOT EXISTS (SELECT 1 FROM cerebro_bk11_backend_dispositions d WHERE d.tenant_ref=o.tenant_ref
     AND d.workspace_ref=o.workspace_ref AND d.route_ref=o.route_ref AND d.receiver_ref=o.receiver_ref
     AND d.message_id=o.message_id AND d.pointer_sha256=o.pointer_sha256 AND d.owner_family=%s)'''+keyset+
    ' ORDER BY o.enqueued_at,o.'+cursor_col+' LIMIT %s'),
    (a['tenant_ref'],a['workspace_ref'],a.get('room_owner_ref') if family=='ROOM_RETURN' else a['owner_ref'],
     a['route_ref'],a['receiver_ref'],*route_owner,a['owner_family'],*values,args['limit']+1))
   found=[]
   for _ in range(args['limit']+1):
    row=_row(c)
    if row is None:break
    found.append(row)
   rows=found[:args['limit']];nxt=None
   if len(found)>args['limit']:
    last=rows[-1];nxt={'enqueued_at':last['enqueued_at'].isoformat(),'event_id':last['event_id']}
  return {'schema':'cerebro-bk11-backend-discovery/v1',
    'rows':[{'message_id':r['message_id'],'pointer_sha256':r['pointer_sha256']} for r in rows],'next':nxt}

 def reconcile_obligation(self,args,identity,session):
  need(set(args)=={'lease_ref','fencing_epoch','message_id','pointer_sha256','attempt_ref','operation_ref'},'RECONCILE_EXACT_FIELDS')
  with self._transaction(identity,session,write=False,args=args) as (c,a):
   self._lease(c,a,args);need(a['service_role']=='publisher','OWNER_RECOVERY_PUBLISHER_REQUIRED')
   owner=self._obligation(c,a,args);disposition=self._disposition(c,a,args)
   state='MATCHED_DURABLE_BACKEND_DISPOSITION' if disposition else 'UNRESOLVED_OWNER_DUTY'
  return {'message_id':owner['message_id'],'pointer_sha256':owner['pointer_sha256'],'state':state,'republished':False}

 def control_attempt(self,args,identity,session):
  base={'assignment_ref','operation_ref','attempt_ref','lease_ref','fencing_epoch','phase'}
  need(base<=set(args) and set(args)<=base|{'obligations','reconcile_attempt_ref'},'ATTEMPT_EXACT_FIELDS')
  for k in ['assignment_ref','operation_ref','attempt_ref']:reference(args[k])
  phase=args['phase'];need(phase in ('publish_begin','publish_observed','receive_begin','receive_item','receive_observed',
    'recovery_receive_begin','recovery_receive_observed','recovery'),'ATTEMPT_PHASE_INVALID')
  with self._transaction(identity,session,write=True,args=args) as (c,a):
   lease=self._lease(c,a,args,write=True)
   need(phase=='recovery' or a['service_role']==('receiver' if 'receive' in phase else 'publisher'),'ATTEMPT_SERVICE_ROLE_REQUIRED')
   c.execute('SELECT * FROM cerebro_bk11_operation_attempts WHERE assignment_ref=%s AND attempt_ref=%s FOR UPDATE',
    (a['assignment_ref'],args['attempt_ref']));current=_row(c)
   need(current is not None and current['operation_ref']==args['operation_ref'],'DURABLE_ORIGINAL_ATTEMPT_REQUIRED')
   if phase=='receive_item':
    items=args.get('obligations');need(type(items)is list and len(items)==1,'RECEIVE_ITEM_REQUIRED')
    self._obligation(c,a,items[0]);stored=current['obligations'];need(type(stored)is list and len(stored)<8,'RECEIVE_ATTEMPT_BOUND')
    need(current['outcome']=='IN_FLIGHT','RECEIVE_ATTEMPT_NOT_OPEN')
    if items[0] not in stored:stored=stored+items
    c.execute('UPDATE cerebro_bk11_operation_attempts SET phase=%s,obligations=%s::jsonb,observed_at=now() WHERE assignment_ref=%s AND attempt_ref=%s',
      (phase,json.dumps(stored),a['assignment_ref'],args['attempt_ref']))
    admitted=True;reconciled=False;target_ref=None
   elif phase.endswith('_begin'):
    c.execute('''SELECT attempt_ref FROM cerebro_bk11_operation_attempts WHERE assignment_ref=%s AND attempt_ref<>%s
     AND outcome IN ('IN_FLIGHT','RECONCILE_REQUIRED') LIMIT 1''',(a['assignment_ref'],args['attempt_ref']))
    blocked=_row(c)
    # Recovery custody can settle the original missing receiver duty, never publish.
    need(blocked is None or phase=='recovery_receive_begin','ORIGINAL_ATTEMPT_RECONCILIATION_REQUIRED')
    items=args.get('obligations',[]);need(type(items)is list and len(items)<=8,'ATTEMPT_OBLIGATIONS_BOUND')
    for item in items:
     need(type(item)is dict and set(item)=={'message_id','pointer_sha256'},'ATTEMPT_OBLIGATION_FIELDS');self._obligation(c,a,item)
    need(current['outcome'] in ('NO_EFFECT_ADMITTED','OBSERVED'),'ATTEMPT_ALREADY_IN_FLIGHT')
    c.execute('UPDATE cerebro_bk11_operation_attempts SET phase=%s,obligations=%s::jsonb,outcome=\'IN_FLIGHT\',observed_at=now() WHERE assignment_ref=%s AND attempt_ref=%s',
      (phase,json.dumps(items),a['assignment_ref'],args['attempt_ref']))
    admitted=True;reconciled=False;target_ref=None
   elif phase.endswith('_observed'):
    need(current['outcome']=='IN_FLIGHT','OBSERVATION_REQUIRES_OPEN_ATTEMPT')
    # State truth, never a local completed flag: unknown claimed/publication states stay fenced.
    items=current['obligations'];need(type(items)is list,'ATTEMPT_OBLIGATIONS_SHAPE')
    uncertain=False
    for item in items:
     owner=self._obligation(c,a,item)
     if 'receive' in phase:
      if self._disposition(c,a,item) is None:uncertain=True
     elif owner['state'] in ('SEND_CLAIMED','UNKNOWN_PENDING','CONFLICT_HOLD','RECONCILE_HOLD'):uncertain=True
    c.execute('UPDATE cerebro_bk11_operation_attempts SET phase=%s,outcome=%s,observed_at=now() WHERE assignment_ref=%s AND attempt_ref=%s',
      (phase,'RECONCILE_REQUIRED' if uncertain else 'OBSERVED',a['assignment_ref'],args['attempt_ref']))
    admitted=not uncertain;reconciled=False;target_ref=None
   else:
    target_ref=args.get('reconcile_attempt_ref');admitted=False;reconciled=False
    if target_ref is not None:
     reference(target_ref)
     c.execute('SELECT * FROM cerebro_bk11_operation_attempts WHERE assignment_ref=%s AND attempt_ref=%s FOR UPDATE',
      (a['assignment_ref'],target_ref));prior=_row(c)
     # All admitted effects register an attempt first. Protected absence proves
     # factory-entry failure before server effect admission, never broker absence.
     items=[] if prior is None else prior['obligations'];need(type(items)is list and len(items)<=8,'PRIOR_ATTEMPT_OBLIGATIONS_BOUND')
     # Terminal outcome, not phase OPEN: no new effect/delivery is inferred.
     # Only this already-reconciled empty publisher case is added.
     reconciled=prior is None or prior['outcome']=='NO_EFFECT_ADMITTED' or (a['service_role']=='receiver' and not items) or (a['service_role']=='publisher' and not items and prior['outcome']=='RECONCILED')
     if items:
      reconciled=all(self._disposition(c,a,item) is not None for item in items)
     if reconciled and prior is not None:
      c.execute('UPDATE cerebro_bk11_operation_attempts SET outcome=\'RECONCILED\',observed_at=now() WHERE assignment_ref=%s AND attempt_ref=%s',
       (a['assignment_ref'],target_ref))
  return {'schema':'cerebro-bk11-server-attempt-proof/v1','assignment_ref':a['assignment_ref'],
   'operation_ref':args['operation_ref'],'attempt_ref':args['attempt_ref'],'lease_ref':lease['lease_ref'],
   'owner_family':a['owner_family'],'fencing_epoch':lease['fencing_epoch'],'effect_admitted':admitted,
   'reconciled_original_attempt':reconciled,'reconciled_attempt_ref':target_ref}

class ContextBk11ServiceHost:
 def __init__(self,state_port):self._custody=ContextBk11ServiceCustody(state_port)
 def call(self,method,args,context):
  return getattr(self._custody,method)(args,context.identity,context.transport_session())
