"""NAL-01 candidate: default-off Natural Actor Lifecycle, no live authority.

This module contains the transition contract and an in-memory transactional test
store. Only separately supplied trusted readers can introduce owner/provider
facts. Hashes check exact bytes; they never authenticate an issuer.
"""
from __future__ import annotations

import copy
import hashlib
import json
import threading
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Protocol


SCHEMA = "cerebro.nal.lifecycle/v1"
BINDING_SCHEMA = "cerebro.task-semantics-binding/v1"
TERMINAL_SCHEMA = "cerebro.terminal-occurrence/v1"
OWNER_EVENT_SCHEMA = "cerebro.owner-event/v1"
OUTBOX_INTENT_SCHEMA = "cerebro.nal.owner-event-intent/v1"
HISTORICAL_TERMINAL_SCHEMA = "cerebro.nal.historical-terminal-report/v1"
HISTORICAL_RECEIPT_SCHEMA = "cerebro.nal.historical-close-receipt/v1"


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def hex64(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


class Hold(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class EvidenceReader(Protocol):
    def read(self, kind: str, ref: str) -> dict | None: ...

    # Optional owner-owned capability. It must serialize revocation/supersession
    # with a close until the store transaction has committed or rolled back.
    def owner_commit_fence(self, binding_ref: str): ...


class UnavailableEvidenceReader:
    def read(self, kind: str, ref: str) -> None:
        return None


@dataclass(frozen=True)
class Result:
    status: str
    receipt: dict | None = None
    mutated: bool = False


class MemoryStore:
    """Serializable per-process fixture. Never production persistence."""

    def __init__(self, before_commit: Callable[[], None] | None = None,
                 after_commit: Callable[[], None] | None = None):
        self._states: dict[str, dict] = {}
        self._lock = threading.RLock()
        self.before_commit = before_commit
        self.after_commit = after_commit

    def read(self, actor_ref: str) -> dict | None:
        with self._lock:
            return copy.deepcopy(self._states.get(actor_ref))

    def read_commit_evidence(self, actor_ref: str, terminal_ref: str) -> dict:
        with self._lock:
            state = self._states.get(actor_ref)
            if state is None:
                raise Hold("COMMIT_EVIDENCE_INCOMPLETE")
            terminal = state["terminals"].get(terminal_ref)
            receipt = state["receipts"].get(terminal_ref)
            intent = state["outbox"].get(receipt["event_ref"]) if receipt else None
            if terminal is None or receipt is None or intent is None or \
                    intent["event"].get("receipt_ref") != receipt["receipt_ref"] or \
                    receipt["terminal_digest"] != terminal["terminal_digest"]:
                raise Hold("COMMIT_EVIDENCE_INCOMPLETE")
            return {"terminal": copy.deepcopy(terminal), "receipt": copy.deepcopy(receipt),
                    "outbox_event": copy.deepcopy(intent["event"])}

    def apply(self, actor_ref: str, transition: Callable[[dict | None], tuple[dict, Result]],
              *, owner_fence_factory: Callable | None = None) -> Result:
        with self._lock:
            old = self._states.get(actor_ref)
            fence = owner_fence_factory(copy.deepcopy(old)) if owner_fence_factory else nullcontext(None)
            with fence as commit_guard:
                draft, result = transition(copy.deepcopy(old))
                if result.mutated:
                    if self.before_commit:
                        self.before_commit()
                    if commit_guard:
                        commit_guard()
                    self._states[actor_ref] = copy.deepcopy(draft)
            if result.mutated and self.after_commit:
                self.after_commit()
            return result


class Lifecycle:
    def __init__(self, store, evidence: EvidenceReader | None = None, *, enabled: bool = False):
        self.store = store
        self.evidence = evidence or UnavailableEvidenceReader()
        self.enabled = enabled

    def _on(self) -> None:
        if not self.enabled:
            raise Hold("DEFAULT_OFF")

    def _read(self, kind: str, ref: str) -> dict:
        if not text(ref):
            raise Hold("EVIDENCE_REF_REQUIRED")
        try:
            value = self.evidence.read(kind, ref)
        except Exception as exc:
            raise Hold("TRUSTED_EVIDENCE_READ_UNAVAILABLE") from exc
        if not isinstance(value, dict) or value.get("evidence_ref") != ref or not text(value.get("provider_revision")):
            raise Hold("TRUSTED_EVIDENCE_READ_UNAVAILABLE")
        return copy.deepcopy(value)

    @staticmethod
    def _state(state: dict | None) -> dict:
        if state is None:
            raise Hold("ACTOR_NOT_REGISTERED")
        return state

    @staticmethod
    def _task(state: dict) -> dict:
        task = state["current_task"]
        if task is None:
            raise Hold("NO_CURRENT_TASK")
        return task

    def register_worker_ready(self, actor_ref: str, generation_ref: str, role_ready_ref: str) -> Result:
        self._on()
        e = self._read("ROLE_READY", role_ready_ref)
        if (e.get("actor_ref"), e.get("generation_ref")) != (actor_ref, generation_ref) or \
                e.get("pre_role_lifecycle") != "READY_UNBOUND" or e.get("birth_role", "not-null") is not None or \
                e.get("attached_role") != "WORKER" or e.get("claim_ref") is not None or \
                not all(text(e.get(k)) for k in ("source_revision", "role_projection_fingerprint", "role_attach_receipt")):
            raise Hold("ROLE_NULL_BIRTH_OR_ZERO_CLAIM_READBACK_REQUIRED")

        def tx(state):
            if state is not None:
                if state["role_ready_ref"] == role_ready_ref and state["generation_ref"] == generation_ref and \
                        state["source_revision"] == e["source_revision"] and \
                        state["role_projection_fingerprint"] == e["role_projection_fingerprint"]:
                    return state, Result("REPLAY_WORKER_READY")
                raise Hold("ACTOR_GENERATION_COLLISION")
            state = {"schema": SCHEMA, "actor_ref": actor_ref, "generation_ref": generation_ref,
                     "role": "WORKER", "source_revision": e["source_revision"],
                     "role_projection_fingerprint": e["role_projection_fingerprint"],
                     "role_ready_ref": role_ready_ref, "epoch": 0, "frontier_ref": "NAL-F0-" + digest([actor_ref, generation_ref])[:24],
                     "reuse_ready": True, "last_reset_ref": None, "last_reset_digest": None,
                     "current_task": None, "bindings": {}, "supersessions": {},
                     "terminals": {}, "receipts": {}, "historical_reports": {},
                     "outbox": {}, "closed_tasks": []}
            return state, Result("WORKER_READY_ZERO_CLAIM", mutated=True)
        return self.store.apply(actor_ref, tx)

    def admit_task(self, actor_ref: str, admitted_task_ref: str) -> Result:
        self._on()
        e = self._read("ADMITTED_TASK", admitted_task_ref)
        required = ("task_ref", "attempt_ref", "claim_ref", "packet_ref", "queue_ref", "task_payload_sha256",
                    "admission_receipt", "owner_revision")
        if not all(text(e.get(k)) for k in required) or e.get("admitted") is not True or \
                not hex64(e.get("task_payload_sha256")):
            raise Hold("ADMITTED_TASK_READBACK_REQUIRED")

        def tx(state):
            state = self._state(state)
            if state["current_task"] is not None and \
                    state["current_task"].get("admitted_task_ref") == admitted_task_ref:
                if state["current_task"].get("admitted_evidence_digest") == digest(e):
                    return state, Result("REPLAY_TASK_ADMISSION")
                raise Hold("ADMITTED_TASK_READBACK_CHANGED")
            if state["current_task"] is not None or not state["reuse_ready"]:
                raise Hold("ACTOR_NOT_REUSABLE_OR_TASK_ACTIVE")
            if any(old["task_ref"] == e["task_ref"] for old in state["closed_tasks"]):
                raise Hold("TASK_REF_ALREADY_CLOSED")
            if (e.get("actor_ref"), e.get("generation_ref"), e.get("actor_role"),
                e.get("source_revision"), e.get("role_projection_fingerprint")) != (
                actor_ref, state["generation_ref"], state["role"], state["source_revision"],
                state["role_projection_fingerprint"]):
                raise Hold("ADMITTED_TASK_ACTOR_ROLE_SOURCE_MISMATCH")
            state["current_task"] = {k: e[k] for k in required}
            optional_provenance = ("task_revision", "decision_ref", "provenance_ref")
            present = [k for k in optional_provenance if k in e]
            if present:
                if set(present) != set(optional_provenance) or                         type(e.get("task_revision")) is not int or e["task_revision"] < 1 or                         not text(e.get("decision_ref")) or not text(e.get("provenance_ref")):
                    raise Hold("ADMITTED_TASK_PROVENANCE_INVALID")
                for key in optional_provenance:
                    state["current_task"][key] = e[key]
            state["current_task"].update({"status": "ADMITTED", "admitted_task_ref": admitted_task_ref,
                                          "admitted_evidence_digest": digest(e),
                                          "binding_ref": None,
                                          "consume_ref": None, "consume_evidence_digest": None,
                                          "start_ref": None, "start_evidence_digest": None,
                                          "terminal_ref": None,
                                          "frontier_ref": state["frontier_ref"]})
            state["reuse_ready"] = False
            return state, Result("TASK_ADMITTED", mutated=True)
        return self.store.apply(actor_ref, tx)

    @staticmethod
    def _binding_core(e: dict) -> dict:
        keys = ("schema", "binding_ref", "semantic_ref", "actor_ref", "generation_ref", "task_ref",
                "attempt_ref", "claim_ref", "packet_ref", "queue_ref", "actor_role", "source_revision",
                "role_projection_fingerprint", "task_payload_sha256", "owner_revision",
                "provider_revision", "admission_receipt", "supersedes")
        return {k: e.get(k) for k in keys}

    def _verified_binding(self, state: dict, task: dict, binding_ref: str, supersedes: str | None) -> dict:
        e = self._read("OWNER_BINDING", binding_ref)
        if e.get("schema") != BINDING_SCHEMA or e.get("binding_ref") != binding_ref or \
                e.get("supersedes") != supersedes or e.get("current") is not True or \
                e.get("revoked") is not False or not all(text(e.get(k)) for k in (
                    "semantic_ref", "owner_revision", "issue_receipt", "canonical_fingerprint")):
            raise Hold("OWNER_BINDING_READBACK_REQUIRED")
        if e["semantic_ref"].startswith(("living:", "living://", "session:", "session://")):
            raise Hold("SEMANTIC_POINTER_NOT_AUTHORITY")
        expected = (state["actor_ref"], state["generation_ref"], task["task_ref"], task["attempt_ref"],
                    task["claim_ref"], task["packet_ref"], task["queue_ref"], state["role"],
                    state["source_revision"], state["role_projection_fingerprint"], task["task_payload_sha256"],
                    task["admission_receipt"])
        actual = tuple(e.get(k) for k in ("actor_ref", "generation_ref", "task_ref", "attempt_ref",
                     "claim_ref", "packet_ref", "queue_ref", "actor_role", "source_revision",
                     "role_projection_fingerprint", "task_payload_sha256", "admission_receipt"))
        if actual != expected or e.get("canonical_fingerprint") != digest(self._binding_core(e)):
            raise Hold("OWNER_BINDING_STALE_WRONG_ROLE_OR_FINGERPRINT")
        return e

    def _current_binding(self, state: dict, task: dict) -> None:
        ref = task["binding_ref"]
        if not ref or ref in state["supersessions"]:
            raise Hold("OWNER_BINDING_NOT_CURRENT")
        current = self._verified_binding(state, task, ref, state["bindings"][ref].get("supersedes"))
        if current != state["bindings"][ref]:
            raise Hold("OWNER_BINDING_READBACK_CHANGED")

    def _owner_commit_fence(self, binding_ref: str, *, cursor=None):
        """Require a trusted owner capability, never a local or caller-made lock."""
        factory = getattr(self.evidence, "owner_commit_fence", None)
        if not callable(factory):
            raise Hold("OWNER_COMMIT_FENCE_UNAVAILABLE")
        try:
            fence = factory(binding_ref, cursor=cursor)
        except Hold:
            raise
        except Exception as exc:
            raise Hold("OWNER_COMMIT_FENCE_UNAVAILABLE") from exc
        if not all(callable(getattr(fence, name, None)) for name in
                   ("__enter__", "__exit__", "assert_current")):
            raise Hold("OWNER_COMMIT_FENCE_UNAVAILABLE")
        return fence

    @staticmethod
    def _assert_owner_fence(fence, expected: dict) -> None:
        try:
            fence.assert_current(expected)
        except Hold:
            raise
        except Exception as exc:
            raise Hold("OWNER_COMMIT_FENCE_UNAVAILABLE") from exc

    def issue_binding(self, actor_ref: str, binding_ref: str) -> Result:
        self._on()
        def tx(state):
            state = self._state(state)
            task = self._task(state)
            if task["binding_ref"] == binding_ref and binding_ref in state["bindings"]:
                e = self._verified_binding(state, task, binding_ref, None)
                if e == state["bindings"][binding_ref]:
                    return state, Result("REPLAY_BINDING_ISSUE")
                raise Hold("OWNER_BINDING_READBACK_CHANGED")
            if task["status"] != "ADMITTED" or task["binding_ref"] is not None:
                raise Hold("BINDING_WINDOW_CLOSED")
            e = self._verified_binding(state, task, binding_ref, None)
            if binding_ref in state["bindings"]:
                raise Hold("BINDING_REF_COLLISION")
            state["bindings"][binding_ref] = e
            task["binding_ref"] = binding_ref
            return state, Result("BINDING_ISSUED_READBACK", mutated=True)
        return self.store.apply(actor_ref, tx)

    def supersede_binding(self, actor_ref: str, new_binding_ref: str) -> Result:
        self._on()
        def tx(state):
            state = self._state(state)
            task = self._task(state)
            if task["binding_ref"] == new_binding_ref and new_binding_ref in state["bindings"] and \
                    state["bindings"][new_binding_ref].get("supersedes") in state["supersessions"]:
                old_ref = state["bindings"][new_binding_ref]["supersedes"]
                e = self._verified_binding(state, task, new_binding_ref, old_ref)
                if e == state["bindings"][new_binding_ref] and \
                        state["supersessions"][old_ref]["new_binding_ref"] == new_binding_ref:
                    return state, Result("REPLAY_BINDING_SUPERSESSION")
                raise Hold("OWNER_BINDING_READBACK_CHANGED")
            old_ref = task["binding_ref"]
            if task["status"] != "ADMITTED" or not old_ref:
                raise Hold("BINDING_SUPERSESSION_WINDOW_CLOSED")
            e = self._verified_binding(state, task, new_binding_ref, old_ref)
            if e["owner_revision"] == state["bindings"][old_ref]["owner_revision"] or \
                    new_binding_ref in state["bindings"]:
                raise Hold("BINDING_SUPERSESSION_REVISION_COLLISION")
            state["bindings"][new_binding_ref] = e
            state["supersessions"][old_ref] = {"new_binding_ref": new_binding_ref,
                                               "owner_revision": e["owner_revision"],
                                               "issue_receipt": e["issue_receipt"]}
            task["binding_ref"] = new_binding_ref
            return state, Result("BINDING_SUPERSEDED_READBACK", mutated=True)
        return self.store.apply(actor_ref, tx)

    @staticmethod
    def _worker_match(state: dict, task: dict, e: dict) -> None:
        expected = (state["actor_ref"], state["generation_ref"], task["attempt_ref"],
                    task["claim_ref"], task["packet_ref"], task["queue_ref"], task["binding_ref"])
        actual = tuple(e.get(k) for k in ("actor_ref", "generation_ref", "attempt_ref",
                                     "claim_ref", "packet_ref", "queue_ref", "binding_ref"))
        if actual != expected or not text(e.get("receipt_ref")):
            raise Hold("WORKER_EVIDENCE_BINDING_MISMATCH")

    @staticmethod
    def _terminal_identity(e: dict) -> tuple:
        return tuple(e.get(k) for k in ("terminal_ref", "terminal_digest", "actor_ref",
                     "generation_ref", "attempt_ref", "claim_ref", "packet_ref", "queue_ref",
                     "binding_ref", "result"))

    @staticmethod
    def _transition_replay(state: dict, transition: str, ref: str) -> bool:
        task = state.get("current_task") or {}
        if transition == "WORK_CONSUME":
            return task.get("consume_ref") == ref
        if transition == "WORK_START":
            return task.get("start_ref") == ref
        if transition == "WORK_TERMINAL":
            return any(
                row.get("evidence_ref") == ref
                for row in state.get("terminals", {}).values()
                if isinstance(row, dict)
            )
        return False

    def _transition_authority_fence_factory(self, transition: str, evidence_ref: str):
        @contextmanager
        def authority_fence(snapshot, *, cursor=None):
            snapshot = self._state(snapshot)
            if self._transition_replay(snapshot, transition, evidence_ref):
                yield None
                return
            task = self._task(snapshot)
            binding_ref = task.get("binding_ref")
            expected = snapshot.get("bindings", {}).get(binding_ref)
            if not binding_ref or expected is None:
                # Let the transition's own order/binding validation return the
                # canonical semantic error; no mutation can pass without it.
                yield None
                return
            factory = getattr(self.evidence, "transition_authority_fence", None)
            if callable(factory):
                try:
                    fence = factory(
                        binding_ref,
                        transition=transition,
                        task=copy.deepcopy(task),
                        expected_binding=copy.deepcopy(expected),
                        cursor=cursor,
                    )
                except Hold:
                    raise
                except Exception as exc:
                    raise Hold("PARENT_AUTHORITY_FENCE_UNAVAILABLE") from exc
                if not all(callable(getattr(fence, name, None)) for name in
                           ("__enter__", "__exit__", "assert_current")):
                    raise Hold("PARENT_AUTHORITY_FENCE_UNAVAILABLE")
                with fence:
                    def commit_guard():
                        try:
                            fence.assert_current(copy.deepcopy(expected), transition)
                        except Hold:
                            raise
                        except Exception as exc:
                            raise Hold("FULL_AUTHORITY_CHANGED_BEFORE_COMMIT") from exc
                    yield commit_guard
                return
            # Legacy in-memory tests predate the parent-authority seam. They may
            # retain child-only serialization as regression evidence; any
            # non-memory/store path without a full-chain fence fails closed.
            if isinstance(self.store, MemoryStore):
                with self._owner_commit_fence(binding_ref, cursor=cursor) as fence:
                    yield lambda: self._assert_owner_fence(fence, expected)
                return
            raise Hold("PARENT_AUTHORITY_FENCE_UNAVAILABLE")

        return authority_fence

    def _apply_authorized_transition(self, actor_ref: str, transition: str,
                                     evidence_ref: str, tx) -> Result:
        try:
            return self.store.apply(
                actor_ref,
                tx,
                owner_fence_factory=self._transition_authority_fence_factory(
                    transition, evidence_ref,
                ),
            )
        except Hold as exc:
            if exc.code == "PG_COMMIT_OUTCOME_UNKNOWN":
                raise Hold("HOLD_UNKNOWN") from exc
            raise

    def worker_consume(self, actor_ref: str, consume_ref: str) -> Result:
        self._on()
        e = self._read("WORKER_CONSUME", consume_ref)
        def tx(state):
            state = self._state(state)
            task = self._task(state)
            self._worker_match(state, task, e)
            if task["consume_ref"] == consume_ref:
                if task["consume_evidence_digest"] == digest(e):
                    return state, Result("REPLAY_CONSUME")
                raise Hold("CONSUME_READBACK_CHANGED")
            if task["status"] != "ADMITTED" or not task["binding_ref"]:
                raise Hold("CONSUME_ORDER_OR_BINDING_REQUIRED")
            self._current_binding(state, task)
            task["consume_ref"] = consume_ref
            task["consume_evidence_digest"] = digest(e)
            task["status"] = "CONSUMED"
            return state, Result("WORK_CONSUMED", mutated=True)
        return self._apply_authorized_transition(
            actor_ref, "WORK_CONSUME", consume_ref, tx,
        )

    def worker_start(self, actor_ref: str, start_ref: str) -> Result:
        self._on()
        e = self._read("WORKER_START", start_ref)
        def tx(state):
            state = self._state(state)
            task = self._task(state)
            self._worker_match(state, task, e)
            if task["start_ref"] == start_ref:
                if task["start_evidence_digest"] == digest(e):
                    return state, Result("REPLAY_START")
                raise Hold("START_READBACK_CHANGED")
            if task["status"] != "CONSUMED":
                raise Hold("START_REQUIRES_CONSUME")
            self._current_binding(state, task)
            task["start_ref"] = start_ref
            task["start_evidence_digest"] = digest(e)
            task["status"] = "STARTED"
            return state, Result("WORK_STARTED", mutated=True)
        return self._apply_authorized_transition(
            actor_ref, "WORK_START", start_ref, tx,
        )

    def worker_terminal(self, actor_ref: str, evidence_ref: str) -> Result:
        self._on()
        e = self._read("WORKER_TERMINAL", evidence_ref)
        if e.get("schema") != TERMINAL_SCHEMA or not text(e.get("terminal_ref")) or \
                not hex64(e.get("terminal_digest")) or e.get("result") not in ("PASS", "REFINE", "HOLD"):
            raise Hold("TERMINAL_OCCURRENCE_INVALID")
        def tx(state):
            state = self._state(state)
            task = self._task(state)
            self._worker_match(state, task, e)
            old = state["terminals"].get(e["terminal_ref"])
            if old is not None:
                if self._terminal_identity(old) == self._terminal_identity(e):
                    return state, Result("REPLAY_TERMINAL")
                raise Hold("CONFLICT_TERMINAL_OCCURRENCE")
            if task["status"] != "STARTED":
                raise Hold("TERMINAL_REQUIRES_START")
            self._current_binding(state, task)
            state["terminals"][e["terminal_ref"]] = e
            task["terminal_ref"] = e["terminal_ref"]
            task["status"] = "TERMINAL"
            return state, Result("TERMINAL_OBSERVED", mutated=True)
        return self._apply_authorized_transition(
            actor_ref, "WORK_TERMINAL", evidence_ref, tx,
        )

    def reconcile_transition(self, actor_ref: str, transition: str, evidence_ref: str) -> Result:
        """Authoritative actor-state readback after an ambiguous transition commit."""
        self._on()
        state = self._state(self.store.read(actor_ref))
        task = state.get("current_task") or {}
        committed = False
        if transition == "WORK_CONSUME":
            committed = task.get("consume_ref") == evidence_ref
        elif transition == "WORK_START":
            committed = task.get("start_ref") == evidence_ref
        elif transition == "WORK_TERMINAL":
            committed = any(
                row.get("evidence_ref") == evidence_ref
                for row in state.get("terminals", {}).values()
                if isinstance(row, dict)
            )
        else:
            raise Hold("TRANSITION_KIND_INVALID")
        return Result(
            "TRANSITION_COMMITTED_READBACK" if committed else "NO_COMMIT_PROVEN",
            mutated=False,
        )

    def report_historical_terminal_after_revoke(
        self, actor_ref: str, evidence_ref: str,
    ) -> Result:
        """Record truth after revoke without granting work/effect/outbox authority."""
        self._on()
        e = self._read("HISTORICAL_TERMINAL", evidence_ref)
        if e.get("schema") != HISTORICAL_TERMINAL_SCHEMA or                 e.get("disposition") not in ("HISTORICAL_AFTER_REVOKE", "ABORTED_REVOKED") or                 e.get("readback_verified") is not True:
            raise Hold("HISTORICAL_TERMINAL_EVIDENCE_INVALID")

        def tx(state):
            state = self._state(state)
            reports = state.setdefault("historical_reports", {})
            report_ref = e.get("report_ref")
            if not text(report_ref):
                raise Hold("HISTORICAL_REPORT_REF_REQUIRED")
            prior = reports.get(report_ref)
            evidence_digest = digest(e)
            if prior is not None:
                if prior.get("evidence_digest") == evidence_digest:
                    return state, Result("REPLAY_HISTORICAL_CLOSE", copy.deepcopy(prior))
                raise Hold("HISTORICAL_REPORT_CONFLICT")

            task = self._task(state)
            required_task = (
                e.get("actor_ref") == actor_ref,
                e.get("generation_ref") == state["generation_ref"],
                e.get("task_ref") == task.get("task_ref"),
                e.get("task_revision") == task.get("task_revision"),
                e.get("task_sha256") == task.get("task_payload_sha256"),
                e.get("claim_ref") == task.get("claim_ref"),
                e.get("packet_ref") == task.get("packet_ref"),
                e.get("queue_ref") == task.get("queue_ref"),
                e.get("decision_ref") == task.get("decision_ref"),
                e.get("provenance_ref") == task.get("provenance_ref"),
                e.get("binding_ref") == task.get("binding_ref"),
            )
            if not all(required_task) or not all(text(e.get(k)) for k in (
                "parent_revoke_ref", "parent_revoke_readback_ref",
                "historical_readback_ref",
            )) or e.get("parent_revoked") is not True or \
                    type(e.get("parent_revoke_owner_revision")) is not int or \
                    e["parent_revoke_owner_revision"] < 1:
                raise Hold("HISTORICAL_TASK_PROVENANCE_MISMATCH")

            had_valid_start = text(task.get("start_ref"))
            independent_execution = bool(e.get("pre_revoke_execution_verified")) and                 text(e.get("pre_revoke_execution_ref"))
            if not had_valid_start and not independent_execution:
                raise Hold("HISTORICAL_PRIOR_EXECUTION_UNPROVEN")

            if e["disposition"] == "HISTORICAL_AFTER_REVOKE":
                if e.get("result") not in ("PASS", "REFINE", "HOLD") or                         e.get("substantive_complete_before_revoke") is not True or                         not text(e.get("completion_evidence_ref")) or                         not hex64(e.get("result_sha256")):
                    raise Hold("HISTORICAL_SUCCESS_PRE_REVOKE_COMPLETION_UNPROVEN")
                status = "HISTORICAL_AFTER_REVOKE"
            else:
                if e.get("result") not in (None, "REVOKED", "ABORTED", "UNKNOWN"):
                    raise Hold("ABORTED_REVOKED_RESULT_INVALID")
                status = "ABORTED_REVOKED"

            receipt = {
                "schema": HISTORICAL_RECEIPT_SCHEMA,
                "receipt_ref": "NAL-HR-" + digest([
                    actor_ref, state["generation_ref"], report_ref,
                ])[:32],
                "report_ref": report_ref,
                "disposition": status,
                "actor_ref": actor_ref,
                "generation_ref": state["generation_ref"],
                "task_ref": task["task_ref"],
                "task_revision": task.get("task_revision"),
                "task_sha256": task["task_payload_sha256"],
                "claim_ref": task["claim_ref"],
                "packet_ref": task["packet_ref"],
                "queue_ref": task["queue_ref"],
                "decision_ref": task.get("decision_ref"),
                "provenance_ref": task.get("provenance_ref"),
                "binding_ref": task["binding_ref"],
                "parent_revoke_ref": e["parent_revoke_ref"],
                "parent_revoke_owner_revision": e["parent_revoke_owner_revision"],
                "parent_revoke_readback_ref": e["parent_revoke_readback_ref"],
                "result": e.get("result"),
                "authority": "NONE",
                "new_work_authorized": False,
                "new_effect_authorized": False,
                "owner_obligation_created": False,
                "outbox_created": False,
                "evidence_digest": evidence_digest,
                "readback_ref": e["historical_readback_ref"],
            }
            reports[report_ref] = copy.deepcopy(receipt)
            task["terminal_ref"] = report_ref
            task["status"] = "CLOSED"
            state["closed_tasks"].append(copy.deepcopy(task))
            state["reuse_ready"] = False
            return state, Result(status, copy.deepcopy(receipt), mutated=True)

        return self.store.apply(actor_ref, tx)

    def admit_release_once(self, actor_ref: str, terminal_ref: str, admission_ref: str) -> Result:
        self._on()
        e = self._read("PM_ADMISSION", admission_ref)
        if e.get("admitted") is not True or e.get("terminal_ref") != terminal_ref or \
                e.get("result") not in ("PASS", "REFINE", "HOLD"):
            raise Hold("PM_ADMISSION_READBACK_REQUIRED")
        def tx(state):
            state = self._state(state)
            terminal = state["terminals"].get(terminal_ref)
            if terminal is None:
                raise Hold("TRUSTED_TERMINAL_REQUIRED")
            lineage = (terminal["terminal_digest"], terminal["binding_ref"], terminal["claim_ref"],
                       terminal["packet_ref"], terminal["queue_ref"], terminal["result"])
            admitted = tuple(e.get(k) for k in ("terminal_digest", "binding_ref", "claim_ref",
                      "packet_ref", "queue_ref", "result"))
            if admitted != lineage or e.get("actor_ref") != actor_ref or \
                    e.get("generation_ref") != state["generation_ref"]:
                raise Hold("CONFLICT_TERMINAL_ADMISSION_BINDING")
            old = state["receipts"].get(terminal_ref)
            if old is not None:
                if old["terminal_digest"] == terminal["terminal_digest"] and \
                        old["binding_ref"] == terminal["binding_ref"] and \
                        old["claim_ref"] == terminal["claim_ref"] and \
                        old["packet_ref"] == terminal["packet_ref"] and \
                        old["queue_ref"] == terminal["queue_ref"]:
                    return state, Result("REPLAY_SAME_RECEIPT", copy.deepcopy(old))
                raise Hold("CONFLICT_TERMINAL_FIRST_USE")
            task = self._task(state)
            if task["status"] != "TERMINAL" or task["terminal_ref"] != terminal_ref or \
                    task["frontier_ref"] != state["frontier_ref"]:
                raise Hold("STALE_OR_WRONG_FRONTIER")
            self._current_binding(state, task)
            if (task["claim_ref"], task["packet_ref"], task["queue_ref"], task["binding_ref"]) != \
                    (terminal["claim_ref"], terminal["packet_ref"], terminal["queue_ref"], terminal["binding_ref"]):
                raise Hold("CONFLICT_TASK_TERMINAL_BINDING")
            receipt_ref = "NAL-R-" + digest([actor_ref, state["generation_ref"], terminal_ref])[:32]
            event_ref = "NAL-E-" + digest([receipt_ref, "owner-event/v1"])[:32]
            receipt = {"schema": "cerebro.nal.close-receipt/v1", "receipt_ref": receipt_ref,
                       "actor_ref": actor_ref, "generation_ref": state["generation_ref"],
                       "task_ref": task["task_ref"],
                       "frontier_ref": state["frontier_ref"], "terminal_ref": terminal_ref,
                       "terminal_digest": terminal["terminal_digest"], "binding_ref": terminal["binding_ref"],
                       "claim_ref": terminal["claim_ref"], "packet_ref": terminal["packet_ref"],
                       "queue_ref": terminal["queue_ref"], "task_result": terminal["result"],
                       "admission_ref": admission_ref, "event_ref": event_ref,
                       "authority": "CANDIDATE_LOCAL_ONLY"}
            event = {"schema": OUTBOX_INTENT_SCHEMA, "event_ref": event_ref, "owner_ref": "pm",
                     "receipt_ref": receipt_ref, "actor_ref": actor_ref,
                     "generation_ref": state["generation_ref"], "task_ref": task["task_ref"],
                     "terminal_ref": terminal_ref,
                     "terminal_digest": terminal["terminal_digest"], "claim_ref": terminal["claim_ref"],
                     "packet_ref": terminal["packet_ref"], "queue_ref": terminal["queue_ref"],
                     "result": terminal["result"], "authority": "CANDIDATE_LOCAL_ONLY"}
            task["status"] = "CLOSED"
            state["closed_tasks"].append(copy.deepcopy(task))
            state["reuse_ready"] = False
            state["receipts"][terminal_ref] = receipt
            state["outbox"][event_ref] = {"event": event, "delivered": False, "delivery_receipt": None}
            return state, Result("TASK_CLOSED_RECEIPTED_OUTBOXED", copy.deepcopy(receipt), mutated=True)
        # Acquire the owner fence after the actor transaction lock, then retain
        # it through commit. All actor transitions use the same lock order.
        @contextmanager
        def close_fence(snapshot, *, cursor=None):
            snapshot = self._state(snapshot)
            if terminal_ref in snapshot["receipts"]:
                yield None  # exact replay has no new effect
                return
            task = self._task(snapshot)
            binding_ref = task.get("binding_ref")
            expected = snapshot["bindings"].get(binding_ref)
            if not binding_ref or expected is None:
                raise Hold("OWNER_BINDING_NOT_CURRENT")
            full_chain = all(task.get(k) is not None for k in
                             ("task_revision", "decision_ref", "provenance_ref"))
            factory = getattr(self.evidence, "transition_authority_fence", None)
            if full_chain:
                if not callable(factory):
                    raise Hold("PARENT_AUTHORITY_FENCE_UNAVAILABLE")
                try:
                    fence = factory(
                        binding_ref,
                        transition="TASK_CLOSE",
                        task=copy.deepcopy(task),
                        expected_binding=copy.deepcopy(expected),
                        cursor=cursor,
                    )
                except Hold:
                    raise
                except Exception as exc:
                    raise Hold("PARENT_AUTHORITY_FENCE_UNAVAILABLE") from exc
                if not all(callable(getattr(fence, name, None)) for name in
                           ("__enter__", "__exit__", "assert_current")):
                    raise Hold("PARENT_AUTHORITY_FENCE_UNAVAILABLE")
                with fence:
                    try:
                        fence.assert_current(copy.deepcopy(expected), "TASK_CLOSE")
                    except Hold:
                        raise
                    except Exception as exc:
                        raise Hold("FULL_AUTHORITY_CHANGED_BEFORE_COMMIT") from exc
                    def guard():
                        try:
                            fence.assert_current(copy.deepcopy(expected), "TASK_CLOSE")
                        except Hold:
                            raise
                        except Exception as exc:
                            raise Hold("FULL_AUTHORITY_CHANGED_BEFORE_COMMIT") from exc
                    yield guard
                return
            # Legacy C995/C999 close proof remains child-binding scoped.
            with self._owner_commit_fence(binding_ref, cursor=cursor) as fence:
                self._assert_owner_fence(fence, expected)
                yield lambda: self._assert_owner_fence(fence, expected)

        try:
            return self.store.apply(actor_ref, tx, owner_fence_factory=close_fence)
        except Hold as exc:
            if exc.code == "PG_COMMIT_OUTCOME_UNKNOWN":
                raise Hold("HOLD_UNKNOWN") from exc
            raise

    def pending_outbox(self, actor_ref: str) -> list[dict]:
        self._on()
        state = self._state(self.store.read(actor_ref))
        return [copy.deepcopy(v["event"]) for v in state["outbox"].values() if not v["delivered"]]

    def project_owner_event(self, actor_ref: str, event_ref: str,
                            observed_at: str | None = None) -> dict:
        """Produce A7-compatible raw event only after independent local readback.

        This is a projection, not an authenticated A7 receipt or a send. The
        downstream trusted owner reader must verify the referenced NAL receipt.
        """
        self._on()
        state = self._state(self.store.read(actor_ref))
        item = state["outbox"].get(event_ref)
        if item is None or item["event"].get("schema") != OUTBOX_INTENT_SCHEMA:
            raise Hold("OUTBOX_EVENT_UNKNOWN")
        intent = item["event"]
        evidence = self.store.read_commit_evidence(actor_ref, intent["terminal_ref"])
        receipt = evidence["receipt"]
        if evidence["outbox_event"] != intent or receipt["event_ref"] != event_ref:
            raise Hold("COMMIT_EVIDENCE_INCOMPLETE")
        fields = {"status": "CLOSED", "result": receipt["task_result"]}
        at = observed_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if not text(at):
            raise Hold("OBSERVATION_TIME_REQUIRED")
        return {"schema": OWNER_EVENT_SCHEMA, "event_id": event_ref,
                "owner_ref": "pm", "source_ref": "nal:pm",
                "referent": {"type": "task", "id": "NAL-T-" + digest([
                    actor_ref, receipt["task_ref"]])[:32]},
                "owner_seq": 1, "revision_basis": {"before": receipt["frontier_ref"],
                                                     "after": receipt["receipt_ref"]},
                "change_class": "SEMANTIC",
                "delta": {"kind": "INLINE", "expected_sha256": hashlib.sha256(canonical(fields)).hexdigest(),
                          "fields": fields},
                "commit": {"state": "COMMITTED_READBACK", "readback_ref": receipt["receipt_ref"],
                           "observed_at": at},
                "way_home": ["nal:receipt:" + receipt["receipt_ref"]]}

    def acknowledge_outbox(self, actor_ref: str, event_ref: str, delivery_ref: str) -> Result:
        self._on()
        e = self._read("OUTBOX_DELIVERY", delivery_ref)
        if e.get("event_ref") != event_ref or not text(e.get("delivery_receipt")):
            raise Hold("DELIVERY_READBACK_MISMATCH")
        def tx(state):
            state = self._state(state)
            item = state["outbox"].get(event_ref)
            if item is None:
                raise Hold("OUTBOX_EVENT_UNKNOWN")
            if item["delivered"]:
                if item["delivery_receipt"] == e["delivery_receipt"]:
                    return state, Result("REPLAY_DELIVERY_ACK")
                raise Hold("CONFLICT_DELIVERY_ACK")
            item["delivered"] = True
            item["delivery_receipt"] = e["delivery_receipt"]
            return state, Result("OUTBOX_DELIVERED_READBACK", mutated=True)
        return self.store.apply(actor_ref, tx)

    def reset_for_reuse(self, actor_ref: str, reset_ref: str) -> Result:
        self._on()
        e = self._read("RESET_READBACK", reset_ref)
        def tx(state):
            state = self._state(state)
            if state["current_task"] is None and state["reuse_ready"] and \
                    state.get("last_reset_ref") == reset_ref:
                if state.get("last_reset_digest") == digest(e):
                    return state, Result("REPLAY_RESET")
                raise Hold("RESET_READBACK_CHANGED")
            task = self._task(state)
            if task["status"] != "CLOSED" or state["reuse_ready"]:
                raise Hold("TASK_CLOSE_NE_ACTOR_REUSABLE")
            if (e.get("actor_ref"), e.get("generation_ref"), e.get("terminal_ref"),
                e.get("closed_capabilities"), e.get("next_epoch")) != (
                actor_ref, state["generation_ref"], task["terminal_ref"], True, state["epoch"] + 1):
                raise Hold("RESET_READBACK_MISMATCH")
            state["epoch"] += 1
            state["frontier_ref"] = "NAL-F-" + digest([actor_ref, state["generation_ref"], state["epoch"]])[:24]
            state["current_task"] = None
            state["reuse_ready"] = True
            state["last_reset_ref"] = reset_ref
            state["last_reset_digest"] = digest(e)
            return state, Result("ACTOR_REUSABLE_AFTER_RESET", mutated=True)
        return self.store.apply(actor_ref, tx)
