"""BK07 read front: re-find the exact current original, remember what was actually read, refresh only what changed,
reuse a still-qualified unchanged read. AUTHORITY NONE; passive one-shot; default OFF.

    read_front(request, ports, state) -> FrontResult
    python -m signalvev_sensing.bk07_read_front --config <trusted-local-config> --question BK07 --receipt <new-file>

Layers (each reuses an existing home, none is a new truth store):
  A  navigation registry import (owner-supplied, read-only, <= 64 entries / 64 KiB): current owner status + explicit
     superseded_by (depth <= 4, cycle detection). Filenames, modifiedTime, row numbers and lexical revision order are
     never authority. Conflicting CURRENT referents => AMBIGUOUS; missing evidence/status => FALLBACK_REQUIRED.
  B  bookmarks: per knagg binding + per known channel scope MESSAGE_ID -> (row locator, exact row digest). A fresh
     bounded owner manifest each call detects edit/sort/delete before the old highwater; duplicate ids or incomplete
     coverage => no reuse for that scope. Row highwater is only a locator hint.
  C  content: a cold canonical read ALWAYS goes through the existing CAP26 ContextSliceResolver (no second reader);
     a hit needs fresh authorized metadata via CAP26 describe_only (<= 2 describes, one shared <= 5 s operation
     deadline), the identical binding, an attested identical principal/scope/provider context and a local hash check
     of the cached <= 1024 B slice. Anything else: no cached payload, the entry is invalidated.
  Native Docs: the existing DNI reader-v2 ``orient_paragraph`` (loaded from an explicit hash-pinned import root) gives
     ORIENTATION_ONLY; without a reader-issued receipt reuse is REUSE_UNQUALIFIED. Docs UTF-16 indices are never a
     CAP26 UTF8_BYTE_RANGE.
Navigation outcomes are separate from PM-X9 semantic dispositions; this module never reads/writes PM-X9, never wakes a
model, sends a message, injects chat text or schedules anything.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .context_slice import (DENIED as C_DENIED, FALLBACK_REQUIRED as C_FALLBACK, HASH_MISMATCH as C_HASH,
                            SELECTOR_DRIFT as C_DRIFT, SLICE_READY, STALE as C_STALE, ContextSliceResolver,
                            RequestInvalid, ResultEnvelopeUnavailable, Selector, SliceLimits, SliceRequest)
from .context_slice_provider import SAME, LocalPinnedSnapshotProvider, ReadDescriptor
from .model import HEX64, is_id
from .read_front_state import CORRUPT, ReadFrontState, StateBoundExceeded, StateBusy, canonical

# ---- closed outcome set (navigation; NOT PM-X9 dispositions) -------------------------------------------------------
FRESH_SLICE = "FRESH_SLICE"
REUSED_SLICE = "REUSED_SLICE"
ORIENTATION_ONLY = "ORIENTATION_ONLY"
INVALIDATED = "INVALIDATED"
STALE = "STALE"
SUPERSEDED = "SUPERSEDED"
DENIED = "DENIED"
AMBIGUOUS = "AMBIGUOUS"
FALLBACK_REQUIRED = "FALLBACK_REQUIRED"
UNREADABLE = "UNREADABLE"
DISABLED = "DISABLED"
MISSING_PORT = "MISSING_PORT"
INVALID_REQUEST = "INVALID_REQUEST"
OUTCOMES = frozenset({FRESH_SLICE, REUSED_SLICE, ORIENTATION_ONLY, INVALIDATED, STALE, SUPERSEDED, DENIED, AMBIGUOUS,
                      FALLBACK_REQUIRED, UNREADABLE, DISABLED, MISSING_PORT, INVALID_REQUEST})
CONTENT_OUTCOMES = frozenset({FRESH_SLICE, REUSED_SLICE, ORIENTATION_ONLY})

REUSE_QUALIFIED = "REUSE_QUALIFIED"
REUSE_UNQUALIFIED = "REUSE_UNQUALIFIED"

REP_CANONICAL = "CANONICAL_UTF8_EXPORT_V1"
REP_NATIVE_DOCS = "NATIVE_DOCS_PROJECTION_V1"
REPRESENTATIONS = frozenset({REP_CANONICAL, REP_NATIVE_DOCS})
STATUSES = frozenset({"CURRENT", "SUPERSEDED", "UNKNOWN", "WITHDRAWN"})

MAX_REGISTRY_BYTES = 65536
MAX_REGISTRY_ENTRIES = 64
MAX_CHAIN_DEPTH = 4
MAX_ROWS = 128
MAX_ROW_CONTENT_BYTES = 1024
MAX_RESULT_BYTES = 4096
MAX_RECEIPT_BYTES = 2048
MAX_OPERATION_SECONDS = 5.0
MAX_DESCRIBES = 2
_QUESTION = re.compile(r"^[A-Z][A-Z0-9_]{1,15}$")

# Exact DNI reader-v2 reference (materials/dni-reader-v2-reference.zip/reader_v2_adapter.py), imported, never forked.
DNI_ADAPTER_SHA256 = "e036f1697e376904b546889688eb94738e3723e81f17794f411f2194a46a3902"


class FrontError(ValueError):
    def __init__(self, outcome: str, reason: str) -> None:
        super().__init__(reason)
        self.outcome, self.reason = outcome, reason


# ====================================================================================================================
# A. navigation registry import
# ====================================================================================================================
_ENTRY_KEYS = {"knagg", "file_id", "content_ref", "owner_ref", "status", "revision", "superseded_by",
               "representation", "section", "full_sha256", "way_home", "evidence_ref", "authority"}
_SECTION_KEYS = {"tab_id", "semantic_anchor", "selector"}
_SECTION_OPTIONAL = {"anchor_text"}


def _strict_json(data: bytes) -> Any:
    def pairs(items):
        out = {}
        for k, v in items:
            if k in out:
                raise ValueError("duplicate key")
            out[k] = v
        return out

    def no_const(_):
        raise ValueError("non-finite")
    return json.loads(data.decode("utf-8"), object_pairs_hook=pairs, parse_constant=no_const)


@dataclass(frozen=True)
class NavEntry:
    knagg: str
    file_id: str
    content_ref: str | None
    owner_ref: str
    status: str
    revision: str
    superseded_by: str | None
    representation: str
    tab_id: str
    semantic_anchor: str
    anchor_text: str | None
    selector: dict | None
    full_sha256: str | None
    way_home: str
    evidence_ref: str | None
    authority: str

    def binding(self) -> dict[str, Any]:
        """Everything a cached read is bound to (per entry, so an unrelated entry change never touches it)."""
        return {"file_id": self.file_id, "content_ref": self.content_ref, "owner_ref": self.owner_ref,
                "status": self.status, "revision": self.revision, "representation": self.representation,
                "tab_id": self.tab_id, "semantic_anchor": self.semantic_anchor, "anchor_text": self.anchor_text,
                "selector": self.selector, "full_sha256": self.full_sha256, "way_home": self.way_home,
                "evidence_ref": self.evidence_ref}


@dataclass(frozen=True)
class NavRegistry:
    revision: str
    assertion: str
    entries: tuple[NavEntry, ...]


def parse_navigation_registry(data: bytes) -> NavRegistry:
    if len(data) > MAX_REGISTRY_BYTES:
        raise FrontError(UNREADABLE, "REGISTRY_OVER_64KIB")
    try:
        doc = _strict_json(data)
    except (ValueError, UnicodeDecodeError):
        raise FrontError(UNREADABLE, "REGISTRY_MALFORMED") from None
    if (type(doc) is not dict or set(doc) - {"schema", "revision", "assertion", "entries"}
            or doc.get("schema") not in ("bk07-read-front-import-fixture/v1", "bk07-read-front-import/v1")
            or not is_id(doc.get("revision")) or type(doc.get("entries")) is not list):
        raise FrontError(UNREADABLE, "REGISTRY_SHAPE")
    if len(doc["entries"]) > MAX_REGISTRY_ENTRIES:
        raise FrontError(UNREADABLE, "REGISTRY_OVER_64_ENTRIES")
    out = []
    for e in doc["entries"]:
        if type(e) is not dict or set(e) != _ENTRY_KEYS:
            raise FrontError(UNREADABLE, "REGISTRY_ENTRY_SHAPE")
        sec = e["section"]
        if type(sec) is not dict or not _SECTION_KEYS <= set(sec) or set(sec) - _SECTION_KEYS - _SECTION_OPTIONAL:
            raise FrontError(UNREADABLE, "REGISTRY_SECTION_SHAPE")
        ok = (all(is_id(e[k]) for k in ("knagg", "file_id", "owner_ref", "revision", "way_home", "authority"))
              and (e["content_ref"] is None or is_id(e["content_ref"]))
              and (e["superseded_by"] is None or is_id(e["superseded_by"]))
              and (e["evidence_ref"] is None or is_id(e["evidence_ref"]))
              and e["status"] in STATUSES and e["representation"] in REPRESENTATIONS
              and is_id(sec["tab_id"]) and is_id(sec["semantic_anchor"])
              and (sec.get("anchor_text") is None or (type(sec["anchor_text"]) is str and 0 < len(sec["anchor_text"]) <= 200))
              and (sec["selector"] is None or type(sec["selector"]) is dict)
              and (e["full_sha256"] is None or (type(e["full_sha256"]) is str and HEX64.match(e["full_sha256"]))))
        if not ok:
            raise FrontError(UNREADABLE, "REGISTRY_ENTRY_VALUE")
        out.append(NavEntry(knagg=e["knagg"], file_id=e["file_id"], content_ref=e["content_ref"],
                            owner_ref=e["owner_ref"], status=e["status"], revision=e["revision"],
                            superseded_by=e["superseded_by"], representation=e["representation"],
                            tab_id=sec["tab_id"], semantic_anchor=sec["semantic_anchor"],
                            anchor_text=sec.get("anchor_text"), selector=sec["selector"],
                            full_sha256=e["full_sha256"], way_home=e["way_home"], evidence_ref=e["evidence_ref"],
                            authority=e["authority"]))
    return NavRegistry(revision=doc["revision"], assertion=str(doc.get("assertion", "UNSTATED")), entries=tuple(out))


def resolve_current(reg: NavRegistry, knagg: str) -> tuple[NavEntry, list[dict[str, Any]]]:
    """Follow ONLY explicit owner-supplied superseded_by from the knagg's entries. Returns (current, chain)."""
    roots = [e for e in reg.entries if e.knagg == knagg]
    if not roots:
        raise FrontError(FALLBACK_REQUIRED, "KNAGG_NOT_IN_REGISTRY")
    by_id: dict[str, list[NavEntry]] = {}
    for e in reg.entries:
        by_id.setdefault(e.file_id, []).append(e)
    ends: dict[str, NavEntry] = {}
    chain_view: list[dict[str, Any]] = []
    unknown: list[str] = []
    for root in roots:
        cur, seen = root, [root.file_id]
        while cur.status == "SUPERSEDED":
            nxt = cur.superseded_by
            if nxt is None:
                unknown.append("SUPERSEDED_WITHOUT_OWNER_REPLACEMENT")
                break
            if nxt in seen:
                raise FrontError(FALLBACK_REQUIRED, "SUPERSESSION_CYCLE")
            if len(seen) > MAX_CHAIN_DEPTH:
                raise FrontError(FALLBACK_REQUIRED, "SUPERSESSION_DEPTH_OVER_4")
            cands = by_id.get(nxt, [])
            if len(cands) != 1:
                if len(cands) > 1:
                    raise FrontError(AMBIGUOUS, "DUPLICATE_REPLACEMENT_FILE_ID")
                unknown.append("REPLACEMENT_NOT_IN_REGISTRY")
                break
            seen.append(nxt)
            cur = cands[0]
        chain_view.append({"original": root.file_id, "chain": seen, "end_status": cur.status})
        if cur.status == "CURRENT":
            if cur.evidence_ref is None:
                unknown.append("CURRENT_WITHOUT_OWNER_EVIDENCE")
            elif cur.file_id in ends and ends[cur.file_id] != cur:
                raise FrontError(AMBIGUOUS, "CONFLICTING_CURRENT_RECORDS")
            else:
                ends[cur.file_id] = cur
        elif cur.status != "SUPERSEDED":
            unknown.append(f"OWNER_STATUS_{cur.status}")
    if len(by_id.get(next(iter(ends), ""), [])) > 1:
        raise FrontError(AMBIGUOUS, "DUPLICATE_CURRENT_FILE_ID")
    if len(ends) > 1:
        raise FrontError(AMBIGUOUS, "MULTIPLE_ACTIVE_REFERENTS")
    if not ends:
        raise FrontError(FALLBACK_REQUIRED, unknown[0] if unknown else "NO_CURRENT_REFERENT")
    current = next(iter(ends.values()))
    return current, chain_view


# ====================================================================================================================
# B. channel manifest bookmarks
# ====================================================================================================================
def row_digest(row: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical({"message_id": row["message_id"], "owner_ref": row["owner_ref"],
                                     "content": row["content"]})).hexdigest()


def process_manifest(manifest: Any, scope_cfg: Mapping[str, Any], stored: dict[str, Any] | None
                     ) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Returns (rows_view, new_stored_rows or None=leave state untouched). Content only for NEW/EDITED rows."""
    scope = scope_cfg["scope"]
    allowed = set(scope_cfg.get("message_ids") or [])
    max_rows = min(int(scope_cfg.get("max_rows", MAX_ROWS)), MAX_ROWS)
    try:
        ok = (type(manifest) is dict and is_id(manifest.get("revision")) and type(manifest.get("rows")) is list
              and type(manifest.get("coverage")) is dict and manifest["coverage"].get("scope") == scope
              and type(manifest["coverage"].get("complete")) is bool)
        if not ok:
            raise ValueError
        if len(manifest["rows"]) > max_rows:
            return {"outcome": INVALIDATED, "reason": "MANIFEST_OVER_MAX_ROWS", "scope": scope, "changes": []}, \
                _invalidate_rows(stored)
        rows = []
        for r in manifest["rows"]:
            if (type(r) is not dict or not is_id(r.get("message_id")) or type(r.get("row")) is not int
                    or type(r.get("row")) is bool or r["row"] < 0 or not is_id(r.get("owner_ref"))
                    or type(r.get("content")) is not str
                    or len(r["content"].encode("utf-8")) > MAX_ROW_CONTENT_BYTES):
                raise ValueError
            if allowed and r["message_id"] not in allowed:
                continue                                     # known scope only: never an unbounded history scan
            rows.append(r)
    except (ValueError, TypeError, KeyError):
        return {"outcome": UNREADABLE, "reason": "MANIFEST_MALFORMED", "scope": scope, "changes": []}, \
            _invalidate_rows(stored)
    ids = [r["message_id"] for r in rows]
    dups = sorted({i for i in ids if ids.count(i) > 1})
    complete = manifest["coverage"]["complete"]
    prev = (stored or {}).get("messages", {}) if (stored or {}).get("valid", True) else {}
    changes: list[dict[str, Any]] = []
    new_msgs: dict[str, Any] = {}
    for r in rows:
        mid = r["message_id"]
        if mid in dups:
            continue
        dg = row_digest(r)
        old = prev.get(mid)
        if old is None or old.get("status") == "TOMBSTONE":
            kind = "NEW"
        elif old["digest"] != dg:
            kind = "EDITED"                                  # same id, other content: earlier read invalid
        elif old["row"] != r["row"]:
            kind = "MOVED"                                   # sort: identity kept, locator rebuilt
        else:
            kind = "UNCHANGED"
        ch = {"message_id": mid, "change": kind, "row": r["row"], "digest": dg}
        pending = (old or {}).get("delivered_digest") != dg
        if pending and kind in ("UNCHANGED", "MOVED"):
            ch["change"] = "PENDING_DELIVERY"
        if pending or kind in ("NEW", "EDITED") or not complete or dups:
            ch["content"] = r["content"]                     # only refreshed rows travel
        changes.append(ch)
        new_msgs[mid] = {"row": r["row"], "digest": dg, "status": "LIVE", "last_outcome": kind,
                         "delivered_digest": (old or {}).get("delivered_digest")}
    for mid in dups:
        changes.append({"message_id": mid, "change": "AMBIGUOUS_DUPLICATE_ID"})
    if dups or not complete:
        # no reuse claim for this scope; tombstones cannot be inferred from incomplete coverage
        outcome = AMBIGUOUS if dups else FALLBACK_REQUIRED
        reason = "DUPLICATE_MESSAGE_IDS" if dups else "MANIFEST_COVERAGE_INCOMPLETE"
        for c in changes:
            if c["change"] != "AMBIGUOUS_DUPLICATE_ID":
                c["change"] = "FRESH_UNQUALIFIED"            # delivered fresh, no reuse/edit/tombstone claim
        return ({"outcome": outcome, "reason": reason, "scope": scope, "manifest_revision": manifest["revision"],
                 "coverage_complete": complete, "global_completeness": "UNKNOWN", "changes": changes},
                _invalidate_rows(stored))
    for mid, old in prev.items():
        if mid not in new_msgs and (not allowed or mid in allowed):
            changes.append({"message_id": mid, "change": "TOMBSTONE", "row": old.get("row")})
            new_msgs[mid] = {"row": old.get("row"), "digest": old.get("digest"), "status": "TOMBSTONE",
                             "last_outcome": "TOMBSTONE"}
    live = [m["row"] for m in new_msgs.values() if m["status"] == "LIVE"]
    new_state = {"valid": True, "manifest_revision": manifest["revision"], "highwater_hint": max(live) if live else None,
                 "messages": new_msgs}
    return ({"outcome": "ROWS_RECONCILED", "reason": "COMPLETE_SCOPE_MANIFEST", "scope": scope,
             "manifest_revision": manifest["revision"], "coverage_complete": True, "global_completeness": "UNKNOWN",
             "changes": changes}, new_state)


def _invalidate_rows(stored: dict[str, Any] | None) -> dict[str, Any] | None:
    if stored is None:
        return None
    s = dict(stored)
    s["valid"] = False
    return s


# ====================================================================================================================
# ports, request, result
# ====================================================================================================================
@dataclass(frozen=True)
class AccessContext:
    principal_ref: str
    scope_fingerprint: str
    provider_context: str
    attestation: str                    # e.g. SYNTHETIC_TEST_ONLY; "NONE" = not attested
    expires_at: float | None            # wall-clock epoch; None = no expiry stated (=> not attested for reuse)

    def qualified(self, now: float) -> bool:
        return (self.attestation != "NONE" and self.expires_at is not None and now < self.expires_at
                and all(is_id(x) for x in (self.principal_ref, self.scope_fingerprint, self.provider_context)))

    def key(self) -> dict[str, str]:
        return {"principal_ref": self.principal_ref, "scope_fingerprint": self.scope_fingerprint,
                "provider_context": self.provider_context, "attestation": self.attestation}


@dataclass
class ReadFrontPorts:
    registry: Callable[[], bytes] | None = None                      # owner-supplied BK07 navigation registry
    cap26: ContextSliceResolver | None = None                        # EXISTING CAP26 resolver + provider
    manifest: Callable[[], Any] | None = None                        # fresh bounded owner manifest for known scope
    dni: Callable[[str], tuple[Mapping, Mapping] | None] | None = None   # trusted connector observations per doc
    dni_orient: Callable[..., dict] | None = None                    # DNI reader_v2_adapter.orient_paragraph
    access: Callable[[], AccessContext | None] | None = None
    human_active: Callable[[], bool] | None = None
    clock: Callable[[], float] = time.monotonic                      # MUST be the CAP26 provider's clock
    wall_clock: Callable[[], float] = time.time
    provider_stats: Any = None                                       # optional ProviderStats for counters


@dataclass(frozen=True)
class FrontRequest:
    question: str
    knagg: str
    subject_ref: str
    scope: Mapping[str, Any]
    assertion: str = "SYNTHETIC_TEST_ONLY"
    limits: SliceLimits = SliceLimits()
    max_result_bytes: int = MAX_RESULT_BYTES

    def __post_init__(self) -> None:
        if type(self.question) is not str or not _QUESTION.match(self.question):
            raise FrontError(INVALID_REQUEST, "QUESTION_INVALID")
        if not is_id(self.knagg) or not is_id(self.subject_ref):
            raise FrontError(INVALID_REQUEST, "REQUEST_REF_INVALID")
        if type(self.scope) is not dict or not is_id(self.scope.get("scope")):
            raise FrontError(INVALID_REQUEST, "SCOPE_INVALID")
        ids = self.scope.get("message_ids") or []
        if type(ids) is not list or len(ids) > MAX_ROWS or not all(is_id(i) for i in ids):
            raise FrontError(INVALID_REQUEST, "SCOPE_MESSAGE_IDS_INVALID")
        mr = self.scope.get("max_rows", MAX_ROWS)
        if type(mr) is not int or not 1 <= mr <= MAX_ROWS:
            raise FrontError(INVALID_REQUEST, "SCOPE_MAX_ROWS_INVALID")
        if type(self.max_result_bytes) is not int or not 256 <= self.max_result_bytes <= MAX_RESULT_BYTES:
            raise FrontError(INVALID_REQUEST, "MAX_RESULT_BYTES_INVALID")


class ResultUnavailable(Exception):
    """Typed transport failure: the result cannot be serialized within the accepted cap; ZERO result bytes."""
    code = "RESULT_ENVELOPE_UNAVAILABLE_WITHIN_CAP"


@dataclass
class FrontResult:
    outcome: str
    reason: str
    question: str
    knagg: str
    unresolved_action: str
    navigation: dict[str, Any] | None = None
    content: dict[str, Any] | None = None
    orientation: dict[str, Any] | None = None
    rows: dict[str, Any] | None = None
    way_home: dict[str, Any] | None = None
    counters: dict[str, Any] = field(default_factory=dict)
    state_status: str = ""
    persisted: str = ""
    assertion: str = "SYNTHETIC_TEST_ONLY"
    missing_port: str | None = None
    max_result_bytes: int = MAX_RESULT_BYTES

    def __post_init__(self) -> None:
        assert self.outcome in OUTCOMES

    def to_dict(self) -> dict[str, Any]:
        return {"schema": "bk07.read_front_result/v1", "authority": "NONE", "assertion": self.assertion,
                "outcome": self.outcome, "reason": self.reason, "question": self.question, "knagg": self.knagg,
                "navigation": self.navigation, "content": self.content, "orientation": self.orientation,
                "rows": self.rows, "way_home": self.way_home, "unresolved_action": self.unresolved_action,
                "missing_port": self.missing_port, "state_status": self.state_status, "persisted": self.persisted,
                "wake": "NONE", "chat_message_injected": False, "pm_x9_disposition": "NOT_TOUCHED",
                "counters": self.counters}

    def to_json(self) -> bytes:
        data = canonical(self.to_dict())
        if len(data) > self.max_result_bytes:
            raise ResultUnavailable(ResultUnavailable.code)
        return data

    def receipt(self) -> bytes:
        """Separate <= 2048 B receipt: ids, hashes, counters; never content, principal or scope values."""
        nav = self.navigation or {}
        cur = nav.get("current") or {}
        c = self.content or {}
        doc = {"schema": "bk07.read_front_receipt/v1", "assignment_id": "EXT-CLAUDE-BK07-DRIVE-READ-REUSE-V01",
               "authority": "NONE", "assertion": self.assertion, "question": self.question, "knagg": self.knagg,
               "outcome": self.outcome, "reason": self.reason, "file_id": cur.get("file_id"),
               "revision": cur.get("revision"), "full_sha256": c.get("full_sha256"),
               "slice_sha256": c.get("slice_sha256"), "reuse_status": c.get("reuse_status"),
               "orientation_status": (self.orientation or {}).get("status"),
               "rows": {k: v for k, v in (self.rows or {}).items() if k in ("outcome", "reason", "manifest_revision")},
               "row_changes": _count_changes(self.rows), "state_status": self.state_status,
               "persisted": self.persisted, "missing_port": self.missing_port,
               "unresolved_action": self.unresolved_action, "counters": self.counters}
        data = canonical(doc)
        if len(data) > MAX_RECEIPT_BYTES:
            doc["counters"] = {k: self.counters.get(k) for k in ("provider_describes", "content_opens",
                                                                  "envelope_bytes", "elapsed_ms")}
            data = canonical(doc)
        if len(data) > MAX_RECEIPT_BYTES:
            raise ResultUnavailable("RECEIPT_OVER_2048_BYTES")
        return data


def _count_changes(rows: Mapping[str, Any] | None) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in (rows or {}).get("changes", []):
        out[c["change"]] = out.get(c["change"], 0) + 1
    return out


# ====================================================================================================================
# read_front
# ====================================================================================================================
class _Op:
    def __init__(self, ports: ReadFrontPorts, limits: SliceLimits) -> None:
        self.ports = ports
        self.t0 = ports.clock()
        self.deadline = self.t0 + min(limits.max_deadline_seconds, MAX_OPERATION_SECONDS)
        self.c: dict[str, Any] = {"registry_lookups": 0, "registry_bytes": 0, "provider_describes": 0,
                                  "content_opens": 0, "read_calls": 0, "bytes_hashed": 0,
                                  "connector_observations": 0, "entries_found": 0, "entries_invalidated": 0,
                                  "repeated_identical_reads": 0, "manifest_reads": 0, "rows_in_scope": 0,
                                  "delivered_text_chars": 0, "delivered_text_utf8_bytes": 0, "envelope_bytes": 0,
                                  "state_read_bytes": 0, "state_write_bytes": 0, "state_ms": 0.0, "elapsed_ms": 0.0,
                                  "backend_bytes": "UNKNOWN", "tokens": "UNKNOWN", "model_starts": "UNKNOWN",
                                  "spend": "UNKNOWN", "net_saving": "UNKNOWN"}

    def expired(self) -> bool:
        return self.ports.clock() >= self.deadline

    def absorb(self, counters) -> None:
        self.c["provider_describes"] += counters.provider_calls
        self.c["content_opens"] += counters.content_opens
        self.c["read_calls"] += counters.read_calls
        self.c["bytes_hashed"] += counters.bytes_hashed


def _external(op: _Op, fn, *args, **kwargs):
    if op.expired():
        raise FrontError(UNREADABLE, "OPERATION_DEADLINE_EXPIRED")
    try:
        return fn(*args, **kwargs)
    finally:
        if op.expired():
            raise FrontError(UNREADABLE, "OPERATION_DEADLINE_EXPIRED")


def _final_access(op: _Op, baseline: AccessContext | None) -> None:
    try:
        current = _external(op, op.ports.access) if op.ports.access else None
    except FrontError:
        raise
    except Exception:
        current = None
    if (not isinstance(baseline, AccessContext) or not isinstance(current, AccessContext)
            or not baseline.qualified(op.ports.wall_clock()) or not current.qualified(op.ports.wall_clock())
            or current.key() != baseline.key()):
        raise FrontError(DENIED, "FINAL_ACCESS_EXPIRED_CHANGED_OR_UNKNOWN")


def _slice_request(entry: NavEntry, req: FrontRequest) -> SliceRequest:
    sel = entry.selector or {}
    try:
        if set(sel) != {"kind", "start", "end", "expected_slice_sha256"}:
            raise RequestInvalid("SELECTOR_SHAPE")
        selector = Selector(kind=sel["kind"], start=sel["start"], end=sel["end"],
                            expected_slice_sha256=sel["expected_slice_sha256"])
        return SliceRequest(content_ref=entry.content_ref, expected_revision=entry.revision,
                            expected_content_sha256=entry.full_sha256, subject_ref=req.subject_ref,
                            owner_ref=entry.owner_ref, way_home=entry.way_home, selector=selector, limits=req.limits)
    except (RequestInvalid, TypeError):
        raise FrontError(FALLBACK_REQUIRED, "REGISTRY_SELECTOR_NOT_A_VALID_CAP26_REQUEST") from None


def _descriptor_view(d: ReadDescriptor) -> dict[str, Any]:
    return {"canonical_ref": d.canonical_ref, "owner_ref": d.owner_ref, "revision": d.revision, "sha256": d.sha256,
            "way_home": d.way_home, "head_relation": d.head_relation, "snapshot_identity": d.snapshot_identity,
            "provider": d.provider, "assertion": d.assertion, "authorization_basis": d.authorization_basis,
            "owner_seq": d.owner_seq}


def _result_descriptor_view(res) -> dict[str, Any]:
    p = res.provenance or {}
    return {"canonical_ref": res.request["content_ref"], "owner_ref": res.observed_owner_ref,
            "revision": res.observed_revision, "sha256": res.descriptor_content_sha256, "way_home": res.way_home,
            "head_relation": (res.currentness or {}).get("head_relation"),
            "snapshot_identity": p.get("snapshot_identity"), "provider": p.get("provider"),
            "assertion": p.get("assertion"), "authorization_basis": p.get("authorization_basis"),
            "owner_seq": res.owner_seq}


_CAP26_MAP = {C_DENIED: DENIED, C_FALLBACK: FALLBACK_REQUIRED, C_HASH: INVALIDATED, C_DRIFT: INVALIDATED,
              "IDENTITY_MISMATCH": INVALIDATED, "UNREADABLE": UNREADABLE, "LIMIT_EXCEEDED": FALLBACK_REQUIRED,
              "INVALID_REQUEST": FALLBACK_REQUIRED}


def _map_cap26(res) -> str:
    if res.outcome == C_STALE:
        return SUPERSEDED if (res.currentness or {}).get("head_relation") == "SUPERSEDED" else STALE
    return _CAP26_MAP.get(res.outcome, UNREADABLE)


def _deliver(op: _Op, text: str) -> None:
    """Delivered text is counted from the FINAL result in read_front.finish (negatives deliver nothing)."""
    return None


def _delivered_texts(kw: Mapping[str, Any]) -> list[str]:
    out = []
    if kw.get("content") and kw["content"].get("slice_text") is not None:
        out.append(kw["content"]["slice_text"])
    if kw.get("orientation") and kw["orientation"].get("projection_text") is not None:
        out.append(kw["orientation"]["projection_text"])
    for c in (kw.get("rows") or {}).get("changes", []):
        if "content" in c:
            out.append(c["content"])
    return out


def _content_canonical(entry: NavEntry, req: FrontRequest, ports: ReadFrontPorts, state: ReadFrontState, op: _Op,
                       access: AccessContext | None) -> tuple[str, str, dict[str, Any] | None, str]:
    """Returns (outcome, reason, content_view, unresolved_action)."""
    if ports.cap26 is None:
        raise FrontError(MISSING_PORT, "CAP26_PROVIDER_PORT_MISSING")
    sreq = _slice_request(entry, req)
    binding = entry.binding()
    now = ports.wall_clock()
    acc_ok = access is not None and access.qualified(now)
    stored = state.entry(req.knagg)

    # ---------------- hit attempt (<= 2 describes, shared deadline, local hash check) ----------------
    if stored and stored.get("valid") and stored.get("slice_text") is not None:
        why = None
        if stored.get("binding") != binding:
            why = "REGISTRY_BINDING_CHANGED"
        elif not acc_ok:
            why = "ACCESS_UNATTESTED_OR_EXPIRED"
        elif stored.get("access") != access.key():
            why = "PRINCIPAL_SCOPE_OR_PROVIDER_CONTEXT_CHANGED"
        elif hashlib.sha256(stored["slice_text"].encode("utf-8")).hexdigest() != stored.get("slice_sha256") \
                or stored.get("slice_sha256") != (entry.selector or {}).get("expected_slice_sha256"):
            why = "CACHED_SLICE_HASH_CHECK_FAILED"
        if why is not None:
            state.invalidate_entry(req.knagg, why)
            op.c["entries_invalidated"] += 1
            # pre-describe invalidation: ONE cold bounded read below (no extra describe spent yet)
        else:
            views = []
            for i in range(MAX_DESCRIBES):
                if op.expired():
                    state.invalidate_entry(req.knagg, "OPERATION_DEADLINE")
                    op.c["entries_invalidated"] += 1
                    return UNREADABLE, "OPERATION_DEADLINE_EXPIRED", None, "CALLER_DECIDES_ONE_NEXT_READ"
                try:
                    res, d = _external(op, ports.cap26.describe_only, sreq)
                except ResultEnvelopeUnavailable:
                    return UNREADABLE, "CAP26_RESULT_ENVELOPE_UNAVAILABLE", None, "RAISE_RESULT_CAP_OR_FULL_ORIGINAL"
                op.absorb(res.counters)
                if d is None:
                    out = _map_cap26(res)
                    state.invalidate_entry(req.knagg, "DESCRIBE_" + res.reason)
                    op.c["entries_invalidated"] += 1
                    return out, f"HIT_DESCRIBE_{i + 1}:{res.reason}", None, "CALLER_DECIDES_ONE_NEXT_READ"
                views.append(_descriptor_view(d))
                if views[-1] != stored.get("descriptor") or d.head_relation != SAME or d.revision != entry.revision:
                    state.invalidate_entry(req.knagg, "DESCRIPTOR_CHANGED")
                    op.c["entries_invalidated"] += 1
                    out = SUPERSEDED if entry.revision in d.superseded_revisions else INVALIDATED
                    return (out, f"STALE_DESCRIPTOR_AT_DESCRIBE_{i + 1}", None,
                            "NEXT_CALL_DOES_ONE_COLD_BOUNDED_READ")
            if op.expired():
                state.invalidate_entry(req.knagg, "OPERATION_DEADLINE")
                op.c["entries_invalidated"] += 1
                return UNREADABLE, "OPERATION_DEADLINE_EXPIRED_BEFORE_OUTPUT", None, "CALLER_DECIDES_ONE_NEXT_READ"
            text = stored["slice_text"]
            if hashlib.sha256(text.encode("utf-8")).hexdigest() != stored["slice_sha256"]:   # re-check before output
                state.invalidate_entry(req.knagg, "CACHED_SLICE_HASH_CHECK_FAILED")
                return INVALIDATED, "CACHED_SLICE_HASH_CHECK_FAILED", None, "NEXT_CALL_DOES_ONE_COLD_BOUNDED_READ"
            _deliver(op, text)
            op.c["repeated_identical_reads"] += 1
            stored["last_outcome"] = REUSED_SLICE
            state.put_entry(req.knagg, stored)
            return REUSED_SLICE, "FRESH_AUTHORIZED_METADATA_UNCHANGED_AND_ATTESTED_ACCESS_SAME", {
                "slice_text": text, "slice_sha256": stored["slice_sha256"], "full_sha256": stored["descriptor"]["sha256"],
                "selector": entry.selector, "reuse_status": REUSE_QUALIFIED,
                "currentness": {"head_relation": SAME, "semantic_currentness": "UNKNOWN",
                                "basis": views[-1]["assertion"]}}, "NONE"

    # ---------------- cold canonical read through the existing CAP26 resolver ----------------
    if op.expired():
        return UNREADABLE, "OPERATION_DEADLINE_EXPIRED_BEFORE_COLD_READ", None, "CALLER_DECIDES_ONE_NEXT_READ"
    try:
        res = _external(op, ports.cap26.resolve, sreq)
    except ResultEnvelopeUnavailable:
        return UNREADABLE, "CAP26_RESULT_ENVELOPE_UNAVAILABLE", None, "RAISE_RESULT_CAP_OR_FULL_ORIGINAL"
    op.absorb(res.counters)
    if res.outcome != SLICE_READY:
        out = _map_cap26(res)
        if stored is not None and state.invalidate_entry(req.knagg, "COLD_" + res.reason):
            op.c["entries_invalidated"] += 1
        action = {FALLBACK_REQUIRED: "EXPLICIT_CAP26_FULL_ORIGINAL_WITH_BUDGET", DENIED: "OWNER_GRANTS_ACCESS_OR_STOP",
                  STALE: "OWNER_UPDATES_REGISTRY_BINDING", SUPERSEDED: "OWNER_UPDATES_REGISTRY_BINDING"}.get(
                      out, "CALLER_DECIDES_ONE_NEXT_READ")
        return out, "CAP26:" + res.reason, None, action
    if op.expired():
        return UNREADABLE, "OPERATION_DEADLINE_EXPIRED_AFTER_COLD_READ", None, "CALLER_DECIDES_ONE_NEXT_READ"
    _deliver(op, res.slice_text)
    reuse = REUSE_QUALIFIED if acc_ok else REUSE_UNQUALIFIED
    new_entry = {"valid": True, "binding": binding, "descriptor": _result_descriptor_view(res),
                 "slice_sha256": res.slice_sha256, "last_outcome": FRESH_SLICE,
                 "access": access.key() if acc_ok else None}
    if acc_ok:
        new_entry["slice_text"] = res.slice_text            # small verified selection only, never the original
    state.put_entry(req.knagg, new_entry)
    return FRESH_SLICE, "CAP26_VERIFIED_EXACT_SLICE", {
        "slice_text": res.slice_text, "slice_sha256": res.slice_sha256, "full_sha256": res.verified_content_sha256,
        "selector": entry.selector, "reuse_status": reuse,
        "currentness": {"head_relation": (res.currentness or {}).get("head_relation"),
                        "semantic_currentness": "UNKNOWN", "basis": (res.provenance or {}).get("assertion")}}, \
        ("NONE" if acc_ok else "ATTESTED_ACCESS_SCOPE_NEEDED_FOR_CROSS_CALL_REUSE")


def _orientation(entry: NavEntry, ports: ReadFrontPorts, state: ReadFrontState, op: _Op, knagg: str
                 ) -> dict[str, Any] | None:
    """Fresh DNI orientation from trusted connector observations. Never authenticated CURRENT, never reused as body."""
    if ports.dni is None or ports.dni_orient is None:
        return None
    obs = _external(op, ports.dni, entry.file_id)
    if obs is None:
        return {"status": "NO_OBSERVATION", "authority": "ORIENTATION_ONLY", "reuse_status": REUSE_UNQUALIFIED}
    find, paragraph = obs
    op.c["connector_observations"] += 2
    try:
        o = _external(op, ports.dni_orient, find, paragraph, expected_document_id=entry.file_id, expected_revision=entry.revision,
                             expected_tab_id=entry.tab_id, max_chars=2000, include_text=True)
    except ValueError as exc:
        _invalidate_orientation(state, knagg)
        return {"status": "INVALIDATED", "reason": "DNI:" + str(exc), "authority": "ORIENTATION_ONLY",
                "reuse_status": REUSE_UNQUALIFIED}
    stored = state.entry(knagg) or {}
    prev = stored.get("orientation")
    if o["status"] != "REVISION_MATCHED":
        _invalidate_orientation(state, knagg)
        return {"status": "STALE", "reason": "DNI_REVISION_DIFFERS", "authority": "ORIENTATION_ONLY",
                "document_id": entry.file_id, "reuse_status": REUSE_UNQUALIFIED, "action": o.get("action")}
    anchor_seen = find.get("resolvedRange", {}).get("matchedText")
    if entry.anchor_text is not None and anchor_seen != entry.anchor_text:
        _invalidate_orientation(state, knagg)
        return {"status": "INVALIDATED", "reason": "ANCHOR_TEXT_NOT_REFOUND", "authority": "ORIENTATION_ONLY",
                "reuse_status": REUSE_UNQUALIFIED}
    loc = {"tab_id": o["locator"]["tab_id"], "start_index": o["locator"]["start_index"],
           "end_index": o["locator"]["end_index"], "index_domain": "DOCS_UTF16_INDEX_NOT_UTF8_BYTES",
           "revision": o["paragraph_revision"]}
    relocated = prev is not None and (prev.get("revision") != loc["revision"] or prev.get("start_index")
                                      != loc["start_index"] or prev.get("end_index") != loc["end_index"])
    bm = {**loc, "projection_sha256": o["projection_sha256"], "anchor_seen_sha256":
          hashlib.sha256(str(anchor_seen).encode("utf-8")).hexdigest()}
    stored.setdefault("valid", False)
    stored["orientation"] = bm                               # navigation hint only
    state.put_entry(knagg, stored)
    text = o["bounded_text_projection"]
    _deliver(op, text)
    return {"status": ORIENTATION_ONLY, "authority": "ORIENTATION_ONLY", "document_id": entry.file_id,
            "locator": loc, "relocated_since_last_bookmark": relocated, "projection_text": text,
            "projection_sha256": o["projection_sha256"], "production_revalidated": False,
            "backend_full_document_hydrated": "UNKNOWN", "anchor_uniqueness": o["anchor_uniqueness"],
            "reader_issued_receipt": "UNSUPPORTED_BY_PRESENT_CONNECTOR", "reuse_status": REUSE_UNQUALIFIED,
            "way_home": o["way_home"]}


def _invalidate_orientation(state: ReadFrontState, knagg: str) -> None:
    e = state.entry(knagg)
    if e is not None and "orientation" in e:
        e.pop("orientation")
        state.put_entry(knagg, e)


def read_front(request: FrontRequest, ports: ReadFrontPorts, state: ReadFrontState) -> FrontResult:
    op = _Op(ports, request.limits)
    op.c["state_read_bytes"] = state.read_bytes
    access = None
    pending_scope = None
    try:
        human_active = bool(_external(op, ports.human_active)) if ports.human_active else False
    except FrontError:
        human_active = False

    def finish(outcome, reason, action, **kw) -> FrontResult:
        def strip_body():
            kw.pop("content", None)
            if kw.get("orientation"):
                kw["orientation"] = {k:v for k,v in kw["orientation"].items() if k != "projection_text"}
            if kw.get("rows"):
                kw["rows"] = {**kw["rows"], "changes":[{k:v for k,v in c.items() if k != "content"}
                                                       for c in kw["rows"]["changes"]]}
        try:
            if op.expired():raise FrontError(UNREADABLE, "OPERATION_DEADLINE_EXPIRED")
            if outcome in CONTENT_OUTCOMES:_final_access(op, access)
        except FrontError as fe:
            outcome, reason, action = fe.outcome, fe.reason, "CALLER_DECIDES_ONE_NEXT_READ"
            state.invalidate_entry(request.knagg, reason)
            _invalidate_orientation(state, request.knagg)
        if outcome not in CONTENT_OUTCOMES:strip_body()
        op.c["human_turn_active_observed"] = human_active
        res = FrontResult(outcome=outcome, reason=reason, question=request.question, knagg=request.knagg,
                          unresolved_action=action, counters=op.c, state_status=state.status,
                          persisted="NOT_ATTEMPTED", assertion=request.assertion,
                          max_result_bytes=request.max_result_bytes, **kw)
        previous_rows = state.rows(pending_scope) if pending_scope else None
        def refresh():
            texts = _delivered_texts(res.to_dict())
            op.c["delivered_text_chars"] = sum(len(t) for t in texts)
            op.c["delivered_text_utf8_bytes"] = sum(len(t.encode("utf-8")) for t in texts)
            op.c["elapsed_ms"] = round((ports.clock()-op.t0)*1000,3)
            op.c["state_write_bytes"] = state.write_bytes
            op.c["state_ms"] = round(state.ms,3)
            for _ in range(3):op.c["envelope_bytes"] = len(canonical(res.to_dict()))
        refresh()
        serializable = True
        try:res.to_json()
        except ResultUnavailable:serializable = False
        if serializable and res.outcome in CONTENT_OUTCOMES and pending_scope:
            delivered = state.rows(pending_scope)
            if delivered and delivered.get("valid"):
                for change in (res.rows or {}).get("changes",[]):
                    if "content" in change and change.get("message_id") in delivered['messages']:
                        delivered['messages'][change['message_id']]['delivered_digest'] = change['digest']
                state.put_rows(pending_scope,delivered)
        def publish_check():
            if op.expired():raise FrontError(UNREADABLE,"OPERATION_DEADLINE_EXPIRED_BEFORE_PERSISTENCE_OR_EMISSION")
            if res.outcome in CONTENT_OUTCOMES:_final_access(op,access)
            refresh()
            if serializable:
                res.to_json()
                # Reserve the final persistence/counter representation WITHOUT
                # clipping actual measurements or prematurely marking delivery.
                probe=res.to_dict()
                probe['persisted']='NOT_PERSISTED:STATE_WRITE_FAILED'
                probe['counters']={**op.c, 'state_ms':max(state.ms+6000.999,6000.999),
                                   'elapsed_ms':6000.999,'envelope_bytes':MAX_RESULT_BYTES}
                for _ in range(3):probe['counters']['envelope_bytes']=len(canonical(probe))
                if len(canonical(probe))>request.max_result_bytes:
                    raise ResultUnavailable(ResultUnavailable.code)
        try:
            if not op.expired():res.persisted = state.commit(check=publish_check)
        except (FrontError,ResultUnavailable) as exc:
            if pending_scope and previous_rows is not None:state.put_rows(pending_scope,previous_rows)
            state.invalidate_entry(request.knagg,"FINAL_PUBLISH_REJECTED")
            _invalidate_orientation(state,request.knagg)
            res.outcome = exc.outcome if isinstance(exc,FrontError) else UNREADABLE
            res.reason = exc.reason if isinstance(exc,FrontError) else ResultUnavailable.code
            res.content = None
            if res.orientation:res.orientation={k:v for k,v in res.orientation.items() if k!='projection_text'}
            if res.rows:res.rows={**res.rows,'changes':[{k:v for k,v in c.items() if k!='content'} for c in res.rows['changes']]}
            res.persisted="ROLLED_BACK_FINAL_PUBLISH_REJECTED"
        except StateBusy as exc:res.persisted=str(exc)
        except StateBoundExceeded as exc:res.persisted="NOT_PERSISTED:"+str(exc)
        except OSError:res.persisted="NOT_PERSISTED:STATE_WRITE_FAILED"
        refresh()
        return res

    if ports.registry is None:
        return finish(MISSING_PORT, "NAVIGATION_REGISTRY_PORT_MISSING", "A1_SUPPLIES_OWNER_REGISTRY_EXPORT",
                      missing_port="registry")
    # ---------------- A: registry -> exact current original ----------------
    try:
        op.c["registry_lookups"] += 1
        data = _external(op, ports.registry)
        op.c["registry_bytes"] = len(data)
        reg = parse_navigation_registry(data)
        entry, chain = resolve_current(reg, request.knagg)
        op.c["entries_found"] = len([e for e in reg.entries if e.knagg == request.knagg])
    except FrontError as fe:
        if state.invalidate_entry(request.knagg, "REGISTRY:" + fe.reason):
            op.c["entries_invalidated"] += 1
        return finish(fe.outcome, fe.reason, "OWNER_SUPPLIES_ONE_CURRENT_REFERENT")
    navigation = {"registry_revision": reg.revision, "registry_assertion": reg.assertion, "chains": chain,
                  "current": {"file_id": entry.file_id, "content_ref": entry.content_ref, "owner_ref": entry.owner_ref,
                              "revision": entry.revision, "representation": entry.representation,
                              "tab_id": entry.tab_id, "semantic_anchor": entry.semantic_anchor,
                              "owner_status": entry.status},
                  "historical": sorted({c["original"] for c in chain} - {entry.file_id})}
    way_home = {"owner": entry.owner_ref, "evidence": entry.evidence_ref, "full_original": entry.way_home,
                "full_original_command": "CAP26 full-original (explicit budget/action)"}

    # ---------------- B: known channel scope rows (fresh bounded manifest) ----------------
    rows_view = None
    if ports.manifest is not None:
        op.c["manifest_reads"] += 1
        try:
            manifest = _external(op, ports.manifest)
        except Exception:  # noqa: BLE001
            manifest = None
        scope = request.scope["scope"]
        pending_scope = scope
        rows_view, new_rows = process_manifest(manifest, request.scope, state.rows(scope))
        if new_rows is not None:
            state.put_rows(scope, new_rows)
        op.c["rows_in_scope"] = len([c for c in rows_view["changes"] if c.get("row") is not None])
        for c in rows_view["changes"]:
            if "content" in c:
                _deliver(op, c["content"])

    # ---------------- C: content ----------------
    access = None
    if ports.access is not None:
        try:
            access = _external(op, ports.access)
        except Exception:  # noqa: BLE001 - unknown access is not attested access
            access = None
    try:
        if entry.representation == REP_NATIVE_DOCS:
            if entry.selector is not None:
                raise FrontError(FALLBACK_REQUIRED, "UTF16_DOC_INDEX_IS_NOT_UTF8_BYTE_RANGE")
            orient = _orientation(entry, ports, state, op, request.knagg)
            if orient is None:
                return finish(MISSING_PORT, "DNI_OBSERVATION_PORT_MISSING", "A1_SUPPLIES_TRUSTED_DNI_OBSERVATIONS",
                              navigation=navigation, rows=rows_view, way_home=way_home, missing_port="dni")
            outcome = ORIENTATION_ONLY if orient["status"] == ORIENTATION_ONLY else (
                STALE if orient["status"] == "STALE" else INVALIDATED)
            return finish(outcome, "DNI_" + orient.get("reason", orient["status"]),
                          "NONE" if outcome == ORIENTATION_ONLY else "CALLER_DECIDES_ONE_NEXT_READ",
                          navigation=navigation, orientation=orient, rows=rows_view, way_home=way_home)
        if entry.content_ref is None or entry.full_sha256 is None:
            raise FrontError(FALLBACK_REQUIRED, "CANONICAL_EXPORT_HASH_OR_REF_NOT_DOCUMENTED")
        if ports.cap26 is None:
            orient = _orientation(entry, ports, state, op, request.knagg)
            if orient is not None and orient["status"] == ORIENTATION_ONLY:
                return finish(ORIENTATION_ONLY, "CAP26_PORT_MISSING_ORIENTATION_ONLY",
                              "SELECTED_OWNER_SUPPLIES_CAP26_CONTENT_PROVIDER", navigation=navigation,
                              orientation=orient, rows=rows_view, way_home=way_home, missing_port="cap26_provider")
            return finish(MISSING_PORT, "CAP26_PROVIDER_PORT_MISSING", "SELECTED_OWNER_SUPPLIES_CAP26_CONTENT_PROVIDER",
                          navigation=navigation, rows=rows_view, way_home=way_home, missing_port="cap26_provider")
        outcome, reason, content, action = _content_canonical(entry, request, ports, state, op, access)
        orient = _orientation(entry, ports, state, op, request.knagg) if outcome in CONTENT_OUTCOMES else None
        return finish(outcome, reason, action, navigation=navigation, content=content, orientation=orient,
                      rows=rows_view, way_home=way_home)
    except FrontError as fe:
        if state.invalidate_entry(request.knagg, fe.reason):
            op.c["entries_invalidated"] += 1
        return finish(fe.outcome, fe.reason, "OWNER_FIXES_REGISTRY_REPRESENTATION",
                      navigation=navigation, rows=rows_view, way_home=way_home)


# ====================================================================================================================
# trusted local config -> ports (fixture or live-observation mode)
# ====================================================================================================================
def load_dni_orient(import_root: str | os.PathLike[str]) -> Callable[..., dict]:
    """Import the EXISTING DNI reader-v2 adapter from an explicit root; exact bytes pinned (no fork)."""
    path = Path(import_root) / "reader_v2_adapter.py"
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != DNI_ADAPTER_SHA256:
        raise FrontError(MISSING_PORT, "DNI_ADAPTER_HASH_MISMATCH")
    spec = importlib.util.spec_from_file_location("dni_reader_v2_adapter_pinned", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.orient_paragraph


def _read_bounded(path: str, cap: int) -> bytes:
    with open(path, "rb") as fh:
        data = fh.read(cap + 1)
    if len(data) > cap:
        raise FrontError(UNREADABLE, "INPUT_FILE_OVER_BOUND")
    return data


def _path(cfg: Mapping[str, Any], *keys: str) -> str | None:
    cur: Any = cfg
    for k in keys:
        if type(cur) is not dict or k not in cur:
            return None
        cur = cur[k]
    if cur is None:
        return None
    if type(cur) is not str or not cur or len(cur) > 4096 or "\x00" in cur:
        raise FrontError(INVALID_REQUEST, "CONFIG_PATH_INVALID:" + ".".join(keys))
    return cur


def build_from_config(cfg: Mapping[str, Any], question: str) -> tuple[FrontRequest, ReadFrontPorts, str | None]:
    if type(cfg) is not dict or cfg.get("schema") != "bk07.read_front_config/v1":
        raise FrontError(INVALID_REQUEST, "CONFIG_SCHEMA")
    if cfg.get("enabled") is not True:
        raise FrontError(DISABLED, "DEFAULT_OFF_ENABLED_NOT_TRUE")
    mode = cfg.get("mode")
    if mode not in ("FIXTURE", "LIVE_OBSERVATIONS"):
        raise FrontError(INVALID_REQUEST, "CONFIG_MODE")
    questions = cfg.get("questions") or {}
    if type(question) is not str or not _QUESTION.match(question) or question not in questions:
        raise FrontError(INVALID_REQUEST, "QUESTION_NOT_CONFIGURED")
    req = FrontRequest(question=question, knagg=questions[question], subject_ref=cfg.get("subject_ref"),
                       scope=cfg.get("known_scope") or {}, assertion="SYNTHETIC_TEST_ONLY" if mode == "FIXTURE"
                       else "LIVE_OBSERVATION_INPUTS_UNAUTHENTICATED")
    ports = ReadFrontPorts()
    reg = _path(cfg, "registry_path")
    if reg:
        ports.registry = lambda: _read_bounded(reg, MAX_REGISTRY_BYTES)
    man = _path(cfg, "manifest_path")
    if man:
        ports.manifest = lambda: _strict_json(_read_bounded(man, 262144))
    cap = cfg.get("cap26")
    if cap is not None:
        if mode != "FIXTURE" or cap.get("provider") != "LOCAL_PINNED_SNAPSHOT_FIXTURE":
            # live mode NEVER falls back to the synthetic local provider
            raise FrontError(MISSING_PORT, "CAP26_LIVE_PROVIDER_NOT_AVAILABLE")
        provider = LocalPinnedSnapshotProvider(_path(cfg, "cap26", "root"), _path(cfg, "cap26", "registry_path"))
        ports.cap26 = ContextSliceResolver(provider, clock=provider.clock)
        ports.clock = provider.clock
        ports.provider_stats = provider.stats
    obs = cfg.get("dni_observations")
    dni_root = _path(cfg, "import_roots", "dni_reader_v2")
    if obs is not None and dni_root:
        ports.dni_orient = load_dni_orient(dni_root)

        def dni(doc_id: str):
            o = obs.get(doc_id) if type(obs) is dict else None
            if o is None:
                return None
            return (_strict_json(_read_bounded(_path(o, "find"), 16384)),
                    _strict_json(_read_bounded(_path(o, "paragraph"), 16384)))
        ports.dni = dni
    acc = cfg.get("access")
    if type(acc) is dict:
        ctx = AccessContext(principal_ref=acc.get("principal_ref"), scope_fingerprint=acc.get("scope_fingerprint"),
                            provider_context=acc.get("provider_context"),
                            attestation=acc.get("attestation", "NONE"), expires_at=acc.get("expires_at"))
        ports.access = lambda: ctx
    flag = _path(cfg, "human_turn_flag_path")
    if flag:
        ports.human_active = lambda: os.path.exists(flag)
    return req, ports, _path(cfg, "state_path")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="signalvev_sensing.bk07_read_front")
    ap.add_argument("--config", required=True)
    ap.add_argument("--question", required=True)
    ap.add_argument("--receipt", required=True)
    a = ap.parse_args(argv)
    if os.path.lexists(a.receipt):
        print(json.dumps({"outcome": INVALID_REQUEST, "reason": "RECEIPT_EXISTS_NO_OVERWRITE"}), file=sys.stderr)
        return 64
    try:
        cfg = _strict_json(_read_bounded(a.config, 65536))
        req, ports, state_path = build_from_config(cfg, a.question)
    except FrontError as fe:
        action = {DISABLED: "A1_ENABLES_IN_TRUSTED_CONFIG_AFTER_ADMISSION",
                  MISSING_PORT: "SELECTED_OWNER_SUPPLIES_LIVE_CAP26_PROVIDER"}.get(fe.outcome, "FIX_TRUSTED_CONFIG")
        print(json.dumps({"schema": "bk07.read_front_result/v1", "authority": "NONE", "outcome": fe.outcome,
                          "reason": fe.reason, "unresolved_action": action, "wake": "NONE"}, sort_keys=True))
        return 3 if fe.outcome in (DISABLED, MISSING_PORT) else 2
    except (OSError, ValueError):
        print(json.dumps({"outcome": INVALID_REQUEST, "reason": "CONFIG_UNREADABLE"}), file=sys.stderr)
        return 64
    state = ReadFrontState.load(state_path)
    res = read_front(req, ports, state)
    try:
        out = res.to_json()
    except ResultUnavailable as exc:
        print(json.dumps({"outcome": UNREADABLE, "reason": exc.code}), file=sys.stderr)
        out = None
    with open(a.receipt, "xb") as fh:
        fh.write(res.receipt())
    if out is not None:
        sys.stdout.buffer.write(out + b"\n")
    if out is None:
        return 2
    if res.outcome in CONTENT_OUTCOMES:
        return 0
    return 3 if res.outcome in (MISSING_PORT, FALLBACK_REQUIRED) else 2


if __name__ == "__main__":
    sys.exit(main())
