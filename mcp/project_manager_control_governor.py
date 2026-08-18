#!/usr/bin/env python3
from __future__ import annotations

import copy
import hashlib
import json
import sys
from typing import Any

SCHEMA = "cerebro-project-manager-control-governor/v1"
DECISION_SCHEMA = "cerebro-project-manager-control-governor-decision/v1"
PROFILE_VERIFICATION_SCHEMA = "cerebro-project-manager-profile-verification/v1"

FRONTIER_ACTORS = {"PROJECT_MANAGER", "HUMAN"}
FRONTIER_STATES = {"PENDING", "COMPLETE", "BLOCKED", "SKIPPED"}
INTERRUPTS = {"NONE", "PAUSE", "STOP", "TERMINATE"}
TERMINAL_STATES = {"ACTIVE", "COMPLETE", "STOP", "TERMINATE"}
CONTINUATION_DISPOSITIONS = {
    "PENDING", "HANDOFF", "NONE_REQUIRED", "RECOVERY_REVIEW", "NON_PROPAGATING"
}
LEARNING_DISPOSITIONS = {
    "PENDING", "GENERALIZABLE_LEARNING_CANDIDATE", "NO_GENERALIZABLE_LEARNING",
    "RECOVERY_REVIEW_ONLY", "NON_PROPAGATING"
}


class ProjectManagerGovernorError(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ProjectManagerGovernorError(message)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _fingerprint(value: dict[str, Any]) -> str:
    subject = copy.deepcopy(value)
    subject.pop("decision_fingerprint", None)
    subject.pop("decision_ref", None)
    return hashlib.sha256(_canonical(subject)).hexdigest()


def _verify_profile(
    *,
    profile_binding: Any,
    session: dict[str, Any],
    profile_verifier: Any | None,
) -> dict[str, Any]:
    _require(isinstance(profile_binding, dict), "trusted-pm-profile-binding-required")
    _require(profile_verifier is not None, "constructor-bound-pm-profile-verifier-required")
    verify = getattr(profile_verifier, "verify", None)
    _require(callable(verify), "pm-profile-verifier-invalid")
    try:
        result = verify(binding=copy.deepcopy(profile_binding), session=copy.deepcopy(session))
    except Exception as exc:
        raise ProjectManagerGovernorError(f"pm-profile-verification-failed:{exc}") from exc
    _require(isinstance(result, dict), "pm-profile-verification-object-required")
    _require(result.get("schema") == PROFILE_VERIFICATION_SCHEMA, "pm-profile-verification-schema-mismatch")
    _require(result.get("result") == "PASS", "pm-profile-verification-nonpass")
    _require(result.get("profile") == "PROJECT_MANAGER", "pm-profile-must-be-PROJECT_MANAGER")
    _require(result.get("session_ref") == session.get("session_ref"), "pm-profile-session-ref-mismatch")
    _require(
        isinstance(result.get("binding_fingerprint"), str) and len(result["binding_fingerprint"]) == 64,
        "pm-profile-binding-fingerprint-required",
    )
    _require(
        isinstance(result.get("verifier_ref"), str) and bool(result["verifier_ref"].strip()),
        "pm-profile-verifier-ref-required",
    )
    return result


def _validate_frontier(candidate: dict[str, Any], canonical_next_action: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    raw = candidate.get("frontier_actions")
    _require(isinstance(raw, list) and bool(raw), "pm-frontier-actions-required")
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    first_incomplete: dict[str, Any] | None = None

    for index, item in enumerate(raw):
        _require(isinstance(item, dict), f"pm-frontier-action-object-required:{index}")
        action_ref = str(item.get("action_ref") or "").strip()
        actor = str(item.get("actor") or "").upper()
        state = str(item.get("state") or "").upper()
        _require(action_ref, f"pm-frontier-action-ref-required:{index}")
        _require(action_ref not in seen, f"pm-frontier-action-ref-duplicate:{action_ref}")
        seen.add(action_ref)
        _require(actor in FRONTIER_ACTORS, f"pm-frontier-actor-invalid:{actor}")
        _require(state in FRONTIER_STATES, f"pm-frontier-state-invalid:{state}")
        internally_executable = item.get("internally_executable") is True
        human_action_required = item.get("human_action_required") is True

        if actor == "PROJECT_MANAGER":
            _require(not human_action_required, f"pm-owned-action-cannot-require-human:{action_ref}")
        if actor == "HUMAN":
            _require(not internally_executable, f"human-action-cannot-be-internally-executable:{action_ref}")
            _require(human_action_required, f"human-action-must-declare-human-required:{action_ref}")

        normalized = {
            "sequence": index + 1,
            "action_ref": action_ref,
            "actor": actor,
            "state": state,
            "internally_executable": internally_executable,
            "human_action_required": human_action_required,
            "delegation_target_ref": item.get("delegation_target_ref"),
        }
        rows.append(normalized)
        if first_incomplete is None and state not in {"COMPLETE", "SKIPPED"}:
            first_incomplete = normalized

    canonical_ref = str(canonical_next_action.get("action_ref") or "NONE")
    canonical_owner = str(canonical_next_action.get("owner") or "NONE").upper()
    if canonical_ref != "NONE":
        _require(first_incomplete is not None, "canonical-next-action-present-but-frontier-complete")
        _require(first_incomplete["action_ref"] == canonical_ref, "pm-frontier-does-not-match-canonical-next-action")
        if canonical_owner == "HUMAN":
            _require(first_incomplete["actor"] == "HUMAN", "canonical-human-next-action-actor-mismatch")
        elif canonical_owner == "MACHINE":
            _require(first_incomplete["actor"] == "PROJECT_MANAGER", "canonical-machine-next-action-must-be-pm-owned-in-pm-governor")
        elif canonical_owner not in {"NONE", ""}:
            raise ProjectManagerGovernorError(f"canonical-next-action-owner-unsupported:{canonical_owner}")

    if first_incomplete is not None and first_incomplete["actor"] == "HUMAN":
        _require(
            canonical_ref != "NONE" and canonical_owner == "HUMAN",
            "human-frontier-cannot-create-unbound-human-boundary",
        )

    if first_incomplete is None:
        next_action = {
            "action_ref": "NONE",
            "action_class": "CONTROL",
            "owner": "NONE",
            "pm_actor": "NONE",
            "internally_executable": False,
            "required_before_event_closure": False,
            "human_continuation_allowed": False,
        }
    elif first_incomplete["actor"] == "PROJECT_MANAGER":
        next_action = {
            "action_ref": first_incomplete["action_ref"],
            "action_class": "CONTROL",
            "owner": "MACHINE",
            "pm_actor": "PROJECT_MANAGER",
            "internally_executable": first_incomplete["internally_executable"],
            "required_before_event_closure": True,
            "human_continuation_allowed": False,
        }
    else:
        next_action = {
            "action_ref": first_incomplete["action_ref"],
            "action_class": "HUMAN_ACTION",
            "owner": "HUMAN",
            "pm_actor": "HUMAN",
            "internally_executable": False,
            "required_before_event_closure": False,
            "human_continuation_allowed": True,
        }

    return {"frontier": rows, "first_incomplete": first_incomplete}, next_action


def _shared_write_gate(candidate: dict[str, Any]) -> dict[str, Any]:
    raw = candidate.get("shared_write_transaction")
    if raw is None:
        return {"applicable": False, "result": "PASS", "next_transaction_allowed": True}
    _require(isinstance(raw, dict), "shared-write-transaction-object-required")
    semantic_intent_id = str(raw.get("semantic_intent_id") or "").strip()
    idempotency_key = str(raw.get("idempotency_key") or "").strip()
    _require(semantic_intent_id, "shared-semantic-intent-id-required")
    _require(idempotency_key, "shared-idempotency-key-required")
    writer_count = int(raw.get("active_semantic_writer_count") or 0)
    overlapping = int(raw.get("overlapping_attempt_count") or 0)
    _require(writer_count == 1, "shared-single-semantic-writer-required")
    _require(overlapping == 0, "shared-overlapping-same-intent-attempt-prohibited")
    _require(raw.get("retry_without_fresh_read") is not True, "shared-retry-without-fresh-read-prohibited")

    attempted = raw.get("provider_write_attempted") is True
    accepted = raw.get("provider_write_accepted") is True
    readback = raw.get("provider_readback_verified") is True
    if accepted:
        _require(attempted, "shared-provider-accept-without-write-attempt-invalid")
    next_allowed = not attempted or (accepted and readback)
    if raw.get("next_transaction_requested") is True:
        _require(next_allowed, "shared-next-transaction-before-provider-readback-prohibited")

    return {
        "applicable": True,
        "result": "PASS",
        "semantic_intent_id": semantic_intent_id,
        "idempotency_key": idempotency_key,
        "provider_write_attempted": attempted,
        "provider_write_accepted": accepted,
        "provider_readback_verified": readback,
        "next_transaction_allowed": next_allowed,
    }


def _worker_terminal_gate(candidate: dict[str, Any]) -> dict[str, Any]:
    raw = candidate.get("worker_terminal_admission")
    if raw is None:
        return {"applicable": False, "result": "PASS", "integrator_open": False}
    _require(isinstance(raw, dict), "worker-terminal-admission-object-required")
    expected = [str(x) for x in raw.get("expected_worker_refs", [])]
    admitted = [str(x) for x in raw.get("admitted_terminal_refs", [])]
    _require(len(expected) == len(set(expected)), "worker-expected-ref-duplicate")
    _require(len(admitted) == len(set(admitted)), "worker-admitted-ref-duplicate")
    _require(set(admitted).issubset(set(expected)), "worker-admitted-terminal-not-expected")
    _require(raw.get("worker_self_admission_present") is not True, "worker-self-admission-prohibited")
    _require(raw.get("pm_independent_admission_verified") is True or not admitted, "worker-terminal-requires-independent-pm-admission")
    integrator_open = set(admitted) == set(expected) and bool(expected)
    if integrator_open:
        _require(raw.get("frozen_integration_input_manifest") is True, "integrator-requires-frozen-input-manifest")
    if raw.get("integrator_open_requested") is True:
        _require(integrator_open, "integrator-open-before-all-terminal-admissions-prohibited")
    return {
        "applicable": True,
        "result": "PASS",
        "expected_worker_refs": expected,
        "admitted_terminal_refs": admitted,
        "integrator_open": integrator_open,
    }


def _interrupt_gate(candidate: dict[str, Any]) -> dict[str, Any]:
    raw = candidate.get("interrupt")
    if raw is None:
        return {"intent": "NONE", "result": "PASS"}
    _require(isinstance(raw, dict), "pm-interrupt-object-required")
    intent = str(raw.get("intent") or "NONE").upper()
    _require(intent in INTERRUPTS, f"pm-interrupt-invalid:{intent}")
    if intent == "PAUSE":
        _require(raw.get("claim_reserved") is True, "pause-must-reserve-claim")
        _require(raw.get("fresh_revalidation_on_resume") is True, "pause-resume-must-revalidate")
        _require(raw.get("continuation_propagates") is True, "pause-must-preserve-continuation")
    elif intent == "STOP":
        _require(raw.get("claim_released") is True, "stop-must-release-claim")
        _require(raw.get("partial_work_quarantined") is True, "stop-must-quarantine-partial-work")
        _require(raw.get("recovery_review_required") is True, "stop-must-require-recovery-review")
        _require(raw.get("direct_continuation_allowed") is False, "stop-direct-continuation-prohibited")
    elif intent == "TERMINATE":
        _require(raw.get("claim_released") is True, "terminate-must-release-claim")
        _require(raw.get("minimal_tombstone_retained") is True, "terminate-must-retain-minimal-tombstone")
        _require(raw.get("continuation_propagates") is False, "terminate-continuation-propagation-prohibited")
        _require(raw.get("substantive_inheritance_allowed") is False, "terminate-substantive-inheritance-prohibited")
    return {"intent": intent, "result": "PASS"}


def _terminal_gate(candidate: dict[str, Any]) -> dict[str, Any]:
    raw = candidate.get("terminal")
    if raw is None:
        return {"state": "ACTIVE", "result": "PASS", "terminal_ready": False}
    _require(isinstance(raw, dict), "pm-terminal-object-required")
    state = str(raw.get("state") or "ACTIVE").upper()
    continuation = str(raw.get("continuation_disposition") or "PENDING").upper()
    learning = str(raw.get("learning_disposition") or "PENDING").upper()
    _require(state in TERMINAL_STATES, f"pm-terminal-state-invalid:{state}")
    _require(continuation in CONTINUATION_DISPOSITIONS, f"pm-continuation-disposition-invalid:{continuation}")
    _require(learning in LEARNING_DISPOSITIONS, f"pm-learning-disposition-invalid:{learning}")

    ready = False
    if state == "COMPLETE":
        _require(continuation in {"HANDOFF", "NONE_REQUIRED"}, "complete-requires-continuation-disposition")
        _require(
            learning in {"GENERALIZABLE_LEARNING_CANDIDATE", "NO_GENERALIZABLE_LEARNING"},
            "complete-requires-learning-disposition",
        )
        ready = True
    elif state == "STOP":
        _require(continuation == "RECOVERY_REVIEW", "stop-terminal-requires-recovery-review-continuation")
        _require(learning == "RECOVERY_REVIEW_ONLY", "stop-learning-routes-through-recovery-review")
        ready = True
    elif state == "TERMINATE":
        _require(continuation == "NON_PROPAGATING", "terminate-terminal-must-be-non-propagating")
        _require(learning == "NON_PROPAGATING", "terminate-learning-must-be-non-propagating")
        ready = True
    return {
        "state": state,
        "continuation_disposition": continuation,
        "learning_disposition": learning,
        "terminal_ready": ready,
        "result": "PASS",
    }


def _progress_gate(candidate: dict[str, Any]) -> dict[str, Any]:
    if candidate.get("long_running_execution") is not True:
        return {"applicable": False, "result": "PASS"}
    raw = candidate.get("progress_observability")
    _require(isinstance(raw, dict), "long-running-pm-work-requires-progress-observability")
    _require(raw.get("heartbeat_enabled") is True, "long-running-pm-work-requires-heartbeat")
    _require(raw.get("phase_updates_enabled") is True, "long-running-pm-work-requires-phase-updates")
    _require(raw.get("silent_stall_surface_prohibited") is True, "long-running-pm-work-silent-stall-prohibited")
    return {
        "applicable": True,
        "result": "PASS",
        "heartbeat_enabled": True,
        "phase_updates_enabled": True,
        "silent_stall_surface_prohibited": True,
    }


def govern_project_manager_event(
    *,
    candidate: dict[str, Any],
    canonical_next_action: dict[str, Any],
    session: dict[str, Any],
    profile_binding: dict[str, Any],
    profile_verifier: Any | None,
) -> dict[str, Any]:
    _require(isinstance(candidate, dict), "pm-governance-candidate-object-required")
    _require(candidate.get("schema") == SCHEMA, "pm-governance-candidate-schema-mismatch")
    _require(isinstance(canonical_next_action, dict), "canonical-next-action-object-required")
    _require(isinstance(session, dict), "control-session-object-required")
    event_ref = str(candidate.get("event_ref") or "").strip()
    _require(event_ref, "pm-governance-event-ref-required")

    profile = _verify_profile(
        profile_binding=profile_binding,
        session=session,
        profile_verifier=profile_verifier,
    )
    frontier, next_action = _validate_frontier(candidate, canonical_next_action)
    shared = _shared_write_gate(candidate)
    worker = _worker_terminal_gate(candidate)
    interrupt = _interrupt_gate(candidate)
    terminal = _terminal_gate(candidate)
    progress = _progress_gate(candidate)

    if next_action["owner"] == "HUMAN":
        _require(terminal.get("state") not in {"TERMINATE"}, "terminated-pm-event-cannot-produce-human-continuation")
    if terminal.get("terminal_ready") is True:
        _require(next_action["action_ref"] == "NONE", "terminal-ready-event-cannot-have-incomplete-frontier")

    decision: dict[str, Any] = {
        "schema": DECISION_SCHEMA,
        "authority": "MCP",
        "direct_live_authority": False,
        "state_mutation_by_governor": False,
        "profile_verification": profile,
        "event_ref": event_ref,
        "session_ref": session.get("session_ref"),
        "frontier": frontier,
        "next_action": next_action,
        "shared_write_gate": shared,
        "worker_terminal_gate": worker,
        "interrupt_gate": interrupt,
        "terminal_gate": terminal,
        "progress_observability_gate": progress,
        "human_continuation_allowed": next_action["human_continuation_allowed"],
        "pm_internal_action_must_run_before_human_boundary": (
            next_action["owner"] == "MACHINE" and next_action["required_before_event_closure"] is True
        ),
        "decision_fingerprint": "",
        "decision_ref": "",
        "result": "PASS",
    }
    decision["decision_fingerprint"] = _fingerprint(decision)
    decision["decision_ref"] = "PMCG-" + decision["decision_fingerprint"][:24].upper()
    return decision


def selftest() -> dict[str, Any]:
    tests: list[dict[str, Any]] = []

    def check(name: str, fn) -> None:
        try:
            ok = bool(fn())
            tests.append({"name": name, "result": "PASS" if ok else "FAIL"})
        except Exception as exc:
            tests.append({"name": name, "result": "FAIL", "detail": str(exc)})

    class Verifier:
        def verify(self, *, binding: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
            if binding.get("binding_id") != "PM-BINDING-1":
                return {"schema": PROFILE_VERIFICATION_SCHEMA, "result": "BLOCKED"}
            return {
                "schema": PROFILE_VERIFICATION_SCHEMA,
                "result": "PASS",
                "profile": "PROJECT_MANAGER",
                "session_ref": session["session_ref"],
                "binding_fingerprint": "a"*64,
                "verifier_ref": "SELFTEST-CONSTRUCTOR-BOUND",
            }

    verifier = Verifier()
    session = {"session_ref": "SESSION-PM-1"}
    binding = {"binding_id": "PM-BINDING-1"}

    def valid_candidate() -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "event_ref": "EVENT-PM-1",
            "frontier_actions": [
                {
                    "action_ref": "PM-REFRESH-SHARED",
                    "actor": "PROJECT_MANAGER",
                    "state": "PENDING",
                    "internally_executable": True,
                    "human_action_required": False,
                }
            ],
            "long_running_execution": True,
            "progress_observability": {
                "heartbeat_enabled": True,
                "phase_updates_enabled": True,
                "silent_stall_surface_prohibited": True,
            },
        }

    canonical_machine = {
        "action_ref": "PM-REFRESH-SHARED",
        "owner": "MACHINE",
        "internally_executable": True,
        "required_before_event_closure": True,
    }

    check("verified-profile-and-pm-internal-frontier-pass", lambda: govern_project_manager_event(
        candidate=valid_candidate(), canonical_next_action=canonical_machine, session=session,
        profile_binding=binding, profile_verifier=verifier
    )["pm_internal_action_must_run_before_human_boundary"] is True)

    def expect_block(mutator, *, canonical=canonical_machine, verifier_arg=verifier, binding_arg=binding) -> bool:
        c = valid_candidate()
        mutator(c)
        try:
            govern_project_manager_event(
                candidate=c, canonical_next_action=canonical, session=session,
                profile_binding=binding_arg, profile_verifier=verifier_arg
            )
        except ProjectManagerGovernorError:
            return True
        return False

    check("self-asserted-profile-without-verifier-blocks", lambda: expect_block(lambda c: None, verifier_arg=None))
    check("pm-owned-action-cannot-be-human-handoff", lambda: expect_block(
        lambda c: c["frontier_actions"][0].update({"human_action_required": True})
    ))
    check("canonical-frontier-mismatch-blocks", lambda: expect_block(
        lambda c: c["frontier_actions"][0].update({"action_ref": "OTHER"})
    ))
    check("human-frontier-cannot-self-create-human-boundary", lambda: expect_block(
        lambda c: c["frontier_actions"][0].update({
            "actor": "HUMAN",
            "internally_executable": False,
            "human_action_required": True,
        }),
        canonical={"action_ref":"NONE","owner":"NONE"},
    ))

    def completed_human_then_pm() -> bool:
        c = valid_candidate()
        c["frontier_actions"] = [
            {
                "action_ref": "HUMAN-RUN-HOST",
                "actor": "HUMAN",
                "state": "COMPLETE",
                "internally_executable": False,
                "human_action_required": True,
            },
            {
                "action_ref": "PM-READ-RESULT",
                "actor": "PROJECT_MANAGER",
                "state": "PENDING",
                "internally_executable": True,
                "human_action_required": False,
            },
        ]
        out = govern_project_manager_event(
            candidate=c,
            canonical_next_action={"action_ref":"NONE","owner":"NONE"},
            session=session, profile_binding=binding, profile_verifier=verifier,
        )
        return out["next_action"]["action_ref"] == "PM-READ-RESULT" and out["human_continuation_allowed"] is False
    check("completed-human-work-is-removed-from-frontier", completed_human_then_pm)

    check("duplicate-shared-writer-blocks", lambda: expect_block(
        lambda c: c.update({"shared_write_transaction":{
            "semantic_intent_id":"SAME","idempotency_key":"ID-1","active_semantic_writer_count":2,
            "overlapping_attempt_count":0,"retry_without_fresh_read":False
        }})
    ))
    check("shared-overlapping-attempt-blocks", lambda: expect_block(
        lambda c: c.update({"shared_write_transaction":{
            "semantic_intent_id":"SAME","idempotency_key":"ID-1","active_semantic_writer_count":1,
            "overlapping_attempt_count":1,"retry_without_fresh_read":False
        }})
    ))
    check("shared-retry-without-fresh-read-blocks", lambda: expect_block(
        lambda c: c.update({"shared_write_transaction":{
            "semantic_intent_id":"SAME","idempotency_key":"ID-1","active_semantic_writer_count":1,
            "overlapping_attempt_count":0,"retry_without_fresh_read":True
        }})
    ))
    check("worker-self-admission-blocks", lambda: expect_block(
        lambda c: c.update({"worker_terminal_admission":{
            "expected_worker_refs":["W1"],"admitted_terminal_refs":["W1"],
            "worker_self_admission_present":True,"pm_independent_admission_verified":True,
            "frozen_integration_input_manifest":True
        }})
    ))
    check("integrator-before-all-terminal-admissions-blocks", lambda: expect_block(
        lambda c: c.update({"worker_terminal_admission":{
            "expected_worker_refs":["W1","W2"],"admitted_terminal_refs":["W1"],
            "worker_self_admission_present":False,"pm_independent_admission_verified":True,
            "frozen_integration_input_manifest":False,"integrator_open_requested":True
        }})
    ))
    check("pause-preserve-and-revalidate-pass", lambda: govern_project_manager_event(
        candidate={**valid_candidate(),"interrupt":{
            "intent":"PAUSE","claim_reserved":True,"fresh_revalidation_on_resume":True,"continuation_propagates":True
        }},
        canonical_next_action=canonical_machine,session=session,profile_binding=binding,profile_verifier=verifier
    )["interrupt_gate"]["intent"]=="PAUSE")
    check("stop-direct-continuation-blocks", lambda: expect_block(
        lambda c: c.update({"interrupt":{
            "intent":"STOP","claim_released":True,"partial_work_quarantined":True,
            "recovery_review_required":True,"direct_continuation_allowed":True
        }})
    ))
    check("terminate-substantive-inheritance-blocks", lambda: expect_block(
        lambda c: c.update({"interrupt":{
            "intent":"TERMINATE","claim_released":True,"minimal_tombstone_retained":True,
            "continuation_propagates":False,"substantive_inheritance_allowed":True
        }})
    ))

    def terminal_missing_learning() -> bool:
        c = valid_candidate()
        c["frontier_actions"][0]["state"] = "COMPLETE"
        c["terminal"] = {"state":"COMPLETE","continuation_disposition":"NONE_REQUIRED","learning_disposition":"PENDING"}
        try:
            govern_project_manager_event(candidate=c,canonical_next_action={"action_ref":"NONE","owner":"NONE"},
                session=session,profile_binding=binding,profile_verifier=verifier)
        except ProjectManagerGovernorError:
            return True
        return False
    check("complete-without-learning-disposition-blocks", terminal_missing_learning)

    def valid_terminal() -> bool:
        c = valid_candidate()
        c["frontier_actions"][0]["state"] = "COMPLETE"
        c["terminal"] = {
            "state":"COMPLETE","continuation_disposition":"NONE_REQUIRED",
            "learning_disposition":"GENERALIZABLE_LEARNING_CANDIDATE"
        }
        out=govern_project_manager_event(candidate=c,canonical_next_action={"action_ref":"NONE","owner":"NONE"},
            session=session,profile_binding=binding,profile_verifier=verifier)
        return out["terminal_gate"]["terminal_ready"] is True
    check("complete-with-continuation-and-learning-disposition-passes", valid_terminal)

    check("long-running-without-heartbeat-blocks", lambda: expect_block(
        lambda c: c.update({"progress_observability":{
            "heartbeat_enabled":False,"phase_updates_enabled":True,"silent_stall_surface_prohibited":True
        }})
    ))

    passed=sum(1 for x in tests if x["result"]=="PASS")
    return {
        "schema":"cerebro-project-manager-control-governor-selftest/v1",
        "result":"PASS" if passed==len(tests) else "FAIL",
        "tests":tests,
        "passed":passed,
        "total":len(tests),
    }


def main() -> int:
    result=selftest()
    print(json.dumps(result,indent=2,ensure_ascii=False))
    return 0 if result["result"]=="PASS" else 1


if __name__=="__main__":
    raise SystemExit(main())
