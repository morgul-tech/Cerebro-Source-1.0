#!/usr/bin/env python3
from __future__ import annotations
import argparse, hashlib, hmac, json, re
from pathlib import Path
from typing import Any
try:
    from . import doctor_gate
except ImportError:
    import importlib.util, sys
    here=Path(__file__).resolve().parent; spec=importlib.util.spec_from_file_location("doctor_gate",here/"doctor_gate.py"); doctor_gate=importlib.util.module_from_spec(spec); sys.modules[spec.name]=doctor_gate; spec.loader.exec_module(doctor_gate)
TRUST_OBJECT_SCHEMA="cerebro-doctor-trust-object/v1"; ATTESTATION_SCHEMA="cerebro-doctor-trust-attestation/v1"
HEX40=re.compile(r"^[0-9a-f]{40}$"); HEX64=re.compile(r"^[0-9a-f]{64}$")
class DoctorTrustError(RuntimeError): pass
def canonical(obj:Any)->bytes: return json.dumps(obj,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode("utf-8")
def sha256_hex(data:bytes)->str: return hashlib.sha256(data).hexdigest()
def file_sha256(path:str|Path)->str: return sha256_hex(Path(path).read_bytes())
def _validate_hex(value:Any,regex:re.Pattern[str],field:str)->str:
    v=str(value or "")
    if not regex.fullmatch(v): raise DoctorTrustError("INVALID:"+field)
    return v
def _semantic_core(obj:dict[str,Any])->dict[str,Any]:
    return {k:v for k,v in obj.items() if k not in {"attestation","trust_object_fingerprint"}}
def _attestation_unsigned(att:dict[str,Any])->dict[str,Any]:
    x=dict(att); x.pop("signature",None); return x
def _fingerprint_payload(obj:dict[str,Any])->dict[str,Any]:
    x=dict(obj); x.pop("trust_object_fingerprint",None); return x

def sign_receipt(*,receipt:dict[str,Any],key:bytes,verifier_path:str|Path,authority_epoch:int)->dict[str,Any]:
    doctor_gate.validate_receipt(receipt)
    if receipt.get("result")!="PASS": raise DoctorTrustError("RECEIPT_NOT_PASS")
    if len(key)<32: raise DoctorTrustError("TRUST_KEY_TOO_SHORT")
    if authority_epoch<1: raise DoctorTrustError("AUTHORITY_EPOCH_INVALID")
    subject=receipt.get("subject") or {}; source_head=_validate_hex(subject.get("source_pre_head"),HEX40,"source_pre_head"); package_sha=_validate_hex(subject.get("package_sha256"),HEX64,"package_sha256"); paths_sha=_validate_hex(subject.get("touched_paths_sha256"),HEX64,"touched_paths_sha256"); manifest_sha=_validate_hex(subject.get("manifest_sha256"),HEX64,"manifest_sha256")
    claim_scope=subject.get("claim_scope")
    if not isinstance(claim_scope,list) or not claim_scope: raise DoctorTrustError("CLAIM_SCOPE_INVALID")
    verifier_sha=file_sha256(verifier_path); key_sha=sha256_hex(key)
    obj:dict[str,Any]={
      "schema":TRUST_OBJECT_SCHEMA,"state":"CERTIFIED_CURRENT","receipt_fingerprint":receipt["receipt_fingerprint"],
      "subject":{"subject_id":str(subject.get("subject_id") or ""),"source_pre_head":source_head,"package_sha256":package_sha,"touched_paths_sha256":paths_sha,"manifest_sha256":manifest_sha,"operation":str(subject.get("operation") or ""),"claim_scope":[str(x) for x in claim_scope]},
      "currentness":{"schema":"cerebro-doctor-currentness-vector/v1","source_pre_head":source_head,"package_sha256":package_sha,"touched_paths_sha256":paths_sha,"manifest_sha256":manifest_sha,"doctor_implementation_sha256":str((receipt.get("basis") or {}).get("doctor_implementation_sha256") or "")},
      "authority_epoch":authority_epoch,"claim_scope":[str(x) for x in claim_scope],"authority":"TRUST_EVIDENCE_ONLY_MCP_CONTROL_REQUIRED"
    }
    att={"schema":ATTESTATION_SCHEMA,"verifier_id":"CEREBRO-DOCTOR-TRUST-VERIFIER-001","implementation_sha256":verifier_sha,"key_sha256":key_sha,"algorithm":"HMAC-SHA256","validation_result":"PASS","receipt_fingerprint":receipt["receipt_fingerprint"]}
    att["signature"]=hmac.new(key,canonical({"trust":_semantic_core(obj),"attestation":_attestation_unsigned(att)}),hashlib.sha256).hexdigest()
    obj["attestation"]=att
    obj["trust_object_fingerprint"]=sha256_hex(canonical(_fingerprint_payload(obj)))
    return obj

def verify_trust_object(*,trust:dict[str,Any],key:bytes,verifier_path:str|Path,expected_source_head:str|None=None,expected_package_sha256:str|None=None,expected_touched_paths_sha256:str|None=None,expected_receipt_fingerprint:str|None=None,expected_authority_epoch:int|None=None,required_claim_scope:str|None=None)->dict[str,Any]:
    if trust.get("schema")!=TRUST_OBJECT_SCHEMA or trust.get("state")!="CERTIFIED_CURRENT": raise DoctorTrustError("TRUST_OBJECT_STATE_INVALID")
    if len(key)<32: raise DoctorTrustError("TRUST_KEY_TOO_SHORT")
    att=trust.get("attestation") or {}
    if att.get("schema")!=ATTESTATION_SCHEMA or att.get("algorithm")!="HMAC-SHA256" or att.get("validation_result")!="PASS": raise DoctorTrustError("ATTESTATION_CONTRACT_INVALID")
    if att.get("implementation_sha256")!=file_sha256(verifier_path): raise DoctorTrustError("VERIFIER_IDENTITY_MISMATCH")
    if att.get("key_sha256")!=sha256_hex(key): raise DoctorTrustError("TRUST_KEY_IDENTITY_MISMATCH")
    if att.get("receipt_fingerprint")!=trust.get("receipt_fingerprint"): raise DoctorTrustError("ATTESTATION_RECEIPT_MISMATCH")
    sig=_validate_hex(att.get("signature"),HEX64,"attestation.signature")
    expected_sig=hmac.new(key,canonical({"trust":_semantic_core(trust),"attestation":_attestation_unsigned(att)}),hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig,expected_sig): raise DoctorTrustError("TRUST_SIGNATURE_INVALID")
    fp=_validate_hex(trust.get("trust_object_fingerprint"),HEX64,"trust_object_fingerprint")
    if fp!=sha256_hex(canonical(_fingerprint_payload(trust))): raise DoctorTrustError("TRUST_OBJECT_FINGERPRINT_MISMATCH")
    subject=trust.get("subject") or {}
    for expected,actual,code in [(expected_source_head,subject.get("source_pre_head"),"SOURCE_HEAD_MISMATCH"),(expected_package_sha256,subject.get("package_sha256"),"PACKAGE_SHA256_MISMATCH"),(expected_touched_paths_sha256,subject.get("touched_paths_sha256"),"TOUCHED_PATHS_SHA256_MISMATCH"),(expected_receipt_fingerprint,trust.get("receipt_fingerprint"),"RECEIPT_FINGERPRINT_MISMATCH")]:
        if expected is not None and str(expected)!=str(actual): raise DoctorTrustError(code)
    if expected_authority_epoch is not None and int(trust.get("authority_epoch") or 0)!=int(expected_authority_epoch): raise DoctorTrustError("AUTHORITY_EPOCH_MISMATCH")
    if required_claim_scope is not None and required_claim_scope not in list(trust.get("claim_scope") or []): raise DoctorTrustError("CLAIM_SCOPE_MISSING:"+required_claim_scope)
    return trust

def main()->int:
    p=argparse.ArgumentParser(); sub=p.add_subparsers(dest="cmd",required=True)
    s=sub.add_parser("sign"); s.add_argument("--receipt",required=True); s.add_argument("--key",required=True); s.add_argument("--verifier-path"); s.add_argument("--authority-epoch",type=int,required=True); s.add_argument("--out",required=True)
    v=sub.add_parser("verify"); v.add_argument("--trust-object",required=True); v.add_argument("--key",required=True); v.add_argument("--verifier-path"); v.add_argument("--source-head"); v.add_argument("--package-sha256"); v.add_argument("--touched-paths-sha256"); v.add_argument("--receipt-fingerprint"); v.add_argument("--authority-epoch",type=int); v.add_argument("--required-claim-scope")
    args=p.parse_args(); verifier_path=args.verifier_path or __file__
    try:
        key=Path(args.key).read_bytes()
        if args.cmd=="sign":
            receipt=json.loads(Path(args.receipt).read_text(encoding="utf-8")); out=sign_receipt(receipt=receipt,key=key,verifier_path=verifier_path,authority_epoch=args.authority_epoch); Path(args.out).parent.mkdir(parents=True,exist_ok=True); Path(args.out).write_text(json.dumps(out,sort_keys=True,separators=(",",":"))+"\n",encoding="utf-8",newline="\n"); print(json.dumps({"result":"PASS","trust_object_fingerprint":out["trust_object_fingerprint"]},sort_keys=True)); return 0
        trust=json.loads(Path(args.trust_object).read_text(encoding="utf-8")); verify_trust_object(trust=trust,key=key,verifier_path=verifier_path,expected_source_head=args.source_head,expected_package_sha256=args.package_sha256,expected_touched_paths_sha256=args.touched_paths_sha256,expected_receipt_fingerprint=args.receipt_fingerprint,expected_authority_epoch=args.authority_epoch,required_claim_scope=args.required_claim_scope); print(json.dumps({"result":"PASS","trust_object_fingerprint":trust["trust_object_fingerprint"]},sort_keys=True)); return 0
    except (OSError,ValueError,DoctorTrustError,doctor_gate.DoctorGateError) as exc:
        print(json.dumps({"result":"BLOCK","reason":str(exc)},sort_keys=True)); return 3
if __name__=="__main__": raise SystemExit(main())
