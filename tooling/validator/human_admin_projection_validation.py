#!/usr/bin/env python3
from __future__ import annotations
import hashlib,importlib.util,json,tempfile,sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
HAP=ROOT/"engines/presentation/human_admin_projection.py"
SOURCES=ROOT/"tooling/host/human_admin_projection_sources.py"
HOST=ROOT/"tooling/host/cerebro_host.py"
SCHEMA=ROOT/"engines/presentation/human-admin-projection.schema.json"
COMPONENT=ROOT/"tooling/host/component.yaml"
HMI_KERNEL_REFS=("standards/human-continuation-surface.yaml","standards/continuation-surface-system-policy.yaml","engines/presentation/human-admin-projection.schema.json")
PRE_ROOM_COLOR_HMI_KERNEL_FINGERPRINT="da91c3784cd85af2cc040b2fbd739790787e97d035b02d8e2cee3adc92b7ad11"

def hmi_kernel_fingerprint()->str:
    material="\n".join(f"{ref}|{hashlib.sha256((ROOT/ref).read_bytes()).hexdigest()}" for ref in HMI_KERNEL_REFS)
    return hashlib.sha256(f"CEREBRO-HMI-BIRTH-KERNEL-001|1.0|{material}".encode("utf-8")).hexdigest()

def load(path:Path,name:str):
    spec=importlib.util.spec_from_file_location(name,path); module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module); return module

def run_all():
    hap=load(HAP,"cerebro_hap_validation_impl"); sources=load(SOURCES,"cerebro_hap_validation_sources")
    if str(HOST.parent) not in sys.path: sys.path.insert(0,str(HOST.parent))
    host=load(HOST,"cerebro_hap_validation_host")
    tests=[]
    def check(name,condition): tests.append({"name":name,"result":"PASS" if condition else "FAIL"})
    def rejects(name,fn):
        try: fn()
        except hap.HumanAdminProjectionError: check(name,True)
        else: check(name,False)
    source="9"*40
    snaps=[
      {"schema":"cerebro-owner-snapshot/v1","owner_ref":"SHARED_WORK","object_ref":"WORK_CLAIMS:838","object_type":"WORK_CLAIM",
       "currentness":"CURRENT","revision_or_token":838,"evidence_ref":"EVENTS:4185","human_summary":"P612 is the active N0 HAP implementation claim",
       "why_it_matters":"It is the current material implementation frontier.","aliases":["P612","claim 838"],"related_refs":["WORK_PACKETS:630"],
       "state":{"status":"ACTIVE_BOUND","human_label":"HAP implementation","current_objective":"N0 Human Admin Projection","where_we_were":"P611 targetset freeze",
       "where_we_are":"P612 HAP core implementation","where_we_are_going":"P613 replaceable Kompass renderer","return_point":"P612 validation",
       "human_cognitive_location":"Implementing shared read-only Human Admin Projection","human_action":"NONE","blockers":[],
       "role_assessment":{"role":"HA"},"responsibility_assessment":{"owner":"MACHINE"},
       "human_boundary_assessment":{"real_human_action_is_next":False},"presentation_request":{"dialect":"IMPLEMENTER"}}},
      {"schema":"cerebro-owner-snapshot/v1","owner_ref":"SHARED_PM","object_ref":"PROJECT_MANAGER_5168B029","object_type":"PROJECT_MANAGER",
       "currentness":"CURRENT","revision_or_token":"pm-current","evidence_ref":"PM_PRINCIPAL_CHANNEL:3448","human_summary":"Current Project Manager",
       "why_it_matters":"Owns bind/start/admit orchestration.","aliases":["current pm"],"state":{"status":"CURRENT","human_label":"HAP implementation"}}
    ]
    p=hap.build_projection(source_revision=source,owner_snapshots=snaps,required_refs=["WORK_CLAIMS:838","PROJECT_MANAGER_5168B029"],projection_revision=3)
    check("projection-validates",hap.validate_projection(p)["projection_fingerprint"]==p["projection_fingerprint"])
    check("presentation-only-authority",p["authority"]=="PRESENTATION_ONLY_NON_AUTHORITATIVE")
    check("deterministic-same-input",p==hap.build_projection(source_revision=source,owner_snapshots=list(snaps),required_refs=["WORK_CLAIMS:838","PROJECT_MANAGER_5168B029"],projection_revision=3))
    check("current-owner-basis-current",p["currentness"]=="CURRENT")
    check("n0-remains-noncurrent-no-authority",p["n0"]["surface_state"]=="NONCURRENT" and p["n0"]["authority_mutation_allowed"] is False)
    check("typed-interaction-signals-consumed",p["responsibility_assessment"]["owner"]=="MACHINE" and p["presentation_request"]["dialect"]=="IMPLEMENTER")
    check("machine-route-before-human-relay",p["next_human_gate"]=="NONE" and p["hmi"]["machine_route_before_human_relay"] is True)
    check("human-label-primary-typed",p["human_label"]=="HAP implementation" and p["hmi"]["human_label_primary"] is True)
    check("HA-presentation-shorthand-only",p["role_assessment"]=={"role":"HUMAN_ADMIN","presentation_shorthand":"HA","identity_authority":"NONE"} and p["hmi"]["actor_identity_mutation_allowed"] is False)
    check("carrier-change-has-no-identity-effect",p["hmi"]["carrier_change_identity_effect"]=="NONE" and "actor_id" not in p["role_assessment"])
    check("no-room-renders-no-room-marker",p["room_identity"]["active"] is False and not hap.render_status(p,"brief")["lines"][0].startswith(tuple(x[0] for x in hap.ROOM_PALETTE)))
    room_snaps=json.loads(json.dumps(snaps))
    for snap in room_snaps:
        snap["state"].update({"room_id":"ROOM-BROWSER-ALPHA-001","case_container_id":"CASE-ALPHA-001","room_name":"Kildeprøving"})
    participant_a=hap.build_projection(source_revision=source,owner_snapshots=[room_snaps[0]])
    participant_b=hap.build_projection(source_revision=source,owner_snapshots=[room_snaps[1]])
    view_a=hap.render_status(participant_a,"brief"); view_b=hap.render_status(participant_b,"brief")
    header_a=view_a["lines"][0]; header_b=view_b["lines"][0]
    check("same-room-independent-participants-same-marker-and-text",header_a==header_b and header_a==view_a["room_identity"]["header"])
    reopened=hap.build_projection(source_revision=source,owner_snapshots=json.loads(json.dumps([room_snaps[0]])))
    check("close-reopen-same-room-same-header-without-live-state",hap.render_status(reopened,"brief")["lines"][0]==header_a)
    changed_room=json.loads(json.dumps([room_snaps[0]])); changed_room[0]["state"]["room_id"]="ROOM-BROWSER-BETA-017"; changed_room[0]["state"]["case_container_id"]="CASE-BETA-017"; changed_room[0]["state"]["room_name"]="Sikkerhetsrom"
    changed=hap.build_projection(source_revision=source,owner_snapshots=changed_room)
    changed_view=hap.render_status(changed,"brief")
    check("changed-room-recomputes-never-inherits-identity",changed["room_identity"]["durable_room_id"]!=participant_a["room_identity"]["durable_room_id"] and changed_view["room_identity"]["palette_marker"]!=view_a["room_identity"]["palette_marker"])
    pass_state=json.loads(json.dumps([room_snaps[0]])); pass_state[0]["state"]["status"]="PASS"
    hold_state=json.loads(json.dumps([room_snaps[0]])); hold_state[0]["state"]["status"]="HOLD"
    pass_view=hap.render_status(hap.build_projection(source_revision=source,owner_snapshots=pass_state),"brief")
    hold_view=hap.render_status(hap.build_projection(source_revision=source,owner_snapshots=hold_state),"brief")
    check("status-health-authority-do-not-change-room-color",pass_view["room_identity"]["palette_marker"]==hold_view["room_identity"]["palette_marker"]==view_a["room_identity"]["palette_marker"])
    check("room-projection-fingerprint-deterministic",participant_a==hap.build_projection(source_revision=source,owner_snapshots=json.loads(json.dumps([room_snaps[0]]))))
    info_room=hap.render_info(participant_a,"P612")
    check("room-header-absolute-top-status-and-info",header_a.startswith(view_a["room_identity"]["palette_marker"]+" ROM · ") and info_room["lines"][0]==header_a)
    gate_snaps=json.loads(json.dumps(snaps)); gate_snaps[0]["state"]["responsibility_assessment"]={"owner":"HUMAN"}
    gate_snaps[0]["state"]["human_boundary_assessment"]={"real_human_action_is_next":True,"next_human_gate":"APPROVE_RELEASE"}
    pgate=hap.build_projection(source_revision=source,owner_snapshots=gate_snaps)
    check("genuine-human-gate-remains-visible",pgate["next_human_gate"]=="APPROVE_RELEASE" and pgate["human_surface"]["human_action"]=="APPROVE_RELEASE" and pgate["human_surface"]["human_decision_required"]=="APPROVE_RELEASE")
    confirm_snaps=json.loads(json.dumps(gate_snaps))
    for snap in confirm_snaps: snap["state"]["human_label"]="Human bekreftelse"
    confirm_snaps[0]["state"].update({"status":"ARMED_PENDING_CONFIRM","human_label":"Human bekreftelse","human_action":"CONFIRM","human_boundary_assessment":{"real_human_action_is_next":True,"next_human_gate":"CONFIRM"}})
    pconfirm=hap.build_projection(source_revision=source,owner_snapshots=confirm_snaps)
    confirm_view=hap.render_status(pconfirm,"standard")
    check("HG04-governing-CONFIRM-rendered-presentation-only",pconfirm["next_human_gate"]=="CONFIRM" and pconfirm["human_surface"]["human_decision_required"]=="CONFIRM" and "CONFIRM" in confirm_view["text"] and pconfirm["authority"]=="PRESENTATION_ONLY_NON_AUTHORITATIVE" and pconfirm["n0"]["authority_mutation_allowed"] is False)
    foreground=json.loads(json.dumps(snaps)); foreground[0]["state"]["responsibility_assessment"]={"owner":"HUMAN"}
    foreground[0]["state"]["human_boundary_assessment"]={"boundary_kind":"FOREGROUND_TRANSPORT","real_human_action_is_next":True,"governing_human_gate_is_next":False}
    foreground[0]["state"]["transport_action"]="Åpne arbeidsvinduet"
    pforeground=hap.build_projection(source_revision=source,owner_snapshots=foreground)
    foreground_view=hap.render_status(pforeground,"standard")
    check("foreground-transport-ne-human-decision",pforeground["next_human_gate"]=="NONE" and pforeground["human_surface"]["human_action"]=="Ingen handling fra deg." and pforeground["human_surface"]["human_transport_required"]=="Åpne arbeidsvinduet" and pforeground["human_surface"]["human_decision_required"]=="NONE" and any(line=="HUMAN_TRANSPORT_REQUIRED: Åpne arbeidsvinduet" for line in foreground_view["lines"]))
    brief=hap.render_status(p,"brief"); standard=hap.render_status(p,"standard",scope="WORK_PACKETS:630")
    deep=hap.render_status(p,"deep",scope="WORK_PACKETS:630")
    check("meaning-first-status-line",brief["lines"][0].startswith("STATUS: "))
    check("human-action-explicit-second-line",brief["lines"][1]=="HUMAN_ACTION: Ingen handling fra deg.")
    check("machine-next-separate-third-line",brief["lines"][2].startswith("NEXT_MACHINE: ") and "Eier: MACHINE" in brief["lines"][2])
    check("brief-one-screen-12-lines-max",len(brief["lines"])<=12)
    check("opaque-ids-absent-brief-standard",not any(token in brief["text"]+standard["text"] for token in ("P612","WORK_CLAIMS:838","WORK_PACKETS:630")))
    check("opaque-ids-available-deep",all(token in deep["text"] for token in ("WORK_CLAIMS:838","WORK_PACKETS:630")))
    info=hap.render_info(p,"P612"); check("info-exact-resolver",info["result"]=="RESOLVED" and info["canonical_ref"]=="WORK_CLAIMS:838")
    check("opaque-id-available-explicit-info",info["lines"][0].startswith("WORK_CLAIMS:838 "))
    check("info-unresolved-fails-closed",hap.render_info(p,"missing-ref")["result"]=="UNRESOLVED")
    amb=[dict(snaps[0],object_ref="A",aliases=["same"]),dict(snaps[1],object_ref="B",aliases=["same"])]
    pa=hap.build_projection(source_revision=source,owner_snapshots=amb)
    check("ambiguous-ref-fails-closed",hap.render_info(pa,"same")["result"]=="AMBIGUOUS")
    no_label=json.loads(json.dumps(snaps))
    for snap in no_label: snap["state"].pop("human_label",None)
    rejects("missing-human-label-fails-closed",lambda: hap.build_projection(source_revision=source,owner_snapshots=no_label))
    ambiguous_label=json.loads(json.dumps(snaps)); ambiguous_label[1]["state"]["human_label"]="PM coordination"
    rejects("ambiguous-human-label-fails-closed",lambda: hap.build_projection(source_revision=source,owner_snapshots=ambiguous_label))
    invalid_label=json.loads(json.dumps(snaps)); invalid_label[0]["state"]["human_label"]="P764 source effect"
    rejects("opaque-human-label-fails-closed",lambda: hap.build_projection(source_revision=source,owner_snapshots=invalid_label))
    legacy_label=json.loads(json.dumps(snaps))
    for snap in legacy_label: snap["state"].pop("human_label",None)
    legacy_label[0]["state"]["human_cognitive_location"]="Kompass closure"
    check("legacy-semantic-cognitive-label-fallback",hap.build_projection(source_revision=source,owner_snapshots=legacy_label)["human_label"]=="Kompass closure")
    waiting=json.loads(json.dumps([snaps[0]])); waiting[0]["state"].update({"waiting_input":True,"next_owner_label":"PM admission","dependency_labels":["validator pass"]})
    pwait=hap.build_projection(source_revision=source,owner_snapshots=waiting)
    waiting_view=hap.render_status(pwait,"standard")
    check("waiting-standard-meaning-first-three-lines",waiting_view["lines"]==["STATUS: Venter på: validator pass","HUMAN_ACTION: Ingen handling fra deg.","NEXT_MACHINE: Fortsetter når: validator pass · Eier: PM admission"])
    check("waiting-does-not-create-human-pulse",pwait["next_human_gate"]=="NONE" and pwait["waiting_input"]["human_action_required"] is False and pwait["hmi"]["waiting_is_human_pulse"] is False)
    room_waiting=json.loads(json.dumps([room_snaps[0]])); room_waiting[0]["state"].update({"waiting_input":True,"next_owner_label":"PM admission","dependency_labels":["validator pass"]})
    room_wait_view=hap.render_status(hap.build_projection(source_revision=source,owner_snapshots=room_waiting),"standard")
    check("room-waiting-keeps-header-absolute-top",len(room_wait_view["lines"])==4 and room_wait_view["lines"][0]==header_a and room_wait_view["lines"][1:]==waiting_view["lines"])
    waiting_deep=hap.render_status(pwait,"deep",scope="WORK_PACKETS:764")
    check("waiting-deep-keeps-opaque-diagnostics-explicit",waiting_deep["lines"][:3]==waiting_view["lines"] and "WORK_PACKETS:764" in waiting_deep["text"] and "WORK_CLAIMS:838" in waiting_deep["text"])
    waiting_gate=json.loads(json.dumps(waiting)); waiting_gate[0]["state"].update({"responsibility_assessment":{"owner":"HUMAN"},"human_boundary_assessment":{"real_human_action_is_next":True,"next_human_gate":"APPROVE"}})
    rejects("waiting-and-genuine-human-gate-conflict-fails-closed",lambda: hap.build_projection(source_revision=source,owner_snapshots=waiting_gate))
    pmiss=hap.build_projection(source_revision=source,owner_snapshots=snaps[:1],required_refs=["WORK_CLAIMS:838","MISSING"])
    check("missing-owner-input-is-unknown",pmiss["currentness"]=="UNKNOWN" and "MISSING_OWNER_INPUT:MISSING" in pmiss["unknowns"])
    stale=[dict(snaps[0],currentness="STALE"),snaps[1]]
    check("stale-basis-not-current",hap.build_projection(source_revision=source,owner_snapshots=stale)["currentness"]=="STALE")
    schema=json.loads(SCHEMA.read_text(encoding="utf-8"))
    check("typed-schema-identity",schema["properties"]["schema"]["const"]=="cerebro-human-admin-projection/v1")
    check("typed-room-identity-schema-consumed",schema["properties"]["room_identity"]["properties"]["membership_semantics"]["const"]=="MEMBERSHIP_ONLY" and "room_identity" in schema["required"])
    check("typed-human-surface-schema-consumed","human_surface" in schema["required"] and set(schema["properties"]["human_surface"]["required"])=={"status","human_action","human_transport_required","human_decision_required","next_machine","next_owner","attention"} and "waiting_input" in schema["required"])
    current_hmi_fingerprint=hmi_kernel_fingerprint()
    check("hmi-birth-kernel-fingerprint-readback-changed",current_hmi_fingerprint!=PRE_ROOM_COLOR_HMI_KERNEL_FINGERPRINT)
    with tempfile.TemporaryDirectory() as td:
        bundle=Path(td)/"bundle.json"
        bundle.write_text(json.dumps({"schema":sources.BUNDLE_SCHEMA,"projection_revision":4,"required_refs":["WORK_CLAIMS:838"],"owner_snapshots":[snaps[0]]}),encoding="utf-8")
        assembled=sources.assemble_inputs(ROOT,source,str(bundle))
        check("provider-neutral-injected-bundle",assembled["projection_revision"]==4 and any(x.get("object_ref")=="WORK_CLAIMS:838" for x in assembled["owner_snapshots"]))
        hosted_args=host.parse_host_arguments(["status","brief","--snapshot-bundle",str(bundle)])
        hosted=host.run_human_admin_projection(ROOT,source,hosted_args)
        check("host-read-only-view-integration",hosted["result"]=="PASS" and hosted["view"]["depth"]=="brief" and hosted["source_mutation"] is False)
        browser_headers=[]
        for index,snapshot in enumerate((room_snaps[0],room_snaps[1],json.loads(json.dumps(room_snaps[0])))):
            room_bundle=Path(td)/f"room-browser-{index}.json"
            room_bundle.write_text(json.dumps({"schema":sources.BUNDLE_SCHEMA,"projection_revision":index,"owner_snapshots":[snapshot]}),encoding="utf-8")
            room_args=host.parse_host_arguments(["status","brief","--snapshot-bundle",str(room_bundle)])
            browser_headers.append(host.run_human_admin_projection(ROOT,source,room_args)["view"]["lines"][0])
        check("browser-live-reopen-render-path",browser_headers==[header_a,header_a,header_a])
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
            "hmi_birth_kernel_fingerprint":current_hmi_fingerprint,"room_header":header_a,
            "tests":tests,"test_count":len(tests),"pass_count":sum(x["result"]=="PASS" for x in tests)}

if __name__=="__main__":
    out=run_all(); print(json.dumps(out,indent=2)); raise SystemExit(0 if out["result"]=="PASS" else 2)
