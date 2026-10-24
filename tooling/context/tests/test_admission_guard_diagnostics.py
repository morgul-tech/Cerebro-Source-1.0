"""Real runtime-fit dispatch/host/model guards with external state/store doubles.

Synthetic verified flags are fixtures, not OAuth verification evidence.
No SQL, token, socket or provider. Run with explicit runtime root, -I -B.
"""
import asyncio,copy,json,pathlib,sys,types,unittest
from contextlib import contextmanager
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(sys.argv.pop(1)).resolve()/'mcp'))
BASELINE='--baseline' in sys.argv
if BASELINE:sys.argv.remove('--baseline')
import actor_admission_model as model
from actor_admission_host import ActorAdmissionHost
from control_context_tools import ControlContextMcpTools,VerifiedMcpIdentity,McpToolCallContext,ControlContextToolError
import control_context_mcp_sdk as sdk

class Store:
    def __init__(self,doc):self.before=dict(revision=3,fingerprint='a'*64,payload=doc);self.writes=0
    @contextmanager
    def transaction(self,*args,write=False,final_guard=None,**kw):
        yield None
        if write and final_guard:final_guard(None)
    def load(self,*args,**kw):return copy.deepcopy(self.before)
    def prior(self,*args,**kw):return None
    def save(self,c,who,project,before,after,*args):
        self.writes+=1;self.before=dict(revision=4,fingerprint=model.digest(after),payload=after)
        return {k:self.before[k] for k in ('revision','fingerprint')}
class State:
    def _load_project(self,*args,**kw):return {'project_status':'ACTIVE'}
    def read_session(self,**kw):return dict(project_ref='fixture:project',session_binding_id='fixture:binding')
class Value:
    @classmethod
    def model_validate(cls,value,**kw):return value
class Server:
    def __init__(self,*args,**kw):self.call=kw['on_call_tool']
class Service(sdk.ControlContextRemoteMcpService):
    def __init__(self,tools,context):self.tools=tools;self.context=context;self.calls=0;self._config=types.SimpleNamespace(resource_documentation='https://example.invalid')
    def list_tools(self):return []
    def server_descriptor(self):return dict(name='fixture',version='0',instructions='inert')
    def invoke(self,tool_name,args,**kw):self.calls+=1;return self.tools.dispatch(tool_name,args,self.context)

def setup_fixture():
    identity=VerifiedMcpIdentity('fixture:t','fixture:w','fixture:p',frozenset({'project_state:read','project_state:transition'}),True,consumer_ref='fixture:c')
    context=McpToolCallContext(identity,{'cerebro/session':'fixture:s'})
    carrier=dict(principal_ref='fixture:p',consumer_ref='fixture:c',session_ref='local:fixture:s')
    route=dict(route_ref='fixture:route',transport_owner_ref='fixture:transport',receiver_ref='fixture:receiver',receiver_assignment_ref='fixture:assignment',binding_revision=1,pointer_ttl_seconds=60)
    doc=model.blank();doc['actors']['W']=dict(carrier=carrier,execution='ACTIVE',access_expires_at='2099-01-01T00:00:00Z',epoch=2,session_binding_id='fixture:binding',kind='NATIVE_EXECUTOR',native_snapshot={'revision':1},allowed_peers=['X9'],allowed_routes=['fixture:route'],route_bindings={'fixture:route':route})
    phase=dict(sender_actor_ref='W',recipient_actor_ref='X9',route=route,material_revision='rev1',original_ref='fixture:original',expected_action='read',original_sha256='d'*64,artifact_repository='https://github.com/example/fixture',artifact_path_prefix='returns',content=dict(mode='SENDER_AUTHORED',material_sha256=None,envelope_sha256=None))
    task=dict(task_ref='fixture:task',attempt_ref='fixture:attempt',owner_actor_ref='X9',not_before='2020-01-01T00:00:00Z',expires_at='2099-01-01T00:00:00Z',phases={'RETURN':phase})
    key,_=model.task_entry(doc,task['task_ref'],task['attempt_ref']);doc['tasks'][key]=task
    text='fixture benign return';art=dict(repository=phase['artifact_repository'],commit='b'*40,path='returns/fixture.txt',bytes=len(text.encode()),sha256=__import__('hashlib').sha256(text.encode()).hexdigest())
    envelope=dict(schema='cerebro-business-material-envelope/v1',task_ref=task['task_ref'],attempt_ref=task['attempt_ref'],material_revision='rev1',original_ref=phase['original_ref'],expected_action='read',artifact_ref='example/fixture@'+'b'*40+':returns/fixture.txt',phase='RETURN',artifact=art)
    args=dict(op='admit_material',project_ref='fixture:project',operation_ref='W-FIXTURE-GUARD-001',expected_revision=3,expected_fingerprint='a'*64,material=dict(task_ref=task['task_ref'],attempt_ref=task['attempt_ref'],phase='RETURN',material=text,envelope=json.dumps(envelope),artifact=art))
    host=object.__new__(ActorAdmissionHost);host.state=State();host.store=Store(doc);host.issuer=None
    host._native=lambda *a:{'revision':1} # Only the external native read is doubled.
    tools=object.__new__(ControlContextMcpTools);tools._actor_admission=host
    return tools,context,host,args,key

class GuardTests(unittest.TestCase):
    def invoke(self,tools,context,args):
        service=Service(tools,context)
        fake=types.SimpleNamespace(Tool=Value,CallToolResult=Value)
        with patch.object(sdk,'_sdk_modules',return_value=(fake,Server,None,None,None,'fixture')):
            server=sdk.create_official_mcp_server(service)
        with patch.object(sdk.logger,'warning') as log:
            result=asyncio.run(server.call(types.SimpleNamespace(request=None),types.SimpleNamespace(name='manage_actor_admission_v1',arguments=args,meta=None)))
        self.assertEqual(service.calls,1)
        return result,json.loads(log.call_args.args[1]) if log.called else None
    def test_positive_real_dispatch_host_model_commit_readback(self):
        tools,context,host,args,key=setup_fixture();result,record=self.invoke(tools,context,args)
        self.assertFalse(result.get('isError',False));self.assertIsNone(record)
        self.assertEqual(result['structuredContent']['result'],'COMMITTED_INDEPENDENT_READBACK_PASS')
        self.assertEqual(host.store.writes,1);self.assertEqual(len(host.store.before['payload']['materials']),1)
    def test_negative_real_guards_remain_refused_without_write(self):
        cases=[('auth','UNCLASSIFIED_REJECTION','REQUEST_REJECTION'),('scope','ADMISSION_SCOPE_REQUIRED','REQUEST_SCOPE'),('cas','OWNER_EXPECTED_OLD_CONFLICT','OWNER_CAS'),('native','OWN_NATIVE_CHANGED_REQUALIFY_OWNER','NATIVE_BINDING'),('phase','SELECTED_TASK_PHASE_REQUIRED','TASK_PHASE'),('window','TASK_PERMISSION_EXPIRED_OR_NOT_YET_VALID','TASK_WINDOW'),('artifact','PORTABLE_ARTIFACT_BYTES_DIGEST_MISMATCH','ARTIFACT_BINDING'),('route','CURRENT_TASK_PEER_ROUTE_SCOPE_REVOKED','TASK_ROUTE')]
        for case,reason,stage in cases:
            with self.subTest(case=case):
                tools,context,host,args,key=setup_fixture();doc=host.store.before['payload']
                if case=='auth':context=McpToolCallContext(VerifiedMcpIdentity('t','w','p',frozenset(),False),{})
                if case=='scope':context=McpToolCallContext(VerifiedMcpIdentity('fixture:t','fixture:w','fixture:p',frozenset(),True,consumer_ref='fixture:c'),{'cerebro/session':'fixture:s'})
                if case=='cas':args['expected_revision']=2
                if case=='native':host._native=lambda *a:{'revision':2}
                if case=='phase':doc['tasks']={}
                if case=='window':doc['tasks'][key]['expires_at']='2021-01-01T00:00:00Z'
                if case=='artifact':args['material']['material']='different bytes'
                if case=='route':doc['actors']['W']['allowed_peers']=[]
                before=copy.deepcopy(host.store.before)
                result,record=self.invoke(tools,context,args)
                self.assertTrue(result['isError'])
                if BASELINE:self.assertIsNone(record)
                else:
                    self.assertEqual(record['reason'],reason);self.assertEqual(record['stage'],stage)
                self.assertEqual(host.store.writes,0);self.assertEqual(host.store.before,before)
                self.assertNotIn(reason,json.dumps(result))
                if not BASELINE:self.assertEqual(record['caller_authentication'],'NOT_ATTESTED_BY_DIAGNOSTIC')

if __name__=='__main__':unittest.main(verbosity=2)
