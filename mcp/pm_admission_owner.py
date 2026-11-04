"""PM-specific admission and current-profile readers; no owner write RPC.

Owner rows are published separately by the installation custodian. Admission
holds the sole-current head before the existing atomic role attach. Attaching
the candidate does not move that head. No Assistant/Principal resolver is used.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar

from pm_bounded_qualification import (GENERATION as BOUNDED_GENERATION,
    QualificationRefused, validate_qualification, validate_activation,
    require_activation_matches, require_issued_contract, digest as bounded_digest)

PROFILE = "PM_OWNER_ADMISSION_V1"
IDENTITY = ("tenant_ref", "workspace_ref", "principal_ref", "consumer_ref", "session_ref")
SHA = re.compile(r"[0-9a-f]{64}\Z")
REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@:-]{0,255}\Z")
DECISION_FIELDS = frozenset((*IDENTITY, "schema", "decision_ref", "owner_authorization_ref",
    "owner_original_sha256", "candidate_chat", "generation_ref", "expected_revision",
    "pre_role_fingerprint", "ready_unbound_receipt_fingerprint", "source_revision",
    "method_fingerprint", "overlay_ref", "actor_ref", "actor_generation_ref", "profile_ref",
    "predecessor_owner_ref", "expected_current_revision", "not_before", "expires_at"))
HEAD_FIELDS = frozenset(("schema", "tenant_ref", "workspace_ref", "revision", "owner_ref",
    "current_decision_ref", "activation_authorization_ref"))


class PmAdmissionRefused(RuntimeError):
    pass


class PmAdmissionCommitUnknown(RuntimeError):
    """Native commit was attempted; reconcile the same attempt, never retry."""
    pass


def need(condition, reason):
    if not condition:
        raise PmAdmissionRefused(reason)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


def reduced_bounded_receipt(decision):
    """Same non-authorizing public markers at resume and operative profile PASS."""
    if decision["generation_ref"] != BOUNDED_GENERATION:
        return {}
    qualification = decision["bounded_qualification"]
    return {"bounded_scope_receipt": {
        "contract_ref": qualification["contract_ref"],
        "qualification_sha256": bounded_digest(qualification),
        "history": copy.deepcopy(qualification["history"]),
        "scope": copy.deepcopy(qualification["scope"]),
        "authority": "NONE", "strict_pass_granted": False}}


def lock_current_head(cursor, who, *, shared):
    """Same workspace lock on readers AND the only privileged owner writer.

    Advisory locking needs no UPDATE grant on owner tables. Immutable decision
    rows need no row lock. The runtime receives SELECT only on these tables.
    """
    key = json.dumps(["PM_CURRENT", who['tenant_ref'], who['workspace_ref']], separators=(',', ':'))
    function = 'pg_advisory_xact_lock_shared' if shared else 'pg_advisory_xact_lock'
    cursor.execute('SELECT ' + function + '(hashtextextended(%s,0))', (key,))


def validate_decision(value):
    need(type(value) is dict, "PM_DECISION_EXACT_FIELDS")
    bounded = value.get("generation_ref") == BOUNDED_GENERATION
    fields = DECISION_FIELDS | ({"bounded_qualification"} if bounded else set())
    need(set(value) == fields, "PM_DECISION_EXACT_FIELDS")
    need(value["schema"] == "cerebro-pm-admission-decision/v1", "PM_DECISION_SCHEMA")
    hashes = {"owner_original_sha256", "pre_role_fingerprint", "ready_unbound_receipt_fingerprint", "method_fingerprint"}
    integers = {"expected_revision", "expected_current_revision"}
    for field in DECISION_FIELDS - hashes - integers - {"schema", "not_before", "expires_at", "source_revision", "candidate_chat"}:
        need(type(value[field]) is str and REF.fullmatch(value[field]), "PM_DECISION_REF:" + field)
    for field in hashes:
        need(type(value[field]) is str and SHA.fullmatch(value[field]), "PM_DECISION_HASH:" + field)
    for field in integers:
        need(type(value[field]) is int and value[field] > 0, "PM_DECISION_REVISION:" + field)
    need(value["expected_revision"] == 2, "PM_EXISTING_NATIVE_BIRTH_REVISION")
    need(type(value["source_revision"]) is str and re.fullmatch(r"[0-9a-f]{40}", value["source_revision"]), "PM_SOURCE")
    need(type(value["candidate_chat"]) is str and re.fullmatch(r"[0-9a-f-]{36}", value["candidate_chat"]), "PM_CANDIDATE_CHAT")
    need(re.fullmatch(r"CEREBRO-BOOT-[A-Z0-9-]{8,96}", value["generation_ref"]), "PM_NATIVE_GENERATION")
    for field in ("not_before", "expires_at"):
        need(type(value[field]) in (int, float) and math.isfinite(value[field]), "PM_FINITE_TIME")
    need(0 < value["not_before"] < value["expires_at"], "PM_FINITE_INTERVAL")
    need(value["actor_ref"] != value["predecessor_owner_ref"], "PM_DISTINCT_SUCCESSOR")
    if bounded:
        try:
            validate_qualification(value["bounded_qualification"], value)
            require_issued_contract(value["bounded_qualification"])
        except QualificationRefused as exc:
            raise PmAdmissionRefused(str(exc)) from exc
    return copy.deepcopy(value)


def validate_head(value):
    need(type(value) is dict and (set(value) == HEAD_FIELDS
         or set(value) == HEAD_FIELDS | {"bounded_activation"}), "PM_HEAD_EXACT_FIELDS")
    if "bounded_activation" in value:
        try:
            validate_activation(value["bounded_activation"])
        except QualificationRefused as exc:
            raise PmAdmissionRefused(str(exc)) from exc
    need(value["schema"] == "cerebro-pm-current-head/v1", "PM_HEAD_SCHEMA")
    need(type(value["revision"]) is int and value["revision"] > 0, "PM_HEAD_REVISION")
    for field in HEAD_FIELDS - {"schema", "revision", "current_decision_ref"}:
        need(type(value[field]) is str and REF.fullmatch(value[field]), "PM_HEAD_REF:" + field)
    ref = value["current_decision_ref"]
    need(ref is None or (type(ref) is str and REF.fullmatch(ref)), "PM_HEAD_DECISION")
    return copy.deepcopy(value)


def row(cursor):
    value = cursor.fetchone()
    if value is None or isinstance(value, dict):
        return value
    return dict(zip((d.name if hasattr(d, "name") else d[0] for d in cursor.description), value))


def payload(value):
    return json.loads(value) if isinstance(value, str) else copy.deepcopy(value)


class PostgresPmAdmissionOwner:
    def __init__(self, state_port, *, clock=time.time):
        need(callable(getattr(state_port, "_transaction", None)), "PM_STATE_PORT_REQUIRED")
        need(callable(clock), "PM_CLOCK_REQUIRED")
        self.state_port = state_port
        self.clock = clock
        self._active = ContextVar("pm_admission_held_guard", default=None)

    def _head(self, cursor, who):
        cursor.execute("SELECT head_payload FROM cerebro_pm_current_heads WHERE tenant_ref=%s AND workspace_ref=%s",
                       tuple(who[k] for k in IDENTITY[:2]))
        found = row(cursor)
        need(found is not None, "PM_CURRENT_HEAD_ABSENT")
        result = validate_head(payload(found["head_payload"]))
        need(all(result[k] == who[k] for k in IDENTITY[:2]), "PM_HEAD_SCOPE")
        return result

    def _decision(self, cursor, who, generation_ref):
        cursor.execute("SELECT decision_payload FROM cerebro_pm_admission_decisions WHERE tenant_ref=%s AND workspace_ref=%s AND generation_ref=%s",
                       tuple(who[k] for k in IDENTITY[:2]) + (generation_ref,))
        found = row(cursor)
        need(found is not None, "PM_OWNER_DECISION_ABSENT")
        decision = validate_decision(payload(found["decision_payload"]))
        need(all(decision[k] == who[k] for k in IDENTITY), "PM_OWN_CALLER_SESSION_REQUIRED")
        need(decision["generation_ref"] == generation_ref, "PM_OWNER_GENERATION_MISMATCH")
        return decision

    def _session(self, cursor, who):
        session = self.state_port._load_session(cursor, **who, for_update=True)
        need(session is not None, "PM_CURRENT_SESSION_ABSENT")
        project = self.state_port._load_project(cursor, **{k: who[k] for k in IDENTITY[:2]},
                                              project_ref=session["project_ref"], for_update=True)
        need(project is not None and project["project_status"] == "ACTIVE"
             and session["project_revision"] == project["revision"], "PM_STALE_SESSION_PROJECT")
        return session

    @contextmanager
    def hold_admission(self, *, identity, session_ref, birth):
        identity.validate()
        need("project_state:transition" in identity.state_scopes, "PM_TRANSITION_SCOPE")
        need(self._active.get() is None, "PM_NESTED_ADMISSION_REFUSED")
        who = {k: getattr(identity, k) for k in IDENTITY[:-1]}
        who["session_ref"] = session_ref
        need(all(birth.get(k) == who[k] for k in IDENTITY[:2]), "PM_BIRTH_SCOPE")
        with self.state_port._transaction(**{k: who[k] for k in IDENTITY[:3]},
                passthrough_errors=(PmAdmissionRefused,)) as cursor:
            cursor.execute("SET LOCAL statement_timeout='5s'")
            cursor.execute("SET LOCAL lock_timeout='3s'")
            lock_current_head(cursor, who, shared=True)
            head = self._head(cursor, who)
            decision = self._decision(cursor, who, birth["generation_ref"])
            session = self._session(cursor, who)
            need(head["owner_ref"] == decision["predecessor_owner_ref"]
                 and head["revision"] == decision["expected_current_revision"], "PM_PREDECESSOR_CURRENT_CHANGED")
            need(birth.get("lifecycle") == "READY_UNBOUND" and birth.get("revision") == decision["expected_revision"]
                 and birth.get("fingerprint") == decision["pre_role_fingerprint"]
                 and birth.get("source_revision") == decision["source_revision"]
                 and birth.get("ready_unbound_receipt", {}).get("receipt_fingerprint") == decision["ready_unbound_receipt_fingerprint"]
                 and birth.get("civilization_method_attestation", {}).get("method_fingerprint") == decision["method_fingerprint"],
                 "PM_EXACT_OWNER_BIRTH_REQUIRED")
            def guard():
                now = self.clock()
                need(decision["not_before"] <= now < decision["expires_at"], "PM_OWNER_DECISION_EXPIRED_OR_EARLY")
                need(self._head(cursor, who) == head and self._decision(cursor, who, birth["generation_ref"]) == decision,
                     "PM_OWNER_CHANGED")
                need(self._session(cursor, who) == session, "PM_SESSION_CHANGED")
            guard()
            token = self._active.set((copy.deepcopy(birth), copy.deepcopy(decision), guard))
            try:
                yield decision, guard
            finally:
                self._active.reset(token)

    def attach_guard(self, birth, overlay_payload):
        active = self._active.get()
        need(active is not None and active[0] == birth, "PM_OWNER_HELD_ATTACH_REQUIRED")
        expected = {k: active[1][k] for k in ("generation_ref", "expected_revision", "overlay_ref",
                    "actor_ref", "actor_generation_ref", "source_revision")}
        expected["role"] = "PROJECT_MANAGER"
        need(overlay_payload == expected, "PM_OWNER_EXACT_OVERLAY_REQUIRED")
        active[2]()
        return active[2]

    def verify(self, *, binding, session):
        """Current PM profile check only; never creates role/currentness/claims."""
        fields = {"tenant_ref", "workspace_ref", "generation_ref", "decision_ref", "current_revision"}
        need(type(binding) is dict and set(binding) == fields, "PM_PROFILE_EXACT_BINDING")
        need(type(session) is dict and all(type(session.get(k)) is str for k in IDENTITY), "PM_PROFILE_SESSION_REQUIRED")
        who = {k: session[k] for k in IDENTITY}
        need(all(binding[k] == who[k] for k in IDENTITY[:2]), "PM_PROFILE_SCOPE")
        with self.state_port._transaction(**{k: who[k] for k in IDENTITY[:3]},
                passthrough_errors=(PmAdmissionRefused,)) as cursor:
            cursor.execute("SET LOCAL statement_timeout='5s'")
            cursor.execute("SET LOCAL lock_timeout='3s'")
            lock_current_head(cursor, who, shared=True)
            head = self._head(cursor, who)
            decision = self._decision(cursor, who, binding["generation_ref"])
            actual_session = self._session(cursor, who)
            need(actual_session == session, "PM_PROFILE_SESSION_NOT_CURRENT")
            need(type(binding["current_revision"]) is int and head["revision"] == binding["current_revision"]
                 and head["owner_ref"] == decision["actor_ref"]
                 and head["current_decision_ref"] == decision["decision_ref"] == binding["decision_ref"],
                 "PM_PROFILE_NOT_SOLE_CURRENT")
            if decision["generation_ref"] == BOUNDED_GENERATION:
                try:
                    require_activation_matches(head.get("bounded_activation"), decision)
                except QualificationRefused as exc:
                    raise PmAdmissionRefused(str(exc)) from exc
            else:
                need("bounded_activation" not in head, "PM_OTHER_CANDIDATE_NO_BOUNDED_ACTIVATION")
            cursor.execute("SELECT shadow_payload FROM cerebro_actor_generation_shadow_heads WHERE tenant_ref=%s AND workspace_ref=%s AND actor_role='PROJECT_MANAGER' AND generation_ref=%s FOR SHARE",
                           (who["tenant_ref"], who["workspace_ref"], decision["actor_generation_ref"]))
            found = row(cursor)
            need(found is not None, "PM_PROFILE_ROLE_NOT_ATTACHED")
            from control_context_registry import validate_actor_generation_shadow
            shadow = payload(found["shadow_payload"])
            validate_actor_generation_shadow(shadow)
            need(shadow["role"] == "PROJECT_MANAGER" and shadow["actor_ref"] == decision["actor_ref"]
                 and shadow["generation_ref"] == decision["actor_generation_ref"]
                 and shadow["source_revision"] == decision["source_revision"]
                 and shadow["lifecycle"] in {"READY", "ACTIVE"}, "PM_PROFILE_NATIVE_ROLE_NOT_READY")
            return {**reduced_bounded_receipt(decision),
                "schema": "cerebro-project-manager-profile-verification/v1", "result": "PASS",
                "profile": "PROJECT_MANAGER", "session_ref": who["session_ref"],
                "binding_fingerprint": digest({"head": head, "decision": decision, "session": actual_session,
                                               "shadow": shadow["fingerprint"]}),
                "verifier_ref": PROFILE}


def resume_pm(tools, owner, issuer, args, context):
    """One candidate-owned call; no caller policy, role, signature or current flip."""
    need(type(args) is dict and set(args) == {"generation_ref", "expected_revision", "source_revision"}, "PM_RESUME_EXACT_FIELDS")
    need(type(args["expected_revision"]) is int and args["expected_revision"] == 2, "PM_RESUME_REVISION")
    need(type(owner) is PostgresPmAdmissionOwner and callable(getattr(issuer, "seal", None)), "PM_OWNER_ISSUER_NOT_BOUND")
    identity = tools._identity(context)
    birth = tools._read_pre_role(identity, context, args["generation_ref"])
    need(birth["revision"] == args["expected_revision"] and birth["source_revision"] == args["source_revision"], "PM_RESUME_BIRTH_CHANGED")
    source = tools._boot_birth_source_continuity_verifier_for_pm(
        args["source_revision"], birth["civilization_method_attestation"]["method_fingerprint"])
    from pm_native_birth import verify_native_pm_birth
    verify_native_pm_birth(birth, generation_ref=args["generation_ref"], source_revision=args["source_revision"], verified_source=source)
    native_outcome = None
    try:
        with owner.hold_admission(identity=identity, session_ref=context.session_ref(), birth=birth) as (decision, guard):
            payload = {k: decision[k] for k in ("generation_ref", "expected_revision", "overlay_ref", "actor_ref", "actor_generation_ref", "source_revision")}
            payload["role"] = "PROJECT_MANAGER"
            guard()
            attestation = issuer.seal(operation="attach_role_overlay", payload=payload, context=context)
            guard()
            # Existing native atomic attach owns commit/readback and UNKNOWN semantics.
            result = tools.attach_role_overlay({**payload, "control_resolution_attestation": attestation}, context)
            native_outcome = copy.deepcopy(result["structuredContent"])
        bounded_receipt = reduced_bounded_receipt(decision)
        return tools._result({**native_outcome, **bounded_receipt,
                              "control_decision_ref": decision["decision_ref"],
                              "current_pm_changed": False, "retry_allowed": False}, context)
    except Exception as exc:
        if native_outcome is None:
            raise
        # A separate owner transaction's exit cannot erase already-known native
        # commit/readback or primary UNKNOWN. Reconcile THIS operation only.
        return tools._result({"result": "OWNER_WINDOW_EXIT_UNKNOWN", "native_outcome": native_outcome,
            "owner_window_exit": {"result": "UNKNOWN", "error_type": type(exc).__name__},
            "control_decision_ref": decision["decision_ref"], "current_pm_changed": False,
            "retry_allowed": False, "repository_permission_required": False}, context)


def tool_definitions():
    return [{"name": "resume_ready_unbound_pm_overlay", "title": "Attach owner-selected PM role",
        "description": "Consume own existing birth through PM-specific owner admission; does not transfer CURRENT PM.",
        "inputSchema": {"type": "object", "additionalProperties": False,
            "required": ["generation_ref", "expected_revision", "source_revision"],
            "properties": {"generation_ref": {"type": "string"}, "expected_revision": {"const": 2},
                           "source_revision": {"type": "string", "pattern": "^[0-9a-f]{40}$"}}},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
        "securitySchemes": [{"type": "oauth2", "scopes": ["project_state:transition"]}]}]
