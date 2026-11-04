"""PM-only native birth checks; no Worker grant/adoption or new birth."""
import re
from pm_admission_owner import need

def verify_native_pm_birth(state, *, generation_ref, source_revision, verified_source):
    need(type(state) is dict and state.get('worker_method_adoption') is None,
         'PM_NATIVE_BIRTH_NO_WORKER_ADOPTION')
    need(state.get('generation_ref') == generation_ref and state.get('lifecycle') == 'READY_UNBOUND'
         and type(state.get('revision')) is int and state['revision'] == 2, 'PM_NATIVE_READY_REV2_REQUIRED')
    need(state.get('source_revision') == source_revision and type(verified_source) is dict
         and verified_source.get('source_revision') == source_revision
         and verified_source.get('method_unchanged') is True
         and verified_source.get('ancestry_verified') is True, 'PM_NATIVE_SOURCE_CONTINUITY_REQUIRED')
    method=state.get('civilization_method_attestation')
    need(type(method) is dict and type(method.get('method_fingerprint')) is str
         and re.fullmatch(r'[0-9a-f]{64}', method['method_fingerprint']) is not None
         and method['method_fingerprint'] == verified_source.get('method_fingerprint'), 'PM_NATIVE_METHOD_MISMATCH')
    receipt=state.get('ready_unbound_receipt')
    need(type(receipt) is dict and receipt.get('receipt_ref') == 'READY_'+generation_ref
         and type(receipt.get('receipt_fingerprint')) is str
         and re.fullmatch(r'[0-9a-f]{64}',receipt['receipt_fingerprint']) is not None,
         'PM_NATIVE_READY_RECEIPT_REQUIRED')
    canaries=state.get('generic_capability_canaries')
    required={'AUTHENTICATED_TRANSITION_SCOPE','CURRENT_SOURCE_AND_METHOD','STATE_CREATE_READBACK'}
    need(type(canaries) is list and len(canaries)==len(required)
         and all(type(c) is dict and c.get('result')=='PASS' for c in canaries)
         and {c.get('canary_ref') for c in canaries}==required, 'PM_NATIVE_CANARIES_REQUIRED')
