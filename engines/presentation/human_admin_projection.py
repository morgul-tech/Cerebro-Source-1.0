#!/usr/bin/env python3
from __future__ import annotations
import hashlib,json,re
from typing import Any,Mapping,Sequence

SCHEMA="cerebro-human-admin-projection/v1"
AUTHORITY="PRESENTATION_ONLY_NON_AUTHORITATIVE"
CURRENTNESS=("CURRENT","STALE","GAP","UNKNOWN","PROVISIONAL")
ROOM_PALETTE=(("🔵","BLUE"),("🟢","GREEN"),("🟣","PURPLE"),("🟠","ORANGE"),("🟡","YELLOW"),("🔴","RED"),("🟤","BROWN"),("⚪","WHITE"))
ROOM_NAME_MAX_LENGTH=48
_HEX40=re.compile(r"^[0-9a-f]{40}$")
_WS=re.compile(r"\s+")
_OPAQUE_WORK_REF=re.compile(
    r"(?i)(?:\bWORK_(?:CLAIMS?|PACKETS?):\d+\b|\bP\d{3,}(?:[-_][A-Z0-9]+)*\b|"
    r"\b(?:CLAIM|PACKET)[-:#\s]*\d+\b|\bPM-[A-Z0-9-]*P\d+[A-Z0-9-]*\b)"
)

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

def _semantic_label(value:Any,name:str)->str:
    value=_text(value,name)
    words=value.split()
    if not 1<=len(words)<=3 or _OPAQUE_WORK_REF.search(value):
        raise HumanAdminProjectionError(f"{name}:semantic-label-1-to-3-words-required")
    if any(not any(ch.isalpha() for ch in word) or any(not (ch.isalpha() or ch in "-&") for ch in word) for word in words):
        raise HumanAdminProjectionError(f"{name}:semantic-label-1-to-3-words-required")
    return value

def _human_label(objects:Sequence[Mapping[str,Any]])->str:
    labels=[]
    for obj in objects:
        if obj.get("currentness")!="CURRENT": continue
        state=obj.get("state") or {}
        if "human_label" in state:
            labels.append(_semantic_label(state.get("human_label"),"state.human_label"))
    labels=sorted(set(labels))
    if len(labels)==1: return labels[0]
    if len(labels)>1:
        raise HumanAdminProjectionError("human-label:missing-or-ambiguous")
    legacy=[]
    for obj in objects:
        if obj.get("currentness")!="CURRENT": continue
        state=obj.get("state") or {}
        value=state.get("human_cognitive_location")
        if not isinstance(value,str) or not value.strip(): continue
        try: legacy.append(_semantic_label(value,"state.human_cognitive_location"))
        except HumanAdminProjectionError: continue
    legacy=sorted(set(legacy))
    if len(legacy)!=1: raise HumanAdminProjectionError("human-label:missing-or-ambiguous")
    return legacy[0]

def _waiting_flag(value:Any)->bool:
    if isinstance(value,bool): return value
    if isinstance(value,str):
        normalized=value.strip().upper()
        if normalized in {"WAITING","WAITING_INPUT","VENTER","TRUE"}: return True
        if normalized in {"NONE","FALSE","ACTIVE",""}: return False
    if isinstance(value,Mapping) and isinstance(value.get("active"),bool): return bool(value.get("active"))
    raise HumanAdminProjectionError("state.waiting_input:boolean-or-waiting-state-required")

def _human_dependency(value:Any,name:str)->str:
    value=_text(value,name)
    if "\n" in value or "\r" in value or _OPAQUE_WORK_REF.search(value):
        raise HumanAdminProjectionError(f"{name}:human-readable-text-required")
    return value

def _waiting_input(objects:Sequence[Mapping[str,Any]])->dict[str,Any]:
    waiting=[]
    for obj in objects:
        if obj.get("currentness")!="CURRENT": continue
        state=obj.get("state") or {}
        if "waiting_input" in state and _waiting_flag(state.get("waiting_input")):
            waiting.append(state)
    if not waiting:
        return {"active":False,"next_owner_label":None,"dependency_labels":[],"human_action_required":False}
    owners=sorted({_semantic_label(state.get("next_owner_label"),"state.next_owner_label") for state in waiting})
    if len(owners)!=1: raise HumanAdminProjectionError("waiting-input:next-owner-missing-or-ambiguous")
    dependencies=[]
    for state in waiting:
        raw=state.get("dependency_labels")
        values=[raw] if isinstance(raw,str) else raw
        if not isinstance(values,list) or not values:
            raise HumanAdminProjectionError("waiting-input:dependency-labels-required")
        dependencies.extend(_human_dependency(value,"state.dependency_labels") for value in values)
    dependencies=sorted(set(dependencies))
    if not dependencies: raise HumanAdminProjectionError("waiting-input:dependency-labels-required")
    return {"active":True,"next_owner_label":owners[0],"dependency_labels":dependencies,"human_action_required":False}

def _public_text(value:Any)->str:
    return _WS.sub(" ",_OPAQUE_WORK_REF.sub("INTERNAL",str(value))).strip()

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

def _unique_state_value(objects:Sequence[Mapping[str,Any]],fields:Sequence[str],default:Any)->Any:
    values={}
    for obj in objects:
        if obj.get("currentness")!="CURRENT": continue
        state=obj.get("state") or {}
        for field in fields:
            if field not in state: continue
            value=state[field]
            if isinstance(value,(str,Mapping,bool)) and (not isinstance(value,str) or value.strip()):
                normalized=dict(value) if isinstance(value,Mapping) else value.strip() if isinstance(value,str) else value
                values[_canonical(normalized)]=normalized
                break
    return next(iter(values.values())) if len(values)==1 else default

def _room_identity(objects:Sequence[Mapping[str,Any]])->dict[str,Any]:
    observed=[]
    for obj in objects:
        if obj.get("currentness")!="CURRENT": continue
        state=obj.get("state") or {}
        room_id=state.get("room_id")
        case_id=state.get("case_container_id")
        room_name=state.get("room_name")
        room_id=room_id.strip() if isinstance(room_id,str) else ""
        case_id=case_id.strip() if isinstance(case_id,str) else ""
        room_name=room_name.strip() if isinstance(room_name,str) else ""
        if not room_id and not case_id:
            if room_name: raise HumanAdminProjectionError("room-identity:durable-id-required")
            continue
        if not room_name or "\n" in room_name or "\r" in room_name or len(room_name)>ROOM_NAME_MAX_LENGTH:
            raise HumanAdminProjectionError("room-identity:short-room-name-required")
        durable_id=room_id or case_id
        source="ROOM_ID" if room_id else "CASE_CONTAINER_ID"
        observed.append((durable_id,room_name,source))
    if not observed:
        return {"type":"ROOM_IDENTITY","active":False,"membership_semantics":"MEMBERSHIP_ONLY","source":"NONE",
                "durable_room_id":None,"room_name":None,"color_derivation":"SHA256_FIXED_PALETTE_AT_RENDER",
                "derived_color_storage":"NONE","encodes_status_or_authority":False}
    identities={(room_id,room_name) for room_id,room_name,_ in observed}
    if len(identities)!=1: raise HumanAdminProjectionError("room-identity:multiple-active-rooms-not-proven")
    durable_id,room_name=next(iter(identities))
    sources={source for _,_,source in observed}
    source=next(iter(sources)) if len(sources)==1 else "ROOM_ID_OR_CASE_CONTAINER_ID"
    return {"type":"ROOM_IDENTITY","active":True,"membership_semantics":"MEMBERSHIP_ONLY","source":source,
            "durable_room_id":durable_id,"room_name":room_name,"color_derivation":"SHA256_FIXED_PALETTE_AT_RENDER",
            "derived_color_storage":"NONE","encodes_status_or_authority":False}

def _render_room_identity(room_identity:Mapping[str,Any])->dict[str,Any]:
    if room_identity.get("active") is not True:
        return {"active":False,"membership_semantics":"MEMBERSHIP_ONLY","palette_marker":None,
                "palette_name":None,"textual_fallback":None,"header":None,"derived_at_render":True}
    durable_id=_text(room_identity.get("durable_room_id"),"room_identity.durable_room_id")
    room_name=_text(room_identity.get("room_name"),"room_identity.room_name")
    palette_marker,palette_name=ROOM_PALETTE[int(hashlib.sha256(durable_id.encode("utf-8")).hexdigest(),16)%len(ROOM_PALETTE)]
    fallback=f"ROM · {room_name}"
    return {"active":True,"membership_semantics":"MEMBERSHIP_ONLY","palette_marker":palette_marker,
            "palette_name":palette_name,"textual_fallback":fallback,"header":f"{palette_marker} {fallback}",
            "derived_at_render":True}

def _typed_hmi_signals(objects:Sequence[Mapping[str,Any]])->dict[str,dict[str,Any]]:
    role_raw=_unique_state_value(objects,("role_assessment","role"),{})
    role=dict(role_raw) if isinstance(role_raw,Mapping) else {"role":role_raw} if isinstance(role_raw,str) else {}
    role_name=str(role.get("role") or role.get("value") or "UNKNOWN").strip().upper()
    if role_name=="HA":
        role={"role":"HUMAN_ADMIN","presentation_shorthand":"HA","identity_authority":"NONE"}
    elif "role" not in role:
        role={"role":role_name}

    responsibility_raw=_unique_state_value(objects,("responsibility_assessment","responsibility"),{})
    responsibility=dict(responsibility_raw) if isinstance(responsibility_raw,Mapping) else {"owner":responsibility_raw} if isinstance(responsibility_raw,str) else {"owner":"UNKNOWN"}
    boundary_raw=_unique_state_value(objects,("human_boundary_assessment","human_boundary"),{})
    boundary=dict(boundary_raw) if isinstance(boundary_raw,Mapping) else {"real_human_action_is_next":boundary_raw} if isinstance(boundary_raw,bool) else {"real_human_action_is_next":False}
    request_raw=_unique_state_value(objects,("presentation_request",),{})
    request=dict(request_raw) if isinstance(request_raw,Mapping) else {"dialect":request_raw} if isinstance(request_raw,str) else {"dialect":"DEFAULT"}
    return {
        "role_assessment":role,
        "responsibility_assessment":responsibility,
        "human_boundary_assessment":boundary,
        "presentation_request":request,
        "hmi":{"surface_first":True,"machine_route_before_human_relay":True,"ha_expansion":"HUMAN_ADMIN",
               "actor_identity_mutation_allowed":False,"carrier_change_identity_effect":"NONE",
               "room_header_at_absolute_top":True,"room_color_membership_only":True,
               "derived_room_color_persisted":False,"human_label_primary":True,
               "opaque_ids_brief_standard":"HIDDEN","waiting_default_max_lines":3,
               "waiting_is_human_pulse":False},
    }

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
    room_identity=_room_identity(objects)
    human_label=_human_label(objects)
    waiting_input=_waiting_input(objects)
    typed_signals=_typed_hmi_signals(objects)
    boundary=typed_signals["human_boundary_assessment"]
    responsibility=typed_signals["responsibility_assessment"]
    if boundary.get("real_human_action_is_next") is True:
        human_gate=str(boundary.get("next_human_gate") or boundary.get("human_action") or (human_gate if human_gate not in {"NONE","UNKNOWN"} else "REAL_HUMAN_ACTION_REQUIRED"))
    elif str(responsibility.get("owner","")).upper() in {"MACHINE","CEREBRO","IMPLEMENTER"}:
        human_gate="NONE"
    if waiting_input["active"] and human_gate!="NONE":
        raise HumanAdminProjectionError("waiting-input:human-action-conflict")
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
        "blockers":blockers,"next_human_gate":human_gate,"active_work":active_work,"human_label":human_label,
        "waiting_input":waiting_input,"room_identity":room_identity,
        "role_assessment":typed_signals["role_assessment"],"responsibility_assessment":typed_signals["responsibility_assessment"],
        "human_boundary_assessment":typed_signals["human_boundary_assessment"],"presentation_request":typed_signals["presentation_request"],
        "hmi":typed_signals["hmi"],"objects":objects,"evidence_refs":evidence_refs,
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
    hmi=value.get("hmi") or {}
    if hmi.get("surface_first") is not True or hmi.get("machine_route_before_human_relay") is not True or hmi.get("ha_expansion")!="HUMAN_ADMIN" or hmi.get("actor_identity_mutation_allowed") is not False or hmi.get("room_header_at_absolute_top") is not True or hmi.get("room_color_membership_only") is not True or hmi.get("derived_room_color_persisted") is not False or hmi.get("human_label_primary") is not True or hmi.get("opaque_ids_brief_standard")!="HIDDEN" or hmi.get("waiting_default_max_lines")!=3 or hmi.get("waiting_is_human_pulse") is not False:
        raise HumanAdminProjectionError("projection:hmi-boundary-invalid")
    if value.get("human_label")!=_human_label(value.get("objects") or []):
        raise HumanAdminProjectionError("projection:human-label-invalid")
    waiting_input=value.get("waiting_input")
    if waiting_input!=_waiting_input(value.get("objects") or []):
        raise HumanAdminProjectionError("projection:waiting-input-invalid")
    if waiting_input.get("active") is True and value.get("next_human_gate")!="NONE":
        raise HumanAdminProjectionError("projection:waiting-input-human-action-conflict")
    room_identity=value.get("room_identity")
    if room_identity!=_room_identity(value.get("objects") or []):
        raise HumanAdminProjectionError("projection:room-identity-invalid")
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
    room=_render_room_identity(projection["room_identity"])
    waiting=projection["waiting_input"]
    if waiting["active"]:
        first=(room["header"]+" · " if room["active"] else "")+"VENTER"
        lines=[first,"Neste eier: "+waiting["next_owner_label"],
               "Jeg starter når: "+", ".join(waiting["dependency_labels"])]
        if depth=="deep":
            if scope: lines.append("SCOPE: "+_text(scope,"scope"))
            lines += ["AKTIVT: "+(", ".join(projection["active_work"]) if projection["active_work"] else "NONE"),
                      "COVERAGE: "+f"{len(projection['coverage']['observed'])}/{len(projection['coverage']['required'])}",
                      "BASIS: "+(", ".join(x["object_ref"] for x in projection["basis_set"]) if projection["basis_set"] else "NONE")]
        return {"schema":"cerebro-human-admin-status-view/v1","depth":depth,"scope":scope,"room_identity":room,"lines":lines,"text":"\n".join(lines),"projection_fingerprint":projection["projection_fingerprint"]}
    lines=[room["header"]] if room["active"] else []
    if scope and depth=="deep": lines.append("SCOPE: "+_text(scope,"scope"))
    public=(lambda value: str(value) if depth=="deep" else _public_text(value))
    lines += [prefix+projection["human_label"],"HER ER VI: "+public(projection["orientation"]["where_we_are"]),"MAL: "+public(projection["current_objective"])]
    if depth!="brief":
        lines += ["VEIEN VIDERE: "+public(projection["orientation"]["where_we_are_going"]),
                  "BLOKKERE: "+(", ".join(public(x) for x in projection["blockers"]) if projection["blockers"] else "NONE"),
                  "HUMAN: "+projection["next_human_gate"],
                  "RETURN: "+(", ".join(public(x) for x in projection["return_points"]) if projection["return_points"] else "UNKNOWN")]
    if depth=="deep":
        lines += ["AKTIVT: "+(", ".join(projection["active_work"]) if projection["active_work"] else "NONE"),
                  "COVERAGE: "+f"{len(projection['coverage']['observed'])}/{len(projection['coverage']['required'])}",
                  "BASIS: "+(", ".join(x["object_ref"] for x in projection["basis_set"]) if projection["basis_set"] else "NONE")]
    return {"schema":"cerebro-human-admin-status-view/v1","depth":depth,"scope":scope,"room_identity":room,"lines":lines,"text":"\n".join(lines),"projection_fingerprint":projection["projection_fingerprint"]}

def render_info(projection:Mapping[str,Any],query:str)->dict[str,Any]:
    validate_projection(projection)
    room=_render_room_identity(projection["room_identity"])
    room_lines=[room["header"]] if room["active"] else []
    resolved=resolve_ref(projection,query)
    if resolved["result"]!="RESOLVED":
        lines=room_lines+[resolved["result"]]
        return {"schema":"cerebro-human-admin-info-view/v1","result":resolved["result"],"query":query,"candidate_refs":resolved.get("candidate_refs",[]),"room_identity":room,"lines":lines,"text":"\n".join(lines)}
    obj=resolved["object"]; state=obj.get("state") or {}
    human=str(state.get("human_action") or state.get("next_human_gate") or "NONE")
    blocked=_collect_state_list([obj],("blockers","blocked_by","blocker"))
    lines=room_lines+[f"{obj['canonical_ref']} [{obj['object_type']}]",
           "WHAT: "+(obj["human_summary"] or "UNKNOWN"),
           "WHY: "+(obj["why_it_matters"] or "UNKNOWN"),
           "NOW: "+obj["currentness"]+" / "+str(state.get("status") or state.get("lifecycle") or "UNKNOWN"),
           "OWNER: "+obj["owner_ref"],
           "BLOCKED_BY: "+(", ".join(blocked) if blocked else "NONE"),
           "HUMAN: "+human,
           "RELATED: "+(", ".join(obj["related_refs"]) if obj["related_refs"] else "NONE")]
    return {"schema":"cerebro-human-admin-info-view/v1","result":"RESOLVED","query":query,
            "canonical_ref":obj["canonical_ref"],"room_identity":room,"lines":lines,"text":"\n".join(lines),
            "evidence_refs":obj["evidence_refs"],"projection_fingerprint":projection["projection_fingerprint"]}
