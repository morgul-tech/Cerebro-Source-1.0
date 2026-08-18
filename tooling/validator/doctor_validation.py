#!/usr/bin/env python3
from __future__ import annotations
import importlib.util, json, tempfile, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path); mod=importlib.util.module_from_spec(spec); sys.modules[name]=mod; spec.loader.exec_module(mod); return mod
gate=load("doctor_gate_validation_subject",ROOT/"tooling"/"doctor"/"doctor_gate.py"); trust=load("doctor_trust_validation_subject",ROOT/"tooling"/"doctor"/"doctor_trust.py")
HEAD="63a546df16c649032caacb339d387d86c924a395"
def req():
    inst=[]
    for i,fam in enumerate(gate.GATE_FAMILIES,1): inst.append({"instance_id":f"G{i:02d}","gate_family":fam,"applicability":"REQUIRED","status":"PASS","evidence_sha256":format(i,"x")[-1]*64})
    return {"schema":gate.REQUEST_SCHEMA,"subject":{"subject_id":"DOCTOR-SELFTEST","source_pre_head":HEAD,"package_sha256":"1"*64,"touched_paths_sha256":"2"*64,"manifest_sha256":"3"*64,"operation":"SOURCE_PROMOTION","claim_scope":["SOURCE_PROMOTION"]},"basis":{"doctor_implementation_sha256":"4"*64,"runtime_baseline_sha256":"5"*64,"knowledge_basis_sha256":"6"*64,"failure_index_sha256":"7"*64,"gate_plan_sha256":"8"*64},"gate_instances":inst}
def blocked(fn,code):
    try: fn()
    except Exception as e:
        assert code in str(e),(code,str(e)); return
    raise AssertionError("EXPECTED_BLOCK:"+code)
def run():
    tests=[]
    x=req(); r=gate.evaluate(x); assert r["result"]=="PASS" and r["gate_family_cardinality"]==19 and r["unresolved_required_count"]==0; gate.validate_receipt(r); tests.append("finite-19-family-pass")
    y=req(); y["gate_instances"]=y["gate_instances"][:-1]; rr=gate.evaluate(y); assert rr["result"]=="BLOCK" and rr["unresolved_required_count"]==1; tests.append("missing-family-blocks")
    y=req(); y["gate_instances"][5]["status"]="FAIL"; rr=gate.evaluate(y); assert rr["result"]=="BLOCK" and rr["findings"][0]["status"]=="FAIL"; tests.append("gate-nonpass-blocks")
    y=json.loads(json.dumps(r)); y["subject"]["operation"]="TAMPERED"; blocked(lambda:gate.validate_receipt(y),"RECEIPT_FINGERPRINT_MISMATCH"); tests.append("receipt-tamper-denies")
    with tempfile.TemporaryDirectory() as d:
        d=Path(d); key=d/"key.bin"; key.write_bytes(b"A"*64); verifier=ROOT/"tooling"/"doctor"/"doctor_trust.py"; trustobj=trust.sign_receipt(receipt=r,key=key.read_bytes(),verifier_path=verifier,authority_epoch=2); trust.verify_trust_object(trust=trustobj,key=key.read_bytes(),verifier_path=verifier,expected_source_head=HEAD,expected_package_sha256="1"*64,expected_touched_paths_sha256="2"*64,expected_receipt_fingerprint=r["receipt_fingerprint"],expected_authority_epoch=2,required_claim_scope="SOURCE_PROMOTION"); tests.append("independent-trust-sign-verify")
        bad=json.loads(json.dumps(trustobj)); bad["attestation"]["signature"]="0"*64; blocked(lambda:trust.verify_trust_object(trust=bad,key=key.read_bytes(),verifier_path=verifier),"TRUST_SIGNATURE_INVALID"); tests.append("trust-tamper-denies")
        blocked(lambda:trust.verify_trust_object(trust=trustobj,key=key.read_bytes(),verifier_path=verifier,expected_source_head="0"*40),"SOURCE_HEAD_MISMATCH"); tests.append("stale-source-denies")
        blocked(lambda:trust.verify_trust_object(trust=trustobj,key=key.read_bytes(),verifier_path=verifier,required_claim_scope="OTHER"),"CLAIM_SCOPE_MISSING"); tests.append("claim-scope-denies")
    return {"result":"PASS","canaries":len(tests),"tests":tests}
if __name__=="__main__": print(json.dumps(run(),sort_keys=True))
