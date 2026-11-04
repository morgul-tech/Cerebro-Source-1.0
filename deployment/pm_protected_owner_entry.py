"""Protected PM install/STAGE intake, never a normal MCP or caller authority.

The existing custodian must place the exact P24-selected original and candidate
capture in a fresh root-private directory. A sealed installer binds its manifest
hash. No credentials, transport, role attach or CURRENT transfer is created here.
"""
from __future__ import annotations
import copy
import hashlib
import json
import os
import stat
import time
from pathlib import Path

from pm_admission_owner import need, digest, IDENTITY, validate_decision
from pm_bounded_qualification import GENERATION as BOUNDED_GENERATION, digest as bounded_digest
from pm_admission_owner_operator import apply_owner_plan
from control_context_registry import validate_pre_role_generation

TARGET = {'project':'ce122571-78e8-45a3-997a-061937e9aa38',
          'environment':'8e602a9f-d23a-40b2-a867-53b5397d27e3',
          'service':'882fdd63-140f-4ea1-a447-623f1d51c420'}


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def plans_commitment(plans):
    value=copy.deepcopy(plans)
    for plan in value:
        if plan.get('decision') is not None:
            plan['decision']['owner_original_sha256']='OWNER_ORIGINAL_SELF_HASH'
    return digest(value)


def private_file(directory, name, expected_sha256, *, owner_uid=0):
    """FD-relative bounded single-link intake; UID is fixed0 by production run.

    Tests may exercise this leaf with their own disposable UID. That does not
    qualify production root custody. No path or mode repair is performed.
    """
    need(directory.is_absolute() and name in {'intake-manifest.json','owner-original.json',
         'candidate-readback.json','owner-plans.json'}, 'PM_FIXED_PRIVATE_INPUT_NAME')
    parent=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        info=os.fstat(parent)
        need(info.st_uid==owner_uid and stat.S_IMODE(info.st_mode)==0o700,'PM_PRIVATE_DIRECTORY_CUSTODY')
        fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=parent)
        try:
            before=os.fstat(fd)
            need(stat.S_ISREG(before.st_mode) and before.st_uid==owner_uid and before.st_nlink==1
                 and stat.S_IMODE(before.st_mode)==0o600 and 0<before.st_size<=1048576,'PM_PRIVATE_FILE_CUSTODY')
            body=b''
            while len(body)<=1048576:
                chunk=os.read(fd,65536)
                if not chunk:break
                body+=chunk
            after=os.fstat(fd)
            need((before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)==
                 (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns)
                 and len(body)==before.st_size,'PM_PRIVATE_INPUT_CHANGED')
            need(hashlib.sha256(body).hexdigest()==expected_sha256,'PM_PRIVATE_INPUT_HASH')
            return body
        finally:os.close(fd)
    finally:os.close(parent)


def facts(connection):
    with connection.cursor() as c:
        c.execute("""SELECT current_user AS role,current_database() AS db,
            (SELECT oid FROM pg_database WHERE datname=current_database()) AS db_oid,
            pg_backend_pid() AS pid,extract(epoch from pg_postmaster_start_time())::text AS postmaster_started,
            inet_server_addr()::text AS server_addr,inet_server_port() AS server_port,
            current_setting('server_version_num') AS version,
            current_setting('application_name') AS application_name,
            (SELECT extract(epoch from backend_start)::text FROM pg_stat_activity WHERE pid=pg_backend_pid()) AS backend_started""")
        value=c.fetchone()
        return dict(value) if isinstance(value,dict) else dict(zip((d.name for d in c.description),value))


class BackendWitness:
    """Concrete R5 same-live-backend witness contract on EVERY real connection.

    Explicit existing runtime/migration factories own their own credentials.
    No default DSN, login, lookup or connection to any other process is allowed.
    """
    def __init__(self, *, runtime_factory, migration_factory, expected_backend_sha256, nonce):
        self.runtime=runtime_factory();self.migration_factory=migration_factory
        self.expected=expected_backend_sha256;self.nonce=nonce;self.owned=[];self.closed=False
        try:
            self.runtime_facts=facts(self.runtime)
            need(self.runtime_facts['application_name']==nonce,'PM_OWN_RUNTIME_NONCE_REQUIRED')
        except BaseException:
            self.runtime.close();self.closed=True;raise

    def verify(self, connection):
        runtime=facts(self.runtime); migration=facts(connection)
        fields=('db','db_oid','postmaster_started','server_addr','server_port','version')
        need(runtime==self.runtime_facts and runtime['application_name']==self.nonce,'PM_RUNTIME_WITNESS_CHANGED')
        need(runtime['pid']!=migration['pid'] and runtime['role']!=migration['role']
             and all(runtime[k]==migration[k] for k in fields),'PM_ACTUAL_BACKEND_OR_ROLE_MISMATCH')
        with connection.cursor() as c:
            c.execute("""SELECT datid,usename,application_name,extract(epoch from backend_start)::text AS backend_started
                FROM pg_stat_activity WHERE pid=%s AND datid=%s AND usename=%s AND application_name=%s
                AND extract(epoch from backend_start)::text=%s""",
                (runtime['pid'],runtime['db_oid'],runtime['role'],self.nonce,runtime['backend_started']))
            value=c.fetchone()
            if value is not None and not isinstance(value,dict):value=dict(zip((d.name for d in c.description),value))
        need(value=={'datid':runtime['db_oid'],'usename':runtime['role'],'application_name':self.nonce,
                     'backend_started':runtime['backend_started']},'PM_EXACT_OWN_WITNESS_NOT_IN_SAME_CLUSTER')
        need(digest({k:runtime[k] for k in fields})==self.expected,'PM_PINNED_BACKEND_CHANGED')
        return self.expected

    def connect(self):
        need(not self.closed,'PM_BACKEND_WITNESS_CLOSED')
        connection=self.migration_factory();self.owned.append(connection)
        try:self.verify(connection);connection.rollback();return connection
        except BaseException:
            try:connection.rollback()
            finally:connection.close()
            raise

    def close(self):
        failures=[]
        for connection in [*reversed(self.owned),self.runtime]:
            for method in ('rollback','close'):
                try:
                    if not connection.closed:getattr(connection,method)()
                except BaseException:failures.append(method)
        self.closed=not failures
        need(not failures,'PM_OWN_BACKEND_CLOSURE_UNKNOWN')


class ProtectedOwnerAuthority:
    """Exact accepted root-custody original + actual caller + live backend.

    This class verifies an existing Principal disposition; it does not sign or
    invent one. Non-exposed provider metadata is explicit owner-accepted scope,
    never synthesized attestation or cryptographic unique-chat proof.
    """
    def __init__(self, *, owner_original_bytes, caller_original_bytes, plans, witness):
        need(type(witness) is BackendWitness,'PM_CONCRETE_BACKEND_WITNESS_REQUIRED')
        self.original=owner_original_bytes; self.owner=json.loads(owner_original_bytes)
        self.caller=caller_original_bytes;self.plans=copy.deepcopy(plans);self.witness=witness
        fields={'schema','issuer','operation_id','target','candidate_chat','generation_ref',
            'candidate_readback_sha256','accepted_metadata_ceiling','backend_sha256',
            'preserve_outgoing_owner','owner_operator_sha256','plans_sha256','not_before','expires_at'}
        if type(self.owner) is dict and self.owner.get('generation_ref') == BOUNDED_GENERATION:
            fields = fields | {'bounded_qualification_sha256'}
        need(type(self.owner)is dict and set(self.owner)==fields,'PM_PRINCIPAL_ORIGINAL_FIELDS')
        o=self.owner
        need(o['schema']=='cerebro-pm-install-stage-owner-disposition/v1' and o['issuer']=='P24'
             and o['target']==TARGET and o['preserve_outgoing_owner']=='PROJECT_MANAGER_1C706AEB',
             'PM_PRINCIPAL_EXACT_INSTALL_STAGE_SCOPE')
        need(hashlib.sha256(caller_original_bytes).hexdigest()==o['candidate_readback_sha256']
             and plans_commitment(plans)==o['plans_sha256'] and o['backend_sha256']==witness.expected,'PM_OWNER_INTAKE_PINS')
        native=json.loads(caller_original_bytes)
        need(type(native)is dict and native.get('isError') is False and type(native.get('structuredContent'))is dict,
             'PM_CANDIDATE_NATIVE_READ_WRAPPER')
        value=native['structuredContent']
        need(set(value)=={'pre_role_generation','authenticated_binding','repository_permission_required'}
             and value['repository_permission_required']is False,'PM_CANDIDATE_READ_EXACT_FIELDS')
        need('_meta' not in native and o['accepted_metadata_ceiling']=='NOT_EXPOSED',
             'PM_METADATA_CEILING_MUST_BE_EXPLICIT_NO_SYNTHESIS')
        self.birth=value['pre_role_generation'];validate_pre_role_generation(self.birth)
        self.binding=value['authenticated_binding']
        need(type(self.binding)is dict and set(self.binding)==set(IDENTITY)
             and all(type(self.binding[k])is str and self.binding[k] for k in IDENTITY),'PM_ACTUAL_CALLER_EXACT_BINDING')
        need(self.birth['generation_ref']==o['generation_ref'] and self.birth['lifecycle']=='READY_UNBOUND'
             and self.birth['revision']==2,'PM_SELECTED_EXISTING_BIRTH_REQUIRED')
        need(type(plans)is list and [p['action'] for p in plans] in (['STAGE'],['BOOTSTRAP','STAGE']),
             'PM_INSTALLER_NO_TRANSFER_OR_NATIVE_EFFECT')
        decision=validate_decision(plans[-1]['decision'])
        if decision['generation_ref'] == BOUNDED_GENERATION:
            need(o['bounded_qualification_sha256'] == bounded_digest(decision['bounded_qualification']),
                 'PM_PRINCIPAL_BOUND_QUALIFICATION_REQUIRED')
        need(all(decision[k]==self.binding[k] for k in IDENTITY)
             and all(decision[k]==self.birth[k] for k in ('tenant_ref','workspace_ref','generation_ref','source_revision'))
             and decision['candidate_chat']==o['candidate_chat']
             and decision['pre_role_fingerprint']==self.birth['fingerprint']
             and decision['ready_unbound_receipt_fingerprint']==self.birth['ready_unbound_receipt']['receipt_fingerprint']
             and decision['method_fingerprint']==self.birth['civilization_method_attestation']['method_fingerprint']
             and decision['predecessor_owner_ref']==o['preserve_outgoing_owner'], 'PM_PRINCIPAL_CALLER_BIRTH_MAPPING_MISMATCH')
        # Avoid a circular raw-owner hash: owner ORIGINAL pins immutable plan
        # content after replacing its own hash by an explicit reserved marker.
        # Final sealing supplies this original's hash in the operator plans.
        need(decision['owner_original_sha256']==hashlib.sha256(owner_original_bytes).hexdigest(),
             'PM_OWNER_ORIGINAL_DECISION_HASH')

    def verify(self, *, connection, release, plan, owner_original_bytes):
        o=self.owner
        need(owner_original_bytes==self.original and plan in self.plans,'PM_EXACT_PROTECTED_OWNER_PLAN')
        need(release['operation_id']==o['operation_id']+':'+plan['action']
             and release['operation']=='PM_OWNER_'+plan['action'] and release['issuer']==o['issuer']
             and release['entry_sha256']==o['owner_operator_sha256']
             and release['not_before']==o['not_before'] and release['expires_at']==o['expires_at'],
             'PM_EXACT_OWNER_RELEASE_MAPPING')
        need(0<o['expires_at']-o['not_before']<=1200,'PM_OWNER_FINITE_WINDOW')
        need(o['not_before']<=time.time()<o['expires_at'],'PM_OWNER_FINITE_WINDOW_EXPIRED')
        self.witness.verify(connection)
        return True


def load_intake(directory, expected_manifest_sha256, witness):
    # Production entry fixes root custody: no runtime caller can choose UID.
    m=json.loads(private_file(directory,'intake-manifest.json',expected_manifest_sha256))
    need(set(m)=={'owner_original_sha256','caller_original_sha256','plans_file_sha256'},'PM_INTAKE_MANIFEST_FIELDS')
    original=private_file(directory,'owner-original.json',m['owner_original_sha256'])
    caller=private_file(directory,'candidate-readback.json',m['caller_original_sha256'])
    plans=json.loads(private_file(directory,'owner-plans.json',m['plans_file_sha256']))
    return ProtectedOwnerAuthority(owner_original_bytes=original,caller_original_bytes=caller,plans=plans,witness=witness)


def run(directory, expected_manifest_sha256, witness):
    """Finite private publication entry; never TRANSFER, retry or candidate call."""
    need(os.geteuid()==0 and directory.parent==Path('/run') and directory.name.startswith('cerebro-pm-introduction-'),
         'PM_ACTUAL_ROOT_PRIVATE_ENTRY_REQUIRED')
    authority=load_intake(directory,expected_manifest_sha256,witness)
    attempt=directory/'attempt-started.json'
    fd=os.open(attempt,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb')as f:f.write(canonical({'result':'STARTED_OUTCOME_UNKNOWN_NO_RETRY'}));f.flush();os.fsync(f.fileno())
    receipts=[]
    for plan in authority.plans:
        o=authority.owner
        release={'issuer':o['issuer'],'operation':'PM_OWNER_'+plan['action'],
            'operation_id':o['operation_id']+':'+plan['action'],'input_sha256':digest(plan),
            'entry_sha256':o['owner_operator_sha256'],'owner_original_sha256':hashlib.sha256(authority.original).hexdigest(),
            'not_before':o['not_before'],'expires_at':o['expires_at']}
        result=apply_owner_plan(checked_connection_factory=witness.connect,plan=plan,release=release,
            owner_original_bytes=authority.original,verify_effect_authority=authority.verify)
        # Each original is fsynced before the next suboperation. Prefix survives.
        path=directory/(plan['action']+'.receipt.json')
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb')as f:f.write(canonical(result));f.flush();os.fsync(f.fileno())
        receipts.append(result)
    return {'result':'INSTALL_STAGE_OWNER_READBACK_PASS','receipts':receipts,
            'current_changed':False,'candidate_invoked':False,'retry_allowed':False}
