"""First-error/material-return encounter. No operation, grant or runtime attestation."""
from __future__ import annotations
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = "cerebro-work-error-reflex/v1"
SCHEMA_V2 = "cerebro-work-error-reflex/v2"
IDENTITY = ("task_ref", "task_revision", "actor_ref", "generation_ref", "carrier_ref", "arc_ref")
METHOD_REFS = ("standards/development/implementation-learning-loop.yaml",
               "standards/development/reflective-adjustment-loop.yaml")
STAGES = {"UNKNOWN": -1, "LOCAL_START": 0, "UNLOCK": 1, "REMOTE": 2, "RUNTIME": 3, "EFFECT": 4}
LAYERS = {"UNKNOWN": -1, "LOCAL_ENVIRONMENT": 0, "LOCAL_PATH": 0,
          "UNLOCK": 1, "REMOTE_IMPORT": 2, "RUNTIME": 3, "EFFECT": 4}


def exact(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError("WORK_REFLEX_FIELDS_INVALID")


def text(value):
    if not isinstance(value, str) or not value.strip() or value != value.strip() or "\0" in value:
        raise ValueError("WORK_REFLEX_REFERENCE_REQUIRED")
    return value


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError("WORK_REFLEX_CONSUMER_UNAVAILABLE:" + str(path))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def local_failure(reason):
    """Called by actual normal preparation exception path; never invent its cause."""
    return {"schema": SCHEMA + "-local-error", "observation": str(reason).split(":", 1)[0],
            "reached_stage": "LOCAL_PREPARATION", "cause": "UNKNOWN",
            "native_effect": "NONE_BY_THIS_API", "local_output_state": "RECONCILE_IF_UNCERTAIN",
            "next_step": "CHECK_EXACT_INPUT_OR_LOCAL_OUTPUT_BEFORE_REPEAT",
            "authority": "NONE", "runtime_closed": False}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def validate_learning_context(value):
    """Bound caller-sanitized data; no secret scrubber, reader or authority."""
    fields = {"schema", "sanitized_failure", "challenged_premise", "considered_decision",
              "epistemic_state", "contract_ref", "edition_ref", "applicability", "original_ref", "tags"}
    exact(value, fields)
    if value["schema"] != "cerebro-learning-context/v1" or value["epistemic_state"] not in {
            "OBSERVED", "HYPOTHESIS", "UNKNOWN"}:
        raise ValueError("WORK_REFLEX_LEARNING_CONTEXT_SCHEMA_OR_STATE")
    try:
        for name in fields - {"tags"}:
            text(value[name])
            if len(value[name].encode("utf-8")) > 2048:
                raise ValueError("WORK_REFLEX_LEARNING_TEXT_BOUND")
        tags = value["tags"]
        if (type(tags) is not list or len(tags) > 16
                or any(text(tag) != tag or len(tag.encode("utf-8")) > 96 for tag in tags)
                or len(set(tags)) != len(tags) or len(canonical(value)) > 16384):
            raise ValueError("WORK_REFLEX_LEARNING_CONTEXT_BOUND")
    except UnicodeError as exc:
        raise ValueError("WORK_REFLEX_LEARNING_UTF8_REQUIRED") from exc
    return json.loads(canonical(value))


def learning_material(event, context, encounter):
    value = {"schema": "cerebro-learning-material/v1", "binding": event["binding"],
             "observation_ref": event["observation_ref"], "attempt_ref": event["attempt_ref"],
             "learning_context": context, "source_learning_encounter": encounter,
             "authority": "EVIDENCE_ONLY"}
    if len(canonical(value)) > 32768:
        raise ValueError("WORK_REFLEX_LEARNING_MATERIAL_BOUND")
    return json.loads(canonical(value))


def evaluate(task, original_sha256, profile_sha256, returned, event, *, root=ROOT):
    v2 = isinstance(event, dict) and event.get("schema") == SCHEMA_V2
    exact(event, {"schema", "binding", "profile_binding_sha256", "trigger", "observation_ref",
                  "attempt_ref", "reached_stage", "effect_state", "cause", "challenged_premise",
                  "inherited_countermodel_refs", "next_step", "retry", "runtime_evidence"} |
          ({"learning_context"} if v2 else set()))
    context = validate_learning_context(event["learning_context"]) if v2 else None
    if (event["schema"] not in {SCHEMA, SCHEMA_V2} or event["binding"] !=
            {**{k: task[k] for k in IDENTITY}, "original_sha256": original_sha256}
            or event["profile_binding_sha256"] != profile_sha256
            or any(returned[k] != task[k] for k in IDENTITY)
            or returned["original_sha256"] != original_sha256):
        raise ValueError("WORK_REFLEX_TASK_OR_PROFILE_BINDING_MISMATCH")
    if event["trigger"] not in {"FIRST_UNEXPECTED_ERROR", "MATERIAL_RETURN", "CONTEXT_BREAK"}:
        raise ValueError("WORK_REFLEX_TRIGGER_INVALID")
    for name in ("observation_ref", "attempt_ref"):
        text(event[name])
    if event["reached_stage"] not in STAGES or event["effect_state"] not in {
            "NONE_OBSERVED", "UNKNOWN", "APPLIED_OBSERVED", "RECONCILED"}:
        raise ValueError("WORK_REFLEX_STAGE_EFFECT_INVALID")
    cause, premise, step = event["cause"], event["challenged_premise"], event["next_step"]
    exact(cause, {"state", "layer", "evidence_ref"})
    exact(premise, {"ref", "state"})
    exact(step, {"kind", "justification_ref", "proof_limit_ref", "attempt_ref", "uses_premise_ref"})
    if cause["state"] not in {"PROVEN", "HYPOTHESIS", "UNKNOWN"} or cause["layer"] not in LAYERS:
        raise ValueError("WORK_REFLEX_CAUSE_INVALID")
    if cause["state"] == "UNKNOWN":
        if cause["evidence_ref"] is not None or cause["layer"] != "UNKNOWN":
            raise ValueError("WORK_REFLEX_UNKNOWN_CAUSE_CONFLICT")
    else:
        text(cause["evidence_ref"])
        if LAYERS[cause["layer"]] > STAGES[event["reached_stage"]]:
            raise ValueError("WORK_REFLEX_CAUSE_AT_UNREACHED_LAYER")
    if cause["state"] == "PROVEN" and cause["layer"] == "UNKNOWN":
        raise ValueError("WORK_REFLEX_PROVEN_LAYER_REQUIRED")
    if premise["state"] not in {"UNCHALLENGED", "CHALLENGED", "FALSIFIED"}:
        raise ValueError("WORK_REFLEX_PREMISE_STATE_INVALID")
    if premise["state"] == "UNCHALLENGED":
        if premise["ref"] is not None:
            raise ValueError("WORK_REFLEX_PREMISE_CONFLICT")
    else:
        text(premise["ref"])
    if (premise["state"] == "FALSIFIED" and step["uses_premise_ref"] == premise["ref"]):
        raise ValueError("WORK_REFLEX_FALSIFIED_PREMISE_REUSED")
    for name in ("justification_ref", "proof_limit_ref", "attempt_ref"):
        text(step[name])
    if step["uses_premise_ref"] is not None:
        text(step["uses_premise_ref"])
    if step["kind"] not in {"QUICK_FIX", "DIAGNOSE", "RECONCILE", "BOUNDED_RETRY",
                           "RESTORE_PREREQUISITE", "STRUCTURAL_CHANGE", "RETURN_FINDING"}:
        raise ValueError("WORK_REFLEX_NEXT_STEP_INVALID")
    counters = event["inherited_countermodel_refs"]
    if not isinstance(counters, list) or any(text(x) != x for x in counters):
        raise ValueError("WORK_REFLEX_COUNTERMODEL_REFS_INVALID")
    evidence = event["runtime_evidence"]
    exact(evidence, {"code", "startup", "operation", "cleanup", "evidence_refs"})
    if (evidence["code"] not in {"PASS", "FAIL", "UNKNOWN"}
            or evidence["startup"] not in {"ACTIVE", "FAILED", "UNKNOWN"}
            or evidence["operation"] not in {"READBACK", "REFUSED", "UNKNOWN"}
            or evidence["cleanup"] not in {"ERROR_STOP_CLOSED", "UNKNOWN"}):
        raise ValueError("WORK_REFLEX_CHECKPOINT_INVALID")
    if not isinstance(evidence["evidence_refs"], list) or any(text(x) != x for x in evidence["evidence_refs"]):
        raise ValueError("WORK_REFLEX_EVIDENCE_REFS_INVALID")
    if any(evidence[k] != "UNKNOWN" for k in ("code", "startup", "operation", "cleanup")) and not evidence["evidence_refs"]:
        raise ValueError("WORK_REFLEX_CHECKPOINT_EVIDENCE_REQUIRED")
    decision, allowed = "OBSERVE_CHEAP_DISCRIMINATOR", step["kind"] in {"DIAGNOSE", "RETURN_FINDING"}
    if event["effect_state"] == "UNKNOWN":
        decision = "RECONCILE_SAME_ATTEMPT"
        allowed = False  # A proposed read/reconcile is not an observed readback.
    elif step["kind"] == "BOUNDED_RETRY":
        retry = event["retry"]
        exact(retry, {"contract_ref", "evidence_ref", "qualified", "idempotent",
                      "attempt_number", "max_attempts", "expires_at", "failure_class"})
        for name in ("contract_ref", "evidence_ref"):
            text(retry[name])
        if (retry["failure_class"] != "KNOWN_TRANSIENT" or retry["qualified"] is not True or retry["idempotent"] is not True
                or type(retry["attempt_number"]) is not int or type(retry["max_attempts"]) is not int
                or not 0 <= retry["attempt_number"] < retry["max_attempts"]
                or type(retry["expires_at"]) not in {int, float} or not math.isfinite(retry["expires_at"]) or retry["expires_at"] <= time.time()):
            raise ValueError("WORK_REFLEX_RETRY_CONTRACT_UNQUALIFIED_OR_EXPIRED")
        if premise["state"] == "FALSIFIED":
            raise ValueError("WORK_REFLEX_SEMANTIC_RETRY_NOT_TRANSIENT")
        decision, allowed = "QUALIFIED_BOUNDED_TRANSIENT_RETRY", True
    elif event["retry"] is not None:
        raise ValueError("WORK_REFLEX_UNUSED_RETRY_CONFLICT")
    elif cause["state"] == "PROVEN" and step["kind"] in {"QUICK_FIX", "RESTORE_PREREQUISITE"}:
        decision, allowed = "DIRECT_KNOWN_FIX", True
    elif event["trigger"] == "CONTEXT_BREAK" and step["kind"] != "DIAGNOSE":
        decision, allowed = "RESTORE_ORIGINAL_AND_CHALLENGED_BASIS", step["kind"] == "RETURN_FINDING"
    elif step["kind"] == "RECONCILE":
        decision = "RECONCILE_SAME_ATTEMPT"
        allowed = (event["effect_state"] == "RECONCILED" and evidence["operation"] == "READBACK"
                   and bool(evidence["evidence_refs"]) and step["attempt_ref"] == event["attempt_ref"])
    refs = [{"path": name, "sha256": hashlib.sha256((root / name).read_bytes()).hexdigest()}
            for name in METHOD_REFS]
    learning, vinkel = None, None
    if decision != "DIRECT_KNOWN_FIX" and decision != "QUALIFIED_BOUNDED_TRANSIENT_RETRY":
        relevance = module("work_reflex_relevance", root / "tooling/context/relevance_engine.py")
        request = {"current_objective": task["task_ref"],
            "current_scope": task["live_scope"], "task_need_ref": event["observation_ref"],
            "learning_projection": "SOURCE_PUBLIC_ENCOUNTER_V2" if v2 else "SOURCE_PUBLIC_ENCOUNTER_V1"}
        if v2:
            request.update(current_failure_state=context["sanitized_failure"] + " " + context["challenged_premise"],
                current_decision_state=context["considered_decision"] + " " + context["applicability"],
                tags=context["tags"], direct_family_refs=counters,
                learning_edition_ref=context["edition_ref"], learning_contract_ref=context["contract_ref"])
        learning = relevance.retrieve(request, root=root)["source_learning_encounter"]
    if step["kind"] == "STRUCTURAL_CHANGE" and event["effect_state"] != "UNKNOWN":
        preflight = module("work_reflex_material", root / "mcp/material_commitment_preflight.py")
        vinkel = preflight.resolve_solution_escalation_preflight({
            "triggered": True, "trigger": "REMEDIATION_TO_NEW_MECHANISM",
            "observed_fact": event["observation_ref"],
            "supported_causal_layer": cause["layer"] if cause["state"] == "PROVEN" else "",
            "proposed_remedy": {"kind": "NEW_MECHANISM", "candidate_id": step["justification_ref"]}})
        decision, allowed = "EXISTING_VINKELPASS_" + vinkel["outcome"], False
    result = {"schema": event["schema"] + "-assessment", "binding": event["binding"],
            "profile_binding_sha256": profile_sha256, "event": event, "method_refs": refs,
            "disposition": decision, "next_material_step_qualified": allowed,
            "source_learning_encounter": learning, "solution_escalation": vinkel,
            "assertion": "CALLER_RECORDED_PUBLIC_METADATA_NOT_CAUSE_OR_RUNTIME_ATTESTATION",
            "authority": "NONE", "runtime_closed": False,
            "reported_checkpoints": evidence, "native_effect": "NONE_BY_THIS_API"}
    if v2:
        result["learning_material"] = learning_material(event, context, learning) if learning is not None else None
    return result
