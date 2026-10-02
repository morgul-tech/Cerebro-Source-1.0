"""4-7,9,10. RECEIVER: ingress -> applicability -> TTL -> dedupe/staleness -> ONE resolver call -> typed disposition
-> activation decision -> selected return. Order is cheapest-first and every step before the resolver is local-only:

  decode+schema(v0.1/v0.15) -> identity -> APPLICABILITY (NOT_APPLICABLE stops here: no state, no reread, no wake)
  -> TTL (EXPIRED stops, nothing stored) -> dedupe/conflict -> local stale check -> claim -> resolver (exactly once)

A frame is stored in the cursor only after it is verified (schema/identity), applicable and live (TTL). The staleness
highwater advances ONLY on an owner-confirmed ACK_READ, so an unverified signal can never decide what is stale.
The lock covers cursor/recorder/sink access, NOT the owner call: local steps never wait behind an owner reread.
Never: calls a model, retries the resolver, writes Drive/canonical state, or advances past receipt stage READ.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from . import _reference as ref
from .activation import MAX_CANDIDATES, ActivationDecision, decide_activation
from .applicability import ApplicabilityPolicy
from .cursor import RECOVERED, DedupeCursor
from .d0 import (content_fingerprint, decode_frame, idempotency_hash, referent_key, validate_d0,
                 validate_envelope_for_d0)
from .evidence import FlightRecorder
from .model import (ACK_READ, APPLICABILITY_HOLD, APPLICABILITY_NOT_APPLICABLE, APPLIES, CONFLICT_HOLD, DEPTH_POINTER_GROUND,
                    DEPTH_REVISION_CHECK, DETERMINISTIC, DISPOSITIONS, DUPLICATE, EXPIRED, HOLD_APPLICABILITY,
                    HOLD_IDENTITY, HOLD_SCHEMA, HOLD_UNREADABLE, MAX_FUTURE_SKEW_SECONDS, MAX_GROUNDING_BYTES,
                    HEX64, ID_RE, NOT_APPLICABLE, STALE_SUPERSEDED, SUBJECT_MESSAGE_TYPE, Hold, canonical, parse_iso_utc)
from .resolver import OwnerResolver, ResolveRequest, ResolverResult
from .return_sink import ReturnSink, make_closure, select_for_return
from .transport import Transport

SAME, SUPERSEDED = "SAME", "SUPERSEDED"


@dataclass(frozen=True)
class IngressResult:
    event_id: str | None
    disposition: str
    reason: str
    applicability: str | None
    resolver_calls: int
    activation: ActivationDecision
    returned: bool = False
    return_error: str | None = None
    evidence_error: str | None = None      # e.g. CURSOR_FINALIZE_WRITE_FAILED: the verdict stands, its durability does not


class SensingReceiver:
    def __init__(self, *, interests: ApplicabilityPolicy, resolver: OwnerResolver, cursor: DedupeCursor,
                 recorder: FlightRecorder, sink: ReturnSink, clock: Callable[[], float] = time.time,
                 registry: Mapping[str, Any] | None = None) -> None:
        self._interests, self._resolver, self._cursor = interests, resolver, cursor
        self._recorder, self._sink, self._clock = recorder, sink, clock
        self._registry = registry or ref.load_registry()
        self._lock = threading.RLock()         # guards cursor/recorder/sink; admit (check+claim) is atomic; NOT held in resolve()

    def attach(self, transport: Transport) -> None:
        for subject in SUBJECT_MESSAGE_TYPE:
            transport.subscribe(subject, self.on_frame)

    def on_frame(self, data: bytes) -> IngressResult:
        try:
            return self._handle(data)
        except Exception:  # noqa: BLE001 - last line of defence: a hostile frame must never raise out of ingress
            with self._lock:
                return self._reject(None, HOLD_SCHEMA, "INGRESS_UNEXPECTED_ERROR", applicability=None)

    # ---------------------------------------------------------------- pipeline
    def _handle(self, data: bytes) -> IngressResult:
        now = self._clock()
        event_id: str | None = None
        try:
            env, d0 = decode_frame(data)
            eid = d0.get("event_id")
            event_id = eid if isinstance(eid, str) and len(eid) <= 160 else None
            validate_d0(d0)
            validate_envelope_for_d0(env, d0, self._registry)
        except Hold as h:
            with self._lock:
                return self._reject(event_id, h.disposition, h.reason, applicability=None)
        except Exception:  # noqa: BLE001 - wrong types/unicode/depth inside a frame => typed hold, never a crash
            with self._lock:
                return self._reject(event_id, HOLD_SCHEMA, "FRAME_UNPROCESSABLE", applicability=None)

        owner, rtype, rid = d0["owner_ref"], d0["referent"]["type"], d0["referent"]["id"]

        # --- APPLICABILITY: local only. NOT_APPLICABLE leaves no per-event state, no reread, no wake.
        try:
            applicability = self._interests.evaluate(owner, rtype, rid)
        except Exception:  # noqa: BLE001 - a broken policy is a HOLD, never an implicit APPLIES
            applicability = APPLICABILITY_HOLD
        if applicability == APPLICABILITY_NOT_APPLICABLE:
            with self._lock:
                self._recorder.count(NOT_APPLICABLE)
            return IngressResult(d0["event_id"], NOT_APPLICABLE, "NO_LOCAL_INTEREST", applicability, 0,
                                 decide_activation(NOT_APPLICABLE, d0["change_class"]))
        if applicability != APPLIES:
            with self._lock:
                return self._reject(d0["event_id"], HOLD_APPLICABILITY, "APPLICABILITY_UNDECIDABLE",
                                    applicability=APPLICABILITY_HOLD, d0=d0)

        # --- TTL: expired/future frames are never stored and never reach the resolver.
        try:
            issued = parse_iso_utc(env["issued_at"])
        except ValueError:
            with self._lock:
                return self._reject(d0["event_id"], HOLD_SCHEMA, "ISSUED_AT_INVALID", applicability=APPLIES)
        if issued > now + MAX_FUTURE_SKEW_SECONDS:
            with self._lock:
                return self._reject(d0["event_id"], HOLD_SCHEMA, "ISSUED_AT_IN_FUTURE", applicability=APPLIES)
        if now > issued + env["ttl_seconds"]:
            with self._lock:
                return self._reject(d0["event_id"], EXPIRED, "TTL_ELAPSED", applicability=APPLIES, d0=d0)

        # --- ADMIT: dedupe / conflict / local staleness / claim, atomically (the resolver call is OUTSIDE the lock).
        with self._lock:
            early = self._admit(d0, env["payload_hash"])
        if early is not None:
            return early
        return self._resolve_and_close(d0)

    def _admit(self, d0: Mapping[str, Any], d0_hash: str) -> IngressResult | None:
        owner, rtype, rid = d0["owner_ref"], d0["referent"]["type"], d0["referent"]["id"]
        fp, idem, rkey, seq = content_fingerprint(d0), idempotency_hash(d0), referent_key(owner, rtype, rid), d0["owner_seq"]

        seen = self._cursor.get(d0["event_id"])
        if seen is not None:
            if seen["fingerprint"] != fp or seen["d0_hash"] != d0_hash:
                return self._conflict(d0, d0_hash, "SAME_EVENT_ID_CHANGED_FINGERPRINT")
            if seen["reason"] == RECOVERED and not seen["surfaced"]:
                self._cursor.mark_surfaced(d0["event_id"])      # the crash-recovered hold is told to the owner once
                return self._terminal(d0, HOLD_UNREADABLE, RECOVERED, applicability=APPLIES)
            return self._terminal(d0, DUPLICATE, "EVENT_ID_ALREADY_ADMITTED", applicability=APPLIES)
        same_scope = self._cursor.by_idem(idem)
        if same_scope is not None:
            if same_scope["fingerprint"] != fp:
                return self._conflict(d0, d0_hash, "SAME_IDEMPOTENCY_SCOPE_CHANGED_FINGERPRINT")
            return self._terminal(d0, DUPLICATE, "IDEMPOTENCY_SCOPE_ALREADY_ADMITTED", applicability=APPLIES)

        hw = self._cursor.highwater(rkey)        # owner-confirmed only; cheap hint, the owner's reread judges the rest
        if hw is not None and seq == hw["owner_seq"] and fp != hw["fingerprint"]:
            return self._conflict(d0, d0_hash, "OWNER_SEQ_COLLISION")
        stale_locally = hw is not None and seq < hw["owner_seq"]

        try:
            self._cursor.claim(event_id=d0["event_id"], fingerprint=fp, d0_hash=d0_hash, idem_key=idem, referent_key=rkey,
                               owner_seq=seq)
        except Exception:  # noqa: BLE001 - dedupe state that cannot be made durable => refuse the work (fail closed)
            return self._reject(d0["event_id"], HOLD_UNREADABLE, "CURSOR_UNWRITABLE_NO_WORK_DONE", applicability=APPLIES, d0=d0)
        if stale_locally:
            return self._terminal(d0, STALE_SUPERSEDED, "LOCAL_OWNER_SEQ_BEHIND_CONFIRMED_HIGHWATER", applicability=APPLIES,
                                  claimed=True)
        return None

    def _resolve_and_close(self, d0: Mapping[str, Any]) -> IngressResult:
        delta = d0["delta"]
        depth = DEPTH_REVISION_CHECK if delta["kind"] == "INLINE" else DEPTH_POINTER_GROUND
        request = ResolveRequest(event_id=d0["event_id"], owner_ref=d0["owner_ref"], referent_type=d0["referent"]["type"],
                                 referent_id=d0["referent"]["id"], depth=depth, pointer_ref=delta.get("ref"),
                                 expected_revision=d0["revision_after"], expected_sha256=delta["expected_sha256"],
                                 owner_seq=d0["owner_seq"])
        try:
            res = self._resolver.resolve(request)            # EXACTLY ONE call; no retry on any outcome; lock NOT held
        except Exception:  # noqa: BLE001 - ResolverUnavailable and any bug alike fail closed
            with self._lock:
                return self._terminal(d0, HOLD_UNREADABLE, "RESOLVER_UNAVAILABLE_OR_FAILED", applicability=APPLIES,
                                      claimed=True, calls=1)
        try:
            disposition, reason, sha, cands, confirmed = self._judge(d0, depth, res)
        except Exception:  # noqa: BLE001 - a malformed result is a typed hold, never a stuck PENDING claim
            disposition, reason, sha, cands, confirmed = HOLD_UNREADABLE, "RESOLVER_RESULT_UNPROCESSABLE", None, (), False
        with self._lock:
            return self._terminal(d0, disposition, reason, applicability=APPLIES, claimed=True, calls=1,
                                  observed_sha256=sha, candidates=cands, seq_confirmed=confirmed)

    @staticmethod
    def _judge(d0: Mapping[str, Any], depth: str, res: Any) -> tuple[str, str, str | None, tuple[str, ...], bool]:
        if not isinstance(res, ResolverResult) or type(res.candidates) is not tuple:
            return HOLD_UNREADABLE, "RESOLVER_BAD_RESULT", None, (), False
        # Only ids/hashes may travel on into the typed closure (STATE_STAYS_HOME): anything else is a bad result.
        if not (type(res.observed_sha256) is str and HEX64.match(res.observed_sha256)
                and all(type(c) is str and ID_RE.match(c) for c in res.candidates)
                and all(type(x) is str for x in (res.source_ref, res.referent_type, res.referent_id,
                                                 res.current_revision, res.revision_relation))):
            return HOLD_UNREADABLE, "RESOLVER_BAD_RESULT", None, (), False
        if not (res.owner_seq is None or (type(res.owner_seq) is int and res.owner_seq >= 1)):
            return HOLD_UNREADABLE, "RESOLVER_BAD_RESULT", None, (), False
        if (res.referent_type, res.referent_id) != (d0["referent"]["type"], d0["referent"]["id"]):
            return HOLD_IDENTITY, "RESOLVER_REFERENT_MISMATCH", None, (), False
        if d0["delta"]["kind"] == "INLINE":
            # v0.16: an EVENT is not STATE_TRUTH; truth exists only from a reread whose source is the declared owner.
            truth = ref.resolve_state_delta_truth(
                message_type="STATE_DELTA", declared_state_owner_ref=d0["owner_ref"], delta_payload=None,
                reread={"source_ref": res.source_ref, "content": {"revision": res.current_revision,
                                                                  "sha256": res.observed_sha256}})
            if truth["status"] != "TRUTH_ESTABLISHED":
                return HOLD_IDENTITY, f"OWNER_SOURCE_NOT_ESTABLISHED:{truth['reason']}", None, (), False
        elif res.source_ref != d0["owner_ref"]:
            # v0.16 is scoped to STATE_DELTA and fails closed for other types, so the pointer path states the same
            # source-attribution rule directly (registry: current access + custody + byte-hash verification).
            return HOLD_IDENTITY, "OWNER_SOURCE_NOT_ESTABLISHED:REREAD_SOURCE_MISMATCH", None, (), False
        if len(res.candidates) > MAX_CANDIDATES:
            return HOLD_UNREADABLE, "CANDIDATES_OVER_BOUND", None, (), False
        if depth == DEPTH_POINTER_GROUND:
            if res.grounding is None:
                return HOLD_UNREADABLE, "GROUNDING_MISSING", None, (), False
            if len(canonical(dict(res.grounding))) > MAX_GROUNDING_BYTES:
                return HOLD_UNREADABLE, "GROUNDING_OVER_BOUND", None, (), False
        if res.revision_relation == SUPERSEDED:
            return STALE_SUPERSEDED, "OWNER_REPORTS_REVISION_SUPERSEDED", res.observed_sha256, (), False
        if res.revision_relation != SAME:
            return HOLD_UNREADABLE, "REVISION_RELATION_UNKNOWN", None, (), False
        if res.owner_seq is not None and res.owner_seq != d0["owner_seq"]:
            return CONFLICT_HOLD, "OWNER_SEQ_DIFFERS_AT_SAME_REVISION", res.observed_sha256, (), False
        if res.observed_sha256 != d0["delta"]["expected_sha256"]:
            return CONFLICT_HOLD, "OWNER_CONTENT_DIFFERS_AT_SAME_REVISION", res.observed_sha256, (), False
        return ACK_READ, "OWNER_READ_MATCHES_EVENT", res.observed_sha256, res.candidates, res.owner_seq == d0["owner_seq"]

    # ---------------------------------------------------------------- closing helpers
    def _conflict(self, d0: Mapping[str, Any], d0_hash: str, reason: str) -> IngressResult:
        first_time = self._cursor.note_conflict(d0["event_id"], d0_hash)
        return self._terminal(d0, CONFLICT_HOLD, reason, applicability=APPLIES, deliver=first_time)

    def _terminal(self, d0: Mapping[str, Any], disposition: str, reason: str, *, applicability: str,
                  claimed: bool = False, calls: int = 0, observed_sha256: str | None = None,
                  candidates: tuple[str, ...] = (), deliver: bool = True, seq_confirmed: bool = False) -> IngressResult:
        assert disposition in DISPOSITIONS
        failures_before = self._cursor.write_failures
        if claimed:
            self._cursor.finalize(d0["event_id"], disposition, reason, seq_confirmed=seq_confirmed)
        activation = decide_activation(disposition, d0["change_class"], candidates)
        returned, err = False, None
        if deliver and select_for_return(disposition):
            closure = make_closure(event_id=d0["event_id"], owner_ref=d0["owner_ref"], referent_type=d0["referent"]["type"],
                                   referent_id=d0["referent"]["id"], revision_after=d0["revision_after"],
                                   disposition=disposition, reason=reason, observed_sha256=observed_sha256,
                                   activation=activation, way_home=d0["way_home"])
            try:
                self._sink.deliver(closure)
                returned = True
            except Exception as exc:  # noqa: BLE001 - a failed return never changes the disposition, never retried here
                err = type(exc).__name__
        if disposition == DUPLICATE:
            self._recorder.count(DUPLICATE)                 # duplicate floods leave a counter, not a log line
        else:
            self._safe_record("INGRESS", event_id=d0["event_id"], disposition=disposition, reason=_code(reason),
                              applicability=applicability, resolver_calls=calls, activation=activation.decision,
                              owner_seq=d0["owner_seq"], returned=returned)
        evidence_error = "CURSOR_WRITE_FAILED" if self._cursor.write_failures > failures_before else None
        return IngressResult(d0["event_id"], disposition, reason, applicability, calls, activation, returned, err,
                             evidence_error)

    def _reject(self, event_id: str | None, disposition: str, reason: str, *, applicability: str | None,
                d0: Mapping[str, Any] | None = None) -> IngressResult:
        activation = decide_activation(disposition, "MECHANICAL")
        fields: dict[str, Any] = {"disposition": disposition, "reason": _code(reason), "resolver_calls": 0,
                                  "activation": activation.decision}
        if event_id is not None:
            if not self._safe_record("INGRESS_REJECT", event_id=event_id, **fields):   # hostile id => record without it
                self._safe_record("INGRESS_REJECT", **fields)
        else:
            self._safe_record("INGRESS_REJECT", **fields)
        returned = False
        if d0 is not None and select_for_return(disposition):
            closure = make_closure(event_id=d0["event_id"], owner_ref=d0["owner_ref"], referent_type=d0["referent"]["type"],
                                   referent_id=d0["referent"]["id"], revision_after=d0["revision_after"],
                                   disposition=disposition, reason=reason, observed_sha256=None, activation=activation,
                                   way_home=d0["way_home"])
            try:
                self._sink.deliver(closure)
                returned = True
            except Exception:  # noqa: BLE001
                pass
        return IngressResult(event_id, disposition, reason, applicability, 0, activation, returned)


    def _safe_record(self, kind: str, **fields: Any) -> bool:
        """The recorder is evidence, not truth: a failing recorder must never change or hide a verdict."""
        try:
            self._recorder.record(kind, **fields)
            return True
        except Exception:  # noqa: BLE001
            self._recorder.count("RECORDER_WRITE_FAILED")
            return False


def _code(reason: str) -> str:
    return "".join(c if (c.isalnum() or c in "_.:-") else "_" for c in reason)[:120] or "NONE"
