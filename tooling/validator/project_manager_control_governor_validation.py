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
        "retry_without_fresh_read: PROHIBITED",
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
    if selftest.get("result")!="PASS" or int(selftest.get("passed") or 0)!=17:
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
        "governor_canaries":17,
        "trusted_profile_binding_required":True,
        "direct_live_authority":False,
        "state_mutation_by_governor":False,
        "current_host_binding_proven":False,
        "activation_claim_allowed":False,
        "integration":integration,
        "selftest":selftest,
    }

# PREBUILD VALIDATION DELTA
# - require mcp/actor-lifecycle-mutation.schema.json
# - require lifecycle_mutation_gate in decision schema
# - execute the 36 race/currentness/idempotency canaries
# - execute 8 targetset/serialization/rebase canaries
# - expected total lifecycle family = 44
# - integration must prove no BOUND/WORK_STARTED effect before Context receipt

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
