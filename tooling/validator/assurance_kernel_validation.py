#!/usr/bin/env python3
from __future__ import annotations
import importlib.util, json, tempfile, sys, threading
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path); mod=importlib.util.module_from_spec(spec); sys.modules[name]=mod; spec.loader.exec_module(mod); return mod
mod=load("assurance_kernel",ROOT/"tooling"/"assurance"/"assurance_kernel.py")
gate=load("doctor_gate",ROOT/"tooling"/"doctor"/"doctor_gate.py")
trust=load("doctor_trust",ROOT/"tooling"/"doctor"/"doctor_trust.py")
BASE_HEAD="63a546df16c649032caacb339d387d86c924a395"; P="1"*64; T="2"*64

def permit(**overrides):
    x={"schema":mod.PERMIT_SCHEMA,"permit_id":"P1","campaign_id":"BOOT23_ASSURANCE_KERNEL_BOOTSTRAP_001","package_class":mod.BOOTSTRAP_PACKAGE_CLASS,"source_pre_head":BASE_HEAD,"package_sha256":P,"touched_paths_sha256":T,"nonce":"0123456789abcdef","authority_epoch":1}; x.update(overrides); return x

def intent(**overrides):
    x=dict(source_head=BASE_HEAD,package_sha256=P,touched_paths_sha256=T,package_class=mod.BOOTSTRAP_PACKAGE_CLASS,campaign_id="BOOT23_ASSURANCE_KERNEL_BOOTSTRAP_001",authority_epoch=1); x.update(overrides); return mod.MaterialIntent(**x)

def denied(fn,code):
    try: fn()
    except mod.AssuranceDenied as e:
        assert code in str(e),(code,str(e)); return
    raise AssertionError("EXPECTED_DENY:"+code)

def pass_request(source_head,package_sha,paths_sha,epoch):
    inst=[]
    for i,fam in enumerate(gate.GATE_FAMILIES,1): inst.append({"instance_id":f"I{i:02d}","gate_family":fam,"applicability":"REQUIRED","status":"PASS","evidence_sha256":str(i%10)*64})
    return {"schema":gate.REQUEST_SCHEMA,"subject":{"subject_id":"TEST","source_pre_head":source_head,"package_sha256":package_sha,"touched_paths_sha256":paths_sha,"manifest_sha256":"3"*64,"operation":"SOURCE_PROMOTION","claim_scope":["SOURCE_PROMOTION"]},"basis":{"doctor_implementation_sha256":"4"*64,"runtime_baseline_sha256":"5"*64,"knowledge_basis_sha256":"6"*64,"failure_index_sha256":"7"*64,"gate_plan_sha256":"8"*64},"gate_instances":inst}

def run():
    tests=[]
    def ok(name,fn): fn(); tests.append(name)
    with tempfile.TemporaryDirectory() as d:
        root=Path(d); state=root/"state.json"; key=root/"key.bin"; verifier=root/"doctor_trust.py"; key.write_bytes(b"K"*64); verifier.write_bytes((ROOT/"tooling"/"doctor"/"doctor_trust.py").read_bytes())
        k=mod.AssuranceKernel(state)
        ok("no-kernel-state-denies",lambda:denied(lambda:k.check(permit(),intent()),"KERNEL_NOT_ENFORCING"))
        k.initialize_bootstrap(external_anchor_proof="external-anchor-proof-0123456789abcdef")
        ok("stale-head-denies",lambda:denied(lambda:k.check(permit(),intent(source_head="0"*40)),"BINDING_MISMATCH:source_pre_head"))
        ok("wrong-package-denies",lambda:denied(lambda:k.check(permit(),intent(package_sha256="3"*64)),"BINDING_MISMATCH:package_sha256"))
        ok("wrong-path-set-denies",lambda:denied(lambda:k.check(permit(),intent(touched_paths_sha256="4"*64)),"BINDING_MISMATCH:touched_paths_sha256"))
        ok("wrong-package-class-denies",lambda:denied(lambda:k.check(permit(),intent(package_class="ORDINARY_SOURCE_PACKAGE")),"BINDING_MISMATCH:package_class"))
        r=k.consume(permit(),intent()); assert r["result"]=="ALLOW"; tests.append("exact-bootstrap-consume-allows")
        ok("permit-replay-denies",lambda:denied(lambda:k.consume(permit(),intent()),"PERMIT_REPLAY"))
        ok("second-bootstrap-package-denies",lambda:denied(lambda:k.consume(permit(permit_id="P2",nonce="fedcba9876543210"),intent()),"BOOTSTRAP_CONSUMPTION_EXHAUSTED"))
        ok("doctor-transition-requires-active-path-proof",lambda:denied(lambda:k.transition_doctor_enforced(active_path_proof_sha256="bad",expected_epoch=1,trust_key_path=str(key),doctor_verifier_path=str(verifier)),"DOCTOR_ACTIVE_PATH_PROOF_INVALID"))
        s=k.transition_doctor_enforced(active_path_proof_sha256="a"*64,expected_epoch=1,trust_key_path=str(key),doctor_verifier_path=str(verifier)); assert s["state"]=="DOCTOR_ENFORCED" and s["authority_epoch"]==2; tests.append("doctor-transition-binds-verifier-and-key")
        ok("stale-authority-epoch-denies",lambda:denied(lambda:k.check(permit(authority_epoch=1,doctor_receipt_sha256="b"*64),intent(authority_epoch=1)),"STALE_AUTHORITY_EPOCH"))
        ok("invalid-doctor-receipt-denies",lambda:denied(lambda:k.check(permit(authority_epoch=2,doctor_receipt_sha256="x"),intent(authority_epoch=2)),"DOCTOR_RECEIPT_SHA256_INVALID"))
        doctor_intent=mod.MaterialIntent(BASE_HEAD,P,T,"ORDINARY_SOURCE_PACKAGE","ORDINARY",2)
        receipt=gate.evaluate(pass_request(BASE_HEAD,P,T,2)); tobj=trust.sign_receipt(receipt=receipt,key=key.read_bytes(),verifier_path=verifier,authority_epoch=2); tf=root/"trust.json"; tf.write_text(json.dumps(tobj,sort_keys=True,separators=(",",":"))+"\n",encoding="utf-8")
        dp={"schema":mod.PERMIT_SCHEMA,"permit_id":"D1","campaign_id":"ORDINARY","package_class":"ORDINARY_SOURCE_PACKAGE","source_pre_head":BASE_HEAD,"package_sha256":P,"touched_paths_sha256":T,"nonce":"doctor-enforced-0001","authority_epoch":2,"doctor_receipt_sha256":receipt["receipt_fingerprint"],"doctor_trust_object_path":str(tf)}
        rr=k.check(dp,doctor_intent); assert rr["result"]=="ALLOW" and rr["doctor_trust_object_fingerprint"]==tobj["trust_object_fingerprint"]; tests.append("doctor-current-trust-allows")
        x=dict(dp); x.pop("doctor_trust_object_path"); x["permit_id"]="D2"; x["nonce"]="doctor-missing-trust1"; ok("raw-doctor-receipt-denies",lambda:denied(lambda:k.check(x,doctor_intent),"DOCTOR_TRUST_OBJECT_REQUIRED"))
        tam=json.loads(json.dumps(tobj)); tam["attestation"]["signature"]="0"*64; ttf=root/"tamper.json"; ttf.write_text(json.dumps(tam,sort_keys=True,separators=(",",":"))+"\n",encoding="utf-8"); x=dict(dp,permit_id="D3",nonce="doctor-tampered-0001",doctor_trust_object_path=str(ttf)); ok("tampered-trust-denies",lambda:denied(lambda:k.check(x,doctor_intent),"DOCTOR_TRUST_SIGNATURE_INVALID"))
        wrong=dict(tobj); wrong["receipt_fingerprint"]="f"*64; # fingerprint/signature now invalid before receipt match can authorize
        wf=root/"wrong.json"; wf.write_text(json.dumps(wrong,sort_keys=True,separators=(",",":"))+"\n",encoding="utf-8"); x=dict(dp,permit_id="D4",nonce="doctor-wrongreceipt01",doctor_trust_object_path=str(wf)); ok("wrong-receipt-trust-denies",lambda:denied(lambda:k.check(x,doctor_intent),"DOCTOR_ATTESTATION_RECEIPT_MISMATCH"))
        old_key=key.read_bytes(); key.write_bytes(b"Z"*64); x=dict(dp,permit_id="D5",nonce="doctor-key-drift-0001"); ok("trust-key-drift-denies",lambda:denied(lambda:k.check(x,doctor_intent),"DOCTOR_TRUST_KEY_DRIFT")); key.write_bytes(old_key)
        old_ver=verifier.read_bytes(); verifier.write_bytes(old_ver+b"\n#drift\n"); x=dict(dp,permit_id="D6",nonce="doctor-verifierdrift"); ok("verifier-drift-denies",lambda:denied(lambda:k.check(x,doctor_intent),"DOCTOR_VERIFIER_DRIFT")); verifier.write_bytes(old_ver)
    with tempfile.TemporaryDirectory() as d:
        root=Path(d); state=root/"state.json"; manifest=root/"manifest.json"; m={"files":[{"path":"b.txt"},{"path":"a.txt"}],"assurance_kernel":{"campaign_id":"BOOT23_ASSURANCE_KERNEL_BOOTSTRAP_001","package_class":mod.BOOTSTRAP_PACKAGE_CLASS,"authority_epoch":1}}; manifest.write_text(json.dumps(m,sort_keys=True,separators=(",",":")),encoding="utf-8"); mi=mod.intent_from_manifest(str(manifest),BASE_HEAD); k=mod.AssuranceKernel(state); k.initialize_bootstrap(external_anchor_proof="external-anchor-proof-0123456789abcdef"); mp=permit(package_sha256=mi.package_sha256,touched_paths_sha256=mi.touched_paths_sha256); assert k.check(mp,mi)["result"]=="ALLOW"; tests.append("manifest-binding-byte-and-path-sensitive"); m["files"].append({"path":"unexpected.txt"}); manifest.write_text(json.dumps(m,sort_keys=True,separators=(",",":")),encoding="utf-8"); changed=mod.intent_from_manifest(str(manifest),BASE_HEAD); ok("manifest-change-denies-old-permit",lambda:denied(lambda:k.check(mp,changed),"BINDING_MISMATCH:package_sha256"))
    with tempfile.TemporaryDirectory() as d:
        state=Path(d)/"state.json"; k1=mod.AssuranceKernel(state); k1.initialize_bootstrap(external_anchor_proof="external-anchor-proof-0123456789abcdef"); barrier=threading.Barrier(3); results=[]
        def worker(pid,nonce):
            kk=mod.AssuranceKernel(state); barrier.wait()
            try: results.append(("ALLOW",kk.consume(permit(permit_id=pid,nonce=nonce),intent())))
            except mod.AssuranceDenied as e: results.append(("DENY",str(e)))
        t1=threading.Thread(target=worker,args=("PX1","0000000000000001")); t2=threading.Thread(target=worker,args=("PX2","0000000000000002")); t1.start(); t2.start(); barrier.wait(); t1.join(); t2.join(); assert sum(1 for r,_ in results if r=="ALLOW")==1 and sum(1 for r,_ in results if r=="DENY")==1,results; tests.append("concurrent-double-consume-fenced")
    with tempfile.TemporaryDirectory() as d:
        k=mod.AssuranceKernel(Path(d)/"state.json"); k.initialize_bootstrap(external_anchor_proof="external-anchor-proof-0123456789abcdef"); s=k.transition_failed_recovery(reason_sha256="c"*64,expected_epoch=1); assert s["state"]=="FAILED_RECOVERY" and s["authority_epoch"]==2; tests.append("failed-recovery-fences-epoch"); ok("failed-recovery-denies-material",lambda:denied(lambda:k.check(permit(authority_epoch=2),intent(authority_epoch=2)),"KERNEL_NOT_ENFORCING"))
    return {"result":"PASS","canaries":len(tests),"tests":tests}
if __name__=="__main__": print(json.dumps(run(),sort_keys=True))
