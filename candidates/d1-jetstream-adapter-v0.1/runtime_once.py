"""D3 EDGE R4: role-factory alignment and actual frozen Context keyset."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from pathlib import Path
import socket
import stat
import sys
import types
import uuid

REF = re.compile(r'\A[A-Za-z0-9][A-Za-z0-9_.:@/#=-]{0,159}\Z')


class Refused(RuntimeError):
    pass


def captured(path, expected):
    """Hash and execute the same snapshot; never importlib-reopen a factory path."""
    with open(path, 'rb') as stream:
        data = stream.read()
    if hashlib.sha256(data).hexdigest() != expected:
        raise Refused('ARTIFACT_DIGEST')
    return data


def interpreter_path(bound):
    alias = Path(bound['path'])
    protected(alias.parent, directory=True)
    # Root-owned venv symlink is legitimate only with explicit admitted final target.
    if alias.is_symlink():
        if os.name == 'posix' and alias.lstat().st_uid != 0:
            raise Refused('INTERPRETER_LINK_CUSTODY')
        resolved = alias.resolve(strict=True)
        if str(resolved) != bound.get('resolved_path'):
            raise Refused('INTERPRETER_TARGET')
        protected(resolved)
        return resolved
    return protected(alias)


def protected(path, *, directory=False):
    path = Path(path)
    if not path.is_absolute():
        raise Refused('ABSOLUTE_PATH')
    for node in (path, *path.parents):
        info = node.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise Refused('SYMLINK_CUSTODY')
        if os.name == 'posix' and (info.st_uid != 0 or info.st_mode & 0o022):
            raise Refused('ROOT_PROTECTED_CUSTODY')
    if directory and not path.is_dir():
        raise Refused('DIRECTORY')
    return path


def atomic_json(path, value):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as file:
            json.dump(value, file, sort_keys=True)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        if os.name == 'posix':
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def cursor(value):
    # Frozen Context list_pending keyset; current A1 production profile still must match.
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {'enqueued_at', 'event_id'}:
        raise Refused('CURSOR_FIELDS')
    if any(not isinstance(v, str) or not 1 <= len(v) <= 160 for v in value.values()):
        raise Refused('CURSOR_VALUES')
    if not re.fullmatch(r'[A-Za-z0-9_:@/#=-]{1,160}', value['event_id']):
        raise Refused('CURSOR_EVENT_ID')
    try:
        timestamp = datetime.fromisoformat(value['enqueued_at'].replace('Z', '+00:00'))
        if timestamp.tzinfo is None:
            raise ValueError('Timezone required')
    except ValueError:
        raise Refused('CURSOR_TIMESTAMP') from None
    return value


@contextmanager
def exclusive(state):
    handle = (state / 'runtime.lock').open('a+b')
    try:
        if os.name == 'posix':
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:  # local Windows test portability, not EDGE qualification
            import msvcrt
            handle.write(b'0'); handle.flush(); handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        yield
    finally:
        handle.close()


def validate(admission_path):
    protected(admission_path)
    admission = json.loads(Path(admission_path).read_text(encoding='utf-8-sig'))
    schema=admission.get('schema')
    if schema not in ('bk11-edge-host-admission/v3','bk11-edge-host-admission/v4') or admission.get('enabled') is not True:
        raise Refused('INERT_OR_SCHEMA')
    if admission.get('host') != socket.gethostname():
        raise Refused('HOST')
    for key in ('a1_production_cut', 'd2_route_custody', 'd4_review', 'service_client_lease_evidence', 'owner_recovery_contract'):
        if not isinstance(admission.get(key), str) or not admission[key].strip():
            raise Refused('MISSING_REFERENCE')
    role = admission.get('role')
    roles = {'publisher': ('cerebro-bk11-pub', '/var/lib/cerebro/bk11/publisher'),
             'receiver': ('cerebro-bk11-recv', '/var/lib/cerebro/bk11/receiver')}
    if role not in roles or admission.get('service_user') != roles[role][0] or admission.get('state_dir') != roles[role][1]:
        raise Refused('ROLE_CUSTODY')
    captured(interpreter_path(admission['interpreter']), admission['interpreter']['sha256'])
    for key in ('runner', 'controller'):
        artifact = admission[key]
        captured(protected(artifact['path']), artifact['sha256'])
    if Path(sys.executable).resolve() != Path(admission['interpreter']['path']).resolve():
        raise Refused('INTERPRETER')
    if Path(__file__).resolve() != Path(admission['runner']['path']).resolve():
        raise Refused('RUNNER')
    if type(admission.get('limit')) is not int or not 1 <= admission['limit'] <= (1 if schema.endswith('/v4') else 64):
        raise Refused('LIMIT')
    state = Path(admission['state_dir'])
    if not state.is_absolute() or not state.is_dir() or state.is_symlink():
        raise Refused('STATE')
    # State is service-owned; all ancestors must be protected root custody.
    protected(state.parent, directory=True)
    if os.name == 'posix':
        info = state.stat()
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise Refused('STATE_SERVICE_CUSTODY')
    if schema.endswith('/v4'):
        plan=admission.get('family_plan')
        if not isinstance(plan,dict) or set(plan)!={'schema','session_ref','session_binding_id','runtime_config_sha256','families'}:
            raise Refused('FAMILY_PLAN_FIELDS')
        if plan.get('schema')!='bk11-edge-family-plan/v1' or not isinstance(plan.get('session_ref'),str) or not REF.fullmatch(plan['session_ref']):
            raise Refused('FAMILY_PLAN_SESSION')
        if not isinstance(plan.get('session_binding_id'),str) or not REF.fullmatch(plan['session_binding_id']):
            raise Refused('FAMILY_PLAN_SESSION_BINDING')
        if not re.fullmatch(r'[0-9a-f]{64}',str(plan.get('runtime_config_sha256',''))):
            raise Refused('FAMILY_PLAN_CONFIG_PIN')
        families=plan.get('families')
        if not isinstance(families,dict) or set(families)!= {'PM_OWNER','ROOM_RETURN'}:
            raise Refused('EXACT_TWO_FAMILY_PLAN_REQUIRED')
        for family,item in families.items():
            required={'owner_family','role','assignment_ref','session_binding_id','factory_sha256','interpreter_sha256'}
            if not isinstance(item,dict) or set(item)!=required or item.get('owner_family')!=family or item.get('role')!=role:
                raise Refused('FAMILY_PROFILE_FIELDS')
            if not isinstance(item.get('assignment_ref'),str) or not REF.fullmatch(item['assignment_ref']):
                raise Refused('FAMILY_ASSIGNMENT_REF')
            if item.get('session_binding_id')!=plan['session_binding_id']:
                raise Refused('FAMILY_SESSION_BINDING_MISMATCH')
            if item.get('factory_sha256')!=admission['factory']['sha256'] or item.get('interpreter_sha256')!=admission['interpreter']['sha256']:
                raise Refused('FAMILY_ARTIFACT_PIN_MISMATCH')
        child=state/'room-return'
        if child.is_symlink() or not child.is_dir():raise Refused('ROOM_RETURN_STATE_CHILD')
        if os.name=='posix':
            child_info=child.stat()
            if child_info.st_uid!=os.geteuid() or stat.S_IMODE(child_info.st_mode)!=0o700:
                raise Refused('ROOM_RETURN_STATE_CUSTODY')
    factory = admission['factory']
    data = captured(protected(factory['path']), factory['sha256'])
    return admission, state, data


def execute(admission_path, action):
    admission, state, factory_bytes = validate(admission_path)
    if admission.get('schema')=='bk11-edge-host-admission/v4':
        return execute_family_plan(admission,state,factory_bytes,action)
    role = admission['role']
    if (role == 'receiver' and action == 'publish') or (role == 'publisher' and action == 'receive'):
        raise Refused('CROSS_ROLE_ACTION')
    with exclusive(state):
        fence_path = state / 'need-recovery.json'
        previous = json.loads(fence_path.read_text()) if fence_path.exists() else None
        if previous and action not in ('recover', 'normaldrift'):
            raise Refused('RECONCILE_ORIGINAL_OPERATION_FIRST')
        obligation = previous or {'operation_id': uuid.uuid4().hex, 'action': action}
        # Durable before compile, factory execution, open_runtime or any external call.
        atomic_json(fence_path, obligation)
        receipt = {'schema': 'bk11-edge-host-operation/v3', 'action': action, 'role': role,
                   'operation_id': obligation['operation_id'], 'time': datetime.now(timezone.utc).isoformat(),
                   'downstream_work': 'NOT_ATTESTED', 'result': 'UNKNOWN_OUTCOME_RETAINED'}
        runtime = None
        clear = False
        code = 2
        try:
            module = types.ModuleType('bk11_admitted_snapshot')
            module.__file__ = admission['factory']['path']
            exec(compile(factory_bytes, module.__file__, 'exec'), module.__dict__)
            runtime = module.open_runtime(admission)
            cursor_path = state / ((('recover' if action == 'normaldrift' else action)) + '-cursor.json')
            after = cursor(json.loads(cursor_path.read_text())) if cursor_path.exists() else None
            if action in ('recover', 'normaldrift'):
                result = runtime.recover_pending_once(limit=admission['limit'], after=after,
                                                     reconcile_operation=obligation)
            elif action == 'publish':
                result = runtime.publish_pending_once(limit=admission['limit'], after=after,
                                                      operation_id=obligation['operation_id'])
            elif action == 'receive':
                result = runtime.drain_once(limit=admission['limit'], operation_id=obligation['operation_id'])
            else:
                raise Refused('ACTION')
            if not isinstance(result, dict) or type(result.get('completed')) is not bool:
                raise Refused('RESULT')
            if action in ('recover', 'normaldrift'):
                clear = (result['completed'] and result.get('publication_reconciled') is True and
                         result.get('reconciled_operation_id') == obligation['operation_id'])
            else:
                clear = result['completed']
            if result['completed']:
                atomic_json(cursor_path, cursor(result.get('next_cursor')))
            if action == 'normaldrift' and clear and role == 'receiver':
                # New backend attempt gets its own persistent fence before entering drain.
                receive = {'operation_id': uuid.uuid4().hex, 'action': 'receive'}
                atomic_json(fence_path, receive)
                receipt['receive_operation_id'] = receive['operation_id']
                clear = False
                result = runtime.drain_once(limit=admission['limit'], operation_id=receive['operation_id'])
                if not isinstance(result, dict) or type(result.get('completed')) is not bool:
                    raise Refused('RECEIVE_RESULT')
                clear = result['completed']
            elif action == 'normaldrift' and clear and role == 'publisher':
                publication = {'operation_id': uuid.uuid4().hex, 'action': 'publish'}
                atomic_json(fence_path, publication)
                receipt['publish_operation_id'] = publication['operation_id']
                clear = False
                publish_cursor = state / 'publish-cursor.json'
                publish_after = cursor(json.loads(publish_cursor.read_text())) if publish_cursor.exists() else None
                result = runtime.publish_pending_once(limit=admission['limit'], after=publish_after,
                                                      operation_id=publication['operation_id'])
                if not isinstance(result, dict) or type(result.get('completed')) is not bool:
                    raise Refused('PUBLISH_RESULT')
                if result['completed']:
                    atomic_json(publish_cursor, cursor(result.get('next_cursor')))
                clear = result['completed']
            if clear:
                receipt['result'] = 'BOUNDED_OPERATION_COMPLETED'
                code = 0
        except Exception as error:
            receipt['error_class'] = type(error).__name__  # never exception text/credentials
            clear = False
        finally:
            if runtime is not None:
                try:
                    runtime.close()
                except Exception as error:
                    receipt.update(close_error_class=type(error).__name__, result='UNKNOWN_OUTCOME_RETAINED')
                    clear, code = False, 2
            # Fence cleared only after durable sanitized receipt and successful close.
            atomic_json(state / ('receipt-' + uuid.uuid4().hex + '.json'), receipt)
            if clear:
                fence_path.unlink()
                if os.name == 'posix':
                    descriptor = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
                    try: os.fsync(descriptor)
                    finally: os.close(descriptor)
        return code


def _family_after(state,kind):
    path=state/(kind+'-cursor.json')
    if not path.exists():return None
    value=cursor(json.loads(path.read_text(encoding='utf-8')))
    if value is None:return None
    return {'schema':'bk11-owner-route-keyset/v1',**value}


def _host_cursor(value):
    if value is None:return None
    if not isinstance(value,dict):raise Refused('FAMILY_CURSOR_SHAPE')
    if set(value)=={'schema','enqueued_at','event_id'}:
        if value['schema']!='bk11-owner-route-keyset/v1':raise Refused('FAMILY_CURSOR_SCHEMA')
        value={k:value[k] for k in ('enqueued_at','event_id')}
    return cursor(value)


def _family_recovery_target(prior, family):
    legacy_fields={'operation_id','action'}
    legacy_record=family=='PM_OWNER' and isinstance(prior,dict) and set(prior)==legacy_fields
    if legacy_record:
        original=prior['operation_id'];original_action=prior['action']
        if original_action not in ('publish','receive','recover','normaldrift'):
            raise Refused('FAMILY_RECOVERY_LEGACY_ACTION_INVALID')
    else:
        required={'schema','owner_family','operation_id','action','reconcile_operation_id'}
        allowed=required|{'reconcile_operation_action','reconcile_attempt_ref'}
        if (not isinstance(prior,dict) or not required<=set(prior) or not set(prior)<=allowed
            or prior.get('schema')!='bk11-family-operation-fence/v1' or prior.get('owner_family')!=family
            or prior.get('action') not in ('publish','receive','recover','normaldrift')):
            raise Refused('FAMILY_RECOVERY_FENCE_INVALID')
        current_attempt=prior.get('operation_id')
        if not isinstance(current_attempt,str) or re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}',current_attempt) is None:
            raise Refused('FAMILY_RECOVERY_TARGET_INVALID')
        recorded=prior.get('reconcile_operation_id')
        if recorded is None:
            original=current_attempt
            original_action=None
        else:
            original=recorded
            original_action=prior.get('reconcile_operation_action')
            if original_action not in (None,'publish','receive','recover','normaldrift'):
                raise Refused('FAMILY_RECOVERY_LEGACY_ACTION_INVALID')
    if not isinstance(original,str) or re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}',original) is None:
        raise Refused('FAMILY_RECOVERY_TARGET_INVALID')
    attempt_ref=prior.get('reconcile_attempt_ref') if not legacy_record else None
    if legacy_record or (attempt_ref is None and original_action is not None):
        attempt_ref='bk11attempt:'+hashlib.sha256(original.encode('utf-8')).hexdigest()
    if attempt_ref is not None and (not isinstance(attempt_ref,str)
        or re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}',attempt_ref) is None):
        raise Refused('FAMILY_RECOVERY_ATTEMPT_REF_INVALID')
    return {'operation_id':original,'action':original_action,'attempt_ref':attempt_ref}


DIAG_STAGES=frozenset({'UNSPECIFIED','TOKEN_RESOLVE','BUILD_REQUEST','HTTP_OPEN','HTTP_READ','JSON_PARSE','RPC_ENVELOPE','RESULT_CONTRACT'})
DIAG_REASONS=frozenset({'QUALIFIED_CONTEXT_CUSTODY_INPUT_REQUIRED','CONTEXT_CALL_TIMEOUT_INVALID','PRIVATE_ISSUER_TOKEN_UNAVAILABLE','CONTEXT_RESPONSE_BOUND','CONTEXT_OPERATION_REFUSED','CONTEXT_SERVICE_CALL_UNAVAILABLE'})
DIAG_TOOLS=frozenset({'UNSPECIFIED','open_bk11_service_runtime_v1','verify_bk11_service_lease_v1','control_bk11_service_attempt_v1','discover_bk11_backend_obligations_v1','reconcile_bk11_backend_obligation_v1','accept_bk11_backend_delivery_v1','read_bk11_backend_disposition_v1','close_bk11_service_runtime_v1','read_room_return_d1_outbox_v1','settle_room_return_d1_outbox_v1','read_pm_owner_d1_outbox_v1','settle_pm_owner_d1_outbox_v1'})
TOKEN_REASON_CODES=frozenset(['ACTUAL_INSTALLATION_EPOCH_REQUIRED', 'ACTUAL_ISSUER_TTL_UNQUALIFIED', 'ACTUAL_JWT_EXPIRY_BASIS_REQUIRED', 'CACHED_TOKEN_BINDING_REQUALIFICATION_REQUIRED', 'CLOCK_BASIS_INVALID', 'CLOCK_ROLLBACK_REFUSED', 'CORRUPT_OR_FUTURE_TOKEN_WINDOW', 'CORRUPT_PROFILE_HOLD', 'CORRUPT_TOKEN_WINDOW', 'CREDENTIAL_REF_ROTATION_EPOCH_REQUIRED', 'DURABLE_ATTEMPT_LEDGER_INVALID', 'DURABLE_BINDING_COMMIT_REQUIRED_OWNER_HOLD', 'DURABLE_BUDGET_FIELDS_INVALID', 'EXACT_APPROVED_ROLE_CLIENT_REQUIRED', 'EXACT_ISSUER_AUDIENCE_REQUIRED', 'EXISTING_BUDGET_MUST_NOT_BE_REINITIALIZED', 'EXISTING_LOCK_REQUIRED', 'FOREIGN_ISSUER_CLIENT_AUDIENCE_METADATA', 'FOREIGN_OR_CORRUPT_BINDING_STATE', 'FOREIGN_OR_CORRUPT_ROLE_STATE', 'FOREIGN_REF_OR_UNQUALIFIED_BINDING_CHANGE', 'FUTURE_ATTEMPT_LEDGER_REFUSED', 'FUTURE_OR_INVALID_ISSUER_TIME', 'HOST_BOOT_CLOCK_REQUALIFICATION_REQUIRED', 'HOST_CLOCK_BOOT_BASIS_REQUIRED', 'INITIAL_EFFECTIVE_VALIDITY_BELOW_12H', 'INSTALLATION_EPOCH_ROLLBACK_REFUSED', 'INSTALLED_EXACT_ROLE_STATE_PATH_REQUIRED', 'INSTALLED_TOKEN_LIFECYCLE_PROFILE_REQUIRED', 'INSTALLER_INITIALIZED_DURABLE_ROLE_BUDGET_REQUIRED', 'ISSUER_ACCESS_TOKEN_REQUIRED', 'ISSUER_BEARER_RESPONSE_REQUIRED', 'LINUX_SEARCH_ONLY_ANCESTRY_REQUIRED', 'NO_USABLE_EXISTING_TOKEN_NO_ACQUISITION', 'PERSISTENT_ACQUISITION_COOLDOWN', 'PRIVATE_REGULAR_SINGLE_LINK_0600_REQUIRED', 'PRIVATE_STATE_DUPLICATE_KEY', 'PRIVATE_STATE_NONFINITE', 'PRIVATE_STATE_REPLACED', 'PRIVATE_STATE_SIZE', 'PRIVATE_WRITE_STALLED', 'QUALIFIED_INITIAL_VALIDITY_PROFILE_REQUIRED', 'QUALIFIED_LINUX_OPATH_ROLE_REQUIRED', 'REQUEST_CLOCK_BASIS_INVALID', 'ROLE_ANCESTRY_CUSTODY_REQUIRED', 'ROLE_DIRECTORY_0700_REQUIRED', 'ROLE_DIRECTORY_REPLACED', 'ROLE_LOCK_REPLACED', 'ROLE_PATH_COMPONENT_INVALID', 'ROLE_SINGLEFLIGHT_TIMEOUT', 'ROLE_STORE_ALREADY_OPEN', 'ROLLING_31_DAY_ATTEMPT_ALLOWANCE_EXHAUSTED', 'TOKEN_METADATA_LOCAL_READ_UNAVAILABLE', 'TOKEN_REF_INVALID', 'UNEXPECTED_CLOCK_JUMP_REFUSED', 'UNKNOWN_ISSUER_OUTCOME_ATTEMPT_RETAINED', 'UNKNOWN_TOKEN_FAILURE'])
TOKEN_STAGES=frozenset(['ACQUISITION_GUARDS', 'ACQUISITION_INTENT_COMMIT', 'BINDING_VALIDATE', 'CACHE_COMMIT', 'CACHE_VALIDATE', 'CLOCK_VALIDATE', 'EPOCH_COMMIT', 'ISSUER_CALLBACK_ENTER', 'ISSUER_OPEN', 'ISSUER_PARSE', 'ISSUER_READ', 'ISSUER_REQUEST_BUILD', 'LOCAL_BINDING', 'POST_ISSUER_RESPONSE', 'POST_ISSUER_VALIDATE', 'ROLE_STATE_READ', 'STATE_LOAD', 'STATE_VALIDATE', 'TOKEN_COMMIT', 'TOKEN_LIFECYCLE'])
def safe_token_diagnostic(value):
    if type(value)is not dict or set(value)!={'schema','stage','reason_code','http_status'}:return None
    if any(type(value[k])is not str for k in ('schema','stage','reason_code')):return None
    if value['schema']!='bk11-token-custody-diagnostic/v1' or value['stage'] not in TOKEN_STAGES or value['reason_code'] not in TOKEN_REASON_CODES:return None
    status=value['http_status']
    if status is not None and (type(status)is not int or not 100<=status<=599):return None
    return dict(value)

def safe_context_diagnostic(value):
    if type(value) is not dict or set(value) not in ({'schema','stage','operation','owner_family','reason_code','http_status'},{'schema','stage','operation','owner_family','reason_code','http_status','token_diagnostic'}) or value.get('schema')!='bk11-context-call-diagnostic/v1':return None
    if any(type(value[k])is not str for k in ('schema','stage','operation','owner_family','reason_code')):return None
    if value['stage'] not in DIAG_STAGES or value['operation'] not in DIAG_TOOLS or value['reason_code'] not in DIAG_REASONS:return None
    if value['owner_family'] not in ('UNSPECIFIED','PM_OWNER','ROOM_RETURN'):return None
    status=value['http_status']
    if status is not None and (type(status)is not int or not 100<=status<=599):return None
    if 'token_diagnostic' in value and (value['stage']!='TOKEN_RESOLVE' or safe_token_diagnostic(value['token_diagnostic']) is None):return None
    return dict(value)


def context_diagnostic_chain(error):
    # Preserve a safe inner primary diagnostic if a finally/close error masks it.
    # This is exception-chain evidence, not a claim of complete execution trace.
    values=[];seen=set()
    for _ in range(4):
        if error is None or id(error) in seen:break
        seen.add(id(error))
        value=safe_context_diagnostic(getattr(error,'safe_diagnostic',None))
        if value is not None:values.append(value)
        error=getattr(error,'__context__',None)
    return (values[-1] if values else None),values[:-1]


import re
FIELDS = {'schema', 'role', 'owner_family', 'stage', 'operation', 'identities', 'reason_code'}
REASONS = frozenset(('BOUNDED_EXISTING_CALL_REQUIRED', 'UNSELECTED_TOOL', 'EXACT_OWNER_FAMILY', 'EXACT_ASSIGNED_ROLE_REQUIRED', 'ACTUAL_OWN_CURRENT_LEASE_REQUIRED', 'NO_BROAD_OWNER_READ', 'RECOVERY_ONLY_PHASE', 'EXACT_SELECTED_OBLIGATIONS', 'EXACT_SELECTED_OBLIGATION', 'ACTUAL_OWN_CURRENT_ATTEMPT_REQUIRED', 'EXACT_PRIOR_ATTEMPT_REQUIRED', 'NO_UNSELECTED_DISCOVERY', 'NO_CLOSE_RETRY', 'ACTUAL_LEASE_RESULT_REQUIRED', 'COMMITTED_LEASE_CLOSE_REQUIRED', 'EXACT_RECOVERY_ONLY', 'CANONICAL_CUSTODY_RESULT_REQUIRED', 'DURABLE_RECEIVER_OBSERVATION_REQUIRED', 'DURABLE_CUSTODY_MATCH_REQUIRED', 'ORIGINAL_DUTY_RECONCILIATION_PENDING', 'EXACT_ORIGINAL_PROOFS_REQUIRED', 'OWN_LEASE_CLOSE_UNCONFIRMED', 'NORMAL_BOUND_REQUIRED', 'UNSELECTED_EXPIRED_INTENT', 'EXPIRED_INTENT_CUSTODY_NOT_PROVEN', 'EXACT_CURRENT_ROW_KEYSET_REQUIRED', 'EXPLICIT_RECOVERY_SELECTION_REQUIRED', 'NO_RECOVERY_BROKER_EFFECT', 'CURRENT_RUNTIME_REQUIRED', 'NO_EXPIRED_PUBLICATION', 'CONTEXT_CALL_FAILURE'))
STAGES = frozenset(('BEFORE_CALL', 'CONTEXT_CALL', 'RESULT_CONTRACT', 'ORIGINAL_RECONCILE', 'LEASE_CLOSE', 'BROKER_BOUNDARY', 'NORMAL_EXPIRED_CUSTODY'))
OPERATIONS = frozenset(('open_bk11_service_runtime_v1', 'verify_bk11_service_lease_v1', 'control_bk11_service_attempt_v1', 'discover_bk11_backend_obligations_v1', 'reconcile_bk11_backend_obligation_v1', 'accept_bk11_backend_delivery_v1', 'read_bk11_backend_disposition_v1', 'close_bk11_service_runtime_v1', 'read_pm_owner_d1_outbox_v1', 'read_room_return_d1_outbox_v1', 'settle_pm_owner_d1_outbox_v1', 'settle_room_return_d1_outbox_v1', 'publish', 'fetch_one', 'ack', 'nak', 'UNSPECIFIED'))
MID = re.compile('\\Ad1:[a-f0-9]{40}\\Z')
HASH = re.compile('\\A[a-f0-9]{64}\\Z')
def safe_scope_diagnostic(value):
    if type(value) is not dict or set(value) != FIELDS:
        return None
    if any((type(value[k]) is not str for k in ('schema', 'role', 'owner_family', 'stage', 'operation', 'reason_code'))):
        return None
    if value['schema'] != 'bk11-scoped-operation-diagnostic/v1' or value['role'] not in ('publisher', 'receiver'):
        return None
    if value['owner_family'] not in ('PM_OWNER', 'ROOM_RETURN', 'UNSPECIFIED'):
        return None
    if value['stage'] not in STAGES or value['operation'] not in OPERATIONS:
        return None
    if value['reason_code'] not in REASONS:
        return None
    identities = value['identities']
    if type(identities) is not list or len(identities) > 3:
        return None
    for item in identities:
        if type(item) is not dict or set(item) != {'message_id', 'pointer_sha256'}:
            return None
        if type(item['message_id']) is not str or type(item['pointer_sha256']) is not str:
            return None
        if MID.fullmatch(item['message_id']) is None or HASH.fullmatch(item['pointer_sha256']) is None:
            return None
    return {**value, 'identities': [dict(item) for item in identities]}

def scope_diagnostic_chain(error):
    values=[];seen=set();pending=[error]
    while pending and len(seen)<8:
        current=pending.pop(0)
        if current is None or id(current) in seen:continue
        seen.add(id(current))
        value=safe_scope_diagnostic(getattr(current,'safe_scope_diagnostic',None))
        if value is not None and value not in values:values.append(value)
        pending.extend([getattr(current,'secondary_close_error',None),getattr(current,'__cause__',None),getattr(current,'__context__',None)])
    return values


def _prior_lineage(prior,family):
    """Retain original AND intermediate interrupted recovery attempts, without guessing outcomes."""
    if prior is None:return []
    if type(prior)is not dict:raise Refused('FAMILY_RECOVERY_FENCE_INVALID')
    values=[]
    if prior.get('schema')=='bk11-family-operation-fence/v2':
        required={'schema','owner_family','operation_id','action','reconcile_operation_id',
                  'reconcile_operation_action','reconcile_attempt_ref','pending_attempt_refs'}
        if set(prior)!=required or prior['owner_family']!=family or prior['action'] not in ('publish','receive','recover','normaldrift'):
            raise Refused('FAMILY_RECOVERY_FENCE_INVALID')
        values=prior['pending_attempt_refs']
        if type(values)is not list or len(values)>127:raise Refused('BOUNDED_PRIOR_LINEAGE_REQUIRED')
        values=list(values)
    else:
        target=_family_recovery_target(prior,family)
        if target['attempt_ref'] is not None:values.append(target['attempt_ref'])
        elif target['operation_id'] is not None:
            values.append('a:'+hashlib.sha256((target['operation_id']+':'+family).encode()).hexdigest()[:32])
    current=prior.get('operation_id')
    if type(current)is not str or REF.fullmatch(current) is None:raise Refused('FAMILY_RECOVERY_TARGET_INVALID')
    values.append('a:'+hashlib.sha256((current+':'+family).encode()).hexdigest()[:32])
    # Preserve the predecessor parser's exact reference, including legacy
    # bk11attempt:<64hex>. Syntax is the existing bounded server reference
    # contract, not proof of old/current assignment or permission to reconcile.
    # The actual authenticated server must prove every exact target separately.
    if any(type(v)is not str or REF.fullmatch(v) is None for v in values):
        raise Refused('PRIOR_ATTEMPT_REF_REQUIRED')
    return list(dict.fromkeys(values))


def execute_family_plan(admission,role_state,factory_bytes,action):
    role=admission['role']
    if (role=='receiver' and action=='publish') or (role=='publisher' and action=='receive'):
        raise Refused('CROSS_ROLE_ACTION')
    family_states={'PM_OWNER':role_state,'ROOM_RETURN':role_state/'room-return'}
    hold_path=role_state/'family-ingress-hold.json'
    with exclusive(role_state):
        # An ambiguous/cross-family ingress stays held for owner disposition; never
        # turn a retained delivery into another pull/ACK loop on the next timer tick.
        if hold_path.exists():return 2
        previous={}
        for family,state in family_states.items():
            path=state/'need-recovery.json'
            previous[family]=json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
        if any(previous.values()) and action not in ('recover','normaldrift'):
            raise Refused('RECONCILE_ORIGINAL_FAMILY_OPERATION_FIRST')
        operation_ids={family:uuid.uuid4().hex for family in family_states}
        lineage={family:_prior_lineage(previous[family],family) for family in family_states}
        prior_ids={};prior_actions={};prior_attempt_refs={}
        for family in family_states:
            prior=previous[family]
            if prior is None:
                prior_ids[family]=None;prior_actions[family]=None;prior_attempt_refs[family]=None
                continue
            target=({'operation_id':prior['operation_id'],'action':prior['action'],'attempt_ref':None}
                    if prior.get('schema')=='bk11-family-operation-fence/v2'
                    else _family_recovery_target(prior,family))
            prior_ids[family]=target['operation_id'];prior_actions[family]=target['action']
            prior_attempt_refs[family]=target['attempt_ref']
        for family,state in family_states.items():
            atomic_json(state/'need-recovery.json',{'schema':'bk11-family-operation-fence/v2',
              'owner_family':family,'operation_id':operation_ids[family],'action':action,
              'reconcile_operation_id':prior_ids[family],
              'reconcile_operation_action':prior_actions[family],
              'reconcile_attempt_ref':prior_attempt_refs[family],
              'pending_attempt_refs':lineage[family]})
        receipt={'schema':'bk11-edge-family-operation/v1','role':role,'action':action,
          'operation_ids':operation_ids,'result':'UNKNOWN_OUTCOME_RETAINED',
          'downstream_work':'PENDING_NOT_MODEL_EXECUTED'}
        runtime=None;result=None;clear=False;code=2;close_failed=False;pending_cursors={}
        try:
            module=types.ModuleType('bk11_admitted_snapshot')
            module.__file__=admission['factory']['path']
            sys.path.insert(0,str(Path(module.__file__).resolve().parent.parent))
            exec(compile(factory_bytes,module.__file__,'exec'),module.__dict__)
            runtime=module.open_runtime(admission,operation_id=operation_ids)
            reconcile={f:prior_ids[f] for f in family_states if prior_ids[f] is not None}
            recover_after={f:_family_after(family_states[f],'recover') for f in family_states}
            publish_after={f:_family_after(family_states[f],'publish') for f in family_states}
            recovery=None
            perform_recovery=action=='recover' or (action=='normaldrift' and any(prior_ids.values()))
            if perform_recovery:
                reconcile_attempt_refs={f:prior_attempt_refs[f] for f in family_states
                                        if prior_attempt_refs[f] is not None}
                recovery=runtime.reconcile_prior_once(targets=lineage,operation_id=operation_ids)
                receipt['recovery_completed']=recovery.get('completed') is True
                if recovery.get('next_by_family'):
                    for family,value in recovery['next_by_family'].items():
                        pending_cursors[family_states[family]/'recover-cursor.json']=_host_cursor(value)
                recovery_ok=recovery.get('completed') is True
                results=recovery.get('family_results',{})
                for family in family_states:
                    if prior_ids[family] is not None:
                        family_result=results.get(family,{})
                        if family_result.get('publication_reconciled') is not True:recovery_ok=False
                if action in ('recover','normaldrift'):
                    result=recovery;clear=recovery_ok
            elif action in ('normaldrift','receive') and role=='receiver':
                def persist_ingress(marker):
                    if not isinstance(marker,dict) or marker.get('schema')!='bk11-family-ingress-handoff/v1':
                        raise Refused('FAMILY_INGRESS_MARKER_INVALID')
                    atomic_json(hold_path,marker)
                progress={};progress_calls=0
                progress_path=role_state/('family-progress-'+operation_ids['PM_OWNER']+'.json')
                def persist_family_progress(family,phase,value):
                    nonlocal progress_calls
                    if family not in family_states or phase not in (
                        'CUSTODY_OBSERVED_OWN_CLOSE_PENDING','OPERATION_OBSERVED_OWN_CLOSE_PENDING','OWN_CLOSE_CONFIRMED'):
                        raise Refused('FAMILY_PROGRESS_SHAPE')
                    progress_calls+=1
                    if progress_calls>6:raise Refused('FAMILY_PROGRESS_BOUND')
                    copied=json.loads(json.dumps(value,allow_nan=False))
                    if len(json.dumps(copied).encode())>32768:raise Refused('FAMILY_PROGRESS_BOUND')
                    progress[family]={'phase':phase,'value':copied}
                    # This exact current operation checkpoint is durable before
                    # another family effect. It never clears fences or cursors.
                    atomic_json(progress_path,{'schema':'bk11-edge-family-progress/v1',
                        'operation_ids':operation_ids,'family_progress':progress})
                    receipt['family_progress']=json.loads(json.dumps(progress))
                    receipt['family_progress_path']=str(progress_path)
                result=runtime.drain_once(limit=1,operation_id=operation_ids,on_family_selection=persist_ingress,
                                          on_family_progress=persist_family_progress)
                clear=result.get('completed') is True
            elif action in ('normaldrift','publish') and role=='publisher':
                result=runtime.publish_pending_once(limit=1,after=publish_after,operation_id=operation_ids)
                if result.get('next_by_family'):
                    for family,value in result['next_by_family'].items():
                        pending_cursors[family_states[family]/'publish-cursor.json']=_host_cursor(value)
                clear=result.get('completed') is True
            else:raise Refused('ACTION_OR_ROLE')
            receipt['family_results']=result.get('family_results') if isinstance(result,dict) else None
            receipt['result']='BOUNDED_OPERATION_COMPLETED' if clear else 'UNKNOWN_OUTCOME_RETAINED'
            if clear:code=0
        except Exception as error:
            receipt['error_class']=type(error).__name__
            diagnostic,secondary=context_diagnostic_chain(error)
            if diagnostic is not None:receipt['context_diagnostic']=diagnostic
            if secondary:receipt['context_secondary_diagnostics']=secondary
            scope_values=scope_diagnostic_chain(error)
            if scope_values:receipt['scope_diagnostics']=scope_values
            clear=False
        finally:
            if runtime is not None:
                try:runtime.close()
                except Exception as error:
                    receipt.update(close_error_class=type(error).__name__,result='UNKNOWN_OUTCOME_RETAINED')
                    clear,close_failed,code=False,True,2
            receipt['cursor_progress']=[]
            if clear and not close_failed:
                try:
                    for path,value in pending_cursors.items():
                        before=path.read_bytes() if path.exists() else None
                        event={'path':str(path),'before_sha256':hashlib.sha256(before).hexdigest() if before is not None else None,
                               'selected_after':value,'write_confirmed':False}
                        receipt['cursor_progress'].append(event)
                        atomic_json(path,value)
                        after=path.read_bytes()
                        if json.loads(after)!=value:raise Refused('CURSOR_READBACK_REQUIRED')
                        event.update(write_confirmed=True,after_sha256=hashlib.sha256(after).hexdigest())
                except Exception as error:
                    receipt.update(cursor_error_class=type(error).__name__,result='UNKNOWN_OUTCOME_RETAINED')
                    clear,code=False,2
            for family,state in family_states.items():
                family_result=(result or {}).get('family_results',{}).get(family,{}) if isinstance(result,dict) else {}
                atomic_json(state/('receipt-'+operation_ids[family]+'.json'),{
                  'schema':'bk11-edge-family-receipt/v1','owner_family':family,'role':role,
                  'operation_id':operation_ids[family],'action':action,
                  'result':family_result.get('completed') is True if family_result else clear,
                  'downstream_work':'PENDING_NOT_MODEL_EXECUTED',
                  'error_class':receipt.get('error_class'),
                  'context_diagnostic':receipt.get('context_diagnostic'),
                  'context_failure_attribution':('MATCHING_FAMILY_CALL' if (receipt.get('context_diagnostic') or {}).get('owner_family')==family else 'SHARED_ROLE_ERROR_NOT_FAMILY_EXECUTION_PROOF'),
                  'context_secondary_diagnostics':receipt.get('context_secondary_diagnostics',[]),
                  'scope_diagnostics':receipt.get('scope_diagnostics',[]),
                  'scope_attribution':'ROLE_CHAIN_NOT_INDEPENDENT_FAMILY_EXECUTION_PROOF',
                  'family_result':family_result})
            atomic_json(role_state/('receipt-'+uuid.uuid4().hex+'.json'),receipt)
            atomic_json(role_state/'last-operation.json',receipt)
            if clear and not close_failed:
                if hold_path.exists():hold_path.unlink()
                for state in family_states.values():
                    fence=state/'need-recovery.json'
                    if fence.exists():fence.unlink()
                    if os.name=='posix':
                        descriptor=os.open(state,os.O_RDONLY|os.O_DIRECTORY)
                        try:os.fsync(descriptor)
                        finally:os.close(descriptor)
        return code


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--admission', required=True)
    parser.add_argument('--action', choices=('publish', 'receive', 'recover', 'normaldrift'), required=True)
    args = parser.parse_args()
    try:
        sys.exit(execute(args.admission, args.action))
    except Exception as error:
        print(json.dumps({'result': 'REFUSED_BEFORE_RUNTIME', 'error_class': type(error).__name__}))
        sys.exit(3)
