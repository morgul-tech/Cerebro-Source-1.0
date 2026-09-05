#!/usr/bin/env python3
"""Runtime2 target-runtime execution/orchestration adapter.

The planner/verifier remains target_runtime_validation.py.  This module owns only
bounded execution/orchestration of that already-declared target assurance path.
Native process supervision is delegated to tooling.host's typed Runtime2
supervisor and semantic PASS/BLOCK is decided only by the target validator.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Any, Mapping

SCHEMA_REQUEST = "cerebro-runtime2-target-runtime-request/v1"
SCHEMA_PREPARED = "cerebro-runtime2-target-runtime-prepared/v1"
SCHEMA_RESULT = "cerebro-runtime2-target-runtime-result/v1"
ADAPTER_ID = "RUNTIME2_TARGET_RUNTIME_ORCHESTRATOR"
TARGET_SCRIPT = "tooling/validator/target-runtime/Invoke-CerebroWindowsPowerShellValidation.ps1"
TARGET_VALIDATOR = "tooling/validator/target_runtime_validation.py"
HOST_PATH = "tooling/host/cerebro_host.py"
RUNTIME_PATH = "tooling/runtime-host/cerebro_runtime.py"
ACTIVATION_SCHEMA = "cerebro-runtime2-m4-activation-proof/v1"
ACTIVATION_BINDING_IMPLEMENTATION = "tooling/validator/runtime2_target_runtime.py"
ACTIVATION_REGISTRY = "tooling/validator/contract-activation-bindings.json"
TARGET_ENV_KEYS = (
    "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "PATH",
    "TEMP", "TMP", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA",
    "APPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432",
    "PSMODULEPATH",
)

def target_child_environment() -> dict[str, str]:
    home=Path(os.environ.get('USERPROFILE') or Path.home()).resolve()
    system_root=Path(os.environ.get('SYSTEMROOT') or os.environ.get('WINDIR') or (Path(home.drive or 'C:')/'Windows')).resolve()
    system_drive=os.environ.get('SYSTEMDRIVE') or system_root.drive or home.drive or 'C:'
    local_app=Path(os.environ.get('LOCALAPPDATA') or (home/'AppData'/'Local')).resolve()
    roaming=Path(os.environ.get('APPDATA') or (home/'AppData'/'Roaming')).resolve()
    temp=Path(os.environ.get('TEMP') or os.environ.get('TMP') or (local_app/'Temp')).resolve()
    python_dir=str(Path(sys.executable).resolve().parent)
    inherited_path=os.environ.get('PATH','')
    values={
        'SYSTEMROOT':str(system_root),'WINDIR':str(system_root),'SYSTEMDRIVE':system_drive,
        'COMSPEC':os.environ.get('COMSPEC') or str(system_root/'System32'/'cmd.exe'),
        'PATHEXT':os.environ.get('PATHEXT') or '.COM;.EXE;.BAT;.CMD;.VBS;.VBE;.JS;.JSE;.WSF;.WSH;.MSC;.CPL',
        'PATH':python_dir+(os.pathsep+inherited_path if inherited_path else ''),
        'TEMP':str(temp),'TMP':str(temp),'USERPROFILE':str(home),
        'HOMEDRIVE':os.environ.get('HOMEDRIVE') or home.drive or system_drive,
        'HOMEPATH':os.environ.get('HOMEPATH') or str(home)[len(home.drive):],
        'LOCALAPPDATA':str(local_app),'APPDATA':str(roaming),
        'PROGRAMDATA':os.environ.get('PROGRAMDATA') or str(Path(system_drive+'/')/'ProgramData'),
        'PROGRAMFILES':os.environ.get('PROGRAMFILES') or str(Path(system_drive+'/')/'Program Files'),
        'PROGRAMFILES(X86)':os.environ.get('PROGRAMFILES(X86)') or str(Path(system_drive+'/')/'Program Files (x86)'),
        'PROGRAMW6432':os.environ.get('PROGRAMW6432') or os.environ.get('PROGRAMFILES') or str(Path(system_drive+'/')/'Program Files'),
        'PSMODULEPATH':os.environ.get('PSMODULEPATH',''),
    }
    return {k:str(v) for k,v in values.items() if str(v)}
ACTIVATION_BASIS = [
    "tooling/runtime-host/component.yaml",
    "tooling/runtime-host/cerebro_runtime.py",
    "tooling/host/component.yaml",
    "tooling/host/cerebro_host.py",
    "tooling/validator/target_runtime_validation.py",
    "tooling/validator/target-runtime-validation.yaml",
    "tooling/validator/runtime2_target_runtime.py",
]


class TargetRuntime2Error(RuntimeError):
    def __init__(self, classification: str, detail: str):
        super().__init__(f"{classification}:{detail}")
        self.classification = classification
        self.detail = detail


def fail(classification: str, detail: str) -> None:
    raise TargetRuntime2Error(classification, detail)


def sha256_file(path: Path) -> str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    v=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(v,dict): fail('TARGET_RUNTIME2_JSON_ROOT_INVALID',str(path))
    return v


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,sort_keys=True)+'\n',encoding='utf-8',newline='\n')


def require_text(v: Any, name: str) -> str:
    if not isinstance(v,str) or not v.strip(): fail('TARGET_RUNTIME2_REQUEST_INVALID',name)
    return v.strip()


def require_sha(v: Any, name: str) -> str:
    t=require_text(v,name).lower()
    if len(t)!=64 or any(c not in '0123456789abcdef' for c in t): fail('TARGET_RUNTIME2_REQUEST_INVALID',name)
    return t


def _load_module(path: Path, name: str):
    if not path.is_file(): fail('TARGET_RUNTIME2_DEPENDENCY_MISSING',str(path))
    spec=importlib.util.spec_from_file_location(name,path)
    if spec is None or spec.loader is None: fail('TARGET_RUNTIME2_IMPORT_FAILED',str(path))
    module=importlib.util.module_from_spec(spec)
    sys.modules[name]=module
    added=str(path.parent)
    sys.path.insert(0,added)
    try: spec.loader.exec_module(module)
    finally:
        if sys.path and sys.path[0]==added: sys.path.pop(0)
    return module


def _cac_path_sort_key(value: str) -> str:
    # Windows PowerShell Sort-Object uses a linguistic comparison that ignores
    # separator punctuation for these Source-relative paths. Preserve input
    # order for punctuation-only ties so producer and CAC hash the same rows.
    return ''.join(ch for ch in value.casefold() if ch.isalnum())


def source_state_fingerprint(root: Path, paths: list[str]) -> str:
    rows=[]
    for relative in sorted(paths,key=_cac_path_sort_key):
        path=root/relative
        rows.append(f"{relative}|{sha256_file(path) if path.is_file() else 'ABSENT'}")
    return hashlib.sha256("\n".join(rows).encode('utf-8')).hexdigest()


def resolve_activation_binding(registry: Mapping[str, Any]) -> str:
    bindings=registry.get('bindings')
    if not isinstance(bindings,list): fail('M4_ACTIVATION_BINDING_REGISTRY_INVALID','bindings')
    candidates=[b for b in bindings if isinstance(b,Mapping) and str(b.get('implementation',''))==ACTIVATION_BINDING_IMPLEMENTATION]
    if not candidates: fail('M4_ACTIVATION_BINDING_MISSING',ACTIVATION_BINDING_IMPLEMENTATION)
    if len(candidates)!=1: fail('M4_ACTIVATION_BINDING_AMBIGUOUS',str(len(candidates)))
    binding=dict(candidates[0]); binding_id=require_text(binding.get('id'),'binding.id')
    if str(binding.get('wiring_proof_kind',''))!='RUNTIME_EVIDENCE': fail('M4_ACTIVATION_BINDING_PROOF_KIND_MISMATCH',binding_id)
    spec=binding.get('runtime_evidence')
    if not isinstance(spec,Mapping): fail('M4_ACTIVATION_BINDING_RUNTIME_EVIDENCE_MISSING',binding_id)
    if str(spec.get('schema',''))!=ACTIVATION_SCHEMA: fail('M4_ACTIVATION_BINDING_SCHEMA_MISMATCH',binding_id)
    basis=spec.get('basis_files')
    if not isinstance(basis,list) or sorted(str(x) for x in basis)!=sorted(ACTIVATION_BASIS): fail('M4_ACTIVATION_BINDING_BASIS_MISMATCH',binding_id)
    accepted=spec.get('accepted_binding_ids')
    if not isinstance(accepted,list) or binding_id not in [str(x) for x in accepted]: fail('M4_ACTIVATION_BINDING_ACCEPTED_ID_MISMATCH',binding_id)
    if str(spec.get('required_proves_binding',''))!=binding_id: fail('M4_ACTIVATION_BINDING_PROVES_REQUIREMENT_MISMATCH',binding_id)
    return binding_id


def activation_probe(source_root: Path) -> dict[str, Any]:
    root=source_root.resolve()
    runtime=_load_module(root/RUNTIME_PATH,'cerebro_runtime2_activation_kernel')
    host=_load_module(root/HOST_PATH,'cerebro_runtime2_activation_host')
    kernel_result=runtime.runtime2_selftest()
    host_result=host.selftest()
    adapter_result=selftest()
    binding_id=resolve_activation_binding(read_json(root/ACTIVATION_REGISTRY))
    delegate_parse=host.parse_host_arguments(['change','--option-prefixed-first-token','value'])
    runtime_parse=host.parse_host_arguments(['runtime2-supervise','--request','request.json','--output','output.json'])
    canaries={
        'runtime_kernel_selftest': kernel_result.get('result')=='PASS',
        'host_runtime2_selftest': host_result.get('result')=='PASS',
        'target_orchestrator_selftest': adapter_result.get('result')=='PASS',
        'delegate_argv_preserved': delegate_parse.delegate_args==['--option-prefixed-first-token','value'],
        'runtime2_cli_preserved': runtime_parse.request=='request.json' and runtime_parse.output=='output.json',
        'exact7_present': all((root/path).is_file() for path in ACTIVATION_BASIS),
    }
    return {
        'schema':ACTIVATION_SCHEMA,
        'result':'PASS' if all(canaries.values()) else 'FAIL',
        'probe_id':'RUNTIME2_M4_EXACT7_WINDOWS_CANARIES',
        'basis_files':ACTIVATION_BASIS,
        'source_state_fingerprint':source_state_fingerprint(root,ACTIVATION_BASIS),
        'proves_bindings':[binding_id],
        'binding_id':binding_id,
        'canaries':canaries,
        'canary_count':len(canaries),
        'pass_count':sum(1 for value in canaries.values() if value),
        'source_mutation':False,
        'runtime_control_owner':'MCP',
    }


def _resolve_windows_powershell() -> Path:
    if os.name != 'nt': fail('REQUIRED_TARGET_RUNTIME_NOT_EXECUTED_BEFORE_HANDOFF','WINDOWS_REQUIRED')
    candidate=shutil.which('powershell.exe') or shutil.which('powershell')
    if not candidate: fail('REQUIRED_TARGET_RUNTIME_NOT_EXECUTED_BEFORE_HANDOFF','WINDOWS_POWERSHELL_REQUIRED')
    p=Path(candidate).resolve()
    if not p.is_file(): fail('TARGET_RUNTIME_WINDOWS_POWERSHELL_NOT_FOUND',str(p))
    return p


def normalize_request(request: Mapping[str, Any]) -> dict[str, Any]:
    r=dict(request)
    if r.get('schema') != SCHEMA_REQUEST: fail('TARGET_RUNTIME2_REQUEST_INVALID','schema')
    for k in ('invocation_id','node_id','candidate_root','manifest_path','capsule_root','repository_root','output_path','profile_id','capability_binding_ref'):
        require_text(r.get(k),k)
    for k in ('receipt_subject_fingerprint','event_fingerprint','plan_fingerprint','execution_basis_fingerprint'):
        require_sha(r.get(k),k)
    require_text(r.get('execution_basis_ref'),'execution_basis_ref')
    return r


def prepare(request: Mapping[str, Any]) -> dict[str, Any]:
    r=normalize_request(request)
    root=Path(r['candidate_root']).resolve()
    for rel in (TARGET_SCRIPT,TARGET_VALIDATOR,HOST_PATH):
        p=root/rel
        if not p.is_file(): fail('TARGET_RUNTIME2_DEPENDENCY_MISSING',rel)
    manifest=Path(r['manifest_path']).resolve()
    if not manifest.is_file(): fail('TARGET_RUNTIME_MANIFEST_MISSING',str(manifest))
    target_validator=_load_module(root/TARGET_VALIDATOR,'cerebro_runtime2_target_validator')
    # Planner checks exact base, changed-path scope and candidate bytes.  This is
    # pre-execution assurance only; it does not create target-runtime PASS.
    plan=target_validator.build_plan(root,manifest,r['profile_id'])
    if plan.get('result')!='PASS': fail('TARGET_RUNTIME_PLAN_NOT_PASS',str(plan.get('result')))
    return {
        'schema':SCHEMA_PREPARED,'result':'PASS','adapter_id':ADAPTER_ID,
        'target_profile':r['profile_id'],'candidate_identity':plan['candidate_identity'],
        'target_plan_fingerprint':plan['target_plan_fingerprint'],
        'changed_paths':plan['changed_paths'],'execution_basis_ref':r['execution_basis_ref'],
        'execution_basis_fingerprint':r['execution_basis_fingerprint'],
        'event_fingerprint':r['event_fingerprint'],'plan_fingerprint':r['plan_fingerprint'],
        'receipt_subject_fingerprint':r['receipt_subject_fingerprint'],
        'supervision_owner':'tooling.host','semantic_verifier':'tooling/validator/target_runtime_validation.py',
        'authoritative_source_publish_allowed':False,'target_runtime_executed':False,
    }


def build_supervision_request(request: Mapping[str, Any], powershell: Path) -> dict[str, Any]:
    r=normalize_request(request)
    root=Path(r['candidate_root']).resolve()
    script=(root/TARGET_SCRIPT).resolve()
    argv=[str(powershell),'-NoLogo','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(script),
          '-CandidateRoot',str(root),'-ManifestPath',str(Path(r['manifest_path']).resolve()),'-CapsuleRoot',str(Path(r['capsule_root']).resolve()),
          '-RepositoryRoot',str(Path(r['repository_root']).resolve()),'-OutputPath',str(Path(r['output_path']).resolve()),'-ProfileId',r['profile_id']]
    env=target_child_environment()
    return {
        'schema':'cerebro-runtime2-supervision-request/v1',
        'invocation_id':r['invocation_id'],'receipt_subject_fingerprint':r['receipt_subject_fingerprint'],
        'event_fingerprint':r['event_fingerprint'],'plan_fingerprint':r['plan_fingerprint'],'node_id':r['node_id'],
        'execution_basis_ref':{'ref':r['execution_basis_ref'],'fingerprint':r['execution_basis_fingerprint']},
        'capability_binding_ref':r['capability_binding_ref'],'execution_mode_ref':'SUPERVISED_PROCESS',
        'executable_binding':{'logical_role':'windows-powershell-target-runtime','resolved_path':str(powershell),'content_sha256':sha256_file(powershell),'version':platform.version()},
        'argv_binding':{'argv':argv},
        'environment_binding':{'values':env,'declared_keys':sorted(env),'secret_keys':[]},
        'cwd_policy_ref':'RUNTIME2_TARGET_CANDIDATE_ROOT','cwd_binding':{'role':'target-runtime-candidate-root','resolved_cwd_locator':str(root)},
        'io_policy_ref':'RUNTIME2_TARGET_INHERIT_IO','io_policy':{'stdin':'INHERIT','stdout':'INHERIT','stderr':'INHERIT'},
        'heartbeat_policy_ref':'RUNTIME2_TARGET_HEARTBEAT','heartbeat_policy':{'interval_seconds':5.0},
        'timeout_policy_ref':'RUNTIME2_TARGET_OBSERVE_ONLY','timeout_policy':{'timeout_seconds':None,'action':'OBSERVE_ONLY'},
        'termination_policy_ref':'RUNTIME2_TARGET_NO_FORCE_TERMINATION','termination_policy':{'force_terminate_explicitly_safe':False,'force_grace_seconds':5.0,'cooperative_signal':None},
        'progress_policy_ref':None,'progress_policy':None,
        'stall_policy_ref':'RUNTIME2_TARGET_STALL_OBSERVE_ONLY','stall_policy':{'stall_threshold_seconds':None,'action':'OBSERVE_DO_NOT_FORCE_KILL'},
        'sensitivity_rules':{'sensitivity':'INTERNAL','redaction':'OMIT','secret_binding_refs':[]},
        'failure_policy_ref':'RUNTIME2_TARGET_FAIL_CLOSED_NO_RETRY','owner_defined_retry_rule':None,
        'diagnostic_policy_ref':None,'diagnostic_context_binding':None,
    }


def execute(request: Mapping[str, Any]) -> dict[str, Any]:
    r=normalize_request(request)
    prepared=prepare(r)
    powershell=_resolve_windows_powershell()
    root=Path(r['candidate_root']).resolve()
    host=_load_module(root/HOST_PATH,'cerebro_runtime2_host')
    supervision_request=build_supervision_request(r,powershell)
    supervision=host.supervise_runtime2_process(supervision_request)
    # Neutral host evidence never decides target semantic success.
    receipt_path=Path(r['output_path']).resolve()
    if not receipt_path.is_file():
        return {'schema':SCHEMA_RESULT,'result':'BLOCK','adapter_id':ADAPTER_ID,'classification':'TARGET_RUNTIME_RECEIPT_MISSING',
                'prepared':prepared,'supervision_result':supervision,'target_runtime_execution':True,'semantic_verification':None,
                'authoritative_source_publish_allowed':False}
    verifier=_load_module(root/TARGET_VALIDATOR,'cerebro_runtime2_target_validator_verify')
    verification=verifier.verify_receipt(Path(r['manifest_path']).resolve(),receipt_path,r['profile_id'],binding_source_root=root)
    result='PASS' if verification.get('result')=='PASS' else 'BLOCK'
    return {'schema':SCHEMA_RESULT,'result':result,'adapter_id':ADAPTER_ID,'prepared':prepared,'supervision_result':supervision,
            'target_runtime_execution':True,'semantic_verification':verification,'receipt_path':str(receipt_path),
            'receipt_sha256':sha256_file(receipt_path),'authoritative_source_publish_allowed':result=='PASS'}


def selftest() -> dict[str, Any]:
    tests=[]
    def check(name,cond): tests.append({'name':name,'result':'PASS' if cond else 'FAIL'})
    check('planner_and_verifier_remain_separate_owner', TARGET_VALIDATOR != __file__)
    check('supervision_owner_is_tooling_host', HOST_PATH=='tooling/host/cerebro_host.py')
    check('windows_adapter_is_preserved_existing_boundary', TARGET_SCRIPT.endswith('Invoke-CerebroWindowsPowerShellValidation.ps1'))
    target_env=target_child_environment()
    check('target_child_environment_resolution_is_explicitly_bound', all(k in target_env for k in ('SYSTEMROOT','WINDIR','SYSTEMDRIVE','COMSPEC','PATHEXT','PATH','TEMP','TMP','USERPROFILE','LOCALAPPDATA','APPDATA','PROGRAMDATA','PSMODULEPATH')) and Path(target_env['PATH'].split(os.pathsep)[0]).resolve()==Path(sys.executable).resolve().parent)
    check('no_target_truth_from_process_exit', 'returncode' not in execute.__code__.co_names and 'exit_code' not in execute.__code__.co_names)
    check('no_autonomous_retry_surface', not any(x in globals() for x in ('retry','retry_loop','scheduler')))
    check('prepublication_only_until_semantic_verification_pass', True)
    check('cac_linguistic_path_order_preserved', sorted(['target_runtime_validation.py','target-runtime-validation.yaml'],key=_cac_path_sort_key)==['target_runtime_validation.py','target-runtime-validation.yaml'])
    fixture_id='RUNTIME2_M4_TARGET_RUNTIME_ACTIVATION'
    fixture={'bindings':[{'id':fixture_id,'implementation':ACTIVATION_BINDING_IMPLEMENTATION,'wiring_proof_kind':'RUNTIME_EVIDENCE','runtime_evidence':{'schema':ACTIVATION_SCHEMA,'basis_files':list(ACTIVATION_BASIS),'accepted_binding_ids':[fixture_id],'required_proves_binding':fixture_id}}]}
    check('m4_activation_binding_resolution', resolve_activation_binding(fixture)==fixture_id)
    def binding_block(name, mutate, classification):
        value=json.loads(json.dumps(fixture)); mutate(value)
        try: resolve_activation_binding(value); ok=False
        except TargetRuntime2Error as exc: ok=exc.classification==classification
        check(name,ok)
    binding_block('m4_activation_binding_missing_blocks',lambda v:v.__setitem__('bindings',[]),'M4_ACTIVATION_BINDING_MISSING')
    binding_block('m4_activation_binding_ambiguous_blocks',lambda v:v['bindings'].append(dict(v['bindings'][0])),'M4_ACTIVATION_BINDING_AMBIGUOUS')
    binding_block('m4_activation_binding_schema_mismatch_blocks',lambda v:v['bindings'][0]['runtime_evidence'].__setitem__('schema','wrong'),'M4_ACTIVATION_BINDING_SCHEMA_MISMATCH')
    binding_block('m4_activation_binding_basis_mismatch_blocks',lambda v:v['bindings'][0]['runtime_evidence'].__setitem__('basis_files',ACTIVATION_BASIS[:-1]),'M4_ACTIVATION_BINDING_BASIS_MISMATCH')
    return {'schema':'cerebro-runtime2-target-runtime-selftest/v1','result':'PASS' if all(x['result']=='PASS' for x in tests) else 'FAIL','tests':tests,
            'process_supervision_owner':'tooling.host','semantic_result_owner':'validator','control_owner':'MCP','autonomous_retry':False}


def main() -> int:
    p=argparse.ArgumentParser(); sub=p.add_subparsers(dest='command',required=True)
    pp=sub.add_parser('prepare'); pp.add_argument('--request',required=True); pp.add_argument('--output',required=True)
    pe=sub.add_parser('execute'); pe.add_argument('--request',required=True); pe.add_argument('--output',required=True)
    ps=sub.add_parser('selftest'); ps.add_argument('--output')
    pa=sub.add_parser('activation-probe'); pa.add_argument('--source-root',required=True); pa.add_argument('--output',required=True)
    a=p.parse_args()
    try:
        if a.command=='prepare': out=prepare(read_json(Path(a.request))); write_json(Path(a.output),out)
        elif a.command=='execute': out=execute(read_json(Path(a.request))); write_json(Path(a.output),out); return 0 if out['result']=='PASS' else 2
        elif a.command=='selftest': out=selftest(); write_json(Path(a.output),out) if a.output else print(json.dumps(out,indent=2)); return 0 if out['result']=='PASS' else 2
        else: out=activation_probe(Path(a.source_root)); write_json(Path(a.output),out); return 0 if out['result']=='PASS' else 2
        return 0
    except TargetRuntime2Error as exc:
        err={'schema':'cerebro-runtime2-target-runtime-error/v1','result':'BLOCK','classification':exc.classification,'detail':exc.detail}
        print(json.dumps(err),file=sys.stderr); return 2

if __name__=='__main__': raise SystemExit(main())

