"""P1625 ordinary WORKER episode callsite. Local-only, explicit injection, OFF."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lifecycle import Hold, Lifecycle, OUTBOX_INTENT_SCHEMA, digest


@dataclass(frozen=True)
class IssuedEpisode:
    mandate_ref: str
    pm_actor_ref: str
    pm_generation_ref: str
    worker_actor_ref: str
    worker_generation_ref: str
    source_revision: str
    method_fingerprint: str
    task_ref: str
    task_revision: int
    task_sha256: str
    binding_ref: str
    consume_ref: str
    start_ref: str


class BoundLocalFileReader:
    """Read only prebound files inside one explicitly provided local root."""

    def __init__(self, root: Path, targets: dict[str, Path], *,
                 max_bytes: int = 1_048_576):
        self.root = root.resolve(strict=True)
        self.targets = dict(targets)
        self.max_bytes = max_bytes

    def digest(self, target_ref: str) -> dict:
        path = self.targets.get(target_ref)
        if path is None:
            raise Hold("LOCAL_TARGET_NOT_BOUND")
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(self.root) or not resolved.is_file():
            raise Hold("LOCAL_TARGET_OUTSIDE_BOUND_ROOT")
        if resolved.stat().st_size > self.max_bytes:
            raise Hold("LOCAL_TARGET_EXCEEDS_BOUND")
        h = hashlib.sha256()
        size = 0
        with resolved.open("rb") as stream:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > self.max_bytes:
                    raise Hold("LOCAL_TARGET_EXCEEDS_BOUND")
                h.update(chunk)
        return {"sha256": h.hexdigest(), "bytes": size}


class WorkerEpisodeRunner:
    """One issued episode; trusted owner ports supply facts and admission.

    The runner cannot register an actor, issue a task, bind a child, seed
    evidence, issue Human rights, delegate PM, or mint an owner decision.
    Restart from any partial stage returns HOLD for owner readback, never a
    blind second local action or effect.
    """

    def __init__(self, authority: Any, lifecycle: Lifecycle,
                 local_reader: BoundLocalFileReader,
                 terminal_owner: Any, admission_owner: Any,
                 *, enabled: bool = False):
        self.authority = authority
        self.lifecycle = lifecycle
        self.local_reader = local_reader
        self.terminal_owner = terminal_owner
        self.admission_owner = admission_owner
        self.enabled = enabled

    def _state(self, actor_ref: str) -> dict:
        state = self.lifecycle.store.read(actor_ref)
        if not isinstance(state, dict):
            raise Hold("WORKER_ACTOR_READBACK_MISSING")
        return state

    def _unknown(self, actor_ref: str, transition: str, ref: str) -> dict:
        # Readback is diagnostic; never use it as permission to retry.
        try:
            readback = self.lifecycle.reconcile_transition(actor_ref, transition, ref)
            status = readback.status
        except Exception:
            status = "READBACK_UNAVAILABLE"
        return {"status": "HOLD_UNKNOWN", "transition": transition,
                "evidence_ref": ref, "authoritative_readback": status}

    def _transition(self, actor_ref: str, kind: str, ref: str, fn):
        try:
            return fn(actor_ref, ref)
        except Hold as exc:
            if exc.code == "HOLD_UNKNOWN":
                return self._unknown(actor_ref, kind, ref)
            raise

    @staticmethod
    def _outcome_key(issued: IssuedEpisode, decision: dict,
                     terminal: dict, material: dict,
                     *, admission: dict | None = None) -> dict:
        return {
            "transition": "TASK_CLOSE" if admission else "WORK_TERMINAL",
            "actor_ref": issued.worker_actor_ref,
            "generation_ref": issued.worker_generation_ref,
            "task_ref": issued.task_ref,
            "task_revision": issued.task_revision,
            "task_sha256": issued.task_sha256,
            "binding_ref": issued.binding_ref,
            "claim_ref": decision["claim_ref"],
            "packet_ref": decision["packet_ref"],
            "queue_ref": decision["queue_ref"],
            "decision_ref": decision["decision_ref"],
            "provenance_ref": decision["readback_ref"],
            "terminal_ref": terminal["terminal_ref"],
            "terminal_evidence_ref": terminal["evidence_ref"],
            "terminal_evidence_digest": digest(terminal),
            "terminal_digest": terminal["terminal_digest"],
            "result": terminal["result"],
            "material_sha256": material["sha256"],
            "material_bytes": material["bytes"],
            "admission_ref": admission["evidence_ref"] if admission else None,
            "admission_evidence_digest": digest(admission) if admission else None,
        }

    @staticmethod
    def _unresolved(key: dict, transition: str, reason: str) -> dict:
        return {"status": "HOLD_UNKNOWN", "transition": transition,
                "outcome": reason, "outcome_key": key}

    def read_ambiguous_outcome(self, key: dict, transition: str) -> dict:
        """Independent persisted readback; never retries a transition.

        The serializable key is only a query identity. Fresh owner evidence,
        the actor aggregate and (for close) immutable ledgers must agree.
        """
        if not self.enabled:
            raise Hold("WORKER_EPISODE_DEFAULT_OFF")
        if transition not in {"WORK_TERMINAL", "TASK_CLOSE"} or \
           not isinstance(key, dict) or key.get("transition") != transition:
            raise Hold("OUTCOME_KEY_INVALID")
        required = ("actor_ref", "generation_ref", "task_ref", "task_revision",
                    "task_sha256", "binding_ref", "claim_ref", "packet_ref",
                    "queue_ref", "decision_ref", "provenance_ref", "terminal_ref",
                    "terminal_evidence_ref", "terminal_evidence_digest",
                    "terminal_digest", "result", "material_sha256",
                    "material_bytes")
        if any(key.get(k) is None for k in required) or \
           (transition == "TASK_CLOSE" and
            (not key.get("admission_ref") or
             not key.get("admission_evidence_digest"))):
            raise Hold("OUTCOME_KEY_INVALID")
        try:
            owner_terminal = self.lifecycle.evidence.read(
                "WORKER_TERMINAL", key["terminal_evidence_ref"])
            owner_admission = (
                self.lifecycle.evidence.read("PM_ADMISSION", key["admission_ref"])
                if key.get("admission_ref") else None)
            state = self.lifecycle.store.read(key["actor_ref"])
        except Exception:
            return self._unresolved(key, transition, "UNREADABLE")
        try:
            owner_terminal_digest = digest(owner_terminal) if isinstance(
                owner_terminal, dict) else None
            owner_admission_digest = digest(owner_admission) if isinstance(
                owner_admission, dict) else None
        except Exception:
            return self._unresolved(key, transition, "OWNER_EVIDENCE_UNREADABLE")
        if not isinstance(owner_terminal, dict) or \
           owner_terminal_digest != key["terminal_evidence_digest"] or \
           owner_terminal.get("evidence_ref") != key["terminal_evidence_ref"] or \
           owner_terminal.get("terminal_ref") != key["terminal_ref"] or \
           owner_terminal.get("terminal_digest") != key["terminal_digest"] or \
           owner_terminal.get("result") != key["result"] or \
           owner_terminal.get("material_sha256") != key["material_sha256"] or \
           owner_terminal.get("material_bytes") != key["material_bytes"] or \
           any(owner_terminal.get(field) != key[field] for field in
               ("actor_ref", "generation_ref", "binding_ref",
                "claim_ref", "packet_ref", "queue_ref")):
            return self._unresolved(key, transition, "OWNER_TERMINAL_MISMATCH")
        if key.get("admission_ref") and (
            not isinstance(owner_admission, dict) or
            owner_admission_digest != key["admission_evidence_digest"] or
            owner_admission.get("evidence_ref") != key["admission_ref"] or
            owner_admission.get("terminal_ref") != key["terminal_ref"] or
            owner_admission.get("terminal_digest") != key["terminal_digest"] or
            owner_admission.get("result") != key["result"] or
            owner_admission.get("material_sha256") != key["material_sha256"] or
            any(owner_admission.get(field) != key[field] for field in
                ("actor_ref", "generation_ref", "binding_ref",
                 "claim_ref", "packet_ref", "queue_ref"))
        ):
            return self._unresolved(key, transition, "OWNER_ADMISSION_MISMATCH")
        if not isinstance(state, dict) or \
           state.get("actor_ref") != key["actor_ref"] or \
           state.get("generation_ref") != key["generation_ref"]:
            return self._unresolved(key, transition, "ACTOR_MISMATCH")
        task = state.get("current_task")
        task_fields = {
            "task_ref": "task_ref", "task_revision": "task_revision",
            "task_sha256": "task_payload_sha256", "binding_ref": "binding_ref",
            "claim_ref": "claim_ref", "packet_ref": "packet_ref",
            "queue_ref": "queue_ref", "decision_ref": "decision_ref",
            "provenance_ref": "provenance_ref",
        }
        if not isinstance(task, dict) or any(
            task.get(field) != key[k] for k, field in task_fields.items()
        ):
            return self._unresolved(key, transition, "TASK_MISMATCH")
        if not all(isinstance(state.get(field), dict) for field in
                   ("terminals", "receipts", "outbox")) or \
           not isinstance(state.get("closed_tasks"), list):
            return self._unresolved(key, transition, "ACTOR_LEDGER_PARTIAL")
        persisted_terminal = state["terminals"].get(key["terminal_ref"])
        if persisted_terminal is None:
            if task.get("status") == "STARTED" and \
               task.get("terminal_ref") is None and \
               not state.get("receipts", {}).get(key["terminal_ref"]):
                return self._unresolved(key, transition, "ABSENT")
            return self._unresolved(key, transition, "PARTIAL_OR_STALE")
        if persisted_terminal != owner_terminal or \
           task.get("terminal_ref") != key["terminal_ref"] or \
           task.get("status") not in {"TERMINAL", "CLOSED"}:
            return self._unresolved(key, transition, "TERMINAL_MISMATCH")
        if transition == "WORK_TERMINAL":
            if task.get("status") != "TERMINAL" or \
               state.get("receipts", {}).get(key["terminal_ref"]) is not None:
                return self._unresolved(
                    key, transition, "LATER_CLOSE_REQUIRES_ADMISSION_KEY")
            return {"status": "COMMITTED_EXACT_READBACK",
                    "transition": transition, "outcome": "COMMITTED_EXACT",
                    "outcome_key": key}
        receipt = state.get("receipts", {}).get(key["terminal_ref"])
        if receipt is None:
            if task.get("status") == "TERMINAL":
                return self._unresolved(key, transition, "ABSENT")
            return self._unresolved(key, transition, "PARTIAL_OR_STALE")
        receipt_ref = "NAL-R-" + digest([
            key["actor_ref"], key["generation_ref"], key["terminal_ref"]])[:32]
        event_ref = "NAL-E-" + digest([receipt_ref, "owner-event/v1"])[:32]
        expected_receipt = {
            "schema": "cerebro.nal.close-receipt/v1",
            "receipt_ref": receipt_ref, "event_ref": event_ref,
            "frontier_ref": state.get("frontier_ref"),
            "authority": "CANDIDATE_LOCAL_ONLY",
            "actor_ref": key["actor_ref"], "generation_ref": key["generation_ref"],
            "task_ref": key["task_ref"], "terminal_ref": key["terminal_ref"],
            "terminal_digest": key["terminal_digest"],
            "binding_ref": key["binding_ref"], "claim_ref": key["claim_ref"],
            "packet_ref": key["packet_ref"], "queue_ref": key["queue_ref"],
            "task_result": key["result"], "admission_ref": key["admission_ref"],
        }
        outbox_item = state["outbox"].get(event_ref)
        event = outbox_item.get("event") if isinstance(outbox_item, dict) else None
        expected_event = {
            "schema": OUTBOX_INTENT_SCHEMA, "owner_ref": "pm",
            "authority": "CANDIDATE_LOCAL_ONLY",
            "event_ref": event_ref, "receipt_ref": receipt_ref,
            "actor_ref": key["actor_ref"], "generation_ref": key["generation_ref"],
            "task_ref": key["task_ref"], "terminal_ref": key["terminal_ref"],
            "terminal_digest": key["terminal_digest"],
            "claim_ref": key["claim_ref"], "packet_ref": key["packet_ref"],
            "queue_ref": key["queue_ref"], "result": key["result"],
        }
        if task.get("status") != "CLOSED" or \
           task.get("frontier_ref") != state.get("frontier_ref") or \
           task not in state.get("closed_tasks", []) or \
           state.get("reuse_ready") is not False or \
           not isinstance(receipt, dict) or not isinstance(event, dict) or \
           any(receipt.get(k) != v for k, v in expected_receipt.items()) or \
           any(event.get(k) != v for k, v in expected_event.items()):
            return self._unresolved(key, transition, "CLOSE_MISMATCH")
        try:
            ledger = self.lifecycle.store.read_commit_evidence(
                key["actor_ref"], key["terminal_ref"])
        except Exception:
            return self._unresolved(key, transition, "LEDGER_UNREADABLE_OR_PARTIAL")
        if not isinstance(ledger, dict) or \
           ledger.get("terminal") != owner_terminal or \
           ledger.get("receipt") != receipt or \
           ledger.get("outbox_event") != event:
            return self._unresolved(key, transition, "LEDGER_MISMATCH")
        return {"status": "COMMITTED_EXACT_READBACK",
                "transition": transition, "outcome": "COMMITTED_EXACT",
                "outcome_key": key, "receipt": receipt}

    def run(self, issued: IssuedEpisode) -> dict:
        if not self.enabled:
            raise Hold("WORKER_EPISODE_DEFAULT_OFF")
        state = self._state(issued.worker_actor_ref)
        if state.get("generation_ref") != issued.worker_generation_ref:
            raise Hold("WORKER_GENERATION_MISMATCH")
        task = state.get("current_task")
        if not isinstance(task, dict) or task.get("task_ref") != issued.task_ref or \
           task.get("binding_ref") != issued.binding_ref or \
           task.get("task_revision") != issued.task_revision or \
           task.get("task_payload_sha256") != issued.task_sha256:
            raise Hold("WORKER_TASK_READBACK_MISMATCH")
        if task.get("status") != "ADMITTED":
            return {"status": "HOLD_RESTART_REQUALIFICATION",
                    "actor_status": task.get("status"),
                    "task_ref": issued.task_ref}
        snapshot = self.authority.read_current_consumer_authority(
            mandate_ref=issued.mandate_ref,
            pm_actor_ref=issued.pm_actor_ref,
            pm_generation_ref=issued.pm_generation_ref,
            worker_generation_ref=issued.worker_generation_ref,
            worker_actor_ref=issued.worker_actor_ref,
            source_revision=issued.source_revision,
            method_fingerprint=issued.method_fingerprint,
            task_ref=issued.task_ref,
            expected_task_revision=issued.task_revision,
            expected_task_sha256=issued.task_sha256,
        )
        decision = snapshot["task"]
        if decision.get("operation") != "LOCAL_READ_SHA256" or \
           any(decision.get(k) != task.get(k) for k in
               ("claim_ref", "packet_ref", "queue_ref", "decision_ref")) or \
           decision.get("readback_ref") != task.get("provenance_ref"):
            raise Hold("LOCAL_EPISODE_AUTHORITY_MISMATCH")
        evidence = self.lifecycle.evidence
        if not hasattr(evidence, "transition_authority_fence"):
            raise Hold("FULL_CHAIN_FENCE_REQUIRED")
        expected_factory = self.authority.nal_transition_parent_fence_factory(snapshot)
        scope = getattr(evidence, "with_parent_factory", None)
        if callable(scope):
            # Private task-scoped adapter: concurrent actors cannot replace
            # each other's parent snapshot on a shared evidence reader.
            episode_lifecycle = Lifecycle(
                self.lifecycle.store, scope(expected_factory), enabled=True)
        elif not hasattr(evidence, "transition_parent_fence_factory"):
            # Synthetic fixture already composes its own parent and child.
            episode_lifecycle = self.lifecycle
        else:
            raise Hold("TASK_SCOPED_PARENT_BINDING_REQUIRED")
        consume = self._transition(
            issued.worker_actor_ref, "WORK_CONSUME", issued.consume_ref,
            episode_lifecycle.worker_consume)
        if isinstance(consume, dict):
            return consume
        if consume.status != "WORK_CONSUMED":
            raise Hold("WORK_CONSUME_NOT_NEW")
        start = self._transition(
            issued.worker_actor_ref, "WORK_START", issued.start_ref,
            episode_lifecycle.worker_start)
        if isinstance(start, dict):
            return start
        if start.status != "WORK_STARTED":
            raise Hold("WORK_START_NOT_NEW")
        # This is the only substantive work: a bounded read of a prebound
        # local file. No shell, network, provider write or arbitrary path.
        material = self.local_reader.digest(decision["effect_target_ref"])
        occurrence = self.terminal_owner.record_local_digest(
            snapshot=snapshot, binding_ref=issued.binding_ref,
            material_sha256=material["sha256"], material_bytes=material["bytes"])
        if not isinstance(occurrence, dict) or not occurrence.get("evidence_ref") or \
           not occurrence.get("terminal_ref") or \
           occurrence.get("material_sha256") != material["sha256"] or \
           occurrence.get("material_bytes") != material["bytes"]:
            raise Hold("TRUSTED_TERMINAL_OWNER_RECEIPT_REQUIRED")
        trusted_occurrence = episode_lifecycle.evidence.read(
            "WORKER_TERMINAL", occurrence["evidence_ref"])
        if not isinstance(trusted_occurrence, dict) or \
           trusted_occurrence.get("terminal_ref") != occurrence["terminal_ref"] or \
           trusted_occurrence.get("material_sha256") != material["sha256"] or \
           trusted_occurrence.get("material_bytes") != material["bytes"]:
            raise Hold("TRUSTED_TERMINAL_MATERIAL_READBACK_REQUIRED")
        terminal_key = self._outcome_key(
            issued, decision, trusted_occurrence, material)
        try:
            terminal = episode_lifecycle.worker_terminal(
                issued.worker_actor_ref, occurrence["evidence_ref"])
        except Hold as exc:
            if exc.code == "HOLD_UNKNOWN":
                return self.read_ambiguous_outcome(
                    terminal_key, "WORK_TERMINAL")
            raise
        if terminal.status != "TERMINAL_OBSERVED":
            raise Hold("WORK_TERMINAL_NOT_NEW")
        admission_ref = self.admission_owner.admit_terminal(
            snapshot=snapshot, terminal_ref=occurrence["terminal_ref"],
            material_sha256=material["sha256"])
        if not isinstance(admission_ref, str) or not admission_ref:
            raise Hold("OWNER_ADMISSION_REF_REQUIRED")
        trusted_admission = episode_lifecycle.evidence.read(
            "PM_ADMISSION", admission_ref)
        if not isinstance(trusted_admission, dict) or \
           trusted_admission.get("terminal_ref") != occurrence["terminal_ref"] or \
           trusted_admission.get("material_sha256") != material["sha256"]:
            raise Hold("OWNER_ADMISSION_MATERIAL_READBACK_REQUIRED")
        close_key = self._outcome_key(
            issued, decision, trusted_occurrence, material,
            admission=trusted_admission)
        try:
            closed = episode_lifecycle.admit_release_once(
                issued.worker_actor_ref, occurrence["terminal_ref"], admission_ref)
        except Hold as exc:
            if exc.code == "HOLD_UNKNOWN":
                return self.read_ambiguous_outcome(close_key, "TASK_CLOSE")
            raise
        if closed.status != "TASK_CLOSED_RECEIPTED_OUTBOXED":
            raise Hold("OWNER_CLOSE_NOT_NEW")
        verified = self.read_ambiguous_outcome(close_key, "TASK_CLOSE")
        if verified["status"] != "COMMITTED_EXACT_READBACK" or \
           verified["receipt"] != closed.receipt:
            return self._unresolved(close_key, "TASK_CLOSE",
                                    "POSTCOMMIT_READBACK_MISMATCH")
        return {"status": "LOCAL_NAL_EPISODE_CLOSED",
                "task_ref": issued.task_ref,
                "terminal_ref": occurrence["terminal_ref"],
                "material_sha256": material["sha256"],
                "material_bytes": material["bytes"],
                "receipt": verified["receipt"]}
