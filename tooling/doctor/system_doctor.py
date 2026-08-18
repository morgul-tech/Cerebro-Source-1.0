#!/usr/bin/env python3
from __future__ import annotations
import argparse, hashlib, importlib.util, json, os, subprocess, sys, tempfile
from pathlib import Path
from typing import Any

DIMENSIONS = ["PRESENT","REGISTERED","OWNED","BOUND","ACTIVATED","CONSUMED","PROPAGATED","EFFECT_OBSERVED","OBSERVABILITY","FAIL_CLOSED","CURRENTNESS","EXCLUSIVITY_NO_SHADOW_PATH"]

def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

def sha256_hex(data: bytes) -> str: return hashlib.sha256(data).hexdigest()
def file_sha(path: Path) -> str: return sha256_hex(path.read_bytes())

def _load_module(name: str, path: Path):
    spec=importlib.util.spec_from_file_location(name,path); mod=importlib.util.module_from_spec(spec); sys.modules[name]=mod; spec.loader.exec_module(mod); return mod

def _git_head(root: Path) -> str:
    p=subprocess.run(["git","-C",str(root),"rev-parse","HEAD"],text=True,capture_output=True)
    if p.returncode!=0: raise RuntimeError("SOURCE_HEAD_UNAVAILABLE:"+(p.stderr or p.stdout).strip())
    return p.stdout.strip().lower()

def _status(dim: str, ok: bool, evidence: Any, failure: str | None = None) -> dict[str, Any]:
    return {"dimension":dim,"status":"PASS" if ok else "FAIL","evidence":evidence,"failure":failure if not ok else None}

def assure(source_root: Path, state_path: Path, trust_key_path: Path) -> dict[str, Any]:
    doctor_dir=source_root/"tooling"/"doctor"; gate_path=doctor_dir/"doctor_gate.py"; trust_path=doctor_dir/"doctor_trust.py"; sysdoc_path=doctor_dir/"system_doctor.py"; kernel_path=source_root/"tooling"/"assurance"/"assurance_kernel.py"
    required=[gate_path,trust_path,sysdoc_path,kernel_path,source_root/"standards"/"doctor-assurance.yaml",source_root/"mcp"/"doctor-trust-object.schema.json",source_root/"tooling"/"delivery"/"Cerebro.StandardDeliveryKernel.ps1"]
    rows=[]
    present=all(p.is_file() for p in required); rows.append(_status("PRESENT",present,{"required":[str(p) for p in required]},"REQUIRED_MEMBER_MISSING"))
    if not present:
        return _finish(rows)
    gate=_load_module("cerebro_system_doctor_gate",gate_path); trustmod=_load_module("cerebro_system_doctor_trust",trust_path); kernelmod=_load_module("cerebro_system_doctor_kernel",kernel_path)
    source_head=_git_head(source_root)

    cerebro=(source_root/"cerebro.yaml").read_text(encoding="utf-8"); standards=(source_root/"standards"/"standards.yaml").read_text(encoding="utf-8"); mcp=(source_root/"mcp"/"manifest.yaml").read_text(encoding="utf-8"); ac=(source_root/"mcp"/"assurance-continuity.yaml").read_text(encoding="utf-8")
    reg_checks={
        "cerebro_doctor_standard":"doctor_assurance: standards/doctor-assurance.yaml" in cerebro,
        "cerebro_doctor_tooling":"doctor: tooling/doctor/" in cerebro,
        "standards_registry":"STD-DOCTOR-ASSURANCE" in standards and "standards/doctor-assurance.yaml" in standards,
        "mcp_doctor_gate":"doctor_gate_ref: tooling/doctor/doctor_gate.py" in mcp,
        "mcp_doctor_trust":"doctor_trust_ref: tooling/doctor/doctor_trust.py" in mcp,
        "mcp_system_doctor":"system_doctor_ref: tooling/doctor/system_doctor.py" in mcp,
        "continuity_contract":"doctor_assurance_contract: standards/doctor-assurance.yaml" in ac,
    }
    rows.append(_status("REGISTERED",all(reg_checks.values()),reg_checks,"REGISTRATION_INCOMPLETE"))
    contract=(source_root/"standards"/"doctor-assurance.yaml").read_text(encoding="utf-8")
    owner_ok="control_owner: MCP" in contract and "semantic_authority: NONE" in contract and "assurance_owner: Doctor" in contract
    rows.append(_status("OWNED",owner_ok,{"control_owner":"MCP","assurance_owner":"Doctor","semantic_authority":"NONE"},"OWNER_SPLIT_INVALID"))

    state=json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    bound_ok=(state.get("state")=="DOCTOR_ENFORCED" and state.get("doctor_verifier_path") and state.get("doctor_trust_key_path") and Path(str(state.get("doctor_verifier_path"))).resolve()==trust_path.resolve())
    rows.append(_status("BOUND",bool(bound_ok),{"kernel_state":state.get("state"),"doctor_verifier_path":state.get("doctor_verifier_path"),"doctor_trust_key_path":state.get("doctor_trust_key_path")},"DOCTOR_BINDING_MISSING"))
    activated=state.get("state")=="DOCTOR_ENFORCED"
    rows.append(_status("ACTIVATED",activated,{"kernel_state":state.get("state"),"authority_epoch":state.get("authority_epoch")},"DOCTOR_NOT_ENFORCED"))
    if not (bound_ok and activated and trust_key_path.is_file()):
        return _finish(rows)

    key=trust_key_path.read_bytes(); epoch=int(state["authority_epoch"])
    # A deterministic no-mutation wiring probe traverses Gate -> Trust -> Kernel.check.
    probe_files=[{"path":"__SYSTEM_DOCTOR_NOOP_PROBE__.json","sha256":sha256_hex(b"noop") }]
    probe_manifest={"schema":"cerebro-system-doctor-probe-manifest/v1","files":probe_files,"assurance_kernel":{"campaign_id":"SYSTEM_DOCTOR_ACTIVE_PATH_PROBE","package_class":"SYSTEM_DOCTOR_PROBE","authority_epoch":epoch}}
    manifest_raw=canonical(probe_manifest); manifest_sha=sha256_hex(manifest_raw); touched_sha=kernelmod.touched_paths_fingerprint([x["path"] for x in probe_files])
    evidence_seed=sha256_hex(canonical({"probe":"SYSTEM_DOCTOR_WIRING","source_head":source_head,"gate":file_sha(gate_path),"trust":file_sha(trust_path),"kernel":file_sha(kernel_path)}))
    gate_instances=[]
    for index,fam in enumerate(gate.GATE_FAMILIES,1):
        gate_instances.append({"instance_id":f"SYS-WIRING-{index:02d}","gate_family":fam,"applicability":"REQUIRED","status":"PASS","evidence_sha256":sha256_hex((evidence_seed+fam).encode("utf-8"))})
    request={"schema":gate.REQUEST_SCHEMA,"subject":{"subject_id":"SYSTEM_DOCTOR_ACTIVE_PATH_PROBE","source_pre_head":source_head,"package_sha256":manifest_sha,"touched_paths_sha256":touched_sha,"manifest_sha256":manifest_sha,"operation":"NON_MUTATING_KERNEL_CHECK","claim_scope":["SOURCE_PROMOTION"]},"basis":{"doctor_implementation_sha256":file_sha(gate_path),"runtime_baseline_sha256":sha256_hex(sys.version.encode()),"knowledge_basis_sha256":file_sha(source_root/"standards"/"doctor-assurance.yaml"),"failure_index_sha256":file_sha(source_root/"tooling"/"validator"/"checks.yaml"),"gate_plan_sha256":sha256_hex(canonical([x["instance_id"] for x in gate_instances]))},"gate_instances":gate_instances}
    receipt=gate.evaluate(request)
    trust_obj=trustmod.sign_receipt(receipt=receipt,key=key,verifier_path=trust_path,authority_epoch=epoch)
    with tempfile.TemporaryDirectory() as td:
        td=Path(td); trust_file=td/"trust.json"; trust_file.write_text(json.dumps(trust_obj,sort_keys=True,separators=(",",":"))+"\n",encoding="utf-8")
        permit={"schema":kernelmod.PERMIT_SCHEMA,"permit_id":"SYSTEM-DOCTOR-PROBE","campaign_id":"SYSTEM_DOCTOR_ACTIVE_PATH_PROBE","package_class":"SYSTEM_DOCTOR_PROBE","source_pre_head":source_head,"package_sha256":manifest_sha,"touched_paths_sha256":touched_sha,"nonce":"system-doctor-probe-0001","authority_epoch":epoch,"doctor_receipt_sha256":receipt["receipt_fingerprint"],"doctor_trust_object_path":str(trust_file)}
        intent=kernelmod.MaterialIntent(source_head,manifest_sha,touched_sha,"SYSTEM_DOCTOR_PROBE","SYSTEM_DOCTOR_ACTIVE_PATH_PROBE",epoch)
        try: allow=kernelmod.AssuranceKernel(state_path).check(permit,intent); consume_ok=allow.get("result")=="ALLOW"
        except Exception as e: allow={"result":"DENY","reason":str(e)}; consume_ok=False
        rows.append(_status("CONSUMED",consume_ok,{"kernel_check":allow},"KERNEL_DID_NOT_CONSUME_DOCTOR_TRUST"))
        propagated=consume_ok and allow.get("doctor_trust_object_fingerprint")==trust_obj.get("trust_object_fingerprint") and trust_obj.get("receipt_fingerprint")==receipt.get("receipt_fingerprint") and permit["doctor_receipt_sha256"]==receipt["receipt_fingerprint"]
        rows.append(_status("PROPAGATED",propagated,{"receipt":receipt.get("receipt_fingerprint"),"trust":trust_obj.get("trust_object_fingerprint"),"kernel_trust":allow.get("doctor_trust_object_fingerprint")},"ASSURANCE_IDENTITY_PROPAGATION_LOSS"))
        rows.append(_status("EFFECT_OBSERVED",consume_ok,{"effect":"VALID_CERTIFIED_CURRENT_TRUST_YIELDS_KERNEL_ALLOW","result":allow.get("result")},"EXPECTED_ALLOW_NOT_OBSERVED"))
        observable=all(isinstance(x,str) and len(x)==64 for x in [receipt.get("receipt_fingerprint"),trust_obj.get("trust_object_fingerprint"),allow.get("doctor_trust_object_fingerprint")])
        rows.append(_status("OBSERVABILITY",observable,{"receipt_fingerprint":receipt.get("receipt_fingerprint"),"trust_object_fingerprint":trust_obj.get("trust_object_fingerprint"),"kernel_receipt":allow},"OBSERVABILITY_GAP"))
        failures=[]
        # Raw receipt without trust object must deny.
        raw=dict(permit); raw.pop("doctor_trust_object_path",None); raw["permit_id"]="NEG-RAW"; raw["nonce"]="negative-raw-receipt-01"
        try: kernelmod.AssuranceKernel(state_path).check(raw,intent); failures.append("RAW_RECEIPT_ACCEPTED")
        except Exception: pass
        # Tampered signature must deny.
        tampered=dict(trust_obj); tampered["signature"]="0"*64; tampered_file=td/"tampered.json"; tampered_file.write_text(json.dumps(tampered,sort_keys=True,separators=(",",":"))+"\n",encoding="utf-8"); tp=dict(permit); tp["doctor_trust_object_path"]=str(tampered_file); tp["permit_id"]="NEG-TAMPER"; tp["nonce"]="negative-tampered-0001"
        try: kernelmod.AssuranceKernel(state_path).check(tp,intent); failures.append("TAMPERED_TRUST_ACCEPTED")
        except Exception: pass
        # Wrong package binding must deny.
        wrong_intent=kernelmod.MaterialIntent(source_head,"f"*64,touched_sha,"SYSTEM_DOCTOR_PROBE","SYSTEM_DOCTOR_ACTIVE_PATH_PROBE",epoch)
        try: kernelmod.AssuranceKernel(state_path).check(permit,wrong_intent); failures.append("WRONG_PACKAGE_ACCEPTED")
        except Exception: pass
        rows.append(_status("FAIL_CLOSED",not failures,{"negative_canaries":["raw-receipt-deny","tampered-trust-deny","wrong-package-deny"],"failures":failures},"FAIL_OPEN:"+",".join(failures) if failures else None))

    current_checks={
        "trust_key":file_sha(trust_key_path)==state.get("doctor_trust_key_sha256"),
        "verifier":file_sha(trust_path)==state.get("doctor_verifier_sha256"),
        "source_head":len(source_head)==40,
        "gate_contract":file_sha(source_root/"standards"/"doctor-assurance.yaml")==request["basis"]["knowledge_basis_sha256"],
    }
    rows.append(_status("CURRENTNESS",all(current_checks.values()),current_checks,"CURRENTNESS_MISMATCH"))
    delivery=(source_root/"tooling"/"delivery"/"Cerebro.StandardDeliveryKernel.ps1").read_text(encoding="utf-8")
    marker="consume-manifest-permit"; install="EXACT_BYTE_INSTALL"
    delivery_occ=delivery.count(marker); order_ok=delivery.find(marker)>=0 and delivery.find(install)>delivery.find(marker)
    other=[]
    delivery_dir=source_root/"tooling"/"delivery"
    for p in delivery_dir.rglob("*"):
        if p.is_file() and p!=source_root/"tooling"/"delivery"/"Cerebro.StandardDeliveryKernel.ps1" and p.suffix.lower() in {".ps1",".py"}:
            try:
                if marker in p.read_text(encoding="utf-8",errors="ignore"): other.append(str(p.relative_to(source_root)))
            except OSError: pass
    exclusive=delivery_occ==1 and order_ok and not other and "delivery_kernel: tooling/delivery/Cerebro.StandardDeliveryKernel.ps1" in cerebro
    rows.append(_status("EXCLUSIVITY_NO_SHADOW_PATH",exclusive,{"canonical_consumer":"tooling/delivery/Cerebro.StandardDeliveryKernel.ps1","consume_marker_count":delivery_occ,"before_exact_install":order_ok,"other_delivery_consumers":other},"SHADOW_OR_MISSING_MATERIAL_CONSUMER"))
    return _finish(rows)

def _finish(rows:list[dict[str,Any]])->dict[str,Any]:
    by={r["dimension"]:r for r in rows}; complete=all(d in by for d in DIMENSIONS); passed=complete and all(by[d]["status"]=="PASS" for d in DIMENSIONS)
    result={"schema":"cerebro-system-doctor-receipt/v1","result":"SYSTEM_DOCTOR_PASS" if passed else "SYSTEM_DOCTOR_FAIL","required_dimensions":DIMENSIONS,"dimensions":rows,"complete":complete}
    result["active_path_proof_sha256"]=sha256_hex(canonical({"required_dimensions":DIMENSIONS,"dimensions":rows})) if passed else None
    return result

def main()->int:
    p=argparse.ArgumentParser(); p.add_argument("assure",nargs="?"); p.add_argument("--source-root",required=True); p.add_argument("--kernel-state",required=True); p.add_argument("--trust-key",required=True); p.add_argument("--out")
    args=p.parse_args()
    try: out=assure(Path(args.source_root).resolve(),Path(args.kernel_state).resolve(),Path(args.trust_key).resolve())
    except Exception as e: out={"schema":"cerebro-system-doctor-receipt/v1","result":"SYSTEM_DOCTOR_FAIL","complete":False,"dimensions":[],"failure":str(e),"active_path_proof_sha256":None}
    if args.out: Path(args.out).write_text(json.dumps(out,sort_keys=True,separators=(",",":"))+"\n",encoding="utf-8",newline="\n")
    print(json.dumps(out,sort_keys=True)); return 0 if out.get("result")=="SYSTEM_DOCTOR_PASS" else 3
if __name__=="__main__": raise SystemExit(main())
