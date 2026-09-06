#!/usr/bin/env python3
from __future__ import annotations
import hashlib,json,re
from typing import Any,Mapping,Sequence

SCHEMA="cerebro-human-admin-projection/v1"
AUTHORITY="PRESENTATION_ONLY_NON_AUTHORITATIVE"
CURRENTNESS=("CURRENT","STALE","GAP","UNKNOWN","PROVISIONAL")
_HEX40=re.compile(r"^[0-9a-f]{40}$")
_WS=re.compile(r"\s+")

class HumanAdminProjectionError(ValueError): pass

def _text(value:Any,name:str,allow_empty:bool=False)->str:
    if not isinstance(value,str): raise HumanAdminProjectionError(f"{name}:string-required")
    value=value.strip()
    if not value and not allow_empty: raise HumanAdminProjectionError(f"{name}:nonempty-string-required")
    return value

def _canonical(value:Any)->bytes:
    return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode("utf-8")

def fingerprint(value:Any)->str:
    return hashlib.sha256(_canonical(value)).hexdigest()

def _currentness(value:Any)->str:
    value=_text(value,"currentness").upper()
    if value not in CURRENTNESS: raise HumanAdminProjectionError("currentness:invalid")
    return value

def _norm_key(value:str)->str:
    return _WS.sub(" ",value.strip().casefold())

def _list_text(value:Any,name:str)->list[str]:
    if value is None: return []
    if not isinstance(value,list) or not all(isinstance(x,str) for x in value):
        raise HumanAdminProjectionError(f"{name}:string-array-required")
    return [x.strip() for x in value if x.strip()]

def normalize_snapshot(raw:Mapping[str,Any])->dict[str,Any]:
    if not isinstance(raw,Mapping): raise HumanAdminProjectionError("snapshot:object-required")
    if raw.get("schema")!="cerebro-owner-snapshot/v1": raise HumanAdminProjectionError("snapshot:schema-mismatch")
    revision=raw.get("revision_or_token")
    valid_revision=(isinstance(revision,int) and not isinstance(revision,bool) and revision>=0) or (isinstance(revision,str) and bool(revision.strip()))
    if not valid_revision: raise HumanAdminProjectionError("snapshot:revision-or-token-required")
    state=raw.get("state") or {}
    if not isinstance(state,Mapping): raise HumanAdminProjectionError("snapshot:state-object-required")
    canonical_ref=_text(raw.get("object_ref"),"snapshot.object_ref")
    evidence_ref=_text(raw.get("evidence_ref"),"snapshot.evidence_ref")
    evidence_refs=[evidence_ref,*_list_text(raw.get("evidence_refs"),"snapshot.evidence_refs")]
    return {
        "canonical_ref":canonical_ref,"object_type":_text(raw.get("object_type"),"snapshot.object_type").upper(),
        "owner_ref":_text(raw.get("owner_ref"),"snapshot.owner_ref"),"currentness":_currentness(raw.get("currentness")),
        "revision_or_token":revision,"human_summary":_text(raw.get("human_summary",""),"snapshot.human_summary",allow_empty=True),
        "why_it_matters":_text(raw.get("why_it_matters",""),"snapshot.why_it_matters",allow_empty=True),"state":dict(state),
        "aliases":sorted(set(_list_text(raw.get("aliases"),"snapshot.aliases"))),
        "related_refs":sorted(set(_list_text(raw.get("related_refs"),"snapshot.related_refs"))),
        "evidence_refs":sorted(set(evidence_refs)),
    }

def _projection_currentness(objects:Sequence[Mapping[str,Any]],missing:Sequence[str])->str:
    if missing: return "UNKNOWN"
    states={str(x.get("currentness")) for x in objects}
    for state in ("GAP","UNKNOWN","STALE","PROVISIONAL"):
        if state in states: return state
    return "CURRENT"

def _unique_state_text(objects:Sequence[Mapping[str,Any]],fields:Sequence[str],default:str="UNKNOWN")->str:
    values=[]
    for obj in objects:
        if obj.get("currentness")!="CURRENT": continue
        state=obj.get("state") or {}
        for field in fields:
            value=state.get(field)
            if isinstance(value,str) and value.strip(): values.append(value.strip()); break
    values=sorted(set(values))
    return values[0] if len(values)==1 else default

def _collect_state_list(objects:Sequence[Mapping[str,Any]],fields:Sequence[str])->list[str]:
    out=[]
    for obj in objects:
        state=obj.get("state") or {}
        for field in fields:
            value=state.get(field)
            if isinstance(value,str) and value.strip(): out.append(value.strip())
            elif isinstance(value,list): out.extend(x.strip() for x in value if isinstance(x,str) and x.strip())
    return sorted(set(out))

def build_projection(*,source_revision:str,owner_snapshots:Sequence[Mapping[str,Any]],required_refs:Sequence[str]=(),projection_ref:str="human-admin/current",projection_revision:int=0)->dict[str,Any]:
    source_revision=_text(source_revision,"source_revision").lower()
    if not _HEX40.fullmatch(source_revision): raise HumanAdminProjectionError("source_revision:sha1-required")
    if not isinstance(projection_revision,int) or isinstance(projection_revision,bool) or projection_revision<0:
        raise HumanAdminProjectionError("projection_revision:nonnegative-int-required")
    objects=[normalize_snapshot(x) for x in owner_snapshots]
    refs=[x["canonical_ref"] for x in objects]
    if len(refs)!=len(set(refs)): raise HumanAdminProjectionError("snapshot:duplicate-canonical-ref")
    required=sorted(set(_text(x,"required_ref") for x in required_refs)); observed=sorted(refs)
    missing=sorted(set(required)-set(observed))
    basis=[{"owner_ref":x["owner_ref"],"object_ref":x["canonical_ref"],"revision_or_token":x["revision_or_token"],"currentness":x["currentness"],"evidence_ref":x["evidence_refs"][0]} for x in objects]
    currentness=_projection_currentness(objects,missing)
    current_objective=_unique_state_text(objects,("current_objective","objective"))
    human_location=_unique_state_text(objects,("human_cognitive_location","cognitive_location"))
    return_points=_collect_state_list(objects,("return_points","return_point"))
    blockers=_collect_state_list(objects,("blockers","blocked_by","blocker"))
    active_work=sorted({x["canonical_ref"] for x in objects if str((x.get("state") or {}).get("status","")).upper().startswith("ACTIVE") or x["object_type"] in {"ACTIVE_WORK","WORK_PACKET_ACTIVE"}})
    human_gate=_unique_state_text(objects,("next_human_gate","human_action","human_gate"),default="UNKNOWN" if missing else "NONE")
    where_were=_unique_state_text(objects,("where_we_were",),default="UNKNOWN")
    where_are=_unique_state_text(objects,("where_we_are","current_state"),default=current_objective)
    where_going=_unique_state_text(objects,("where_we_are_going","next_horizon"),default="UNKNOWN")
    unknowns=[f"MISSING_OWNER_INPUT:{x}" for x in missing]
    evidence_refs=sorted({r for x in objects for r in x["evidence_refs"]})
    content={
        "schema":SCHEMA,"authority":AUTHORITY,"projection_ref":_text(projection_ref,"projection_ref"),
        "projection_revision":projection_revision,"source_revision":source_revision,"currentness":currentness,
        "basis_set":basis,"coverage":{"required":required,"observed":observed,"missing":missing},"unknowns":unknowns,
        "orientation":{"where_we_were":where_were,"where_we_are":where_are,"where_we_are_going":where_going},
        "current_objective":current_objective,"return_points":return_points,"human_cognitive_location":human_location,
        "blockers":blockers,"next_human_gate":human_gate,"active_work":active_work,"objects":objects,"evidence_refs":evidence_refs,
        "n0":{"surface_state":"NONCURRENT","authority_mutation_allowed":False,"current_situation":where_are,
              "continuation_view":{"current_objective":current_objective,"return_points":return_points,"active_work":active_work,"blockers":blockers,"next_human_gate":human_gate},
              "human_cognitive_location":human_location},
    }
    return {**content,"projection_fingerprint":fingerprint(content)}

def validate_projection(value:Mapping[str,Any])->dict[str,Any]:
    if not isinstance(value,Mapping) or value.get("schema")!=SCHEMA or value.get("authority")!=AUTHORITY:
        raise HumanAdminProjectionError("projection:identity-or-authority-mismatch")
    supplied=value.get("projection_fingerprint"); body={k:v for k,v in value.items() if k!="projection_fingerprint"}
    if supplied!=fingerprint(body): raise HumanAdminProjectionError("projection:fingerprint-mismatch")
    if value.get("currentness") not in CURRENTNESS: raise HumanAdminProjectionError("projection:currentness-invalid")
    if (value.get("n0") or {}).get("surface_state")!="NONCURRENT" or (value.get("n0") or {}).get("authority_mutation_allowed") is not False:
        raise HumanAdminProjectionError("projection:n0-authority-boundary-invalid")
    return dict(value)

def resolve_ref(projection:Mapping[str,Any],query:str)->dict[str,Any]:
    validate_projection(projection); key=_norm_key(_text(query,"query")); matches=[]
    for obj in projection["objects"]:
        keys={_norm_key(obj["canonical_ref"]),*(_norm_key(x) for x in obj.get("aliases",[]))}
        if key in keys: matches.append(obj)
    if len(matches)==1: return {"result":"RESOLVED","object":matches[0]}
    return {"result":"AMBIGUOUS" if len(matches)>1 else "UNRESOLVED","candidate_refs":sorted(x["canonical_ref"] for x in matches)}

def render_status(projection:Mapping[str,Any],depth:str="standard",scope:str|None=None)->dict[str,Any]:
    validate_projection(projection); depth=_text(depth,"depth").lower()
    if depth not in {"brief","standard","deep"}: raise HumanAdminProjectionError("status:depth-invalid")
    prefix=f"[{projection['currentness']}] "
    lines=[prefix+"HER ER VI: "+projection["orientation"]["where_we_are"],"MAL: "+projection["current_objective"]]
    if depth!="brief":
        lines += ["VEIEN VIDERE: "+projection["orientation"]["where_we_are_going"],
                  "BLOKKERE: "+(", ".join(projection["blockers"]) if projection["blockers"] else "NONE"),
                  "HUMAN: "+projection["next_human_gate"],
                  "RETURN: "+(", ".join(projection["return_points"]) if projection["return_points"] else "UNKNOWN")]
    if depth=="deep":
        lines += ["AKTIVT: "+(", ".join(projection["active_work"]) if projection["active_work"] else "NONE"),
                  "COVERAGE: "+f"{len(projection['coverage']['observed'])}/{len(projection['coverage']['required'])}",
                  "BASIS: "+(", ".join(x["object_ref"] for x in projection["basis_set"]) if projection["basis_set"] else "NONE")]
    if scope: lines.insert(0,"SCOPE: "+_text(scope,"scope"))
    return {"schema":"cerebro-human-admin-status-view/v1","depth":depth,"scope":scope,"lines":lines,"text":"\n".join(lines),"projection_fingerprint":projection["projection_fingerprint"]}

def render_info(projection:Mapping[str,Any],query:str)->dict[str,Any]:
    resolved=resolve_ref(projection,query)
    if resolved["result"]!="RESOLVED":
        return {"schema":"cerebro-human-admin-info-view/v1","result":resolved["result"],"query":query,"candidate_refs":resolved.get("candidate_refs",[]),"text":resolved["result"]}
    obj=resolved["object"]; state=obj.get("state") or {}
    human=str(state.get("human_action") or state.get("next_human_gate") or "NONE")
    blocked=_collect_state_list([obj],("blockers","blocked_by","blocker"))
    lines=[f"{obj['canonical_ref']} [{obj['object_type']}]",
           "WHAT: "+(obj["human_summary"] or "UNKNOWN"),
           "WHY: "+(obj["why_it_matters"] or "UNKNOWN"),
           "NOW: "+obj["currentness"]+" / "+str(state.get("status") or state.get("lifecycle") or "UNKNOWN"),
           "OWNER: "+obj["owner_ref"],
           "BLOCKED_BY: "+(", ".join(blocked) if blocked else "NONE"),
           "HUMAN: "+human,
           "RELATED: "+(", ".join(obj["related_refs"]) if obj["related_refs"] else "NONE")]
    return {"schema":"cerebro-human-admin-info-view/v1","result":"RESOLVED","query":query,
            "canonical_ref":obj["canonical_ref"],"lines":lines,"text":"\n".join(lines),
            "evidence_refs":obj["evidence_refs"],"projection_fingerprint":projection["projection_fingerprint"]}
