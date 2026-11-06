"""Concrete reader of the existing custodian's manually reconciled originals.

No attestation/signing service and no automatic rollback freshness proof. Only
the owned SSH/capture driver places the sealed private record/raw copies after
actual independent HumanCyborg archive reconciliation. This reader verifies
that exact record, its full-current-state joins and retained original bytes.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from pm_principal_authority import canonical,decode,require,sha

CUSTODY=Path('/var/lib/cerebro-pm-authority/authority')
ARCHIVE='C:/Cerebro/Authority/PM/history'
METHOD='OPERATOR-SUPPORT1.3/CURRENT_PM/D1_HUMAN_CYBORG/OWNED_RAILWAY_SSH_PRIVATE_CAPTURE'
FIELDS={'schema','custodian_ref','method_ref','archive_ref','archive_anchor_sha256',
        'invocation_ref','target','backend_sha256','volume_ref','ledger_sha256',
        'config_sha256','scope_kind','binding','unresolved_prior_invocations',
        'restore_disposition','originals'}
MAX_ORIGINAL=16*1024*1024


def verify_export(raw,expected_ledger,expected_config):
    def pairs(items):
        result={}
        for key,value in items:
            require(key not in result,'HISTORY_DUPLICATE_EXPORT_FIELD')
            result[key]=value
        return result
    value=json.loads(raw,object_pairs_hook=pairs,
        parse_constant=lambda _:(_ for _ in ()).throw(ValueError('HISTORY_NONFINITE_EXPORT')))
    require(type(value)is dict and set(value)=={'schema','config_sha256','ledger_sha256','rows'}
            and value['schema']=='cerebro-pm-private-full-history-export/v1'
            and value['config_sha256']==expected_config and value['ledger_sha256']==expected_ledger,
            'HISTORY_BEFORE_EXPORT_CONTEXT_MISMATCH')
    require(type(value['rows'])is dict and set(value['rows'])==
            {'appointments','operations','support_steps','support_rebinds'},'HISTORY_FULL_EXPORT_TABLES')
    digest=hashlib.sha256()
    for table,width in (('appointments',4),('operations',7),('support_steps',5)):
        rows=value['rows'][table]
        require(type(rows)is list,'HISTORY_EXPORT_ROWS')
        for row in rows:
            require(type(row)is list and len(row)==width,'HISTORY_EXPORT_ROW_SHAPE')
            for item in row:
                if type(item)is dict:
                    require(set(item)=={'blob_hex'} and type(item['blob_hex'])is str
                            and re.fullmatch('(?:[0-9a-f]{2})*',item['blob_hex']), 'HISTORY_EXPORT_BLOB')
                    body=bytes.fromhex(item['blob_hex'])
                else:
                    require(item is None or type(item)in(str,int,float),'HISTORY_EXPORT_SCALAR')
                    body=canonical(item)
                digest.update(len(body).to_bytes(8,'big'));digest.update(body)
    require(digest.hexdigest()==expected_ledger,'HISTORY_FULL_EXPORT_DIGEST_MISMATCH')


def _private_raw(directory,name,limit):
    parent=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        info=os.fstat(parent)
        require(info.st_uid==0 and stat.S_IMODE(info.st_mode)==0o700,
                'HISTORY_PRIVATE_DIRECTORY_REQUIRED')
        fd=os.open(name,os.O_RDONLY|os.O_NONBLOCK|os.O_NOFOLLOW,dir_fd=parent)
        try:
            before=os.fstat(fd)
            require(stat.S_ISREG(before.st_mode) and before.st_uid==0 and before.st_nlink==1
                    and stat.S_IMODE(before.st_mode)==0o600 and 0<before.st_size<=limit,
                    'HISTORY_BOUNDED_PRIVATE_ORIGINAL_REQUIRED')
            raw=b''
            while len(raw)<=limit:
                block=os.read(fd,min(65536,limit+1-len(raw)))
                if not block:break
                raw+=block
            after=os.fstat(fd)
            require((before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)==
                    (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns)
                    and len(raw)==before.st_size,'HISTORY_ORIGINAL_CHANGED')
            return raw
        finally:os.close(fd)
    finally:os.close(parent)


def verify_record(raw,*,target,backend_sha256,config_raw,ledger_sha256,kind,
                  read_original,expected_volume_ref,operation_id=None,raw_owner=None,raw_plan=None,decision=None):
    """Pure joins; read_original is this module's fixed private reader in use.

    Fixture injection of raw copies does not establish actual archive/custody.
    Manual custodian assessment is labelled as such, never machine authority.
    """
    record=decode(raw)
    require(type(record)is dict and set(record)==FIELDS
            and record['schema']=='cerebro-pm-custodian-history-reconciliation/v1',
            'HISTORY_EXACT_RECONCILIATION_FIELDS')
    require(record['method_ref']==METHOD and record['archive_ref']==ARCHIVE
            and all(type(record[k])is str and record[k] for k in ('custodian_ref','invocation_ref','volume_ref')),
            'HISTORY_EXISTING_OWNER_ROUTE_REQUIRED')
    require(record['target']==target and record['backend_sha256']==backend_sha256
            and record['volume_ref']==expected_volume_ref
            and record['config_sha256']==sha(config_raw) and record['ledger_sha256']==ledger_sha256,
            'HISTORY_ACTUAL_STATE_OR_TARGET_MISMATCH_HOLD')
    require(record['unresolved_prior_invocations']==[]
            and record['restore_disposition'] in ('NORMAL_SAME_VOLUME_RECONCILED',
                'RESTORE_RECONCILED_WITH_INDEPENDENT_ORIGINALS','FIRST_EXCLUSIVE_COMMISSION_CONFIRMED'),
            'HISTORY_UNRESOLVED_OR_RESTORE_NOT_RECONCILED_HOLD')
    require(record['scope_kind']==kind,'HISTORY_WRONG_ACTUAL_PURPOSE')
    if kind=='OPERATION':
        binding={'operation_id':operation_id,'raw_owner_sha256':sha(raw_owner),'raw_plan_sha256':sha(raw_plan)}
    elif kind in ('REBIND','OFFICE'):
        binding={'decision_sha256':sha(decision)}
    else:
        require(kind=='INSPECT','HISTORY_PURPOSE_REQUIRED')
        binding={}
    require(record['binding']==binding,'HISTORY_WRONG_EXACT_OPERATION_OR_DECISION')
    originals=record['originals']
    require(type(originals)is list and 3<=len(originals)<=32,'HISTORY_REQUIRED_INDEPENDENT_ORIGINALS')
    roles=set();total=0
    for item in originals:
        require(type(item)is dict and set(item)=={'role','sha256','bytes'}
                and type(item['role'])is str and item['role'] not in roles
                and type(item['sha256'])is str and re.fullmatch('[0-9a-f]{64}',item['sha256'])
                and type(item['bytes'])is int and 0<item['bytes']<=MAX_ORIGINAL,
                'HISTORY_EXACT_ORIGINAL_INDEX')
        roles.add(item['role']);total+=item['bytes']
        require(total<=64*1024*1024,'HISTORY_TOTAL_MATERIAL_BOUND')
        body=read_original(item['sha256'])
        require(len(body)==item['bytes'] and sha(body)==item['sha256'],'HISTORY_RETAINED_ORIGINAL_BYTES_MISMATCH')
        if item['role']=='before_ledger_export':
            verify_export(body,ledger_sha256,sha(config_raw))
    require({'before_ledger_export','independent_archive_anchor','prior_invocation_effect_closure_index'}<=roles,
            'HISTORY_REQUIRED_OWNER_ORIGINAL_ROLES')
    require(any(i['role']=='independent_archive_anchor' and i['sha256']==record['archive_anchor_sha256'] for i in originals),
            'HISTORY_INDEPENDENT_ANCHOR_JOIN')
    return {'history_original_sha256':sha(raw),'record':record,
            'assessment':'MANUAL_CUSTODIAN_RECONCILIATION_ORIGINALS_AND_STATE_MATCH',
            'automatic_restore_freshness_proof':False,'authority_granted':False}


def read_selected_history(**kwargs):
    raw=_private_raw(CUSTODY,'custodian-history-original.json',1048576)
    def original(digest):
        return _private_raw(CUSTODY/'history-originals',digest+'.original',MAX_ORIGINAL)
    return verify_record(raw,read_original=original,**kwargs)


def verify_operation_history_nonce(payload,history_sha256):
    prefix='HISTORY-'+history_sha256+'-'
    nonce=payload.get('nonce')
    require(type(nonce)is str and nonce.startswith(prefix) and len(nonce)>len(prefix)
            and len(nonce)<=240,'HISTORY_NOT_BOUND_BY_SIGNED_OPERATION_NONCE')
