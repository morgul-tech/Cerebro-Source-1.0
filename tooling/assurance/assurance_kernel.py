#!/usr/bin/env python3
from __future__ import annotations
import argparse, contextlib, hashlib, hmac, json, os, re, tempfile, time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

PERMIT_SCHEMA = "cerebro-assurance-kernel-permit/v1"
RECEIPT_SCHEMA = "cerebro-assurance-kernel-receipt/v1"
STATE_SCHEMA = "cerebro-assurance-kernel-state/v1"
TRUST_OBJECT_SCHEMA = "cerebro-doctor-trust-object/v1"
TRUST_ATTESTATION_SCHEMA = "cerebro-doctor-trust-attestation/v1"
STATES = {"UNINITIALIZED", "BOOTSTRAP_ONLY", "DOCTOR_ENFORCED", "FAILED_RECOVERY"}
BOOTSTRAP_PACKAGE_CLASS = "DOCTOR_BOOTSTRAP_PACKAGE"
HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

class AssuranceDenied(RuntimeError):
    pass

def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def file_sha256(path: str | Path) -> str:
    return sha256_hex(Path(path).read_bytes())

def touched_paths_fingerprint(paths: list[str]) -> str:
    normalized=[]; seen=set()
    for raw in paths:
        p=str(raw).replace("\\", "/").strip("/")
        if not p: raise AssuranceDenied("TOUCHED_PATH_EMPTY")
        if p in seen: raise AssuranceDenied("TOUCHED_PATH_DUPLICATE:"+p)
        seen.add(p); normalized.append(p)
    normalized.sort(); return sha256_hex(canonical(normalized))

def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix=".assurance-kernel-",suffix=".json",dir=str(path.parent))
    try:
        with os.fdopen(fd,"w",encoding="utf-8",newline="\n") as f:
            json.dump(value,f,sort_keys=True,separators=(",",":"),ensure_ascii=False); f.write("\n"); f.flush(); os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

@contextlib.contextmanager
def _exclusive_lock(lock_path: Path, *, retries: int=200, delay: float=0.025) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True,exist_ok=True); f=open(lock_path,"a+b"); f.seek(0,os.SEEK_END)
    if f.tell()==0: f.write(b"0"); f.flush(); os.fsync(f.fileno())
    acquired=False
    try:
        for _ in range(retries):
            try:
                if os.name=="nt":
                    import msvcrt; f.seek(0); msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl; fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                acquired=True; break
            except (OSError,BlockingIOError): time.sleep(delay)
        if not acquired: raise AssuranceDenied("KERNEL_STATE_LOCK_TIMEOUT")
        yield
    finally:
        if acquired:
            try:
                if os.name=="nt":
                    import msvcrt; f.seek(0); msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
                else:
                    import fcntl; fcntl.flock(f.fileno(),fcntl.LOCK_UN)
            except OSError: pass
        f.close()

@dataclass(frozen=True)
class MaterialIntent:
    source_head: str
    package_sha256: str
    touched_paths_sha256: str
    package_class: str
    campaign_id: str
    authority_epoch: int

class AssuranceKernel:
    def __init__(self,state_path:Path): self.state_path=state_path; self.lock_path=state_path.with_name(state_path.name+".lock")

    def _read(self)->dict[str,Any]:
        if not self.state_path.exists(): return {"schema":STATE_SCHEMA,"state":"UNINITIALIZED","authority_epoch":1,"consumed":[]}
        data=json.loads(self.state_path.read_text(encoding="utf-8"))
        if data.get("schema")!=STATE_SCHEMA: raise AssuranceDenied("KERNEL_STATE_SCHEMA_INVALID")
        if data.get("state") not in STATES: raise AssuranceDenied("KERNEL_STATE_INVALID")
        if not isinstance(data.get("authority_epoch"),int) or data["authority_epoch"]<1: raise AssuranceDenied("KERNEL_EPOCH_INVALID")
        if not isinstance(data.get("consumed"),list) or len(data["consumed"])!=len(set(map(str,data["consumed"]))): raise AssuranceDenied("KERNEL_LEDGER_INVALID")
        if data["state"]=="DOCTOR_ENFORCED":
            for f in ("doctor_active_path_proof_sha256","doctor_trust_key_path","doctor_trust_key_sha256","doctor_verifier_path","doctor_verifier_sha256"):
                if data.get(f) in (None,""): raise AssuranceDenied("DOCTOR_BINDING_STATE_MISSING:"+f)
            for f in ("doctor_active_path_proof_sha256","doctor_trust_key_sha256","doctor_verifier_sha256"):
                if not HEX64_RE.fullmatch(str(data[f])): raise AssuranceDenied("DOCTOR_BINDING_STATE_INVALID:"+f)
        return data

    def initialize_bootstrap(self,*,external_anchor_proof:str,authority_epoch:int=1)->dict[str,Any]:
        with _exclusive_lock(self.lock_path):
            state=self._read()
            if state["state"]!="UNINITIALIZED": raise AssuranceDenied("INITIALIZE_FROM_NONINITIAL_STATE")
            if len(external_anchor_proof)<32: raise AssuranceDenied("EXTERNAL_ANCHOR_PROOF_INVALID")
            if authority_epoch<1: raise AssuranceDenied("AUTHORITY_EPOCH_INVALID")
            new={"schema":STATE_SCHEMA,"state":"BOOTSTRAP_ONLY","authority_epoch":authority_epoch,"consumed":[],"anchor_proof_sha256":sha256_hex(external_anchor_proof.encode("utf-8"))}
            _atomic_json(self.state_path,new); return new

    @staticmethod
    def _validate_permit(permit:dict[str,Any])->None:
        required=("permit_id","campaign_id","package_class","source_pre_head","package_sha256","touched_paths_sha256","nonce","authority_epoch")
        if permit.get("schema")!=PERMIT_SCHEMA: raise AssuranceDenied("PERMIT_SCHEMA_INVALID")
        for f in required:
            if permit.get(f) in (None,""): raise AssuranceDenied("PERMIT_FIELD_MISSING:"+f)
        if not HEX40_RE.fullmatch(str(permit["source_pre_head"])): raise AssuranceDenied("SOURCE_HEAD_FORMAT_INVALID")
        for f in ("package_sha256","touched_paths_sha256"):
            if not HEX64_RE.fullmatch(str(permit[f])): raise AssuranceDenied("SHA256_FORMAT_INVALID:"+f)
        if len(str(permit["nonce"]))<16: raise AssuranceDenied("NONCE_TOO_SHORT")
        if not isinstance(permit["authority_epoch"],int) or permit["authority_epoch"]<1: raise AssuranceDenied("AUTHORITY_EPOCH_INVALID")
        doctor_hash=permit.get("doctor_receipt_sha256")
        if doctor_hash not in (None,"") and not HEX64_RE.fullmatch(str(doctor_hash)): raise AssuranceDenied("DOCTOR_RECEIPT_SHA256_INVALID")
        trust_path=permit.get("doctor_trust_object_path")
        if trust_path not in (None,"") and not isinstance(trust_path,str): raise AssuranceDenied("DOCTOR_TRUST_OBJECT_PATH_INVALID")

    @staticmethod
    def _validate_intent(intent:MaterialIntent)->None:
        if not HEX40_RE.fullmatch(str(intent.source_head)): raise AssuranceDenied("INTENT_SOURCE_HEAD_INVALID")
        if not HEX64_RE.fullmatch(str(intent.package_sha256)): raise AssuranceDenied("INTENT_PACKAGE_SHA256_INVALID")
        if not HEX64_RE.fullmatch(str(intent.touched_paths_sha256)): raise AssuranceDenied("INTENT_PATHS_SHA256_INVALID")
        if not intent.package_class or not intent.campaign_id: raise AssuranceDenied("INTENT_IDENTITY_MISSING")
        if not isinstance(intent.authority_epoch,int) or intent.authority_epoch<1: raise AssuranceDenied("INTENT_AUTHORITY_EPOCH_INVALID")

    @staticmethod
    def _trust_fingerprint_payload(trust:dict[str,Any])->dict[str,Any]:
        x=dict(trust); x.pop("trust_object_fingerprint",None); return x
    @staticmethod
    def _trust_semantic_core(trust:dict[str,Any])->dict[str,Any]:
        return {k:v for k,v in trust.items() if k not in {"attestation","trust_object_fingerprint"}}
    @staticmethod
    def _attestation_unsigned(att:dict[str,Any])->dict[str,Any]:
        x=dict(att); x.pop("signature",None); return x

    def _verify_doctor_trust(self,state:dict[str,Any],permit:dict[str,Any],intent:MaterialIntent)->dict[str,Any]:
        receipt_fp=str(permit.get("doctor_receipt_sha256") or "")
        if not HEX64_RE.fullmatch(receipt_fp): raise AssuranceDenied("DOCTOR_RECEIPT_REQUIRED")
        trust_path=str(permit.get("doctor_trust_object_path") or "")
        if not trust_path: raise AssuranceDenied("DOCTOR_TRUST_OBJECT_REQUIRED")
        key_path=Path(str(state["doctor_trust_key_path"])); verifier_path=Path(str(state["doctor_verifier_path"])); tpath=Path(trust_path)
        if not key_path.is_file(): raise AssuranceDenied("DOCTOR_TRUST_KEY_MISSING")
        if not verifier_path.is_file(): raise AssuranceDenied("DOCTOR_VERIFIER_MISSING")
        if file_sha256(key_path)!=str(state["doctor_trust_key_sha256"]): raise AssuranceDenied("DOCTOR_TRUST_KEY_DRIFT")
        if file_sha256(verifier_path)!=str(state["doctor_verifier_sha256"]): raise AssuranceDenied("DOCTOR_VERIFIER_DRIFT")
        if not tpath.is_file(): raise AssuranceDenied("DOCTOR_TRUST_OBJECT_MISSING")
        try: trust=json.loads(tpath.read_text(encoding="utf-8"))
        except Exception as e: raise AssuranceDenied("DOCTOR_TRUST_OBJECT_INVALID_JSON") from e
        if trust.get("schema")!=TRUST_OBJECT_SCHEMA or trust.get("state")!="CERTIFIED_CURRENT": raise AssuranceDenied("DOCTOR_TRUST_OBJECT_STATE_INVALID")
        att=trust.get("attestation") or {}
        if att.get("schema")!=TRUST_ATTESTATION_SCHEMA or att.get("algorithm")!="HMAC-SHA256" or att.get("validation_result")!="PASS": raise AssuranceDenied("DOCTOR_ATTESTATION_CONTRACT_INVALID")
        if att.get("implementation_sha256")!=str(state["doctor_verifier_sha256"]): raise AssuranceDenied("DOCTOR_ATTESTATION_VERIFIER_MISMATCH")
        if att.get("key_sha256")!=str(state["doctor_trust_key_sha256"]): raise AssuranceDenied("DOCTOR_ATTESTATION_KEY_MISMATCH")
        if att.get("receipt_fingerprint")!=trust.get("receipt_fingerprint"): raise AssuranceDenied("DOCTOR_ATTESTATION_RECEIPT_MISMATCH")
        if int(trust.get("authority_epoch") or 0)!=int(state["authority_epoch"]): raise AssuranceDenied("DOCTOR_TRUST_EPOCH_MISMATCH")
        if trust.get("receipt_fingerprint")!=receipt_fp: raise AssuranceDenied("DOCTOR_TRUST_RECEIPT_MISMATCH")
        subject=trust.get("subject") or {}
        checks=(("source_pre_head",intent.source_head),("package_sha256",intent.package_sha256),("touched_paths_sha256",intent.touched_paths_sha256))
        for field,expected in checks:
            if str(subject.get(field) or "")!=str(expected): raise AssuranceDenied("DOCTOR_TRUST_SUBJECT_MISMATCH:"+field)
        if "SOURCE_PROMOTION" not in list(trust.get("claim_scope") or []): raise AssuranceDenied("DOCTOR_TRUST_CLAIM_SCOPE_DENIED")
        sig=str(att.get("signature") or "")
        if not HEX64_RE.fullmatch(sig): raise AssuranceDenied("DOCTOR_TRUST_SIGNATURE_INVALID")
        expected_sig=hmac.new(key_path.read_bytes(),canonical({"trust":self._trust_semantic_core(trust),"attestation":self._attestation_unsigned(att)}),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig,expected_sig): raise AssuranceDenied("DOCTOR_TRUST_SIGNATURE_INVALID")
        fp=str(trust.get("trust_object_fingerprint") or "")
        if not HEX64_RE.fullmatch(fp) or fp!=sha256_hex(canonical(self._trust_fingerprint_payload(trust))): raise AssuranceDenied("DOCTOR_TRUST_OBJECT_FINGERPRINT_INVALID")
        return trust

    def _check_against_state(self,state:dict[str,Any],permit:dict[str,Any],intent:MaterialIntent)->dict[str,Any]:
        self._validate_permit(permit); self._validate_intent(intent)
        if state["state"] not in {"BOOTSTRAP_ONLY","DOCTOR_ENFORCED"}: raise AssuranceDenied("KERNEL_NOT_ENFORCING")
        if int(permit["authority_epoch"])!=int(state["authority_epoch"]) or intent.authority_epoch!=int(state["authority_epoch"]): raise AssuranceDenied("STALE_AUTHORITY_EPOCH")
        pairs={"campaign_id":intent.campaign_id,"package_class":intent.package_class,"source_pre_head":intent.source_head,"package_sha256":intent.package_sha256,"touched_paths_sha256":intent.touched_paths_sha256}
        for key,actual in pairs.items():
            if str(permit[key])!=str(actual): raise AssuranceDenied("BINDING_MISMATCH:"+key)
        consumption_id=sha256_hex(canonical({"permit_id":permit["permit_id"],"nonce":permit["nonce"],"intent":pairs,"epoch":intent.authority_epoch}))
        if consumption_id in state["consumed"]: raise AssuranceDenied("PERMIT_REPLAY")
        if state["state"]=="BOOTSTRAP_ONLY":
            if intent.package_class!=BOOTSTRAP_PACKAGE_CLASS: raise AssuranceDenied("BOOTSTRAP_PACKAGE_CLASS_DENIED")
            if len(state["consumed"])>=1: raise AssuranceDenied("BOOTSTRAP_CONSUMPTION_EXHAUSTED")
            trust_fp=None
        else:
            trust=self._verify_doctor_trust(state,permit,intent); trust_fp=trust["trust_object_fingerprint"]
        out={"schema":RECEIPT_SCHEMA,"result":"ALLOW","reason":"CURRENT_EXACT_PERMIT","permit_id":str(permit["permit_id"]),"campaign_id":intent.campaign_id,"package_sha256":intent.package_sha256,"authority_epoch":intent.authority_epoch,"consumption_id":consumption_id,"kernel_state":state["state"]}
        if trust_fp: out["doctor_trust_object_fingerprint"]=trust_fp
        return out

    def check(self,permit:dict[str,Any],intent:MaterialIntent)->dict[str,Any]: return self._check_against_state(self._read(),permit,intent)
    def consume(self,permit:dict[str,Any],intent:MaterialIntent)->dict[str,Any]:
        with _exclusive_lock(self.lock_path):
            current=self._read(); receipt=self._check_against_state(current,permit,intent); current["consumed"]=list(current["consumed"])+[receipt["consumption_id"]]; _atomic_json(self.state_path,current); return receipt

    def transition_doctor_enforced(self,*,active_path_proof_sha256:str,expected_epoch:int,trust_key_path:str,doctor_verifier_path:str)->dict[str,Any]:
        with _exclusive_lock(self.lock_path):
            state=self._read()
            if state["state"]!="BOOTSTRAP_ONLY": raise AssuranceDenied("DOCTOR_TRANSITION_FROM_INVALID_STATE")
            if len(state["consumed"])!=1: raise AssuranceDenied("DOCTOR_BOOTSTRAP_NOT_EXACTLY_ONCE")
            if not HEX64_RE.fullmatch(active_path_proof_sha256): raise AssuranceDenied("DOCTOR_ACTIVE_PATH_PROOF_INVALID")
            if int(state["authority_epoch"])!=expected_epoch: raise AssuranceDenied("STALE_AUTHORITY_EPOCH")
            kp=Path(trust_key_path); vp=Path(doctor_verifier_path)
            if not kp.is_file() or len(kp.read_bytes())<32: raise AssuranceDenied("DOCTOR_TRUST_KEY_INVALID")
            if not vp.is_file(): raise AssuranceDenied("DOCTOR_VERIFIER_INVALID")
            state["state"]="DOCTOR_ENFORCED"; state["authority_epoch"]=expected_epoch+1; state["doctor_active_path_proof_sha256"]=active_path_proof_sha256
            state["doctor_trust_key_path"]=str(kp.resolve()); state["doctor_trust_key_sha256"]=file_sha256(kp)
            state["doctor_verifier_path"]=str(vp.resolve()); state["doctor_verifier_sha256"]=file_sha256(vp)
            _atomic_json(self.state_path,state); return state

    def transition_failed_recovery(self,*,reason_sha256:str,expected_epoch:int)->dict[str,Any]:
        with _exclusive_lock(self.lock_path):
            state=self._read()
            if state["state"] not in {"BOOTSTRAP_ONLY","DOCTOR_ENFORCED"}: raise AssuranceDenied("FAILED_RECOVERY_FROM_INVALID_STATE")
            if not HEX64_RE.fullmatch(reason_sha256): raise AssuranceDenied("RECOVERY_REASON_SHA256_INVALID")
            if int(state["authority_epoch"])!=expected_epoch: raise AssuranceDenied("STALE_AUTHORITY_EPOCH")
            state["state"]="FAILED_RECOVERY"; state["authority_epoch"]=expected_epoch+1; state["recovery_reason_sha256"]=reason_sha256; _atomic_json(self.state_path,state); return state

def deny_receipt(reason:str)->dict[str,Any]: return {"schema":RECEIPT_SCHEMA,"result":"DENY","reason":reason}
def _load(path:str)->dict[str,Any]: return json.loads(Path(path).read_text(encoding="utf-8"))

def intent_from_manifest(manifest_path:str,source_head:str)->MaterialIntent:
    path=Path(manifest_path); raw=path.read_bytes(); manifest=json.loads(raw.decode("utf-8")); binding=manifest.get("assurance_kernel")
    if not isinstance(binding,dict): raise AssuranceDenied("MANIFEST_ASSURANCE_BINDING_MISSING")
    files=manifest.get("files")
    if not isinstance(files,list) or not files: raise AssuranceDenied("MANIFEST_FILES_MISSING")
    paths=[]
    for entry in files:
        if not isinstance(entry,dict) or not str(entry.get("path") or "").strip(): raise AssuranceDenied("MANIFEST_FILE_PATH_INVALID")
        paths.append(str(entry["path"]))
    try: epoch=int(binding["authority_epoch"])
    except Exception as e: raise AssuranceDenied("MANIFEST_AUTHORITY_EPOCH_INVALID") from e
    return MaterialIntent(source_head=source_head,package_sha256=sha256_hex(raw),touched_paths_sha256=touched_paths_fingerprint(paths),package_class=str(binding.get("package_class") or ""),campaign_id=str(binding.get("campaign_id") or ""),authority_epoch=epoch)

def _intent(args:argparse.Namespace)->MaterialIntent: return MaterialIntent(args.source_head,args.package_sha256,args.touched_paths_sha256,args.package_class,args.campaign_id,args.authority_epoch)

def main()->int:
    p=argparse.ArgumentParser(); p.add_argument("--state",required=True); sub=p.add_subparsers(dest="cmd",required=True)
    i=sub.add_parser("initialize-bootstrap"); i.add_argument("--anchor-proof",required=True); i.add_argument("--authority-epoch",type=int,default=1)
    for name in ("check-permit","consume-permit"):
        q=sub.add_parser(name); q.add_argument("--permit",required=True); q.add_argument("--source-head",required=True); q.add_argument("--package-sha256",required=True); q.add_argument("--touched-paths-sha256",required=True); q.add_argument("--package-class",required=True); q.add_argument("--campaign-id",required=True); q.add_argument("--authority-epoch",type=int,required=True)
    for name in ("check-manifest-permit","consume-manifest-permit"):
        q=sub.add_parser(name); q.add_argument("--permit",required=True); q.add_argument("--manifest",required=True); q.add_argument("--source-head",required=True)
    t=sub.add_parser("doctor-enforced"); t.add_argument("--active-path-proof-sha256",required=True); t.add_argument("--expected-epoch",type=int,required=True); t.add_argument("--trust-key",required=True); t.add_argument("--doctor-verifier",required=True)
    r=sub.add_parser("failed-recovery"); r.add_argument("--reason-sha256",required=True); r.add_argument("--expected-epoch",type=int,required=True)
    args=p.parse_args(); k=AssuranceKernel(Path(args.state))
    try:
        if args.cmd=="initialize-bootstrap": out=k.initialize_bootstrap(external_anchor_proof=args.anchor_proof,authority_epoch=args.authority_epoch)
        elif args.cmd=="check-permit": out=k.check(_load(args.permit),_intent(args))
        elif args.cmd=="consume-permit": out=k.consume(_load(args.permit),_intent(args))
        elif args.cmd=="check-manifest-permit": out=k.check(_load(args.permit),intent_from_manifest(args.manifest,args.source_head))
        elif args.cmd=="consume-manifest-permit": out=k.consume(_load(args.permit),intent_from_manifest(args.manifest,args.source_head))
        elif args.cmd=="doctor-enforced": out=k.transition_doctor_enforced(active_path_proof_sha256=args.active_path_proof_sha256,expected_epoch=args.expected_epoch,trust_key_path=args.trust_key,doctor_verifier_path=args.doctor_verifier)
        else: out=k.transition_failed_recovery(reason_sha256=args.reason_sha256,expected_epoch=args.expected_epoch)
        print(json.dumps(out,sort_keys=True)); return 0
    except (AssuranceDenied,OSError,ValueError) as e:
        print(json.dumps(deny_receipt(str(e)),sort_keys=True)); return 3

if __name__=="__main__": raise SystemExit(main())
