"""6. OWNER RESOLVER INTERFACE. One injected selective resolver; this package has NO truth store of its own.

A resolver is anything owner-local that can answer 'what does the owner say now about this referent, at this depth'
(Drive reader-v2 selective grounding, a repo reader, a DB reader ...). It returns bounded evidence; the OWNER decides
the revision relation (revision ids are opaque here and are never ordered by the runtime).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .model import DEPTHS


class ResolverUnavailable(Exception):
    """Owner cannot be read right now (offline, auth failed, timeout). Maps to HOLD_UNREADABLE; never retried here."""


@dataclass(frozen=True)
class ResolveRequest:
    event_id: str
    owner_ref: str
    referent_type: str
    referent_id: str
    depth: str                       # REVISION_CHECK (inline delta) | POINTER_GROUND (pointer)
    pointer_ref: str | None
    expected_revision: str           # opaque
    expected_sha256: str
    owner_seq: int                   # the sequence the EVENT claims (the owner may confirm or refute it)

    def __post_init__(self) -> None:
        assert self.depth in DEPTHS


@dataclass(frozen=True)
class ResolverResult:
    source_ref: str                              # who actually answered; must equal the declared owner_ref
    referent_type: str
    referent_id: str
    current_revision: str                        # opaque
    revision_relation: str                       # SAME | SUPERSEDED | UNKNOWN (owner-judged relation to the event's revision)
    observed_sha256: str                         # owner's fingerprint of the state/section it just read
    grounding: Mapping[str, Any] | None = None   # bounded minimum-sufficient grounding (POINTER_GROUND only); stays home
    candidates: tuple[str, ...] = ()             # >=2 means the owner found the grounding ambiguous
    owner_seq: int | None = None                 # the owner's OWN current seq for the referent at read time; only an
                                                 # owner-confirmed seq may raise the local staleness highwater


class OwnerResolver(Protocol):
    def resolve(self, request: ResolveRequest) -> ResolverResult: ...
