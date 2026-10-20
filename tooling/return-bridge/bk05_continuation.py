#!/usr/bin/env python3
"""Prepare an immutable complete continuation; read it without sending or doing work.

Python >=3.12, stdlib. Owner/issuer, reviewer and recipient remain distinct.
Hashes establish byte consistency, never authority, semantic completeness or memory.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / 'candidates/bk05-capsule-tool-v1/src/bk05_capsule/core.py'
SCHEMA = 'bk05-recipient-continuation/v1'
LIMIT = 2_000_000
RENDER_LIMIT = 16_000_000
INLINE = 12_000
PART = 8_000
SHA = re.compile(r'^[0-9a-f]{64}$')
INPUTS = {'original': 'inputs/original.txt', 'review': 'inputs/review.txt',
          'owner_decision': 'inputs/owner-decision.json', 'owner_delta': 'inputs/owner-delta.json',
          'current': 'inputs/current.txt', 'parent_manifest': 'inputs/parent-manifest.json',
          'reviewer_delta': 'inputs/reviewer-delta.json', 'producer_currentness': 'inputs/producer-currentness.json'}
OPTIONAL = {'parent_manifest', 'reviewer_delta', 'producer_currentness'}
FIXED_OUTPUTS = {'request.json', 'short.txt', 'full.txt', 'profile.json', 'stage-owner.json'}
GUARD = ('LOCAL_STRUCTURAL_ONLY; authority NONE. This packet grants no role, access or effect. '
         'The actual recipient reads sufficient originals and reconciles newer legitimate direction, '
         'reached work and unknown send/effect before action. Reading never sends or replays work.\n')


def need(value, code):
    if not value:
        raise ValueError(code)


def canonical(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n').encode()


def pin(raw):
    return {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def fields(value, names):
    need(isinstance(value, dict) and set(value) == set(names.split()), 'FIXED_FIELDS_REQUIRED')


def text(value):
    need(isinstance(value, str) and value.strip() and not any(0xD800 <= ord(c) <= 0xDFFF for c in value), 'NONEMPTY_UTF8_TEXT_REQUIRED')
    return value


def digest(value):
    need(isinstance(value, str) and SHA.fullmatch(value), 'SHA256_REQUIRED')


def parse(raw):
    def unique(pairs):
        obj = {}
        for key, val in pairs:
            need(key not in obj, 'DUPLICATE_JSON_KEY')
            obj[key] = val
        return obj
    try:
        obj = json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                         parse_constant=lambda _: (_ for _ in ()).throw(ValueError('NONFINITE_JSON')))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError('INVALID_UTF8_JSON') from exc
    need(isinstance(obj, dict), 'JSON_OBJECT_REQUIRED')
    return obj


def path(value):
    need(isinstance(value, (str, Path)), 'LOCAL_ABSOLUTE_PATH_REQUIRED')
    value = str(value)
    need(not value.startswith(('\\\\', '//')) and not any(ord(c) < 32 for c in value), 'LOCAL_ABSOLUTE_PATH_REQUIRED')
    need(not any(x in ('.', '..') for x in re.split(r'[/\\]', value)), 'PATH_TRAVERSAL')
    p = Path(value)
    need(p.is_absolute(), 'LOCAL_ABSOLUTE_PATH_REQUIRED')
    for q in [*reversed(p.parents), p]:
        if q.exists() or q.is_symlink():
            st = q.lstat()
            need(not q.is_symlink() and not getattr(st, 'st_file_attributes', 0) & 0x400, 'REPARSE_PATH_REFUSED')
    # Refuse Windows alternate streams/device-like components; no arbitrary packet member paths.
    need(all(':' not in part for part in p.parts[1:]), 'ALTERNATE_STREAM_REFUSED')
    return p


def read_bytes(value, limit=LIMIT):
    p = path(value)
    before = p.stat()
    need(p.is_file() and before.st_size <= limit, 'FILE_MISSING_OR_OVERBOUND')
    with p.open('rb') as stream:
        raw = stream.read(limit + 1)
    after = p.stat()
    need(len(raw) <= limit and (before.st_ino, before.st_size, before.st_mtime_ns)
         == (after.st_ino, after.st_size, after.st_mtime_ns), 'FILE_CHANGED_DURING_READ')
    return raw


def core():
    spec = importlib.util.spec_from_file_location('bk05_continuation_core', CORE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def actor(value):
    fields(value, 'actor_ref generation_ref carrier_ref')
    for v in value.values():
        text(v)


def strings(value):
    need(isinstance(value, list) and value and all(isinstance(v, str) and v.strip() for v in value), 'NONEMPTY_STRING_LIST_REQUIRED')


def identity(r):
    return {key: r[key] for key in ('task_ref', 'task_revision', 'continuation_ref', 'kind', 'issuer', 'reviewer', 'recipient')}


def validate_request(r):
    fields(r, 'schema task_ref task_revision continuation_ref kind issuer reviewer recipient review_ref purpose return_target way_home code_basis inputs')
    need(r['schema'] == SCHEMA and r['kind'] in ('REPAIR', 'REFINE'), 'CONTINUATION_KIND_REQUIRED')
    for key in ('task_ref', 'task_revision', 'continuation_ref', 'review_ref', 'purpose', 'return_target', 'way_home'):
        text(r[key])
    for who in ('issuer', 'reviewer', 'recipient'):
        actor(r[who])
    need(len({r[who]['actor_ref'] for who in ('issuer', 'reviewer', 'recipient')}) == 3, 'DISTINCT_ISSUER_REVIEWER_RECIPIENT_REQUIRED')
    fields(r['code_basis'], 'source_commit core_sha256')
    need(isinstance(r['code_basis']['source_commit'], str) and re.fullmatch('[a-f0-9]{40}', r['code_basis']['source_commit']), 'SELECTED_SOURCE_COMMIT_REQUIRED')
    digest(r['code_basis']['core_sha256'])
    fields(r['inputs'], ' '.join(INPUTS))
    for name, loc in r['inputs'].items():
        if loc is None:
            need(name in OPTIONAL, 'REQUIRED_INPUT_LOCATOR_MISSING')
        else:
            path(loc)


def validate_owner(r, data):
    decision, delta = parse(data['inputs/owner-decision.json']), parse(data['inputs/owner-delta.json'])
    fields(decision, 'schema task_ref task_revision continuation_ref issuer reviewer recipient original_sha256 review_sha256 decisions')
    need(decision['schema'] == 'bk05-owner-decision/v1', 'OWNER_DECISION_SCHEMA_REQUIRED')
    for key in ('task_ref', 'task_revision', 'continuation_ref', 'issuer', 'reviewer', 'recipient'):
        need(decision[key] == r[key], 'OWNER_DECISION_IDENTITY_MISMATCH')
    need(decision['original_sha256'] == pin(data['inputs/original.txt'])['sha256']
         and decision['review_sha256'] == pin(data['inputs/review.txt'])['sha256'], 'OWNER_DECISION_SOURCE_MISMATCH')
    need(isinstance(decision['decisions'], list) and decision['decisions'], 'ATTRIBUTED_DECISIONS_REQUIRED')
    refs = []
    for row in decision['decisions']:
        fields(row, 'finding_ref disposition reviewer_position owner_position dissent')
        for key in ('finding_ref', 'reviewer_position', 'owner_position', 'dissent'):
            text(row[key])
        need(row['disposition'] in ('ACCEPTED', 'PARTIAL', 'REJECTED'), 'OWNER_DISPOSITION_INVALID')
        refs.append(row['finding_ref'])
    need(len(refs) == len(set(refs)), 'DUPLICATE_FINDING_REF')
    fields(delta, 'schema task_ref task_revision continuation_ref owner_decision_sha256 action scope critical_constraints done_criteria')
    need(delta['schema'] == 'bk05-owner-delta/v1', 'OWNER_DELTA_SCHEMA_REQUIRED')
    for key in ('task_ref', 'task_revision', 'continuation_ref'):
        need(delta[key] == r[key], 'OWNER_DELTA_IDENTITY_MISMATCH')
    need(delta['owner_decision_sha256'] == pin(data['inputs/owner-decision.json'])['sha256'], 'OWNER_DELTA_DECISION_MISMATCH')
    text(delta['action']); strings(delta['critical_constraints']); strings(delta['done_criteria'])
    fields(delta['scope'], 'effect_class privacy_class live_scope authority_class allowed_paths')
    for key in ('effect_class', 'privacy_class', 'live_scope', 'authority_class'):
        text(delta['scope'][key])
    allowed = delta['scope']['allowed_paths']
    need(isinstance(allowed, list) and len(allowed) == len(set(allowed))
         and all(isinstance(p, str) and core().check_path(p) is None for p in allowed), 'OWNER_PATH_SCOPE_INVALID')
    return decision, delta


def profile(r, data):
    c = core()
    present = [name for name in OPTIONAL if r['inputs'][name] is not None]
    if not present:
        return {'selection': 'FULL_CONTINUATION', 'reason': 'CODE_PROFILE_NOT_SELECTED', 'core_decision': None}, None
    # Validate each provided JSON even when full code-profile inputs are unavailable.
    specs = {'parent_manifest': c.PARENT_MANIFEST_SPEC, 'reviewer_delta': c.VERIFIER_DELTA_SPEC,
             'producer_currentness': c.CURRENTNESS_SPEC}
    for name in present:
        collector = c._Collector()
        c._load(data[INPUTS[name]], name, specs[name], collector)
        need(not collector.items, 'INVALID_CODE_PROFILE_INPUT')
    if 'parent_manifest' in present:
        parent = parse(data[INPUTS['parent_manifest']])
        need(parent['task_ref'] == r['task_ref'] and parent['parent_revision'] == r['task_revision']
             and parent['parent_sha256'] == pin(data[INPUTS['original']])['sha256'], 'CORE_TASK_OR_ORIGINAL_MISMATCH')
    if 'reviewer_delta' in present:
        review = parse(data[INPUTS['reviewer_delta']])
        need(review['verifier_ref'] == r['review_ref']
             and review['verifier_sha256'] == pin(data[INPUTS['review']])['sha256'], 'REVIEW_REF_OR_BYTES_MISMATCH')
    if len(present) != len(OPTIONAL):
        return {'selection': 'FULL_CONTINUATION', 'reason': 'CODE_PROFILE_BASIS_INCOMPLETE', 'core_decision': None}, None
    parent = parse(data[INPUTS['parent_manifest']]); review = parse(data[INPUTS['reviewer_delta']])
    # Never edit reviewer actor/finding to fit the code profile or owner's chosen delta.
    need(parent['task_ref'] == r['task_ref'] and parent['parent_revision'] == r['task_revision'], 'CORE_TASK_MISMATCH')
    need(review['verifier_ref'] == r['review_ref'], 'REVIEW_REF_MISMATCH')
    result = c.evaluate(data[INPUTS['original']], data[INPUTS['parent_manifest']], data[INPUTS['review']],
                        data[INPUTS['reviewer_delta']], data[INPUTS['producer_currentness']])
    need(result.decision != c.INVALID_INPUT, 'INVALID_CORE_INPUT')
    need(result.decision != c.HOLD_STALE, 'PRODUCER_BASIS_STALE')
    decision_doc = result.decision_document()
    delta = parse(data[INPUTS['owner_delta']])
    same_scope = parent['actor_ref'] == r['recipient']['actor_ref'] and all(
        parent[key] == delta['scope'][key] for key in ('effect_class', 'privacy_class', 'live_scope', 'authority_class'))
    same_scope = same_scope and set(delta['scope']['allowed_paths']).issubset(parent['allowed_paths'])
    if result.decision == c.FULL_TASK_REQUIRED or not same_scope:
        return {'selection': 'FULL_CONTINUATION', 'reason': 'CODE_PROFILE_NONFIT_NO_AUTHORITY_EXTENSION',
                'core_decision': decision_doc}, None
    return {'selection': 'SHORT_AVAILABLE', 'reason': 'STRUCTURAL_CAPSULE_NOT_HELD_MEMORY_PROOF',
            'core_decision': decision_doc}, c.capsule_file_bytes(result.capsule)


def render(r, data, result):
    decision, delta = validate_owner(r, data)
    common = (GUARD + 'ATTRIBUTION AND TASK\n' + canonical(identity(r)).decode()
              + 'PURPOSE\n' + r['purpose'] + '\nOWNER DECISION (issuer; reviewer position/dissent preserved)\n'
              + data[INPUTS['owner_decision']].decode('utf-8')
              + '\nOPERATIVE OWNER DELTA, CRITICAL CONSTRAINTS AND FINISH\n' + data[INPUTS['owner_delta']].decode('utf-8')
              + '\nRETURN TARGET\n' + r['return_target'] + '\nWAY HOME\n' + r['way_home']
              + '\nPROFILE (no role/semantic/permission assertion)\n' + canonical(result).decode())
    refs = {name: {'member': INPUTS[name], **pin(data[INPUTS[name]])} for name in INPUTS if r['inputs'][name] is not None}
    short = common + '\nEXACT NECESSARY ORIGINAL READ POINTERS (inside this packet)\n' + canonical(refs).decode()
    full = common + '\nEXACT TASK ORIGINAL\n' + data[INPUTS['original']].decode('utf-8') + '\nEXACT ATTRIBUTED REVIEW\n' + data[INPUTS['review']].decode('utf-8')
    for name in ('reviewer_delta', 'parent_manifest', 'producer_currentness'):
        if r['inputs'][name] is not None:
            full += '\nEXACT ' + name.upper() + '\n' + data[INPUTS[name]].decode('utf-8')
    full += '\nFROZEN OWNER CURRENT IS PROVENANCE, NOT A NEW LIVE TASK SELECTION\n' + canonical(refs).decode()
    need(len(short.encode()) <= RENDER_LIMIT and len(full.encode()) <= RENDER_LIMIT, 'COMPLETE_RENDER_OVERBOUND')
    return short.encode(), full.encode()


def key_for(r):
    return pin(canonical(identity(r)))['sha256']


def source_fingerprint(data, code):
    return pin(canonical({'inputs': {p: pin(raw) for p, raw in data.items()}, 'producer_code': code}))['sha256']


def verify(manifest, expected, *, _stage=False):
    digest(expected)
    loc = path(manifest)
    need(loc.name == 'manifest.json', 'EXACT_MANIFEST_LOCATOR_REQUIRED')
    raw = read_bytes(loc)
    need(pin(raw)['sha256'] == expected, 'MANIFEST_HASH_MISMATCH')
    m = parse(raw)
    fields(m, 'schema version key request_fingerprint producer_code files authority status')
    need(m['schema'] == SCHEMA and m['version'] == 1 and type(m['version']) is int
         and m['authority'] == 'NONE' and m['status'] == 'COMPLETE_IMMUTABLE_PACKET', 'PACKET_VERSION_OR_CLAIM_INVALID')
    prefix = '.bk05-stage-' if _stage else 'packet-'
    need(loc.parent.name == prefix + m['key'], 'PACKET_LOCATOR_IDENTITY_MISMATCH')
    fields(m['producer_code'], 'adapter core')
    for value in m['producer_code'].values():
        digest(value)
    need(isinstance(m['files'], dict), 'FILE_MANIFEST_REQUIRED')
    allowed = set(INPUTS.values()) | FIXED_OUTPUTS | {'capsule.json'}
    need(FIXED_OUTPUTS.issubset(m['files']) and set(m['files']).issubset(allowed), 'PACKET_MEMBER_SET_INVALID')
    data = {}
    for rel, expected_pin in m['files'].items():
        fields(expected_pin, 'bytes sha256'); digest(expected_pin['sha256'])
        need(type(expected_pin['bytes']) is int and expected_pin['bytes'] >= 0, 'FILE_SIZE_INVALID')
        body = read_bytes(loc.parent / rel, RENDER_LIMIT if rel in ('short.txt', 'full.txt') else LIMIT)
        need(pin(body) == expected_pin, 'PACKET_MEMBER_CHANGED')
        data[rel] = body
    actual = set()
    for child in loc.parent.iterdir():
        path(child)
        if child.name == 'inputs' and child.is_dir():
            for member in child.iterdir():
                need(member.is_file(), 'UNEXPECTED_PACKET_MEMBER')
                actual.add('inputs/' + member.name)
        else:
            need(child.is_file(), 'UNEXPECTED_PACKET_MEMBER')
            actual.add(child.name)
    need(actual == set(m['files']) | {'manifest.json'}, 'PACKET_INCOMPLETE_OR_EXTRA_MEMBER')
    r = parse(data['request.json']); validate_request(r)
    need(key_for(r) == m['key'], 'PACKET_IDENTITY_MISMATCH')
    expected_inputs = {INPUTS[name] for name in INPUTS if r['inputs'][name] is not None}
    need({p for p in data if p.startswith('inputs/')} == expected_inputs, 'INPUT_SET_MISMATCH')
    sources = {p: data[p] for p in {'request.json'} | expected_inputs}
    need(source_fingerprint(sources, m['producer_code']) == m['request_fingerprint'], 'INPUT_FINGERPRINT_MISMATCH')
    need(data['stage-owner.json'] == canonical({'schema': SCHEMA + '-own-stage', 'key': m['key'],
                                               'request_fingerprint': m['request_fingerprint']}), 'PACKET_OWNERSHIP_MARKER_MISMATCH')
    validate_owner(r, data)
    result = parse(data['profile.json'])
    fields(result, 'selection reason core_decision')
    need(result['selection'] in ('SHORT_AVAILABLE', 'FULL_CONTINUATION'), 'PROFILE_SELECTION_INVALID')
    if result['selection'] == 'SHORT_AVAILABLE':
        need('capsule.json' in data, 'CAPSULE_MISSING')
        need(pin(read_bytes(CORE))['sha256'] == m['producer_code']['core'], 'CORE_VERSION_FIT_REQUIRED')
        c = core()
        status, _, _ = c.reconstruct(data['capsule.json'], data[INPUTS['original']], data[INPUTS['review']])
        need(status == c.RECONSTRUCTED, 'STORED_CAPSULE_RECONSTRUCTION_FAILED')
    else:
        need('capsule.json' not in data, 'NONFIT_CAPSULE_CONFLICT')
    short, full = render(r, data, result)
    need((data['short.txt'], data['full.txt']) == (short, full), 'RENDERED_OWNER_BINDING_MISMATCH')
    need(read_bytes(loc) == raw, 'PACKET_CHANGED_DURING_READ')
    return m, r, data, result


def delivery(manifest, sha, r, state):
    return {'state': state, 'manifest': str(manifest), 'manifest_sha256': sha,
            'envelope': {'identity': identity(r), 'manifest': str(manifest), 'sha256': sha,
                         'return_target': r['return_target'], 'way_home': r['way_home']},
            'authority': 'NONE', 'dispatch': 'NOT_SENT', 'recipient_use': 'NOT_OBSERVED'}


def clear_owned_stage(stage, marker):
    """Only a matching deterministic own stage with fixed known files; preserve unknown data."""
    need(read_bytes(stage / 'stage-owner.json') == marker, 'STAGING_OWNER_OR_INPUT_CONFLICT')
    permitted = set(INPUTS.values()) | FIXED_OUTPUTS | {'capsule.json', 'manifest.json', 'stage-owner.json'}
    found = []
    for child in stage.iterdir():
        path(child)
        if child.name == 'inputs' and child.is_dir():
            for member in child.iterdir():
                path(member)
                need(member.is_file() and 'inputs/' + member.name in permitted, 'UNKNOWN_STAGING_CONTENT_RECONCILE')
                found.append(member)
        else:
            need(child.is_file() and child.name in permitted, 'UNKNOWN_STAGING_CONTENT_RECONCILE')
            if child.name != 'stage-owner.json':
                found.append(child)
    for member in found:
        member.unlink()
    if (stage / 'inputs').exists():
        (stage / 'inputs').rmdir()
    (stage / 'stage-owner.json').unlink()
    stage.rmdir()


def prepare(*, request, out_root):
    need(sys.version_info >= (3, 12), 'PYTHON_312_OR_NEWER_REQUIRED')
    raw = read_bytes(request); r = parse(raw); validate_request(r)
    root = path(out_root); need(root.is_dir(), 'EXISTING_TASK_OUTPUT_ROOT_REQUIRED')
    data = {'request.json': raw}
    for name, rel in INPUTS.items():
        if r['inputs'][name] is not None:
            value = read_bytes(r['inputs'][name]); text(value.decode('utf-8'))
            data[rel] = value
    need(pin(read_bytes(CORE))['sha256'] == r['code_basis']['core_sha256'], 'SELECTED_CORE_BYTES_MISMATCH')
    validate_owner(r, data)
    code = {'adapter': pin(read_bytes(Path(__file__)))['sha256'], 'core': pin(read_bytes(CORE))['sha256']}
    fingerprint = source_fingerprint(data, code)
    key = key_for(r); target = root / ('packet-' + key)
    if target.exists():
        manifest = target / 'manifest.json'
        need(manifest.is_file(), 'PUBLISHED_PACKET_INCOMPLETE_RECONCILE')
        expected = pin(read_bytes(manifest))['sha256']
        old, _, _, _ = verify(manifest, expected)
        need(old['request_fingerprint'] == fingerprint, 'CONTINUATION_IDENTITY_INPUT_CONFLICT')
        return delivery(manifest, expected, r, 'EXACT_REUSE')
    result, capsule = profile(r, data)
    data['profile.json'] = canonical(result)
    if capsule is not None:
        data['capsule.json'] = capsule
    data['short.txt'], data['full.txt'] = render(r, data, result)
    stage = root / ('.bk05-stage-' + key)
    marker = canonical({'schema': SCHEMA + '-own-stage', 'key': key, 'request_fingerprint': fingerprint})
    # Keep the ownership marker as immutable packet evidence; no unlink/rename crash gap.
    data['stage-owner.json'] = marker
    manifest_obj = {'schema': SCHEMA, 'version': 1, 'key': key, 'request_fingerprint': fingerprint,
                    'producer_code': code, 'files': {p: pin(b) for p, b in data.items()},
                    'authority': 'NONE', 'status': 'COMPLETE_IMMUTABLE_PACKET'}
    manifest_raw = canonical(manifest_obj)
    if stage.exists():
        clear_owned_stage(path(stage), marker)
    stage.mkdir()  # Exclusive reservation; marker failure leaves concrete reconcile boundary.
    with (stage / 'stage-owner.json').open('xb') as stream:
        stream.write(marker); stream.flush(); os.fsync(stream.fileno())
    try:
        (stage / 'inputs').mkdir()
        for rel, body in data.items():
            if rel != 'stage-owner.json':
                with path(stage / rel).open('xb') as stream:
                    stream.write(body); stream.flush(); os.fsync(stream.fileno())
            need(read_bytes(stage / rel, RENDER_LIMIT) == body, 'STAGING_READBACK_FAILED')
        # Marker is the completed packet, published with the whole directory on this filesystem.
        with (stage / 'manifest.json').open('xb') as stream:
            stream.write(manifest_raw); stream.flush(); os.fsync(stream.fileno())
        need(read_bytes(stage / 'manifest.json') == manifest_raw, 'MANIFEST_READBACK_FAILED')
        verify(stage / 'manifest.json', pin(manifest_raw)['sha256'], _stage=True)
        need(read_bytes(request) == raw, 'PRODUCER_REQUEST_CHANGED_DURING_PREPARE')
        for name, rel in INPUTS.items():
            if r['inputs'][name] is not None:
                need(read_bytes(r['inputs'][name]) == data[rel], 'PRODUCER_INPUT_CHANGED_DURING_PREPARE')
        need(not target.exists(), 'PACKET_APPEARED_RECONCILE')
        os.rename(stage, target)  # Atomic same-filesystem publication; never os.replace.
    except BaseException:
        # Preserve incomplete own stage and all caller files. Next same-input call reconciles it.
        raise
    expected = pin(manifest_raw)['sha256']
    verify(target / 'manifest.json', expected)
    return delivery(target / 'manifest.json', expected, r, 'PREPARED_COMPLETE_PACKET')


def observe(observation, r, data):
    raw = read_bytes(observation); o = parse(raw)
    fields(o, 'schema task_ref task_revision continuation_ref recipient owner_decision_sha256 original_sha256 source_current selected_source_commit observed_global_source_head observed_at provenance relevant_change newer_direction work_state send_outcome')
    need(o['schema'] == 'bk05-recipient-current-observation/v1', 'CURRENT_OBSERVATION_SCHEMA_REQUIRED')
    actor(o['recipient'])
    need(o['recipient'] == r['recipient'], 'ACTUAL_RECIPIENT_BINDING_MISMATCH')
    for key in ('task_ref', 'task_revision', 'continuation_ref'):
        need(o[key] == r[key], 'CURRENT_TASK_DECISION_CHANGED_RECONCILE')
    need(o['owner_decision_sha256'] == pin(data[INPUTS['owner_decision']])['sha256']
         and o['original_sha256'] == pin(data[INPUTS['original']])['sha256'], 'CURRENT_OWNER_DECISION_CHANGED_RECONCILE')
    fields(o['source_current'], 'path sha256'); digest(o['source_current']['sha256'])
    source = read_bytes(o['source_current']['path'])
    need(pin(source)['sha256'] == o['source_current']['sha256'], 'CURRENT_OBSERVATION_SOURCE_DRIFT')
    try:
        stamp = datetime.datetime.fromisoformat(o['observed_at'])
        need(stamp.utcoffset() is not None, 'CURRENT_OBSERVATION_TIME_REQUIRED')
    except (TypeError, ValueError) as exc:
        raise ValueError('CURRENT_OBSERVATION_TIME_REQUIRED') from exc
    text(o['provenance']); text(o['observed_global_source_head'])
    need(o['relevant_change'] in ('NONE', 'UNRELATED_GLOBAL_HEAD', 'RELEVANT_CODE', 'TASK_DECISION', 'SCOPE_OR_ACCESS'), 'CHANGE_CLASS_REQUIRED')
    need(o['newer_direction'] in ('NONE', 'QUESTION', 'CORRECTION', 'STOP'), 'NEWER_DIRECTION_CLASS_REQUIRED')
    need(o['work_state'] in ('NOT_STARTED', 'PARTIAL', 'COMPLETED', 'UNKNOWN_EFFECT'), 'WORK_STATE_REQUIRED')
    need(o['send_outcome'] in ('NOT_SENT', 'SENT', 'UNKNOWN'), 'SEND_OUTCOME_REQUIRED')
    need(o['selected_source_commit'] == r['code_basis']['source_commit']
         and o['relevant_change'] in ('NONE', 'UNRELATED_GLOBAL_HEAD')
         and o['newer_direction'] in ('NONE', 'QUESTION'), 'RELEVANT_SCOPE_CODE_OR_DIRECTION_REQUIRES_OWNER')
    need(read_bytes(observation) == raw and read_bytes(o['source_current']['path']) == source, 'OBSERVATION_CHANGED_DURING_READ')
    return o, pin(raw)


def parts(raw):
    result = []; start = 0
    while start < len(raw):
        end = min(start + PART, len(raw))
        while end < len(raw) and raw[end] & 0xC0 == 0x80:
            end -= 1
        value = raw[start:end]; value.decode('utf-8')
        result.append({'index': len(result), 'byte_offset': start, 'byte_length': len(value), 'sha256': pin(value)['sha256']})
        start = end
    return result


def read(*, manifest, expect_sha, mode, current_observation, part=None):
    need(sys.version_info >= (3, 12), 'PYTHON_312_OR_NEWER_REQUIRED')
    need(mode in ('short', 'full'), 'READ_MODE_REQUIRED')
    m, r, data, result = verify(manifest, expect_sha)
    o, op = observe(current_observation, r, data)
    member = 'short.txt' if mode == 'short' and result['selection'] == 'SHORT_AVAILABLE' else 'full.txt'
    raw = data[member]; chunks = parts(raw)
    action = 'READ_ONLY_NO_WORK_EXECUTED'
    if o['work_state'] == 'COMPLETED':
        action = 'ALREADY_COMPLETED_NO_REPLAY'
    elif o['work_state'] == 'UNKNOWN_EFFECT' or o['send_outcome'] == 'UNKNOWN':
        action = 'RECONCILE_UNKNOWN_SEND_OR_EFFECT_BEFORE_ACTION'
    output = {'state': 'VALIDATED_READ_NOT_UNDERSTOOD_OR_RELEASED', 'identity': identity(r),
              'manifest': str(path(manifest)), 'manifest_sha256': expect_sha, 'requested_mode': mode,
              'selected_member': member, 'profile': result, 'action': action, 'authority': 'NONE',
              'observation': {'sha256': op['sha256'], 'source_current': o['source_current'], 'observed_at': o['observed_at'],
                              'provenance': o['provenance'], 'age': 'CALLER_SUPPLIED_NOT_JUDGED_BY_READER',
                              'relevant_change': o['relevant_change'], 'newer_direction': o['newer_direction']},
              'necessary_originals': {name: {'path': str(path(manifest).parent / INPUTS[name]), **pin(data[INPUTS[name]])}
                                     for name in INPUTS if r['inputs'][name] is not None},
              'return_target': r['return_target'], 'way_home': r['way_home'],
              'limits': ['NOT_OWNER_OR_RECIPIENT_AUTHENTICATION', 'NOT_SEMANTIC_COMPLETENESS', 'NOT_HELD_MEMORY_PROOF',
                         'NO_SEND_OR_WORK_EFFECT', 'RECONCILE_ACTUAL_NEWER_HUMAN_DIRECTION']}
    output['code_version'] = {'packet_format': m['version'], 'producer': m['producer_code'],
                              'reader': {'adapter': pin(read_bytes(Path(__file__)))['sha256'],
                                         'core': pin(read_bytes(CORE))['sha256']},
                              'basis': 'EXPLICIT_V1_FORMAT_AND_STORED_RENDER_RECONSTRUCTION_FIT; NOT_A_NEW_PRODUCER_PACKET'}
    if part is not None:
        need(type(part) is int and 0 <= part < len(chunks), 'PART_INDEX_INVALID')
        chunk = chunks[part]; start = chunk['byte_offset']
        output['delivery'] = {'kind': 'EXACT_PART_ONLY', 'member': member, 'part': chunk,
                              'parts_total': len(chunks), 'complete_delivery_claim': False,
                              'text': raw[start:start + chunk['byte_length']].decode('utf-8')}
    elif len(raw) <= INLINE:
        output['delivery'] = {'kind': 'COMPLETE_INLINE_TEXT', 'member': member, 'text': raw.decode('utf-8')}
    else:
        output['delivery'] = {'kind': 'EXACT_PARTS_REQUIRED_NOT_FULL_DELIVERY', 'member': member,
                              'path': str(path(manifest).parent / member), 'parts': chunks,
                              'complete_delivery_claim': False,
                              'next': 'Use this same read command with --part INDEX for every necessary exact part; no new production or send.'}
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    p = sub.add_parser('prepare'); p.add_argument('--request', required=True); p.add_argument('--out-root', required=True)
    p = sub.add_parser('read'); p.add_argument('--manifest', required=True); p.add_argument('--expect-sha', required=True)
    p.add_argument('--mode', required=True, choices=('short', 'full')); p.add_argument('--current-observation', required=True)
    p.add_argument('--part', type=int)
    args = vars(parser.parse_args()); action = args.pop('action')
    try:
        result = (prepare if action == 'prepare' else read)(**args)
        print(json.dumps(result, sort_keys=True, ensure_ascii=True))
        return 0
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, ImportError, RecursionError) as exc:
        reason = str(exc) if isinstance(exc, ValueError) and re.fullmatch('[A-Z0-9_]+', str(exc)) else 'LOCAL_INPUT_IO_OR_VERSION_UNRESOLVED'
        print(json.dumps({'state': 'OPEN_RECONCILE_SAME_ATTEMPT', 'reason': reason,
                          'authority': 'NONE', 'payload_released': False, 'dispatch': 'NOT_SENT', 'work_executed': False}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
