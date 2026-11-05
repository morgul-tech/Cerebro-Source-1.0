"""Own-status read; no supplied binding, project event or authorization receipt.

Resolve the caller's immutable staged decision and take a session snapshot in
one bounded transaction. Close it before the unchanged admission verifier opens
its own transaction and locks the actual session/project/head. Never nest those
transactions: a queued exclusive head writer could otherwise deadlock them.
A final separately locked full own frame must equal the initial frame before
either PASS or NOT_SOLE_CURRENT is returned. Drift is REFUSED, with no retry.
"""
from __future__ import annotations

from pm_admission_owner import (
    IDENTITY, PROFILE, PostgresPmAdmissionOwner, PmAdmissionRefused,
    digest, lock_current_head, payload, row, validate_decision,
)


def tool_definitions():
    return [{
        "name": "read_own_pm_profile_v1", "title": "Read own current PM profile",
        "description": "Read only the authenticated caller's own current PM status. No project event or work authority.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "securitySchemes": [{"type": "oauth2", "scopes": ["project_state:read"]}],
        "annotations": {"readOnlyHint": True, "destructiveHint": False,
                        "idempotentHint": True, "openWorldHint": False},
        "_meta": {"cerebro/requiredScope": "project_state:read"},
    }]


def _receipt(status, reason):
    # Deliberately exclude raw identity/session/project, head, counts, decisions,
    # binding fingerprints, other owners and the verifier's bounded scope data.
    return {"schema": "cerebro-own-pm-profile-read/v1", "status": status,
            "reason": reason, "profile": "PROJECT_MANAGER",
            "verifier_ref": PROFILE, "authority": "NONE"}


def _database_reason(error):
    # The existing port wraps DB errors. Inspect codes, never publish messages.
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        code = getattr(error, "sqlstate", None)
        if code is None:
            code = getattr(getattr(error, "diag", None), "sqlstate", None)
        if code == "55P03":
            return "LOCK_TIMEOUT"
        if code == "57014":
            return "STATEMENT_TIMEOUT"
        if code in {"40P01", "40001"}:
            return "CONCURRENT_STATE_CHANGE"
        if code in {"42501", "28000", "28P01"}:
            return "DATABASE_ACCESS_REFUSED"
        if isinstance(code, str) and code.startswith("08"):
            return "DATABASE_UNAVAILABLE"
        error = error.__cause__
    return "PROFILE_READ_UNAVAILABLE"


def _snapshot(owner, who):
    from control_context_registry import validate_actor_generation_shadow

    with owner.state_port._transaction(
            **{k: who[k] for k in IDENTITY[:3]},
            passthrough_errors=(PmAdmissionRefused,)) as cursor:
        cursor.execute("SET LOCAL statement_timeout='5s'")
        cursor.execute("SET LOCAL lock_timeout='3s'")
        lock_current_head(cursor, who, shared=True)
        # A staged decision is the immutable admission table row. The head
        # has no staged field; never use its outgoing current decision as
        # a shortcut for discovering the caller's candidate.
        cursor.execute(
            "SELECT generation_ref, decision_payload FROM cerebro_pm_admission_decisions "
            "WHERE tenant_ref=%s AND workspace_ref=%s "
            "AND decision_payload->>'principal_ref'=%s "
            "AND decision_payload->>'consumer_ref'=%s "
            "AND decision_payload->>'session_ref'=%s LIMIT 2",
            tuple(who[k] for k in IDENTITY))
        first, second = row(cursor), row(cursor)
        if first is None:
            raise PmAdmissionRefused("OWN_DECISION_UNKNOWN")
        if second is not None:
            raise PmAdmissionRefused("OWN_DECISION_AMBIGUOUS")
        decision = validate_decision(payload(first["decision_payload"]))
        if (any(decision[k] != who[k] for k in IDENTITY)
                or decision["generation_ref"] != first["generation_ref"]):
            raise PmAdmissionRefused("OWN_CALLER_MISMATCH")
        head = owner._head(cursor, who)
        # Retain actual ACTIVE/current-revision project and session checks.
        # These locks close here, before verify reopens/rechecks them.
        session = owner._session(cursor, who)
        if any(session.get(k) != who[k] for k in IDENTITY):
            raise PmAdmissionRefused("OWN_CALLER_MISMATCH")
        cursor.execute(
            "SELECT shadow_payload FROM cerebro_actor_generation_shadow_heads "
            "WHERE tenant_ref=%s AND workspace_ref=%s "
            "AND actor_role='PROJECT_MANAGER' AND generation_ref=%s FOR SHARE",
            (who["tenant_ref"], who["workspace_ref"], decision["actor_generation_ref"]))
        found = row(cursor)
        if found is None:
            raise PmAdmissionRefused("OWN_NATIVE_ROLE_NOT_ATTACHED")
        shadow = payload(found["shadow_payload"])
        validate_actor_generation_shadow(shadow)
        if not (shadow["role"] == "PROJECT_MANAGER"
                and shadow["actor_ref"] == decision["actor_ref"]
                and shadow["generation_ref"] == decision["actor_generation_ref"]
                and shadow["source_revision"] == decision["source_revision"]
                and shadow["lifecycle"] in {"READY", "ACTIVE"}):
            raise PmAdmissionRefused("OWN_NATIVE_ROLE_NOT_READY")
        return {"decision": decision, "head": head, "session": session, "shadow": shadow}


def read_own_profile(tools, args, context):
    from control_context_tools import ControlContextToolAuthorizationError
    from control_context_state_port import StatePortError

    if type(args) is not dict or args:
        return tools._result(_receipt("REFUSED", "EMPTY_ARGUMENTS_REQUIRED"), context)
    try:
        identity = tools._identity(context)
        if "project_state:read" not in identity.state_scopes:
            raise ControlContextToolAuthorizationError("own-profile-read-scope-required")
    except ControlContextToolAuthorizationError:
        return tools._result(_receipt("REFUSED", "AUTHENTICATION_OR_READ_SCOPE_REQUIRED"), context)
    try:
        session_ref = context.session_ref()
    except ControlContextToolAuthorizationError:
        return tools._result(_receipt("REFUSED", "HOST_SESSION_MISSING_OR_INVALID"), context)
    who = {k: getattr(identity, k) for k in IDENTITY[:-1]}
    who["session_ref"] = session_ref
    owner = tools._pm_admission_owner
    if type(owner) is not PostgresPmAdmissionOwner or owner.state_port is not tools._state_port:
        return tools._result(_receipt("REFUSED", "PROFILE_OWNER_NOT_BOUND"), context)
    try:
        initial = _snapshot(owner, who)
        decision, head, session = (initial[k] for k in ("decision", "head", "session"))
        binding = {k: decision[k] for k in ("tenant_ref", "workspace_ref", "generation_ref", "decision_ref")}
        binding["current_revision"] = head["revision"]
        # Every derivation lock/connection is closed before this real call.
        # Its single NOT_SOLE_CURRENT error also covers stale revision input:
        # never classify it until a separate final locked own frame matches.
        noncurrent = False
        try:
            verified = owner.verify(binding=binding, session=session)
        except PmAdmissionRefused as error:
            if str(error) != "PM_PROFILE_NOT_SOLE_CURRENT":
                raise
            noncurrent = True
            verified = None
        final = _snapshot(owner, who)
        if final != initial:
            raise PmAdmissionRefused("OWN_PROFILE_FRAME_CHANGED")
        if noncurrent:
            result = _receipt("NOT_SOLE_CURRENT", "PM_PROFILE_NOT_SOLE_CURRENT")
        else:
            expected = digest({"head": initial["head"], "decision": decision,
                               "session": session, "shadow": initial["shadow"]["fingerprint"]})
            if not (type(verified) is dict and verified.get("result") == "PASS"
                    and verified.get("profile") == "PROJECT_MANAGER"
                    and verified.get("binding_fingerprint") == expected):
                raise PmAdmissionRefused("OWN_VERIFIER_FRAME_MISMATCH")
            result = _receipt("PASS", "SOLE_CURRENT_PM")
    except PmAdmissionRefused as error:
        reason = str(error)
        reasons = {
            "OWN_PROFILE_FRAME_CHANGED": "OWN_PROFILE_FRAME_CHANGED",
            "OWN_VERIFIER_FRAME_MISMATCH": "OWN_PROFILE_FRAME_CHANGED",
            "OWN_DECISION_UNKNOWN": "OWN_DECISION_UNKNOWN",
            "OWN_DECISION_AMBIGUOUS": "OWN_DECISION_AMBIGUOUS",
            "OWN_CALLER_MISMATCH": "OWN_CALLER_MISMATCH",
            "PM_OWN_CALLER_SESSION_REQUIRED": "OWN_CALLER_MISMATCH",
            "PM_CURRENT_SESSION_ABSENT": "CURRENT_SESSION_ABSENT",
            "PM_STALE_SESSION_PROJECT": "SESSION_PROJECT_NOT_ACTIVE_OR_WRONG_REVISION",
            "PM_PROFILE_SESSION_NOT_CURRENT": "SESSION_CHANGED",
            "PM_CURRENT_HEAD_ABSENT": "CURRENT_HEAD_UNAVAILABLE",
            "OWN_NATIVE_ROLE_NOT_ATTACHED": "OWN_NATIVE_ROLE_NOT_ATTACHED",
            "OWN_NATIVE_ROLE_NOT_READY": "OWN_NATIVE_ROLE_NOT_READY",
            "PM_PROFILE_ROLE_NOT_ATTACHED": "OWN_NATIVE_ROLE_NOT_ATTACHED",
            "PM_PROFILE_NATIVE_ROLE_NOT_READY": "OWN_NATIVE_ROLE_NOT_READY",
        }
        result = _receipt("REFUSED", reasons.get(reason, "PROFILE_DATA_OR_BINDING_INVALID"))
    except StatePortError as error:
        reasons = {"control-session-not-bound": "CURRENT_SESSION_ABSENT",
                   "project-instance-not-found": "SESSION_PROJECT_UNAVAILABLE"}
        result = _receipt("REFUSED", reasons.get(str(error), _database_reason(error)))
    except Exception as error:
        # Registry validation or unexpected driver failures cannot become a
        # profile denial or leak exception text. No automatic read retry.
        result = _receipt("REFUSED", _database_reason(error))
    return tools._result(result, context)
