#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
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
    shared_request=copy.deepcopy(request)
    shared_request["actor_generation"]="IMPLEMENTER-1"
    shared_request["shared_control_transition"]={
        "transition_kind":"START",
        "canonical_consumer":"PROJECT_MANAGER",
        "start_receipt_id":"START-1",
        "packet_id":"PACKET-1",
        "target_generation":"IMPLEMENTER-1",
    }
    shared_request["shared_provider_readback"]={
        "evidence_class":"PROVIDER_READBACK",
        "append_frontier":1142,
        "current_state_frontier":1139,
        "exact_owned_rows_readback_verified":True,
    }
    shared_request["project_manager_governance_candidate"]["shared_write_transaction"]={
        "semantic_intent_id":"START-1:PACKET-1:IMPLEMENTER-1",
        "idempotency_key":"START-KEY-1",
        "active_semantic_writer_count":1,
        "overlapping_attempt_count":0,
        "retry_without_fresh_read":False,
        "transition_kind":"START",
        "h3_safe_publication":True,
        "expected_append_frontier":1142,
        "current_state_used_as_authority":False,
        "duplicate_replay":True,
        "existing_canonical_disposition":{"claim_id":"CLAIM-1"},
        "semantic_deltas":{"authority":0,"claim":0,"assignment":0,"work_start":0,"semantic_start_event":0},
        "provider_write_attempted":False,
        "provider_write_outcome":"NOT_ATTEMPTED",
        "provider_readback_verified":False,
    }
    shared=control.resolve(
        shared_request,root,require_git_ancestry=False,pm_profile_verifier=FixturePMProfileVerifier()
    )
    shared_ok=(
        shared.get("mcp_control_decision",{}).get("outcome")=="CONTINUE"
        and shared.get("shared_control_disposition")=="RETURN_EXISTING_BINDING_NOOP"
        and shared.get("project_manager_control_governance",{}).get("shared_write_gate",{}).get("append_frontier")==1142
    )

    injected=copy.deepcopy(shared_request)
    injected["project_manager_governance_candidate"]["shared_write_transaction"]["provider_state"]={
        "evidence_class":"PROVIDER_READBACK","append_frontier":999,
    }
    injection=control.resolve(
        injected,root,require_git_ancestry=False,pm_profile_verifier=FixturePMProfileVerifier()
    )
    injection_blocked=(
        injection.get("mcp_control_decision",{}).get("outcome")=="BLOCK"
        and "CONTROL_CONTEXT_TRANSITION_INVALID" in injection.get("mcp_control_decision",{}).get("invalidates",[])
    )
    return {
        "result":"PASS" if good_ok and bad_ok and shared_ok and injection_blocked else "FAIL",
        "verified_profile_integration":good_ok,
        "missing_verifier_fail_closed":bad_ok,
        "provider_frontier_and_exact_transition_binding":shared_ok,
        "candidate_provider_state_injection_blocked":injection_blocked,
    }

def validate(root: Path = SOURCE_ROOT, *, require_integration: bool=False) -> dict:
    contract=root/"mcp/project-manager-control-governor.yaml"
    implementation=root/"mcp/project_manager_control_governor.py"
    schema=root/"mcp/project-manager-control-governor-decision.schema.json"
    required=[contract,implementation,schema]
    missing=[str(p.relative_to(root)) for p in required if not p.is_file()]
    if missing:
        return {"schema":"cerebro-project-manager-control-governor-validation/v1","result":"FAIL","missing":missing}

    text=contract.read_text(encoding="utf-8")
    required_contract_tokens=[
        "direct_live_authority: false",
        "state_owner: NONE",
        "AI_supplied_profile_binding_is_authority: false",
        "profile_verifier_injection: CONSTRUCTOR_BOUND",
        "current_host_binding_status: SOURCE_CONTRACT_PROVEN",
        "retry_without_fresh_read: PROHIBITED",
        "authoritative_currentness_basis: PROVIDER_EVENTS_APPEND_FRONTIER",
        "canonical_consumer: PROJECT_MANAGER",
        "provider_readback_before_retry: REQUIRED",
        "worker_self_admission: PROHIBITED",
        "silent_stall_surface_prohibited",
        "terminal-completion-without-learning-disposition-blocks",
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
    schema_gate=(decision_schema.get("properties") or {}).get("shared_write_gate") or {}
    if not {"applicable","result","next_transaction_allowed"}.issubset(set(schema_gate.get("required") or [])):
        return {"schema":"cerebro-project-manager-control-governor-validation/v1","result":"FAIL","error":"shared-write-gate-schema-incomplete"}

    control_text=(root/"mcp/control_resolution.py").read_text(encoding="utf-8")
    host_text=(root/"mcp/control_resolution_host.py").read_text(encoding="utf-8")
    manifest_text=(root/"mcp/manifest.yaml").read_text(encoding="utf-8")
    checks_text=(root/"tooling/validator/checks.yaml").read_text(encoding="utf-8")
    registration_tokens=[
        "pm_profile_verifier",
        "project_manager_governance_candidate",
        "project_manager_control_governance",
        "_bind_shared_pm_governance_candidate",
        "shared_control_disposition",
        "self._pm_profile_verifier",
        "mcp/project-manager-control-governor.yaml",
        "implementation_ref: mcp/project_manager_control_governor.py",
        "status: SOURCE_IMPLEMENTED_ACTIVATION_PENDING_TRUSTED_PROFILE_BINDING",
        "project_manager_control_governor_validation:",
        "validator: tooling/validator/project_manager_control_governor_validation.py",
    ]
    registration_subject="\n".join([control_text,host_text,manifest_text,checks_text])
    missing_registration=[x for x in registration_tokens if x not in registration_subject]
    if missing_registration:
        return {
            "schema":"cerebro-project-manager-control-governor-validation/v1",
            "result":"FAIL","missing_registration_tokens":missing_registration
        }

    mod=load(implementation,"cerebro_project_manager_control_governor_validation_subject")
    selftest=mod.selftest()
    if selftest.get("result")!="PASS" or int(selftest.get("passed") or 0)!=25:
        return {"schema":"cerebro-project-manager-control-governor-validation/v1","result":"FAIL","selftest":selftest}

    integration=integration_test(root)
    if require_integration and integration.get("result")!="PASS":
        return {
            "schema":"cerebro-project-manager-control-governor-validation/v1",
            "result":"FAIL","selftest":selftest,"integration":integration
        }

    return {
        "schema":"cerebro-project-manager-control-governor-validation/v1",
        "result":"PASS",
        "governor_canaries":25,
        "trusted_profile_binding_required":True,
        "direct_live_authority":False,
        "state_mutation_by_governor":False,
        "current_host_binding_proven":True,
        "activation_claim_allowed":False,
        "integration":integration,
        "selftest":selftest,
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
