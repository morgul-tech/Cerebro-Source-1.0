"""Task-local profile sidecar; local assertions never grant or attest capability."""
from __future__ import annotations
import hashlib
import json

SCHEMA = "cerebro-task-local-profile/v1"
PROFILE_FIELDS = {"source_ref", "source_revision", "primary_coat", "lenses",
                  "packages", "required_tools"}
TASK_FIELDS = ("task_ref", "task_revision", "actor_ref", "generation_ref",
               "source_head", "effect_class", "live_scope", "authority_class",
               "allowed_paths", "required_invariants", "stop_edges")
ASSERTION = "LOCAL_RECORDED_NOT_IDENTITY_TOOL_GRANT_OR_USE_ATTESTATION"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def text(value):
    if not isinstance(value, str) or not value.strip() or value != value.strip() or "\0" in value:
        raise ValueError("PROFILE_TEXT_REQUIRED")
    return value


def fields(value, expected):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ValueError("PROFILE_FIELDS_INVALID")


def unique(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("PROFILE_DUPLICATE_KEY")
        result[key] = value
    return result


def decode(raw):
    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("PROFILE_NONFINITE")))


def binding(task, profile, capture_raw, original):
    fields(profile, PROFILE_FIELDS)
    for name in ("source_ref", "source_revision", "primary_coat"):
        text(profile[name])
    for name in ("lenses", "packages", "required_tools"):
        values = profile[name]
        if not isinstance(values, list) or any(text(x) != x for x in values) or len(set(values)) != len(values):
            raise ValueError("PROFILE_LIST_INVALID")
    return {"schema": SCHEMA, "task": {k: task[k] for k in TASK_FIELDS},
            "profile": profile, "capture_sha256": sha(capture_raw),
            "original_sha256": sha(original), "authority": "NONE",
            "assertion": ASSERTION, "uptake": "NOT_ATTESTED"}


def read_binding(raw, task, capture_raw, original):
    value = decode(raw)
    fields(value, {"schema", "task", "profile", "capture_sha256", "original_sha256",
                   "authority", "assertion", "uptake"})
    expected = binding(task, value["profile"], capture_raw, original)
    if value != expected or raw != canonical(expected):
        raise ValueError("PROFILE_CAPTURE_BINDING_MISMATCH")
    return value


def consume(bound, observation, returned, original_sha256):
    """Record caller-sourced observations; never infer LOADED/USED from selection."""
    for name in ("task_ref", "task_revision", "actor_ref", "generation_ref"):
        if returned[name] != bound["task"][name]:
            raise ValueError("PROFILE_WORKER_TASK_MISMATCH")
    if returned["original_sha256"] != original_sha256:
        raise ValueError("PROFILE_RETURN_ORIGINAL_MISMATCH")
    base = {"binding_sha256": sha(canonical(bound)), "binding": bound,
            "assertion": ASSERTION, "authority": "NONE",
            "identity_attested": False, "tools_attested": False,
            "recipient_use_attested": False}
    if observation is None:
        return {**base, "uptake": {"state": "NOT_ATTESTED", "evidence_ref": None},
                "result": {"state": "NOT_OBSERVED", "evidence_ref": None},
                "recipient_use": {"state": "NOT_OBSERVED", "evidence_ref": None},
                "relevant_delta": {"state": "NOT_OBSERVED", "evidence_ref": None},
                "available_tools": []}
    fields(observation, {"schema", "binding_sha256", "task_ref", "task_revision",
                         "actor_ref", "generation_ref", "profile_source_ref",
                         "profile_source_revision", "uptake", "result",
                         "recipient_use", "relevant_delta", "available_tools"})
    if (observation["schema"] != SCHEMA + "-observation"
            or observation["binding_sha256"] != base["binding_sha256"]
            or any(observation[k] != bound["task"][k] for k in
                   ("task_ref", "task_revision", "actor_ref", "generation_ref"))
            or observation["profile_source_ref"] != bound["profile"]["source_ref"]
            or observation["profile_source_revision"] != bound["profile"]["source_revision"]):
        raise ValueError("PROFILE_OBSERVATION_BINDING_MISMATCH")
    states = {"uptake": {"NOT_ATTESTED", "LOADED", "USED"},
              "result": {"NOT_OBSERVED", "DELIVERED"},
              "recipient_use": {"NOT_OBSERVED", "USED"},
              "relevant_delta": {"NOT_OBSERVED", "NONE_REPORTED", "REPORTED"}}
    for name, allowed in states.items():
        item = observation[name]
        fields(item, {"state", "evidence_ref"})
        if item["state"] not in allowed:
            raise ValueError("PROFILE_OBSERVATION_STATE_INVALID")
        unknown = item["state"] in {"NOT_ATTESTED", "NOT_OBSERVED"}
        if unknown:
            if item["evidence_ref"] is not None:
                raise ValueError("PROFILE_UNKNOWN_EVIDENCE_CONFLICT")
        else:
            text(item["evidence_ref"])
    tools = observation["available_tools"]
    if not isinstance(tools, list):
        raise ValueError("PROFILE_TOOL_LIST_INVALID")
    seen = {}
    for tool in tools:
        fields(tool, {"tool_ref", "state", "evidence_ref"})
        name = text(tool["tool_ref"])
        if name in seen or tool["state"] not in {"AVAILABLE", "UNAVAILABLE", "NOT_ATTESTED"}:
            raise ValueError("PROFILE_TOOL_STATE_INVALID")
        if tool["state"] == "NOT_ATTESTED":
            if tool["evidence_ref"] is not None:
                raise ValueError("PROFILE_UNKNOWN_EVIDENCE_CONFLICT")
        else:
            text(tool["evidence_ref"])
        seen[name] = tool["state"]
    required = bound["profile"]["required_tools"]
    unavailable = sorted(k for k in required if seen.get(k) == "UNAVAILABLE")
    if unavailable:
        raise ValueError("PROFILE_REQUIRED_TOOL_UNAVAILABLE:" + ",".join(unavailable))
    if observation["uptake"]["state"] == "USED":
        missing = sorted(k for k in required if seen.get(k) != "AVAILABLE")
        if missing:
            raise ValueError("PROFILE_REQUIRED_TOOL_NOT_ATTESTED:" + ",".join(missing))
    if (observation["recipient_use"]["state"] == "USED"
            and observation["result"]["state"] != "DELIVERED"):
        raise ValueError("PROFILE_USE_WITHOUT_RESULT")
    return {**base, **{k: observation[k] for k in
            ("uptake", "result", "recipient_use", "relevant_delta", "available_tools")}}


RESUME_SCHEMA = "cerebro-owner-selected-work-resume/v1"
RESUME_BINDING_FIELDS = ("task_ref", "task_revision", "actor_ref", "generation_ref",
                         "carrier_ref", "arc_ref")


def consume_resume(task, original_sha256, profile_sha256, value):
    """Read an explicit owner-selected ordinary work record, never infer an order."""
    fields(value, {"schema", "binding", "owner_selection_ref", "current_task",
                   "received_order", "current_blocks", "inspection", "cases",
                   "learning", "fact_delta"})
    if (value["schema"] != RESUME_SCHEMA or value["binding"] !=
            {**{k: task[k] for k in RESUME_BINDING_FIELDS}, "original_sha256": original_sha256}):
        raise ValueError("RESUME_ORIGINAL_TASK_BINDING_MISMATCH")
    selection = text(value["owner_selection_ref"])
    current = value["current_task"]
    fields(current, {"task_ref", "task_revision", "owner_ref", "next_action_ref",
                     "attempt_ref", "evidence_refs", "closed_arcs"})
    for key in ("task_ref", "task_revision", "owner_ref", "next_action_ref", "attempt_ref"):
        text(current[key])
    for key in ("evidence_refs", "closed_arcs"):
        if not isinstance(current[key], list) or any(text(x) != x for x in current[key]):
            raise ValueError("RESUME_CURRENT_LIST_INVALID")
    if not current["evidence_refs"]:
        raise ValueError("RESUME_CURRENT_OWNER_EVIDENCE_REQUIRED")
    if any(current[k] != task[k] for k in ("task_ref", "task_revision")):
        raise ValueError("RESUME_SELECTED_CURRENT_TASK_MISMATCH")
    received = value["received_order"]
    fields(received, {"kind", "selection_ref", "task_ref", "task_revision", "owner_ref", "original_ref"})
    if received["kind"] == "NO_NEW_ORDER":
        if any(received[k] is not None for k in received if k != "kind"):
            raise ValueError("RESUME_NO_NEW_ORDER_CONFLICT")
    elif received["kind"] == "OWNER_SELECTED_NEW_ORDER":
        for k in received:
            text(received[k])
        if (received["selection_ref"] != selection or received["owner_ref"] != current["owner_ref"]
                or any(received[k] != current[k] for k in ("task_ref", "task_revision"))):
            raise ValueError("RESUME_RECEIVED_ORDER_NOT_SELECTED_CURRENT")
    else:
        raise ValueError("RESUME_EXPLICIT_ORDER_KIND_REQUIRED")
    blocks = value["current_blocks"]
    if not isinstance(blocks, list):
        raise ValueError("RESUME_CURRENT_BLOCKS_REQUIRED")
    current_count = 0
    for block in blocks:
        fields(block, {"task_ref", "task_revision", "state", "superseded_by"})
        text(block["task_ref"]); text(block["task_revision"])
        if (block["state"] in {"CLOSED", "SUPERSEDED"} and
                all(block[k] == current[k] for k in ("task_ref", "task_revision"))):
            raise ValueError("RESUME_CURRENT_PAIR_MARKED_HISTORICAL")
        if block["state"] == "CURRENT":
            current_count += 1
            if (block["superseded_by"] is not None or
                    any(block[k] != current[k] for k in ("task_ref", "task_revision"))):
                raise ValueError("RESUME_CONTRADICTORY_CURRENT_BLOCK")
        elif block["state"] == "SUPERSEDED":
            if block["superseded_by"] != selection:
                raise ValueError("RESUME_SUPERSESSION_OWNER_SELECTION_REQUIRED")
        elif block["state"] == "CLOSED":
            if block["task_ref"] not in current["closed_arcs"] or block["superseded_by"] is not None:
                raise ValueError("RESUME_CLOSED_ARC_NOT_RETAINED")
        else:
            raise ValueError("RESUME_BLOCK_STATE_INVALID")
    if current_count != 1:
        raise ValueError("RESUME_ONE_CURRENT_TASK_REQUIRED")
    cases = value["cases"]
    if not isinstance(cases, list):
        raise ValueError("RESUME_CASE_LIST_INVALID")
    seen = set()
    for case in cases:
        fields(case, {"case_ref", "scope_ref", "period_ref", "analysis_owner_ref",
                      "finish_criterion_ref", "state", "pending_obligations"})
        for k in ("case_ref", "scope_ref", "period_ref", "analysis_owner_ref", "finish_criterion_ref"):
            text(case[k])
        if case["case_ref"] in seen or case["state"] not in {"ACTIVE", "PENDING", "COMPLETE"}:
            raise ValueError("RESUME_CASE_STATE_OR_DUPLICATE_INVALID")
        seen.add(case["case_ref"])
        if not isinstance(case["pending_obligations"], list) or any(text(x) != x for x in case["pending_obligations"]):
            raise ValueError("RESUME_CASE_PENDING_INVALID")
    inspection = value["inspection"]
    fields(inspection, {"intent", "authorization_ref", "case_ref"})
    if inspection["intent"] == "EXPLICIT_AUTHORIZED_ROOM_INSPECTION":
        text(inspection["authorization_ref"]); text(inspection["case_ref"])
        if inspection["case_ref"] not in seen:
            raise ValueError("RESUME_INSPECTION_EXISTING_CASE_BIND_REQUIRED")
    elif inspection["intent"] in {"STATUS", "NONE"}:
        if inspection["authorization_ref"] is not None or inspection["case_ref"] is not None:
            raise ValueError("RESUME_STATUS_NOT_INSPECTION_ORDER")
    else:
        raise ValueError("RESUME_EXPLICIT_INSPECTION_KIND_REQUIRED")
    learning = value["learning"]
    fields(learning, {"consumer_ref", "method_source_ref", "source_revision",
                      "profile_binding_sha256", "known_countermodel_refs",
                      "next_choice_ref", "use_state", "use_evidence_ref"})
    for key in ("consumer_ref", "method_source_ref", "source_revision", "next_choice_ref"):
        text(learning[key])
    if (learning["method_source_ref"] != "standards/development/implementation-learning-loop.yaml"
            or learning["source_revision"] != task["source_head"]
            or learning["profile_binding_sha256"] != profile_sha256):
        raise ValueError("RESUME_LEARNING_SOURCE_PROFILE_MISMATCH")
    if not isinstance(learning["known_countermodel_refs"], list) or any(text(x) != x for x in learning["known_countermodel_refs"]):
        raise ValueError("RESUME_COUNTERMODEL_REFS_INVALID")
    if learning["use_state"] == "NOT_ATTESTED":
        if learning["use_evidence_ref"] is not None:
            raise ValueError("RESUME_USE_UNKNOWN_CONFLICT")
    elif learning["use_state"] == "USED":
        text(learning["use_evidence_ref"])
    else:
        raise ValueError("RESUME_USE_STATE_INVALID")
    facts = value["fact_delta"]
    if facts is not None:
        fields(facts, {"premise_ref", "supersedes_evidence_ref", "current_evidence_ref",
                       "store_state", "connections", "subscriptions"})
        for k in ("premise_ref", "supersedes_evidence_ref", "current_evidence_ref", "store_state"):
            text(facts[k])
        if facts["supersedes_evidence_ref"] == facts["current_evidence_ref"]:
            raise ValueError("RESUME_FRESH_FACT_DELTA_REQUIRED")
        if any(type(facts[k]) is not int or facts[k] < 0 for k in ("connections", "subscriptions")):
            raise ValueError("RESUME_FACT_COUNT_INVALID")
        facts = {**facts, "active_clients": None, "assertion": "OWNER_REPORTED_NO_CLIENT_COUNT_INFERENCE"}
    return {"schema": RESUME_SCHEMA + "-receipt", "binding": value["binding"],
            "owner_selection_ref": selection, "current_task": current,
            "received_order": received, "history_blocks": blocks,
            "inspection": inspection, "preserved_cases": cases,
            "learning": learning, "fact_delta": facts,
            "local_record_consumed": True, "actual_worker_use_attested": False,
            "next_material_step_qualified": True, "authority": "NONE",
            "assertion": "OWNER_SELECTED_CALLER_METADATA_NOT_IDENTITY_GRANT_OR_GLOBAL_CHAT_HOOK"}
