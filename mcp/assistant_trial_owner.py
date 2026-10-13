"""One constructor-bound ASSISTANT trial. No file/env reader, key, grant or default.

The installation owner must supply a protected admission reader whose hold_current
serializes profile changes/revocation through the handler's attach/readback. This
module verifies that explicit contract; it does not attest a real lock/installation.
"""
from __future__ import annotations
import copy,math,re,sys
from contextlib import contextmanager, ExitStack
from contextvars import ContextVar


class AssistantTrialOwnerRefused(RuntimeError):
    pass


def need(value, code):
    if not value:
        raise AssistantTrialOwnerRefused(code)


def opaque(value):
    try:
        return (type(value) is str and bool(value) and value == value.strip()
                and len(value.encode('utf-8')) <= 512
                and not any(ord(c) < 32 or ord(c) == 127 for c in value))
    except UnicodeError:
        return False


class AssistantTrialOwnerResolver:
    """Reader/clock are host constructor dependencies, never caller tool fields."""
    def __init__(self, *, profile_ref, admission_reader, clock):
        need(opaque(profile_ref), 'assistant-trial-profile-ref-invalid')
        need(callable(getattr(admission_reader, 'hold_current', None))
             and callable(getattr(admission_reader, 'assert_current', None))
             and callable(clock), 'assistant-trial-real-owner-dependencies-required')
        self._ref = profile_ref
        self._reader = admission_reader
        self._clock = clock
        self._active = ContextVar('assistant_trial_owner_window', default=None)

    def _validate_profile(self, profile):
        fields = {'schema','profile_ref','owner_ref','owner_revision','enabled',
                  'trial_scope','caller','generation_ref','source_revision',
                  'method_fingerprint','expected_revision','decision_ref','overlay_ref',
                  'actor_ref','actor_generation_ref','recipient_ref','trial_target_ref',
                  'not_before','not_after'}
        need(type(profile) is dict and set(profile) == fields,
             'assistant-trial-profile-fields-invalid')
        need(profile['schema'] == 'cerebro-isolated-assistant-trial-profile/v1'
             and profile['profile_ref'] == self._ref and profile['enabled'] is True
             and profile['trial_scope'] == 'BUILD7_SINGLE_ISOLATED_ASSISTANT'
             and profile['recipient_ref'] == 'A1', 'assistant-trial-not-admitted')
        for key in ('owner_ref','owner_revision','decision_ref','overlay_ref','actor_ref',
                    'actor_generation_ref','trial_target_ref'):
            need(opaque(profile[key]), 'assistant-trial-opaque-field-invalid')
        need(re.fullmatch(r'[A-Z][A-Z0-9_]{0,31}', profile['trial_target_ref']) is not None,
             'assistant-trial-target-format-invalid')
        caller = profile['caller']
        need(type(caller) is dict and set(caller) == {'tenant_ref','workspace_ref',
             'principal_ref','consumer_ref','session_ref'} and all(opaque(v) for v in caller.values()),
             'assistant-trial-caller-fields-invalid')
        need(type(profile['generation_ref']) is str and
             re.fullmatch(r'CEREBRO-BOOT-[A-Z0-9-]{8,96}', profile['generation_ref']) is not None,
             'assistant-trial-generation-invalid')
        for key, pattern in (('source_revision', r'[0-9a-f]{40}'),
                             ('method_fingerprint', r'[0-9a-f]{64}')):
            need(type(profile[key]) is str and re.fullmatch(pattern, profile[key]) is not None,
                 'assistant-trial-source-method-invalid')
        need(type(profile['expected_revision']) is int and profile['expected_revision'] == 2,
             'assistant-trial-ready-revision-invalid')
        need(all(type(profile[k]) in (int,float) and math.isfinite(profile[k])
                 for k in ('not_before','not_after')) and profile['not_before'] < profile['not_after'],
             'assistant-trial-explicit-owner-window-required')

    def check_admission_current(self):
        active = self._active.get()
        need(active is not None, 'assistant-trial-held-owner-window-required')
        window_ref, profile = active
        # Actual reader must raise on revocation/change; no public current=True input.
        try:
            self._reader.assert_current(self._ref, window_ref, profile['owner_revision'])
            now = self._clock()
        except Exception:
            raise AssistantTrialOwnerRefused('assistant-trial-owner-currentness-unavailable') from None
        need(type(now) in (int,float) and math.isfinite(now)
             and profile['not_before'] <= now < profile['not_after'],
             'assistant-trial-window-not-current')

    @contextmanager
    def _non_suppressing_reader_window(self):
        manager = self._reader.hold_current(self._ref)
        admitted = manager.__enter__()
        try:
            yield admitted
        except BaseException:
            if manager.__exit__(*sys.exc_info()):
                raise AssistantTrialOwnerRefused('assistant-trial-window-cannot-suppress-handler-refusal') from None
            raise
        else:
            manager.__exit__(None, None, None)

    @contextmanager
    def hold_admission_window(self, **_):
        need(self._active.get() is None, 'assistant-trial-nested-window-refused')
        with ExitStack() as stack:
            try:
                admitted = stack.enter_context(self._non_suppressing_reader_window())
                need(type(admitted) is dict and set(admitted) == {'window_ref','profile'},
                     'assistant-trial-window-reader-shape-invalid')
                need(opaque(admitted['window_ref']), 'assistant-trial-window-ref-invalid')
                profile = copy.deepcopy(admitted['profile'])
                self._validate_profile(profile)
            except AssistantTrialOwnerRefused:
                raise
            except Exception:
                raise AssistantTrialOwnerRefused('assistant-trial-owner-window-unavailable') from None
            token = self._active.set((admitted['window_ref'], profile))
            try:
                self.check_admission_current()
                yield  # Preserve the handler's actual state/role/schema refusals.
            finally:
                self._active.reset(token)

    def resolve(self, *, pre_role_generation, verified_source, identity, session_ref):
        self.check_admission_current()
        profile = self._active.get()[1]
        caller = {k:getattr(identity,k) for k in ('tenant_ref','workspace_ref','principal_ref','consumer_ref')}
        caller['session_ref'] = session_ref
        need(caller == profile['caller'], 'assistant-trial-owner-caller-mismatch')
        state = pre_role_generation
        need(state['lifecycle'] == 'READY_UNBOUND'
             and state['generation_ref'] == profile['generation_ref']
             and state['revision'] == profile['expected_revision']
             and state['source_revision'] == profile['source_revision']
             and state['tenant_ref'] == caller['tenant_ref']
             and state['workspace_ref'] == caller['workspace_ref']
             and state['civilization_method_attestation']['method_fingerprint'] == profile['method_fingerprint'],
             'assistant-trial-original-state-mismatch')
        need(verified_source.get('source_revision') == profile['source_revision']
             and verified_source.get('method_fingerprint') == profile['method_fingerprint']
             and verified_source.get('method_unchanged') is True
             and verified_source.get('ancestry_verified') is True,
             'assistant-trial-source-continuity-not-qualified')
        return {'result':'ALLOW','decision_ref':profile['decision_ref'],
                'pre_role_fingerprint':state['fingerprint'],
                'ready_unbound_receipt_fingerprint':state['ready_unbound_receipt']['receipt_fingerprint'],
                'overlay_payload':{'generation_ref':state['generation_ref'],
                  'expected_revision':state['revision'],'source_revision':state['source_revision'],
                  'overlay_ref':profile['overlay_ref'],'actor_ref':profile['actor_ref'],
                  'actor_generation_ref':profile['actor_generation_ref'],'role':'ASSISTANT'}}

    def check_client_admission(self, request):
        """Optional local proposal check; NOT authentication or a host effect grant."""
        with self.hold_admission_window():
            profile = self._active.get()[1]
            expected = request['owner_expectation']
            caller = profile['caller']
            need(request['selected_role'] == 'ASSISTANT'
                 and request['boot_attempt_id'] == profile['generation_ref']
                 and request['source_revision'] == profile['source_revision']
                 and request['method_fingerprint'] == profile['method_fingerprint']
                 and request['destination_ref'] == profile['trial_target_ref'],
                 'assistant-trial-client-original-mismatch')
            need(all(expected[k] == caller[k] for k in ('tenant_ref','workspace_ref','principal_ref','session_ref'))
                 and caller['consumer_ref'] in expected['assigned_consumers']
                 and expected['ref'] == profile['owner_ref']
                 and expected['revision'] == profile['owner_revision'],
                 'assistant-trial-client-owner-expectation-mismatch')
            need(expected['target'] == {k:profile[k] for k in ('overlay_ref','actor_ref','actor_generation_ref','recipient_ref')},
                 'assistant-trial-client-target-mismatch')
