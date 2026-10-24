"""Closed business-identity diagnostics; no credentials or arbitrary arguments."""
import re

FIELDS={'schema','role','owner_family','stage','operation','identities','reason_code'}
REASONS=frozenset(('BOUNDED_EXISTING_CALL_REQUIRED','UNSELECTED_TOOL','EXACT_OWNER_FAMILY',
 'EXACT_ASSIGNED_ROLE_REQUIRED','ACTUAL_OWN_CURRENT_LEASE_REQUIRED','NO_BROAD_OWNER_READ',
 'RECOVERY_ONLY_PHASE','EXACT_SELECTED_OBLIGATIONS','EXACT_SELECTED_OBLIGATION',
 'ACTUAL_OWN_CURRENT_ATTEMPT_REQUIRED','EXACT_PRIOR_ATTEMPT_REQUIRED','NO_UNSELECTED_DISCOVERY',
 'NO_CLOSE_RETRY','ACTUAL_LEASE_RESULT_REQUIRED','COMMITTED_LEASE_CLOSE_REQUIRED',
 'EXACT_RECOVERY_ONLY','CANONICAL_CUSTODY_RESULT_REQUIRED','DURABLE_RECEIVER_OBSERVATION_REQUIRED',
 'DURABLE_CUSTODY_MATCH_REQUIRED','ORIGINAL_DUTY_RECONCILIATION_PENDING','EXACT_ORIGINAL_PROOFS_REQUIRED',
 'OWN_LEASE_CLOSE_UNCONFIRMED','NORMAL_BOUND_REQUIRED','UNSELECTED_EXPIRED_INTENT',
 'EXPIRED_INTENT_CUSTODY_NOT_PROVEN','EXACT_CURRENT_ROW_KEYSET_REQUIRED',
 'EXPLICIT_RECOVERY_SELECTION_REQUIRED','NO_RECOVERY_BROKER_EFFECT','CURRENT_RUNTIME_REQUIRED',
 'NO_EXPIRED_PUBLICATION','CONTEXT_CALL_FAILURE'))
STAGES=frozenset(('BEFORE_CALL','CONTEXT_CALL','RESULT_CONTRACT','ORIGINAL_RECONCILE',
                  'LEASE_CLOSE','BROKER_BOUNDARY','NORMAL_EXPIRED_CUSTODY'))
OPERATIONS=frozenset(('open_bk11_service_runtime_v1','verify_bk11_service_lease_v1',
    'control_bk11_service_attempt_v1','discover_bk11_backend_obligations_v1',
    'reconcile_bk11_backend_obligation_v1','accept_bk11_backend_delivery_v1',
    'read_bk11_backend_disposition_v1','close_bk11_service_runtime_v1',
    'read_pm_owner_d1_outbox_v1','read_room_return_d1_outbox_v1',
    'settle_pm_owner_d1_outbox_v1','settle_room_return_d1_outbox_v1',
    'publish','fetch_one','ack','nak','UNSPECIFIED'))
MID=re.compile(r'\Ad1:[a-f0-9]{40}\Z');HASH=re.compile(r'\A[a-f0-9]{64}\Z')


def safe_scope_diagnostic(value):
    if type(value) is not dict or set(value)!=FIELDS:return None
    if any(type(value[k]) is not str for k in ('schema','role','owner_family','stage','operation','reason_code')):return None
    if value['schema']!='bk11-scoped-operation-diagnostic/v1' or value['role'] not in ('publisher','receiver'):return None
    if value['owner_family'] not in ('PM_OWNER','ROOM_RETURN','UNSPECIFIED'):return None
    if value['stage'] not in STAGES or value['operation'] not in OPERATIONS:return None
    if value['reason_code'] not in REASONS:return None
    identities=value['identities']
    if type(identities) is not list or len(identities)>3:return None
    for item in identities:
        if type(item) is not dict or set(item)!={'message_id','pointer_sha256'}:return None
        if type(item['message_id']) is not str or type(item['pointer_sha256']) is not str:return None
        if MID.fullmatch(item['message_id']) is None or HASH.fullmatch(item['pointer_sha256']) is None:return None
    return {**value,'identities':[dict(item) for item in identities]}


class ScopedOperationRefused(RuntimeError):
    def __init__(self,reason,diagnostic):
        super().__init__(reason)
        self.safe_scope_diagnostic=safe_scope_diagnostic(diagnostic)


def diagnostic(role,family,stage,operation,args=None,reason_code='CONTEXT_CALL_FAILURE'):
    args=args if type(args) is dict else {}
    candidates=args.get('obligations',[])
    if 'message_id' in args:candidates=[{'message_id':args.get('message_id'),'pointer_sha256':args.get('pointer_sha256')}]
    value={'schema':'bk11-scoped-operation-diagnostic/v1','role':role,
           'owner_family':family if type(family) is str and family in ('PM_OWNER','ROOM_RETURN') else 'UNSPECIFIED',
           'stage':stage,'operation':operation if type(operation) is str and operation in OPERATIONS else 'UNSPECIFIED',
           'reason_code':reason_code,
           'identities':candidates if type(candidates) is list and len(candidates)<=3 else []}
    return safe_scope_diagnostic(value) or safe_scope_diagnostic({**value,'identities':[]})
