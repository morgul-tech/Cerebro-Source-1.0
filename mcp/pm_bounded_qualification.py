"""One-generation PM qualification data validation; never an authority issuer.

Consumed only inside the existing protected PM owner path. A well-formed receipt
is not authenticated evidence: the protected owner original must bind its digest,
and normal source/authority/backend/caller/finite-window gates remain mandatory.
"""
from __future__ import annotations
import copy
import hashlib
import json
import re
from pathlib import Path

GENERATION = 'CEREBRO-BOOT-2872BF170DBF470C86A8A52C7B1179A1'
CHAT = '01a1a700-0a7c-7322-8b78-9cb73953eaa6'
BIRTH_SOURCE = '2e2f75e938fba46bad5fb1cdfaa03c1bd8cc9a4c'
METHOD = 'cd794305005b4a4fd2145386e2e9b7776c838c20898c9e27f9810b1d3b7a11fd'
CHOICE = '1affdd1c4686cdcafd1a1441dbf41a8efb93b6aae27e9e8bca143c9b9a7ba45a'
CONTRACT = 'PM-2872-BOUNDED-SUCCESSION-20261104-01'
HISTORY = {
    'strict_step03_pre_step04': 'UNPROVEN',
    'strict_15_step_order': 'UNPROVEN',
    'strict_step06_fixed12': 'UNPROVEN',
    'strict_bios_terminal_historical': 'UNPROVEN',
    'strict_boot_pass': False,
    'strict_boot_green': False,
    'clean_frozen_order_pass': False,
    'arvetone_exposure': 'EARLY_PARTIAL_METADATA_EXPOSURE_PRESERVED',
}
SHA = re.compile(r'[0-9a-f]{64}\Z')
STAGE_REFS = {'contract_owner_disposition', 'published_contract',
              'candidate_method_consumption', 'Shared_and_diagnostic_readback',
              'current_integrity_disposition', 'R5_technical_qualification', 'affected_contract_review'}
FINAL_REFS = {'actual_native_attach_readback', 'pretransfer_profile_contract_fit',
              'sole_current_transition_disposition', 'open_returns_uptake',
              'arvetone_last_full_consumption', 'own_identity_greeting',
              'bounded_order_disposition'}
SCOPE = ['EXISTING_PORTFOLIO_CURRENTNESS_AND_RETURNS',
         'EXISTING_AUTHORIZED_WORK_COORDINATION',
         'ONLY_ACTUAL_QUALIFIED_PM_PROFILE_EFFECTS']


class QualificationRefused(ValueError):
    pass


def need(ok, reason):
    if not ok:
        raise QualificationRefused(reason)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


def require_issued_contract(qualification):
    """Require legitimately issued exact policy bytes; never execution authority.

    A future rightful publication changes this exact contract status, then its
    exact Git/source/deployment bytes and receipt must be qualified together.
    This check grants nothing; the protected finite owner original still owns
    all decision authority. Load bytes once; do not hash then reopen.
    """
    raw = Path(__file__).with_name('pm-2872-bounded-succession.json').read_bytes()
    policy = json.loads(raw)
    need(type(policy) is dict and policy.get('id') == CONTRACT
         and policy.get('schema') == 'cerebro-candidate-bounded-succession-contract/v1'
         and policy.get('status') == 'ISSUED_BY_LEGITIMATE_CONTRACT_OWNER',
         'BOUNDED_CONTRACT_UNISSUED')
    need(policy.get('selection', {}).get('generation') == GENERATION
         and policy.get('selection', {}).get('Human_qualification_choice_sha256') == CHOICE
         and policy.get('claims', {}).get('strict_boot_pass') is False
         and policy.get('claims', {}).get('clean_frozen_order_pass') is False,
         'BOUNDED_ISSUED_CONTRACT_SCOPE')
    need(qualification['receipts']['published_contract']['sha256']
         == hashlib.sha256(raw).hexdigest(), 'BOUNDED_ISSUED_CONTRACT_PIN')
    return hashlib.sha256(raw).hexdigest()


def refs(value, names):
    need(type(value) is dict and set(value) == names, 'BOUNDED_EXACT_RECEIPT_REFS')
    for name, item in value.items():
        need(type(item) is dict and set(item) == {'ref', 'sha256'}, 'BOUNDED_REF_FIELDS:'+name)
        need(type(item['ref']) is str and 0 < len(item['ref']) <= 512
             and item['ref'] == item['ref'].strip()
             and all(ord(c) >= 32 for c in item['ref']), 'BOUNDED_REF:'+name)
        need(type(item['sha256']) is str and SHA.fullmatch(item['sha256']), 'BOUNDED_HASH:'+name)


def validate_qualification(value, decision):
    fields = {'schema', 'contract_ref', 'Human_choice_sha256', 'generation_ref',
              'candidate_chat', 'birth_source', 'method_fingerprint', 'history',
              'scope', 'receipts', 'authority', 'strict_pass_granted'}
    need(type(value) is dict and set(value) == fields, 'BOUNDED_EXACT_FIELDS')
    need(value['schema'] == 'cerebro-pm-2872-qualification/v1'
         and value['contract_ref'] == CONTRACT and value['Human_choice_sha256'] == CHOICE,
         'BOUNDED_EXACT_CONTRACT')
    need(value['generation_ref'] == decision.get('generation_ref') == GENERATION
         and value['candidate_chat'] == decision.get('candidate_chat') == CHAT,
         'BOUNDED_ONE_CANDIDATE_ONLY')
    need(type(decision.get('expected_revision')) is int and decision['expected_revision'] == 2
         and value['birth_source'] == decision.get('source_revision') == BIRTH_SOURCE
         and value['method_fingerprint'] == decision.get('method_fingerprint') == METHOD,
         'BOUNDED_UNCHANGED_NATIVE_BIRTH_METHOD')
    # JSON bool/int equality must not turn 0 into a truthful False marker.
    need(type(value['history']) is dict and set(value['history']) == set(HISTORY)
         and all(type(value['history'][k]) is type(v) and value['history'][k] == v
                 for k, v in HISTORY.items()), 'BOUNDED_HISTORY_NOT_PASS')
    need(value['scope'] == SCOPE and type(value['scope']) is list,
         'BOUNDED_SCOPE_NO_EXTRA_EFFECTS')
    need(value['authority'] == 'NONE' and value['strict_pass_granted'] is False,
         'BOUNDED_NOT_AUTHORITY_OR_STRICT_PASS')
    refs(value['receipts'], STAGE_REFS)
    return copy.deepcopy(value)


def validate_activation(value):
    fields = {'schema', 'generation_ref', 'qualification_sha256', 'history',
              'receipts', 'scope', 'authority', 'strict_pass_granted',
              'pretransfer_profile_fit'}
    need(type(value) is dict and set(value) == fields
         and value['schema'] == 'cerebro-pm-2872-bounded-activation/v1'
         and value['generation_ref'] == GENERATION, 'BOUNDED_ACTIVATION_FIELDS')
    need(type(value['qualification_sha256']) is str
         and SHA.fullmatch(value['qualification_sha256']), 'BOUNDED_ACTIVATION_HASH')
    need(type(value['history']) is dict and set(value['history']) == set(HISTORY)
         and all(type(value['history'][k]) is type(v) and value['history'][k] == v
                 for k, v in HISTORY.items()), 'BOUNDED_ACTIVATION_HISTORY')
    need(type(value['scope']) is list and value['scope'] == SCOPE
         and value['authority'] == 'NONE' and value['strict_pass_granted'] is False,
         'BOUNDED_ACTIVATION_SCOPE')
    refs(value['receipts'], FINAL_REFS)
    fit = value['pretransfer_profile_fit']
    need(type(fit) is dict and set(fit) == {'schema', 'profile_ref', 'result',
         'operative_profile_pass', 'candidate_is_current'}
         and fit['schema'] == 'cerebro-pm-pretransfer-profile-fit/v1'
         and type(fit['profile_ref']) is str and 0 < len(fit['profile_ref']) <= 256
         and fit['result'] == 'TECHNICAL_FIT_ONLY'
         and fit['operative_profile_pass'] is False and fit['candidate_is_current'] is False,
         'BOUNDED_PRETRANSFER_NOT_OPERATIVE_PASS')
    return copy.deepcopy(value)


def require_activation_matches(value, decision):
    result = validate_activation(value)
    qualification = validate_qualification(decision.get('bounded_qualification'), decision)
    need(result['qualification_sha256'] == digest(qualification), 'BOUNDED_ACTIVATION_STAGED_MISMATCH')
    need(result['pretransfer_profile_fit']['profile_ref'] == decision.get('profile_ref'),
         'BOUNDED_PRETRANSFER_PROFILE_MISMATCH')
    return result
