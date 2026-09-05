#!/usr/bin/env python3
from __future__ import annotations
import importlib.util,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
IMPL=ROOT/"engines/presentation/live_shadow_projection.py"
def load():
 spec=importlib.util.spec_from_file_location("cerebro_live_shadow_projection",IMPL); m=importlib.util.module_from_spec(spec); sys.modules[spec.name]=m; spec.loader.exec_module(m); return m
def run_all():
 m=load(); tests=[]
 def check(n,c): tests.append({"name":n,"result":"PASS" if c else "FAIL"})
 h=lambda x: x*64
 ev={"ref":"R2REC-1","fingerprint":h("a"),"schema":"cerebro-runtime2-terminal-receipt/v1"}
 basis=[{"owner_ref":"runtime2","stream_ref":"runtime/event","revision":7,"fingerprint":h("b"),"currentness":"CURRENT"}]
 p=m.build_projection(stream_ref="native/live-shadow",stream_revision=7,runtime_evidence=ev,basis_set=basis)
 check("current_basis_projects_current",p["projection_state"]=="CURRENT")
 check("presentation_only_non_authoritative",p["authority"]=="PRESENTATION_ONLY_NON_AUTHORITATIVE")
 check("deterministic_projection_fingerprint",p==m.build_projection(stream_ref="native/live-shadow",stream_revision=7,runtime_evidence=ev,basis_set=list(reversed(basis))))
 check("same_revision_same_fingerprint_idempotent",m.reconcile(p,p).get("effect")=="IDEMPOTENT_NO_EFFECT")
 q=m.build_projection(stream_ref="native/live-shadow",stream_revision=7,runtime_evidence={**ev,"fingerprint":h("c")},basis_set=basis)
 check("same_revision_different_fingerprint_blocks",m.reconcile(p,q).get("classification")=="SAME_REVISION_FINGERPRINT_COLLISION")
 old=m.build_projection(stream_ref="native/live-shadow",stream_revision=6,runtime_evidence=ev,basis_set=basis)
 check("stale_out_of_order_blocks",m.reconcile(p,old).get("classification")=="STALE_OR_OUT_OF_ORDER")
 for state in ("STALE","GAP","UNKNOWN","PROVISIONAL"):
  b=[{**basis[0],"currentness":state}]; x=m.build_projection(stream_ref="native/live-shadow",stream_revision=8,runtime_evidence=ev,basis_set=b); check("state_"+state.lower()+"_preserved",x["projection_state"]==state)
 nr=m.build_projection(stream_ref="native/live-shadow",stream_revision=8,runtime_evidence=ev,basis_set=basis,delivery_state="NO_RESPONSE")
 rej=m.build_projection(stream_ref="native/live-shadow",stream_revision=8,runtime_evidence=ev,basis_set=basis,delivery_state="REJECTED")
 fail=m.build_projection(stream_ref="native/live-shadow",stream_revision=8,runtime_evidence=ev,basis_set=basis,delivery_state="FAIL")
 check("no_response_distinct_from_rejected",nr["delivery_state"]!=rej["delivery_state"])
 check("no_response_distinct_from_fail",nr["delivery_state"]!=fail["delivery_state"])
 unk=m.build_projection(stream_ref="native/live-shadow",stream_revision=8,runtime_evidence=ev,basis_set=[{**basis[0],"currentness":"UNKNOWN"}],delivery_state="OBSERVED")
 check("unknown_not_fail",unk["projection_state"]=="UNKNOWN" and unk["delivery_state"]!="FAIL")
 check("basis_set_required",bool(p["basis_set"]) and all("revision" in x and "fingerprint" in x for x in p["basis_set"]))
 check("no_writer_or_store_surface",not any(hasattr(m,n) for n in ("write","commit","save","persist","database","scheduler")))
 check("runtime_evidence_exact_identity_required",len(p["runtime_evidence"]["fingerprint"])==64)
 return {"schema":"cerebro-native-live-shadow-projection-validation/v1","result":"PASS" if all(x["result"]=="PASS" for x in tests) else "FAIL","tests":tests,"test_count":len(tests),"pass_count":sum(x["result"]=="PASS" for x in tests)}
if __name__=="__main__":
 out=run_all(); print(json.dumps(out,indent=2)); raise SystemExit(0 if out["result"]=="PASS" else 2)
