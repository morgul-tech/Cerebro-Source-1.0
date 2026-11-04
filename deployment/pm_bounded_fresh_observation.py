"""Proposed separate root-custodian READ-ONLY preflight before install sealing.

Not an MCP tool, CLI, credential resolver or authorization issuer. The existing
custodian must first admit an exact finite observation purpose through its real
protected port. A qualification choice or these Python arguments cannot do so.
No driver starts, key operations, default DSNs, SQL writes or final plan issuance.
"""
from __future__ import annotations
import hashlib,json,math,re,time
import sys
from pathlib import Path

GENERATION='CEREBRO-BOOT-2872BF170DBF470C86A8A52C7B1179A1'
CHAT='01a1a700-0a7c-7322-8b78-9cb73953eaa6'
BIRTH_SOURCE='2e2f75e938fba46bad5fb1cdfaa03c1bd8cc9a4c'
METHOD='cd794305005b4a4fd2145386e2e9b7776c838c20898c9e27f9810b1d3b7a11fd'
TARGET={'project':'ce122571-78e8-45a3-997a-061937e9aa38',
        'environment':'8e602a9f-d23a-40b2-a867-53b5397d27e3',
        'service':'882fdd63-140f-4ea1-a447-623f1d51c420'}

class ObservationRefused(ValueError):pass
def need(ok,reason):
    if not ok:raise ObservationRefused(reason)

def validate_observation_original(raw,caller_raw,actual_runtime,*,now):
    need(type(raw)is bytes and type(caller_raw)is bytes,'OBS_RAW_ORIGINALS_REQUIRED')
    o=json.loads(raw)
    fields={'schema','issuer','operation','operation_id','target','expected_deployment',
            'expected_runtime_source','candidate_generation','candidate_chat','not_before',
            'expires_at','entry_sha256','candidate_readback_sha256'}
    need(type(o)is dict and set(o)==fields and o['schema']=='cerebro-pm-fresh-observation-purpose/v1'
         and o['issuer']=='P24' and o['operation']=='PM_FRESH_OBSERVATION_ONLY',
         'OBS_EXACT_SEPARATE_PURPOSE_NOT_INSTALL_OR_QUALIFICATION_GO')
    need(o['target']==TARGET and o['candidate_generation']==GENERATION and o['candidate_chat']==CHAT,
         'OBS_FIXED_CANDIDATE_TARGET')
    for k in ['not_before','expires_at']:
        need(type(o[k])in(int,float) and math.isfinite(o[k]),'OBS_FINITE_TIME')
    need(0<o['expires_at']-o['not_before']<=1200 and o['not_before']<=now<o['expires_at'],
         'OBS_FINITE_PURPOSE_EXPIRED_OR_INVALID')
    need(type(actual_runtime)is dict and set(actual_runtime)=={'target','deployment_id','source_revision'}
         and actual_runtime['target']==TARGET and actual_runtime['deployment_id']==o['expected_deployment']
         and actual_runtime['source_revision']==o['expected_runtime_source'],
         'OBS_ACTUAL_ROOT_RUNTIME_PROVIDER_SELECTION')
    need(type(o['expected_deployment'])is str and re.fullmatch(r'[0-9a-f-]{36}',o['expected_deployment'])
         and type(o['expected_runtime_source'])is str and re.fullmatch(r'[0-9a-f]{40}',o['expected_runtime_source']),
         'OBS_EXACT_DEPLOYMENT_SOURCE_FORMAT')
    need(o['entry_sha256']==hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
         and o['candidate_readback_sha256']==hashlib.sha256(caller_raw).hexdigest(),
         'OBS_CODE_AND_OWN_CALLER_ORIGINAL_PINS')
    c=json.loads(caller_raw)
    need(type(c)is dict and c.get('isError')is False and type(c.get('structuredContent'))is dict,
         'OBS_ACTUAL_CANDIDATE_WRAPPER')
    v=c['structuredContent']
    need(type(v.get('authenticated_binding'))is dict and type(v.get('pre_role_generation'))is dict,
         'OBS_CALLER_BINDING_AND_BIRTH_REQUIRED')
    birth=v['pre_role_generation'];who=v['authenticated_binding']
    need(birth.get('generation_ref')==GENERATION and birth.get('revision')==2
         and birth.get('lifecycle')=='READY_UNBOUND'
         and birth.get('source_revision')==BIRTH_SOURCE
         and birth.get('civilization_method_attestation',{}).get('method_fingerprint')==METHOD
         and type(birth.get('fingerprint'))is str and re.fullmatch(r'[0-9a-f]{64}',birth['fingerprint']),
         'OBS_EXACT_EXISTING_BIRTH')
    need(all(type(who.get(k))is str and who[k] for k in
         ('tenant_ref','workspace_ref','principal_ref','consumer_ref','session_ref'))
         and all(who[k]==birth.get(k) for k in ('tenant_ref','workspace_ref')),
         'OBS_NO_INFERRED_CALLER')
    return o,birth,who


def observe_from_protected_intake(directory,manifest_sha256,*,actual_runtime,
                                  runtime_factory,migration_factory,nonce):
    """Existing root entry supplies concrete target-bound factories, never client input.

    actual_runtime must be the existing root service's independently selected
    deployment/source projection, qualified at that port; passing a dict here is
    NOT proof of its origin. Port selection/admission remains an owned boundary.
    """
    from pm_protected_owner_entry import private_file,BackendWitness,facts,digest
    manifest=json.loads(private_file(directory,'intake-manifest.json',manifest_sha256))
    need(type(manifest)is dict and set(manifest)=={'owner_original_sha256','caller_original_sha256'},
         'OBS_MANIFEST_EXACT_FIELDS')
    raw=private_file(directory,'owner-original.json',manifest['owner_original_sha256'])
    caller=private_file(directory,'candidate-readback.json',manifest['caller_original_sha256'])
    o,birth,who=validate_observation_original(raw,caller,actual_runtime,now=time.time())
    need(type(nonce)is str and nonce=='pm_observe_'+hashlib.sha256(raw).hexdigest()[:32],
         'OBS_OWN_PURPOSE_BOUND_NONCE_REQUIRED')
    own=runtime_factory();witness=None
    try:
        runtime=facts(own)
        need(runtime['application_name']==nonce,'OBS_RUNTIME_NONCE')
        fields=('db','db_oid','postmaster_started','server_addr','server_port','version')
        # Initial digest comes from this actual target-bound live runtime, not
        # any AD1 installation or migration connection chosen by the client.
        backend_sha=digest({k:runtime[k] for k in fields})
        witness=BackendWitness(runtime_factory=lambda:own,migration_factory=migration_factory,
                               expected_backend_sha256=backend_sha,nonce=nonce)
        cuts=[]
        for _ in range(2):
            need(time.time()<o['expires_at'],'OBS_EXPIRED_NO_NEXT_READ')
            con=witness.connect()
            try:
                with con.cursor() as cur:
                    cur.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                    witness.verify(con)
                    for k in ('tenant_ref','workspace_ref'):
                        cur.execute('SELECT set_config(%s,%s,true)',('cerebro.'+k,who[k]))
                    cur.execute("SELECT to_regclass('cerebro_pm_current_heads') AS relation")
                    relation=cur.fetchone();exists=(relation.get('relation') if isinstance(relation,dict) else relation[0])
                    if exists is None:
                        head={'observation':'RELATION_ABSENT_NOT_BOOTSTRAP_AUTHORITY'}
                    else:
                        cur.execute('SELECT head_payload FROM cerebro_pm_current_heads WHERE tenant_ref=%s AND workspace_ref=%s',
                                    (who['tenant_ref'],who['workspace_ref']))
                        row=cur.fetchone();head=row if row is not None else {'observation':'ROW_ABSENT_NOT_BOOTSTRAP_AUTHORITY'}
                    cur.execute('SELECT generation_payload FROM cerebro_pre_role_generation_heads WHERE tenant_ref=%s AND workspace_ref=%s AND generation_ref=%s',
                                (who['tenant_ref'],who['workspace_ref'],GENERATION))
                    observed=cur.fetchone()
                    need(observed is not None,'OBS_NATIVE_BIRTH_ABSENT')
                    actual=observed['generation_payload'] if isinstance(observed,dict) else observed[0]
                    if isinstance(actual,str):actual=json.loads(actual)
                    need(actual==birth,'OBS_NATIVE_ORIGINAL_NOT_CURRENT')
                    cuts.append({'head':head,'birth_fingerprint':actual['fingerprint']})
                con.rollback()
            finally:con.close()
        need(cuts[0]==cuts[1],'OBS_TWO_CUTS_DRIFT_NO_PLAN_SEAL')
        need(time.time()<o['expires_at'],'OBS_EXPIRED_NO_RESULT_SEAL')
        return {'schema':'cerebro-pm-private-fresh-observation/v1','authority':'NONE',
                'operation_id':o['operation_id'],'purpose_sha256':hashlib.sha256(raw).hexdigest(),
                'caller_original_sha256':hashlib.sha256(caller).hexdigest(),
                'backend_sha256':backend_sha,'private_cut':cuts[1],
                'actual_runtime':actual_runtime,'recorded_at':time.time(),
                'expires_at':o['expires_at'],'reads_only':True,
                'does_not_issue_install_stage_transfer_or_bootstrap':True}
    finally:
        primary=sys.exception()
        try:
            if witness is not None:witness.close()
            else:
                try:own.rollback()
                finally:own.close()
        except BaseException as closure:
            if primary is not None:primary.add_note('OBS_OWN_CLOSURE_UNKNOWN:'+type(closure).__name__)
            else:raise
