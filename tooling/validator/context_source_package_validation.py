#!/usr/bin/env python3
from __future__ import annotations
import argparse, copy, json, os, pathlib, subprocess, sys, tempfile
import yaml

ROOT=pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"tooling"/"builder"))
from context_source_package import PackageError, build, load_contract, verify

def check(name, cond, rows):
    rows.append({"name":name,"result":"PASS" if cond else "FAIL"})

def installed_read(package:pathlib.Path, source_revision:str)->dict:
    script=r'''
import copy, json, pathlib, sys
pkg=pathlib.Path(sys.argv[1])
source_revision=sys.argv[2]
sys.path[:0]=[str(pkg/"mcp"),str(pkg/"tooling"/"context"),str(pkg/"tooling"/"validator")]
from control_context_registry import bootstrap_pre_role_generation, complete_pre_role_generation
from control_context_tools import ControlContextMcpTools, VerifiedMcpIdentity
from control_context_remote_service import ControlContextRemoteMcpService, RemoteMcpServiceConfig, VerifiedBearerToken

generation="CEREBRO-BOOT-PACKAGE-READ-0001"
state=bootstrap_pre_role_generation(
 tenant_ref="TENANT-PKG",workspace_ref="WORKSPACE-PKG",generation_ref=generation,
 source_revision=source_revision,method_ref="CURRENT_CIVILIZATION_METHOD_PROFILE",
 method_version="1.0",method_fingerprint="1"*64,provider_frontier_ref="PKG-R0",provider_revision=1)
state=complete_pre_role_generation(state,source_revision=source_revision,
 generic_capability_canaries=[{"canary_ref":"PKG-READ","result":"PASS","evidence_ref":"PKG-EVIDENCE"}],
 receipt_ref="READY-PKG-READ-1")
class StatePort:
 def __init__(self,row): self.row=copy.deepcopy(row); self.reads=0; self.writes=0
 def read_pre_role_generation(self,tenant_ref,workspace_ref,generation_ref,principal_ref=None,scopes=None):
  self.reads+=1
  if tenant_ref!="TENANT-PKG" or workspace_ref!="WORKSPACE-PKG" or generation_ref!=generation:
   raise RuntimeError("unexpected-read-scope")
  if principal_ref!="PRINCIPAL-PKG" or scopes!={"project_state:read"}:
   raise RuntimeError("unexpected-read-authority")
  return copy.deepcopy(self.row)
class Attestor:
 def verify(self,**kw): return None
class TokenVerifier:
 def verify(self,token):
  if token!="reader-token": raise RuntimeError("bad-token")
  return VerifiedBearerToken(claims={
   "iss":"https://issuer.invalid","aud":"https://context.invalid","exp":2000003600,
   "scope":"project_state:read","sub":"PRINCIPAL-PKG",
   "cerebro_tenant":"TENANT-PKG","cerebro_workspace":"WORKSPACE-PKG"},signature_verified=True)
port=StatePort(state)
tools=ControlContextMcpTools(port,Attestor())
cfg=RemoteMcpServiceConfig(
 resource="https://context.invalid",authorization_servers=("https://issuer.invalid",),
 resource_documentation="https://docs.invalid/context",clock_skew_seconds=0)
service=ControlContextRemoteMcpService(config=cfg,tools=tools,token_verifier=TokenVerifier(),
 readiness_probe=lambda:True,clock=lambda:2000000000)
before=copy.deepcopy(port.row)
out=service.invoke(tool_name="read_boot_generation_state",args={"generation_ref":generation},
 headers={"Authorization":"Bearer reader-token"},request_meta={"openai/session":"PKG-SESSION"})
after=copy.deepcopy(port.row)
bad=service.invoke(tool_name="read_boot_generation_state",args={"generation_ref":generation},
 headers={"Authorization":"Bearer reader-token"},request_meta={"openai/session":"PKG-SESSION"})
result={
 "generation_ref":out["structuredContent"]["pre_role_generation"]["generation_ref"],
 "lifecycle":out["structuredContent"]["pre_role_generation"]["lifecycle"],
 "authenticated_binding":out["structuredContent"]["authenticated_binding"],
 "reads":port.reads,"writes":port.writes,"state_unchanged":before==after,
 "repeat_same_generation":bad["structuredContent"]["pre_role_generation"]["fingerprint"]==
                         out["structuredContent"]["pre_role_generation"]["fingerprint"]}
print(json.dumps(result))
'''
    env=dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"]="1"
    cp=subprocess.run([sys.executable,"-c",script,str(package),source_revision],
        cwd=package,text=True,capture_output=True,env=env)
    if cp.returncode: raise RuntimeError("installed-read-failed:"+cp.stderr[-1000:])
    return json.loads(cp.stdout)

def build_blocks_mutant(mutator)->str:
    target=ROOT/"mcp/control_context_mcp_sdk.py"
    original=target.read_bytes()
    created=[]
    try:
        mutator(target,created)
        with tempfile.TemporaryDirectory() as td:
            try:
                build(pathlib.Path(td)/"pkg",root=ROOT)
            except PackageError as exc:
                return str(exc)
            raise AssertionError("mutant-build-unexpected-pass")
    finally:
        target.write_bytes(original)
        for path in created:
            if path.exists():
                path.unlink()

def unresolved_mutant(target,created):
    with target.open("a",encoding="utf-8",newline="\n") as f:
        f.write("\nimport definitely_missing_local_module\n")

def ambiguous_mutant(target,created):
    left=ROOT/"mcp/localdup.py"
    right=ROOT/"tooling/localdup.py"
    left.write_text("VALUE='mcp'\n",encoding="utf-8",newline="\n")
    right.write_text("VALUE='tooling'\n",encoding="utf-8",newline="\n")
    created.extend([left,right])
    with target.open("a",encoding="utf-8",newline="\n") as f:
        f.write("\nimport localdup\n")

def selftest()->dict:
    rows=[]
    contract=load_contract(ROOT)
    head=subprocess.check_output(["git","-C",str(ROOT),"rev-parse","HEAD"],text=True).strip()
    check("contract-read-tool-is-read-only",
          contract["required_tool"]["name"]=="read_boot_generation_state"
          and contract["required_tool"]["required_scope"]=="project_state:read"
          and contract["required_tool"]["state_mutation"] is False,rows)
    check("PR7-authority-not-in-reader-profile",
          "mcp/worker_attach_grant.py" in contract["forbidden_package_paths"],rows)
    check("contract-enables-unresolved-local-import-fail-closed",
          contract["dependency_policy"].get("forbid_unresolved_local_import") is True,rows)
    check("contract-governs-external-reader-imports",
          set(contract["dependency_policy"].get("governed_external_imports") or ())
          == {"mcp","starlette","yaml"},rows)
    unresolved=build_blocks_mutant(unresolved_mutant)
    check("unresolved-local-import-mutant-blocks-build",
          unresolved.startswith("unresolved-local-import:")
          and "definitely_missing_local_module" in unresolved,rows)
    ambiguous=build_blocks_mutant(ambiguous_mutant)
    check("ambiguous-local-import-mutant-blocks-build",
          ambiguous.startswith("ambiguous-local-import:")
          and "mcp/localdup.py" in ambiguous
          and "tooling/localdup.py" in ambiguous,rows)
    with tempfile.TemporaryDirectory() as td:
        pkg=pathlib.Path(td)/"pkg"
        manifest=build(pkg,root=ROOT,expected_source_revision=head)
        vr=verify(pkg,root=ROOT)
        paths={x["path"] for x in manifest["files"]}
        check("dependency-closure-has-reader-service-sdk",
              {"mcp/control_context_tools.py","mcp/control_context_remote_service.py",
               "mcp/control_context_mcp_sdk.py","mcp/control-context-mcp-sdk-requirements.txt"}<=paths,rows)
        check("dependency-closure-excludes-deploy-grant-authority",
              "mcp/worker_attach_grant.py" not in paths and "deployment/app.py" not in paths,rows)
        check("package-bytes-match-source",vr["result"]=="PASS",rows)
        read=installed_read(pkg,head)
        check("installed-authenticated-exact-generation-read",
              read["generation_ref"]=="CEREBRO-BOOT-PACKAGE-READ-0001"
              and read["lifecycle"]=="READY_UNBOUND"
              and read["authenticated_binding"]["tenant_ref"]=="TENANT-PKG"
              and read["authenticated_binding"]["workspace_ref"]=="WORKSPACE-PKG"
              and read["authenticated_binding"]["principal_ref"]=="PRINCIPAL-PKG",rows)
        check("installed-read-has-zero-state-mutation",
              read["writes"]==0 and read["state_unchanged"] is True,rows)
        check("installed-read-repeat-is-stable",read["repeat_same_generation"] is True,rows)
    return {"schema":"cerebro-context-source-package-validation/v1",
            "result":"PASS" if all(x["result"]=="PASS" for x in rows) else "FAIL",
            "test_count":len(rows),"failures":[x for x in rows if x["result"]!="PASS"],"tests":rows}
def main()->int:
    ap=argparse.ArgumentParser(); ap.add_argument("command",nargs="?",choices=["selftest"],default="selftest"); ap.parse_args()
    try: r=selftest()
    except Exception as exc: r={"result":"BLOCK","error":str(exc)}
    print(json.dumps(r,indent=2)); return 0 if r.get("result")=="PASS" else 1
if __name__=="__main__": raise SystemExit(main())
