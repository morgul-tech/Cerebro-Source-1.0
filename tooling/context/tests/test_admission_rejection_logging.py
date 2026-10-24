"""Changed seam only. Run with -I -B and an explicit SDK-root argument.

No listener, network, real identity or database. SDK wire types/server are
inert doubles; service domain dispatch is tested separately on runtime fit.
"""
import asyncio,copy,hashlib,json,pathlib,sys,types,unittest
from unittest.mock import patch

SDK_ROOT=pathlib.Path(sys.argv.pop(1)).resolve()
sys.path.insert(0,str(SDK_ROOT/'mcp'))
import control_context_mcp_sdk as sdk
from control_context_tools import ControlContextToolError

def fixture():
    return dict(op='admit_material',project_ref='fixture:project',operation_ref='W-FIXTURE-001',
        expected_revision=3,expected_fingerprint='a'*64,material=dict(task_ref='fixture:task',
        attempt_ref='fixture:attempt',phase='RETURN',material='SENSITIVE_MATERIAL',envelope='PRIVATE_ENVELOPE',
        artifact=dict(repository='https://github.com/example/fixture',commit='b'*40,path='return.txt',bytes=18,sha256='c'*64)))

class Value:
    @classmethod
    def model_validate(cls,value,**kw):return value
class Server:
    def __init__(self,*args,**kw):self.on_call_tool=kw['on_call_tool']
class Service(sdk.ControlContextRemoteMcpService):
    def __init__(self):self._config=types.SimpleNamespace(resource_documentation='https://example.invalid');self.calls=0;self.error=None;self.value={'structuredContent':{'fixture':'OK'}}
    def list_tools(self):return []
    def server_descriptor(self):return dict(name='fixture',version='0',instructions='inert')
    def invoke(self,**kw):
        self.calls+=1
        if self.error is not None:raise self.error
        return copy.deepcopy(self.value)

class DiagnosticTests(unittest.TestCase):
    def record(self,args=None,error=None,unexpected=False):
        with patch.object(sdk.logger,'warning') as log:
            self.assertTrue(sdk._log_admission_rejection('manage_actor_admission_v1',fixture() if args is None else args,
                error if error is not None else ControlContextToolError('OWNER_EXPECTED_OLD_CONFLICT'),unexpected=unexpected))
        return json.loads(log.call_args.args[1])
    def test_allowlist_all_fixed_reasons(self):
        for reason,stage in sdk._ADMISSION_REJECTION_STAGES.items():
            with self.subTest(reason=reason):
                r=self.record(error=ControlContextToolError(reason));self.assertEqual((r['reason'],r['stage']),(reason,stage))
                self.assertEqual(r['caller_authentication'],'NOT_ATTESTED_BY_DIAGNOSTIC')
    def test_unknown_malformed_never_stringifies(self):
        class HostileError(Exception):
            def __str__(self):raise AssertionError('must not stringify')
        for error in (HostileError('TOKEN_SECRET'),ControlContextToolError(['TOKEN_SECRET']),ControlContextToolError('X','SECRET')):
            r=self.record(error=error);self.assertEqual(r['reason'],'UNCLASSIFIED_REJECTION')
            self.assertNotIn('SECRET',json.dumps(r))
        self.assertEqual(self.record(error=HostileError('SECRET'),unexpected=True)['reason'],'UNEXPECTED_EXCEPTION')
    def test_canonical_correlation_and_no_raw_payload(self):
        a=fixture();r=self.record(a)
        self.assertEqual(r['request_fingerprint'],hashlib.sha256(json.dumps(a,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest())
        self.assertEqual(r['operation_ref'],a['operation_ref'])
        for raw in ('SENSITIVE_MATERIAL','PRIVATE_ENVELOPE','fixture:project','fixture:task','return.txt'):
            self.assertNotIn(raw,json.dumps(r))
        self.assertLess(len(json.dumps(r)),640)
    def test_hostile_wrong_type_and_oversize_omit_correlation(self):
        variants=[]
        for value in ('W-SECRET-123','W-SESSION-123','W-SK-123','w-lower','W-X\nTOKEN',3,{},'W-'+'A'*130):
            a=fixture();a['operation_ref']=value;variants.append(a)
        for key,value in (('expected_revision',True),('expected_fingerprint','bad'),('project_ref',[])):
            a=fixture();a[key]=value;variants.append(a)
        a=fixture();a['private_token']='SECRET';variants.append(a)
        a=fixture();a['material']['material']='x'*16385;variants.append(a)
        a=fixture();a['material']['artifact']['bytes']=True;variants.append(a)
        for a in variants:
            with self.subTest(shape=type(a['operation_ref']).__name__):
                r=self.record(a);self.assertNotIn('operation_ref',r);self.assertNotIn('request_fingerprint',r)
    def test_logging_and_bad_encoding_fail_closed(self):
        with patch.object(sdk.logger,'warning',side_effect=RuntimeError('LOG_SECRET')):
            self.assertTrue(sdk._log_admission_rejection('manage_actor_admission_v1',fixture(),ValueError('SECRET')))
        a=fixture();a['material']['material']='\ud800'
        r=self.record(a)
        self.assertEqual(r['reason'],'OWNER_EXPECTED_OLD_CONFLICT')
        self.assertNotIn('operation_ref',r)
    def test_nonselected_never_logs(self):
        with patch.object(sdk.logger,'warning') as log:
            for name,a in (('other',fixture()),('manage_actor_admission_v1',{'op':'own_read'}),('manage_actor_admission_v1',[])):
                self.assertFalse(sdk._log_admission_rejection(name,a,ValueError('SECRET')))
            log.assert_not_called()
    def test_sdk_result_and_error_paths_unchanged(self):
        fake_types=types.SimpleNamespace(Tool=Value,CallToolResult=Value)
        service=Service()
        with patch.object(sdk,'_sdk_modules',return_value=(fake_types,Server,None,None,None,'fixture')):
            server=sdk.create_official_mcp_server(service)
        context=types.SimpleNamespace(request=None)
        params=types.SimpleNamespace(name='manage_actor_admission_v1',arguments=fixture(),meta=None)
        run=lambda:asyncio.run(server.on_call_tool(context,params))
        self.assertEqual(run()['structuredContent'],{'fixture':'OK'})
        service.error=ControlContextToolError('OWNER_EXPECTED_OLD_CONFLICT')
        with patch.object(sdk.logger,'warning') as log:
            self.assertEqual(run(),{'content':[{'type':'text','text':'The project-control request was rejected.'}],'isError':True})
            self.assertEqual(json.loads(log.call_args.args[1])['stage'],'OWNER_CAS')
        service.error=ValueError('TOKEN_SECRET')
        with patch.object(sdk.logger,'warning',side_effect=RuntimeError('LOG_SECRET')),patch.object(sdk.logger,'exception') as raw:
            r=run();self.assertTrue(r['isError']);self.assertNotIn('SECRET',json.dumps(r));raw.assert_not_called()
        params.name='unrelated'
        with patch.object(sdk.logger,'exception') as raw:
            self.assertTrue(run()['isError']);raw.assert_called_once()
        self.assertEqual(service.calls,4)
    def test_runtime_safe_receive_retained(self):
        if not hasattr(sdk,'_receive_error_result'):self.skipTest('canonical base has no safe-receive extension')
        r=sdk._receive_error_result('read_accepted_room_return_v1',ControlContextToolError('ACTOR_ACCESS_EXPIRED'))
        self.assertEqual(r['structuredContent']['error_code'],'ACTOR_ACCESS_EXPIRED')
        self.assertIsNone(sdk._receive_error_result('manage_actor_admission_v1',ControlContextToolError('ACTOR_ACCESS_EXPIRED')))

if __name__=='__main__':unittest.main(verbosity=2)
