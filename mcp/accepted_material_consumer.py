"""One ordinary accepted-material scan, not a poller or work-completion ledger.

The caller supplies its already authorized read port and its existing durable
actor/project-scoped completed-work ledger. Each invocation starts at the first
page. Only the caller's actual work/disposition may update that ledger.
"""
import hashlib
import json
import re
from collections.abc import Mapping

PAGE_SIZE = 8
MAX_SCAN_PAGES = 16  # Selected admission model: 128 materials / 8 per page.
_HEX = re.compile(r"[a-f0-9]{64}\Z")
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@/#=-]{0,159}\Z")
_SAFE_ERRORS = frozenset({
    'OLD_CARRIER_EXECUTION_FENCED',
    'ROOM_ACTOR_READ_PERMISSION_EXPIRED_OR_NOT_YET_VALID',
    'ACTOR_ACCESS_EXPIRED', 'RECEIVE_BINDING_NOT_CURRENT',
    'RECEIVE_ARGUMENTS_INVALID',
})

class AcceptedMaterialScanRefused(RuntimeError):
    pass

def _need(ok, reason):
    if not ok:
        raise AcceptedMaterialScanRefused(reason)

def consumption_fingerprint(row):
    """Immutable body identity, independent of carrier and notification age."""
    _need(type(row) is dict, 'ACCEPTED_ROW_INVALID')
    key = row.get('business_dedupe_key')
    _need(type(key) is str and _HEX.fullmatch(key), 'BUSINESS_DEDUPE_KEY_REQUIRED')
    hashes = {}
    for name, bound in (('material', 16384), ('envelope', 4096)):
        value = row.get(name); expected = row.get(name+'_sha256')
        _need(type(value) is str and 0 < len(value.encode('utf-8')) <= bound
              and type(expected) is str and _HEX.fullmatch(expected), 'ACCEPTED_BODY_INVALID')
        actual = hashlib.sha256(value.encode('utf-8')).hexdigest()
        _need(actual == expected, 'ACCEPTED_BODY_DIGEST_MISMATCH')
        hashes[name+'_sha256'] = actual
    return hashlib.sha256(json.dumps(hashes, sort_keys=True,
        separators=(',', ':')).encode()).hexdigest()

def scan_accepted_room_returns(read_page, completed, *, page_budget=MAX_SCAN_PAGES):
    """Discover without known IDs; no writes, grant renewal, dispatch or use.

    `completed` maps business_dedupe_key -> consumption_fingerprint, qualified
    within one stable actor/project scope. It must survive legitimate carrier
    changes. A scan cursor is never stored as the next check's watermark.
    """
    _need(callable(read_page) and isinstance(completed, Mapping), 'READ_PORT_AND_LEDGER_REQUIRED')
    _need(type(page_budget) is int and 1 <= page_budget <= MAX_SCAN_PAGES, 'SCAN_PAGE_BUDGET_INVALID')
    after = None; seen_cursors = set(); seen = {}; rows = []; skipped = 0
    window_counts = []; window_observations = []; window_owner_required = False
    for page in range(1, page_budget+1):
        args = {'op': 'list_accepted'}
        if after is not None:
            args['after'] = after
        response = read_page(args)
        _need(type(response) is dict, 'READ_PAGE_INVALID')
        if response.get('isError'):
            failure = response.get('structuredContent')
            code = failure.get('error_code') if type(failure) is dict else None
            raise AcceptedMaterialScanRefused(code if type(code) is str and code in _SAFE_ERRORS else 'READ_PAGE_REFUSED')
        value = response.get('structuredContent', response)
        _need(type(value) is dict and type(value.get('rows')) is list
              and len(value['rows']) <= PAGE_SIZE and 'next' in value, 'READ_PAGE_INVALID')
        count = value.get('out_of_window_materials')
        flag = value.get('out_of_window_owner_disposition_required')
        _need(count is None or type(count) is int and 0 <= count <= PAGE_SIZE*MAX_SCAN_PAGES,
              'READ_PAGE_WINDOW_METADATA_INVALID')
        _need(flag is None or type(flag) is bool, 'READ_PAGE_WINDOW_METADATA_INVALID')
        if count is not None:window_counts.append(count)
        window_owner_required = window_owner_required or flag is True or count is not None and count > 0
        window_observations.append({'page':page,'after':after,
            'out_of_window_materials':count,'out_of_window_owner_disposition_required':flag})
        for row in value['rows']:
            fingerprint = consumption_fingerprint(row); key = row['business_dedupe_key']
            if key in seen:
                _need(seen[key] == fingerprint, 'BUSINESS_DEDUPE_CONTENT_CONFLICT')
                continue
            seen[key] = fingerprint
            if key in completed:
                _need(completed[key] == fingerprint, 'BUSINESS_DEDUPE_CONTENT_CONFLICT')
                skipped += 1
            else:
                rows.append(dict(row))
        following = value['next']
        _need(following is None or type(following) is str and _REF.fullmatch(following), 'SCAN_CURSOR_INVALID')
        if following is not None:
            _need(following not in seen_cursors and (after is None or following > after), 'SCAN_CURSOR_NOT_ADVANCING')
            seen_cursors.add(following)
        if following is None or page == page_budget:
            return {'rows': rows, 'complete': following is None, 'next': following,
                    'pages': page, 'completed_rows_skipped': skipped,
                    'observed_fingerprints': seen,
                    'restart_next_check': 'FROM_BEGINNING',
                    'out_of_window_materials': max(window_counts) if window_counts else None,
                    'out_of_window_owner_disposition_required': window_owner_required,
                    'out_of_window_count_basis': 'MAX_OBSERVED_GLOBAL_COUNT_NOT_SUM',
                    'window_observations': window_observations,
                    'window_metadata_complete': all(x['out_of_window_materials'] is not None and x['out_of_window_owner_disposition_required'] is not None for x in window_observations),
                    'owner_action': 'OWNER_MATERIAL_WINDOW_DISPOSITION_REQUIRED_NO_RENEW' if window_owner_required else None,
                    'work_consumed': False, 'effect': 'NONE_READ_ONLY_SCAN'}
        after = following  # Empty rows is NOT terminal when next is present.
