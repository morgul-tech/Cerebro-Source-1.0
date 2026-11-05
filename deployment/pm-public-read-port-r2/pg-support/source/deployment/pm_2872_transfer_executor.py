"""UNACTIVATED dedicated bounded2872 TRANSFER, never generic warm authority.

Only the protected Context custodian commissions the P24 PUBLIC verifier and
global acceptance store. No signing keys, SSH, env/DSN lookup, native admission,
STAGE/BOOTSTRAP, assignment, retirement or candidate impersonation here.
"""
from __future__ import annotations
import contextlib
import copy
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import time
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


class TransferRefused(RuntimeError):
    pass


def need(ok, reason):
    if not ok:
        raise TransferRefused(reason)


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def decode(raw):
    need(type(raw) is bytes and 0<len(raw)<=1048576,'TRANSFER_BOUNDED_RAW_BYTES')
    def pairs(values):
        result={}
        for k,v in values:
            need(k not in result,'TRANSFER_DUPLICATE_JSON_FIELD');result[k]=v
        return result
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda x:(_ for _ in ()).throw(TransferRefused('TRANSFER_NONFINITE_JSON')))


STAGED_DECISION='f8f234d9ab2753ddceb1d8700f4967544c0bc9c3eba62ddf033e634107909f74'
BEFORE_HEAD='3834d555cad791786d8945de340b5f9e68b12ead5dbbc7d6bd744ae918525e2f'
NATIVE_FINGERPRINT='fa432627db4bd8e41a015fa9c5dbcc6932fd8da29114961c891f8be02bfb4514'
SHADOW_FINGERPRINT='e710c13ea22e839314ab0959e050e7c8f973e79c4bd501faf7ff7b337eaf9a26'
ACTIVATION_CANONICAL='b23fede82f8d4665db01c5a1e6bed948ca311121e331ce2dc2c759f39c3b5b6b'
AFTER_HEAD='783ec1a02110d92aca406cc34e0cbaa2040a6ff172e9610cfd08d5ba7d1cc0f9'
SOURCE_PINS={
 'pm_admission_owner':'9ee6b63e025d3e863b061f8cc727f2e7b29a788d508d532b298c9345926fa895',
 'pm_bounded_qualification':'5f5a470a3e83edf8ecf576d8fa1381a3a5f9d17fc5dfa4ef696e772c59c743fb',
 'pm_admission_owner_operator':'b0614e3b72fd7c7bb70cb0ba9d03b4f3063b54a17de02fb8c25299d33383b58c',
 'control_context_registry':'4ca14de7d182e0ddc38e64c2cc0065038d9e4ff63fa63af7dd12182bb8025fd7',
 'pm_protected_owner_entry':'b400ebf739710341fe0d698e53358d6426c12d61fdde6b62b26103547365c3f0',
}
TOKEN_FIELDS={'schema','issuer','operation','operation_id','nonce','raw_owner_sha256',
 'raw_plan_sha256','staged_decision_sha256','before_head_sha256','after_head_sha256',
 'native_binding_sha256','final_refs_sha256','target','backend_sha256','source_pins',
 'executor_sha256','not_before','expires_at','evidence_mode'}


def verify_final_originals(receipts, final_evidence):
    from pm_bounded_qualification import FINAL_REFS, refs
    refs(receipts, FINAL_REFS)
    need(type(final_evidence)is dict and set(final_evidence)==FINAL_REFS,'TRANSFER_SEVEN_GENUINE_FINAL_REFS_REQUIRED')
    for name,raw in final_evidence.items():
        need(type(raw)is bytes and 0<len(raw)<=1048576 and sha(raw)==receipts[name]['sha256'],
             'TRANSFER_FINAL_ORIGINAL_MISSING_OR_CHANGED:'+name)


def private_path(path,directory=False):
    need(os.name=='posix' and os.geteuid()==0,'TRANSFER_ROOT_CUSTODIAN_REQUIRED')
    path=Path(path);need(path.is_absolute(),'TRANSFER_ABSOLUTE_CUSTODY_PATH')
    for part in [*reversed(path.parents),path]:
        info=part.lstat()
        need(info.st_uid==0 and not stat.S_ISLNK(info.st_mode) and not info.st_mode&0o022,'TRANSFER_PROTECTED_PATH')
    info=path.lstat()
    need((stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink==1)
         and not info.st_mode&0o077,'TRANSFER_PRIVATE_CUSTODY')


class P24BoundedTransferPort:
    """Explicit protected commissioning/integration, not a worker callable.
    Commissioned config contains public_key (hex), target and backend_sha256.
    No runtime record/root is commissioned by the source or local test.
    """
    def __init__(self,*args,**kwargs):
        raise TransferRefused('TRANSFER_PROTECTED_FACTORY_REQUIRED')

    @classmethod
    def _open(cls,directory,config,clock,test_only):
        need(type(config) is dict and set(config)=={'schema','issuer','public_key','target','backend_sha256'}
             and config['schema']=='cerebro-p24-bounded-transfer-custody/v1' and config['issuer']=='P24',
             'TRANSFER_EXACT_P24_COMMISSIONED_CONFIG')
        need(type(config['target']) is dict and set(config['target'])=={'project','environment','service','deployment'}
             and all(type(x)is str and x for x in config['target'].values()),'TRANSFER_COMMISSIONED_TARGET')
        try: Ed25519PublicKey.from_public_bytes(bytes.fromhex(config['public_key']))
        except (ValueError,TypeError) as exc: raise TransferRefused('TRANSFER_P24_KEY_NOT_COMMISSIONED') from exc
        obj=object.__new__(cls);obj.directory=Path(directory);obj.config=copy.deepcopy(config)
        obj.clock=clock;obj.test_only=test_only;obj.database=obj.directory/'bounded-transfer-acceptance.sqlite3'
        need(obj.database.is_file(),'TRANSFER_GLOBAL_STORE_NOT_COMMISSIONED')
        with obj._db() as db:need(db.execute('PRAGMA user_version').fetchone()[0]==1,'TRANSFER_STORE_SCHEMA')
        return obj

    @classmethod
    def from_protected_context(cls,directory,*,clock=time.time):
        directory=Path(directory);private_path(directory,True);private_path(directory/'p24-transfer-config.json')
        return cls._open(directory,decode((directory/'p24-transfer-config.json').read_bytes()),clock,False)

    @classmethod
    def provision(cls,directory,*,clock=time.time):
        """One-time separately authorized installation. NO acceptance/key generation."""
        directory=Path(directory);private_path(directory,True);private_path(directory/'p24-transfer-config.json')
        cls._initialize(directory)
        return cls.from_protected_context(directory,clock=clock)

    @classmethod
    def isolated_test_custody(cls,directory,config,*,clock):
        directory=Path(directory);directory.mkdir(mode=0o700,parents=True,exist_ok=False)
        cls._initialize(directory);return cls._open(directory,config,clock,True)

    @staticmethod
    def _initialize(directory):
        fd=os.open(directory/'bounded-transfer-acceptance.sqlite3',os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
        with contextlib.closing(sqlite3.connect(directory/'bounded-transfer-acceptance.sqlite3')) as db:
            db.execute('PRAGMA synchronous=FULL')
            db.executescript('CREATE TABLE accepted(operation_id TEXT PRIMARY KEY,nonce TEXT UNIQUE NOT NULL,token BLOB NOT NULL,state TEXT NOT NULL,result BLOB);PRAGMA user_version=1;')

    def _db(self):
        if not self.test_only:private_path(self.database)
        db=sqlite3.connect(self.database.as_uri()+'?mode=rw',uri=True,isolation_level=None,timeout=5)
        db.execute('PRAGMA synchronous=FULL');return contextlib.closing(db)

    def _signed(self,raw):
        envelope=decode(raw);need(type(envelope)is dict and set(envelope)=={'payload','signature'},'TRANSFER_SIGNED_ACCEPTANCE_FIELDS')
        try:Ed25519PublicKey.from_public_bytes(bytes.fromhex(self.config['public_key'])).verify(bytes.fromhex(envelope['signature']),canonical(envelope['payload']))
        except (InvalidSignature,ValueError,TypeError) as exc:raise TransferRefused('TRANSFER_EXTERNAL_P24_ACCEPTANCE_INVALID') from exc
        p=envelope['payload'];need(type(p)is dict and set(p)==TOKEN_FIELDS and p['schema']=='cerebro-p24-2872-transfer-acceptance/v1'
             and p['issuer']=='P24' and p['operation']=='PM_OWNER_TRANSFER','TRANSFER_EXACT_ACCEPTANCE_SCOPE')
        need(p['target']==self.config['target'] and p['backend_sha256']==self.config['backend_sha256'], 'TRANSFER_TARGET_BACKEND_MISMATCH')
        need(all(type(p[k])is str and 0<len(p[k])<=256 for k in ('operation_id','nonce')),'TRANSFER_OPERATION_NONCE')
        now=self.clock();a,b=p['not_before'],p['expires_at']
        need(all(type(v)in(int,float) and math.isfinite(v)for v in (now,a,b)) and 0<b-a<=1200 and a<=now<b,'TRANSFER_FINITE_RELEASE_EXPIRED_OR_EARLY')
        need(p['evidence_mode']==('ISOLATED_FIXTURE' if self.test_only else 'ACTUAL_P24_CONTENT_QUALIFIED_FINAL_REFS'), 'TRANSFER_FIXTURE_NOT_ACTUAL_ACCEPTANCE')
        return p

    def _runtime_target(self):
        """Actual protected service-process carrier, never token/config fallback.
        Custodian must run the commissioned port in the actual Context runtime;
        presence of matching environment strings alone is not remote attestation.
        No endpoint lookup or mutation. Missing/stale service carrier refuses.
        """
        names={'project':'RAILWAY_PROJECT_ID','environment':'RAILWAY_ENVIRONMENT_ID',
               'service':'RAILWAY_SERVICE_ID','deployment':'RAILWAY_DEPLOYMENT_ID'}
        actual={k:os.environ.get(v,'')for k,v in names.items()}
        need(all(type(v)is str and v and v==v.strip()for v in actual.values())
             and actual==self.config['target'],'TRANSFER_ACTUAL_RUNTIME_TARGET_MISSING_OR_DRIFTED')
        return actual

    def validate(self,token,*,owner_original_bytes,raw_plan_bytes,native_binding_bytes,final_evidence):
        p=self._signed(token)
        if not self.test_only:
            self._runtime_target() # accept, execute, and EVERY lower authority recheck.
        need(p['source_pins']==SOURCE_PINS and p['executor_sha256']==sha(Path(__file__).read_bytes()),'TRANSFER_EXACT_SOURCE_EXECUTOR')
        for name,pin in SOURCE_PINS.items():
            need(sha(Path(importlib.import_module(name).__file__).read_bytes())==pin,'TRANSFER_INSTALLED_SOURCE_DRIFT:'+name)
        pm=importlib.import_module('pm_admission_owner');q=importlib.import_module('pm_bounded_qualification');registry=importlib.import_module('control_context_registry')
        need(type(final_evidence)is dict and set(final_evidence)==q.FINAL_REFS,'TRANSFER_SEVEN_GENUINE_FINAL_REFS_REQUIRED')
        plan=decode(raw_plan_bytes);need(type(plan)is dict and set(plan)=={'action','before','after','decision'} and plan['action']=='TRANSFER','TRANSFER_PLAN_ONLY')
        decision=pm.validate_decision(plan['decision']);before=pm.validate_head(plan['before']);after=pm.validate_head(plan['after'])
        activation=q.require_activation_matches(after.get('bounded_activation'),decision)
        need(q.digest(activation)==ACTIVATION_CANONICAL and pm.digest(after)==AFTER_HEAD,
             'TRANSFER_ACTUAL_SEVEN_ACTIVATION_AND_AFTER_HEAD_REQUIRED')
        need(pm.digest(decision)==p['staged_decision_sha256']==STAGED_DECISION
             and pm.digest(before)==p['before_head_sha256']==BEFORE_HEAD,'TRANSFER_EXACT_STAGED_ORIGINAL_BEFORE')
        need(p['raw_owner_sha256']==sha(owner_original_bytes) and p['raw_plan_sha256']==sha(raw_plan_bytes)
             and p['after_head_sha256']==pm.digest(after) and p['native_binding_sha256']==sha(native_binding_bytes)
             and p['final_refs_sha256']==pm.digest(activation['receipts']),'TRANSFER_EXACT_RAW_BINDINGS')
        verify_final_originals(activation['receipts'],final_evidence)
        native=decode(native_binding_bytes)
        need(type(native)is dict and set(native)=={'pre_role_generation','actor_generation_shadow','authenticated_binding'},'TRANSFER_NATIVE_BINDING_FIELDS')
        birth=native['pre_role_generation'];shadow=native['actor_generation_shadow'];who=native['authenticated_binding']
        registry.validate_pre_role_generation(birth);registry.validate_actor_generation_shadow(shadow)
        need(type(who)is dict and set(who)==set(pm.IDENTITY) and all(who[k]==decision[k]for k in pm.IDENTITY),'TRANSFER_EXACT_FIVE_FIELD_BINDING')
        expected={'overlay_ref':decision['overlay_ref'],'actor_ref':decision['actor_ref'],'role':'PROJECT_MANAGER',
                  'actor_generation_ref':decision['actor_generation_ref'],'ready_unbound_receipt_ref':'READY_'+q.GENERATION}
        need(birth['generation_ref']==q.GENERATION and birth['lifecycle']=='ROLE_ATTACHED' and birth['revision']==3
             and birth['fingerprint']==NATIVE_FINGERPRINT and birth['role_overlay']==expected
             and birth['source_revision']==decision['source_revision']
             and birth['civilization_method_attestation']['method_fingerprint']==decision['method_fingerprint']
             and shadow['fingerprint']==SHADOW_FINGERPRINT and shadow['actor_ref']==decision['actor_ref']
             and shadow['generation_ref']==decision['actor_generation_ref'] and shadow['source_revision']==decision['source_revision']
             and shadow['lifecycle']in('READY','ACTIVE'),'TRANSFER_EXACT_ACTUAL_NATIVE_ATTACH')
        need(after['revision']==before['revision']+1 and after['owner_ref']==decision['actor_ref']
             and after['current_decision_ref']==decision['decision_ref']
             and after['activation_authorization_ref']!=before['activation_authorization_ref']
             and all(after[k]==before[k]==decision[k]for k in pm.IDENTITY[:2]),'TRANSFER_SEPARATE_EXACT_ACTIVATION')
        # Staged decision and admission interval are immutable; this NEW release
        # has its own finite interval. Never renew/restage or change owner hash.
        return p,plan

    def accept(self,token,**material):
        """Protected genuine external acceptance, separate from execute."""
        p,_=self.validate(token,**material)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            try:db.execute('INSERT INTO accepted VALUES(?,?,?,\'ACCEPTED\',NULL)',(p['operation_id'],p['nonce'],token))
            except sqlite3.IntegrityError as exc:raise TransferRefused('TRANSFER_OPERATION_OR_NONCE_ACCEPTED_NO_RETRY') from exc
            db.execute('COMMIT')
        return {'operation_id':p['operation_id'],'token_sha256':sha(token),'test_only':self.test_only}

    def execute(self,token,*,witness,owner_original_bytes,raw_plan_bytes,native_binding_bytes,final_evidence,isolated_test=False):
        from pm_protected_owner_entry import BackendWitness
        need(type(witness)is BackendWitness and witness.expected==self.config['backend_sha256'], 'TRANSFER_REAL_CHECKED_BACKEND_REQUIRED')
        need(not self.test_only or isolated_test is True,'TRANSFER_TEST_CUSTODY_NOT_PRODUCTION')
        material=dict(owner_original_bytes=owner_original_bytes,raw_plan_bytes=raw_plan_bytes,native_binding_bytes=native_binding_bytes,final_evidence=final_evidence)
        p,plan=self.validate(token,**material)
        lower=importlib.import_module('pm_admission_owner_operator');pm=importlib.import_module('pm_admission_owner')
        release={'issuer':'P24','operation':'PM_OWNER_TRANSFER','operation_id':p['operation_id']+':TRANSFER',
             'input_sha256':pm.digest(plan),'entry_sha256':SOURCE_PINS['pm_admission_owner_operator'],
             'owner_original_sha256':sha(owner_original_bytes),'not_before':p['not_before'],'expires_at':p['expires_at']}
        def authority(*,connection,release:dict,plan:dict,owner_original_bytes:bytes):
            check,current=self.validate(token,**material)
            need(check==p and current==plan and owner_original_bytes==material['owner_original_bytes']
                 and release==expected_release,'TRANSFER_ACCEPTED_OPERATION_CHANGED')
            witness.verify(connection)
            return True
        expected_release=copy.deepcopy(release)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT token,state FROM accepted WHERE operation_id=?',(p['operation_id'],)).fetchone()
            need(row is not None and row==(token,'ACCEPTED'),'TRANSFER_NOT_ACCEPTED_OR_SPENT')
            db.execute("UPDATE accepted SET state='STARTED_UNKNOWN_NO_RETRY' WHERE operation_id=?",(p['operation_id'],))
            db.execute('COMMIT') # Durable, global to commissioned port, before backend effect.
            db.execute('BEGIN IMMEDIATE')
            try:
                result=lower.apply_owner_plan(checked_connection_factory=witness.connect,plan=plan,release=release,
                    owner_original_bytes=owner_original_bytes,verify_effect_authority=authority,clock=self.clock)
                db.execute("UPDATE accepted SET state='COMPLETED',result=? WHERE operation_id=?",(canonical(result),p['operation_id']))
                db.execute('COMMIT')
            except BaseException as exc:
                exc.add_note('TRANSFER_ACCEPTED_OPERATION_SPENT_NO_RETRY')
                try:db.execute('ROLLBACK')
                except BaseException:exc.add_note('TRANSFER_CUSTODY_ROLLBACK_UNKNOWN_NO_RETRY')
                raise
        post_binding={k:plan['after'][k]for k in ('tenant_ref','workspace_ref')}
        post_binding.update(generation_ref=plan['decision']['generation_ref'],
             decision_ref=plan['decision']['decision_ref'],current_revision=plan['after']['revision'])
        return {**result,'actual_operative_profile_pass':False,'recipient_use_proven':False,
            'post_transfer_binding':post_binding,
            'next':'Candidate own authenticated PostgresPmAdmissionOwner.verify after actual independent head readback; then first separately admitted scoped recipient-use return. Legacy owner-ready/outbox assignment is NOT retargeted here. No RETIRE.'}


def run_protected_transfer(custody_directory,token,*,witness,owner_original_bytes,
                          raw_plan_bytes,native_binding_bytes,final_evidence):
    """Production-only protected composition entry. Acceptance must ALREADY
    exist in this commissioned stable store. No isolated_test argument, signer,
    trust-root generation, automatic acceptance or credential acquisition.
    This entry owns closure of the supplied actual witness.
    """
    from pm_protected_owner_entry import BackendWitness
    need(type(witness)is BackendWitness,'TRANSFER_REAL_CHECKED_BACKEND_REQUIRED')
    import sys
    try:
        port=P24BoundedTransferPort.from_protected_context(custody_directory)
        result=port.execute(token,witness=witness,owner_original_bytes=owner_original_bytes,
             raw_plan_bytes=raw_plan_bytes,native_binding_bytes=native_binding_bytes,final_evidence=final_evidence)
    finally:
        primary=sys.exception()
        try:witness.close()
        except BaseException:
            if primary is not None:primary.add_note('TRANSFER_OWN_BACKEND_CLOSURE_UNKNOWN_NO_RETRY')
            else:raise TransferRefused('TRANSFER_OWN_BACKEND_CLOSURE_UNKNOWN_NO_RETRY')
    return {**result,'own_backend_closure_verified':True}
