"""PgAdmissionStore: the PostgreSQL-backed admission fence and attempt ledger (isolated candidate, authority NONE).

What is durable and where the atomic boundary is
------------------------------------------------
* ADMISSION is ONE database transaction: (1) look the idempotency key up; (2) lock-and-read the owner's delegation row
  and approval row with ``SELECT ... FOR SHARE`` (owner port, same database); (3) run V0.1's admission checks on what
  was read; (4) INSERT admission + FENCED event + progress; (5) COMMIT. A concurrent owner revoke is an ``UPDATE`` of the
  locked delegation row, so it waits for this transaction (revoke ordered AFTER the fence) or has already committed
  (revoke ordered BEFORE: the read sees REVOKED and denies). There is no gap between read and insert for it to land in.
  A Python lock, or two connections doing read-then-write, are NOT what provides this.
* One admission per idempotency key: UNIQUE(idempotency_key) + ``INSERT ... ON CONFLICT DO NOTHING``; the loser reads
  the winner's committed row and replays it (same digest) or is refused (different basis).
* ATTEMPT RESERVATION is its own short transaction: ``SELECT ... FOR UPDATE`` on the progress row, requires FENCED,
  appends ATTEMPT_STARTED, moves the projection, COMMITs. Only after that COMMIT is confirmed may anyone call a
  provider. No transaction of this store is open while a provider is being called.
* COMMIT that raises is NEVER success: admission -> DENIED ``ADMISSION_COMMIT_NOT_CONFIRMED`` (nothing may execute; an
  identical retry replays if it did commit); reservation -> ``ReservationNotConfirmed`` (no provider call).

The owner must live in the SAME database as this store for the transaction to be shared (see ARCHITECTURE.md). The
synthetic owner in ``synthetic_pg`` does; a production owner in another store would need a different mechanism that is
NOT provided or proven here.

SCOPE: ``scope_ref`` binds every read and write of this store (listing, status, reservation, ledger appends). An
idempotency key is global: another scope presenting an existing key is refused, never replayed.

Connections are opened per operation and closed before returning; nothing is pooled or shared between threads.
"""
from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from controlled_effect_executor.canonical import canonical_text
from controlled_effect_executor.errors import LedgerBasisError
from controlled_effect_executor.receipts import (AdmissionDecision, AdmissionReceipt, LedgerEvent, Provenance,
                                                 reconciliation_problem)
from controlled_effect_executor.spec import BatchSpec
from controlled_effect_executor.states import DENIED, FENCED, IN_FLIGHT, TRANSITIONS, UNKNOWN_EFFECT
from controlled_effect_executor.store import SYNTHETIC_PREFIX, AdmissionRecord, InMemoryAdmissionStore
from controlled_effect_executor.views import ApprovalView, DelegationView

from .errors import CommitNotConfirmed, ReservationNotConfirmed, StateMoved, StorageError
from .pg_libpq import PgError
from .schema import UNRESOLVED_STATES, validate_schema
from .settlement import SETTLED_ABSENT

MAX_PAGE_LIMIT = 500


class OwnerTransactionPort(Protocol):
    """The owner side of the shared transaction. Both methods run on the ADMISSION transaction's cursor and must take a
    row lock that conflicts with the owner's own mutations (``FOR SHARE``/``FOR UPDATE``) so that revoke/amend/withdraw
    are ordered strictly before or strictly after this transaction."""

    def lock_and_read_delegation(self, cur: Any, delegation_ref: str) -> "DelegationView | None": ...

    def lock_and_read_approval(self, cur: Any, approval_ref: str) -> "ApprovalView | None": ...


@dataclass(frozen=True)
class Progress:
    admission_ref: str
    state: str
    last_seq: int
    attempt_ref: "str | None"
    ack_recorded: bool
    batch_digest: str


@dataclass(frozen=True)
class UnresolvedAdmission:
    """Everything a fresh process needs to continue or reconcile one admitted batch WITHOUT the original pointer.
    Returned by a read-only listing; possessing it grants no authority (execute/recover re-verify against the store)."""

    admission_ref: str
    attempt_ref: "str | None"
    state: str
    ack_recorded: bool
    batch_digest: str
    work_order_ref: str
    effect_ref: str
    idempotency_key: str
    scope_ref: str
    admit_seq: int
    spec: "BatchSpec | None"  # None only when the stored row could not be parsed (integrity_ok is then False)
    receipt: "AdmissionReceipt | None"
    integrity_ok: bool


@dataclass(frozen=True)
class RecoveryPage:
    items: tuple
    next_cursor: "str | None"


class _Abort(Exception):
    """Internal: leave the transaction body WITHOUT committing and hand back a ready result."""

    def __init__(self, value: Any) -> None:
        super().__init__("abort")
        self.value = value


class _Snap:
    """Adapts the already-read (and locked) owner rows to the ``read_*`` shape of V0.1's admission checks."""

    def __init__(self, delegation: "DelegationView | None", approval: "ApprovalView | None") -> None:
        self._d, self._a = delegation, approval

    def read_delegation(self, delegation_ref: str) -> "DelegationView | None":
        return self._d

    def read_approval(self, approval_ref: str) -> "ApprovalView | None":
        return self._a


class _CheckShim:
    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock


def _loads(text: str) -> Any:
    return json.loads(text)


def _bool(raw: "str | None") -> bool:
    return raw in ("t", "true", "True")


class PgAdmissionStore:
    def __init__(self, connect: Callable[[], Any], *, schema: str, scope_ref: str, owner: OwnerTransactionPort,
                 clock: Callable[[], float], hooks: "Mapping[str, Callable[[], None]] | None" = None,
                 lock_timeout_ms: int = 5000, statement_timeout_ms: int = 15000) -> None:
        if not (isinstance(scope_ref, str) and scope_ref):
            raise ValueError("scope_ref-required")
        for name, value in (("lock_timeout_ms", lock_timeout_ms), ("statement_timeout_ms", statement_timeout_ms)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name}-invalid")
        self._connect = connect
        self._s = validate_schema(schema)
        self._scope = scope_ref
        self._owner = owner
        self._clock = clock
        self._hooks = dict(hooks or {})
        self._timeouts = (f"{lock_timeout_ms}ms", f"{statement_timeout_ms}ms")  # connection hygiene, NOT a policy choice
        self._denials: list = []
        self._lock = threading.Lock()
        self._writer_key: object = object()
        self._writer_key_issued = False

    # -- plumbing ---------------------------------------------------------------------------------------------------
    @property
    def scope_ref(self) -> str:
        return self._scope

    def _hook(self, name: str) -> None:
        fn = self._hooks.get(name)
        if fn is not None:
            fn()

    @contextmanager
    def _session(self):
        try:
            conn = self._connect()
        except PgError as exc:
            raise StorageError("storage-unavailable", exc.sqlstate) from None
        try:
            cur = conn.cursor()
            cur.execute("SELECT set_config('lock_timeout', %s, false), set_config('statement_timeout', %s, false)",
                        self._timeouts)
            yield cur
        except PgError as exc:
            raise StorageError("storage-error", exc.sqlstate) from None
        finally:
            conn.close()  # closing an open transaction aborts it: nothing stays held after any exit

    def _tx(self, body: Callable[[Any], Any], *, read_only: bool = False) -> Any:
        """Run ``body`` in ONE transaction. Returns only after COMMIT is confirmed; a body that raises ``_Abort`` is
        rolled back and its value returned; a COMMIT that raises is ``CommitNotConfirmed`` (never success)."""
        with self._session() as cur:
            try:
                cur.execute("BEGIN READ ONLY" if read_only else "BEGIN ISOLATION LEVEL READ COMMITTED")
                result = body(cur)
            except _Abort as abort:
                self._rollback(cur)
                return abort.value
            except PgError as exc:
                self._rollback(cur)
                raise StorageError("storage-error", exc.sqlstate) from None
            except BaseException:
                self._rollback(cur)
                raise
            try:
                cur.execute("COMMIT")
            except Exception as exc:  # noqa: BLE001 - any failure here leaves durability unknown
                raise CommitNotConfirmed("commit-not-confirmed", getattr(exc, "sqlstate", "")) from None
            return result

    @staticmethod
    def _rollback(cur: Any) -> None:
        try:
            cur.execute("ROLLBACK")
        except Exception:  # noqa: BLE001
            pass

    def _deny(self, reason: str, digest: "str | None") -> AdmissionDecision:
        with self._lock:
            self._denials.append((reason, digest))
        return AdmissionDecision(DENIED, reason, None, False, digest)

    def denials(self) -> tuple:
        with self._lock:
            return tuple(self._denials)

    # -- the fence --------------------------------------------------------------------------------------------------
    def admit(self, spec: BatchSpec, presented_digest: str) -> AdmissionDecision:
        if not isinstance(spec, BatchSpec):
            return self._deny("SPEC_INVALID", None)
        digest = spec.digest
        if presented_digest != digest:
            return self._deny("DIGEST_MISMATCH", digest)
        for value in (spec.delegation_ref, spec.actor_ref, spec.human_approval_ref, spec.target_identity):
            if not value.startswith(SYNTHETIC_PREFIX):
                return self._deny("NON_SYNTHETIC_REF_REFUSED", digest)
        try:
            return self._tx(lambda cur: self._admit_body(cur, spec, digest))
        except CommitNotConfirmed:
            return self._deny("ADMISSION_COMMIT_NOT_CONFIRMED", digest)
        except StorageError as exc:
            return self._deny({"55P03": "ADMISSION_LOCK_TIMEOUT", "40P01": "ADMISSION_DEADLOCK_ABORTED",
                               "57014": "ADMISSION_STATEMENT_TIMEOUT"}.get(exc.sqlstate, "ADMISSION_STORAGE_ERROR"),
                              digest)

    def _admit_body(self, cur: Any, spec: BatchSpec, digest: str) -> AdmissionDecision:
        s = self._s
        existing = self._by_key(cur, spec.idempotency_key)
        if existing is not None:
            raise _Abort(self._replay_or_conflict(existing, digest))
        delegation = self._owner.lock_and_read_delegation(cur, spec.delegation_ref)
        approval = self._owner.lock_and_read_approval(cur, spec.human_approval_ref)
        reason, view = InMemoryAdmissionStore._check(_CheckShim(self._clock), spec, _Snap(delegation, approval))
        if reason is not None:
            raise _Abort(self._deny(reason, digest))
        assert view is not None
        self._hook("admission.after_owner_read")
        receipt = AdmissionReceipt.seal(
            state=FENCED, batch_ref=spec.batch_ref, batch_digest=digest, operations_digest=spec.operations_digest,
            work_order_ref=spec.work_order_ref, idempotency_key=spec.idempotency_key,
            delegation_ref=spec.delegation_ref, delegation_revision=spec.delegation_revision,
            delegation_currentness=view.owner_currentness, owner_currentness_at_fence=view.owner_currentness,
            delegation_expiry=spec.delegation_expiry, actor_ref=spec.actor_ref, actor_generation=spec.actor_generation,
            approval_ref=spec.human_approval_ref, target_identity=spec.target_identity,
            artifact_version=spec.artifact_version, fenced_at=int(self._clock()))
        cur.execute(
            f"INSERT INTO {s}.cee_admission (admission_ref, scope_ref, idempotency_key, batch_digest, work_order_ref,"
            f" effect_ref, spec_canonical, receipt_canonical, receipt_fingerprint)"
            f" VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING admit_seq",
            (receipt.admission_ref, self._scope, spec.idempotency_key, digest, spec.work_order_ref, spec.effect_ref,
             spec.canonical_text(), canonical_text(receipt.to_dict()), receipt.receipt_fingerprint))
        row = cur.fetchone()
        if row is None:  # a concurrent request committed this key first: read the winner, never insert a second
            winner = self._by_key(cur, spec.idempotency_key)
            if winner is None:
                raise StorageError("admission-conflict-without-winner")
            raise _Abort(self._replay_or_conflict(winner, digest))
        event = LedgerEvent.seal(
            event_kind="ADMISSION_FENCED", seq=1, admission_ref=receipt.admission_ref, batch_digest=digest,
            attempt_ref=None, state_after=FENCED, reason_code="FENCED_ONCE", detail=(),
            prev_fingerprint=receipt.receipt_fingerprint)
        self._insert_event(cur, event)
        cur.execute(
            f"INSERT INTO {s}.cee_progress (admission_ref, scope_ref, admit_seq, last_seq, state, attempt_ref,"
            f" ack_recorded) VALUES (%s, %s, %s, 1, 'FENCED', NULL, false)",
            (receipt.admission_ref, self._scope, int(row["admit_seq"])))
        self._hook("admission.before_commit")
        return AdmissionDecision(FENCED, "FENCED_ONCE", receipt, False, digest)

    def _by_key(self, cur: Any, key: str) -> "dict | None":
        cur.execute(
            f"SELECT a.scope_ref, a.batch_digest, a.receipt_canonical, p.state FROM {self._s}.cee_admission a"
            f" JOIN {self._s}.cee_progress p ON p.admission_ref = a.admission_ref WHERE a.idempotency_key = %s", (key,))
        return cur.fetchone()

    def _replay_or_conflict(self, row: dict, digest: str) -> AdmissionDecision:
        if row["scope_ref"] != self._scope:  # an idempotency key is global; another scope's admission is never replayed
            return self._deny("IDEMPOTENCY_KEY_BELONGS_TO_ANOTHER_SCOPE", digest)
        if row["batch_digest"] != digest:
            return self._deny("IDEMPOTENCY_KEY_CONFLICT", digest)
        receipt = AdmissionReceipt(**_loads(row["receipt_canonical"]))
        if not receipt.verify() or receipt.batch_digest != digest:
            return self._deny("STORED_ADMISSION_INTEGRITY_FAILED", digest)
        return AdmissionDecision(row["state"], "EXACT_REPLAY", receipt, True, digest)

    # -- immutable / projected reads (each its own short READ ONLY transaction) ----------------------------------------
    def progress_of(self, admission_ref: str) -> "Progress | None":
        def body(cur: Any) -> "Progress | None":
            cur.execute(
                f"SELECT p.state, p.last_seq, p.attempt_ref, p.ack_recorded, a.batch_digest FROM {self._s}.cee_progress p"
                f" JOIN {self._s}.cee_admission a ON a.admission_ref = p.admission_ref"
                f" WHERE p.admission_ref = %s AND p.scope_ref = %s", (admission_ref, self._scope))
            r = cur.fetchone()
            return None if r is None else Progress(admission_ref, r["state"], int(r["last_seq"]), r["attempt_ref"],
                                                   _bool(r["ack_recorded"]), r["batch_digest"])
        return self._tx(body, read_only=True)

    def get_admission(self, admission_ref: str) -> "AdmissionRecord | None":
        def body(cur: Any) -> "AdmissionRecord | None":
            cur.execute(f"SELECT spec_canonical, receipt_canonical FROM {self._s}.cee_admission"
                        f" WHERE admission_ref = %s AND scope_ref = %s", (admission_ref, self._scope))
            r = cur.fetchone()
            if r is None:
                return None
            try:
                return AdmissionRecord(BatchSpec.from_mapping(_loads(r["spec_canonical"])),
                                       AdmissionReceipt(**_loads(r["receipt_canonical"])))
            except (KeyError, TypeError, ValueError):
                raise LedgerBasisError("ADMISSION_UNPARSEABLE") from None
        return self._tx(body, read_only=True)

    def state_of(self, admission_ref: str) -> "str | None":
        p = self.progress_of(admission_ref)
        return None if p is None else p.state

    def provenance(self, admission_ref: str) -> Provenance:
        def body(cur: Any) -> Provenance:
            cur.execute(f"SELECT spec_canonical, receipt_canonical FROM {self._s}.cee_admission"
                        f" WHERE admission_ref = %s AND scope_ref = %s", (admission_ref, self._scope))
            a = cur.fetchone()
            if a is None:
                raise LedgerBasisError("ADMISSION_UNKNOWN")
            cur.execute(f"SELECT seq, event_canonical FROM {self._s}.cee_event WHERE admission_ref = %s ORDER BY seq",
                        (admission_ref,))
            events = []
            for r in cur.fetchall():
                try:
                    events.append(_event(_loads(r["event_canonical"])))
                except (KeyError, TypeError, ValueError, AttributeError):
                    raise LedgerBasisError(f"PROVENANCE_UNPARSEABLE:{r['seq']}") from None
            try:
                return Provenance(_loads(a["spec_canonical"]), AdmissionReceipt(**_loads(a["receipt_canonical"])),
                                  tuple(events))
            except (KeyError, TypeError, ValueError):
                raise LedgerBasisError("PROVENANCE_UNPARSEABLE:admission") from None
        return self._tx(body, read_only=True)

    def verify_projection(self, admission_ref: str) -> tuple:
        """Compare the mutable projection with what the immutable ledger says. Returns problem codes (empty == equal).
        Detects a privileged writer that bypassed the triggers; the triggers themselves block everyone else."""
        try:
            prov = self.provenance(admission_ref)
        except LedgerBasisError as exc:  # a detection tool reports bad rows, it does not crash on them
            return (str(exc),)
        prog = self.progress_of(admission_ref)
        problems: list[str] = []
        if prog is None:
            return ("PROGRESS_ROW_MISSING",)
        events = prov.events
        if prog.last_seq != len(events):
            problems.append("PROJECTION_LAST_SEQ")
        if events and prog.state != events[-1].state_after:
            problems.append("PROJECTION_STATE")
        attempt = next((e.attempt_ref for e in events if e.event_kind == "ATTEMPT_STARTED"), None)
        if prog.attempt_ref != attempt:
            problems.append("PROJECTION_ATTEMPT")
        acked = any(e.event_kind == "PROVIDER_RESULT" and dict(e.detail).get("acked") is True for e in events)
        if prog.ack_recorded != acked:
            problems.append("PROJECTION_ACK")
        return tuple(problems)

    # -- bounded, read-only recovery listing ------------------------------------------------------------------------
    def unresolved_high_water(self, *, work_order_ref: "str | None" = None) -> int:
        """Committed unresolved maximum at the start of a read-only sweep."""
        states = ", ".join(f"'{x}'" for x in UNRESOLVED_STATES)
        extra = " AND a.work_order_ref = %s" if work_order_ref is not None else ""
        params = [self._scope] + ([work_order_ref] if work_order_ref is not None else [])

        def body(cur: Any) -> int:
            cur.execute(
                f"SELECT COALESCE(MAX(p.admit_seq), 0) AS high_water FROM {self._s}.cee_progress p"
                f" JOIN {self._s}.cee_admission a ON a.admission_ref = p.admission_ref"
                f" WHERE p.scope_ref = %s AND p.state IN ({states}){extra}", params)
            return int(cur.fetchone()["high_water"])
        return self._tx(body, read_only=True)

    def list_unresolved(self, *, after_cursor: "str | None" = None, limit: int = 100,
                        work_order_ref: "str | None" = None,
                        through_seq: "int | None" = None) -> RecoveryPage:
        """THIS store's scope only; FENCED / IN_FLIGHT / UNKNOWN_EFFECT admissions in admission order, at most ``limit``
        (<= MAX_PAGE_LIMIT) per page. Runs in a READ ONLY transaction: it creates no row, event or authority.

        CURSOR CAVEAT: ``admit_seq`` is assigned at INSERT but becomes visible at COMMIT, so an admission that commits
        late can carry a LOWER seq than rows already listed; a persisted ``next_cursor`` can therefore step past it.
        A recovery sweep must treat the cursor as a paging convenience only and always re-sweep from ``None``
        (resolved admissions drop out of the listing, so the full sweep stays bounded by the unresolved set)."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= MAX_PAGE_LIMIT):
            raise ValueError("limit-out-of-bounds")
        after = 0
        if after_cursor is not None:
            if not (isinstance(after_cursor, str) and after_cursor.isdigit()):
                raise ValueError("cursor-invalid")
            after = int(after_cursor)
        if through_seq is not None and (type(through_seq) is not int or through_seq < 0):
            raise ValueError("sweep-high-water-invalid")
        states = ", ".join(f"'{x}'" for x in UNRESOLVED_STATES)
        extra, params = "", [self._scope, after]
        if through_seq is not None:
            extra += " AND p.admit_seq <= %s"
            params.append(through_seq)
        if work_order_ref is not None:
            extra += " AND a.work_order_ref = %s"
            params.append(work_order_ref)
        params.append(limit)

        def body(cur: Any) -> RecoveryPage:
            cur.execute(
                f"SELECT p.admission_ref, p.state, p.attempt_ref, p.ack_recorded, p.admit_seq, a.batch_digest,"
                f" a.work_order_ref, a.effect_ref, a.idempotency_key, a.spec_canonical, a.receipt_canonical"
                f" FROM {self._s}.cee_progress p JOIN {self._s}.cee_admission a ON a.admission_ref = p.admission_ref"
                f" WHERE p.scope_ref = %s AND p.admit_seq > %s AND p.state IN ({states}){extra}"
                f" ORDER BY p.admit_seq LIMIT %s", params)
            items = []
            for r in cur.fetchall():
                try:
                    spec = BatchSpec.from_mapping(_loads(r["spec_canonical"]))
                    receipt = AdmissionReceipt(**_loads(r["receipt_canonical"]))
                    ok = (spec.digest == r["batch_digest"] and spec.canonical_text() == r["spec_canonical"]
                          and receipt.verify() and receipt.batch_digest == r["batch_digest"]
                          and receipt.admission_ref == r["admission_ref"])
                except (KeyError, TypeError, ValueError, AttributeError):
                    spec, receipt, ok = None, None, False  # one bad row must not hide every healthy one
                items.append(UnresolvedAdmission(
                    admission_ref=r["admission_ref"], attempt_ref=r["attempt_ref"], state=r["state"],
                    ack_recorded=_bool(r["ack_recorded"]), batch_digest=r["batch_digest"],
                    work_order_ref=r["work_order_ref"], effect_ref=r["effect_ref"],
                    idempotency_key=r["idempotency_key"], scope_ref=self._scope, admit_seq=int(r["admit_seq"]),
                    spec=spec, receipt=receipt, integrity_ok=ok))
            nxt = str(items[-1].admit_seq) if len(items) == limit else None
            return RecoveryPage(tuple(items), nxt)
        return self._tx(body, read_only=True)

    # -- the only writes after admission (append-only) ---------------------------------------------------------------
    def issue_writer_key(self) -> object:
        """One-shot in-process capability for the ledger-writing methods; the executor takes it. API boundary only."""
        with self._lock:
            if self._writer_key_issued:
                raise LedgerBasisError("WRITER_KEY_ALREADY_ISSUED")
            self._writer_key_issued = True
            return self._writer_key

    def _require_writer(self, writer_key: object) -> None:
        if writer_key is None or writer_key is not self._writer_key:
            raise LedgerBasisError("WRITER_CAPABILITY_REQUIRED")

    def begin_attempt(self, admission_ref: str, batch_digest: str, *, writer_key: object = None) -> "str | None":
        """FENCED -> IN_FLIGHT exactly once across ALL processes (row lock + state check in one transaction). Returns
        the attempt ref once the COMMIT is confirmed; None if this admission is not FENCED (lost race / already
        started): the caller must then NOT call the provider."""
        self._require_writer(writer_key)
        attempt_ref = "ATT-" + hashlib.sha256((admission_ref + ":attempt:1").encode()).hexdigest()[:24].upper()

        def body(cur: Any) -> "str | None":
            prog = self._lock_progress(cur, admission_ref, batch_digest)
            if prog.state != FENCED:
                raise _Abort(None)
            self._append(cur, prog, "ATTEMPT_STARTED", IN_FLIGHT, "ATTEMPT_RECORDED_BEFORE_PROVIDER_CALL", attempt_ref,
                         batch_digest, None)
            self._hook("attempt.before_commit")
            return attempt_ref
        try:
            return self._tx(body)
        except CommitNotConfirmed:
            raise ReservationNotConfirmed("ATTEMPT_RESERVATION_NOT_CONFIRMED") from None

    def record_provider_result(self, admission_ref: str, attempt_ref: str, batch_digest: str, *, acked: bool,
                               failure_type: "str | None" = None, writer_key: object = None) -> None:
        self._require_writer(writer_key)
        state_after = IN_FLIGHT if acked else UNKNOWN_EFFECT

        def body(cur: Any) -> None:
            prog = self._lock_progress(cur, admission_ref, batch_digest, attempt_ref)
            self._append(cur, prog, "PROVIDER_RESULT", state_after,
                         "PROVIDER_ACK_UNVERIFIED" if acked else "PROVIDER_RESPONSE_NOT_RECEIVED", attempt_ref,
                         batch_digest, {"acked": acked, "failure_type": failure_type})
        self._tx(body)

    def record_reconciliation(self, admission_ref: str, attempt_ref: str, batch_digest: str, *, state_after: str,
                              classification: str, reason_code: str, observation_digest: "str | None",
                              settlement_status: "str | None" = None, settlement_digest: "str | None" = None,
                              basis_reason: "str | None" = None, expected_last_seq: "int | None" = None,
                              writer_key: object = None) -> bool:
        """Append one RECONCILIATION event. Returns False (writing nothing) when the admission is already UNKNOWN_EFFECT
        and this verdict is identical to the ledger head (same reason, classification, evidence digests): periodic
        recovery sweeps must not grow the ledger without bound. A changed observation or settlement is always written."""
        self._require_writer(writer_key)

        def body(cur: Any) -> bool:
            prog = self._lock_progress(cur, admission_ref, batch_digest, attempt_ref)
            if expected_last_seq is not None and prog.last_seq != expected_last_seq:
                raise StateMoved("STATE_MOVED_SINCE_DECISION")
            problem = reconciliation_problem(state_after, classification, reason_code, observation_digest,
                                             prog.ack_recorded)
            if problem:
                raise LedgerBasisError(problem)
            if state_after == "NO_COMMIT" and (settlement_status != SETTLED_ABSENT or not settlement_digest):
                raise LedgerBasisError("NO_COMMIT_REQUIRES_EXPLICIT_SETTLEMENT_EVIDENCE")
            detail = {"classification": classification, "observation_digest": observation_digest}
            if settlement_status is not None:
                detail["settlement_status"] = settlement_status
                detail["settlement_digest"] = settlement_digest
            if basis_reason is not None:
                detail["basis_reason"] = basis_reason
            if prog.state == UNKNOWN_EFFECT and state_after == UNKNOWN_EFFECT:
                cur.execute(f"SELECT event_kind, event_canonical FROM {self._s}.cee_event"
                            f" WHERE admission_ref = %s AND seq = %s", (admission_ref, prog.last_seq))
                head = cur.fetchone()
                if head is not None and head["event_kind"] == "RECONCILIATION":
                    h = _loads(head["event_canonical"])
                    if h.get("reason_code") == reason_code and h.get("detail") == detail:
                        raise _Abort(False)
            self._append(cur, prog, "RECONCILIATION", state_after, reason_code, attempt_ref, batch_digest, detail)
            return True
        return self._tx(body)

    def declare_in_flight_lost(self, admission_ref: str, batch_digest: str, reason_ref: str, *,
                               writer_key: object = None) -> None:
        self._require_writer(writer_key)

        def body(cur: Any) -> None:
            prog = self._lock_progress(cur, admission_ref, batch_digest)
            if prog.state != IN_FLIGHT:
                raise LedgerBasisError("DECLARE_LOST_REQUIRES_IN_FLIGHT")
            self._append(cur, prog, "IN_FLIGHT_DECLARED_LOST", UNKNOWN_EFFECT, "OWNER_DECLARED_IN_FLIGHT_LOST",
                         prog.attempt_ref, batch_digest, {"owner_reason_ref": reason_ref})
        self._tx(body)

    # -- internals ---------------------------------------------------------------------------------------------------
    def _lock_progress(self, cur: Any, admission_ref: str, batch_digest: str,
                       attempt_ref: "str | None" = None) -> Progress:
        cur.execute(f"SELECT state, last_seq, attempt_ref, ack_recorded FROM {self._s}.cee_progress"
                    f" WHERE admission_ref = %s AND scope_ref = %s FOR UPDATE", (admission_ref, self._scope))
        r = cur.fetchone()
        if r is None:
            raise LedgerBasisError("ADMISSION_UNKNOWN")
        cur.execute(f"SELECT batch_digest FROM {self._s}.cee_admission WHERE admission_ref = %s", (admission_ref,))
        stored = cur.fetchone()["batch_digest"]
        if stored != batch_digest:
            raise LedgerBasisError("BASIS_MISMATCH")
        if attempt_ref is not None and attempt_ref != r["attempt_ref"]:
            raise LedgerBasisError("ATTEMPT_MISMATCH")
        return Progress(admission_ref, r["state"], int(r["last_seq"]), r["attempt_ref"], _bool(r["ack_recorded"]),
                        stored)

    def _append(self, cur: Any, prog: Progress, kind: str, state_after: str, reason: str, attempt_ref: "str | None",
                batch_digest: str, detail: "dict | None") -> None:
        if state_after not in TRANSITIONS.get(prog.state, frozenset()):
            raise LedgerBasisError(f"TRANSITION_NOT_ALLOWED:{prog.state}->{state_after}")
        cur.execute(f"SELECT event_fingerprint FROM {self._s}.cee_event WHERE admission_ref = %s AND seq = %s",
                    (prog.admission_ref, prog.last_seq))
        prev = cur.fetchone()
        if prev is None:
            raise LedgerBasisError("LEDGER_HEAD_MISSING")
        event = LedgerEvent.seal(
            event_kind=kind, seq=prog.last_seq + 1, admission_ref=prog.admission_ref, batch_digest=batch_digest,
            attempt_ref=attempt_ref, state_after=state_after, reason_code=reason,
            detail=tuple((detail or {}).items()), prev_fingerprint=prev["event_fingerprint"])
        self._insert_event(cur, event)
        acked = prog.ack_recorded or bool(kind == "PROVIDER_RESULT" and (detail or {}).get("acked") is True)
        cur.execute(
            f"UPDATE {self._s}.cee_progress SET last_seq = %s, state = %s, attempt_ref = COALESCE(attempt_ref, %s),"
            f" ack_recorded = %s WHERE admission_ref = %s AND last_seq = %s",
            (event.seq, state_after, attempt_ref, acked, prog.admission_ref, prog.last_seq))
        if cur.rowcount != 1:
            raise StateMoved("PROGRESS_COMPARE_AND_SET_FAILED")

    def _insert_event(self, cur: Any, ev: LedgerEvent) -> None:
        cur.execute(
            f"INSERT INTO {self._s}.cee_event (admission_ref, seq, event_ref, event_kind, state_after, attempt_ref,"
            f" event_canonical, event_fingerprint, prev_fingerprint) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (ev.admission_ref, ev.seq, ev.event_ref, ev.event_kind, ev.state_after, ev.attempt_ref,
             canonical_text(ev.to_dict()), ev.event_fingerprint, ev.prev_fingerprint))


def _event(d: dict) -> LedgerEvent:
    d = dict(d)
    d["detail"] = tuple(sorted(d["detail"].items()))
    return LedgerEvent(**d)


__all__ = ["MAX_PAGE_LIMIT", "OwnerTransactionPort", "PgAdmissionStore", "Progress", "RecoveryPage",
           "UnresolvedAdmission"]
