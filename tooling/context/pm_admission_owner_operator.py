"""Private custodian entry. No CLI, environment/credential lookup or MCP export.

Only a future exact finite P24 effect disposition and a checked existing owner
connection may call this. The callback checks the real authority/backend before
SQL and again before commit. This file is not an authorization issuer.
"""
from __future__ import annotations
import copy
import hashlib
import json
import time
import sys
from pathlib import Path
from pm_admission_owner import (IDENTITY, PostgresPmAdmissionOwner, need, digest,
                                validate_head, validate_decision, row, payload, lock_current_head)


from pm_bounded_qualification import (GENERATION as BOUNDED_GENERATION,
    QualificationRefused, require_activation_matches)


class PmOwnerCommitUnknown(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def close_owned(connection, cursor, *, committed):
    primary = sys.exception()
    errors = []
    for resource, name in ((cursor, 'cursor'), (connection, 'connection')):
        if resource is not None:
            try:
                resource.close()
            except BaseException:
                errors.append(name)
    if errors:
        reason = 'PM_OWNER_OWN_CLOSURE_UNKNOWN:' + ','.join(errors)
        if primary is not None:
            primary.add_note(reason)
        elif committed:
            raise PmOwnerCommitUnknown(reason + ':NO_RETRY')
        else:
            from pm_admission_owner import PmAdmissionRefused
            raise PmAdmissionRefused(reason)


def apply_owner_plan(*, checked_connection_factory, plan, release, owner_original_bytes,
                     verify_effect_authority, clock=time.time):
    """BOOTSTRAP preserved old head, STAGE exact candidate, or TRANSFER current.

    All modes are explicit distinct effects. STAGE does not grant operative PM;
    TRANSFER is NOT implicitly covered by installation or an admission release.
    No automatic retry, source retrieval, generation birth or shadow mutation.
    """
    need(callable(checked_connection_factory) and callable(verify_effect_authority), "PM_CHECKED_OWNER_PORT_REQUIRED")
    need(type(plan) is dict and set(plan) == {"action", "before", "after", "decision"}, "PM_OWNER_PLAN_FIELDS")
    need(plan["action"] in {"BOOTSTRAP", "STAGE", "TRANSFER"}, "PM_OWNER_PLAN_ACTION")
    fields = {"issuer", "operation", "operation_id", "input_sha256", "entry_sha256",
              "owner_original_sha256", "not_before", "expires_at"}
    need(type(release) is dict and set(release) == fields, "PM_FINITE_RELEASE_FIELDS")
    need(release["issuer"] == "P24" and release["operation"] == "PM_OWNER_" + plan["action"], "PM_EXACT_PRINCIPAL_RELEASE")
    need(type(owner_original_bytes) is bytes and hashlib.sha256(owner_original_bytes).hexdigest() == release["owner_original_sha256"], "PM_OWNER_ORIGINAL_HASH")
    need(release["input_sha256"] == digest(plan)
         and release["entry_sha256"] == hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "PM_EXACT_PLAN_ENTRY")
    need(type(release["not_before"]) in (int, float) and type(release["expires_at"]) in (int, float)
         and 0 < release["expires_at"] - release["not_before"] <= 1200, "PM_FINITE_RELEASE_INTERVAL")
    def authority(connection):
        need(release["not_before"] <= clock() < release["expires_at"], "PM_RELEASE_EXPIRED_OR_EARLY")
        # Owner integrator must verify the actual original's exact action, target,
        # sole-current/closure disposition, operation ID and actual backend. Not a bool flag.
        need(verify_effect_authority(connection=connection, release=copy.deepcopy(release),
            plan=copy.deepcopy(plan), owner_original_bytes=owner_original_bytes) is True, "PM_EFFECT_AUTHORITY_NOT_VERIFIED")
    before = None if plan["before"] is None else validate_head(plan["before"])
    after = None if plan["after"] is None else validate_head(plan["after"])
    decision = None if plan["decision"] is None else validate_decision(plan["decision"])
    scope = after or before
    need(scope is not None, "PM_OWNER_SCOPE_REQUIRED")
    need(before is None or after is None or all(before[k] == after[k] for k in IDENTITY[:2]), "PM_OWNER_NO_SCOPE_CHANGE")
    # Receipt data cannot authorize TRANSFER. Existing exact release/backend,
    # actual staged decision/native attach/head-CAS/readback gates still follow.
    if plan["action"] == "TRANSFER" and decision["generation_ref"] == BOUNDED_GENERATION:
        try:
            require_activation_matches(after.get("bounded_activation"), decision)
        except QualificationRefused as exc:
            from pm_admission_owner import PmAdmissionRefused
            raise PmAdmissionRefused(str(exc)) from exc
    elif after is not None:
        need("bounded_activation" not in after, "PM_BOUNDED_ACTIVATION_ONLY_SELECTED_TRANSFER")
    connection = checked_connection_factory(); cursor = None; committing = False; committed = False
    try:
        authority(connection)
        cursor = connection.cursor()
        cursor.execute("SET LOCAL statement_timeout='5s'")
        cursor.execute("SET LOCAL lock_timeout='3s'")
        for name in IDENTITY[:2]:
            cursor.execute("SELECT set_config(%s,%s,true)", ("cerebro." + name, scope[name]))
        key = (scope["tenant_ref"], scope["workspace_ref"])
        lock_current_head(cursor, scope, shared=False)
        cursor.execute("SELECT head_payload FROM cerebro_pm_current_heads WHERE tenant_ref=%s AND workspace_ref=%s FOR UPDATE", key)
        found = row(cursor)
        actual = payload(found["head_payload"]) if found else None
        need(actual == before, "PM_OWNER_BEFORE_CHANGED_NO_RETRY")
        if plan["action"] == "BOOTSTRAP":
            need(before is None and decision is None and after["revision"] == 1
                 and after["current_decision_ref"] is None, "PM_BOOTSTRAP_PRESERVED_OUTGOING_ONLY")
        else:
            need(before is not None and decision is not None
                 and all(decision[k] == before[k] for k in IDENTITY[:2]), "PM_OWNER_SCOPE_DECISION")
            need(decision["predecessor_owner_ref"] == before["owner_ref"]
                 and decision["expected_current_revision"] == before["revision"], "PM_OWNER_PREDECESSOR_CHANGED")
            if plan["action"] == "STAGE":
                need(after == before and decision["owner_original_sha256"] == release["owner_original_sha256"], "PM_STAGE_NO_CURRENT_CHANGE")
                cursor.execute("SELECT generation_payload FROM cerebro_pre_role_generation_heads WHERE tenant_ref=%s AND workspace_ref=%s AND generation_ref=%s FOR SHARE", key + (decision["generation_ref"],))
                found = row(cursor); birth = payload(found["generation_payload"]) if found else None
                need(birth is not None and birth["lifecycle"] == "READY_UNBOUND"
                     and birth["revision"] == decision["expected_revision"]
                     and birth["fingerprint"] == decision["pre_role_fingerprint"]
                     and birth["source_revision"] == decision["source_revision"]
                     and birth["ready_unbound_receipt"]["receipt_fingerprint"] == decision["ready_unbound_receipt_fingerprint"], "PM_STAGE_EXISTING_EXACT_BIRTH")
                cursor.execute("INSERT INTO cerebro_pm_admission_decisions (tenant_ref,workspace_ref,generation_ref,decision_ref,decision_payload) VALUES (%s,%s,%s,%s,%s::jsonb)",
                               key + (decision["generation_ref"], decision["decision_ref"], canonical(decision)))
            else:
                need(after["revision"] == before["revision"] + 1 and after["owner_ref"] == decision["actor_ref"]
                     and after["current_decision_ref"] == decision["decision_ref"], "PM_TRANSFER_EXACT_HEAD")
                cursor.execute("SELECT decision_payload FROM cerebro_pm_admission_decisions WHERE tenant_ref=%s AND workspace_ref=%s AND generation_ref=%s FOR SHARE", key + (decision["generation_ref"],))
                found = row(cursor)
                need(found is not None and payload(found["decision_payload"]) == decision, "PM_TRANSFER_STAGED_ORIGINAL_REQUIRED")
                cursor.execute("SELECT generation_payload FROM cerebro_pre_role_generation_heads WHERE tenant_ref=%s AND workspace_ref=%s AND generation_ref=%s FOR SHARE", key + (decision["generation_ref"],))
                found = row(cursor); birth = payload(found["generation_payload"]) if found else None
                expected_overlay = {"overlay_ref": decision["overlay_ref"], "actor_ref": decision["actor_ref"],
                    "role": "PROJECT_MANAGER", "actor_generation_ref": decision["actor_generation_ref"],
                    "ready_unbound_receipt_ref": "READY_" + decision["generation_ref"]}
                need(birth is not None and birth["lifecycle"] == "ROLE_ATTACHED"
                     and birth["revision"] == decision["expected_revision"] + 1
                     and birth["role_overlay"] == expected_overlay, "PM_TRANSFER_ACTUAL_ROLE_ATTACH_REQUIRED")
                cursor.execute("SELECT shadow_payload FROM cerebro_actor_generation_shadow_heads WHERE tenant_ref=%s AND workspace_ref=%s AND actor_role='PROJECT_MANAGER' AND generation_ref=%s FOR SHARE", key + (decision["actor_generation_ref"],))
                found = row(cursor); shadow = payload(found["shadow_payload"]) if found else None
                from control_context_registry import validate_actor_generation_shadow
                need(shadow is not None, "PM_TRANSFER_NATIVE_ROLE_ABSENT")
                validate_actor_generation_shadow(shadow)
                need(shadow["actor_ref"] == decision["actor_ref"] and shadow["lifecycle"] in {"READY", "ACTIVE"}
                     and shadow["source_revision"] == decision["source_revision"], "PM_TRANSFER_NATIVE_ROLE_NOT_READY")
                need(after["activation_authorization_ref"] != before["activation_authorization_ref"], "PM_TRANSFER_NEW_EXPLICIT_DISPOSITION")
        if plan["action"] != "STAGE":
            if before is None:
                cursor.execute("INSERT INTO cerebro_pm_current_heads (tenant_ref,workspace_ref,revision,head_payload) VALUES (%s,%s,%s,%s::jsonb)", key + (after["revision"], canonical(after)))
            else:
                cursor.execute("UPDATE cerebro_pm_current_heads SET revision=%s,head_payload=%s::jsonb WHERE tenant_ref=%s AND workspace_ref=%s AND revision=%s", (after["revision"], canonical(after), *key, before["revision"]))
                need(cursor.rowcount == 1, "PM_TRANSFER_CAS_FAILED")
            cursor.execute("INSERT INTO cerebro_pm_current_revisions (tenant_ref,workspace_ref,revision,head_payload) VALUES (%s,%s,%s,%s::jsonb)", key + (after["revision"], canonical(after)))
        authority(connection)
        committing = True
        connection.commit()
        committing = False
        committed = True
    except BaseException as exc:
        try:
            connection.rollback()
        except Exception:
            exc.add_note('PM_OWNER_ROLLBACK_UNKNOWN')
        if committing:
            raise PmOwnerCommitUnknown("PM_OWNER_COMMIT_UNKNOWN_RECONCILE_EXACT_ORIGINAL_NO_RETRY") from exc
        raise
    finally:
        close_owned(connection, cursor, committed=committed)
    # Separate checked connection; all write errors/readback failures are NO_RETRY.
    try:
        separate = checked_connection_factory(); read_cursor = None
        try:
            need(separate is not connection, 'PM_OWNER_FRESH_INDEPENDENT_READBACK_REQUIRED')
            read_cursor = separate.cursor()
            read_cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            authority(separate)
            for name in IDENTITY[:2]:
                read_cursor.execute("SELECT set_config(%s,%s,true)", ("cerebro." + name, scope[name]))
            read_cursor.execute("SELECT head_payload FROM cerebro_pm_current_heads WHERE tenant_ref=%s AND workspace_ref=%s", key)
            found = row(read_cursor)
            need(found is not None and payload(found["head_payload"]) == after, "PM_OWNER_READBACK_HEAD_CHANGED")
            if decision is not None:
                read_cursor.execute("SELECT decision_payload FROM cerebro_pm_admission_decisions WHERE tenant_ref=%s AND workspace_ref=%s AND generation_ref=%s", key + (decision["generation_ref"],))
                found = row(read_cursor)
                need(found is not None and payload(found["decision_payload"]) == decision, "PM_OWNER_READBACK_DECISION_CHANGED")
            separate.rollback()
        finally:
            close_owned(separate, read_cursor, committed=True)
    except Exception as exc:
        raise PmOwnerCommitUnknown("PM_OWNER_COMMITTED_READBACK_UNKNOWN_NO_RETRY") from exc
    return {"result": "COMMITTED_INDEPENDENT_READBACK_PASS", "action": plan["action"],
            "head_sha256": digest(after), "decision_sha256": digest(decision), "retry_allowed": False}
