#!/usr/bin/env python3
from __future__ import annotations
import hashlib,importlib.util,json,os
from pathlib import Path
from typing import Any

BUNDLE_SCHEMA="cerebro-human-admin-owner-snapshot-bundle/v1"
STATIC_REFS=(
 ("SOURCE:PROJECT_STATUS","STANDARD","standards/project-status.yaml",["status","project status"]),
 ("SOURCE:HUMAN_CONTINUATION","STANDARD","standards/human-continuation-surface.yaml",["continuation","return point"]),
 ("SOURCE:TERMINOLOGY","TERMINOLOGY","modules/terminology/terms.yaml",["terms","terminology"]),
 ("SOURCE:FROZEN_ROADMAP","ROADMAP_FOUNDATION","engines/project/roadmap-schema.yaml",["roadmap","frozen roadmap","forankret grunnlag"]),
 ("SOURCE:LIVE_SHADOW","PRESENTATION_CONTRACT","engines/presentation/live_shadow_projection.py",["live shadow","currentness"]),
)

class HumanAdminProjectionSourceError(ValueError): pass

def _sha(path:Path)->str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def _load_hap(source_root:Path):
    path=source_root/"engines/presentation/human_admin_projection.py"
    spec=importlib.util.spec_from_file_location("cerebro_human_admin_projection",path)
    if spec is None or spec.loader is None: raise HumanAdminProjectionSourceError("hap-module-load-failed")
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module

def source_static_snapshots(source_root:Path,source_revision:str)->list[dict[str,Any]]:
    out=[]
    for object_ref,object_type,rel,aliases in STATIC_REFS:
        path=source_root/rel
        if not path.is_file(): continue
        out.append({"schema":"cerebro-owner-snapshot/v1","owner_ref":"CEREBRO_SOURCE","object_ref":object_ref,
                    "object_type":object_type,"currentness":"CURRENT","revision_or_token":source_revision,
                    "evidence_ref":f"{rel}@sha256:{_sha(path)}","human_summary":f"Source-backed {object_type.lower()} contract",
                    "why_it_matters":"Stable read dependency for Human Admin projection; not live owner state.",
                    "state":{"status":"SOURCE_CURRENT"},"aliases":aliases,"related_refs":[rel]})
    return out

def load_snapshot_bundle(path:str|None=None,env:dict[str,str]|None=None)->dict[str,Any]:
    env=env or os.environ
    selected=path or env.get("CEREBRO_HAP_SNAPSHOT_BUNDLE")
    if not selected:
        return {"schema":BUNDLE_SCHEMA,"projection_revision":0,"required_refs":[],"owner_snapshots":[],
                "provider_state":"UNAVAILABLE","unknowns":["OWNER_SNAPSHOT_BUNDLE_NOT_INJECTED"]}
    value=json.loads(Path(selected).read_text(encoding="utf-8"))
    if not isinstance(value,dict) or value.get("schema")!=BUNDLE_SCHEMA:
        raise HumanAdminProjectionSourceError("snapshot-bundle-schema-mismatch")
    if not isinstance(value.get("owner_snapshots",[]),list): raise HumanAdminProjectionSourceError("owner-snapshots-array-required")
    if not isinstance(value.get("required_refs",[]),list): raise HumanAdminProjectionSourceError("required-refs-array-required")
    return value

def assemble_inputs(source_root:Path,source_revision:str,snapshot_bundle_path:str|None=None)->dict[str,Any]:
    bundle=load_snapshot_bundle(snapshot_bundle_path)
    dynamic=list(bundle.get("owner_snapshots") or [])
    static=source_static_snapshots(source_root,source_revision)
    required=list(bundle.get("required_refs") or [])
    unknowns=list(bundle.get("unknowns") or [])
    if bundle.get("provider_state")=="UNAVAILABLE": unknowns.append("PROVIDER_OWNER_INPUT_UNAVAILABLE")
    return {"owner_snapshots":[*dynamic,*static],"required_refs":required,
            "projection_revision":int(bundle.get("projection_revision") or 0),"source_unknowns":sorted(set(unknowns))}

def build_projection(source_root:Path,source_revision:str,snapshot_bundle_path:str|None=None)->dict[str,Any]:
    hap=_load_hap(source_root); inputs=assemble_inputs(source_root,source_revision,snapshot_bundle_path)
    projection=hap.build_projection(source_revision=source_revision,owner_snapshots=inputs["owner_snapshots"],
                                    required_refs=inputs["required_refs"],projection_revision=inputs["projection_revision"])
    if inputs["source_unknowns"]:
        body={k:v for k,v in projection.items() if k!="projection_fingerprint"}
        body["unknowns"]=sorted(set([*body["unknowns"],*inputs["source_unknowns"]]))
        if body["currentness"]=="CURRENT": body["currentness"]="UNKNOWN"
        projection={**body,"projection_fingerprint":hap.fingerprint(body)}
    return projection

def run_view(source_root:Path,source_revision:str,command:str,*,snapshot_bundle_path:str|None=None,depth:str="standard",scope:str|None=None,query:str|None=None)->dict[str,Any]:
    hap=_load_hap(source_root); projection=build_projection(source_root,source_revision,snapshot_bundle_path)
    if command=="status": view=hap.render_status(projection,depth=depth,scope=scope)
    elif command=="info": view=hap.render_info(projection,query or "")
    else: raise HumanAdminProjectionSourceError("unsupported-hap-command")
    return {"schema":"cerebro-human-admin-cli-result/v1","result":"PASS","command":command,
            "projection":projection,"view":view,"source_mutation":False,"authority_mutation":False}
