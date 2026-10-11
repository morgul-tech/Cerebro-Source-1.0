"""CAP26 pointer-first context slice: typed request/result, strict parsing, ONE selector kind, bounded verified read.

Opt-in, passive, one-shot. authority NONE. A caller supplies content_ref + opaque pinned revision + full-content
SHA256 + an exact UTF8_BYTE_RANGE selector (with the expected slice SHA256). The resolver asks ONE injected provider
for a fresh authorized descriptor, checks identity/currentness/pins/bounds on metadata BEFORE opening content, reads
one stable snapshot in bounded chunks while hashing ALL bytes, and only after EOF integrity proof (full hash, size,
handle identity) plus a second fresh authorized describe (head/ACL/identity unchanged) returns the slice.

Digest domains (never mixed):
  full content digest  = SHA256 of all canonical raw source bytes        (expected_content_sha256 / verified_content_sha256)
  slice digest         = SHA256 of the returned raw range bytes           (selector.expected_slice_sha256 / slice_sha256)
  result envelope      = canonical JSON (sorted keys, ',' ':' separators, UTF-8, no NaN) -- size-bounded, not hashed
No bytes or newlines are normalized. Opaque revisions are compared for equality only, never ordered.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping

from .context_slice_provider import (SAME, SUPERSEDED, UNKNOWN, ContentSliceProvider, ProviderDeadline,
                                     ProviderDenied, ProviderError, ReadDescriptor)
from .model import AUTHORITY, HEX64, is_id

# ---- closed outcome set -------------------------------------------------------------------------------------------
SLICE_READY = "SLICE_READY"
FULL_ORIGINAL_READY = "FULL_ORIGINAL_READY"
FALLBACK_REQUIRED = "FALLBACK_REQUIRED"
STALE = "STALE"
HASH_MISMATCH = "HASH_MISMATCH"
SELECTOR_DRIFT = "SELECTOR_DRIFT"
DENIED = "DENIED"
IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
UNREADABLE = "UNREADABLE"
INVALID_REQUEST = "INVALID_REQUEST"
LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
OUTCOMES = frozenset({SLICE_READY, FULL_ORIGINAL_READY, FALLBACK_REQUIRED, STALE, HASH_MISMATCH, SELECTOR_DRIFT,
                      DENIED, IDENTITY_MISMATCH, UNREADABLE, INVALID_REQUEST, LIMIT_EXCEEDED})

SELECTOR_UTF8_BYTE_RANGE = "UTF8_BYTE_RANGE"
NEED_SELECTED_RANGE = "SELECTED_RANGE"
NEED_COMPLETE_ORIGINAL = "COMPLETE_ORIGINAL"

# ---- hard maxima (a request may only LOWER these) -----------------------------------------------------------------
HARD = {"max_source_bytes": 1_048_576, "max_slice_bytes": 1024, "max_result_bytes": 4096, "max_provider_calls": 2,
        "max_deadline_seconds": 5.0, "chunk_bytes": 65_536}
MAX_FULL_OUTPUT_BYTES = 1_048_576
MAX_REQUEST_BYTES = 8192
_REQUEST_KEYS = {"content_ref", "expected_revision", "expected_content_sha256", "subject_ref", "owner_ref",
                 "way_home", "selector", "limits"}
_REQUEST_OPTIONAL = {"need", "allow_unknown_head_relation"}
_SELECTOR_KEYS = {"kind", "start", "end", "expected_slice_sha256"}


class ResultEnvelopeUnavailable(ValueError):
    """Typed transport failure: emit ZERO result bytes; caller retains its request/Way Home."""
    code = "RESULT_ENVELOPE_UNAVAILABLE_WITHIN_CAP"


class RequestInvalid(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class SliceLimits:
    max_source_bytes: int = HARD["max_source_bytes"]
    max_slice_bytes: int = HARD["max_slice_bytes"]
    max_result_bytes: int = HARD["max_result_bytes"]
    max_provider_calls: int = HARD["max_provider_calls"]
    max_deadline_seconds: float = HARD["max_deadline_seconds"]
    chunk_bytes: int = HARD["chunk_bytes"]

    def __post_init__(self) -> None:
        for name, hard in HARD.items():
            v = getattr(self, name)
            if name == "max_deadline_seconds":
                if type(v) not in (int, float) or v != v or not 0 < v <= hard:
                    raise RequestInvalid(f"LIMIT_{name.upper()}")
            elif type(v) is not int or not 1 <= v <= hard:
                raise RequestInvalid(f"LIMIT_{name.upper()}")

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class Selector:
    start: int
    end: int
    expected_slice_sha256: str
    kind: str = SELECTOR_UTF8_BYTE_RANGE

    def __post_init__(self) -> None:
        if self.kind != SELECTOR_UTF8_BYTE_RANGE:
            raise RequestInvalid("SELECTOR_KIND_UNSUPPORTED")
        if type(self.start) is not int or type(self.end) is not int or not 0 <= self.start < self.end:
            raise RequestInvalid("SELECTOR_RANGE_INVALID")
        if type(self.expected_slice_sha256) is not str or not HEX64.match(self.expected_slice_sha256):
            raise RequestInvalid("SELECTOR_SLICE_SHA256_INVALID")

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "start": self.start, "end": self.end,
                "expected_slice_sha256": self.expected_slice_sha256}


@dataclass(frozen=True)
class SliceRequest:
    content_ref: str
    expected_revision: str
    expected_content_sha256: str
    subject_ref: str
    owner_ref: str
    way_home: str
    selector: Selector
    limits: SliceLimits = field(default_factory=SliceLimits)
    need: str = NEED_SELECTED_RANGE
    allow_unknown_head_relation: bool = False   # standalone pinned-only read; never receiver-acceptable

    def __post_init__(self) -> None:
        for name in ("content_ref", "expected_revision", "subject_ref", "owner_ref", "way_home"):
            if not is_id(getattr(self, name)):
                raise RequestInvalid(f"FIELD_{name.upper()}")
        if type(self.expected_content_sha256) is not str or not HEX64.match(self.expected_content_sha256):
            raise RequestInvalid("FIELD_EXPECTED_CONTENT_SHA256")
        if not isinstance(self.selector, Selector) or not isinstance(self.limits, SliceLimits):
            raise RequestInvalid("FIELD_TYPES")
        if self.need not in (NEED_SELECTED_RANGE, NEED_COMPLETE_ORIGINAL):
            raise RequestInvalid("FIELD_NEED")
        if type(self.allow_unknown_head_relation) is not bool:
            raise RequestInvalid("FIELD_ALLOW_UNKNOWN_HEAD_RELATION")

    def echo(self) -> dict[str, Any]:
        return {"content_ref": self.content_ref, "expected_revision": self.expected_revision,
                "expected_content_sha256": self.expected_content_sha256, "subject_ref": self.subject_ref,
                "owner_ref": self.owner_ref, "way_home": self.way_home}


def _strict_loads(data: bytes) -> Any:
    def pairs(items):
        out = {}
        for k, v in items:
            if k in out:
                raise RequestInvalid("JSON_DUPLICATE_KEY")
            out[k] = v
        return out

    def no_const(_):
        raise RequestInvalid("JSON_NON_FINITE")
    if len(data) > MAX_REQUEST_BYTES:
        raise RequestInvalid("REQUEST_OVER_BOUND")
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=pairs, parse_constant=no_const)
    except RequestInvalid:
        raise
    except (ValueError, UnicodeDecodeError):
        raise RequestInvalid("JSON_MALFORMED") from None


def parse_request(data: bytes | Mapping[str, Any]) -> SliceRequest:
    """Strict: unknown/duplicate fields, wrong types (incl. bool-as-int), NaN, blank refs, missing constraints."""
    doc = _strict_loads(data) if isinstance(data, (bytes, bytearray)) else data
    if type(doc) is not dict:
        raise RequestInvalid("REQUEST_NOT_OBJECT")
    missing, unknown = _REQUEST_KEYS - set(doc), set(doc) - _REQUEST_KEYS - _REQUEST_OPTIONAL
    if missing:
        raise RequestInvalid("REQUEST_MISSING_FIELD")
    if unknown:
        raise RequestInvalid("REQUEST_UNKNOWN_FIELD")
    sel, lim = doc["selector"], doc["limits"]
    if type(sel) is not dict or set(sel) != _SELECTOR_KEYS:
        raise RequestInvalid("SELECTOR_SHAPE")
    if type(lim) is not dict or set(lim) - set(HARD):
        raise RequestInvalid("LIMITS_SHAPE")
    return SliceRequest(
        content_ref=doc["content_ref"], expected_revision=doc["expected_revision"],
        expected_content_sha256=doc["expected_content_sha256"], subject_ref=doc["subject_ref"],
        owner_ref=doc["owner_ref"], way_home=doc["way_home"],
        selector=Selector(kind=sel["kind"], start=sel["start"], end=sel["end"],
                          expected_slice_sha256=sel["expected_slice_sha256"]),
        limits=SliceLimits(**{**{k: v for k, v in SliceLimits().as_dict().items()}, **lim}),
        need=doc.get("need", NEED_SELECTED_RANGE),
        allow_unknown_head_relation=doc.get("allow_unknown_head_relation", False))


# ---- result -------------------------------------------------------------------------------------------------------
@dataclass
class Counters:
    provider_calls: int = 0          # describe_authorized calls (budget max_provider_calls)
    content_opens: int = 0           # open_verified_snapshot calls (recorded separately, never hidden)
    read_calls: int = 0              # chunks received
    bytes_read: int = 0              # actual bytes received, also on rejection
    bytes_hashed: int = 0
    retries: int = 0                 # always 0: no automatic retry anywhere
    elapsed_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["elapsed_ms"] = round(d["elapsed_ms"], 3)
        return d


@dataclass
class SliceResult:
    outcome: str
    reason: str
    request: dict[str, Any]
    selector: dict[str, Any] | None
    counters: Counters
    observed_revision: str | None = None
    observed_owner_ref: str | None = None          # the owner the provider described (not a caller field)
    descriptor_content_sha256: str | None = None   # owner/registry assertion (metadata)
    verified_content_sha256: str | None = None     # computed over all bytes actually read
    slice_sha256: str | None = None
    slice_text: str | None = None                  # only on SLICE_READY
    slice_bytes: int | None = None
    provenance: dict[str, Any] | None = None
    currentness: dict[str, Any] | None = None
    way_home: str | None = None                    # owner/provider evidence, never the caller's string
    fallback: dict[str, Any] | None = None         # full-original descriptor (no body)
    full_output: dict[str, Any] | None = None      # FULL_ORIGINAL_READY: file + bytes + sha256
    missing_capability: str | None = None
    owner_seq: int | None = None                   # provider-attested only
    authority: str = AUTHORITY
    max_result_bytes: int = HARD["max_result_bytes"]  # local serialization guard; not a wire field

    def __post_init__(self) -> None:
        assert self.outcome in OUTCOMES
        if self.outcome != SLICE_READY:
            assert self.slice_text is None, "no content on any non-SLICE_READY outcome"

    def to_dict(self) -> dict[str, Any]:
        return {"schema": "cap26.slice_result/v1", "outcome": self.outcome, "reason": self.reason,
                "authority": self.authority, "request": self.request, "selector": self.selector,
                "observed_revision": self.observed_revision, "observed_owner_ref": self.observed_owner_ref,
                "descriptor_content_sha256": self.descriptor_content_sha256,
                "verified_content_sha256": self.verified_content_sha256, "slice_sha256": self.slice_sha256,
                "slice_bytes": self.slice_bytes, "slice_text": self.slice_text, "provenance": self.provenance,
                "currentness": self.currentness, "way_home": self.way_home, "owner_seq": self.owner_seq,
                "fallback": self.fallback, "full_output": self.full_output,
                "missing_capability": self.missing_capability, "counters": self.counters.as_dict()}

    def to_json(self) -> bytes:
        data = encode_envelope(self.to_dict())
        if len(data) > self.max_result_bytes:
            raise ResultEnvelopeUnavailable(ResultEnvelopeUnavailable.code)
        return data


def encode_envelope(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


class _Fail(Exception):
    def __init__(self, outcome: str, reason: str) -> None:
        super().__init__(reason)
        self.outcome, self.reason = outcome, reason


def _currentness(d: ReadDescriptor, relation: str) -> dict[str, Any]:
    return {"pinned_identity": "NOT_YET_VERIFIED", "head_relation": relation,
            "semantic_currentness": d.semantic_currentness if d.semantic_currentness == UNKNOWN else UNKNOWN,
            "owner_seq_attested": d.owner_seq is not None, "receiver_acceptable": False,
            "basis": d.assertion}


class ContextSliceResolver:
    """ONE injected provider per resolver. Public API: resolve(SliceRequest) -> SliceResult; full_original(...)."""

    def __init__(self, provider: ContentSliceProvider | None, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._provider = provider
        self._clock = clock

    # ------------------------------------------------------------------ metadata stage (shared)
    def _describe(self, req: SliceRequest, c: Counters, deadline: float) -> ReadDescriptor:
        if self._provider is None:
            raise _Fail(UNREADABLE, "NO_PROVIDER")
        if c.provider_calls >= req.limits.max_provider_calls:
            raise _Fail(LIMIT_EXCEEDED, "PROVIDER_CALL_BUDGET_EXHAUSTED")
        if self._clock() >= deadline:
            raise _Fail(UNREADABLE, "DEADLINE_EXPIRED")
        c.provider_calls += 1
        try:
            d = self._provider.describe_authorized(req.subject_ref, req.content_ref, req.owner_ref, deadline=deadline)
        except ProviderDenied as exc:
            raise _Fail(DENIED, exc.code) from None
        except ProviderDeadline as exc:
            raise _Fail(UNREADABLE, exc.code) from None
        except ProviderError as exc:
            raise _Fail(UNREADABLE, exc.code) from None
        except Exception:  # noqa: BLE001 - a provider bug is unreadable, never success
            raise _Fail(UNREADABLE, "PROVIDER_FAILED") from None
        if self._clock() >= deadline:
            raise _Fail(UNREADABLE, "DEADLINE_EXPIRED_AFTER_DESCRIBE")
        if not isinstance(d, ReadDescriptor):
            raise _Fail(UNREADABLE, "PROVIDER_BAD_DESCRIPTOR")
        return d

    @staticmethod
    def relation_of(req_revision: str, d: ReadDescriptor) -> str:
        """Owner-judged relation of the caller's pinned revision; opaque equality only, never ordering."""
        if req_revision in d.superseded_revisions:
            return SUPERSEDED
        if req_revision != d.revision:
            return UNKNOWN
        return d.head_relation

    def _metadata_checks(self, req: SliceRequest, d: ReadDescriptor, *, need_slice: bool) -> str:
        if (d.canonical_ref, d.owner_ref) != (req.content_ref, req.owner_ref):
            raise _Fail(IDENTITY_MISMATCH, "DESCRIPTOR_REF_OR_OWNER_MISMATCH")
        if d.way_home != req.way_home:
            raise _Fail(IDENTITY_MISMATCH, "WAY_HOME_DISAGREES_WITH_OWNER")
        if not (type(d.size) is int and d.size >= 0 and type(d.sha256) is str and HEX64.match(d.sha256)
                and d.head_relation in (SAME, SUPERSEDED, UNKNOWN)):
            raise _Fail(UNREADABLE, "PROVIDER_BAD_DESCRIPTOR")
        relation = self.relation_of(req.expected_revision, d)
        if relation == SUPERSEDED:
            raise _Fail(STALE, "REVISION_SUPERSEDED_BY_OWNER")
        if relation == UNKNOWN and req.expected_revision != d.revision:
            raise _Fail(STALE, "REVISION_NOT_SERVED_BY_OWNER_RELATION_UNKNOWN")
        if relation == UNKNOWN and not req.allow_unknown_head_relation:
            raise _Fail(STALE, "HEAD_RELATION_UNKNOWN_FAIL_CLOSED")
        if req.expected_content_sha256 != d.sha256:
            raise _Fail(HASH_MISMATCH, "EXPECTED_FULL_SHA256_DIFFERS_FROM_OWNER")
        if d.size > req.limits.max_source_bytes:
            raise _Fail(LIMIT_EXCEEDED, "SOURCE_OVER_MAX_SOURCE_BYTES")
        if need_slice:
            if req.selector.end > d.size:
                raise _Fail(SELECTOR_DRIFT, "SELECTOR_END_BEYOND_SOURCE")
        return relation

    def _fallback(self, req: SliceRequest, d: ReadDescriptor, reason: str) -> dict[str, Any]:
        return {"kind": "FULL_ORIGINAL_REQUIRED", "reason": reason, "canonical_ref": d.canonical_ref,
                "revision": d.revision, "content_sha256": d.sha256, "size": d.size, "owner_ref": d.owner_ref,
                "scope": {"subject_ref": req.subject_ref, "authorization_basis": d.authorization_basis},
                "way_home": d.way_home, "command": "full-original (explicit, separate output budget)",
                "body_inlined": False}

    # ------------------------------------------------------------------ bounded verified read (shared)
    def _read_verified(self, req: SliceRequest, d: ReadDescriptor, c: Counters, deadline: float, *,
                       keep: tuple[int, int] | None, keep_all: bool) -> tuple[bytes, str]:
        """Read ONE stable snapshot. Returns (kept bytes, verified full sha256). Nothing is returned before EOF proof."""
        cap = min(req.limits.max_source_bytes, d.size)
        h = hashlib.sha256()
        kept = bytearray()
        total = 0
        if self._clock() >= deadline:
            raise _Fail(UNREADABLE, "DEADLINE_EXPIRED_BEFORE_OPEN")
        c.content_opens += 1
        try:
            with self._provider.open_verified_snapshot(d, max_bytes=cap, chunk_bytes=req.limits.chunk_bytes,
                                                       deadline=deadline) as snap:
                for chunk in snap.chunks():
                    if c.read_calls >= req.limits.max_source_bytes + 1:
                        raise _Fail(LIMIT_EXCEEDED, "READ_CALL_BUDGET_EXHAUSTED")
                    c.read_calls += 1
                    if type(chunk) is not bytes:
                        raise _Fail(UNREADABLE, "PROVIDER_BAD_CHUNK")
                    c.bytes_read += len(chunk)
                    if len(chunk) > req.limits.chunk_bytes:
                        raise _Fail(LIMIT_EXCEEDED, "PROVIDER_CHUNK_OVER_BOUND")
                    if total + len(chunk) > cap:
                        raise _Fail(LIMIT_EXCEEDED, "SOURCE_LONGER_THAN_DECLARED_OR_CAP")
                    if self._clock() >= deadline:
                        raise _Fail(UNREADABLE, "DEADLINE_EXPIRED_DURING_READ")
                    h.update(chunk)
                    c.bytes_hashed += len(chunk)
                    if keep_all:
                        kept += chunk
                    elif keep is not None:
                        lo, hi = max(keep[0], total), min(keep[1], total + len(chunk))
                        if lo < hi:
                            kept += chunk[lo - total:hi - total]
                    total += len(chunk)
                if self._clock() >= deadline:                       # last read must not slip through success
                    raise _Fail(UNREADABLE, "DEADLINE_EXPIRED_AFTER_LAST_READ")
                if total != d.size:
                    raise _Fail(HASH_MISMATCH, "SOURCE_SIZE_DIFFERS_FROM_DESCRIPTOR")
                if not snap.confirm():
                    raise _Fail(UNREADABLE, "SNAPSHOT_IDENTITY_CHANGED_DURING_READ")
        except _Fail:
            raise
        except ProviderDenied as exc:
            raise _Fail(DENIED, exc.code) from None
        except ProviderError as exc:
            raise _Fail(UNREADABLE, exc.code) from None
        except Exception:  # noqa: BLE001 - interrupted/failed read => no slice, no retry
            raise _Fail(UNREADABLE, "READ_INTERRUPTED") from None
        full = h.hexdigest()
        if full != d.sha256 or full != req.expected_content_sha256:
            raise _Fail(HASH_MISMATCH, "VERIFIED_FULL_SHA256_DIFFERS")
        return bytes(kept), full

    def _recheck(self, req: SliceRequest, first: ReadDescriptor, c: Counters, deadline: float) -> None:
        try:
            second = self._describe(req, c, deadline)
        except _Fail as f:
            raise _Fail(f.outcome, "RECHECK:" + f.reason) from None
        if second != first:
            raise _Fail(STALE, "DESCRIPTOR_CHANGED_DURING_READ")

    # ------------------------------------------------------------------ public: slice
    def resolve(self, req: SliceRequest) -> SliceResult:
        t0 = self._clock()
        deadline = t0 + req.limits.max_deadline_seconds
        c = Counters()
        d: ReadDescriptor | None = None
        relation = None
        self._done(LIMIT_EXCEEDED, "RESULT_ENVELOPE_OVER_MAX_RESULT_BYTES", req, c, t0, None, None)
        try:
            if req.limits.max_provider_calls < 2:
                raise _Fail(LIMIT_EXCEEDED, "PROVIDER_CALL_BUDGET_TOO_SMALL_FOR_RECHECK")
            d = self._describe(req, c, deadline)
            relation = self._metadata_checks(req, d, need_slice=True)
            if req.need == NEED_COMPLETE_ORIGINAL:
                return self._done(FALLBACK_REQUIRED, "TASK_NEEDS_COMPLETE_ORIGINAL", req, c, t0, d, relation,
                                  fallback=self._fallback(req, d, "TASK_NEEDS_COMPLETE_ORIGINAL"))
            span = req.selector.end - req.selector.start
            if span > req.limits.max_slice_bytes:
                return self._done(FALLBACK_REQUIRED, "SELECTOR_OVER_MAX_SLICE_BYTES", req, c, t0, d, relation,
                                  fallback=self._fallback(req, d, "SELECTOR_OVER_MAX_SLICE_BYTES"))
            # The complete no-content fallback must fit BEFORE content is opened.
            self._preflight_final_fallback(req, d, relation)
            raw, full = self._read_verified(req, d, c, deadline, keep=(req.selector.start, req.selector.end),
                                            keep_all=False)
            self._recheck(req, d, c, deadline)
            if len(raw) != span:
                raise _Fail(SELECTOR_DRIFT, "SELECTED_LENGTH_DIFFERS")
            slice_sha = hashlib.sha256(raw).hexdigest()
            if slice_sha != req.selector.expected_slice_sha256:
                raise _Fail(SELECTOR_DRIFT, "SLICE_SHA256_DRIFT")
            try:
                text = raw.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                raise _Fail(SELECTOR_DRIFT, "SELECTOR_SPLITS_UTF8_SEQUENCE") from None
            if self._clock() >= deadline:
                raise _Fail(UNREADABLE, "DEADLINE_EXPIRED_BEFORE_SUCCESS")
            res = self._done(SLICE_READY, "VERIFIED_EXACT_SLICE", req, c, t0, d, relation, verified=full,
                             slice_sha=slice_sha, text=text, nbytes=len(raw))
            return res
        except _Fail as f:
            return self._done(f.outcome, f.reason, req, c, t0, d, relation)

    def _preflight_final_fallback(self, req: SliceRequest, d: ReadDescriptor, relation: str) -> None:
        # Size reservation ONLY, never returned as verification evidence. All digest
        # hex strings have the same encoded length. Reserve the FINAL verified
        # currentness and complete fallback, plus bounded successful-read counters.
        reserve = Counters(provider_calls=req.limits.max_provider_calls, content_opens=1,
                           read_calls=req.limits.max_source_bytes + 1,
                           bytes_read=req.limits.max_source_bytes + req.limits.chunk_bytes,
                           bytes_hashed=req.limits.max_source_bytes, retries=0)
        res = self._done(FALLBACK_REQUIRED, "RESULT_ENVELOPE_OVER_MAX_RESULT_BYTES", req, reserve,
                         self._clock(), d, relation, verified=req.expected_content_sha256,
                         fallback=self._fallback(req, d, "RESULT_ENVELOPE_OVER_MAX_RESULT_BYTES"))
        # A successful final read is deadline-bounded; one extra second reserves
        # serialization/counter formatting. No counter in the real result is clipped.
        reserve.elapsed_ms = (req.limits.max_deadline_seconds + 1) * 1000 + 0.999
        res.to_json()

    def _done(self, outcome: str, reason: str, req: SliceRequest, c: Counters, t0: float,
              d: ReadDescriptor | None, relation: str | None, *, verified: str | None = None,
              slice_sha: str | None = None, text: str | None = None, nbytes: int | None = None,
              fallback: dict[str, Any] | None = None, full_output: dict[str, Any] | None = None,
              missing: str | None = None) -> SliceResult:
        c.elapsed_ms = (self._clock() - t0) * 1000.0
        cur = None
        if d is not None:
            cur = _currentness(d, relation or self.relation_of(req.expected_revision, d))
            if verified is not None:
                cur["pinned_identity"] = "EXACT_FULL_SHA256_VERIFIED"
            cur["receiver_acceptable"] = (outcome == SLICE_READY and cur["head_relation"] == SAME)
        res = SliceResult(
            outcome=outcome, reason=reason, request=req.echo(), selector=req.selector.as_dict(), counters=c,
            observed_revision=d.revision if d else None, observed_owner_ref=d.owner_ref if d else None,
            descriptor_content_sha256=d.sha256 if d else None,
            verified_content_sha256=verified, slice_sha256=slice_sha, slice_text=text, slice_bytes=nbytes,
            provenance=d.provenance() if d else None, currentness=cur, way_home=d.way_home if d else None,
            fallback=fallback, full_output=full_output, missing_capability=missing,
            owner_seq=d.owner_seq if d else None, max_result_bytes=req.limits.max_result_bytes)
        if len(encode_envelope(res.to_dict())) > req.limits.max_result_bytes and outcome == SLICE_READY:
            # Never let the smaller slice turn into an oversized error envelope.
            res = replace(res, outcome=FALLBACK_REQUIRED, reason="RESULT_ENVELOPE_OVER_MAX_RESULT_BYTES",
                          slice_text=None, slice_sha256=None, slice_bytes=None,
                          currentness={**cur, "receiver_acceptable": False},
                          fallback=self._fallback(req, d, "RESULT_ENVELOPE_OVER_MAX_RESULT_BYTES"))
        res.to_json()  # final bound for EVERY outcome, including negative/metadata/full-original
        return res

    # ------------------------------------------------------------------ public: metadata only (REVISION_CHECK)
    def describe_only(self, req: SliceRequest) -> tuple[SliceResult, ReadDescriptor | None]:
        """One fresh authorized describe, ZERO content opens. Used by the OwnerResolver adapter for REVISION_CHECK."""
        t0 = self._clock()
        c = Counters()
        self._done(LIMIT_EXCEEDED, "RESULT_ENVELOPE_OVER_MAX_RESULT_BYTES", req, c, t0, None, None)
        try:
            d = self._describe(req, c, t0 + req.limits.max_deadline_seconds)
        except _Fail as f:
            return self._done(f.outcome, f.reason, req, c, t0, None, None), None
        return self._done(FALLBACK_REQUIRED, "METADATA_ONLY_NO_CONTENT", req, c, t0, d, None,
                          fallback=self._fallback(req, d, "METADATA_ONLY_NO_CONTENT")), d

    # ------------------------------------------------------------------ public: explicit full original
    def full_original(self, req: SliceRequest, *, max_full_output_bytes: int | None,
                      out_path: str | os.PathLike[str] | None) -> SliceResult:
        """Explicit caller choice. Same authorization/currentness/stable-snapshot/source-size checks as resolve().
        Separate output budget (hard max 1 MiB; absent => descriptor only). The verified body is written ONLY after
        EOF integrity proof + fresh recheck, to an explicit caller file that must not exist (no overwrite)."""
        t0 = self._clock()
        deadline = t0 + req.limits.max_deadline_seconds
        c = Counters()
        d: ReadDescriptor | None = None
        relation = None
        self._done(LIMIT_EXCEEDED, "RESULT_ENVELOPE_OVER_MAX_RESULT_BYTES", req, c, t0, None, None)
        try:
            if max_full_output_bytes is not None and (type(max_full_output_bytes) is not int
                                                      or not 1 <= max_full_output_bytes <= MAX_FULL_OUTPUT_BYTES):
                raise _Fail(INVALID_REQUEST, "MAX_FULL_OUTPUT_BYTES_INVALID")
            if req.limits.max_provider_calls < 2:
                raise _Fail(LIMIT_EXCEEDED, "PROVIDER_CALL_BUDGET_TOO_SMALL_FOR_RECHECK")
            d = self._describe(req, c, deadline)
            relation = self._metadata_checks(req, d, need_slice=False)
            if max_full_output_bytes is None or out_path is None:
                return self._done(FALLBACK_REQUIRED, "FULL_OUTPUT_BUDGET_OR_FILE_ABSENT_DESCRIPTOR_ONLY", req, c, t0,
                                  d, relation, fallback=self._fallback(req, d, "DESCRIPTOR_ONLY"),
                                  missing="EXPLICIT_MAX_FULL_OUTPUT_BYTES_AND_OUTPUT_FILE")
            out = Path(out_path)
            if os.path.lexists(out):
                raise _Fail(INVALID_REQUEST, "OUTPUT_FILE_EXISTS_NO_OVERWRITE")
            envelope_reserve = 4096
            if d.size + envelope_reserve > max_full_output_bytes:
                return self._done(LIMIT_EXCEEDED, "SOURCE_EXCEEDS_FULL_OUTPUT_BUDGET", req, c, t0, d, relation,
                                  fallback=self._fallback(req, d, "SOURCE_EXCEEDS_FULL_OUTPUT_BUDGET"),
                                  missing="LARGER_EXPLICIT_FULL_OUTPUT_BUDGET")
            self._preflight_final_fallback(req, d, relation)
            body, full = self._read_verified(req, d, c, deadline, keep=None, keep_all=True)
            self._recheck(req, d, c, deadline)
            if self._clock() >= deadline:
                raise _Fail(UNREADABLE, "DEADLINE_EXPIRED_BEFORE_EMISSION")
            res = self._done(FULL_ORIGINAL_READY, "VERIFIED_COMPLETE_ORIGINAL", req, c, t0, d, relation,
                             verified=full, full_output={"file": out.name, "bytes": len(body), "sha256": full,
                                                         "complete": True})
            try:
                with open(out, "xb") as fh:                      # exclusive create: never overwrite
                    fh.write(body)
            except FileExistsError:
                raise _Fail(INVALID_REQUEST, "OUTPUT_FILE_EXISTS_NO_OVERWRITE") from None
            except OSError:
                try:
                    os.unlink(out)
                except OSError:
                    pass
                raise _Fail(UNREADABLE, "OUTPUT_WRITE_FAILED") from None
            return res
        except _Fail as f:
            return self._done(f.outcome, f.reason, req, c, t0, d, relation)
