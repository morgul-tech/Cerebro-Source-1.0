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
def run_immune():
    immune_tests=[]
    immune=load("immune_attestation",ROOT/"tooling"/"assurance"/"immune_attestation.py")
    assert "IMMUNE_MIGRATING" in mod.STATES and "IMMUNE_ENFORCED" in mod.STATES and "IMMUNE_QUARANTINED" in mod.STATES
    immune_tests.append("immune-state-vocabulary-bound")
    for relative in (
        "standards/immune-system.yaml",
        "mcp/immune-material-permit.schema.json",
        "mcp/immune-material-receipt.schema.json",
        "mcp/immune-attestation.schema.json",
        "mcp/immune-migration.schema.json",
        "tooling/assurance/immune_attestation.py",
    ):
        assert (ROOT/relative).is_file(),relative
    immune_tests.append("immune-contract-targetset-present")
    with tempfile.TemporaryDirectory() as d:
        root=Path(d); state=root/"state.json"; key=root/"key.bin"; key.write_bytes(b"I"*64)
        attestor=ROOT/"tooling"/"assurance"/"immune_attestation.py"
        base_state={
            "schema":mod.STATE_SCHEMA,
            "state":"IMMUNE_ENFORCED",
            "authority_epoch":9,
            "consumed":[],
            "migration_id":"TEST-MIGRATION",
            "migration_source_state":"FAILED_RECOVERY",
            "migration_source_head":BASE_HEAD,
            "migration_source_tree":"1"*40,
            "migration_consumed_ledger_sha256":mod.ledger_fingerprint([]),
            "migration_receipt_sha256":"2"*64,
            "external_anchor_id":"EXTERNAL-ANCHOR-TEST",
            "external_anchor_fingerprint":"3"*64,
            "external_anchor_verifier_path":str(root/"external.py"),
            "external_anchor_verifier_sha256":"4"*64,
            "immune_attestor_path":str(attestor),
            "immune_attestor_sha256":mod.file_sha256(attestor),
            "immune_attestor_key_path":str(key),
            "immune_attestor_key_fingerprint":mod.sha256_hex(key.read_bytes()),
            "migration_recovery_record":{
                "schema":mod.IMMUNE_RECOVERY_RECORD_SCHEMA,
                "migration_id":"TEST-MIGRATION",
                "migration_subject_sha256":"a"*64,
                "entry_authorization_fingerprint":"b"*64,
                "prestate_fingerprint":"c"*64,
                "post_entry_authority_epoch":9,
                "consumed_ledger_sha256":mod.ledger_fingerprint([]),
                "installation_plan_sha256":"d"*64,
                "entry_nonce_sha256":"e"*64,
                "recovery_consumptions":[],
            },
            "immune_activation_proof_sha256":"5"*64,
        }
        state.write_text(json.dumps(base_state,sort_keys=True,separators=(",",":"))+"\n",encoding="utf-8")
        k=mod.AssuranceKernel(state)
        mi=mod.MaterialIntent(BASE_HEAD,P,T,"STANDARD_SOURCE_PACKAGE","IMMUNE-TEST",9)
        ip={
            "schema":mod.IMMUNE_PERMIT_SCHEMA,
            "permit_id":"IP1",
            "campaign_id":"IMMUNE-TEST",
            "package_class":"STANDARD_SOURCE_PACKAGE",
            "source_pre_head":BASE_HEAD,
            "package_sha256":P,
            "touched_paths_sha256":T,
            "quarantine_scope_sha256":"6"*64,
            "risk_profile":"I3_TRUST_CRITICAL",
            "intended_consequence_class":"SOURCE_EFFECT",
            "nonce":"immune-validator-0001",
            "authority_epoch":9,
            "producer_identity":"IMPLEMENTER-TEST",
            "attestation_path":str(root/"attestation.json"),
            "material_consumer_identity":"STANDARD_DELIVERY",
        }
        subject=mod.AssuranceKernel._immune_subject(ip,mi)
        signed=immune.sign_attestation(subject=subject,key=key.read_bytes(),attestor_identity="INDEPENDENT-ATTESTOR-TEST",implementation_path=attestor)
        Path(ip["attestation_path"]).write_text(json.dumps(signed,sort_keys=True,separators=(",",":"))+"\n",encoding="utf-8")
        receipt=k.consume(ip,mi)
        assert receipt["result"]=="ALLOW" and receipt["assurance_profile"]=="IMMUNE"
        immune_tests.append("immune-independent-attestation-allows")
        denied(lambda:k.consume(ip,mi),"PERMIT_REPLAY")
        immune_tests.append("immune-permit-replay-denies")
        base_state["state"]="IMMUNE_MIGRATING"; base_state["authority_epoch"]=10
        state.write_text(json.dumps(base_state,sort_keys=True,separators=(",",":"))+"\n",encoding="utf-8")
        moving_intent=mod.MaterialIntent(BASE_HEAD,P,T,"STANDARD_SOURCE_PACKAGE","IMMUNE-TEST",10)
        moving=dict(ip,permit_id="IP2",nonce="immune-validator-0002",authority_epoch=10)
        denied(lambda:mod.AssuranceKernel(state).check(moving,moving_intent),"IMMUNE_MIGRATION_NOT_FINALIZED")
        immune_tests.append("immune-migrating-denies-material")
        base_state["state"]="IMMUNE_QUARANTINED"; base_state["authority_epoch"]=11
        base_state["quarantine_reason_sha256"]="7"*64; base_state["quarantine_scope_sha256"]="8"*64
        state.write_text(json.dumps(base_state,sort_keys=True,separators=(",",":"))+"\n",encoding="utf-8")
        quarantined_intent=mod.MaterialIntent(BASE_HEAD,P,T,"STANDARD_SOURCE_PACKAGE","IMMUNE-TEST",11)
        quarantined=dict(ip,permit_id="IP3",nonce="immune-validator-0003",authority_epoch=11)
        denied(lambda:mod.AssuranceKernel(state).check(quarantined,quarantined_intent),"KERNEL_NOT_ENFORCING")
        immune_tests.append("immune-quarantined-denies-material")
        try:
            immune.sign_attestation(subject=subject,key=key.read_bytes(),attestor_identity=subject["producer_identity"],implementation_path=attestor)
        except immune.ImmuneAttestationError as e:
            assert "PRODUCER_SELF_ATTESTATION_PROHIBITED" in str(e)
        else:
            raise AssertionError("EXPECTED_SELF_ATTESTATION_DENY")
        immune_tests.append("producer-self-attestation-denies")
    return {"result":"PASS","canaries":len(immune_tests),"tests":immune_tests}


def run_recovery():
    immune=load("immune_attestation_recovery",ROOT/"tooling"/"assurance"/"immune_attestation.py")
    tests=[]
    def denied_code(fn,code):
        try: fn()
        except mod.AssuranceDenied as e:
            assert code in str(e),(code,str(e)); return
        raise AssertionError("EXPECTED_DENY:"+code)

    def fixture():
        temp=tempfile.TemporaryDirectory(); root=Path(temp.name)
        state=root/"state.json"; key=root/"immune-key.bin"; key.write_bytes(b"R"*64)
        attestor=ROOT/"tooling"/"assurance"/"immune_attestation.py"
        external=root/"external_verifier.py"
        external.write_text("""import hashlib,json\n\ndef c(v): return json.dumps(v,sort_keys=True,separators=(",", ":")).encode()\ndef h(v): return hashlib.sha256(c(v)).hexdigest()\ndef verify_migration(migration,attestation_path):\n return {"result":"PASS","external_anchor_id":migration["external_anchor_id"],"external_anchor_fingerprint":migration["external_anchor_fingerprint"],"currentness":"CURRENT","preexisting":True,"outside_targetset":True,"receipt_sha256":h({"entry":migration["migration_id"]}),"anchor_authorization_fingerprint":h({"entry_subject":migration["migration_id"],"nonce":migration["nonce"]})}\ndef verify_recovery(recovery,recovery_record,attestation_path):\n if recovery.get("migration_subject_sha256") != recovery_record.get("migration_subject_sha256"): return {"result":"DENY"}\n return {"result":"PASS","external_anchor_id":"ANCHOR-1","external_anchor_fingerprint":"3"*64,"currentness":"CURRENT","recovery_authorization_fingerprint":h({"subject":recovery["migration_subject_sha256"],"nonce":recovery["recovery_nonce"],"mcp":recovery["mcp_recovery_decision_sha256"],"host":recovery["current_host_proof_sha256"]})}\n""",encoding="utf-8")
        external_att=root/"external-entry.json"; external_att.write_text("{}\n",encoding="utf-8")
        external_recovery=root/"external-recovery.json"; external_recovery.write_text("{}\n",encoding="utf-8")
        plan=root/"plan.json"
        plan_obj={"schema":mod.IMMUNE_INSTALLATION_PLAN_SCHEMA,"entries":[
            {"path":"a.txt","pre_sha256":"1"*64,"final_sha256":"a"*64},
            {"path":"b.txt","pre_sha256":"2"*64,"final_sha256":"b"*64},
            {"path":"c.txt","pre_sha256":"ABSENT","final_sha256":"c"*64},
        ]}
        plan.write_text(json.dumps(plan_obj,sort_keys=True,separators=(",",":"))+"\n",encoding="utf-8")
        initial={"schema":mod.STATE_SCHEMA,"state":"FAILED_RECOVERY","authority_epoch":4,"consumed":[],"recovery_reason_sha256":"9"*64}
        state.write_text(json.dumps(initial,sort_keys=True,separators=(",",":"))+"\n",encoding="utf-8")
        migration={
            "schema":mod.IMMUNE_MIGRATION_SCHEMA,"migration_id":"MIG-RECOVERY-TEST","operation":"IMMUNE_MIGRATION",
            "source_state":"FAILED_RECOVERY","target_state":"IMMUNE_MIGRATING","source_repository":"morgul-tech/Cerebro-Source-1.0",
            "source_branch":"main","source_head":BASE_HEAD,"source_tree":"1"*40,"authority_epoch":4,
            "consumed_ledger_sha256":mod.ledger_fingerprint([]),"campaign_id":"IMMUNE-RECOVERY-TEST","package_sha256":P,
            "touched_paths_sha256":T,"mcp_decision_fingerprint":"4"*64,"current_host_proof_sha256":"5"*64,
            "quarantine_scope_sha256":"6"*64,"risk_profile":"I3_TRUST_CRITICAL","prestate_manifest_sha256":"7"*64,
            "rollback_plan_sha256":"8"*64,"installation_plan_path":str(plan),"installation_plan_sha256":mod.file_sha256(plan),
            "producer_identity":"IMPLEMENTER-TEST","external_anchor_id":"ANCHOR-1","external_anchor_fingerprint":"3"*64,
            "external_anchor_preexisting":True,"external_anchor_outside_targetset":True,"external_anchor_verifier_path":str(external),
            "external_anchor_verifier_sha256":mod.file_sha256(external),"external_anchor_attestation_path":str(external_att),
            "immune_attestor_path":str(attestor),"immune_attestor_sha256":mod.file_sha256(attestor),"immune_attestor_key_path":str(key),
            "immune_attestor_key_fingerprint":mod.sha256_hex(key.read_bytes()),"claim_scope":["SOURCE_PROMOTION"],"nonce":"ENTRY:recovery-test-000001",
        }
        k=mod.AssuranceKernel(state); entered=k.begin_immune_migration(migration,expected_epoch=4)
        assert entered["state"]=="IMMUNE_MIGRATING" and entered["authority_epoch"]==5
        assert entered["migration_recovery_record"]["migration_subject_sha256"] and entered["migration_recovery_record"]["recovery_consumptions"]==[]
        def observation(values,name):
            path=root/(name+".json")
            obj={"schema":mod.IMMUNE_INSTALLATION_OBSERVATION_SCHEMA,"entries":[
                {"path":"a.txt","sha256":values[0]},{"path":"b.txt","sha256":values[1]},{"path":"c.txt","sha256":values[2]},
            ]}
            path.write_text(json.dumps(obj,sort_keys=True,separators=(",",":"))+"\n",encoding="utf-8"); return path
        observations={
            "pre":observation(["1"*64,"2"*64,"ABSENT"],"pre"),
            "prefix":observation(["a"*64,"2"*64,"ABSENT"],"prefix"),
            "full":observation(["a"*64,"b"*64,"c"*64],"full"),
            "drift":observation(["1"*64,"b"*64,"ABSENT"],"drift"),
        }
        def request(action="RESUME_EXACT",obs="pre",publication="NOT_PUBLISHED",nonce="RECOVERY:nonce-00000000000001",mcp="a"*64,host="b"*64):
            st=mod.AssuranceKernel(state)._read(); rr=st["migration_recovery_record"]; op=observations[obs]
            r={
                "schema":mod.IMMUNE_MIGRATION_SCHEMA,"migration_id":st["migration_id"],"operation":"IMMUNE_MIGRATION_RECOVERY",
                "requested_recovery_action":action,"authority_epoch":st["authority_epoch"],"migration_subject_sha256":rr["migration_subject_sha256"],
                "entry_authorization_fingerprint":rr["entry_authorization_fingerprint"],"mcp_recovery_decision_sha256":mcp,
                "current_host_proof_sha256":host,"external_anchor_recovery_attestation_path":str(external_recovery),
                "recovery_attestation_path":str(root/"recovery-attestation.json"),"recovery_nonce":nonce,
                "installation_plan_path":str(plan),"installation_plan_sha256":mod.file_sha256(plan),
                "installation_observation_path":str(op),"installation_observation_sha256":mod.file_sha256(op),
                "publication_state":publication,"quarantine_scope_sha256":"6"*64,"producer_identity":"RECOVERY-EXECUTOR-TEST",
            }
            if action=="ROLLBACK_EXACT_PREPUBLICATION":
                r.update(rollback_completion_proof_sha256="d"*64,prestate_manifest_sha256="7"*64,rollback_plan_sha256="8"*64)
            if action=="QUARANTINE": r["reason_sha256"]="e"*64
            signed=immune.sign_attestation(subject=mod.AssuranceKernel._recovery_subject(r),key=key.read_bytes(),attestor_identity="INDEPENDENT-RECOVERY-ATTESTOR",implementation_path=attestor)
            Path(r["recovery_attestation_path"]).write_text(json.dumps(signed,sort_keys=True,separators=(",",":"))+"\n",encoding="utf-8")
            return r
        return temp,root,state,k,migration,request,observations

    # F26: crash immediately after durable entry can authenticate a no-install resume.
    t,root,state,k,mig,req,obs=fixture(); r=req(); chk=k.check_immune_migration_recovery(r,expected_epoch=5); assert chk["result"]=="PASS" and chk["installation_progress"]=="PRESTATE_EXACT"; out=k.recover_immune_migration(r,expected_epoch=5); assert out["result"]=="RECOVERY" and out["kernel_state"]=="IMMUNE_MIGRATING" and out["authority_epoch_after"]==6; tests.append("F26-post-entry-crash-authenticated-resume"); t.cleanup()
    # F27: after state entry/before install is the same authenticated no-install class.
    t,root,state,k,mig,req,obs=fixture(); r=req(nonce="RECOVERY:f27-000000000000001"); assert k.check_immune_migration_recovery(r,expected_epoch=5)["installation_progress"]=="PRESTATE_EXACT"; tests.append("F27-post-lock-release-preinstall-return"); t.cleanup()
    # F28: deterministic prefix resumes; non-prefix drift cannot resume.
    t,root,state,k,mig,req,obs=fixture(); r=req(obs="prefix",nonce="RECOVERY:f28-prefix-000000001"); assert k.check_immune_migration_recovery(r,expected_epoch=5)["installation_progress"]=="DETERMINISTIC_PREFIX"; drift=req(obs="drift",nonce="RECOVERY:f28-drift-0000000001"); denied_code(lambda:k.check_immune_migration_recovery(drift,expected_epoch=5),"RECOVERY_QUARANTINE_REQUIRED"); tests.append("F28-prefix-classifier-fail-closed"); t.cleanup()
    # F29: full local install before publication remains resumable with renewed authority.
    t,root,state,k,mig,req,obs=fixture(); r=req(obs="full",nonce="RECOVERY:f29-full-00000000001"); assert k.check_immune_migration_recovery(r,expected_epoch=5)["installation_progress"]=="FULLY_INSTALLED"; tests.append("F29-full-local-prepublication-resume"); t.cleanup()
    # F30: uncertain/published recovery cannot resume; authenticated quarantine is permitted.
    t,root,state,k,mig,req,obs=fixture(); r=req(publication="UNKNOWN",nonce="RECOVERY:f30-unknown-000000001"); denied_code(lambda:k.check_immune_migration_recovery(r,expected_epoch=5),"RECOVERY_QUARANTINE_REQUIRED"); q=req(action="QUARANTINE",publication="UNKNOWN",obs="drift",nonce="RECOVERY:f30-quarantine-000001"); qo=k.recover_immune_migration(q,expected_epoch=5); assert qo["kernel_state"]=="IMMUNE_QUARANTINED"; tests.append("F30-uncertain-publication-quarantines"); t.cleanup()
    # F31: entry nonce cannot be re-used as recovery authority.
    t,root,state,k,mig,req,obs=fixture(); r=req(); r["recovery_nonce"]=mig["nonce"]; denied_code(lambda:k.check_immune_migration_recovery(r,expected_epoch=5),"IMMUNE_RECOVERY_NONCE_NAMESPACE_INVALID"); tests.append("F31-entry-nonce-replay-denies"); t.cleanup()
    # F32: a fresh recovery nonce cannot detach from the persisted in-flight subject.
    t,root,state,k,mig,req,obs=fixture(); r=req(nonce="RECOVERY:f32-000000000000001"); r["migration_subject_sha256"]="f"*64; denied_code(lambda:k.check_immune_migration_recovery(r,expected_epoch=5),"IMMUNE_RECOVERY_SUBJECT_MISMATCH:migration_subject_sha256"); tests.append("F32-new-nonce-subject-break-denies"); t.cleanup()
    # F33: state observation is evidence only; changing MCP recovery decision invalidates independent attestation.
    t,root,state,k,mig,req,obs=fixture(); r=req(nonce="RECOVERY:f33-000000000000001"); r["mcp_recovery_decision_sha256"]="f"*64; denied_code(lambda:k.check_immune_migration_recovery(r,expected_epoch=5),"IMMUNE_RECOVERY_ATTESTATION_INVALID"); tests.append("F33-observation-cannot-self-authorize"); t.cleanup()
    # F34: two concurrent consumes at one epoch produce exactly one recovery effect.
    t,root,state,k,mig,req,obs=fixture(); r1=req(nonce="RECOVERY:f34-a-000000000000001"); r2=req(nonce="RECOVERY:f34-b-000000000000001"); barrier=threading.Barrier(3); results=[]
    def worker(r):
        kk=mod.AssuranceKernel(state); barrier.wait()
        try: results.append(("RECOVERY",kk.recover_immune_migration(r,expected_epoch=5)))
        except mod.AssuranceDenied as e: results.append(("DENY",str(e)))
    th1=threading.Thread(target=worker,args=(r1,)); th2=threading.Thread(target=worker,args=(r2,)); th1.start(); th2.start(); barrier.wait(); th1.join(); th2.join(); assert sum(1 for x,_ in results if x=="RECOVERY")==1 and sum(1 for x,_ in results if x=="DENY")==1,results; tests.append("F34-concurrent-recovery-exactly-one-effect"); t.cleanup()
    # F35: rollback can only occur after exact prestate is restored and epoch still increases.
    t,root,state,k,mig,req,obs=fixture(); r=req(action="ROLLBACK_EXACT_PREPUBLICATION",nonce="RECOVERY:f35-rollback-00000001"); out=k.recover_immune_migration(r,expected_epoch=5); assert out["kernel_state"]=="FAILED_RECOVERY" and out["authority_epoch_before"]==5 and out["authority_epoch_after"]==6; tests.append("F35-rollback-preserves-monotonic-epoch"); t.cleanup()
    # F36: post-recovery completion still requires exact migration receipt and activation proof.
    t,root,state,k,mig,req,obs=fixture(); r=req(obs="full",nonce="RECOVERY:f36-000000000000001"); out=k.recover_immune_migration(r,expected_epoch=5); denied_code(lambda:k.finalize_immune_enforced(expected_epoch=6,migration_receipt_sha256="0"*64,immune_activation_proof_sha256="1"*64),"IMMUNE_MIGRATION_RECEIPT_MISMATCH"); denied_code(lambda:k.finalize_immune_enforced(expected_epoch=6,migration_receipt_sha256=mod.AssuranceKernel(state)._read()["migration_receipt_sha256"],immune_activation_proof_sha256="bad"),"IMMUNE_ACTIVATION_PROOF_INVALID"); tests.append("F36-recovered-completion-still-proof-gated"); t.cleanup()
    return {"result":"PASS","canaries":len(tests),"tests":tests}

def run_first_activation():
    tests=[]
    with tempfile.TemporaryDirectory() as d:
        root=Path(d); state=root/"state.json"; source=root/"source"; source.mkdir()
        key=root/"doctor-key.bin"; key.write_bytes(b"A"*64)
        attestor=ROOT/"tooling"/"assurance"/"immune_attestation.py"
        doctor_verifier=ROOT/"tooling"/"doctor"/"doctor_trust.py"
        manifest=root/"manifest.json"
        manifest_obj={
            "expected_base_commit":mod.FIRST_ACTIVATION_BASE_COMMIT,
            "files":[{
                "path":"tooling/assurance/assurance_kernel.py","operation":"replace",
                "expected_git_blob_sha":"1"*40,"final_git_blob_sha":"2"*40,"sha256":"3"*64,
            }],
        }
        manifest.write_text(json.dumps(manifest_obj,sort_keys=True,separators=(",",":"))+"\n",encoding="utf-8")
        candidate=mod.candidate_identity_from_manifest(manifest)
        current="4"*40; tree="5"*40; host="6"*64; consumed="7"*64
        base_state={
            "schema":mod.STATE_SCHEMA,"state":"DOCTOR_ENFORCED","authority_epoch":2,
            "consumed":[consumed],"anchor_proof_sha256":"8"*64,
            "doctor_active_path_proof_sha256":"a"*64,
            "doctor_trust_key_path":str(key.resolve()),"doctor_trust_key_sha256":mod.file_sha256(key),
            "doctor_verifier_path":str(doctor_verifier.resolve()),"doctor_verifier_sha256":mod.file_sha256(doctor_verifier),
        }
        state.write_text(json.dumps(base_state,sort_keys=True,separators=(",",":"))+"\n",encoding="utf-8")
        authorization={
            "schema":mod.ONE_TIME_HUMAN_ADMIN_FIRST_ACTIVATION_SCHEMA,
            "authorization_id":"AUTH-TEST","authorization_type":mod.ONE_TIME_HUMAN_ADMIN_FIRST_ACTIVATION,
            "directive":mod.FIRST_ACTIVATION_DIRECTIVE,
            "candidate_parent_fingerprint":mod.FIRST_ACTIVATION_PARENT_CANDIDATE_FINGERPRINT,
            "candidate_identity":candidate,"source_base_commit":mod.FIRST_ACTIVATION_BASE_COMMIT,
            "source_current_commit":current,"source_current_tree":tree,"working_source_path":str(source.resolve()),
            "host_fingerprint":host,"migration_id":"IMMUNE-FIRST-ACTIVATION-TEST",
            "nonce":"9"*64,"authority_epoch":2,"delivery_consumption_id":consumed,
            "immune_attestor_path":str(attestor.resolve()),"immune_attestor_sha256":mod.file_sha256(attestor),
            "immune_attestor_key_path":str(key.resolve()),"immune_attestor_key_fingerprint":mod.file_sha256(key),
            "authoritative_source":"origin/main","branch":"main","remote_equality_verified":True,
        }
        original_host=mod.current_host_fingerprint; original_git=mod._git_value
        def fake_git(_root,*args):
            table={
                ("branch","--show-current"):"main",("status","--porcelain","--untracked-files=all"):"",
                ("remote","get-url","origin"):"https://github.com/morgul-tech/Cerebro-Source-1.0.git",
                ("fetch","--no-tags","origin","main"):"",("rev-parse","HEAD"):current,
                ("rev-parse","refs/remotes/origin/main"):current,("rev-parse","HEAD^{tree}"):tree,
            }
            return table[args]
        try:
            mod.current_host_fingerprint=lambda _path:host; mod._git_value=fake_git
            k=mod.AssuranceKernel(state)
            mismatch=dict(authorization,host_fingerprint="a"*64)
            denied(lambda:k.transition_one_time_human_admin_first_activation(
                mismatch,expected_epoch=2,manifest_path=str(manifest),working_source_path=str(source),
                immune_attestor_path=str(attestor)),"FIRST_ACTIVATION_HOST_MISMATCH")
            tests.append("first-activation-host-mismatch-denies")
            missing=root/"missing-key.bin"
            missing_auth=dict(authorization,immune_attestor_key_path=str(missing),immune_attestor_key_fingerprint="b"*64)
            missing_state=dict(base_state,doctor_trust_key_path=str(missing),doctor_trust_key_sha256="b"*64)
            state.write_text(json.dumps(missing_state,sort_keys=True,separators=(",",":"))+"\n",encoding="utf-8")
            denied(lambda:k.transition_one_time_human_admin_first_activation(
                missing_auth,expected_epoch=2,manifest_path=str(manifest),working_source_path=str(source),
                immune_attestor_path=str(attestor)),"FIRST_ACTIVATION_ATTESTOR_KEY_INVALID")
            tests.append("first-activation-missing-key-denies")
            state.write_text(json.dumps(base_state,sort_keys=True,separators=(",",":"))+"\n",encoding="utf-8")
            activated=k.transition_one_time_human_admin_first_activation(
                authorization,expected_epoch=2,manifest_path=str(manifest),working_source_path=str(source),
                immune_attestor_path=str(attestor))
            assert activated["state"]=="IMMUNE_ENFORCED" and activated["authority_epoch"]==3
            assert activated["one_time_human_admin_first_activation_consumed"] is True
            assert activated["migrated_existing_doctor_key_without_new_secret"] is True
            assert "doctor_trust_key_path" not in activated
            tests.append("first-activation-exact-binding-activates")
            denied(lambda:k.transition_one_time_human_admin_first_activation(
                authorization,expected_epoch=3,manifest_path=str(manifest),working_source_path=str(source),
                immune_attestor_path=str(attestor)),"FIRST_ACTIVATION_FROM_INVALID_STATE")
            tests.append("first-activation-replay-denies")
        finally:
            mod.current_host_fingerprint=original_host; mod._git_value=original_git
    return {"result":"PASS","canaries":len(tests),"tests":tests}

def run_all():
    legacy=run(); immune=run_immune(); recovery=run_recovery(); first=run_first_activation()
    assert legacy["result"]=="PASS" and immune["result"]=="PASS" and recovery["result"]=="PASS" and first["result"]=="PASS"
    return {
        "result":"PASS",
        "legacy_canaries":legacy["canaries"],
        "immune_canaries":immune["canaries"],
        "recovery_canaries":recovery["canaries"],
        "first_activation_canaries":first["canaries"],
        "canaries":legacy["canaries"]+immune["canaries"]+recovery["canaries"]+first["canaries"],
        "legacy_tests":legacy["tests"],
        "immune_tests":immune["tests"],
        "recovery_tests":recovery["tests"],
        "first_activation_tests":first["tests"],
    }

if __name__=="__main__": print(json.dumps(run_all(),sort_keys=True))
