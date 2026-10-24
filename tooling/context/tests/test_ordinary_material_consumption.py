"""Real ordinary SDK->tools->host->provider->accepted-reader synthetic route.

Only verified transport/OAuth, DB/store and official SDK object construction are
doubled. No server/SQL/broker/access/installation proof is claimed.
"""
import asyncio,copy,hashlib,importlib.util,json,pathlib,sys,types,unittest
from contextlib import contextmanager
from datetime import datetime,timezone,timedelta
from unittest.mock import patch
def deny_native_effect(event,args):
    if event.startswith(('socket.','subprocess.')) or event=='os.system':raise RuntimeError('SYNTHETIC_NATIVE_EFFECT_FORBIDDEN')
sys.addaudithook(deny_native_effect)
ROOT=pathlib.Path(sys.argv.pop(1)).resolve();sys.path.insert(0,str(ROOT/'mcp'))
SDK=ROOT/'mcp/control_context_mcp_sdk.py'
if '--canonical-sdk' in sys.argv:
    i=sys.argv.index('--canonical-sdk');sys.argv.pop(i);SDK=pathlib.Path(sys.argv.pop(i)).resolve()
s=importlib.util.spec_from_file_location('selected_sdk',SDK);sdk=importlib.util.module_from_spec(s);s.loader.exec_module(sdk)
import actor_admission_model as model
from actor_material_provider import ActorMaterialProvider,accepted_binding_for
from room_return_actor_reader import RoomReturnActorReadBinding,RoomReturnActorIdentity,_scope,ROW_BINDING_FIELDS
from room_return_d1_outbox import RoomReturnD1Outbox,RoomReturnOutboxHost,_canonical,_sha,_build_pointer
from control_context_tools import ControlContextMcpTools,VerifiedMcpIdentity,McpToolCallContext
from accepted_material_consumer import scan_accepted_room_returns,consumption_fingerprint,AcceptedMaterialScanRefused

def sha(value):return hashlib.sha256(value.encode()).hexdigest()
class Cursor:
    def __init__(self,world):self.world=world;self.queue=[]
    def fetchone(self):return self.queue.pop(0) if self.queue else None
    def execute(self,sql,args=()):
        w=self.world;self.queue=[];w.queries.append(sql)
        if sql.startswith('SELECT now()'):self.queue=[{'server_now':w.now}]
        elif 'FROM cerebro_owner_state_heads' in sql:
            self.queue=[dict(state_payload=copy.deepcopy(w.doc),state_schema=model.SCHEMA,state_fingerprint=model.digest(w.doc))]
        elif 'JOIN cerebro_bk11_backend_dispositions' in sql:
            scope=dict(zip(ROW_BINDING_FIELDS,args));row=w.rows.get(scope['assignment_ref'])
            message_matches='o.message_id=%s' not in sql or row is not None and row['message_id']==args[len(ROW_BINDING_FIELDS)]
            if row and message_matches and all(row[k]==v for k,v in scope.items()) and row['backend_status'] in ('ACCEPTED_PENDING_WORK','EXPIRED_NEEDS_OWNER_DISPOSITION') and row['accepted_receiver_assignment_ref']==args[-1]:self.queue=[copy.deepcopy(row)]
        elif 'set_config' not in sql:raise AssertionError('Unexpected fixture SQL boundary')
class State:
    def __init__(self,world):self.w=world
    def read_session(self,**kw):return dict(project_ref='fixture:project',session_binding_id='fixture:binding')
    def _load_session(self,*args,**kw):return dict(project_ref='fixture:project',project_revision=1,session_binding_id='fixture:binding')
    def _load_project(self,*args,**kw):return dict(project_status='ACTIVE',revision=1)
    @contextmanager
    def _transaction(self,**kw):
        assert kw['read_only'] and kw['repeatable_read'];yield Cursor(self.w)
class Store:
    def __init__(self,world):self.w=world
    @contextmanager
    def transaction(self,*args,**kw):assert kw['write'] is False;yield None
    def load(self,*args):return dict(payload=copy.deepcopy(self.w.doc),revision=1,fingerprint=model.digest(self.w.doc))
class Value:
    @classmethod
    def model_validate(cls,value,**kw):return value
class Server:
    def __init__(self,*args,**kw):self.call=kw['on_call_tool']
class Service(sdk.ControlContextRemoteMcpService):
    def __init__(self,world):self.w=world;self._config=types.SimpleNamespace(resource_documentation='https://example.invalid')
    def list_tools(self):return []
    def server_descriptor(self):return dict(name='ordinary-fixture',version='1',instructions='synthetic')
    def invoke(self,tool_name,args,**kw):
        self.w.calls.append(copy.deepcopy(args));return self.w.tools.dispatch(tool_name,args,self.w.context)
class World:
    def __init__(self):
        self.now=datetime.now(timezone.utc);self.doc=model.blank();self.rows={};self.calls=[];self.queries=[]
        self.carrier=dict(principal_ref='fixture:reader',consumer_ref='fixture:client',session_ref='local:fixture:reader')
        self.doc['business_owners']['Reader']=dict(carrier=copy.deepcopy(self.carrier),execution='ACTIVE',epoch=1,session_binding_id='fixture:binding',kind='AUTHENTICATED_BUSINESS_PARTY',access_expires_at=(self.now+timedelta(hours=2)).isoformat(),retired_carriers=[])
        self.route=dict(route_ref='fixture:route',transport_owner_ref='fixture:transport',receiver_ref='fixture:receiver',receiver_assignment_ref='fixture:receiver-assignment',binding_revision=1,pointer_ttl_seconds=60)
        self.state=State(self);host=types.SimpleNamespace(state=self.state,store=Store(self));self.provider=ActorMaterialProvider(host)
        self.tools=object.__new__(ControlContextMcpTools);self.tools._room_return_d1_outbox=None
        self.tools.bind_room_return_d1_outbox(RoomReturnOutboxHost(RoomReturnD1Outbox(state_port=self.state,binding=None),admission_provider=self.provider))
        self.set_context('fixture:reader');self.server=self.make_server()
    def make_server(self):
        fake=types.SimpleNamespace(Tool=Value,CallToolResult=Value)
        with patch.object(sdk,'_sdk_modules',return_value=(fake,Server,None,None,None,'fixture')):return sdk.create_official_mcp_server(Service(self))
    def set_context(self,session):
        ident=VerifiedMcpIdentity('fixture:tenant','fixture:workspace','fixture:reader',frozenset({'project_state:read','project_state:transition'}),True,consumer_ref='fixture:client')
        self.context=McpToolCallContext(ident,{'cerebro/session':session})
    def move(self):
        a=self.doc['business_owners']['Reader'];a['retired_carriers'].append(dict(carrier=copy.deepcopy(a['carrier']),epoch=a['epoch'],session_binding_id=a['session_binding_id']));a['epoch']+=1;a['carrier']['session_ref']='local:fixture:new';self.set_context('fixture:new')
    def add(self,n,status='ACCEPTED_PENDING_WORK',notification_expired=False):
        ref='material:'+str(n).zfill(4);taskref='fixture:task'+str(n);attempt='fixture:attempt'+str(n)
        task=dict(task_ref=taskref,attempt_ref=attempt,not_before=(self.now-timedelta(minutes=5)).isoformat(),expires_at=(self.now+timedelta(hours=1)).isoformat())
        key,_=model.task_entry(self.doc,taskref,attempt);self.doc['tasks'][key]=task
        material=json.dumps({'value':n},sort_keys=True);envelope=json.dumps(dict(task_ref=taskref,material_revision='rev1',original_ref='fixture:original',attempt_ref=attempt,artifact_ref='fixture:artifact'+str(n)),sort_keys=True)
        m=dict(material_ref=ref,task_ref=taskref,attempt_ref=attempt,sender_actor_ref='Sender',recipient_actor_ref='Reader',owner_actor_ref='Sender',route=self.route,material_revision='rev1',original_ref='fixture:original',expected_action='read',parent_sha256='a'*64,material_sha256=sha(material),envelope_sha256=sha(envelope),artifact_ref='fixture:artifact'+str(n),producer=dict(principal_ref='fixture:sender',consumer_ref='fixture:sender-client',session_ref='fixture:sender-session',session_binding_id='fixture:sender-binding',epoch=1))
        self.doc['materials'][ref]=m;who=dict(tenant_ref='fixture:tenant',workspace_ref='fixture:workspace',**self.carrier)
        producer=accepted_binding_for(m,self.doc,who,'fixture:project');reader=RoomReturnActorIdentity(**who,project_ref='fixture:project',session_binding_id='fixture:binding');binding=RoomReturnActorReadBinding(ref,producer,reader,self.route['receiver_assignment_ref'],attempt,m['artifact_ref'],task['not_before'],task['expires_at'])
        row={**_scope(binding),'material':material,'envelope':envelope,'material_bytes':len(material.encode())}
        reg=_sha(_canonical({k:row[v] for k,v in {'owner':'owner_ref','task':'task_ref','revision':'material_revision','recipient':'recipient_ref','original_ref':'source_original_ref','parent_sha256':'parent_sha256','material_sha256':'material_sha256','envelope_sha256':'envelope_sha256'}.items()}));event='rr:'+reg[:32];seq=n+1
        fields={**{k:v for k,v in row.items() if k!='source_original_ref'},'original_ref':row['source_original_ref']};event_fp=_sha(_canonical(fields))
        created=self.now-timedelta(days=1) if notification_expired else self.now
        pointer,message=_build_pointer(tenant_ref=who['tenant_ref'],workspace_ref=who['workspace_ref'],project_ref='fixture:project',owner_ref='Sender',owner_event_key=event,owner_revision=seq,event_fingerprint=event_fp,referent_id='rr:'+str(n),receiver_ref=self.route['receiver_ref'],created_at=created,ttl_seconds=60)
        row.update(registration_key=reg,owner_event_key=event,owner_seq=seq,event_fingerprint=event_fp,pointer=pointer,pointer_sha256=_sha(pointer),message_id=message,room_return_ref='rr:'+str(n),expires_at=created+timedelta(seconds=60),backend_status=status,accepted_pointer_sha256=_sha(pointer),accepted_event_id=event,accepted_owner_seq=seq,accepted_receiver_assignment_ref=self.route['receiver_assignment_ref'])
        self.rows[ref]=row;return ref
    def read(self,args,tool='read_accepted_room_return_v1'):
        # This SDK callback contains synchronous service.invoke and no awaits.
        # Drive that actual coroutine once without Windows asyncio IPC sockets.
        coroutine=self.server.call(types.SimpleNamespace(request=None),types.SimpleNamespace(name=tool,arguments=args,meta=None))
        try:
            coroutine.send(None)
            raise AssertionError('New asynchronous boundary needs explicit fixture coverage')
        except StopIteration as complete:return complete.value
        finally:coroutine.close()

class OrdinaryTests(unittest.TestCase):
    def test_ordinary_multistep_late_acceptance_repeat_and_new_carrier(self):
        w=World()
        for n in range(14):w.add(n,'PENDING' if n<4 else 'REJECTED' if n<8 else 'ACCEPTED_PENDING_WORK' if n<12 else 'PENDING')
        ledger={};used=[];first=scan_accepted_room_returns(w.read,ledger)
        self.assertTrue(first['complete']);self.assertEqual(first['pages'],2);self.assertEqual(len(first['rows']),4)
        self.assertNotIn('after',w.calls[0]);self.assertIn('after',w.calls[1]);self.assertEqual(ledger,{})
        def use(scan):
            for row in scan['rows']:
                used.append(json.loads(row['material'])['value']);ledger[row['business_dedupe_key']]=consumption_fingerprint(row)
        use(first);w.rows['material:0001']['backend_status']='ACCEPTED_PENDING_WORK';w.calls=[]
        second=scan_accepted_room_returns(w.read,ledger);self.assertEqual(len(second['rows']),1);self.assertNotIn('after',w.calls[0]);self.assertEqual(second['completed_rows_skipped'],4);use(second)
        w.move();self.assertEqual(scan_accepted_room_returns(w.read,ledger)['rows'],[])
        w.add(14);use(scan_accepted_room_returns(w.read,ledger));self.assertEqual(sorted(used),[1,8,9,10,11,14]);self.assertEqual(sum(used),53)
        self.assertTrue(all(q.startswith('SELECT') for q in w.queries))
    def test_empty_nonterminal_and_explicit_budget_are_not_empty_inbox(self):
        w=World()
        for n in range(10):w.add(n,'PENDING' if n<8 else 'ACCEPTED_PENDING_WORK')
        result=scan_accepted_room_returns(w.read,{},page_budget=1);self.assertFalse(result['complete']);self.assertEqual(result['rows'],[]);self.assertIsNotNone(result['next'])
        self.assertEqual(len(scan_accepted_room_returns(w.read,{})['rows']),2)
    def test_notification_expiry_is_readonly_not_admission_renewal(self):
        w=World();ref=w.add(1,notification_expired=True);result=scan_accepted_room_returns(w.read,{})
        self.assertTrue(result['rows'][0]['notification_expired']);self.assertFalse(result['rows'][0]['effect_eligible'])
        key=next(iter(w.doc['tasks']));w.doc['tasks'][key]['expires_at']=(w.now-timedelta(minutes=1)).isoformat()
        args=dict(op='read_exact',message_id=w.rows[ref]['message_id'],material_ref=ref);r=w.read(args)
        self.assertTrue(r['isError']);self.assertEqual(r['structuredContent']['error_code'],'ROOM_ACTOR_READ_PERMISSION_EXPIRED_OR_NOT_YET_VALID');self.assertFalse(r['structuredContent']['retry_allowed']);self.assertIn('owner_action',r['structuredContent']);self.assertEqual(len(r['structuredContent']['request_sha256']),64)
    def test_old_carrier_read_typed_and_write_refused_new_read_still_valid(self):
        w=World();w.add(1);w.move();self.assertEqual(len(scan_accepted_room_returns(w.read,{})['rows']),1);w.set_context('fixture:reader');before=copy.deepcopy(w.doc)
        r=w.read(dict(op='list_accepted'));self.assertTrue(r['isError']);self.assertEqual(r['structuredContent']['error_code'],'OLD_CARRIER_EXECUTION_FENCED');self.assertFalse(r['structuredContent']['empty_result'])
        write=w.read(dict(idempotency_key='fixture:key',material='benign',envelope='{}'),tool='register_room_return_v1');self.assertTrue(write['isError']);self.assertEqual(w.doc,before)
    def test_integrity_and_dedupe_conflict_never_skip_changed_body(self):
        w=World();w.add(1);row=scan_accepted_room_returns(w.read,{})['rows'][0]
        with self.assertRaisesRegex(AcceptedMaterialScanRefused,'BUSINESS_DEDUPE_CONTENT_CONFLICT'):scan_accepted_room_returns(w.read,{row['business_dedupe_key']:'f'*64})
        w.rows['material:0001']['material']='changed'
        with self.assertRaisesRegex(AcceptedMaterialScanRefused,'READ_PAGE_REFUSED'):scan_accepted_room_returns(w.read,{})
    def test_protocol_failure_and_cursor_loop_never_retry_or_echo_raw_error(self):
        calls=[]
        def bad(args):calls.append(args);return dict(rows=[],next='material:a')
        with self.assertRaisesRegex(AcceptedMaterialScanRefused,'SCAN_CURSOR_NOT_ADVANCING'):scan_accepted_room_returns(bad,{})
        self.assertEqual(len(calls),2)
        with self.assertRaisesRegex(AcceptedMaterialScanRefused,'READ_PAGE_REFUSED'):scan_accepted_room_returns(lambda a:dict(isError=True,content=[dict(text='SECRET_RAW')],structuredContent=None),{})
    def test_wrong_exact_message_and_other_recipient_are_not_delivery_proof(self):
        w=World();ref=w.add(1)
        result=w.read(dict(op='read_exact',message_id='d1:'+'0'*40,material_ref=ref))
        self.assertEqual(result['structuredContent']['rows'],[]);self.assertFalse(result.get('isError',False))
        w.doc['materials'][ref]['recipient_actor_ref']='Other'
        result=scan_accepted_room_returns(w.read,{})
        self.assertTrue(result['complete']);self.assertEqual(result['rows'],[]);self.assertFalse(result['work_consumed'])
    def test_material_window_owner_warning_survives_scan_and_repeated_global_count(self):
        w=World();w.add(0);key=next(iter(w.doc['tasks']))
        w.doc['tasks'][key]['expires_at']=(w.now-timedelta(minutes=1)).isoformat()
        page=w.read(dict(op='list_accepted'))['structuredContent']
        self.assertEqual(page['rows'],[]);self.assertIsNone(page['next'])
        self.assertEqual(page['out_of_window_materials'],1)
        self.assertTrue(page['out_of_window_owner_disposition_required'])
        scan=scan_accepted_room_returns(w.read,{})
        self.assertTrue(scan['complete']);self.assertEqual(scan['rows'],[])
        self.assertEqual(scan['out_of_window_materials'],1)
        self.assertTrue(scan['out_of_window_owner_disposition_required'])
        self.assertEqual(scan['owner_action'],'OWNER_MATERIAL_WINDOW_DISPOSITION_REQUIRED_NO_RENEW')
        self.assertEqual(len(scan['window_observations']),1)
        for n in range(1,17):w.add(n,'PENDING')
        scan=scan_accepted_room_returns(w.read,{})
        self.assertEqual(scan['pages'],2);self.assertEqual(scan['out_of_window_materials'],1)
        self.assertTrue(scan['out_of_window_owner_disposition_required'])
        self.assertEqual([x['out_of_window_materials'] for x in scan['window_observations']],[1,1])
        self.assertTrue(scan['window_metadata_complete']);self.assertFalse(scan['work_consumed'])
    def test_tool_error_contract_is_scoped_and_diagnostic_admission_unchanged(self):
        for reason in ('OLD_CARRIER_EXECUTION_FENCED','ROOM_ACTOR_READ_PERMISSION_EXPIRED_OR_NOT_YET_VALID'):
            self.assertIsNone(sdk._receive_error_result('unrelated_tool',RuntimeError(reason)))
            self.assertIsNone(sdk._receive_error_result('read_accepted_room_return_v1',RuntimeError(reason+' SECRET_RAW')))
        self.assertTrue(callable(sdk._log_admission_rejection))

if __name__=='__main__':unittest.main(verbosity=2)
