#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

SOURCE_ROOT = Path(__file__).resolve().parents[2]

def load(path: Path, name: str):
    spec=importlib.util.spec_from_file_location(name,path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"module-load-failed:{path}")
    mod=importlib.util.module_from_spec(spec)
    sys.modules[name]=mod
    spec.loader.exec_module(mod)
    return mod

def integration_test(root: Path) -> dict[str, Any]:
    control_path=root/"mcp/control_resolution.py"
    if not control_path.is_file():
        return {"result":"NOT_PRESENT"}
    control=load(control_path,"cerebro_pm_governor_control_resolution_integration")
    binding=control._fixture_control_context_binding(root)
    intent=control._fixture_intent_assessment(root,binding)

    class FixturePMProfileVerifier:
        def verify(self, *, binding: dict[str,Any], session: dict[str,Any]) -> dict[str,Any]:
            if binding.get("binding_id")!="PM-PROFILE-BINDING-SELFTEST":
                return {"schema":"cerebro-project-manager-profile-verification/v1","result":"BLOCKED"}
            return {
                "schema":"cerebro-project-manager-profile-verification/v1",
                "result":"PASS",
                "profile":"PROJECT_MANAGER",
                "session_ref":session["session_ref"],
                "binding_fingerprint":"b"*64,
                "verifier_ref":"SELFTEST-CONSTRUCTOR-BOUND",
            }

    request={
        "objective_ref":"PM-CONTROL-GOVERNOR-INTEGRATION",
        "project_bound":True,
        "control_context_binding":binding,
        "control_context_intent_assessment":intent,
        "context_transition_candidate":{"project_operations":[],"session_operations":[]},
        "project_manager_profile_binding":{"binding_id":"PM-PROFILE-BINDING-SELFTEST"},
        "project_manager_governance_candidate":{
            "schema":"cerebro-project-manager-control-governor/v1",
            "event_ref":binding["event_id"],
            "frontier_actions":[{
                "action_ref":"PM-REFRESH-SHARED",
                "actor":"PROJECT_MANAGER",
                "state":"PENDING",
                "internally_executable":True,
                "human_action_required":False,
            }],
            "long_running_execution":True,
            "progress_observability":{
                "heartbeat_enabled":True,
                "phase_updates_enabled":True,
                "silent_stall_surface_prohibited":True,
            },
        },
        "consequence":"LOW",
        "uncertainty":"LOW",
    }
    good=control.resolve(
        request,root,require_git_ancestry=False,pm_profile_verifier=FixturePMProfileVerifier()
    )
    g=good.get("project_manager_control_governance") or {}
    next_action=good.get("mcp_control_decision",{}).get("next_action",{})
    good_ok=(
        good.get("mcp_control_decision",{}).get("outcome")=="CONTINUE"
        and g.get("result")=="PASS"
        and next_action.get("action_ref")=="PM-REFRESH-SHARED"
        and next_action.get("owner")=="MACHINE"
        and good.get("current_event_machine_action_required") is True
        and good.get("event_closure_allowed_before_required_machine_action") is False
        and good.get("human_navigation_surface_required") is False
    )

    bad=control.resolve(request,root,require_git_ancestry=False,pm_profile_verifier=None)
    bad_ok=(
        bad.get("mcp_control_decision",{}).get("outcome")=="BLOCK"
        and "CONTROL_CONTEXT_TRANSITION_INVALID" in bad.get("mcp_control_decision",{}).get("invalidates",[])
    )
    return {
        "result":"PASS" if good_ok and bad_ok else "FAIL",
        "verified_profile_integration":good_ok,
        "missing_verifier_fail_closed":bad_ok,
    }


def lifecycle_canaries(mod: Any) -> dict[str, Any]:
    tests: list[dict[str, Any]] = []

    def check(name: str, fn) -> None:
        try:
            passed = bool(fn())
            tests.append({"name": name, "result": "PASS" if passed else "FAIL"})
        except Exception as exc:
            tests.append({"name": name, "result": "FAIL", "detail": str(exc)})

    def seal(candidate: dict[str, Any]) -> dict[str, Any]:
        candidate.pop("candidate_fingerprint", None)
        candidate["candidate_fingerprint"] = mod._lifecycle_candidate_fingerprint(candidate)
        return candidate

    def candidate(*, evidence: bool = False) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema": "cerebro-actor-lifecycle-mutation/v1",
            "mutation_id": "MUT-REQUALIFY-1",
            "idempotency_key": "IDEMPOTENCY-REQUALIFY-1",
            "operation": "BIND",
            "actor_generation_id": "W-CANARY-1",
            "slot_pointer_ref": "READY_QUEUE:CANARY",
            "expected_slot_pointer": "W-CANARY-1",
            "observed_slot_pointer": "W-CANARY-1",
            "expected_lifecycle_revision": 7,
            "expected_lifecycle_state": "REQUALIFICATION_REQUIRED",
            "expected_claim_revision": "NOT_APPLICABLE",
            "packet_ref": "WORK_PACKETS:534",
            "claim_ref": None,
            "authority_source": "PROJECT_MANAGER+MCP",
            "observed_event_frontier": 3870,
            "source_transition": {
                "intent": "SAME_GENERATION_SOURCE_REQUALIFICATION",
                "previous_source_head": "1" * 40,
                "target_source_head": "2" * 40,
                "changed_contracts": ["mcp/control-resolution"],
                "gates_rerun": ["SOURCE", "CURRENTNESS", "ROLE", "INTEGRITY"],
                "prerequisite_fingerprint": "3" * 64,
                "boot_fingerprint": "4" * 64,
                "current_source_verified": True,
                "actor_nonretired_verified": True,
                "no_active_claim_verified": True,
                "generation_pointer_verified": True,
            },
        }
        if evidence:
            value["effect_evidence"] = {
                "receipt_id": "CONTEXT-RECEIPT-1",
                "context_commit_result": "COMMITTED",
                "durable": True,
                "post_state_readback_verified": True,
                "post_actor_generation_id": "W-CANARY-1",
                "post_lifecycle_state": "READY_CURRENT",
                "post_source_head": "2" * 40,
                "provider_revision": 8,
                "receipt_fingerprint": "5" * 64,
            }
        return seal(value)

    class ContextEffectVerifier:
        def verify_lifecycle_effect(
            self, *, evidence: dict[str, Any], candidate: dict[str, Any], session: dict[str, Any]
        ) -> dict[str, Any]:
            return {
                "schema": "cerebro-context-lifecycle-effect-verification/v1",
                "result": "PASS",
                "receipt_id": evidence["receipt_id"],
                "post_actor_generation_id": evidence["post_actor_generation_id"],
                "post_lifecycle_state": evidence["post_lifecycle_state"],
                "post_source_head": evidence["post_source_head"],
                "provider_revision": evidence["provider_revision"],
                "receipt_fingerprint": evidence["receipt_fingerprint"],
                "verifier_ref": "SELFTEST-CONSTRUCTOR-BOUND-CONTEXT-EFFECT",
            }

    effect_verifier = ContextEffectVerifier()

    def run(value: dict[str, Any], *, verifier: Any = effect_verifier) -> dict[str, Any]:
        return mod._lifecycle_mutation_gate(
            {"lifecycle_mutation": value},
            lifecycle_effect_verifier=verifier,
            session={"session_ref": "SESSION-LIFECYCLE-CANARY"},
        )

    def expect_block(
        mutator, *, evidence: bool = False, reseal: bool = True, verifier: Any = effect_verifier
    ) -> bool:
        value = candidate(evidence=evidence)
        mutator(value)
        if reseal:
            seal(value)
        try:
            run(value, verifier=verifier)
        except mod.ProjectManagerGovernorError:
            return True
        return False

    # 36 race/currentness/idempotency/effect canaries.
    check("lifecycle-01-candidate-ready-pending-context", lambda: (
        run(candidate())["result"] == "PASS_CANDIDATE_READY_FOR_CONTEXT"
        and run(candidate())["ready_effect_allowed"] is False
    ))
    check("lifecycle-02-effect-verified-after-durable-readback", lambda: (
        run(candidate(evidence=True))["result"] == "PASS_EFFECT_VERIFIED"
        and run(candidate(evidence=True))["ready_effect_allowed"] is True
    ))
    check("lifecycle-03-requalification-keeps-existing-bind-operation", lambda: expect_block(
        lambda c: c.update({"operation": "RETIRE"})
    ))
    check("lifecycle-04-held-state-required", lambda: expect_block(
        lambda c: c.update({"expected_lifecycle_state": "ACTIVE"})
    ))
    check("lifecycle-05-active-claim-rejected", lambda: expect_block(
        lambda c: c.update({"claim_ref": "WORK_CLAIMS:1"})
    ))
    check("lifecycle-06-claim-revision-not-applicable-required", lambda: expect_block(
        lambda c: c.update({"expected_claim_revision": 1})
    ))
    check("lifecycle-07-observed-pointer-required", lambda: expect_block(
        lambda c: c.update({"observed_slot_pointer": ""})
    ))
    check("lifecycle-08-pointer-race-rejected", lambda: expect_block(
        lambda c: c.update({"observed_slot_pointer": "W-OTHER"})
    ))
    check("lifecycle-09-current-source-proof-required", lambda: expect_block(
        lambda c: c["source_transition"].update({"current_source_verified": False})
    ))
    check("lifecycle-10-nonretired-proof-required", lambda: expect_block(
        lambda c: c["source_transition"].update({"actor_nonretired_verified": False})
    ))
    check("lifecycle-11-no-active-claim-proof-required", lambda: expect_block(
        lambda c: c["source_transition"].update({"no_active_claim_verified": False})
    ))
    check("lifecycle-12-generation-pointer-proof-required", lambda: expect_block(
        lambda c: c["source_transition"].update({"generation_pointer_verified": False})
    ))
    check("lifecycle-13-candidate-fingerprint-readback-required", lambda: expect_block(
        lambda c: c.update({"candidate_fingerprint": "f" * 64}), reseal=False
    ))
    check("lifecycle-14-lifecycle-revision-nonnegative", lambda: expect_block(
        lambda c: c.update({"expected_lifecycle_revision": -1})
    ))
    check("lifecycle-15-claim-revision-domain-enforced", lambda: expect_block(
        lambda c: c.update({"expected_claim_revision": "UNKNOWN"})
    ))
    check("lifecycle-16-event-frontier-nonnegative", lambda: expect_block(
        lambda c: c.update({"observed_event_frontier": -1})
    ))
    check("lifecycle-17-mutation-id-required", lambda: expect_block(
        lambda c: c.update({"mutation_id": ""})
    ))
    check("lifecycle-18-idempotency-key-required", lambda: expect_block(
        lambda c: c.update({"idempotency_key": ""})
    ))
    check("lifecycle-19-generation-id-required", lambda: expect_block(
        lambda c: c.update({"actor_generation_id": ""})
    ))
    check("lifecycle-20-authority-source-required", lambda: expect_block(
        lambda c: c.update({"authority_source": ""})
    ))
    check("lifecycle-21-context-receipt-id-required", lambda: expect_block(
        lambda c: c["effect_evidence"].update({"receipt_id": ""}), evidence=True
    ))
    check("lifecycle-22-context-commit-required", lambda: expect_block(
        lambda c: c["effect_evidence"].update({"context_commit_result": "PENDING"}), evidence=True
    ))
    check("lifecycle-23-durable-receipt-required", lambda: expect_block(
        lambda c: c["effect_evidence"].update({"durable": False}), evidence=True
    ))
    check("lifecycle-24-provider-readback-required", lambda: expect_block(
        lambda c: c["effect_evidence"].update({"post_state_readback_verified": False}), evidence=True
    ))
    check("lifecycle-25-post-generation-must-match", lambda: expect_block(
        lambda c: c["effect_evidence"].update({"post_actor_generation_id": "W-OTHER"}), evidence=True
    ))
    check("lifecycle-26-post-state-ready-current-required", lambda: expect_block(
        lambda c: c["effect_evidence"].update({"post_lifecycle_state": "READY"}), evidence=True
    ))
    check("lifecycle-27-post-source-must-match-target", lambda: expect_block(
        lambda c: c["effect_evidence"].update({"post_source_head": "6" * 40}), evidence=True
    ))
    check("lifecycle-28-provider-revision-nonnegative", lambda: expect_block(
        lambda c: c["effect_evidence"].update({"provider_revision": -1}), evidence=True
    ))
    check("lifecycle-29-constructor-bound-context-effect-verifier-required", lambda: expect_block(
        lambda c: None, evidence=True, verifier=None
    ))
    check("lifecycle-30-source-transition-object-required", lambda: expect_block(
        lambda c: c.update({"source_transition": "bad"})
    ))
    check("lifecycle-31-effect-evidence-object-required", lambda: expect_block(
        lambda c: c.update({"effect_evidence": "bad"})
    ))
    check("lifecycle-32-receipt-fingerprint-valid", lambda: expect_block(
        lambda c: c["effect_evidence"].update({"receipt_fingerprint": "bad"}), evidence=True
    ))
    check("lifecycle-33-start-cannot-stand-in-for-requalification", lambda: expect_block(
        lambda c: c.update({"operation": "START"})
    ))
    check("lifecycle-34-terminal-state-cannot-requalify", lambda: expect_block(
        lambda c: c.update({"expected_lifecycle_state": "TERMINATED"})
    ))
    check("lifecycle-35-expected-pointer-required", lambda: expect_block(
        lambda c: c.update({"expected_slot_pointer": ""})
    ))
    check("lifecycle-36-gates-rerun-required", lambda: expect_block(
        lambda c: c["source_transition"].update({"gates_rerun": []})
    ))

    # 8 targetset/serialization/rebase canaries.
    check("lifecycle-37-targetset-intent-fixed", lambda: expect_block(
        lambda c: c["source_transition"].update({"intent": "REQUALIFY"})
    ))
    check("lifecycle-38-previous-source-sha-required", lambda: expect_block(
        lambda c: c["source_transition"].update({"previous_source_head": "bad"})
    ))
    check("lifecycle-39-target-source-sha-required", lambda: expect_block(
        lambda c: c["source_transition"].update({"target_source_head": "bad"})
    ))
    check("lifecycle-40-source-head-must-advance", lambda: expect_block(
        lambda c: c["source_transition"].update({"target_source_head": "1" * 40})
    ))
    check("lifecycle-41-changed-contracts-array-required", lambda: expect_block(
        lambda c: c["source_transition"].update({"changed_contracts": "mcp/control-resolution"})
    ))
    check("lifecycle-42-changed-contracts-unique", lambda: expect_block(
        lambda c: c["source_transition"].update({"changed_contracts": ["A", "A"]})
    ))
    check("lifecycle-43-prerequisite-fingerprint-valid", lambda: expect_block(
        lambda c: c["source_transition"].update({"prerequisite_fingerprint": "bad"})
    ))
    check("lifecycle-44-boot-fingerprint-valid", lambda: expect_block(
        lambda c: c["source_transition"].update({"boot_fingerprint": "bad"})
    ))

    passed = sum(1 for item in tests if item["result"] == "PASS")
    return {
        "schema": "cerebro-project-manager-lifecycle-canaries/v1",
        "result": "PASS" if passed == 44 and len(tests) == 44 else "FAIL",
        "passed": passed,
        "total": len(tests),
        "race_currentness_idempotency_effect": 36,
        "targetset_serialization_rebase": 8,
        "tests": tests,
    }


def anti_loop_canaries(mod: Any) -> dict[str, Any]:
    tests: list[dict[str, Any]] = []
    def check(name: str, fn) -> None:
        try:
            passed = bool(fn())
            tests.append({"name": name, "result": "PASS" if passed else "FAIL"})
        except Exception as exc:
            tests.append({"name": name, "result": "FAIL", "detail": str(exc)})
    def base(*, verification: str = "PENDING", admission: str = "PENDING") -> dict[str, Any]:
        return {"executor_terminal_reconciliation": {"executor_ref": "L-VALIDATOR-CANARY", "execution_state": "TERMINAL_REPORTED", "verification_state": verification, "admission_state": admission, "new_execution_defect_verified": False, "executor_reactivated": False, "evidence_carrier": {"status": "AVAILABLE"}}}
    check("anti-loop-01-verification-pending-does-not-reopen", lambda: mod._executor_terminal_reconciliation_gate(base())["executor_reactivation_allowed"] is False)
    check("anti-loop-02-admission-pending-routes-admission-not-executor", lambda: mod._executor_terminal_reconciliation_gate(base(verification="PASS"))["next_edge_class"] == "ADMIT")
    check("anti-loop-03-unavailable-carrier-routes-unknown", lambda: mod._executor_terminal_reconciliation_gate({"executor_terminal_reconciliation": {**base()["executor_terminal_reconciliation"], "verification_state": "UNAVAILABLE", "evidence_carrier": {"status": "UNAVAILABLE", "exact_evidence_question": "did the terminal effect occur?", "capable_carrier_ref": "CAPABLE-CARRIER"}}})["evidence_result"] == "UNKNOWN")
    def unavailable_subject_fail_blocks() -> bool:
        value = base(verification="FAIL")
        value["executor_terminal_reconciliation"]["evidence_carrier"] = {"status": "UNAVAILABLE", "exact_evidence_question": "did the terminal effect occur?", "capable_carrier_ref": "CAPABLE-CARRIER"}
        try:
            mod._executor_terminal_reconciliation_gate(value)
        except mod.ProjectManagerGovernorError:
            return True
        return False
    check("anti-loop-04-unavailable-carrier-cannot-subject-fail", unavailable_subject_fail_blocks)
    check("anti-loop-05-verified-defect-is-only-reopen-path", lambda: mod._executor_terminal_reconciliation_gate({"executor_terminal_reconciliation": {**base(verification="FAIL")["executor_terminal_reconciliation"], "new_execution_defect_verified": True}})["executor_reactivation_allowed"] is True)
    machine_next = {"owner": "MACHINE", "pm_actor": "PROJECT_MANAGER", "internally_executable": True}
    check("anti-loop-06-self-next-owner-obligation-active", lambda: mod._continuation_progress_gate({}, machine_next)["same_cycle_progress_required"] is True)
    def status_only_blocks() -> bool:
        try:
            mod._continuation_progress_gate({"pm_same_cycle_progress": {"state_delta_observed": False, "status_only_terminal_surface": True, "machine_route_available": True}}, machine_next)
        except mod.ProjectManagerGovernorError:
            return True
        return False
    check("anti-loop-07-status-only-self-next-owner-blocks", status_only_blocks)
    check("anti-loop-08-exact-external-blocker-is-terminal-evidence", lambda: mod._continuation_progress_gate({"pm_same_cycle_progress": {"state_delta_observed": False, "status_only_terminal_surface": False, "exact_external_blocker": "REPO_CARRIER_UNAVAILABLE", "machine_route_available": False}}, machine_next)["result"] == "PASS_EXACT_EXTERNAL_BLOCKER")
    passed = sum(1 for item in tests if item["result"] == "PASS")
    return {"schema": "cerebro-project-manager-anti-loop-canaries/v1", "result": "PASS" if passed == 8 and len(tests) == 8 else "FAIL", "passed": passed, "total": len(tests), "tests": tests}

def validate(root: Path = SOURCE_ROOT, *, require_integration: bool=False) -> dict:
    contract=root/"mcp/project-manager-control-governor.yaml"
    implementation=root/"mcp/project_manager_control_governor.py"
    schema=root/"mcp/project-manager-control-governor-decision.schema.json"
    lifecycle_schema=root/"mcp/actor-lifecycle-mutation.schema.json"
    required=[contract,implementation,schema,lifecycle_schema]
    missing=[str(p.relative_to(root)) for p in required if not p.is_file()]
    if missing:
        return {"schema":"cerebro-project-manager-control-governor-validation/v1","result":"FAIL","missing":missing}

    text=contract.read_text(encoding="utf-8")
    required_contract_tokens=[
        "direct_live_authority: false",
        "state_owner: NONE",
        "AI_supplied_profile_binding_is_authority: false",
        "profile_verifier_injection: CONSTRUCTOR_BOUND",
        "retry_without_fresh_read: PROHIBITED",
        "worker_self_admission: PROHIBITED",
        "silent_stall_surface_prohibited",
        "terminal-completion-without-learning-disposition-blocks",
        "same_generation_source_requalification:",
        "candidate_acceptance_is_ready_effect: false",
        "durable-context-commit-receipt",
        "exact-target-source-head-readback",
        "new_operation_created: false",
        "context_effect_verifier_injection: CONSTRUCTOR_BOUND",
        "AI_supplied_context_receipt_is_authority: false",
        "executor_terminal_reconciliation:",
        "admission_pending_reactivates_executor: false",
        "subject_failure_from_unavailable_carrier: PROHIBITED",
        "self_next_owner_progress:",
        "status_only_terminal_surface: PROHIBITED",
    ]
    missing_tokens=[x for x in required_contract_tokens if x not in text]
    if missing_tokens:
        return {
            "schema":"cerebro-project-manager-control-governor-validation/v1",
            "result":"FAIL","missing_contract_tokens":missing_tokens
        }

    decision_schema=json.loads(schema.read_text(encoding="utf-8"))
    if decision_schema.get("$id")!="cerebro://schemas/project-manager-control-governor-decision/v1":
        return {"schema":"cerebro-project-manager-control-governor-validation/v1","result":"FAIL","error":"schema-id-mismatch"}

    control_text=(root/"mcp/control_resolution.py").read_text(encoding="utf-8")
    manifest_text=(root/"mcp/manifest.yaml").read_text(encoding="utf-8")
    checks_text=(root/"tooling/validator/checks.yaml").read_text(encoding="utf-8")
    registration_tokens=[
        "pm_profile_verifier",
        "project_manager_governance_candidate",
        "project_manager_control_governance",
        "mcp/project-manager-control-governor.yaml",
        "implementation_ref: mcp/project_manager_control_governor.py",
        "status: SOURCE_IMPLEMENTED_ACTIVATION_PENDING_TRUSTED_PROFILE_BINDING",
        "project_manager_control_governor_validation:",
        "validator: tooling/validator/project_manager_control_governor_validation.py",
    ]
    registration_subject="\n".join([control_text,manifest_text,checks_text])
    missing_registration=[x for x in registration_tokens if x not in registration_subject]
    if missing_registration:
        return {
            "schema":"cerebro-project-manager-control-governor-validation/v1",
            "result":"FAIL","missing_registration_tokens":missing_registration
        }

    mod=load(implementation,"cerebro_project_manager_control_governor_validation_subject")
    selftest=mod.selftest()
    if selftest.get("result")!="PASS" or int(selftest.get("passed") or 0)!=23:
        return {"schema":"cerebro-project-manager-control-governor-validation/v1","result":"FAIL","selftest":selftest}

    lifecycle_schema_data=json.loads(lifecycle_schema.read_text(encoding="utf-8"))
    lifecycle_required={
        "source_transition", "effect_evidence", "observed_slot_pointer",
    }
    if not lifecycle_required.issubset(lifecycle_schema_data.get("properties", {})):
        return {
            "schema":"cerebro-project-manager-control-governor-validation/v1",
            "result":"FAIL","error":"lifecycle-schema-currentization-fields-missing"
        }
    lifecycle=lifecycle_canaries(mod)
    if lifecycle.get("result")!="PASS" or int(lifecycle.get("passed") or 0)!=44:
        return {
            "schema":"cerebro-project-manager-control-governor-validation/v1",
            "result":"FAIL","selftest":selftest,"lifecycle_canaries":lifecycle
        }

    anti_loop=anti_loop_canaries(mod)
    if anti_loop.get("result")!="PASS" or int(anti_loop.get("passed") or 0)!=8:
        return {
            "schema":"cerebro-project-manager-control-governor-validation/v1",
            "result":"FAIL","selftest":selftest,"lifecycle_canaries":lifecycle,
            "anti_loop_canaries":anti_loop
        }

    integration=integration_test(root)
    if require_integration and integration.get("result")!="PASS":
        return {
            "schema":"cerebro-project-manager-control-governor-validation/v1",
            "result":"FAIL","selftest":selftest,"integration":integration
        }

    return {
        "schema":"cerebro-project-manager-control-governor-validation/v1",
        "result":"PASS",
        "governor_canaries":23,
        "anti_loop_canaries":anti_loop,
        "executor_terminal_reconciliation_effect":True,
        "self_next_owner_same_cycle_progress_effect":True,
        "trusted_profile_binding_required":True,
        "direct_live_authority":False,
        "state_mutation_by_governor":False,
        "current_host_binding_proven":False,
        "activation_claim_allowed":False,
        "integration":integration,
        "selftest":selftest,
        "lifecycle_canaries":lifecycle,
        "same_generation_source_requalification_effect":True,
        "ready_effect_requires_context_receipt_and_exact_readback":True,
    }

def main()->int:
    parser=argparse.ArgumentParser()
    parser.add_argument("--source-root",default=str(SOURCE_ROOT))
    parser.add_argument("--require-integration",action="store_true")
    args=parser.parse_args()
    result=validate(Path(args.source_root).resolve(),require_integration=args.require_integration)
    print(json.dumps(result,indent=2,ensure_ascii=False))
    return 0 if result["result"]=="PASS" else 1

if __name__=="__main__":
    raise SystemExit(main())
