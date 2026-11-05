"""UNISSUED exact0016 installer source; D1 owns the actual finite A entry.

No environment/credential/provider/role discovery or import-time effect. This
helper owns only connections it opens; the A entry retains witness/root closure.
"""
from __future__ import annotations
import copy,hashlib,inspect,json,re
from contextlib import contextmanager
from pathlib import Path

PREFIX_SHA='b956299d863187eea8dde6a8f1d74adc2a4c0a485f9017f496975a82cd84094a'
DDL_SHA='e78a3070c5ec924207bc1f04a30393deaa78cf1fd041f11f6e475c0e42761e73'
WITNESS_SHA='b400ebf739710341fe0d698e53358d6426c12d61fdde6b62b26103547365c3f0'
MIGRATOR_PINS={'f5559357ba18ffe332be99d17f5a7c253f1a63285f8779c23a0cc5c041e30d98',
               '3603ce40a0f86fb1384a5c7892cdbccf7fd75f9340f0861d5ad79edd1e9b3c2c'}
SQL_RAW_PINS = {'control_context_state_postgres.sql': '6312ab6d5fae58cfa52bfb1d13b1d361ebe73e3a4b37bb34f4875e480afc420d', 'control_context_state_postgres_0002_actor_claim_shadow.sql': '9b51fa1cb0b46618f5677670a7ad48411ceb50febb05503c8948df8844e02b0c', 'control_context_state_postgres_0003_human_t3_break_glass.sql': '5c83038f88d7946554ee6cc7a01cdb703d23da739cd0734bd6c50b5e51a34167', 'control_context_state_postgres_0004_principal_succession_permit.sql': '71c64f0caca37fa116a68e8c05bb9b08c63671e80b132c1f85f34177f5fdfde6', 'control_context_state_postgres_0005_pre_role_generation.sql': '6465fec59b0ae07e43408fbc3d3394efe1ca6d4537f1c57bb35d06f6517207dd', 'control_context_state_postgres_0006_pm_owner_ready.sql': 'a11ea2b46ef33f404e97a5319e93a399353a94da061f0e5cf6daeb13c09f6332', 'control_context_state_postgres_0007_x9_channel.sql': '139d6704defe4f6758fb1b285d5e5ba55fd4170697a103a0ab8eb391fdfd36b4', 'control_context_state_postgres_0008_assistant_overlay_decision.sql': 'e24301ac096da7bc30c917dfc84d525b7b5d96a6db12e52099c06d387a0933d1', 'control_context_state_postgres_0009_bk11_owner_backend_custody.sql': 'dd10acd0804bc4715d9243d9caf60522d7ba5262227623d9832dd44adf6ae6cc', 'control_context_state_postgres_0010_room_return_d1_outbox.sql': 'eb54b59323c42f9d36a2a1b4d0f5a39d498f07b5e41f1019e85897e1caeb0f69', 'control_context_state_postgres_0011_bk11_assignment_family.sql': 'b3acb5b89aeef2c8534110b5fdbd76bda12932f95615092f1857ce3c12299814', 'control_context_state_postgres_0012_worker_method_owner.sql': '410b461007c64846ec3edde3b2c7791770f8d89f0a5ac26090da3c32c57d4101', 'control_context_state_postgres_0013_room_return_actor_read.sql': 'e80444d02933ba37ecf10de2efada29de5150db08ec37506641619c5849f9967', 'control_context_state_postgres_0014_actor_admission_owner.sql': '2a460c9452b7306be08bfe9cd76beaeaeb68f41ca9e6baf37a6c1776f94e18b0', 'control_context_state_postgres_0015_pm_admission_owner.sql': 'a80ad58627047f382c016dfa60d3682753189c72ca4ad74bb20ae2556d182b42', 'control_context_state_postgres_0016_pm_2872_accepted_store.sql': 'e78a3070c5ec924207bc1f04a30393deaa78cf1fd041f11f6e475c0e42761e73'}
NEXT={'migration_id':'0016-pm-2872-accepted-store','schema_version':'1.15.0-pm-2872-accepted-store-bounded',
      'path':'control_context_state_postgres_0016_pm_2872_accepted_store.sql','checksum_sha256':DDL_SHA}
PRESERVE={'head_sha256':'3834d555cad791786d8945de340b5f9e68b12ead5dbbc7d6bd744ae918525e2f',
 'staged_decision_sha256':'f8f234d9ab2753ddceb1d8700f4967544c0bc9c3eba62ddf033e634107909f74',
 'native_sha256':'fa432627db4bd8e41a015fa9c5dbcc6932fd8da29114961c891f8be02bfb4514',
 'shadow_sha256':'e710c13ea22e839314ab0959e050e7c8f973e79c4bd501faf7ff7b337eaf9a26'}
TABLES=('cerebro_pm_2872_enrolments','cerebro_pm_2872_accepted')
FUNCTION='cerebro_pm_2872_guard_enrolment'

class InstallRefused(RuntimeError):pass
class InstallUnknown(RuntimeError):pass

def need(value,reason):
    if not value:raise InstallRefused(reason)

def sha(raw):return hashlib.sha256(raw).hexdigest()

def identifier(value):
    need(type(value)is str and 0<len(value.encode())<=63 and '\0' not in value,'INSTALL_ACTUAL_ROLE_NAME_INVALID')
    return '"'+value.replace('"','""')+'"'

def rows(cursor):
    values=cursor.fetchall()
    names=[getattr(d,'name',None) or d[0] for d in cursor.description]
    return [dict(v) if isinstance(v,dict) else dict(zip(names,v)) for v in values]

def query(cursor,sql,params=()):
    cursor.execute(sql,params);return rows(cursor)

@contextmanager
def own_cursor(connection):
    cursor=connection.cursor();primary=None
    try:yield cursor
    except BaseException as exc:primary=exc;raise
    finally:
        try:cursor.close()
        except BaseException:
            if primary is not None:primary.add_note('INSTALL_OWN_CURSOR_CLOSE_UNKNOWN_NO_RETRY')
            else:raise InstallUnknown('INSTALL_OWN_CURSOR_CLOSE_UNKNOWN_NO_RETRY')

def ledger(cursor):
    return query(cursor,'SELECT migration_id,schema_version,checksum_sha256 FROM public.cerebro_schema_migrations ORDER BY migration_id LIMIT 17')

def tx(connection,status=None):
    need(getattr(connection,'autocommit',None)is False,'INSTALL_AUTOCOMMIT_FALSE_REQUIRED')
    if status is not None:
        reader=getattr(connection,'get_transaction_status',None)
        actual=reader() if callable(reader) else getattr(getattr(connection,'info',None),'transaction_status',None)
        need(actual==status,'INSTALL_TRANSACTION_STATE_REQUIRED')

def material(prefix_path,manifest_path):
    prefix_path=Path(prefix_path).resolve(strict=True);manifest_path=Path(manifest_path).resolve(strict=True)
    raw=prefix_path.read_bytes();need(sha(raw)==PREFIX_SHA,'INSTALL_EXACT_PREFIX_ORIGINAL_REQUIRED')
    prefix=json.loads(raw);extended=json.loads(manifest_path.read_bytes())
    need(len(prefix['migrations'])==15 and [x['migration_id'][:4] for x in prefix['migrations']]==[f'{n:04}' for n in range(1,16)],'INSTALL_EXACT15_REQUIRED')
    expected=copy.deepcopy(prefix);expected['migrations'].append(NEXT)
    need(extended==expected,'INSTALL_ONLY_EXACT0016_APPEND_ALLOWED')
    for entry in extended['migrations']:
        path=(manifest_path.parent/entry['path']).resolve(strict=True)
        need(path.parent==manifest_path.parent and path.is_file(),'INSTALL_SQL_SOURCE_CHANGED_OR_ESCAPE')
        sql_raw=path.read_bytes()
        # Reuse the existing migrator's declared canonical Git-text checksum;
        # independently bind exact raw bytes, including the two retained CRLFs.
        need(sha(sql_raw)==SQL_RAW_PINS.get(entry['path']) and
             sha(sql_raw.replace(b'\r\n',b'\n'))==entry['checksum_sha256'],
             'INSTALL_SQL_SOURCE_CHANGED_OR_ESCAPE')
    fields=('migration_id','schema_version','checksum_sha256')
    project=lambda items:[{key:x[key] for key in fields} for x in items]
    ddl=(manifest_path.parent/NEXT['path']).read_text(encoding='utf-8')
    body=re.search(r'AS \$\$(.*?)\$\$;',ddl,re.S)
    need(body is not None,'INSTALL_EXACT_FUNCTION_BODY_REQUIRED')
    return project(prefix['migrations']),project(extended['migrations']),body.group(1)

def schema_snapshot(cursor,runtime_role,function_body):
    columns=query(cursor,"""SELECT c.relname,a.attnum,a.attname,format_type(a.atttypid,a.atttypmod) AS type,
        a.attnotnull,a.attidentity,a.attgenerated,a.attacl::text AS column_acl,pg_get_expr(d.adbin,d.adrelid) AS default_expr
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_attribute a ON a.attrelid=c.oid
        LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
        WHERE n.nspname='public' AND c.relname=ANY(%s) AND a.attnum>0 AND NOT a.attisdropped
        ORDER BY c.relname,a.attnum""",(list(TABLES),))
    expected={TABLES[0]:[('enrolment_id','text',True),('original','bytea',True),('active','boolean',True)],
              TABLES[1]:[('operation_id','text',True),('nonce','text',True),('enrolment_id','text',True),
                         ('token','bytea',True),('state','text',True),('result','bytea',False)]}
    for table,fields in expected.items():
        actual=[(r['attname'],r['type'],r['attnotnull']) for r in columns if r['relname']==table]
        need(actual==fields,'INSTALL_COLUMN_CONTRACT_MISMATCH')
    need(all(not r['attidentity'] and not r['attgenerated'] and r['default_expr']is None
         and r['column_acl']is None for r in columns),
         'INSTALL_UNEXPECTED_COLUMN_EFFECT')
    version=query(cursor,"SELECT current_setting('server_version_num')::integer AS server_version_num")[0]['server_version_num']
    need(type(version)is int and version>0,'INSTALL_PG_CATALOG_VERSION_REQUIRED')
    constraints=query(cursor,"""SELECT c.relname,k.contype,k.conkey,k.confkey,k.convalidated,k.condeferrable,
        k.condeferred,(to_jsonb(k)->>'conenforced')::boolean AS conenforced,
        k.confupdtype,k.confdeltype,k.confrelid::regclass::text AS foreign_table,
        pg_get_constraintdef(k.oid) AS definition
        FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relname=ANY(%s) ORDER BY c.relname,k.conname""",(list(TABLES),))
    expected_keys={TABLES[0]:sorted([('p',(1,)),('c',(2,))]),
        TABLES[1]:sorted([('p',(1,)),('u',(2,)),('f',(3,)),('c',(4,)),('c',(5,)),('c',(5,6))])}
    for table,keys in expected_keys.items():
        # PG18 represents each table NOT NULL as its own catalog constraint.
        # Require the exact expected columns, not a filter that ignores n rows.
        if version>=180000:
            keys=sorted(keys+[('n',(index,)) for index,field in enumerate(expected[table],1) if field[2]])
        need(sorted((r['contype'],tuple(r['conkey'])) for r in constraints if r['relname']==table)==keys,
             'INSTALL_CONSTRAINT_SHAPE_MISMATCH')
    need(all(r['convalidated'] is True and not r['condeferrable'] and not r['condeferred']
         and (r['conenforced'] is True if version>=180000 else r['conenforced'] is None)
         for r in constraints),'INSTALL_CONSTRAINT_VALIDATION_REQUIRED')
    fk=[r for r in constraints if r['contype']=='f']
    need(len(fk)==1 and fk[0]['foreign_table'] in (TABLES[0],'public.'+TABLES[0]) and fk[0]['confkey']==[1]
         and fk[0]['confupdtype']=='a' and fk[0]['confdeltype']=='a','INSTALL_FOREIGN_KEY_MISMATCH')
    functions=query(cursor,"""SELECT p.oid,p.prosrc,p.prosecdef,l.lanname,p.proacl::text AS acl,
        pg_get_userbyid(p.proowner) AS owner FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        JOIN pg_language l ON l.oid=p.prolang WHERE n.nspname='public' AND p.proname=%s AND p.pronargs=0""",(FUNCTION,))
    need(len(functions)==1 and functions[0]['prosrc']==function_body and not functions[0]['prosecdef']
         and functions[0]['lanname']=='plpgsql','INSTALL_REVOCATION_FUNCTION_MISMATCH')
    triggers=query(cursor,"""SELECT c.relname,t.tgname,t.tgtype,t.tgenabled,t.tgfoid,t.tgnargs
        FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relname=ANY(%s) AND NOT t.tgisinternal ORDER BY c.relname,t.tgname""",(list(TABLES),))
    need(len(triggers)==1 and triggers[0]=={'relname':TABLES[0],'tgname':'cerebro_pm_2872_enrolment_immutable',
        'tgtype':27,'tgenabled':'O','tgfoid':functions[0]['oid'],'tgnargs':0},'INSTALL_REVOCATION_TRIGGER_MISMATCH')
    owners=query(cursor,"""SELECT c.relname,pg_get_userbyid(c.relowner) AS owner,c.relacl::text AS acl,
        c.relrowsecurity,c.relforcerowsecurity,c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relname=ANY(%s) ORDER BY c.relname""",(list(TABLES),))
    migration_role=query(cursor,'SELECT current_user::text AS role')[0]['role']
    need(len(owners)==2 and all(r['owner']==migration_role and r['relkind']=='r'
         and not r['relrowsecurity'] and not r['relforcerowsecurity'] for r in owners)
         and functions[0]['owner']==migration_role,'INSTALL_ACTUAL_PRIVATE_CUSTODIAN_OWNERSHIP_REQUIRED')
    public_acl=query(cursor,"""SELECT c.relname,a.privilege_type FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace CROSS JOIN LATERAL
        aclexplode(coalesce(c.relacl,acldefault('r',c.relowner))) a
        WHERE n.nspname='public' AND c.relname=ANY(%s) AND a.grantee=0
        UNION ALL SELECT p.proname,a.privilege_type FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        CROSS JOIN LATERAL aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a
        WHERE n.nspname='public' AND p.proname=%s AND p.pronargs=0 AND a.grantee=0""",(list(TABLES),FUNCTION))
    need(not public_acl,'INSTALL_PUBLIC_RIGHTS_NOT_REVOKED')
    foreign_acl=query(cursor,"""SELECT c.relname,a.grantee,a.privilege_type FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace CROSS JOIN LATERAL
        aclexplode(coalesce(c.relacl,acldefault('r',c.relowner))) a
        WHERE n.nspname='public' AND c.relname=ANY(%s) AND a.grantee<>c.relowner
        UNION ALL SELECT p.proname,a.grantee,a.privilege_type FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        CROSS JOIN LATERAL aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a
        WHERE n.nspname='public' AND p.proname=%s AND p.pronargs=0 AND a.grantee<>p.proowner""",(list(TABLES),FUNCTION))
    need(not foreign_acl,'INSTALL_NEW_OBJECT_RIGHTS_MUST_BE_PRIVATE_CUSTODIAN_ONLY')
    runtime=[]
    for table in TABLES:
        for privilege in ('SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER'):
            granted=query(cursor,'SELECT has_table_privilege(%s,%s,%s) AS granted',(runtime_role,'public.'+table,privilege))[0]['granted']
            need(granted is False,'INSTALL_RUNTIME_INHERITED_OR_DIRECT_TABLE_RIGHTS')
            runtime.append((table,privilege,granted))
    need(query(cursor,'SELECT has_function_privilege(%s,%s,%s) AS granted',
        (runtime_role,'public.'+FUNCTION+'()','EXECUTE'))[0]['granted']is False,'INSTALL_RUNTIME_FUNCTION_RIGHTS')
    counts=[]
    for table in TABLES:
        n=query(cursor,'SELECT count(*) AS n FROM public.'+table)[0]['n']
        need(n==0,'INSTALL_NO_ENROLMENT_OR_ACCEPTANCE_ALLOWED');counts.append((table,n))
    roles=query(cursor,"""SELECT rolname,rolsuper,rolcreaterole,rolcreatedb,rolbypassrls
        FROM pg_roles WHERE rolname IN (%s,current_user) ORDER BY rolname""",(runtime_role,))
    return {'columns':columns,'constraints':constraints,'function':functions,'triggers':triggers,
            'owners':owners,'public_acl':public_acl,'foreign_acl':foreign_acl,
            'runtime_denials':runtime,'empty_rows':counts,'actual_role_scope':roles}

def install_store(*,witness,migration_module,prefix_manifest_path,manifest_path,authority_guard):
    """Consume only D1's protected real A guard. Guard signature is
    (connection=..., phase=..., preserve=<fixed four hashes>); it must independently
    enforce actual fresh finite A authority/state, return None on success and
    raise on refusal. A boolean payload is not accepted as authority. This
    function generates no grant and treats the guard as a trusted root dependency,
    never a boolean supplied by an operation payload. Root entry owns witness.
    """
    from pm_protected_owner_entry import BackendWitness
    need(type(witness)is BackendWitness,'INSTALL_REAL_WITNESS_REQUIRED')
    need(sha(Path(inspect.getfile(BackendWitness)).read_bytes())==WITNESS_SHA,'INSTALL_WITNESS_SOURCE_CHANGED')
    need(callable(authority_guard),'INSTALL_ROOT_A_AUTHORITY_GUARD_REQUIRED')
    need(sha(Path(migration_module.__file__).read_bytes()) in MIGRATOR_PINS,'INSTALL_MIGRATOR_SOURCE_CHANGED')
    need(inspect.getsourcefile(migration_module.apply_postgres_migrations)==migration_module.__file__,
         'INSTALL_EXISTING_MIGRATOR_ENTRY_REQUIRED')
    prefix,complete,function_body=material(prefix_manifest_path,manifest_path)
    runtime_role=witness.runtime_facts['role']
    owned=[];primary=None;write_pid=None;precommit=None;commit_attempted=False;ddl_attempted=False;cleanup_faults=[]

    def check(connection,phase):
        tx(connection);witness.verify(connection)
        result=authority_guard(connection=connection,phase=phase,preserve=copy.deepcopy(PRESERVE))
        need(result is None,'INSTALL_ROOT_GUARD_MUST_RAISE_OR_RETURN_NONE')
        tx(connection)

    class Connection:
        def __init__(self,raw):self.raw=raw
        def cursor(self):return Cursor(self.raw.cursor())
        def rollback(self):
            try:return self.raw.rollback()
            except BaseException:cleanup_faults.append('migration_rollback');raise
        def close(self):
            try:return self.raw.close()
            except BaseException:cleanup_faults.append('migration_connection_close');raise
        def commit(self):
            nonlocal commit_attempted
            need(precommit is not None,'INSTALL_BEFORE_COMMIT_GUARD_NOT_COMPLETED')
            check(self.raw,'COMMIT');tx(self.raw,2);commit_attempted=True
            self.raw.commit();tx(self.raw,0)

    class Cursor:
        def __init__(self,raw):self.raw=raw
        def __getattr__(self,name):return getattr(self.raw,name)
        def execute(self,sql,params=()):
            nonlocal ddl_attempted
            if type(sql)is str and sha(sql.encode())==DDL_SHA:
                need(not ddl_attempted,'INSTALL_SINGLE0016_ONLY');ddl_attempted=True
            return self.raw.execute(sql,params)
        def close(self):
            try:return self.raw.close()
            except BaseException:cleanup_faults.append('migration_cursor_close');raise

    def connection_factory():
        nonlocal write_pid
        need(not owned,'INSTALL_SINGLE_MIGRATION_CONNECTION_ONLY')
        connection=witness.connect();owned.append(connection);check(connection,'BEFORE_DDL')
        with own_cursor(connection) as cursor:
            cursor.execute("SET LOCAL search_path=public")
            cursor.execute('SELECT pg_advisory_xact_lock(48329716411208371)')
            tx(connection,2)
            need(ledger(cursor)==prefix,'INSTALL_EXACT15_EXISTING_ONLY_NO_REPLAY')
            collision=query(cursor,"SELECT to_regclass('public.cerebro_pm_2872_enrolments') AS a,to_regclass('public.cerebro_pm_2872_accepted') AS b,to_regprocedure('public.cerebro_pm_2872_guard_enrolment()') AS f")[0]
            need(all(v is None for v in collision.values()),'INSTALL_NEW_OBJECT_COLLISION_NO_REPLAY')
            role=query(cursor,'SELECT current_user::text AS role,pg_backend_pid() AS pid')[0]
            need(role['role']!=runtime_role,'INSTALL_PRIVATE_CUSTODIAN_MUST_DIFFER_RUNTIME');write_pid=role['pid']
        return Connection(connection)

    def before_commit(cursor):
        nonlocal precommit
        connection=owned[0];check(connection,'BEFORE_COMMIT')
        need(material(prefix_manifest_path,manifest_path)[:2]==(prefix,complete),'INSTALL_INPUT_CHANGED')
        need(ledger(cursor)==complete,'INSTALL_EXACT16_LEDGER_REQUIRED')
        # Preserve all unrelated ACLs. Only PUBLIC and the actual ordinary runtime
        # are denied on the NEW objects. No grant/create-role/credential operation.
        for role in ('PUBLIC',identifier(runtime_role)):
            cursor.execute('REVOKE ALL ON TABLE public.cerebro_pm_2872_enrolments, public.cerebro_pm_2872_accepted FROM '+role)
            cursor.execute('REVOKE ALL ON FUNCTION public.cerebro_pm_2872_guard_enrolment() FROM '+role)
        precommit=schema_snapshot(cursor,runtime_role,function_body)
        check(connection,'AFTER_ACL_BEFORE_COMMIT');tx(connection,2)

    try:
        result=migration_module.apply_postgres_migrations(connection_factory,manifest_path,
            expected_installed_prefix=prefix,before_commit=before_commit)
        need(ddl_attempted and not cleanup_faults,'INSTALL_DDL_OR_MIGRATOR_CLOSURE_NOT_PROVEN')
        need(result['applied']==[NEXT['migration_id']] and result['already_applied']==[x['migration_id'] for x in prefix],
             'INSTALL_ONLY0016_MAY_BE_APPLIED')
        independent=witness.connect();owned.append(independent);check(independent,'INDEPENDENT_READBACK')
        with own_cursor(independent) as cursor:
            cursor.execute('SET LOCAL search_path=public')
            read_pid=query(cursor,'SELECT pg_backend_pid() AS pid')[0]['pid']
            need(read_pid!=write_pid,'INSTALL_INDEPENDENT_BACKEND_REQUIRED')
            need(ledger(cursor)==complete,'INSTALL_INDEPENDENT_EXACT16_LEDGER_REQUIRED')
            need(schema_snapshot(cursor,runtime_role,function_body)==precommit,'INSTALL_SCHEMA_OR_PRIVILEGE_READBACK_CHANGED')
        check(independent,'AFTER_INDEPENDENT_READBACK')
        return {'state':'EXACT0016_COMMITTED_AND_INDEPENDENTLY_READ_BACK','migration':result,
            'source_ddl_sha256':DDL_SHA,'preserved':copy.deepcopy(PRESERVE),'schema_and_acl':precommit,
            'custodian_scope':'Actual creation/migration role retained its existing private privileges; may be privileged root. No claim of globally least-privileged role and no new role/credential granted.',
            'scope':'No enrolment/spend row, STAGE, TRANSFER, native mutation or source/deployment effect. Runtime role denied all new object rights.',
            'closure':'Opened connections close in finally; caller retains and must close root BackendWitness.'}
    except BaseException as exc:
        primary=exc
        if ddl_attempted or commit_attempted:
            error=InstallUnknown('INSTALL_COMMIT_OR_ACL_READBACK_UNKNOWN_NO_RETRY_RECONCILE')
            error.add_note('Exact original0016/ledger/schema/grants must be reconciled before another affected attempt; no DROP/reset/replay.')
            primary=error;raise error from exc
        exc.add_note('INSTALL_REFUSED_OR_ROLLED_BACK_NO_AUTOMATIC_RETRY')
        raise
    finally:
        failures=[]
        for connection in reversed(owned):
            for method in ('rollback','close'):
                try:
                    if not connection.closed:getattr(connection,method)()
                except BaseException:failures.append(method)
        failures=cleanup_faults+failures
        if failures:
            if primary is not None:primary.add_note('INSTALL_OWN_CONNECTION_CLOSURE_UNKNOWN_NO_RETRY:'+','.join(failures))
            else:raise InstallUnknown('INSTALL_OWN_CONNECTION_CLOSURE_UNKNOWN_NO_RETRY')
