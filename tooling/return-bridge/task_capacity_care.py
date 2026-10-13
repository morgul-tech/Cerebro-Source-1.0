"""Task-local capacity and earned return-care records for the ordinary Source path.

All inputs are caller-sourced operational observations. This module is not a
scheduler, reservation service, identity oracle, authority grant, or care sender.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

SCHEMA = "cerebro-task-capacity-care/v1"
OBSERVATION_SCHEMA = SCHEMA + "-observation"
CARE_SCHEMA = SCHEMA + "-return-observation"
ASSERTION = "LOCAL_RECORDED_NOT_IDENTITY_AUTHORITY_OR_NATIVE_RESERVATION"
TASK_FIELDS = ("task_ref", "task_revision", "actor_ref", "generation_ref",
               "source_head", "effect_class", "live_scope", "authority_class",
               "allowed_paths", "required_invariants", "stop_edges")
OBS_FIELDS = {"schema", "cut_ref", "cut_revision", "observer_ref", "task_ref",
              "task_revision", "workers", "prior_care_readback"}
WORKER_FIELDS = {"actor_ref", "generation_ref", "state", "fit", "reservation_ref",
                 "reservation_owner_ref", "evidence_ref", "fit_evidence_ref",
                 "profile_source_ref", "profile_source_revision", "ui_state"}
CARE_FIELDS = {"schema", "care_ref", "state", "task_ref", "task_revision",
               "original_sha256", "profile_binding_sha256", "delta_ref",
               "caretaker_ref", "next_consumer_ref", "readback"}
READBACK_FIELDS = {"care_ref", "delta_ref", "source_ref", "source_revision",
                   "source_sha256", "readback_ref"}
OWNER_RECORD_SCHEMA = "cerebro-task-capacity-owner-observation/v1"
OWNER_RECORD_FIELDS = {"schema", "record_ref", "owner_ref", "stage", "task_ref",
                       "task_revision", "source_ref", "source_revision", "currentness",
                       "observed_at", "observation", "authority", "assertion"}
OWNER_RECORD_ASSERTION = "OWNER_ASSERTED_NOT_NATIVE_LOCK"
SCHEMA_V2 = "cerebro-task-capacity-care/v2"
OBSERVATION_SCHEMA_V2 = SCHEMA_V2 + "-observation"
OWNER_RECORD_SCHEMA_V2 = "cerebro-task-capacity-owner-observation/v2"



def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _fields(value, expected, reason):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ValueError(reason)


class CapacityRefusal(ValueError):
    """Fail-closed local decision with bounded owner-route redistribution input."""
    def __init__(self, code: str, observation: dict):
        super().__init__(code)
        self.code = code
        self.details = {
            "schema": SCHEMA + "-refusal/v1",
            "cut_ref": observation["cut_ref"],
            "cut_revision": observation["cut_revision"],
            "task_ref": observation["task_ref"],
            "task_revision": observation["task_revision"],
            "eligible_candidates": [_candidate_ref(w) for w in observation["workers"] if _eligible(w)],
            "input_only_candidates": [_candidate_ref(w) for w in observation["workers"]
                                       if w["state"] == "AVAILABLE" and w["fit"] == "INPUT_ONLY"],
            "authority": "NONE",
            "next_action": "RETURN_TO_OWNER_ROUTE_FOR_TASK_REBIND_OR_MORE_EVIDENCE",
        }


def _candidate_ref(worker):
    return {name: worker[name] for name in
            ("actor_ref", "generation_ref", "profile_source_ref", "profile_source_revision",
             "fit_evidence_ref", "evidence_ref")}


def _text(value, reason):
    if not isinstance(value, str) or not value.strip() or value != value.strip() or "\0" in value:
        raise ValueError(reason)
    return value


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("CAPACITY_DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def decode(raw: bytes):
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique,
                       parse_constant=lambda _: (_ for _ in ()).throw(
                           ValueError("CAPACITY_NONFINITE_JSON")))
    return value


def _observation(raw: bytes):
    value = decode(raw)
    v2 = value.get("schema") == OBSERVATION_SCHEMA_V2 if isinstance(value, dict) else False
    _fields(value, OBS_FIELDS | {"work_phase", "owner_port_contract", "selected_actor_ref", "attempt_ref", "input_kind"} if v2 else OBS_FIELDS,
            "CAPACITY_OBSERVATION_FIELDS_INVALID")
    if raw != canonical(value):
        raise ValueError("CAPACITY_OBSERVATION_NOT_CANONICAL")
    if value["schema"] not in {OBSERVATION_SCHEMA, OBSERVATION_SCHEMA_V2}:
        raise ValueError("CAPACITY_OBSERVATION_SCHEMA_INVALID")
    for name in ("cut_ref", "cut_revision", "observer_ref", "task_ref", "task_revision"):
        _text(value[name], "CAPACITY_" + name.upper() + "_INVALID")
    workers = value["workers"]
    if not isinstance(workers, list) or not workers:
        raise ValueError("CAPACITY_WORKERS_REQUIRED")
    seen = set()
    for worker in workers:
        _fields(worker, WORKER_FIELDS | {"current_work", "pending_bindings", "pending_currentness", "pending_evidence_ref"} if v2 else WORKER_FIELDS, "CAPACITY_WORKER_FIELDS_INVALID")
        for name in ("actor_ref", "generation_ref", "profile_source_ref",
                     "profile_source_revision"):
            _text(worker[name], "CAPACITY_WORKER_ID_INVALID")
        key = (worker["actor_ref"], worker["generation_ref"])
        if key in seen:
            raise ValueError("CAPACITY_WORKER_DUPLICATE")
        seen.add(key)
        if worker["state"] not in {"AVAILABLE", "RUNNING", "RESERVED",
                                   "DEPENDENCY_WAIT", "UNKNOWN"}:
            raise ValueError("CAPACITY_STATE_INVALID")
        if worker["fit"] not in {"FIT", "INPUT_ONLY", "NO_FIT", "UNKNOWN"}:
            raise ValueError("CAPACITY_FIT_INVALID")
        if worker["ui_state"] not in {"ACTIVE", "IDLE", "NOT_LOADED", "UNKNOWN"}:
            raise ValueError("CAPACITY_UI_STATE_INVALID")
        for name in ("reservation_ref", "reservation_owner_ref", "evidence_ref",
                     "fit_evidence_ref"):
            item = worker[name]
            if item is not None:
                _text(item, "CAPACITY_WORKER_EVIDENCE_INVALID")
        reserved = worker["state"] in {"RUNNING", "RESERVED", "DEPENDENCY_WAIT"}
        has_ref = worker["reservation_ref"] is not None
        has_owner = worker["reservation_owner_ref"] is not None
        if reserved and not (has_ref and has_owner):
            raise ValueError("CAPACITY_RESERVATION_STATE_CONFLICT")
        if worker["state"] == "AVAILABLE" and (has_ref or has_owner):
            raise ValueError("CAPACITY_RESERVATION_STATE_CONFLICT")
        if worker["state"] == "UNKNOWN" and has_ref != has_owner:
            raise ValueError("CAPACITY_UNKNOWN_RESERVATION_CONFLICT")
        if worker["state"] == "UNKNOWN" and worker["evidence_ref"] is not None:
            raise ValueError("CAPACITY_UNKNOWN_EVIDENCE_CONFLICT")
        if worker["state"] != "UNKNOWN" and worker["evidence_ref"] is None:
            raise ValueError("CAPACITY_EVIDENCE_REQUIRED")
        if worker["fit"] == "UNKNOWN" and worker["fit_evidence_ref"] is not None:
            raise ValueError("CAPACITY_UNKNOWN_FIT_EVIDENCE_CONFLICT")
        if worker["fit"] != "UNKNOWN" and worker["fit_evidence_ref"] is None:
            raise ValueError("CAPACITY_FIT_EVIDENCE_REQUIRED")
    if value["task_ref"] != value["task_ref"].strip() or value["task_revision"] != value["task_revision"].strip():
        raise ValueError("CAPACITY_TASK_INVALID")
    if v2:
        _text(value["attempt_ref"], "CAPACITY_ATTEMPT_REQUIRED")
        if value["input_kind"] not in {"TASK_ASSIGNMENT", "MATERIAL_INPUT", "SAME_TASK_CONTINUATION"}:
            raise ValueError("CAPACITY_INPUT_KIND_INVALID")
        for worker in workers:
            _work_record(worker)
        _text(value["selected_actor_ref"], "OWNER_PORT_SELECTED_ACTOR_REQUIRED")
        owner_port_assessment(value)
    return value


def _eligible(worker):
    available = worker["state"] == "AVAILABLE" and worker["fit"] == "FIT"
    if "current_work" in worker:
        available = (available and worker["current_work"]["currentness"] == "CURRENT"
                     and worker["current_work"]["state"] in {"NONE", "COMPLETED"}
                     and worker["pending_currentness"] == "CURRENT"
                     and all(p["currentness"] == "CURRENT" for p in worker["pending_bindings"])
                     and not any(p["state"] in {"DISPATCH_INTENT", "UPTAKE"}
                                 for p in worker["pending_bindings"]))
    return available


def _worker_for(observation, actor_ref, generation_ref):
    return next((w for w in observation["workers"]
                 if w["actor_ref"] == actor_ref and w["generation_ref"] == generation_ref), None)


def _decision(observation, task):
    alternatives = [w for w in observation["workers"] if _eligible(w)]
    chosen = _worker_for(observation, task["actor_ref"], task["generation_ref"])
    if chosen is None:
        if alternatives:
            raise CapacityRefusal("CAPACITY_REDISTRIBUTION_REQUIRED", observation)
        raise CapacityRefusal("CAPACITY_NO_ELIGIBLE_WORKER", observation)
    continuation = _work_commitment(chosen, observation, task) if observation["schema"] == OBSERVATION_SCHEMA_V2 else False
    if chosen["state"] == "UNKNOWN" or chosen["fit"] == "UNKNOWN":
        raise CapacityRefusal("CAPACITY_UNKNOWN", observation)
    if chosen["state"] in {"RUNNING", "RESERVED", "DEPENDENCY_WAIT"} and not continuation:
        raise CapacityRefusal("CAPACITY_WORKER_RESERVED", observation)
    if chosen["state"] != "AVAILABLE" and not continuation:
        raise CapacityRefusal("CAPACITY_WORKER_NOT_AVAILABLE", observation)
    if chosen["fit"] == "INPUT_ONLY":
        if alternatives:
            raise CapacityRefusal("CAPACITY_REDISTRIBUTION_REQUIRED", observation)
        raise CapacityRefusal("CAPACITY_INPUT_ONLY_NOT_WHOLE_JOB_FIT", observation)
    if chosen["fit"] != "FIT":
        if alternatives:
            raise CapacityRefusal("CAPACITY_REDISTRIBUTION_REQUIRED", observation)
        raise CapacityRefusal("CAPACITY_WORK_NOT_FIT", observation)
    worker_ref = lambda w: {"actor_ref": w["actor_ref"], "generation_ref": w["generation_ref"]}
    return chosen, [worker_ref(w) for w in alternatives], [
        worker_ref(w) for w in observation["workers"] if w["fit"] == "INPUT_ONLY"]


def bind(task, capture_raw: bytes, original: bytes, profile_binding: dict,
         observation_raw: bytes) -> dict:
    """Bind an observed reservation to the exact existing task/profile/original."""
    obs = _observation(observation_raw)
    if (obs["task_ref"] != task["task_ref"] or obs["task_revision"] != task["task_revision"]):
        raise ValueError("CAPACITY_TASK_BINDING_MISMATCH")
    if obs["schema"] == OBSERVATION_SCHEMA_V2 and obs["selected_actor_ref"] != task["actor_ref"]:
        raise ValueError("OWNER_PORT_SELECTED_ACTOR_MISMATCH")
    worker, eligible, input_only = _decision(obs, task)
    profile_hash = sha(canonical(profile_binding))
    care_consumption = consume_prior_care(profile_binding, obs.get("prior_care_readback"))
    result = {"schema": SCHEMA_V2 if obs["schema"] == OBSERVATION_SCHEMA_V2 else SCHEMA, "task": {k: task[k] for k in TASK_FIELDS},
              "capture_sha256": sha(capture_raw), "original_sha256": sha(original),
              "profile_binding_sha256": profile_hash,
              "observation": obs, "observation_sha256": sha(observation_raw),
              "selected_worker": worker, "eligible_workers": eligible,
              "input_only_workers": input_only,
              "care_update_consumption": care_consumption,
              "authority": "NONE", "assertion": ASSERTION,
              "reservation_status": "CALLER_OBSERVED_NOT_NATIVE_LOCKED"}
    if obs["schema"] == OBSERVATION_SCHEMA_V2:
        result["owner_port_assessment"] = require_owner_ports(obs)
    return result


def read_binding(raw: bytes, task, capture_raw: bytes, original: bytes,
                 profile_binding: dict) -> dict:
    value = decode(raw)
    _fields(value, {"schema", "task", "capture_sha256", "original_sha256",
                    "profile_binding_sha256", "observation", "observation_sha256",
                    "selected_worker", "eligible_workers", "input_only_workers",
                    "care_update_consumption", "authority", "assertion",
                    "reservation_status"} | ({"owner_port_assessment"} if value.get("schema") == SCHEMA_V2 else set()),
             "CAPACITY_BINDING_FIELDS_INVALID")
    obs_raw = canonical(value["observation"])
    expected = bind(task, capture_raw, original, profile_binding, obs_raw)
    if value != expected or raw != canonical(expected):
        raise ValueError("CAPACITY_CAPTURE_BINDING_MISMATCH")
    return value


def check_current(bound: dict, observation_raw: bytes) -> dict:
    """Re-evaluate the same worker against a fresh owner observation."""
    obs = _observation(observation_raw)
    task = bound["task"]
    if (obs["task_ref"] != task["task_ref"] or obs["task_revision"] != task["task_revision"]):
        raise ValueError("CAPACITY_TASK_BINDING_MISMATCH")
    port_result = current_owner_ports(bound, obs)
    worker, _, _ = _decision(obs, task)
    prior = bound["selected_worker"]
    unchanged = worker == prior
    if obs["schema"] == OBSERVATION_SCHEMA_V2:
        unchanged = all(worker[k] == prior[k] for k in ("actor_ref", "generation_ref", "profile_source_ref", "profile_source_revision"))
    if not unchanged:
        raise CapacityRefusal("CAPACITY_RESERVATION_CHANGED", obs)
    return {"cut_ref": obs["cut_ref"], "cut_revision": obs["cut_revision"],
            "observer_ref": obs["observer_ref"], "worker_ref": worker["actor_ref"],
            "evidence_ref": worker["evidence_ref"],
            "assertion": "CALLER_OBSERVED_NOT_NATIVE_LOCKED",
            **({"owner_ports": port_result} if port_result is not None else {})}


def _owner_record(raw: bytes, bound: dict, stage: str) -> tuple[dict, bytes]:
    """Validate one exact owner-supplied record before interpreting its observation."""
    value = decode(raw)
    _fields(value, OWNER_RECORD_FIELDS, "CAPACITY_OWNER_RECORD_FIELDS_INVALID")
    if raw != canonical(value):
        raise ValueError("CAPACITY_OWNER_RECORD_NOT_CANONICAL")
    expected_schema = OWNER_RECORD_SCHEMA_V2 if bound["schema"] == SCHEMA_V2 else OWNER_RECORD_SCHEMA
    if value["schema"] != expected_schema:
        raise ValueError("CAPACITY_OWNER_RECORD_SCHEMA_INVALID")
    for name in ("record_ref", "owner_ref", "task_ref", "task_revision", "source_ref",
                 "source_revision", "observed_at"):
        _text(value[name], "CAPACITY_OWNER_" + name.upper() + "_INVALID")
    if value["stage"] != stage:
        raise ValueError("CAPACITY_OWNER_STAGE_MISMATCH")
    task = bound["task"]
    if (value["task_ref"], value["task_revision"]) != (task["task_ref"], task["task_revision"]):
        raise ValueError("CAPACITY_TASK_BINDING_MISMATCH")
    if value["currentness"] != "CURRENT":
        raise ValueError("CAPACITY_OWNER_RECORD_NOT_CURRENT")
    if value["authority"] != "NONE" or value["assertion"] != OWNER_RECORD_ASSERTION:
        raise ValueError("CAPACITY_OWNER_ASSERTION_INVALID")
    try:
        observed = datetime.fromisoformat(value["observed_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("CAPACITY_OWNER_TIMESTAMP_INVALID") from exc
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("CAPACITY_OWNER_TIMESTAMP_INVALID")
    observation = value["observation"]
    if not isinstance(observation, dict):
        raise ValueError("CAPACITY_OWNER_OBSERVATION_REQUIRED")
    observation_raw = canonical(observation)
    parsed = _observation(observation_raw)
    if (parsed["task_ref"], parsed["task_revision"]) != (task["task_ref"], task["task_revision"]):
        raise ValueError("CAPACITY_TASK_BINDING_MISMATCH")
    provenance = {name: value[name] for name in
                  ("record_ref", "owner_ref", "stage", "source_ref", "source_revision",
                   "currentness", "observed_at", "authority", "assertion")}
    return provenance, observation_raw


def check_uptake(bound: dict, observation_raw: bytes) -> dict:
    """Accept only the exact task's owner-observed reservation at uptake."""
    obs = _observation(observation_raw)
    task = bound["task"]
    if (obs["task_ref"], obs["task_revision"]) != (task["task_ref"], task["task_revision"]):
        raise ValueError("CAPACITY_TASK_BINDING_MISMATCH")
    port_result = current_owner_ports(bound, obs)
    worker = _worker_for(obs, task["actor_ref"], task["generation_ref"])
    if worker is None:
        raise ValueError("CAPACITY_UPTAKE_WORKER_MISSING")
    if worker["state"] not in {"RESERVED", "RUNNING"}:
        raise ValueError("CAPACITY_UPTAKE_RESERVATION_NOT_OBSERVED")
    if worker["reservation_owner_ref"] != task["task_ref"]:
        raise ValueError("CAPACITY_UPTAKE_RESERVATION_OWNER_MISMATCH")
    if obs["schema"] == OBSERVATION_SCHEMA_V2:
        _work_commitment(worker, obs, task, uptake=True)
    if worker["fit"] != "FIT":
        raise ValueError("CAPACITY_UPTAKE_WHOLE_JOB_FIT_NOT_CURRENT")
    selected = bound["selected_worker"]
    if (worker["profile_source_ref"], worker["profile_source_revision"]) != (
            selected["profile_source_ref"], selected["profile_source_revision"]):
        raise ValueError("CAPACITY_UPTAKE_PROFILE_BINDING_CHANGED")
    return {"schema": SCHEMA + "-uptake/v1", "task_ref": task["task_ref"],
            "task_revision": task["task_revision"], "actor_ref": worker["actor_ref"],
            "generation_ref": worker["generation_ref"], "state": worker["state"],
            "reservation_ref": worker["reservation_ref"],
            "reservation_owner_ref": worker["reservation_owner_ref"],
            "evidence_ref": worker["evidence_ref"], "fit_evidence_ref": worker["fit_evidence_ref"],
            "authority": "NONE", "assertion": "OWNER_OBSERVED_NOT_NATIVE_LOCKED",
            **({"owner_ports": port_result} if port_result is not None else {})}


class OwnerCapacityObservationReader:
    """Read the two named owner records used by dispatch and task uptake."""

    def __init__(self, *, dispatch_record: Path, uptake_record: Path,
                 material_return_record: Path | None = None):
        for path in (dispatch_record, uptake_record):
            if not isinstance(path, Path) or not path.is_absolute():
                raise ValueError("CAPACITY_OWNER_RECORD_ABSOLUTE_PATH_REQUIRED")
        if material_return_record is not None and (
                not isinstance(material_return_record, Path) or not material_return_record.is_absolute()):
            raise ValueError("CAPACITY_OWNER_RECORD_ABSOLUTE_PATH_REQUIRED")
        self._paths = {"DISPATCH": dispatch_record, "UPTAKE": uptake_record,
                       "MATERIAL_RETURN": material_return_record}

    def _read(self, bound: dict, stage: str) -> tuple[dict, bytes]:
        path = self._paths[stage]
        if path is None or path.is_symlink() or not path.is_file():
            raise ValueError("CAPACITY_OWNER_RECORD_UNAVAILABLE")
        provenance, observation_raw = _owner_record(path.read_bytes(), bound, stage)
        if stage == "DISPATCH":
            check_current(bound, observation_raw)
        elif stage == "UPTAKE":
            check_uptake(bound, observation_raw)
        else:
            current_owner_ports(bound, _observation(observation_raw), enforce_effect=False)
        provenance["record_path"] = str(path)
        return provenance, observation_raw

    def read_dispatch(self, bound: dict) -> dict:
        provenance, observation_raw = self._read(bound, "DISPATCH")
        return {"provenance": provenance, "observation_raw": observation_raw}

    def read_uptake(self, bound: dict) -> dict:
        provenance, observation_raw = self._read(bound, "UPTAKE")
        return {"provenance": provenance, "observation_raw": observation_raw,
                "uptake": check_uptake(bound, observation_raw)}

    def read_material_return(self, bound: dict) -> dict:
        provenance, observation_raw = self._read(bound, "MATERIAL_RETURN")
        return {"provenance": provenance, "observation_raw": observation_raw}


def consume_prior_care(profile_binding: dict, prior: object | None) -> dict:
    """Record that an ordinary task profile uses a read-back care revision."""
    if prior is None:
        return {"state": "NONE", "authority": "NONE"}
    _fields(prior, {"care_ref", "delta_ref", "readback_ref", "source_ref",
                    "source_revision", "source_sha256"}, "CARE_READBACK_FIELDS_INVALID")
    for name in ("care_ref", "delta_ref", "readback_ref", "source_ref",
                 "source_revision", "source_sha256"):
        _text(prior[name], "CARE_READBACK_VALUE_INVALID")
    profile = profile_binding["profile"]
    if (profile["source_ref"] != prior["source_ref"] or
            profile["source_revision"] != prior["source_revision"]):
        raise ValueError("CARE_UPDATED_PROFILE_NOT_CONSUMED")
    return {"state": "CONSUMED_BY_ORDINARY_PROFILE_BINDING",
            "care_ref": prior["care_ref"], "delta_ref": prior["delta_ref"],
            "readback_ref": prior["readback_ref"], "source_ref": prior["source_ref"],
            "source_revision": prior["source_revision"],
            "source_sha256": prior["source_sha256"], "authority": "NONE"}


def evaluate_return_care(bound: dict, profile_receipt: dict | None,
                          care_raw: bytes | None) -> dict:
    """Classify earned care honestly; never creates/sends Gartner work."""
    if profile_receipt is None:
        if care_raw is not None:
            raise ValueError("CARE_INPUT_WITHOUT_PROFILE_RETURN")
        return {"schema": SCHEMA + "-return", "state": "UNKNOWN",
                "reason": "PROFILE_RETURN_NOT_AVAILABLE", "authority": "NONE"}
    delta = profile_receipt["relevant_delta"]
    if delta["state"] == "NONE_REPORTED":
        if care_raw is not None:
            raise ValueError("CARE_INPUT_WITHOUT_RELEVANT_DELTA")
        return {"schema": SCHEMA + "-return", "state": "NONE",
                "delta_ref": delta["evidence_ref"], "authority": "NONE"}
    if delta["state"] != "REPORTED":
        if care_raw is not None:
            raise ValueError("CARE_INPUT_WITHOUT_OBSERVED_DELTA")
        return {"schema": SCHEMA + "-return", "state": "UNKNOWN",
                "reason": "RELEVANT_DELTA_NOT_OBSERVED", "authority": "NONE"}
    delta_ref = delta["evidence_ref"]
    if care_raw is None:
        return {"schema": SCHEMA + "-return", "state": "UNKNOWN",
                "reason": "CARE_OWNER_AND_NEXT_CONSUMER_NOT_SUPPLIED",
                "delta_ref": delta_ref, "authority": "NONE"}
    care = decode(care_raw)
    _fields(care, CARE_FIELDS, "CARE_OBSERVATION_FIELDS_INVALID")
    if care["schema"] != CARE_SCHEMA:
        raise ValueError("CARE_OBSERVATION_SCHEMA_INVALID")
    task = bound["task"]
    for name, expected in (("task_ref", task["task_ref"]),
                           ("task_revision", task["task_revision"]),
                           ("original_sha256", bound["original_sha256"]),
                           ("profile_binding_sha256", bound["profile_binding_sha256"]),
                           ("delta_ref", delta_ref)):
        if care[name] != expected:
            raise ValueError("CARE_BINDING_MISMATCH:" + name)
    _text(care["care_ref"], "CARE_REF_INVALID")
    state = care["state"]
    if state == "CARE_PENDING":
        _text(care["caretaker_ref"], "CARE_CARETAKER_REQUIRED")
        _text(care["next_consumer_ref"], "CARE_NEXT_CONSUMER_REQUIRED")
        if care["readback"] is not None:
            raise ValueError("CARE_PENDING_READBACK_CONFLICT")
        return {"schema": SCHEMA + "-return", "state": state,
                "care_ref": care["care_ref"], "delta_ref": delta_ref,
                "caretaker_ref": care["caretaker_ref"],
                "next_consumer_ref": care["next_consumer_ref"],
                "assertion": ASSERTION, "authority": "NONE"}
    if state == "REUSED":
        _text(care["caretaker_ref"], "CARE_CARETAKER_REQUIRED")
        _text(care["next_consumer_ref"], "CARE_NEXT_CONSUMER_REQUIRED")
        if care["readback"] is not None:
            raise ValueError("CARE_REUSED_READBACK_CONFLICT")
        return {"schema": SCHEMA + "-return", "state": state,
                "care_ref": care["care_ref"], "delta_ref": delta_ref,
                "caretaker_ref": care["caretaker_ref"],
                "next_consumer_ref": care["next_consumer_ref"],
                "assertion": ASSERTION, "authority": "NONE"}
    if state == "CARE_DONE":
        _text(care["caretaker_ref"], "CARE_CARETAKER_REQUIRED")
        _text(care["next_consumer_ref"], "CARE_NEXT_CONSUMER_REQUIRED")
        readback = care["readback"]
        _fields(readback, READBACK_FIELDS, "CARE_READBACK_FIELDS_INVALID")
        for name in READBACK_FIELDS:
            _text(readback[name], "CARE_READBACK_VALUE_INVALID")
        if not re.fullmatch(r"[0-9a-f]{64}", readback["source_sha256"]):
            raise ValueError("CARE_READBACK_SOURCE_HASH_INVALID")
        if readback["care_ref"] != care["care_ref"] or readback["delta_ref"] != delta_ref:
            raise ValueError("CARE_READBACK_REF_MISMATCH")
        return {"schema": SCHEMA + "-return", "state": state,
                "care_ref": care["care_ref"], "delta_ref": delta_ref,
                "caretaker_ref": care["caretaker_ref"],
                "next_consumer_ref": care["next_consumer_ref"],
                "readback": readback, "assertion": ASSERTION, "authority": "NONE"}
    raise ValueError("CARE_STATE_INVALID")


class OwnerPortRefusal(ValueError):
    def __init__(self, assessment):
        super().__init__("OWNER_PORT_EFFECT_BLOCKED")
        self.code = "OWNER_PORT_EFFECT_BLOCKED"
        self.details = assessment


def _refs(value, reason):
    if not isinstance(value, list) or not value or len(set(value)) != len(value):
        raise ValueError(reason)
    for ref in value:
        _text(ref, reason)


def owner_port_assessment(obs):
    """Task-local observed port disposition; no grants, allocation or effect."""
    if obs["schema"] != OBSERVATION_SCHEMA_V2:
        return None
    if obs["work_phase"] not in {"PREPARATION", "EFFECT"}:
        raise ValueError("OWNER_PORT_WORK_PHASE_INVALID")
    contract = obs["owner_port_contract"]
    _fields(contract, {"case_ref", "governing_basis_refs", "applicable_control_refs",
                       "requirements", "ports", "owner_disposition"}, "OWNER_PORT_CONTRACT_INVALID")
    _text(contract["case_ref"], "OWNER_PORT_EXISTING_CASE_REQUIRED")
    for name in ("governing_basis_refs", "applicable_control_refs"):
        _refs(contract[name], "OWNER_PORT_GOVERNANCE_REFS_REQUIRED")
    requirements, ports, dispositions = contract["requirements"], contract["ports"], contract["owner_disposition"]
    if not isinstance(requirements, list) or not requirements or not isinstance(ports, list) or not isinstance(dispositions, list):
        raise ValueError("OWNER_PORT_REQUIREMENTS_REQUIRED")
    port_map, disposition_map = {}, {}
    for item in ports:
        _fields(item, {"port_ref", "source_ref", "source_revision", "currentness", "state",
                       "evidence_ref", "active_fronts", "prepared_input", "delegation",
                       "release", "consumer_use"}, "OWNER_PORT_RECORD_INVALID")
        for name in ("port_ref", "source_ref", "source_revision"):
            _text(item[name], "OWNER_PORT_SOURCE_REQUIRED")
        if item["port_ref"] in port_map:
            raise ValueError("OWNER_PORT_DUPLICATE")
        if item["currentness"] not in {"CURRENT", "STALE", "UNKNOWN"} or item["state"] not in {"AVAILABLE", "BUSY", "RELEASED", "UNKNOWN"}:
            raise ValueError("OWNER_PORT_STATE_INVALID")
        if item["state"] != "UNKNOWN":
            _text(item["evidence_ref"], "OWNER_PORT_EVIDENCE_REQUIRED")
        elif item["evidence_ref"] is not None:
            raise ValueError("OWNER_PORT_UNKNOWN_EVIDENCE_CONFLICT")
        fronts = item["active_fronts"]
        if not isinstance(fronts, list):
            raise ValueError("OWNER_PORT_FRONTS_INVALID")
        seen = set()
        for front in fronts:
            _fields(front, {"task_ref", "state"}, "OWNER_PORT_FRONT_INVALID")
            _text(front["task_ref"], "OWNER_PORT_FRONT_INVALID")
            if front["state"] not in {"ACTIVE", "CLOSED", "STOPPED"} or front["task_ref"] in seen:
                raise ValueError("OWNER_PORT_FRONT_INVALID")
            seen.add(front["task_ref"])
        port_map[item["port_ref"]] = item
    for item in dispositions:
        _fields(item, {"port_ref", "disposition_ref", "priority_owner_ref", "action", "evidence_ref"},
                "OWNER_PORT_DISPOSITION_INVALID")
        for value in item.values():
            _text(value, "OWNER_PORT_DISPOSITION_INVALID")
        if item["port_ref"] in disposition_map:
            raise ValueError("OWNER_PORT_DISPOSITION_DUPLICATE")
        disposition_map[item["port_ref"]] = item
    decisions, seen = [], set()
    for req in requirements:
        _fields(req, {"port_ref", "function_ref", "effect_scope", "owner_ref", "priority_owner_ref",
                      "preparation_actor_ref", "prepared_input_ref", "next_consumer_ref"}, "OWNER_PORT_REQUIREMENT_INVALID")
        for value in req.values():
            _text(value, "OWNER_PORT_REQUIREMENT_INVALID")
        if req["preparation_actor_ref"] == req["owner_ref"]:
            raise ValueError("OWNER_PORT_DELEGABLE_PREPARATION_ON_UNIQUE_OWNER")
        if obs["work_phase"] == "PREPARATION" and req["preparation_actor_ref"] != obs_worker_ref(obs):
            raise ValueError("OWNER_PORT_PREPARATION_ACTOR_MISMATCH")
        key = req["port_ref"]
        if key in seen:
            raise ValueError("OWNER_PORT_REQUIREMENT_DUPLICATE")
        seen.add(key)
        port = port_map.get(key)
        reason, ready, delegation_ref, performer = "OWNER_PORT_MISSING", False, None, req["owner_ref"]
        active, prepared = [], False
        if port is not None:
            active = sorted(f["task_ref"] for f in port["active_fronts"] if f["state"] == "ACTIVE")
            inp = port["prepared_input"]
            if inp is not None:
                _fields(inp, {"input_ref", "source_revision", "currentness", "evidence_ref"}, "OWNER_PORT_INPUT_INVALID")
                for name in ("input_ref", "source_revision", "currentness", "evidence_ref"):
                    _text(inp[name], "OWNER_PORT_INPUT_INVALID")
                prepared = inp["input_ref"] == req["prepared_input_ref"] and inp["currentness"] == "CURRENT"
            direct = (port["currentness"] == "CURRENT" and port["state"] == "AVAILABLE"
                      and not any(ref != obs["task_ref"] for ref in active)
                      and obs_worker_ref(obs) == req["owner_ref"])
            ready = direct and prepared
            reason = "PORT_READY_FOR_OWNER" if ready else "IRREDUCIBLE_OWNER_ACTION"
            delegation = port["delegation"]
            if delegation is not None:
                _fields(delegation, {"delegation_ref", "owner_ref", "delegate_ref", "task_ref",
                                     "effect_scope", "port_revision", "currentness", "evidence_ref",
                                     "governing_basis_refs", "applicable_control_refs"}, "OWNER_PORT_DELEGATION_INVALID")
                for name in ("delegation_ref", "owner_ref", "delegate_ref", "task_ref", "effect_scope",
                             "port_revision", "currentness", "evidence_ref"):
                    _text(delegation[name], "OWNER_PORT_DELEGATION_INVALID")
                for name in ("governing_basis_refs", "applicable_control_refs"):
                    _refs(delegation[name], "OWNER_PORT_DELEGATION_INVALID")
                valid = (port["currentness"] == "CURRENT" and port["state"] != "UNKNOWN"
                         and delegation["currentness"] == "CURRENT"
                         and delegation["owner_ref"] == req["owner_ref"]
                         and delegation["delegate_ref"] == obs_worker_ref(obs)
                         and delegation["task_ref"] == obs["task_ref"]
                         and delegation["effect_scope"] == req["effect_scope"]
                         and delegation["port_revision"] == port["source_revision"]
                         and all(set(delegation[n]).issubset(contract[n]) for n in
                                 ("governing_basis_refs", "applicable_control_refs")))
                if valid and prepared:
                    ready, reason, delegation_ref, performer = True, "REUSE_CURRENT_SAME_SCOPE_DELEGATION", delegation["delegation_ref"], delegation["delegate_ref"]
                else:
                    ready, reason = False, "STALE_OR_OUT_OF_SCOPE_DELEGATION"
            if port["currentness"] != "CURRENT" or port["state"] == "UNKNOWN":
                ready, reason = False, "OWNER_PORT_NOT_CURRENT"
            if not prepared:
                ready, reason = False, "CURRENT_PREPARED_INPUT_REQUIRED"
        disposition = disposition_map.get(key)
        if disposition is not None and disposition["priority_owner_ref"] != req["priority_owner_ref"]:
            raise ValueError("OWNER_PORT_PRIORITY_OWNER_MISMATCH")
        decisions.append({"port_ref": key, "function_ref": req["function_ref"], "effect_scope": req["effect_scope"],
                          "owner_ref": req["owner_ref"], "effect_performer_ref": performer,
                          "preparation_actor_ref": req["preparation_actor_ref"],
                          "preparation_action": "KEEP_LAWFUL_PREPARATION_WITH_QUALIFIED_ROOM",
                          "next_consumer_ref": req["next_consumer_ref"],
                          "priority_owner_ref": req["priority_owner_ref"],
                          "owner_action": "ONLY_PREPARED_IRREDUCIBLE_ACTION" if prepared else "AWAIT_CURRENT_INPUT_FROM_PREPARATION_ACTOR",
                          "effect_ready": ready, "reason": reason, "delegation_ref": delegation_ref,
                          "active_fronts": active, "multiple_front_bottleneck": len(active) > 1 and not ready,
                          "owner_disposition": disposition,
                          "next_action": "REUSE_QUALIFIED_EFFECT_ROUTE" if ready else "EXISTING_PRIORITY_OWNER_DISPOSE_EXACT_REST"})
    if set(port_map) - seen or set(disposition_map) - seen:
        raise ValueError("OWNER_PORT_OUT_OF_SCOPE_RECORD")
    return {"schema": "cerebro-task-owner-port-disposition/v1", "case_ref": contract["case_ref"],
            "governing_basis_refs": contract["governing_basis_refs"],
            "applicable_control_refs": contract["applicable_control_refs"],
            "work_phase": obs["work_phase"], "effect_ready": all(d["effect_ready"] for d in decisions),
            "relief_observed": False, "preparation_permitted": obs["work_phase"] == "PREPARATION",
            "ports": decisions, "authority": "NONE", "assertion": OWNER_RECORD_ASSERTION}


def obs_worker_ref(obs):
    # The declaration identifies the selected actor explicitly, independent of UI.
    return obs["selected_actor_ref"]


def require_owner_ports(obs):
    assessment = owner_port_assessment(obs)
    if assessment is not None and obs["work_phase"] == "EFFECT" and not assessment["effect_ready"]:
        raise OwnerPortRefusal(assessment)
    return assessment


def current_owner_ports(bound, obs, *, enforce_effect=True):
    old = bound["observation"]
    if old["schema"] != obs["schema"]:
        raise ValueError("OWNER_PORT_CONTRACT_VERSION_CHANGED")
    if old["schema"] != OBSERVATION_SCHEMA_V2:
        return None
    if obs["selected_actor_ref"] != bound["task"]["actor_ref"]:
        raise ValueError("OWNER_PORT_SELECTED_ACTOR_MISMATCH")
    for name in ("case_ref", "requirements", "governing_basis_refs", "applicable_control_refs"):
        if old["owner_port_contract"][name] != obs["owner_port_contract"][name]:
            raise ValueError("OWNER_PORT_DECLARATION_CHANGED")
    if (old["attempt_ref"], old["input_kind"]) != (obs["attempt_ref"], obs["input_kind"]):
        raise ValueError("CAPACITY_ATTEMPT_OR_INPUT_KIND_CHANGED")
    if old["work_phase"] != obs["work_phase"]:
        raise ValueError("OWNER_PORT_WORK_PHASE_CHANGED")
    return require_owner_ports(obs) if enforce_effect else owner_port_assessment(obs)


def owner_port_material_return(bound, readback):
    if bound["schema"] != SCHEMA_V2:
        return None
    if not isinstance(readback, dict) or set(readback) != {"provenance", "observation_raw"}:
        raise ValueError("OWNER_PORT_MATERIAL_RETURN_READ_REQUIRED")
    if readback["provenance"].get("stage") != "MATERIAL_RETURN" or readback["provenance"].get("currentness") != "CURRENT":
        raise ValueError("OWNER_PORT_MATERIAL_RETURN_NOT_CURRENT")
    obs = _observation(readback["observation_raw"])
    if (obs["task_ref"], obs["task_revision"]) != (bound["task"]["task_ref"], bound["task"]["task_revision"]):
        raise ValueError("CAPACITY_TASK_BINDING_MISMATCH")
    assessment = current_owner_ports(bound, obs, enforce_effect=False)
    contract = obs["owner_port_contract"]
    ports = {p["port_ref"]: p for p in contract["ports"]}
    closed = []
    for req in contract["requirements"]:
        port = ports.get(req["port_ref"])
        complete = False
        if port is not None and port["currentness"] == "CURRENT":
            release, use = port["release"], port["consumer_use"]
            if release is not None:
                _fields(release, {"state", "evidence_ref", "owner_ref", "task_ref", "effect_scope", "port_revision"}, "OWNER_PORT_RELEASE_INVALID")
                for v in release.values():
                    _text(v, "OWNER_PORT_RELEASE_INVALID")
            if use is not None:
                _fields(use, {"state", "evidence_ref", "consumer_ref", "release_ref"}, "OWNER_PORT_CONSUMER_USE_INVALID")
                for v in use.values():
                    _text(v, "OWNER_PORT_CONSUMER_USE_INVALID")
            complete = (port["state"] == "RELEASED" and release is not None and use is not None
                        and release["state"] == "RELEASED" and release["owner_ref"] == req["owner_ref"]
                        and release["task_ref"] == obs["task_ref"] and release["effect_scope"] == req["effect_scope"]
                        and release["port_revision"] == port["source_revision"]
                        and use["state"] == "USED" and use["consumer_ref"] == req["next_consumer_ref"]
                        and use["release_ref"] == release["evidence_ref"])
        closed.append({"port_ref": req["port_ref"], "dependency_state": "OWNER_REPORTED_RELEASE_AND_CONSUMER_USE" if complete else "PENDING_OWNER_RELEASE_AND_CONSUMER_USE",
                       "owner_ref": req["owner_ref"], "next_consumer_ref": req["next_consumer_ref"]})
    worker = _worker_for(obs, bound["task"]["actor_ref"], bound["task"]["generation_ref"])
    work_disposition = {"material_return_permitted": True, "authority": "NONE"}
    if worker is None:
        work_disposition.update(state="AFFECTED_UPTAKE_HELD", reason="CAPACITY_UPTAKE_WORKER_MISSING")
    else:
        work_disposition.update(current_work=worker["current_work"], pending_bindings=worker["pending_bindings"],
                                pending_currentness=worker["pending_currentness"])
        try:
            _work_commitment(worker, obs, bound["task"])
            work_disposition.update(state="OBSERVED_NO_COMPETING_BINDING", reason=None)
        except CapacityRefusal as exc:
            work_disposition.update(state="AFFECTED_UPTAKE_HELD", reason=exc.code)
    return {"assessment": assessment, "work_disposition": work_disposition,
            "dependencies": closed, "owner_record": readback["provenance"],
            "relief_observed": all(c["dependency_state"] == "OWNER_REPORTED_RELEASE_AND_CONSUMER_USE" for c in closed),
            "authority": "NONE", "actual_effect_attested": False,
            "assertion": "OWNER_REPORTED_NOT_NATIVE_EFFECT_ATTESTATION"}


def _work_record(worker):
    record = worker["current_work"]
    _fields(record, {"task_ref", "task_revision", "attempt_ref", "state", "currentness", "evidence_ref"}, "CAPACITY_CURRENT_WORK_INVALID")
    if record["state"] not in {"NONE", "ACTIVE", "COMPLETED", "UNKNOWN"} or record["currentness"] not in {"CURRENT", "STALE", "UNKNOWN"}:
        raise ValueError("CAPACITY_CURRENT_WORK_INVALID")
    if record["state"] in {"ACTIVE", "COMPLETED"}:
        for k in ("task_ref", "task_revision", "attempt_ref"):
            _text(record[k], "CAPACITY_CURRENT_WORK_BINDING_REQUIRED")
    elif any(record[k] is not None for k in ("task_ref", "task_revision", "attempt_ref")):
        raise ValueError("CAPACITY_CURRENT_WORK_CONFLICT")
    if record["currentness"] == "CURRENT" and record["state"] != "UNKNOWN":
        _text(record["evidence_ref"], "CAPACITY_CURRENT_WORK_EVIDENCE_REQUIRED")
    if worker["pending_currentness"] not in {"CURRENT", "STALE", "UNKNOWN"}:
        raise ValueError("CAPACITY_PENDING_CURRENTNESS_INVALID")
    if worker["pending_currentness"] == "CURRENT":
        _text(worker["pending_evidence_ref"], "CAPACITY_PENDING_EVIDENCE_REQUIRED")
    if not isinstance(worker["pending_bindings"], list):
        raise ValueError("CAPACITY_PENDING_BINDINGS_INVALID")
    seen = set()
    for pending in worker["pending_bindings"]:
        _fields(pending, {"task_ref", "task_revision", "attempt_ref", "state", "currentness", "evidence_ref"}, "CAPACITY_PENDING_BINDING_INVALID")
        for k in ("task_ref", "task_revision", "attempt_ref", "evidence_ref"):
            _text(pending[k], "CAPACITY_PENDING_BINDING_INVALID")
        if pending["state"] not in {"DISPATCH_INTENT", "UPTAKE", "CONSUMED", "CANCELLED"} or pending["currentness"] not in {"CURRENT", "STALE", "UNKNOWN"}:
            raise ValueError("CAPACITY_PENDING_BINDING_INVALID")
        key = tuple(pending[k] for k in ("task_ref", "task_revision", "attempt_ref"))
        if key in seen:
            raise ValueError("CAPACITY_PENDING_BINDING_DUPLICATE")
        seen.add(key)


def _work_commitment(worker, obs, task, *, uptake=False):
    record = worker["current_work"]
    if record["currentness"] != "CURRENT" or record["state"] == "UNKNOWN" or worker["pending_currentness"] != "CURRENT":
        raise CapacityRefusal("CAPACITY_CURRENT_OR_PENDING_UNKNOWN", obs)
    key = (task["task_ref"], task["task_revision"], obs["attempt_ref"])
    if record["state"] == "COMPLETED" and tuple(record[k] for k in ("task_ref", "task_revision", "attempt_ref")) == key:
        raise CapacityRefusal("CAPACITY_CURRENT_ATTEMPT_ALREADY_COMPLETED", obs)
    own_active = record["state"] == "ACTIVE" and tuple(record[k] for k in ("task_ref", "task_revision", "attempt_ref")) == key
    if record["state"] == "ACTIVE" and not own_active:
        raise CapacityRefusal("CAPACITY_CURRENT_OR_PENDING_OCCUPIED", obs)
    continuation = own_active and (obs["input_kind"] in {"MATERIAL_INPUT", "SAME_TASK_CONTINUATION"} or uptake)
    for pending in worker["pending_bindings"]:
        if pending["currentness"] != "CURRENT":
            raise CapacityRefusal("CAPACITY_CURRENT_OR_PENDING_UNKNOWN", obs)
        if pending["state"] in {"CONSUMED", "CANCELLED"} and tuple(pending[k] for k in ("task_ref", "task_revision", "attempt_ref")) == key:
            raise CapacityRefusal("CAPACITY_CURRENT_ATTEMPT_ALREADY_COMPLETED", obs)
        if pending["state"] in {"DISPATCH_INTENT", "UPTAKE"}:
            if tuple(pending[k] for k in ("task_ref", "task_revision", "attempt_ref")) != key:
                raise CapacityRefusal("CAPACITY_CURRENT_OR_PENDING_OCCUPIED", obs)
            if obs["input_kind"] == "TASK_ASSIGNMENT" and not uptake:
                raise CapacityRefusal("CAPACITY_EXISTING_PENDING_REUSE_ONLY", obs)
    if own_active and not continuation:
        raise CapacityRefusal("CAPACITY_EXISTING_ACTIVE_REUSE_ONLY", obs)
    if continuation and (worker["state"] not in {"RUNNING", "RESERVED"} or worker["reservation_owner_ref"] != task["task_ref"]):
        raise CapacityRefusal("CAPACITY_CURRENT_WORK_RESERVATION_CONFLICT", obs)
    return continuation
