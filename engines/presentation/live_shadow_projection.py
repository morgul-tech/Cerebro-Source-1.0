#!/usr/bin/env python3
from __future__ import annotations
import hashlib,json,re
from typing import Any,Mapping
SCHEMA="cerebro-native-live-shadow-projection/v1"
AUTHORITY="PRESENTATION_ONLY_NON_AUTHORITATIVE"
STATES=("CURRENT","STALE","GAP","UNKNOWN","PROVISIONAL")
DELIVERY_STATES=("OBSERVED","NO_RESPONSE","REJECTED","FAIL")
HEX64=re.compile(r"^[0-9a-f]{64}$")
class LiveShadowError(ValueError): pass

def _text(v:Any,n:str)->str:
    if not isinstance(v,str) or not v.strip(): raise LiveShadowError(n+":nonempty-string-required")
    return v.strip()
def _sha(v:Any,n:str)->str:
    t=_text(v,n).lower()
    if not HEX64.fullmatch(t): raise LiveShadowError(n+":sha256-required")
    return t
def _canonical(v:Any)->bytes:
    return json.dumps(v,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode("utf-8")
def fingerprint(v:Any)->str: return hashlib.sha256(_canonical(v)).hexdigest()

def normalize_basis_set(value:Any)->list[dict[str,Any]]:
    if not isinstance(value,list) or not value: raise LiveShadowError("basis_set:nonempty-list-required")
    out=[]; seen=set()
    for i,item in enumerate(value):
        if not isinstance(item,Mapping): raise LiveShadowError(f"basis_set[{i}]:object-required")
        owner=_text(item.get("owner_ref"),f"basis_set[{i}].owner_ref")
        stream=_text(item.get("stream_ref"),f"basis_set[{i}].stream_ref")
        rev=item.get("revision")
        if not isinstance(rev,int) or isinstance(rev,bool) or rev<0: raise LiveShadowError(f"basis_set[{i}].revision:nonnegative-int-required")
        cur=_text(item.get("currentness"),f"basis_set[{i}].currentness").upper()
        if cur not in STATES: raise LiveShadowError(f"basis_set[{i}].currentness:invalid")
        row={"owner_ref":owner,"stream_ref":stream,"revision":rev,"fingerprint":_sha(item.get("fingerprint"),f"basis_set[{i}].fingerprint"),"currentness":cur}
        key=(owner,stream)
        if key in seen: raise LiveShadowError("basis_set:duplicate-owner-stream")
        seen.add(key); out.append(row)
    return sorted(out,key=lambda x:(x["owner_ref"],x["stream_ref"]))

def derive_projection_state(basis_set:list[dict[str,Any]])->str:
    states={x["currentness"] for x in basis_set}
    for state in ("GAP","UNKNOWN","STALE","PROVISIONAL"):
        if state in states: return state
    return "CURRENT"

def build_projection(*,stream_ref:str,stream_revision:int,runtime_evidence:Mapping[str,Any],basis_set:Any,delivery_state:str="OBSERVED")->dict[str,Any]:
    stream_ref=_text(stream_ref,"stream_ref")
    if not isinstance(stream_revision,int) or isinstance(stream_revision,bool) or stream_revision<0: raise LiveShadowError("stream_revision:nonnegative-int-required")
    if not isinstance(runtime_evidence,Mapping): raise LiveShadowError("runtime_evidence:object-required")
    ev={"ref":_text(runtime_evidence.get("ref"),"runtime_evidence.ref"),"fingerprint":_sha(runtime_evidence.get("fingerprint"),"runtime_evidence.fingerprint"),"schema":_text(runtime_evidence.get("schema"),"runtime_evidence.schema")}
    ds=_text(delivery_state,"delivery_state").upper()
    if ds not in DELIVERY_STATES: raise LiveShadowError("delivery_state:invalid")
    basis=normalize_basis_set(basis_set)
    content={"schema":SCHEMA,"authority":AUTHORITY,"projection_state":derive_projection_state(basis),"stream_ref":stream_ref,"stream_revision":stream_revision,"runtime_evidence":ev,"basis_set":basis,"delivery_state":ds}
    return {**content,"projection_fingerprint":fingerprint(content)}

def validate_projection(value:Mapping[str,Any])->dict[str,Any]:
    if not isinstance(value,Mapping): raise LiveShadowError("projection:object-required")
    expected=build_projection(stream_ref=value.get("stream_ref"),stream_revision=value.get("stream_revision"),runtime_evidence=value.get("runtime_evidence") or {},basis_set=value.get("basis_set"),delivery_state=value.get("delivery_state"))
    if value.get("schema")!=SCHEMA or value.get("authority")!=AUTHORITY: raise LiveShadowError("projection:identity-or-authority-mismatch")
    if value.get("projection_state")!=expected["projection_state"]: raise LiveShadowError("projection_state:mismatch")
    if value.get("projection_fingerprint")!=expected["projection_fingerprint"]: raise LiveShadowError("projection_fingerprint:mismatch")
    return expected

def reconcile(previous:Mapping[str,Any]|None,incoming:Mapping[str,Any])->dict[str,Any]:
    cur=validate_projection(incoming)
    if previous is None: return {"result":"ACCEPT","effect":"FIRST_OBSERVATION","projection":cur}
    prev=validate_projection(previous)
    if cur["stream_ref"]!=prev["stream_ref"]: raise LiveShadowError("stream_ref:mismatch")
    if cur["stream_revision"]<prev["stream_revision"]: return {"result":"BLOCK","classification":"STALE_OR_OUT_OF_ORDER"}
    if cur["stream_revision"]==prev["stream_revision"]:
        if cur["projection_fingerprint"]==prev["projection_fingerprint"]: return {"result":"PASS","effect":"IDEMPOTENT_NO_EFFECT","projection":prev}
        return {"result":"BLOCK","classification":"SAME_REVISION_FINGERPRINT_COLLISION"}
    return {"result":"ACCEPT","effect":"ADVANCE_READ_ONLY_PROJECTION","projection":cur}
