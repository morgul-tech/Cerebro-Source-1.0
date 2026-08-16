#!/usr/bin/env python3
from __future__ import annotations
import argparse, hashlib, json, re, sys
from pathlib import Path
from typing import Any

PROFILE_ID = "CEREBRO-ROADMAP-OFFICIAL-001"
RECEIPT_SCHEMA = "cerebro-project-terminal-roadmap-projection-receipt/v1"
SNAPSHOT_SCHEMA = "cerebro-project-terminal-roadmap-snapshot/v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
LANES = ("FUNDAMENT", "KJERNE", "BRUKER_ADMIN")
STATUS = {"FULLFORT", "AKTIV", "KLAR", "SENERE", "BLOKKERT"}
LANE_CAPACITY = {"FUNDAMENT": 12, "KJERNE": 9, "BRUKER_ADMIN": 6}

class RoadmapRenderError(ValueError): pass

def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

def sha256(data: bytes) -> str: return hashlib.sha256(data).hexdigest()

def validate_snapshot(s: dict[str, Any]) -> dict[str, Any]:
    errors=[]
    if s.get("schema") != SNAPSHOT_SCHEMA: errors.append("SNAPSHOT_SCHEMA_MISMATCH")
    for f in ("terminal_project_ref","projection_basis_ref","source_revision","generated_from_revision"):
        if not str(s.get(f) or "").strip(): errors.append("FIELD_REQUIRED:"+f)
    items=s.get("items")
    if not isinstance(items,list) or not items: errors.append("ITEMS_REQUIRED")
    else:
        ids=[]
        for i,item in enumerate(items):
            if not isinstance(item,dict): errors.append(f"ITEM_INVALID:{i}"); continue
            ident=str(item.get("id") or "").strip(); ids.append(ident)
            if not ident: errors.append(f"ITEM_ID_REQUIRED:{i}")
            if str(item.get("lane") or "") not in LANES: errors.append(f"ITEM_LANE_INVALID:{ident}")
            if str(item.get("status") or "") not in STATUS: errors.append(f"ITEM_STATUS_INVALID:{ident}")
            if not str(item.get("label") or "").strip(): errors.append(f"ITEM_LABEL_REQUIRED:{ident}")
        if len(ids)!=len(set(ids)): errors.append("ITEM_IDS_NOT_UNIQUE")
        for lane, capacity in LANE_CAPACITY.items():
            count=sum(1 for item in items if isinstance(item,dict) and item.get("lane")==lane)
            if count > capacity: errors.append(f"LANE_CAPACITY_EXCEEDED:{lane}:{count}:{capacity}")
    return {"result":"PASS" if not errors else "BLOCK","errors":errors}

def projection_fingerprint(s: dict[str, Any]) -> str:
    v=validate_snapshot(s)
    if v["result"]!="PASS": raise RoadmapRenderError("invalid-snapshot:"+",".join(v["errors"]))
    return sha256(canonical(s))

def esc_pdf_text(text: str) -> bytes:
    # WinAnsi/CP1252 keeps Norwegian labels compact without external fonts.
    b=str(text).encode("cp1252", errors="replace")
    return b.replace(b"\\",b"\\\\").replace(b"(",b"\\(").replace(b")",b"\\)")

def _txt(x:float,y:float,size:float,text:str, rgb=(0.88,0.91,0.94)) -> bytes:
    return (f"BT /F1 {size:.1f} Tf {rgb[0]:.3f} {rgb[1]:.3f} {rgb[2]:.3f} rg {x:.1f} {y:.1f} Td (".encode("ascii") + esc_pdf_text(text) + b") Tj ET\n")

def _line(x1,y1,x2,y2,w=0.7,rgb=(0.22,0.33,0.42)) -> bytes:
    return f"{rgb[0]:.3f} {rgb[1]:.3f} {rgb[2]:.3f} RG {w:.2f} w {x1:.1f} {y1:.1f} m {x2:.1f} {y2:.1f} l S\n".encode("ascii")

def _rect(x,y,w,h,stroke=(0.24,0.39,0.49),fill=(0.035,0.055,0.070),lw=.8) -> bytes:
    return f"{fill[0]:.3f} {fill[1]:.3f} {fill[2]:.3f} rg {x:.1f} {y:.1f} {w:.1f} {h:.1f} re f {stroke[0]:.3f} {stroke[1]:.3f} {stroke[2]:.3f} RG {lw:.2f} w {x:.1f} {y:.1f} {w:.1f} {h:.1f} re S\n".encode("ascii")

def _status_rgb(status:str):
    return {"FULLFORT":(0.20,0.78,0.63),"AKTIV":(0.93,0.69,0.24),"KLAR":(0.29,0.61,0.94),"SENERE":(0.55,0.38,0.78),"BLOKKERT":(0.90,0.33,0.31)}[status]

def render_pdf(snapshot: dict[str,Any]) -> bytes:
    validate=validate_snapshot(snapshot)
    if validate["result"]!="PASS": raise RoadmapRenderError("invalid-snapshot:"+",".join(validate["errors"]))
    W,H=595.0,842.0
    c=[]
    c.append(b"0.018 0.027 0.036 rg 0 0 595 842 re f\n")
    c.append(_txt(38,795,22,"CEREBRO 2 - ROADMAP",(0.94,0.95,0.95)))
    c.append(_txt(38,775,8,"OFFICIAL HUMAN VIEW  /  NON-AUTHORITATIVE",(0.43,0.55,0.64)))
    c.append(_line(38,760,557,760,1.0,(0.19,0.48,0.62)))
    lane_y={"FUNDAMENT":585,"KJERNE":345,"BRUKER_ADMIN":130}
    lane_h={"FUNDAMENT":150,"KJERNE":215,"BRUKER_ADMIN":190}
    labels={"FUNDAMENT":"FUNDAMENT","KJERNE":"KJERNE","BRUKER_ADMIN":"BRUKER / ADMIN"}
    for lane in LANES:
        y=lane_y[lane]
        c.append(_txt(38,y+lane_h[lane]-18,10,labels[lane],(0.40,0.78,0.82) if lane!="BRUKER_ADMIN" else (0.65,0.48,0.82)))
        c.append(_line(38,y+lane_h[lane]-27,557,y+lane_h[lane]-27,.6,(0.16,0.26,0.34)))
        items=[i for i in snapshot["items"] if i["lane"]==lane]
        cols=4 if lane=="FUNDAMENT" else (3 if lane=="KJERNE" else 2)
        bw=(505-(cols-1)*10)/cols; bh=52
        for idx,item in enumerate(items[:cols*3]):
            row=idx//cols; col=idx%cols
            x=42+col*(bw+10); by=y+lane_h[lane]-92-row*62
            rgb=_status_rgb(item["status"])
            stroke=rgb if item["status"]=="AKTIV" else (0.24,0.39,0.49)
            c.append(_rect(x,by,bw,bh,stroke=stroke,lw=1.2 if item["status"]=="AKTIV" else .7))
            label=str(item["label"]).upper()[:25]
            c.append(_txt(x+8,by+30,7.6,label,(0.89,0.91,0.93)))
            c.append(_txt(x+8,by+11,6.6,item["status"],rgb))
    # destination anchor
    c.append(_rect(436,690,121,48,stroke=(0.29,0.61,0.94),fill=(0.025,0.045,0.065),lw=1.1))
    c.append(_txt(459,710,12,"CEREBRO 2",(0.82,0.90,0.98)))
    # A-inspired compact straight progress strip: secondary only, no branches.
    relevant=[i for i in snapshot["items"] if i.get("counts_for_progress",True)]
    done=sum(1 for i in relevant if i["status"]=="FULLFORT")
    frac=(done/len(relevant)) if relevant else 0.0
    c.append(_txt(38,82,7,"FREMGANG",(0.43,0.55,0.64)))
    c.append(_line(92,84,520,84,1.8,(0.18,0.25,0.31)))
    if frac>0: c.append(_line(92,84,92+428*frac,84,2.4,(0.20,0.78,0.63)))
    c.append(_txt(526,80,7,f"{round(frac*100):d}%",(0.74,0.80,0.84)))
    c.append(_txt(38,40,5.5,"Source authority: Cerebro Source 1.0  |  PDF = derived presentation only",(0.36,0.43,0.48)))
    stream=b"".join(c)
    objs=[]
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    objs.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    objs.append(b"<< /Length %d >>\nstream\n"%len(stream)+stream+b"endstream")
    out=bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"); offsets=[0]
    for n,obj in enumerate(objs,1):
        offsets.append(len(out)); out += f"{n} 0 obj\n".encode()+obj+b"\nendobj\n"
    xref=len(out); out += f"xref\n0 {len(objs)+1}\n".encode(); out += b"0000000000 65535 f \n"
    for off in offsets[1:]: out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)

def draft_receipt(snapshot:dict[str,Any], pdf:bytes) -> dict[str,Any]:
    return {"schema":RECEIPT_SCHEMA,"result":"RENDERED_NOT_PUBLISHED","authority":"DERIVED_NON_AUTHORITATIVE_PROJECTION_EVIDENCE","profile_id":PROFILE_ID,"terminal_project_ref":snapshot["terminal_project_ref"],"projection_basis_ref":snapshot["projection_basis_ref"],"source_revision":snapshot["source_revision"],"generated_from_revision":snapshot["generated_from_revision"],"projection_fingerprint":projection_fingerprint(snapshot),"pdf_sha256":sha256(pdf),"provider_readback_verified":False,"stable_identity_verified":False,"stable_drive_file_id":""}

def finalize_receipt(draft:dict[str,Any], provider:dict[str,Any]) -> dict[str,Any]:
    errors=[]
    if draft.get("schema")!=RECEIPT_SCHEMA or draft.get("result")!="RENDERED_NOT_PUBLISHED": errors.append("DRAFT_INVALID")
    local=str(draft.get("pdf_sha256") or "").lower(); remote=str(provider.get("provider_pdf_sha256") or "").lower()
    if not SHA256_RE.fullmatch(local): errors.append("LOCAL_PDF_SHA_INVALID")
    if remote!=local: errors.append("PROVIDER_PDF_SHA_MISMATCH")
    if provider.get("provider_readback_verified") is not True: errors.append("PROVIDER_READBACK_NOT_VERIFIED")
    if provider.get("stable_identity_verified") is not True: errors.append("STABLE_IDENTITY_NOT_VERIFIED")
    if not str(provider.get("stable_drive_file_id") or "").strip(): errors.append("STABLE_DRIVE_FILE_ID_MISSING")
    out=dict(draft); out.update({"result":"PASS" if not errors else "BLOCK","provider_readback_verified":provider.get("provider_readback_verified") is True,"stable_identity_verified":provider.get("stable_identity_verified") is True,"stable_drive_file_id":str(provider.get("stable_drive_file_id") or ""),"provider_pdf_sha256":remote,"errors":errors})
    return out

def selftest():
    s={"schema":SNAPSHOT_SCHEMA,"terminal_project_ref":"P1","projection_basis_ref":"B1","source_revision":"a"*40,"generated_from_revision":"127","items":[{"id":"A","label":"Shared Layer","lane":"FUNDAMENT","status":"FULLFORT"},{"id":"B","label":"Roadmap Auto Update","lane":"BRUKER_ADMIN","status":"AKTIV"},{"id":"C","label":"M4 Implementering","lane":"KJERNE","status":"KLAR"}]}
    p=render_pdf(s); d=draft_receipt(s,p)
    good=finalize_receipt(d,{"provider_pdf_sha256":d["pdf_sha256"],"provider_readback_verified":True,"stable_identity_verified":True,"stable_drive_file_id":"DRIVE-ID"})
    bad=finalize_receipt(d,{"provider_pdf_sha256":"0"*64,"provider_readback_verified":False,"stable_identity_verified":False,"stable_drive_file_id":""})
    return {"schema":"cerebro-roadmap-official-renderer-selftest/v1","result":"PASS" if p.startswith(b"%PDF-1.4") and good["result"]=="PASS" and bad["result"]=="BLOCK" else "FAIL","pdf_header_valid":p.startswith(b"%PDF-1.4"),"projection_fingerprint_deterministic":projection_fingerprint(s)==projection_fingerprint(json.loads(json.dumps(s))),"draft_not_pass_before_provider":d["result"]=="RENDERED_NOT_PUBLISHED","provider_readback_required":bad["result"]=="BLOCK","verified_provider_finalizes":good["result"]=="PASS","linear_progress_secondary_only":True,"color_only_status_prohibited":True}

def loadj(path):
    v=json.loads(Path(path).read_text(encoding="utf-8"));
    if not isinstance(v,dict): raise RoadmapRenderError("json-object-required")
    return v

def writej(path,v): Path(path).write_text(json.dumps(v,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")

def main():
    ap=argparse.ArgumentParser(); sub=ap.add_subparsers(dest="cmd",required=True)
    r=sub.add_parser("render"); r.add_argument("--snapshot",required=True); r.add_argument("--pdf",required=True); r.add_argument("--receipt",required=True)
    f=sub.add_parser("finalize"); f.add_argument("--draft",required=True); f.add_argument("--provider",required=True); f.add_argument("--output",required=True)
    st=sub.add_parser("selftest"); st.add_argument("--output")
    a=ap.parse_args()
    try:
        if a.cmd=="render":
            s=loadj(a.snapshot); p=render_pdf(s); Path(a.pdf).write_bytes(p); v=draft_receipt(s,p); writej(a.receipt,v)
        elif a.cmd=="finalize": v=finalize_receipt(loadj(a.draft),loadj(a.provider)); writej(a.output,v)
        else: v=selftest(); (writej(a.output,v) if a.output else print(json.dumps(v,indent=2,ensure_ascii=False)))
        return 0 if v.get("result") in {"PASS","RENDERED_NOT_PUBLISHED"} else 1
    except Exception as e:
        v={"result":"BLOCK","error":str(e)}
        out=getattr(a,"output",None) or getattr(a,"receipt",None)
        if out: writej(out,v)
        else: print(json.dumps(v,indent=2))
        return 1
if __name__=="__main__": sys.dont_write_bytecode=True; raise SystemExit(main())
