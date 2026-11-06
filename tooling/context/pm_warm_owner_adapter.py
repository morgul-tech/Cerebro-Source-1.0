"""UNACTIVATED custodian adapter for existing warm PM owner port.

Not an MCP endpoint or installer. STAGE and TRANSFER require distinct exact
root-private owner originals. No code here creates a backend or credentials.
Existing apply_owner_plan owns SQL/head/native guards and independent readback.
"""
from __future__ import annotations
import copy,hashlib,json,math,os,re,stat,time
from pathlib import Path
from pm_admission_owner import IDENTITY,need,digest,validate_decision,validate_head
from pm_ordinary_owner_operator import apply_owner_plan
from pm_protected_owner_entry import BackendWitness,private_file,canonical
from pm_bounded_qualification import GENERATION as SPECIAL_GENERATION
from control_context_registry import validate_pre_role_generation

SCHEMA='cerebro-pm-warm-owner-disposition/v1'
FIELDS={'schema','issuer','operation_id','action','target','prior_owner_ref',
 'candidate_chat','generation_ref','candidate_readback_sha256','accepted_metadata_ceiling',
 'backend_sha256','owner_operator_sha256','adapter_sha256','plan_commitment',
 'not_before','expires_at'}

def plan_commitment(plan):
    value=copy.deepcopy(plan)
    # STAGE alone carries its own original hash. TRANSFER MUST bind the exact
    # already-staged decision owner hash instead of resetting it to a marker.
    if value['action']=='STAGE':value['decision']['owner_original_sha256']='OWNER_ORIGINAL_SELF_HASH'
    return digest(value)

class PreparedPmOwnerMaterial:
    """Shared structural guards. No acceptance, clock, backend or authority claim."""
    def __init__(self,*,owner_original_bytes,caller_original_bytes,plan,actual_target,
                 backend_sha256,adapter_sha256,owner_operator_sha256):
        from pm_principal_authority import decode
        self.original=owner_original_bytes;self.owner=decode(owner_original_bytes)
        self.plan=copy.deepcopy(plan)
        need(type(plan)is dict and set(plan)=={'action','before','after','decision'},'WARM_EXACT_PLAN_FIELDS')
        o=self.owner
        need(type(o)is dict and set(o)==FIELDS,'WARM_EXACT_OWNER_FIELDS')
        # Issuer authority comes from genuine protected acceptance, not this label.
        # Human-signed appointment/epoch is checked in both consumers.
        need(o['schema']==SCHEMA,'WARM_DISPOSITION_SCHEMA')
        need(type(o['operation_id'])is str and re.fullmatch(r'[A-Za-z0-9-]{1,96}',o['operation_id']),'WARM_EXACT_OPERATION')
        need(o['action'] in ('STAGE','TRANSFER') and plan['action']==o['action'],'WARM_SINGLE_ACTION_ONLY')
        need(type(actual_target)is dict and set(actual_target)=={'project','environment','service','deployment'}
             and all(type(v)is str and v for v in actual_target.values()) and o['target']==actual_target,'WARM_ACTUAL_TARGET_REQUIRED')
        need(o['backend_sha256']==backend_sha256 and o['plan_commitment']==plan_commitment(plan),'WARM_BACKEND_PLAN_PINS')
        need(o['adapter_sha256']==adapter_sha256==hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
             and o['owner_operator_sha256']==owner_operator_sha256,'WARM_EXACT_ADAPTER_OPERATOR_PINS')
        need(all(type(o[k])in(int,float) and math.isfinite(o[k]) for k in ('not_before','expires_at'))
             and 0<o['expires_at']-o['not_before']<=1200,'WARM_FINITE_INTERVAL')
        before=validate_head(plan['before']);after=validate_head(plan['after']);decision=validate_decision(plan['decision'])
        need(decision['generation_ref']!=SPECIAL_GENERATION,'WARM_SPECIAL_CANDIDATE_KEEPS_OWN_ENTRY')
        need(before['owner_ref']==o['prior_owner_ref']==decision['predecessor_owner_ref']
             and before['revision']==decision['expected_current_revision'],'WARM_EXACT_OUTGOING_CURRENT_PLAN')
        native=decode(caller_original_bytes)
        need(hashlib.sha256(caller_original_bytes).hexdigest()==o['candidate_readback_sha256']
             and native.get('isError')is False and '_meta'not in native
             and o['accepted_metadata_ceiling']=='NOT_EXPOSED','WARM_RAW_CALLER_ORIGINAL')
        content=native['structuredContent']
        need(set(content)=={'pre_role_generation','authenticated_binding','repository_permission_required'}
             and content['repository_permission_required']is False,'WARM_NATIVE_CAPTURE_FIELDS')
        who=content['authenticated_binding'];birth=content['pre_role_generation'];validate_pre_role_generation(birth)
        need(type(who)is dict and set(who)==set(IDENTITY)
             and all(type(who[k])is str and who[k] and decision[k]==who[k] for k in IDENTITY),'WARM_EXACT_FIVE_FIELD_CALLER')
        need(decision['candidate_chat']==o['candidate_chat'] and decision['generation_ref']==o['generation_ref']==birth['generation_ref']
             and all(decision[k]==birth[k] for k in ('tenant_ref','workspace_ref','source_revision'))
             and decision['ready_unbound_receipt_fingerprint']==birth['ready_unbound_receipt']['receipt_fingerprint']
             and decision['method_fingerprint']==birth['civilization_method_attestation']['method_fingerprint'],'WARM_EXACT_NATIVE_BIRTH')
        if o['action']=='STAGE':
            need(before==after and birth['lifecycle']=='READY_UNBOUND'
                 and birth['revision']==decision['expected_revision']
                 and birth['fingerprint']==decision['pre_role_fingerprint']
                 and decision['owner_original_sha256']==hashlib.sha256(self.original).hexdigest(),'WARM_STAGE_ORIGINAL_BIRTH_NO_CURRENT_CHANGE')
        else:
            need(birth['lifecycle']=='ROLE_ATTACHED' and birth['revision']==decision['expected_revision']+1
                 and after['owner_ref']==decision['actor_ref'] and after['revision']==before['revision']+1
                 and after['current_decision_ref']==decision['decision_ref'],'WARM_TRANSFER_AFTER_ACTUAL_ATTACH')

    def release(self):
        o=self.owner
        return {'issuer':o['issuer'],'operation':'PM_OWNER_'+o['action'],
          'operation_id':o['operation_id']+':'+o['action'],'input_sha256':digest(self.plan),
          'entry_sha256':o['owner_operator_sha256'],'owner_original_sha256':hashlib.sha256(self.original).hexdigest(),
          'not_before':o['not_before'],'expires_at':o['expires_at']}


def preflight_material(*,clock=time.time,**kwargs):
    """Key-free packet fit only. It cannot create or imply a live acceptance."""
    material=PreparedPmOwnerMaterial(**kwargs)
    now=clock()
    need(type(now)in(int,float) and math.isfinite(now)
         and now<min(material.owner['expires_at'],material.plan['decision']['expires_at'])
         and max(material.owner['not_before'],material.plan['decision']['not_before'])
             <min(material.owner['expires_at'],material.plan['decision']['expires_at']),
         'WARM_EXPIRED_OR_NONOVERLAPPING_PREPARATION')
    return {'schema':'cerebro-pm-material-preflight/v1','material_fit':'PASS',
            'action':material.plan['action'],'plan_sha256':digest(material.plan),
            'owner_sha256':hashlib.sha256(material.original).hexdigest(),
            'authority':'NONE','acceptance':'NOT_ASSESSED',
            'backend_freshness':'NOT_ASSESSED','receiver_ready':'NOT_ASSESSED',
            'validity':'NOT_EXPIRED_START_MAY_BE_FUTURE',
            'key_field':'NOT_OPENED','execution_allowed':False}


def validate_operation_material(operation,material):
    """Join a genuinely verified signed payload to this exact prepared release.

    This function does not verify a signature or admit an operation on its own.
    The protected factory remains the verifier and global acceptance producer.
    """
    release=material.release()
    owner=material.owner
    need(all(operation.get(k)==expected for k,expected in {
        'issuer':owner['issuer'],'operation_id':owner['operation_id'],
        'action':owner['action'],'target':owner['target'],
        'backend_sha256':owner['backend_sha256'],
        'raw_owner_sha256':release['owner_original_sha256'],
        'plan_sha256':release['input_sha256'],
        'entry_sha256':release['entry_sha256'],
        'adapter_sha256':owner['adapter_sha256'],
        'not_before':release['not_before'],'expires_at':release['expires_at']}.items()),
        'WARM_SIGNED_OPERATION_NOT_EXACT_PREPARED_RELEASE')
    return True


def _private_material_file(directory,name,expected):
    """Ordinary-only bounded FD read; FIFO never blocks before type check."""
    need(name in {'intake-manifest.json','owner-original.json','candidate-readback.json','owner-plans.json'},
         'WARM_FIXED_PRIVATE_NAME')
    parent=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        info=os.fstat(parent)
        need(info.st_uid==0 and stat.S_IMODE(info.st_mode)==0o700,'WARM_PRIVATE_DIRECTORY')
        fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
        try:
            before=os.fstat(fd)
            need(stat.S_ISREG(before.st_mode) and before.st_uid==0 and before.st_nlink==1
                 and stat.S_IMODE(before.st_mode)==0o600 and 0<before.st_size<=1048576,'WARM_PRIVATE_FILE')
            raw=b''
            while len(raw)<=1048576:
                block=os.read(fd,min(65536,1048577-len(raw)))
                if not block:break
                raw+=block
            after=os.fstat(fd)
            need((before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)==
                 (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns)
                 and len(raw)==before.st_size and hashlib.sha256(raw).hexdigest()==expected,
                 'WARM_PRIVATE_FILE_CHANGED_OR_HASH')
            return raw
        finally:os.close(fd)
    finally:os.close(parent)


def read_intake_material(directory,manifest_sha256):
    """Same complete private packet parser for preflight and execution."""
    from pm_principal_authority import decode
    directory=Path(directory)
    need(os.geteuid()==0 and directory.is_absolute() and directory.parent==Path('/run')
         and directory.name.startswith('cerebro-pm-warm-'),'WARM_ROOT_PRIVATE_INTAKE_REQUIRED')
    m=decode(_private_material_file(directory,'intake-manifest.json',manifest_sha256))
    need(type(m)is dict and set(m)=={'owner_original_sha256','caller_original_sha256','plans_file_sha256'},
         'WARM_INTAKE_FIELDS')
    original=_private_material_file(directory,'owner-original.json',m['owner_original_sha256'])
    caller=_private_material_file(directory,'candidate-readback.json',m['caller_original_sha256'])
    plans=decode(_private_material_file(directory,'owner-plans.json',m['plans_file_sha256']))
    need(type(plans)is list and len(plans)==1,'WARM_ONE_EXPLICIT_ACTION')
    return original,caller,plans[0]


class WarmPmOwnerAuthority(PreparedPmOwnerMaterial):
    def __init__(self,*,owner_original_bytes,caller_original_bytes,plan,witness,actual_target,
                 adapter_sha256,owner_operator_sha256,accepted_operation,clock=time.time):
        from pm_principal_authority import AcceptedPmOperation
        need(type(witness) is BackendWitness,'WARM_ACTUAL_CHECKED_BACKEND_WITNESS_REQUIRED')
        need(type(accepted_operation) is AcceptedPmOperation and accepted_operation.witness is witness,
             'WARM_PROTECTED_ACCEPTANCE_REQUIRED')
        super().__init__(owner_original_bytes=owner_original_bytes,caller_original_bytes=caller_original_bytes,
             plan=plan,actual_target=actual_target,backend_sha256=witness.expected,
             adapter_sha256=adapter_sha256,owner_operator_sha256=owner_operator_sha256)
        self.witness=witness;self.clock=clock;self.accepted_operation=accepted_operation
        self.accepted_operation.check_prepared(release=self.release(),plan=self.plan,
             owner_original_bytes=self.original,adapter_sha256=adapter_sha256)

    def verify(self,*,connection,release,plan,owner_original_bytes):
        need(owner_original_bytes==self.original and plan==self.plan,'WARM_EXACT_ORIGINAL_PLAN')
        need(release==self.release(),'WARM_EXACT_RELEASE')
        need(max(self.owner['not_before'],self.plan['decision']['not_before'])<=self.clock()
             <min(self.owner['expires_at'],self.plan['decision']['expires_at']),'WARM_EXPIRED_OR_EARLY')
        self.accepted_operation.verify(connection=connection,release=release,plan=plan,owner_original_bytes=owner_original_bytes)
        return True

def execute_intake(directory,manifest_sha256,*,witness,authority_factory,accepted_operation_id,clock=time.time):
    """Supported root-custodian invocation; caller supplies ACTUAL existing
    BackendWitness, not callbacks/defaultTrue/env credentials from a worker.
    Own wrapper must close witness in finally. No per-rotation code edits.
    """
    from pm_principal_authority import PrincipalAuthorityFactory
    need(type(authority_factory) is PrincipalAuthorityFactory and not authority_factory.test_only, 'WARM_COMMISSIONED_AUTHORITY_FACTORY_REQUIRED')
    directory=Path(directory)
    actual_target={k:os.environ.get(v,'') for k,v in {'project':'RAILWAY_PROJECT_ID',
       'environment':'RAILWAY_ENVIRONMENT_ID','service':'RAILWAY_SERVICE_ID','deployment':'RAILWAY_DEPLOYMENT_ID'}.items()}
    original,caller,plan=read_intake_material(directory,manifest_sha256)
    from pm_ordinary_owner_operator import __file__ as operator_file
    authority=WarmPmOwnerAuthority(owner_original_bytes=original,caller_original_bytes=caller,plan=plan,
       witness=witness,actual_target=actual_target,adapter_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
       owner_operator_sha256=hashlib.sha256(Path(operator_file).read_bytes()).hexdigest(),
       accepted_operation=authority_factory.accepted_operation(accepted_operation_id,witness=witness,actual_target=actual_target),clock=clock)
    need(authority.owner['not_before']<=clock()<authority.owner['expires_at'],'WARM_EXPIRED_OR_EARLY')
    # The protected Context store owns the global accepted-op claim, not this folder.
    return apply_owner_plan(checked_connection_factory=witness.connect,plan=authority.plan,
       release=authority.release(),owner_original_bytes=original,verify_effect_authority=authority.verify,
       operation_authority=authority.accepted_operation,clock=clock)
