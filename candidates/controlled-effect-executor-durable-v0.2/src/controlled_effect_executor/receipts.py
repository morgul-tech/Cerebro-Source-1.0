"""Immutable receipts and the append-only ledger event. Every identity is a recomputable fingerprint.

Same pattern as the existing owner receipts: the ref and the fingerprint are derived from the canonical subject
WITHOUT the ref/fingerprint fields themselves (ref = PREFIX + fingerprint[:24].upper()).
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any

from .canonical import sha256_hex
from .errors import BatchSpecError
from .states import STATES, TRANSITIONS

ADMISSION_SCHEMA = "cerebro-controlled-effect-admission-receipt/v1"
EVENT_SCHEMA = "cerebro-controlled-effect-ledger-event/v1"
EVENT_KINDS = ("ADMISSION_FENCED", "ATTEMPT_STARTED", "PROVIDER_RESULT", "RECONCILIATION", "IN_FLIGHT_DECLARED_LOST")


def _subject(obj: Any, drop: tuple) -> dict:
    return {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj) if f.name not in drop}


@dataclass(frozen=True)
class AdmissionReceipt:
    admission_ref: str
    receipt_fingerprint: str
    state: str  # always FENCED: the receipt is the immutable admission fact; progress lives in the ledger
    batch_ref: str
    batch_digest: str
    operations_digest: str
    work_order_ref: str
    idempotency_key: str
    delegation_ref: str
    delegation_revision: int
    delegation_currentness: int
    owner_currentness_at_fence: int
    delegation_expiry: "int | None"
    actor_ref: str
    actor_generation: int
    approval_ref: str
    target_identity: str
    artifact_version: str
    fenced_at: int
    schema: str = ADMISSION_SCHEMA

    @classmethod
    def seal(cls, **fields: Any) -> "AdmissionReceipt":
        probe = cls(admission_ref="", receipt_fingerprint="", **fields)
        fp = sha256_hex(_subject(probe, ("admission_ref", "receipt_fingerprint")))
        return dataclasses.replace(probe, receipt_fingerprint=fp, admission_ref="ADM-" + fp[:24].upper())

    def verify(self) -> bool:
        try:
            fp = sha256_hex(_subject(self, ("admission_ref", "receipt_fingerprint")))
        except BatchSpecError:
            return False
        return (self.state == "FENCED" and self.schema == ADMISSION_SCHEMA and fp == self.receipt_fingerprint
                and self.admission_ref == "ADM-" + fp[:24].upper())

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class LedgerEvent:
    event_ref: str
    event_fingerprint: str
    event_kind: str
    seq: int
    admission_ref: str
    batch_digest: str
    attempt_ref: "str | None"
    state_after: str
    reason_code: str
    detail: tuple  # sorted tuple of (str, str|int|bool|None) pairs; never exception messages, never secrets
    prev_fingerprint: str
    schema: str = EVENT_SCHEMA

    @classmethod
    def seal(cls, **fields: Any) -> "LedgerEvent":
        if fields["event_kind"] not in EVENT_KINDS or fields["state_after"] not in STATES:
            raise BatchSpecError("ledger-event-vocabulary")
        detail = tuple(sorted(dict(fields.pop("detail", ())).items()))
        probe = cls(event_ref="", event_fingerprint="", detail=detail, **fields)
        fp = sha256_hex(_subject(probe, ("event_ref", "event_fingerprint")))
        return dataclasses.replace(probe, event_fingerprint=fp, event_ref="EVT-" + fp[:24].upper())

    def verify(self) -> bool:
        try:
            fp = sha256_hex(_subject(self, ("event_ref", "event_fingerprint")))
        except BatchSpecError:
            return False
        return fp == self.event_fingerprint and self.event_ref == "EVT-" + fp[:24].upper()

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["detail"] = {k: v for k, v in self.detail}
        return d


def reconciliation_problem(state_after: str, classification: str, reason_code: str, observation_digest: "str | None",
                           acked_before: bool) -> "str | None":
    """Semantic consistency of a RECONCILIATION record. A resolved state must be DERIVED from an authoritative
    observation: the classification fixes the state, the reason code, the presence of an observation digest, and (for
    NO_COMMIT) that no provider ack was ever seen. This proves ledger consistency, not the authenticity of the
    readback (there are no signatures in this candidate)."""
    if classification == "COMMITTED":
        ok = (state_after == "COMMITTED_READBACK" and reason_code == "READBACK_MATCHES_INTENDED_STATE"
              and observation_digest is not None)
    elif classification == "NO_COMMIT":
        ok = (state_after == "NO_COMMIT" and reason_code == "READBACK_PROVES_NO_COMMIT"
              and observation_digest is not None and not acked_before)
    elif classification == "INDETERMINATE":
        ok = state_after == "UNKNOWN_EFFECT"
    else:
        ok = False
    return None if ok else "RECONCILIATION_NOT_DERIVED_FROM_AN_AUTHORITATIVE_OBSERVATION"


@dataclass(frozen=True)
class AdmissionDecision:
    """Result of the admission fence. state is FENCED (new or replayed) / a later progress state on replay / DENIED."""

    state: str
    reason_code: str
    receipt: "AdmissionReceipt | None" = None
    replayed: bool = False
    batch_digest: "str | None" = None


@dataclass(frozen=True)
class ExecutionResult:
    state: str
    reason_code: str
    admission_ref: "str | None"
    attempt_ref: "str | None"
    batch_digest: "str | None"
    attempts_started: int  # 0 or 1, ever, for this admission
    replayed: bool = False
    automatic_retry_allowed: bool = False  # never True: there is no retry policy in this package
    owner_action_required: bool = False  # True for NO_COMMIT / UNKNOWN_EFFECT: the decision returns to the owner


@dataclass(frozen=True)
class Provenance:
    """Everything needed to reconstruct delegation -> batch -> admission -> attempt -> readback/result."""

    spec_basis: dict
    admission: AdmissionReceipt
    events: tuple

    @property
    def state(self) -> str:
        return self.events[-1].state_after


def verify_provenance(p: Provenance) -> tuple:
    """Recompute every identity from scratch. Returns a tuple of problem codes (empty == chain intact)."""
    from .spec import BatchSpec

    problems: list[str] = []
    try:
        spec = BatchSpec.from_mapping(p.spec_basis)
    except Exception:  # noqa: BLE001 - a malformed basis is itself the finding
        return ("SPEC_BASIS_UNPARSEABLE",)
    if spec.basis() != p.spec_basis:
        problems.append("SPEC_BASIS_NOT_CANONICAL")
    a = p.admission
    if not a.verify():
        problems.append("ADMISSION_RECEIPT_FINGERPRINT")
    if a.owner_currentness_at_fence != a.delegation_currentness:
        problems.append("ADMISSION_OWNER_MOVED_BETWEEN_READ_AND_FENCE")
    if a.batch_digest != spec.digest or a.batch_ref != spec.batch_ref:
        problems.append("ADMISSION_BATCH_DIGEST_MISMATCH")
    if (a.delegation_ref, a.delegation_revision, a.actor_ref, a.actor_generation, a.idempotency_key,
        a.approval_ref, a.target_identity, a.artifact_version, a.delegation_expiry) != (
            spec.delegation_ref, spec.delegation_revision, spec.actor_ref, spec.actor_generation,
            spec.idempotency_key, spec.human_approval_ref, spec.target_identity, spec.artifact_version,
            spec.delegation_expiry):
        problems.append("ADMISSION_BASIS_MISMATCH")
    prev_fp, prev_state, acked_seen = a.receipt_fingerprint, None, False
    for i, ev in enumerate(p.events, start=1):
        if not ev.verify():
            problems.append(f"EVENT_FINGERPRINT:{i}")
        if ev.seq != i or ev.prev_fingerprint != prev_fp:
            problems.append(f"EVENT_CHAIN:{i}")
        if ev.admission_ref != a.admission_ref or ev.batch_digest != a.batch_digest:
            problems.append(f"EVENT_BASIS:{i}")
        if i == 1:
            if ev.event_kind != "ADMISSION_FENCED" or ev.state_after != "FENCED":
                problems.append("EVENT_FIRST_NOT_FENCED")
        elif ev.state_after not in TRANSITIONS.get(prev_state, frozenset()):
            problems.append(f"EVENT_TRANSITION:{i}:{prev_state}->{ev.state_after}")
        if ev.event_kind == "RECONCILIATION":
            d = dict(ev.detail)
            if reconciliation_problem(ev.state_after, d.get("classification", ""), ev.reason_code,
                                      d.get("observation_digest"), acked_seen):
                problems.append(f"EVENT_SEMANTICS:{i}")
        if ev.event_kind == "PROVIDER_RESULT":
            acked = dict(ev.detail).get("acked") is True
            acked_seen = acked_seen or acked
            if ev.state_after != ("IN_FLIGHT" if acked else "UNKNOWN_EFFECT"):
                problems.append(f"EVENT_SEMANTICS:{i}")
        prev_fp, prev_state = ev.event_fingerprint, ev.state_after
    if not p.events:
        problems.append("EVENTS_EMPTY")
    return tuple(problems)
