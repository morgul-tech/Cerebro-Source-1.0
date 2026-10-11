"""CAP26 thin adapter: the EXISTING OwnerResolver.resolve(ResolveRequest) -> ResolverResult over ContextSliceResolver.

A bounded, explicit registry maps an opaque pointer_ref (never a path) to ONE pinned content binding + exact selector.
The event's owner/referent/pointer must equal the binding exactly; anything else fails closed (ResolverUnavailable,
which the existing receiver turns into HOLD_UNREADABLE -- one call, no retry).

Digest domains: request.expected_sha256 and result.observed_sha256 are the FULL canonical source digest; the slice
digest, selector, provenance, currentness and Way Home travel inside ``grounding`` only.
REVISION_CHECK: one fresh authorized describe, ZERO content opens, grounding=None.
POINTER_GROUND: a verified slice; grounding fits the existing MAX_GROUNDING_BYTES.
The existing receiver keeps every acceptance decision (source, referent, relation, seq, digest); this adapter never
fabricates SAME + expected hash: a digest conflict is returned as observed so the receiver sees it.
owner_seq is set only when the provider attests it. authority NONE; no wake, send, model call or owner mutation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .context_slice import (HASH_MISMATCH, SLICE_READY, STALE, ContextSliceResolver, RequestInvalid, Selector,
                            SliceLimits, SliceRequest, ResultEnvelopeUnavailable)
from .context_slice_provider import SAME, SUPERSEDED, UNKNOWN
from .model import DEPTH_POINTER_GROUND, DEPTH_REVISION_CHECK, MAX_GROUNDING_BYTES, canonical, is_id
from .resolver import ResolveRequest, ResolverResult, ResolverUnavailable

MAX_POINTER_BINDINGS = 64


@dataclass(frozen=True)
class PointerBinding:
    pointer_ref: str               # opaque known key (the event's delta.ref), not a path
    owner_ref: str
    referent_type: str
    referent_id: str
    content_ref: str
    way_home: str
    selector: Selector
    limits: SliceLimits = SliceLimits()

    def __post_init__(self) -> None:
        for name in ("pointer_ref", "owner_ref", "referent_type", "referent_id", "content_ref", "way_home"):
            if not is_id(getattr(self, name)):
                raise RequestInvalid(f"BINDING_{name.upper()}")


class ContextSliceOwnerResolver:
    """Implements the existing ``OwnerResolver`` protocol. Opt-in: constructed explicitly with its bindings."""

    def __init__(self, resolver: ContextSliceResolver, *, subject_ref: str, bindings: list[PointerBinding]) -> None:
        if not is_id(subject_ref):
            raise RequestInvalid("SUBJECT_REF")
        if len(bindings) > MAX_POINTER_BINDINGS:
            raise RequestInvalid("BINDINGS_OVER_BOUND")
        self._r, self._subject = resolver, subject_ref
        self._by_pointer: dict[str, PointerBinding] = {}
        self._by_referent: dict[tuple[str, str, str], PointerBinding] = {}
        for b in bindings:
            key = (b.owner_ref, b.referent_type, b.referent_id)
            if b.pointer_ref in self._by_pointer or key in self._by_referent:
                raise RequestInvalid("BINDING_DUPLICATE")
            self._by_pointer[b.pointer_ref] = b
            self._by_referent[key] = b
        self.last_result = None                    # inspectable: the last SliceResult (no body after failure)

    def _binding(self, request: ResolveRequest) -> PointerBinding:
        if request.depth == DEPTH_POINTER_GROUND:
            b = self._by_pointer.get(request.pointer_ref or "")
        elif request.depth == DEPTH_REVISION_CHECK and request.pointer_ref is None:
            b = self._by_referent.get((request.owner_ref, request.referent_type, request.referent_id))
        else:
            b = None
        if b is None:
            raise ResolverUnavailable("CAP26_NO_BINDING_FOR_POINTER_OR_DEPTH")
        if (b.owner_ref, b.referent_type, b.referent_id) != (request.owner_ref, request.referent_type,
                                                             request.referent_id):
            raise ResolverUnavailable("CAP26_BINDING_IDENTITY_MISMATCH")
        return b

    def _slice_request(self, request: ResolveRequest, b: PointerBinding) -> SliceRequest:
        try:
            return SliceRequest(content_ref=b.content_ref, expected_revision=request.expected_revision,
                                expected_content_sha256=request.expected_sha256, subject_ref=self._subject,
                                owner_ref=b.owner_ref, way_home=b.way_home, selector=b.selector, limits=b.limits,
                                allow_unknown_head_relation=False)
        except RequestInvalid as exc:
            raise ResolverUnavailable(f"CAP26_REQUEST_INVALID:{exc.code}") from None

    def resolve(self, request: ResolveRequest) -> ResolverResult:
        self.last_result = None  # never expose a prior successful read after a transport failure
        try:
            return self._resolve(request)
        except ResultEnvelopeUnavailable as exc:
            raise ResolverUnavailable(f"CAP26_{exc.code}") from None

    def _resolve(self, request: ResolveRequest) -> ResolverResult:
        b = self._binding(request)
        sreq = self._slice_request(request, b)
        if request.depth == DEPTH_REVISION_CHECK:
            res, d = self._r.describe_only(sreq)
            self.last_result = res
            if d is None:
                raise ResolverUnavailable(f"CAP26_{res.outcome}:{res.reason}")
            relation = ContextSliceResolver.relation_of(request.expected_revision, d)
            return ResolverResult(source_ref=d.owner_ref, referent_type=b.referent_type, referent_id=b.referent_id,
                                  current_revision=d.revision, revision_relation=relation, observed_sha256=d.sha256,
                                  grounding=None, owner_seq=d.owner_seq)
        res = self._r.resolve(sreq)
        self.last_result = res
        if res.outcome == SLICE_READY:
            grounding = {"kind": "CAP26_VERIFIED_SLICE", "original_ref": res.request["content_ref"],
                         "selector": res.selector, "slice_sha256": res.slice_sha256, "slice_bytes": res.slice_bytes,
                         "slice_text": res.slice_text, "provenance": res.provenance,
                         "currentness": res.currentness, "way_home": res.way_home, "authority": "NONE"}
            if len(canonical(grounding)) > MAX_GROUNDING_BYTES:
                raise ResolverUnavailable("CAP26_GROUNDING_OVER_BOUND")
            return ResolverResult(source_ref=_owner(res), referent_type=b.referent_type,
                                  referent_id=b.referent_id, current_revision=res.observed_revision,
                                  revision_relation=res.currentness["head_relation"],
                                  observed_sha256=res.verified_content_sha256, grounding=grounding,
                                  owner_seq=res.owner_seq)
        no_content = {"kind": "CAP26_NO_CONTENT", "outcome": res.outcome, "reason": res.reason,
                      "original_ref": res.request["content_ref"], "way_home": res.way_home, "authority": "NONE"}
        if res.outcome == STALE and res.observed_revision is not None:
            relation = res.currentness["head_relation"] if res.currentness else UNKNOWN
            if relation == SAME:              # descriptor changed between describes: no trustworthy relation
                relation = UNKNOWN
            return ResolverResult(source_ref=_owner(res), referent_type=b.referent_type, referent_id=b.referent_id,
                                  current_revision=res.observed_revision, revision_relation=relation,
                                  observed_sha256=res.descriptor_content_sha256, grounding=no_content,
                                  owner_seq=None)
        if (res.outcome == HASH_MISMATCH and res.reason == "EXPECTED_FULL_SHA256_DIFFERS_FROM_OWNER"
                and res.currentness and res.currentness["head_relation"] == SAME):
            # owner says: same revision, other content. Return it AS OBSERVED -> existing receiver CONFLICT_HOLD.
            return ResolverResult(source_ref=_owner(res), referent_type=b.referent_type, referent_id=b.referent_id,
                                  current_revision=res.observed_revision, revision_relation=SAME,
                                  observed_sha256=res.descriptor_content_sha256, grounding=no_content,
                                  owner_seq=None)
        raise ResolverUnavailable(f"CAP26_{res.outcome}:{res.reason}")


def _owner(res: Any) -> str:
    """source_ref = the owner the PROVIDER described (configured synthetic/authenticated evidence), never a caller
    field; the existing receiver then compares it with the event's declared owner."""
    return res.observed_owner_ref or ""
