"""UNACTIVATED custodian adapter for existing warm PM owner port.

Not an MCP endpoint or installer. STAGE and TRANSFER require distinct exact
root-private owner originals. No code here creates a backend or credentials.
Existing apply_owner_plan owns SQL/head/native guards and independent readback.
"""
from __future__ import annotations
import copy,hashlib,json,os,re,time
from pathlib import Path
from pm_admission_owner import IDENTITY,need,digest,validate_decision,validate_head
from pm_admission_owner_operator import apply_owner_plan
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

class WarmPmOwnerAuthority:
    def __init__(self,*,owner_original_bytes,caller_original_bytes,plan,witness,actual_target,
                 adapter_sha256,owner_operator_sha256,accepted_operation,clock=time.time):
        need(type(witness) is BackendWitness,'WARM_ACTUAL_CHECKED_BACKEND_WITNESS_REQUIRED')
        self.original=owner_original_bytes;self.owner=json.loads(owner_original_bytes)
        self.plan=copy.deepcopy(plan);self.witness=witness;self.clock=clock
        from pm_principal_authority import AcceptedPmOperation
        need(type(accepted_operation) is AcceptedPmOperation and accepted_operation.witness is witness, 'WARM_PROTECTED_ACCEPTANCE_REQUIRED')
        self.accepted_operation=accepted_operation
        o=self.owner
        need(type(o)is dict and set(o)==FIELDS,'WARM_EXACT_OWNER_FIELDS')
        # Issuer authority comes from genuine protected acceptance, not this label.
        # Human-signed appointment/epoch is checked in both consumers.
        need(o['schema']==SCHEMA,'WARM_DISPOSITION_SCHEMA')
        need(type(o['operation_id'])is str and re.fullmatch(r'[A-Za-z0-9-]{1,96}',o['operation_id']),'WARM_EXACT_OPERATION')
        need(o['action'] in ('STAGE','TRANSFER') and plan['action']==o['action'],'WARM_SINGLE_ACTION_ONLY')
        need(type(actual_target)is dict and set(actual_target)=={'project','environment','service','deployment'}
             and all(type(v)is str and v for v in actual_target.values()) and o['target']==actual_target,'WARM_ACTUAL_TARGET_REQUIRED')
        need(o['backend_sha256']==witness.expected and o['plan_commitment']==plan_commitment(plan),'WARM_BACKEND_PLAN_PINS')
        need(o['adapter_sha256']==adapter_sha256==hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
             and o['owner_operator_sha256']==owner_operator_sha256,'WARM_EXACT_ADAPTER_OPERATOR_PINS')
        need(all(type(o[k])in(int,float) for k in ('not_before','expires_at'))
             and 0<o['expires_at']-o['not_before']<=1200,'WARM_FINITE_INTERVAL')
        before=validate_head(plan['before']);after=validate_head(plan['after']);decision=validate_decision(plan['decision'])
        need(decision['generation_ref']!=SPECIAL_GENERATION,'WARM_SPECIAL_CANDIDATE_KEEPS_OWN_ENTRY')
        need(before['owner_ref']==o['prior_owner_ref']==decision['predecessor_owner_ref']
             and before['revision']==decision['expected_current_revision'],'WARM_EXACT_OUTGOING_CURRENT_PLAN')
        native=json.loads(caller_original_bytes)
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

        self.accepted_operation.check_prepared(release=self.release(), plan=self.plan,
             owner_original_bytes=self.original, adapter_sha256=adapter_sha256)

    def verify(self,*,connection,release,plan,owner_original_bytes):
        o=self.owner
        need(owner_original_bytes==self.original and plan==self.plan,'WARM_EXACT_ORIGINAL_PLAN')
        need(release==self.release(),'WARM_EXACT_RELEASE')
        need(o['not_before']<=self.clock()<o['expires_at'],'WARM_EXPIRED_OR_EARLY')
        self.accepted_operation.verify(connection=connection,release=release,plan=plan,owner_original_bytes=owner_original_bytes)
        return True

    def release(self):
        o=self.owner
        return {'issuer':o['issuer'],'operation':'PM_OWNER_'+o['action'],
          'operation_id':o['operation_id']+':'+o['action'],'input_sha256':digest(self.plan),
          'entry_sha256':o['owner_operator_sha256'],'owner_original_sha256':hashlib.sha256(self.original).hexdigest(),
          'not_before':o['not_before'],'expires_at':o['expires_at']}

def execute_intake(directory,manifest_sha256,*,witness,authority_factory,accepted_operation_id,clock=time.time):
    """Supported root-custodian invocation; caller supplies ACTUAL existing
    BackendWitness, not callbacks/defaultTrue/env credentials from a worker.
    Own wrapper must close witness in finally. No per-rotation code edits.
    """
    from pm_principal_authority import PrincipalAuthorityFactory
    need(type(authority_factory) is PrincipalAuthorityFactory and not authority_factory.test_only, 'WARM_COMMISSIONED_AUTHORITY_FACTORY_REQUIRED')
    directory=Path(directory)
    need(os.geteuid()==0 and directory.is_absolute() and directory.parent==Path('/run')
         and directory.name.startswith('cerebro-pm-warm-'),'WARM_ROOT_PRIVATE_INTAKE_REQUIRED')
    actual_target={k:os.environ.get(v,'') for k,v in {'project':'RAILWAY_PROJECT_ID',
       'environment':'RAILWAY_ENVIRONMENT_ID','service':'RAILWAY_SERVICE_ID','deployment':'RAILWAY_DEPLOYMENT_ID'}.items()}
    m=json.loads(private_file(directory,'intake-manifest.json',manifest_sha256))
    need(set(m)=={'owner_original_sha256','caller_original_sha256','plans_file_sha256'},'WARM_INTAKE_FIELDS')
    original=private_file(directory,'owner-original.json',m['owner_original_sha256'])
    caller=private_file(directory,'candidate-readback.json',m['caller_original_sha256'])
    plans=json.loads(private_file(directory,'owner-plans.json',m['plans_file_sha256']))
    need(type(plans)is list and len(plans)==1,'WARM_ONE_EXPLICIT_ACTION')
    from pm_admission_owner_operator import __file__ as operator_file
    authority=WarmPmOwnerAuthority(owner_original_bytes=original,caller_original_bytes=caller,plan=plans[0],
       witness=witness,actual_target=actual_target,adapter_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
       owner_operator_sha256=hashlib.sha256(Path(operator_file).read_bytes()).hexdigest(),
       accepted_operation=authority_factory.accepted_operation(accepted_operation_id,witness=witness,actual_target=actual_target),clock=clock)
    need(authority.owner['not_before']<=clock()<authority.owner['expires_at'],'WARM_EXPIRED_OR_EARLY')
    # The protected Context store owns the global accepted-op claim, not this folder.
    return apply_owner_plan(checked_connection_factory=witness.connect,plan=authority.plan,
       release=authority.release(),owner_original_bytes=original,verify_effect_authority=authority.verify,
       operation_authority=authority.accepted_operation,clock=clock)