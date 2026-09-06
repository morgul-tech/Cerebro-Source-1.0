#!/usr/bin/env python3
from __future__ import annotations
import importlib.util,json,tempfile,sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
HAP=ROOT/"engines/presentation/human_admin_projection.py"
SOURCES=ROOT/"tooling/host/human_admin_projection_sources.py"
HOST=ROOT/"tooling/host/cerebro_host.py"
SCHEMA=ROOT/"engines/presentation/human-admin-projection.schema.json"
COMPONENT=ROOT/"tooling/host/component.yaml"

def load(path:Path,name:str):
    spec=importlib.util.spec_from_file_location(name,path); module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module); return module

def run_all():
    hap=load(HAP,"cerebro_hap_validation_impl"); sources=load(SOURCES,"cerebro_hap_validation_sources")
    if str(HOST.parent) not in sys.path: sys.path.insert(0,str(HOST.parent))
    host=load(HOST,"cerebro_hap_validation_host")
    tests=[]
    def check(name,condition): tests.append({"name":name,"result":"PASS" if condition else "FAIL"})
    source="9"*40
    snaps=[
      {"schema":"cerebro-owner-snapshot/v1","owner_ref":"SHARED_WORK","object_ref":"WORK_CLAIMS:838","object_type":"WORK_CLAIM",
       "currentness":"CURRENT","revision_or_token":838,"evidence_ref":"EVENTS:4185","human_summary":"P612 is the active N0 HAP implementation claim",
       "why_it_matters":"It is the current material implementation frontier.","aliases":["P612","claim 838"],"related_refs":["WORK_PACKETS:630"],
       "state":{"status":"ACTIVE_BOUND","current_objective":"N0 Human Admin Projection","where_we_were":"P611 targetset freeze",
       "where_we_are":"P612 HAP core implementation","where_we_are_going":"P613 replaceable Kompass renderer","return_point":"P612 validation",
       "human_cognitive_location":"Implementing shared read-only Human Admin Projection","human_action":"NONE","blockers":[]}},
      {"schema":"cerebro-owner-snapshot/v1","owner_ref":"SHARED_PM","object_ref":"PROJECT_MANAGER_5168B029","object_type":"PROJECT_MANAGER",
       "currentness":"CURRENT","revision_or_token":"pm-current","evidence_ref":"PM_PRINCIPAL_CHANNEL:3448","human_summary":"Current Project Manager",
       "why_it_matters":"Owns bind/start/admit orchestration.","aliases":["current pm"],"state":{"status":"CURRENT"}}
    ]
    p=hap.build_projection(source_revision=source,owner_snapshots=snaps,required_refs=["WORK_CLAIMS:838","PROJECT_MANAGER_5168B029"],projection_revision=3)
    check("projection-validates",hap.validate_projection(p)["projection_fingerprint"]==p["projection_fingerprint"])
    check("presentation-only-authority",p["authority"]=="PRESENTATION_ONLY_NON_AUTHORITATIVE")
    check("deterministic-same-input",p==hap.build_projection(source_revision=source,owner_snapshots=list(snaps),required_refs=["WORK_CLAIMS:838","PROJECT_MANAGER_5168B029"],projection_revision=3))
    check("current-owner-basis-current",p["currentness"]=="CURRENT")
    check("n0-remains-noncurrent-no-authority",p["n0"]["surface_state"]=="NONCURRENT" and p["n0"]["authority_mutation_allowed"] is False)
    brief=hap.render_status(p,"brief"); check("brief-one-screen-12-lines-max",len(brief["lines"])<=12)
    info=hap.render_info(p,"P612"); check("info-exact-resolver",info["result"]=="RESOLVED" and info["canonical_ref"]=="WORK_CLAIMS:838")
    check("info-unresolved-fails-closed",hap.render_info(p,"missing-ref")["result"]=="UNRESOLVED")
    amb=[dict(snaps[0],object_ref="A",aliases=["same"]),dict(snaps[1],object_ref="B",aliases=["same"])]
    pa=hap.build_projection(source_revision=source,owner_snapshots=amb)
    check("ambiguous-ref-fails-closed",hap.render_info(pa,"same")["result"]=="AMBIGUOUS")
    pmiss=hap.build_projection(source_revision=source,owner_snapshots=snaps[:1],required_refs=["WORK_CLAIMS:838","MISSING"])
    check("missing-owner-input-is-unknown",pmiss["currentness"]=="UNKNOWN" and "MISSING_OWNER_INPUT:MISSING" in pmiss["unknowns"])
    stale=[dict(snaps[0],currentness="STALE"),snaps[1]]
    check("stale-basis-not-current",hap.build_projection(source_revision=source,owner_snapshots=stale)["currentness"]=="STALE")
    schema=json.loads(SCHEMA.read_text(encoding="utf-8"))
    check("typed-schema-identity",schema["properties"]["schema"]["const"]=="cerebro-human-admin-projection/v1")
    with tempfile.TemporaryDirectory() as td:
        bundle=Path(td)/"bundle.json"
        bundle.write_text(json.dumps({"schema":sources.BUNDLE_SCHEMA,"projection_revision":4,"required_refs":["WORK_CLAIMS:838"],"owner_snapshots":[snaps[0]]}),encoding="utf-8")
        assembled=sources.assemble_inputs(ROOT,source,str(bundle))
        check("provider-neutral-injected-bundle",assembled["projection_revision"]==4 and any(x.get("object_ref")=="WORK_CLAIMS:838" for x in assembled["owner_snapshots"]))
        hosted_args=host.parse_host_arguments(["status","brief","--snapshot-bundle",str(bundle)])
        hosted=host.run_human_admin_projection(ROOT,source,hosted_args)
        check("host-read-only-view-integration",hosted["result"]=="PASS" and hosted["view"]["depth"]=="brief" and hosted["source_mutation"] is False)
    unavailable=sources.load_snapshot_bundle(None,env={})
    check("missing-provider-bundle-explicit-unavailable",unavailable["provider_state"]=="UNAVAILABLE" and "OWNER_SNAPSHOT_BUNDLE_NOT_INJECTED" in unavailable["unknowns"])
    status_args=host.parse_host_arguments(["status","brief"]); info_args=host.parse_host_arguments(["info","P612"])
    check("host-status-info-parser",status_args.command=="status" and status_args.hap_depth=="brief" and info_args.hap_query=="P612")
    runtime_args=host.parse_host_arguments(["runtime2-supervise","--request","request.json","--output","output.json"])
    check("runtime2-cli-preserved",runtime_args.request=="request.json" and runtime_args.output=="output.json")
    component=COMPONENT.read_text(encoding="utf-8")
    check("host-does-not-own-roadmap-or-projection-truth","- parse-roadmap" in component and "own-human-admin-projection-truth" in component)
    check("no-store-bus-scheduler-surface",not any(hasattr(hap,n) for n in ("save","persist","commit","scheduler","database","bus")))
    return {"schema":"cerebro-human-admin-projection-validation/v1","result":"PASS" if all(x["result"]=="PASS" for x in tests) else "FAIL",
            "tests":tests,"test_count":len(tests),"pass_count":sum(x["result"]=="PASS" for x in tests)}

if __name__=="__main__":
    out=run_all(); print(json.dumps(out,indent=2)); raise SystemExit(0 if out["result"]=="PASS" else 2)
