"""Changed-risk unit checks; OAuth verifier and DB cursor are explicit SYNTHETIC doubles.
No PostgreSQL integration/live custody/production concurrency claim.
"""
import sys,json,unittest
from pathlib import Path
from dataclasses import replace
C=Path(__file__).resolve().parents[1]; ROOT=C.parents[1]
sys.path[:0]=[str(C/'src'),str(C),str(ROOT/'mcp')]
from control_context_remote_service import OAuthBearerAuthenticator,RemoteMcpServiceConfig,VerifiedBearerToken
from providers.x9_postgres_custody import PostgresX9SessionAPI,PostgresPMProducerChannel,CustodyUnavailable,bind_existing_pm_x9
from providers.x9_session_channel import X9_PRINCIPAL,X9_SESSION_REF,REQUIRED_SCOPES
from adapters.x9_channel_ingress import PointerRecord,X9Disposition,SCHEMA,DISPOSITION_SCHEMA,MATERIAL
PM='CURRENT_PM_PROJECT_MANAGER_C1A05B39';NOW=2000000000

class Verifier:
 def __init__(self,principal,scopes='project_state:read project_state:transition'):
  self.principal=principal;self.scopes=scopes;self.calls=0;self.exp=NOW+100
 def verify(self,token):
  self.calls+=1
  return VerifiedBearerToken({'iss':'https://auth.cerebro.invalid','aud':'https://mcp.cerebro.invalid','sub':self.principal,'cerebro_tenant':'tenant:t','cerebro_workspace':'workspace:w','iat':NOW-10,'exp':self.exp,'scope':self.scopes},True)
class DB:
 def __init__(self):self.records={};self.connections=0;self.statements=[];self.current=True;self.fail_commit=False
 def connect(self):self.connections+=1;return Conn(self)
class Conn:
 autocommit=False
 def __init__(self,db):self.db=db;self.pending={}
 def cursor(self):return Cursor(self)
 def commit(self):
  if self.db.fail_commit:raise RuntimeError('synthetic commit loss')
  self.db.records.update(self.pending)
 def rollback(self):self.pending={}
 def close(self):pass
class Cursor:
 def __init__(self,c):self.c=c;self.answer=None
 def execute(self,sql,args=()):
  self.c.db.statements.append((sql,args));self.answer=None
  if 'FROM cerebro_control_session_bindings s' in sql:
   self.answer={'session_binding_id':'existing:bind','session_revision':2,'session_fingerprint':'d'*64} if self.c.db.current else None
  elif sql.startswith('INSERT INTO cerebro_x9_channel_records'):
   t,w,ch,kind,event,writer,consumer,session,sha,body=args;key=(kind,event)
   if key not in self.c.db.records and key not in self.c.pending:
    self.c.pending[key]={'record_payload':json.loads(body),'content_sha256':sha,'writer_principal':writer,'writer_session':session,'provider_revision':len(self.c.db.records)+1}
  elif sql.startswith('SELECT record_payload'):
   key=(args[3],args[4]);self.answer=self.c.pending.get(key,self.c.db.records.get(key))
 def fetchone(self):return self.answer
 def close(self):pass

class ChangedCustodyRisk(unittest.TestCase):
 def setUp(self):self.db=DB()
 def api(self,principal=X9_PRINCIPAL,scopes='project_state:read project_state:transition',role='receiver',enabled=True):
  v=Verifier(principal,scopes);auth=OAuthBearerAuthenticator(config=RemoteMcpServiceConfig('https://mcp.cerebro.invalid',('https://auth.cerebro.invalid',),'https://mcp.cerebro.invalid/docs',clock_skew_seconds=0),token_verifier=v,clock=lambda:NOW)
  return PostgresX9SessionAPI(authenticator=auth,header_provider=lambda:{'Authorization':'Bearer SYNTHETIC_ONLY'},connection_factory=self.db.connect,role=role,producer_session_ref='codex:registered-pm' if role=='producer' else None,enabled=enabled),v
 def pointer(self):return PointerRecord(SCHEMA,'closure:1','event:1','attempt:1','owner:pm','work-packet','rev:1','a'*64,'claim:1','packet:1','b'*64,'queue:1',PM,X9_SESSION_REF,'cut:1','2026-10-06T15:00:00Z',('way:pm',))
 def disp(self,p):return X9Disposition(DISPOSITION_SCHEMA,p.event_id,p.attempt_id,p.content_sha256,'rev:1','c'*64,MATERIAL,'NEXT_CURRENT_PM_WORK')
 def test_default_off_and_wrong_authenticated_principal_never_open_db(self):
  off,_=self.api(enabled=False)
  with self.assertRaises(CustodyUnavailable):off.identity()
  wrong,_=self.api(principal=PM)
  with self.assertRaises(CustodyUnavailable):wrong.identity()
  self.assertEqual(self.db.connections,0)
 def test_scopes_derive_real_auth_result_not_caller_booleans(self):
  api,v=self.api();self.assertEqual(api.identity().scopes,REQUIRED_SCOPES)
  read,v=self.api(scopes='project_state:read');self.assertNotIn('disposition:append',read.identity().scopes)
  p=self.pointer();self.assertEqual(read.append_disposition_once(p.event_id,self.disp(p).content_sha256,self.disp(p)),'NOT_SENT')
  v.exp=NOW-1
  with self.assertRaises(CustodyUnavailable):read.identity()
 def test_stale_existing_session_fail_closed(self):
  api,_=self.api();self.db.current=False
  with self.assertRaises(CustodyUnavailable):api.identity()
  self.assertFalse(self.db.records)
 def test_original_atomic_statement_duplicate_collision_and_independent_readback(self):
  producer,_=self.api(principal=PM,role='producer');reader,_=self.api();p=self.pointer()
  self.assertEqual(producer.append_pointer_once(p),'ACCEPTED')
  original=reader.read_pointer_by_event_id(p.event_id)
  self.assertEqual(producer.append_pointer_once(p),'ACCEPTED')
  self.assertEqual(producer.append_pointer_once(replace(p,attempt_id='different')),'NOT_SENT')
  self.assertEqual(reader.read_pointer_by_event_id(p.event_id),original)
  self.assertEqual(len(self.db.records),1)
  self.assertTrue(any('ON CONFLICT (tenant_ref,workspace_ref,channel,record_kind,event_id) DO NOTHING' in s for s,_ in self.db.statements))
  self.assertGreaterEqual(self.db.connections,5) # independent connections, no read from append ACK
 def test_sameattempt_disposition_once_preserves_original_writer_revision(self):
  producer,_=self.api(principal=PM,role='producer');api,_=self.api();p=self.pointer();producer.append_pointer_once(p);d=self.disp(p)
  self.assertEqual(api.append_disposition_once(d.event_id,d.content_sha256,d),'ACCEPTED')
  r=api.read_disposition_by_event_id(d.event_id)
  self.assertEqual((r.record,r.producer_principal),(d,X9_PRINCIPAL))
  self.assertEqual(api.append_disposition_once(d.event_id,d.content_sha256,d),'ACCEPTED')
  altered=replace(d,reason='OTHER')
  self.assertEqual(api.append_disposition_once(altered.event_id,altered.content_sha256,altered),'NOT_SENT')
  self.assertEqual(api.read_disposition_by_event_id(d.event_id),r)
 def test_crosswired_role_attempt_and_hash_rejected(self):
  api,_=self.api();producer,_=self.api(principal=PM,role='producer');p=self.pointer();d=self.disp(p)
  self.assertEqual(api.append_pointer_once(p),'NOT_SENT')
  self.assertEqual(producer.append_disposition_once(d.event_id,d.content_sha256,d),'NOT_SENT')
  producer.append_pointer_once(p)
  wrong=replace(d,attempt_id='other')
  self.assertEqual(api.append_disposition_once(wrong.event_id,wrong.content_sha256,wrong),'NOT_SENT')
  self.assertEqual(api.append_disposition_once(d.event_id,'0'*64,d),'NOT_SENT')
  self.assertEqual(len(self.db.records),1)
 def test_commit_unknown_no_retry_and_no_ack_as_readback(self):
  producer,v=self.api(principal=PM,role='producer');self.db.fail_commit=True
  self.assertEqual(producer.append_pointer_once(self.pointer()),'UNKNOWN_SEND')
  self.assertEqual(v.calls,1);self.assertEqual(self.db.records,{})
 def test_original_read_tamper_rejected_and_registration_default_off(self):
  producer,_=self.api(principal=PM,role='producer');api,_=self.api();producer.append_pointer_once(self.pointer())
  self.db.records[('POINTER','event:1')]['content_sha256']='0'*64
  with self.assertRaises(CustodyUnavailable):api.read_pointer_by_event_id('event:1')
  with self.assertRaises(CustodyUnavailable):bind_existing_pm_x9(None,None,receiver_api=api,producer_api=producer)
