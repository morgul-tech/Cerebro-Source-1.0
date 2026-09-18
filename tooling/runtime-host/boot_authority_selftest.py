#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CURRENT_SOURCE = "github:morgul-tech/Cerebro-Source-1.0/main/cerebro.yaml"
ACTIVATION_SCHEMA = "cerebro-operational-status-semantics-activation-proof/v1"


def execution_projection(status: str, current: dict[str, str] | None, next_: dict[str, str] | None) -> dict[str, str | None]:
    if status == "COMPLETED":
        if current is None and next_ is None:
            return {"current_patch": None, "next_patch": None, "canonical_command": None}
        raise ValueError("BOOT_COMPLETED_EXECUTION_CONFLICT")
    if current is None:
        raise ValueError("BOOT_EXECUTION_SECTION_NOT_FOUND:current")
    if next_ is None:
        raise ValueError("BOOT_EXECUTION_SECTION_NOT_FOUND:next")
    for field in ("patch_ref", "canonical_command"):
        if not str(current.get(field) or "").strip():
            raise ValueError(f"BOOT_EXECUTION_VALUE_NOT_FOUND:current:{field}")
    if not str(next_.get("patch_ref") or "").strip():
        raise ValueError("BOOT_EXECUTION_VALUE_NOT_FOUND:next:patch_ref")
    return {
        "current_patch": current["patch_ref"],
        "next_patch": next_["patch_ref"],
        "canonical_command": current["canonical_command"],
    }


def resolve(command: str, candidates: list[dict[str, str]], current_commit: str) -> dict[str, str]:
    if command.strip().lower() not in {"boot cerebro", "bootcerebro", "bootini"}:
        raise ValueError("BOOT_COMMAND_UNRECOGNIZED")
    source = next((item for item in candidates if item.get("identity") == CURRENT_SOURCE), None)
    if source is None:
        raise ValueError("AUTHORITATIVE_SOURCE_MISSING")
    if source.get("commit") != current_commit:
        raise ValueError("AUTHORITATIVE_SOURCE_COMMIT_MISMATCH")
    return {"canonical_command": "bootCerebro", "authority": CURRENT_SOURCE, "commit": current_commit}


def principal_permit_runtime_canaries(root: Path) -> list[dict[str, object]]:
    """Exercise the actual PowerShell permit helper; never invokes a live boot."""
    import os
    import shutil
    import subprocess
    import tempfile
    if os.name != "nt":
        return [{"name":"P673-Windows-permit-runtime", "result":"HOLD_CAPABILITY",
                 "detail":"Windows PowerShell runtime unavailable"}]
    executable = shutil.which("powershell") or shutil.which("pwsh")
    if executable is None:
        return [{"name":"P673-Windows-permit-runtime", "result":"HOLD_CAPABILITY",
                 "detail":"PowerShell executable unavailable"}]
    script = r"""param([Parameter(Mandatory)][string]$SourceRoot, [Parameter(Mandatory)][string]$RunRoot)
$ErrorActionPreference='Stop'
. (Join-Path $SourceRoot 'tooling/runtime-host/cerebro_boot.ps1')
$sourceHead='5555555555555555555555555555555555555555'
$baseText=[IO.File]::ReadAllText((Join-Path $RunRoot 'synthetic_permit.json'))
$results=[Collections.Generic.List[object]]::new()
function Invoke-P672Canary {
    param([string]$Name,[scriptblock]$Mutate,[bool]$ShouldPass=$false,[bool]$Reseal=$true,[bool]$Reader=$true,[scriptblock]$DiaryVerifier,[object]$Frontier=77)
    $script:casePermit=$baseText | ConvertFrom-Json
    & $Mutate $script:casePermit
    if($Reseal) {
        $baselineProperty=$script:casePermit.PSObject.Properties['principal_continuity_baseline']
        if ($null -ne $baselineProperty -and $null -ne $baselineProperty.Value) {
            $caseBaseline=$baselineProperty.Value
            $baselineSubject=[ordered]@{}
            foreach($property in @($caseBaseline.PSObject.Properties | Sort-Object Name)) {
                if($property.Name -ne 'baseline_fingerprint') {
                    $baselineSubject[$property.Name]=ConvertTo-CerebroCanonicalObject $property.Value
                }
            }
            $caseBaseline.baseline_fingerprint=Get-CerebroBootSha256Text (
                $baselineSubject | ConvertTo-Json -Compress -Depth 32
            )
        }
        $canonical=ConvertTo-CerebroCanonicalObject $script:casePermit
        $script:casePermit.permit_fingerprint=Get-CerebroBootSha256Text ($canonical | ConvertTo-Json -Compress -Depth 32)
    }
    $binding=@{PredecessorGenerationId='P672-OLD';SuccessorGenerationId='P672-NEW';PermitRef='P672-PERMIT';PermitFingerprint=$script:casePermit.permit_fingerprint;SourceHead=$sourceHead;ObservedEventFrontier=$Frontier;MachineDiaryEffectVerifier=$DiaryVerifier}
    if($Reader){$binding.PermitReader={param($request) $script:casePermit}}
    $accepted=$false; $detail=$null
    try {$detail=Test-CerebroPrincipalSuccessionPermit @binding; $accepted=($detail.result -eq 'PASS')}
    catch {$detail=$_.Exception.Message}
    $verdict=if($accepted -eq $ShouldPass){'PASS'}else{'FAIL'}
    $results.Add([ordered]@{id=$Name;verdict=$verdict;expected=$(if($ShouldPass){'ACCEPT'}else{'BLOCK'});accepted=$accepted;observed=$detail})
}
Invoke-P672Canary 'ps_valid_no_capture_cross_language_fingerprint' {} $true $false
Invoke-P672Canary 'ps_missing_principal_continuity_baseline' {$args[0].PSObject.Properties.Remove('principal_continuity_baseline')}
Invoke-P672Canary 'ps_missing_diary_bound' {$args[0].principal_continuity_baseline.PSObject.Properties.Remove('diary_bound')}
Invoke-P672Canary 'ps_missing_living_ledger_baseline' {$args[0].principal_continuity_baseline.PSObject.Properties.Remove('living_ledger_baseline')}
Invoke-P672Canary 'ps_missing_livspuls_baseline' {$args[0].principal_continuity_baseline.PSObject.Properties.Remove('livspuls_baseline')}
Invoke-P672Canary 'ps_baseline_wrong_generation' {$args[0].principal_continuity_baseline.generation_ref='P672-OLD'}
Invoke-P672Canary 'ps_baseline_stale_source' {$args[0].principal_continuity_baseline.source_head='0000000000000000000000000000000000000000'}
Invoke-P672Canary 'ps_baseline_ready_epoch_zero' {$args[0].principal_continuity_baseline.ready_epoch=0}
Invoke-P672Canary 'ps_baseline_ready_epoch_bool' {$args[0].principal_continuity_baseline.ready_epoch=$true}
Invoke-P672Canary 'ps_baseline_provider_revision_drift' {$args[0].principal_continuity_baseline.provider_revision=10}
Invoke-P672Canary 'ps_baseline_frontier_drift' {$args[0].principal_continuity_baseline.covered_through_frontier=78}
Invoke-P672Canary 'ps_baseline_readback_false' {$args[0].principal_continuity_baseline.post_state_readback_verified=$false}
Invoke-P672Canary 'ps_baseline_readback_string' {$args[0].principal_continuity_baseline.post_state_readback_verified='true'}
Invoke-P672Canary 'ps_baseline_receipt_private_locator' {$args[0].principal_continuity_baseline.diary_bound.receipt_ref='file:private'}
Invoke-P672Canary 'ps_baseline_ledger_not_durable' {$args[0].principal_continuity_baseline.living_ledger_baseline.durable=$false}
Invoke-P672Canary 'ps_baseline_livspuls_not_readback' {$args[0].principal_continuity_baseline.livspuls_baseline.readback_verified=$false}
Invoke-P672Canary 'ps_missing_bound_reader' {} $false $false $false
Invoke-P672Canary 'ps_tampered_fingerprint' {$args[0].provider_revision=10} $false $false
Invoke-P672Canary 'ps_stale_source' {$args[0].source_head='0000000000000000000000000000000000000000'}
Invoke-P672Canary 'ps_unknown_qualification' {$args[0].lived_continuity.qualification='UNKNOWN'}
Invoke-P672Canary 'ps_missing_closeout' {$args[0].evidence.PSObject.Properties.Remove('predecessor_closeout')}
Invoke-P672Canary 'ps_cold_successor_fail' {$args[0].evidence.cold_successor_canary.result='FAIL'}
Invoke-P672Canary 'ps_cold_successor_unknown' {$args[0].evidence.cold_successor_canary.result='UNKNOWN'}
Invoke-P672Canary 'ps_capture_missing_diary' {$args[0].lived_continuity.qualification='CAPTURE'}
Invoke-P672Canary 'ps_capture_empty_self_asserted_diary' {
    $args[0].lived_continuity.qualification='CAPTURE'
    $args[0].lived_continuity | Add-Member NoteProperty machine_diary_effect_receipt ([pscustomobject]@{})
}
Invoke-P672Canary 'ps_receipt_uri_value' {$args[0].evidence.living_ledger.receipt_ref='https://example.invalid/private'}
Invoke-P672Canary 'ps_receipt_windows_path_value' {$args[0].evidence.living_ledger.receipt_ref='D:\Synthetic\P672-private.txt'}
Invoke-P672Canary 'ps_negative_provider_revision' {$args[0].provider_revision=-1}
Invoke-P672Canary 'ps_boolean_provider_revision' {$args[0].provider_revision=$true}
Invoke-P672Canary 'ps_missing_frontier' {$args[0].PSObject.Properties.Remove('covered_through_frontier')}
Invoke-P672Canary 'ps_unknown_top_level_prose_field' {$args[0] | Add-Member NoteProperty arbitrary_note 'SYNTHETIC private prose'}

Invoke-P672Canary 'ps_capture_valid_receipt_missing_verifier' {
    $args[0].lived_continuity.qualification='CAPTURE'
    $args[0].lived_continuity | Add-Member NoteProperty machine_diary_effect_receipt $args[0].evidence.livspuls
}
$script:diaryCalls=0
$captureMutation = {
    $args[0].lived_continuity.qualification='CAPTURE'
    $args[0].lived_continuity | Add-Member NoteProperty machine_diary_effect_receipt $args[0].evidence.livspuls
}
Invoke-P672Canary 'ps_capture_bound_verifier_PASS' $captureMutation $true $true $true {
    param($request)
    $script:diaryCalls++
    if ($request.event.event_id -ne 'P672-E1' -or $request.receipt.receipt_ref -ne 'R-1') { throw 'wrong-effect-binding' }
    @{result='PASS'}
}
$results.Add(@{id='ps_actual_diary_verifier_called';verdict=$(if($script:diaryCalls -eq 1){'PASS'}else{'FAIL'})})
Invoke-P672Canary 'ps_capture_verifier_FAIL' $captureMutation $false $true $true { @{result='FAIL'} }
Invoke-P672Canary 'ps_capture_verifier_UNKNOWN' $captureMutation $false $true $true { @{result='UNKNOWN'} }
Invoke-P672Canary 'ps_capture_verifier_throws' $captureMutation $false $true $true { throw 'synthetic-rejection' }
Invoke-P672Canary 'ps_capture_receipt_extra_field' {
    & $captureMutation $args[0]
    $args[0].lived_continuity.machine_diary_effect_receipt | Add-Member NoteProperty arbitrary_note 'synthetic'
} $false $true $true { @{result='PASS'} }
Invoke-P672Canary 'ps_receipt_prose' {$args[0].evidence.livspuls.receipt_ref='synthetic private prose'}
Invoke-P672Canary 'ps_receipt_trailing_newline' {$args[0].evidence.livspuls.receipt_ref="R-1`n"}
Invoke-P672Canary 'ps_event_URI' {$args[0].lived_continuity.event_id='urn:synthetic'}
Invoke-P672Canary 'ps_permit_prose' {$args[0].permit_id='synthetic prose'}
Invoke-P672Canary 'ps_unknown_lived_field' {$args[0].lived_continuity | Add-Member NoteProperty note 'synthetic'}
Invoke-P672Canary 'ps_unknown_evidence_field' {$args[0].evidence | Add-Member NoteProperty note 'synthetic'}
Invoke-P672Canary 'ps_unknown_receipt_field' {$args[0].evidence.livspuls | Add-Member NoteProperty note 'synthetic'}
Invoke-P672Canary 'ps_no_capture_null_receipt' {$args[0].lived_continuity | Add-Member NoteProperty machine_diary_effect_receipt $null}
Invoke-P672Canary 'ps_frontier_behind' {$args[0].covered_through_frontier=76}
Invoke-P672Canary 'ps_frontier_ahead' {$args[0].covered_through_frontier=78;$args[0].principal_continuity_baseline.covered_through_frontier=78} $true
Invoke-P672Canary 'ps_frontier_bool' {$args[0].covered_through_frontier=$true}
Invoke-P672Canary 'ps_frontier_negative' {$args[0].covered_through_frontier=-1}
Invoke-P672Canary 'ps_observed_frontier_missing' {} $false $true $true $null $null
Invoke-P672Canary 'ps_observed_frontier_bool' {} $false $true $true $null $true
Invoke-P672Canary 'ps_observed_frontier_negative' {} $false $true $true $null -1
Invoke-P672Canary 'ps_provider_revision_string' {$args[0].provider_revision='9'}
Invoke-P672Canary 'ps_provider_revision_fractional' {$args[0].provider_revision=9.5}
Invoke-P672Canary 'ps_provider_revision_zero' {$args[0].provider_revision=0;$args[0].principal_continuity_baseline.provider_revision=0} $true
Invoke-P672Canary 'ps_receipt_durable_string' {$args[0].evidence.livspuls.durable='true'}
Invoke-P672Canary 'ps_receipt_readback_integer' {$args[0].evidence.livspuls.readback_verified=1}

$nonprincipal=Test-CerebroPrincipalSuccessionPermit -SourceHead $sourceHead
$results.Add([ordered]@{id='ps_nonprincipal_empty_binding_unchanged';verdict=$(if($nonprincipal.result -eq 'PASS_NON_PRINCIPAL_UNCHANGED'){'PASS'}else{'FAIL'});expected='PASS_NON_PRINCIPAL_UNCHANGED';observed=$nonprincipal})
$out=[ordered]@{packet='P672';source_head=$sourceHead;execution='WINDOWS_LOCAL_SYNTHETIC_HELPER_ONLY';live_succession_effect='NOT_RUN';time_utc=[DateTime]::UtcNow.ToString('o');test_count=$results.Count;pass_count=@($results | Where-Object verdict -eq 'PASS').Count;fail_count=@($results | Where-Object verdict -eq 'FAIL').Count;tests=$results}
$json=$out | ConvertTo-Json -Depth 32
$json
"""
    fixture = {'schema': 'cerebro-principal-succession-permit/v1',
     'permit_id': 'P672-PERMIT',
     'predecessor_generation_id': 'P672-OLD',
     'successor_generation_id': 'P672-NEW',
     'source_head': '5555555555555555555555555555555555555555',
     'currentness': 'CURRENT',
     'provider_revision': 9,
     'covered_through_frontier': 77,
     'lived_continuity': {'event_id': 'P672-E1',
                          'event_fingerprint': '3333333333333333333333333333333333333333333333333333333333333333',
                          'qualification': 'NO_CAPTURE',
                          'debt_state': 'CLEAR'},
     'evidence': {'living_ledger': {'receipt_ref': 'R-1',
                                    'receipt_fingerprint': '4444444444444444444444444444444444444444444444444444444444444444',
                                    'durable': True,
                                    'readback_verified': True},
                  'livspuls': {'receipt_ref': 'R-1',
                               'receipt_fingerprint': '4444444444444444444444444444444444444444444444444444444444444444',
                               'durable': True,
                               'readback_verified': True},
                  'etterklang_review': {'receipt_ref': 'R-1',
                                        'receipt_fingerprint': '4444444444444444444444444444444444444444444444444444444444444444',
                                        'durable': True,
                                        'readback_verified': True},
                  'stambok_seal': {'receipt_ref': 'R-1',
                                   'receipt_fingerprint': '4444444444444444444444444444444444444444444444444444444444444444',
                                   'durable': True,
                                   'readback_verified': True},
                  'gjenklang_publication': {'receipt_ref': 'R-1',
                                            'receipt_fingerprint': '4444444444444444444444444444444444444444444444444444444444444444',
                                            'durable': True,
                                            'readback_verified': True},
                  'human_readability': {'receipt_ref': 'R-1',
                                        'receipt_fingerprint': '4444444444444444444444444444444444444444444444444444444444444444',
                                        'durable': True,
                                        'readback_verified': True},
                  'predecessor_closeout': {'receipt_ref': 'R-1',
                                           'receipt_fingerprint': '4444444444444444444444444444444444444444444444444444444444444444',
                                           'durable': True,
                                           'readback_verified': True},
                  'cold_successor_canary': {'receipt_ref': 'R-1',
                                            'receipt_fingerprint': '4444444444444444444444444444444444444444444444444444444444444444',
                                            'durable': True,
                                            'readback_verified': True,
                                            'result': 'PASS'}},
     'post_state_readback_verified': True,
     'permit_fingerprint': ''}
    baseline_receipt = {'receipt_ref': 'R-BASE',
                        'receipt_fingerprint': '6' * 64,
                        'durable': True,
                        'readback_verified': True}
    baseline = {'schema': 'cerebro-principal-continuity-baseline/v1',
                'generation_ref': 'P672-NEW',
                'source_head': fixture['source_head'],
                'ready_epoch': 2,
                'currentness': 'CURRENT',
                'provider_revision': fixture['provider_revision'],
                'covered_through_frontier': fixture['covered_through_frontier'],
                'diary_bound': dict(baseline_receipt),
                'living_ledger_baseline': dict(baseline_receipt),
                'livspuls_baseline': dict(baseline_receipt),
                'post_state_readback_verified': True,
                'baseline_fingerprint': ''}
    baseline_subject = dict(baseline)
    baseline_subject.pop('baseline_fingerprint')
    baseline['baseline_fingerprint'] = hashlib.sha256(
        json.dumps(baseline_subject, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    ).hexdigest()
    fixture['principal_continuity_baseline'] = baseline
    permit_subject = dict(fixture)
    permit_subject.pop('permit_fingerprint')
    fixture['permit_fingerprint'] = hashlib.sha256(
        json.dumps(permit_subject, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    ).hexdigest()
    with tempfile.TemporaryDirectory(prefix="cerebro-p673-") as directory:
        run_root = Path(directory)
        script_path = run_root / "permit-canaries.ps1"
        script_path.write_text(script, encoding="utf-8")
        (run_root / "synthetic_permit.json").write_text(json.dumps(fixture), encoding="utf-8")
        process = subprocess.run(
            [executable, "-NoProfile", "-NonInteractive", "-File", str(script_path),
             "-SourceRoot", str(root), "-RunRoot", str(run_root)],
            capture_output=True, text=True, timeout=45,
        )
        if process.returncode != 0:
            return [{"name":"P673-Windows-permit-runtime", "result":"FAIL",
                     "detail":process.stderr or process.stdout}]
        try:
            result = json.loads(process.stdout)
            return [{"name":"P673-" + row["id"], "result":row["verdict"],
                     "detail":row.get("observed")} for row in result["tests"]]
        except (ValueError, KeyError) as exc:
            return [{"name":"P673-Windows-permit-runtime", "result":"FAIL",
                     "detail":str(exc) + ":" + process.stdout}]



def civilization_method_runtime_canaries(root: Path) -> list[dict[str, object]]:
    """Run actual boot and receipt helpers against disposable offline fixtures."""
    import shutil
    import subprocess
    import tempfile
    executable = shutil.which("powershell") or shutil.which("pwsh")
    if executable is None:
        return [{"name": "K157-runtime", "result": "HOLD_CAPABILITY",
                 "detail": "PowerShell unavailable; behavior not verified"}]
    script = r"""param([string]$SourceRoot,[string]$RunRoot)
$ErrorActionPreference='Stop'
. (Join-Path $SourceRoot 'tooling/runtime-host/cerebro_boot.ps1')
$fixture=Join-Path $RunRoot 'source'
$sourceHead='5555555555555555555555555555555555555555'
$paths=@('cerebro.yaml','mcp/activation.yaml','mcp/constitution.yaml',
'standards/boot-critical-path-architecture.yaml','mcp/boot-architecture-control.yaml',
'standards/runtime/minimal-runtime-bootstrap.yaml','standards/runtime/handboot.yaml',
'standards/runtime/handboot-bootstrap-state-schema.yaml','standards/session-handoff.yaml',
'standards/session-handoff-schema.yaml','engines/context/working-context.yaml','engines/project/roadmap.yaml',
'tooling/builder/templates/pshell/Cerebro.Tools.psd1','tooling/builder/templates/pshell/Cerebro.Tools.psm1',
'tooling/builder/templates/pshell/cerebro_handoff.ps1','tooling/runtime-host/cerebro_resume.ps1',
'tooling/runtime-host/cerebro_boot.ps1','tooling/runtime-host/cerebro_machine_proof.ps1',
'tooling/validator/checks.yaml','standards/human-continuation-surface.yaml',
'standards/continuation-surface-system-policy.yaml','engines/presentation/human-admin-projection.schema.json',
'engines/presentation/component.yaml','engines/presentation/rules.yaml')
foreach($p in $paths){
    $dest=Join-Path $fixture $p
    [IO.Directory]::CreateDirectory((Split-Path $dest -Parent)) | Out-Null
    [IO.File]::WriteAllBytes($dest,[IO.File]::ReadAllBytes((Join-Path $SourceRoot $p)))
}
$owner=Join-Path $fixture 'standards/boot-critical-path-architecture.yaml'
$originalOwner=[IO.File]::ReadAllText($owner)
[IO.File]::WriteAllText((Join-Path $fixture 'engines/project/roadmap.yaml'),"roadmap:`n  status: COMPLETED`n  execution:`n    current: null`n    next: null`n")
$blobs=@{}
foreach($p in $paths){$blobs[$p]=(Get-FileHash -Algorithm SHA1 -LiteralPath (Join-Path $fixture $p)).Hash.ToLowerInvariant()}
$originalOwnerBlob=$blobs['standards/boot-critical-path-architecture.yaml']
# Only immutable source lookup and transport are faked. Boot/method/state code is real.
function git {
    $a=@($args)
    if($a[0] -eq '-C'){$a=@($a | Select-Object -Skip 2)}
    $global:LASTEXITCODE=0
    switch($a[0]){
        'rev-parse' {
            if($a[1] -eq '--is-inside-work-tree'){return 'true'}
            if($a[1] -in @('HEAD','origin/main')){return ('5'*40)}
            $relative=([string]$a[1]).Substring(41)
            if(-not $blobs.ContainsKey($relative)){throw 'FIXTURE_UNEXPECTED_SOURCE_LOOKUP'}
            return $blobs[$relative]
        }
        'hash-object' {return (Get-FileHash -Algorithm SHA1 -LiteralPath $a[-1]).Hash.ToLowerInvariant()}
        'status' {return}
        'branch' {return 'main'}
        'remote' {return 'https://github.com/morgul-tech/Cerebro-Source-1.0'}
        'fetch' {return}
        default {throw ('FIXTURE_UNEXPECTED_GIT_COMMAND:'+$a[0])}
    }
}
function Invoke-WebRequest {
    return [pscustomobject]@{StatusCode=200;Content=@(
        'BOOTSTRAP ENGINE','Configuration := IMMUTABLE',
        'CurrentMaster.Repository := morgul-tech/Cerebro-Source-1.0','CurrentMaster.Branch := main',
        'CurrentMaster.Path := cerebro.yaml','Command.NaturalLanguageAlias := boot cerebro',
        'Authority.First := github:morgul-tech/Cerebro-Source-1.0/main/cerebro.yaml',
        'FilenameMarker.CURRENT.Authority := NONE','StaleDerivedState.Action := REJECT_INPUT_CONTINUE_CURRENT_SOURCE',
        'OperationalClaim.Requires := ACTIVE_CONTROL_TRANSFERRED_RECEIPT_AT_CURRENT_SOURCE_COMMIT',
        'IDLE','FETCHING','VERIFYING','ANALYZING','ACTIVATING','COMPLETE','FAILED','UndefinedTransition','FORBIDDEN'
    ) -join '`n'}
}
$writerBody=(Get-Item Function:Write-CerebroBootRuntimeState).ScriptBlock
$writeMode='NORMAL'
$writtenStates=[Collections.Generic.List[string]]::new()
function Write-CerebroBootRuntimeState {
    param([string]$Path,[object]$RuntimeState)
    $writtenStates.Add([string]$RuntimeState.runtime.state)
    if($writeMode -eq 'MISSING' -and $RuntimeState.runtime.state -eq 'CONSTRUCTED_INACTIVE'){return}
    & $writerBody -Path $Path -RuntimeState $RuntimeState
    if(($writeMode -eq 'TAMPER' -and $RuntimeState.runtime.state -eq 'CONSTRUCTED_INACTIVE') -or
       ($writeMode -eq 'FINAL_TAMPER' -and $RuntimeState.runtime.state -eq 'ACTIVE_CONTROL_TRANSFERRED')){
        [IO.File]::AppendAllText($Path,' ')
    }
}
$results=[Collections.Generic.List[object]]::new()
function Check([string]$name,[scriptblock]$action,[string]$expected=''){
    try{
        & $action
        if($expected){throw 'EXPECTED_REJECTION_NOT_OBSERVED'}
        $results.Add(@{name=$name;result='PASS'})
    }catch{
        $message=$_.Exception.Message
        $ok=$expected -and $message.Contains($expected)
        $results.Add(@{name=$name;result=$(if($ok){'PASS'}else{'FAIL'});detail=$message})
    }
}
function Binding([string]$role='IMPLEMENTER'){
    Resolve-CerebroCivilizationMethod -SourceRoot $fixture -SourceHead $sourceHead -ActorRole $role
}
function Receipt($binding){
    New-CerebroAwakeningReceipt -Binding $binding -SourceRoot $fixture -SourceHead $sourceHead -ActorRole 'IMPLEMENTER'
}
function Readback($state,[string]$path){
    Test-CerebroAwakeningReadback -Path $path -RuntimeState $state -SourceRoot $fixture -SourceHead $sourceHead -ActorRole 'IMPLEMENTER'
}
Check 'K157-cross-role-profile-shared-projection-distinct' {
    $one=Binding 'IMPLEMENTER';$two=Binding 'PROJECT_MANAGER'
    if($one.method_profile.fingerprint -cne $two.method_profile.fingerprint -or
       $one.role_method_projection.fingerprint -ceq $two.role_method_projection.fingerprint){throw 'CROSS_ROLE_MISMATCH'}
}
Check 'K157-stale-head' {
    Resolve-CerebroCivilizationMethod -SourceRoot $fixture -SourceHead ('6'*40) -ActorRole 'IMPLEMENTER' | Out-Null
} 'CIVILIZATION_METHOD_STALE_SOURCE'
Check 'K157-invalid-role' {Binding 'task/private' | Out-Null} 'CIVILIZATION_METHOD_EXACT_SOURCE_AND_ROLE_REQUIRED'
foreach($case in @('missing','duplicate','version','authority','private','durable-leak','duplicate-key')){
    $caseText=switch($case){
        'missing' {$originalOwner -replace '(?m)^  current_civilization_method_profile:[^\r\n]+\r?\n',''}
        'duplicate' {$originalOwner+"`n"+([regex]::Match($originalOwner,'(?m)^  current_civilization_method_profile:[^\r\n]+').Value)}
        'version' {$originalOwner.Replace('"version":"1.0","authority":"NONE"','"version":"0.9","authority":"NONE"')}
        'authority' {$originalOwner.Replace('"authority":"NONE"','"authority":"SOURCE_WRITER"')}
        'private' {$originalOwner.Replace('"id":"CURRENT_CIVILIZATION_METHOD_PROFILE"','"private_state":"task","id":"CURRENT_CIVILIZATION_METHOD_PROFILE"')}
        'durable-leak' {$originalOwner.Replace('"CURRENTNESS_FIRST"','"CURRENT_TASK_123"')}
        'duplicate-key' {$originalOwner.Replace('"version":"1.0"','"version":"1.0","version":"1.0"')}
    }
    [IO.File]::WriteAllText($owner,$caseText)
    $blobs['standards/boot-critical-path-architecture.yaml']=(Get-FileHash -Algorithm SHA1 $owner).Hash.ToLowerInvariant()
    $expected=switch($case){
        {$_ -in @('missing','duplicate')} {'CIVILIZATION_METHOD_SINGLE_OWNER_REQUIRED'}
        {$_ -in @('version','authority')} {'CIVILIZATION_METHOD_ID_VERSION_AUTHORITY_INVALID'}
        'private' {'CIVILIZATION_METHOD_PRIVATE_OR_EXTRA_FIELD'}
        'durable-leak' {'CIVILIZATION_METHOD_DURABLE_CONTRACT_REQUIRED'}
        'duplicate-key' {'CIVILIZATION_METHOD_EXACT_FIELDS_REQUIRED'}
    }
    Check ('K157-'+$case) {Binding | Out-Null} $expected
}
[IO.File]::WriteAllText($owner,$originalOwner)
$blobs['standards/boot-critical-path-architecture.yaml']=$originalOwnerBlob
Check 'K157-tampered-pinned-source' {
    [IO.File]::AppendAllText($owner,' ')
    Binding | Out-Null
} 'CIVILIZATION_METHOD_SOURCE_BLOB_MISMATCH'
[IO.File]::WriteAllText($owner,$originalOwner)
Check 'K157-projection-fingerprint-mismatch' {
    $binding=Binding;$binding.role_method_projection.fingerprint='0'*64
    Receipt $binding | Out-Null
} 'CIVILIZATION_METHOD_BINDING_MISMATCH'
Check 'K157-projection-task-leak' {
    $binding=Binding;$binding.role_method_projection.task_state='old'
    Receipt $binding | Out-Null
} 'CIVILIZATION_METHOD_BINDING_MISMATCH'
$statePath=Join-Path $RunRoot 'receipt/state.json'
$state=[ordered]@{runtime=[ordered]@{awakening_receipt=(Receipt (Binding))}}
Check 'K157-consumed-persisted-readback' {
    & $writerBody -Path $statePath -RuntimeState $state
    $proof=Readback $state $statePath
    if($proof.result -ne 'PASS'){throw 'READBACK_NONPASS'}
}
Check 'K157-verified-not-consumed' {
    $state.runtime.awakening_receipt.consumed=$false
    & $writerBody -Path $statePath -RuntimeState $state
    Readback $state $statePath | Out-Null
} 'AWAKENING_CONSUMPTION_READBACK_MISMATCH'
$state.runtime.awakening_receipt.consumed=$true
Check 'K157-persisted-tamper' {Readback $state $statePath | Out-Null} 'AWAKENING_PERSISTED_STATE_MISMATCH'
Check 'K157-readback-stale-currentness' {
    & $writerBody -Path $statePath -RuntimeState $state
    [IO.File]::AppendAllText($owner,' ')
    Readback $state $statePath | Out-Null
} 'CIVILIZATION_METHOD_SOURCE_BLOB_MISMATCH'
[IO.File]::WriteAllText($owner,$originalOwner)
$script:pulseObserved=$false
function Run-Boot([string]$suffix,[switch]$Principal){
    $params=@{WorkingSourcePath=$fixture;RuntimeStatePath=(Join-Path $RunRoot ($suffix+'/state.json'));
        HandoffPath=(Join-Path $RunRoot 'absent-handoff.json');SkipHandoff=$true;OperationalPulseActorRole='IMPLEMENTER';
        OperationalPulseReader={
            $script:pulseObserved=$true
            return @{control=@{currentness='CURRENT';carrier_ref='FIXTURE';provider_revision=1;event_frontier=77};
                protobox=@{currentness='CURRENT';channel_ref='FIXTURE';observed_revision=1}}
        }}
    if($Principal){$params.PredecessorPrincipalGenerationId='OLD'}
    Invoke-CerebroBootCore @params 6>$null
}
Check 'K157-core-normal-path-ready-after-readback' {
    $result=Run-Boot 'normal'
    if($result.state -ne 'ACTIVE_CONTROL_TRANSFERRED' -or $result.succession.final_state -ne 'READY' -or
       $result.awakening_readback.result -ne 'PASS' -or -not $script:pulseObserved){throw 'CORE_NOT_READY'}
}
foreach($mode in @('MISSING','TAMPER','FINAL_TAMPER')){
    $writeMode=$mode;$writtenStates.Clear()
    $expected=if($mode -eq 'MISSING'){'AWAKENING_PERSISTED_READBACK_MISSING'}else{'AWAKENING_PERSISTED_STATE_MISMATCH'}
    Check ('K157-core-'+$mode+'-holds') {Run-Boot $mode | Out-Null} $expected
    Check ('K157-core-'+$mode+'-inactive-terminal') {
        $saved=[IO.File]::ReadAllText((Join-Path $RunRoot ($mode+'/state.json'))) | ConvertFrom-Json
        if($saved.runtime.operational -or $saved.runtime.state -ne 'INACTIVE_CONTROL_RETAINED'){throw 'FAILED_BOOT_LEFT_ACTIVE'}
        if($mode -ne 'FINAL_TAMPER' -and $writtenStates.Contains('ACTIVE_CONTROL_TRANSFERRED')){throw 'READY_BEFORE_READBACK'}
    }
}
$writeMode='NORMAL';$script:pulseObserved=$false
[IO.File]::AppendAllText($owner,' ')
Check 'K157-core-stale-method-before-fresh-world' {Run-Boot 'stale' | Out-Null} 'CIVILIZATION_METHOD_SOURCE_BLOB_MISMATCH'
Check 'K157-core-stale-does-not-consume-fresh-world' {if($script:pulseObserved){throw 'FRESH_WORLD_REACHED'}}
[IO.File]::WriteAllText($owner,$originalOwner)
Check 'K157-core-principal-gate-preserved' {Run-Boot 'principal' -Principal | Out-Null} 'PRINCIPAL_SUCCESSION_BOUND_READER_AND_EXACT_BINDING_REQUIRED'
@{tests=@($results)} | ConvertTo-Json -Depth 16
"""
    with tempfile.TemporaryDirectory(prefix="cerebro-k157-") as directory:
        run_root = Path(directory)
        script_path = run_root / "method-canaries.ps1"
        script_path.write_text(script, encoding="utf-8")
        process = subprocess.run(
            [executable, "-NoProfile", "-NonInteractive", "-File", str(script_path),
             "-SourceRoot", str(root), "-RunRoot", str(run_root)],
            capture_output=True, text=True, timeout=90,
        )
        if process.returncode:
            return [{"name": "K157-runtime", "result": "FAIL",
                     "detail": process.stderr or process.stdout}]
        try:
            return json.loads(process.stdout)["tests"]
        except (ValueError, KeyError) as exc:
            return [{"name": "K157-runtime", "result": "FAIL",
                     "detail": str(exc) + ":" + process.stdout}]


def selftest(root: Path = ROOT, bootengine_path: Path | None = None) -> dict[str, object]:
    commit = "CURRENT-COMMIT"
    source = {"identity": CURRENT_SOURCE, "commit": commit, "kind": "SOURCE"}
    legacy_docx = {"identity": "library:Regelverk_v1.4_Master_CURRENT.docx", "commit": "", "kind": "DOCUMENT"}
    stale_receipt = {"identity": "receipt:SELF-RUNNING-REBUILD-006", "commit": "OLD", "kind": "RECEIPT"}
    stale_handoff = {"identity": "handoff:OLD", "commit": "OLD", "kind": "HANDOFF"}
    tests: list[dict[str, str]] = []

    def check(name: str, ok: bool) -> None:
        tests.append({"name": name, "result": "PASS" if ok else "FAIL"})

    for alias in ("boot cerebro", "BOOT CEREBRO", "bootCerebro", "bootini"):
        result = resolve(alias, [legacy_docx, stale_receipt, stale_handoff, source], commit)
        check(f"alias:{alias}", result["canonical_command"] == "bootCerebro")
    result = resolve("boot cerebro", [legacy_docx, stale_receipt, stale_handoff, source], commit)
    check("legacy-docx-current-has-no-authority", result["authority"] == CURRENT_SOURCE)
    check("stale-rebuild-receipt-cannot-block", result["commit"] == commit)
    check("stale-handoff-cannot-override", result["commit"] != stale_handoff["commit"])

    terminal = execution_projection("COMPLETED", None, None)
    check(
        "P1-terminal-no-continuation-handoff",
        terminal == {"current_patch": None, "next_patch": None, "canonical_command": None},
    )
    check("P2-terminal-boot-no-handoff", terminal["current_patch"] is None)
    check("P3-terminal-boot-stale-handoff", terminal["canonical_command"] is None)

    for name, status, current, next_, failure in (
        ("N1-active-missing-current", "ACTIVE", None, {"patch_ref": "NEXT"}, "current"),
        ("N2-active-missing-next", "ACTIVE", {"patch_ref": "CUR", "canonical_command": "run"}, None, "next"),
        ("N3-resumable-current-token-missing", "ACTIVE", {"patch_ref": "CUR"}, {"patch_ref": "NEXT"}, "canonical_command"),
        ("N5-completed-with-active-execution-conflict", "COMPLETED", {"patch_ref": "CUR", "canonical_command": "run"}, None, "CONFLICT"),
    ):
        try:
            execution_projection(status, current, next_)
            check(name, False)
        except ValueError as exc:
            check(name, failure in str(exc))
    check("N6-no-fake-execution", all(value is None for value in terminal.values()))

    handboot = (root / "standards/runtime/handboot.yaml").read_text(encoding="utf-8")
    source_authority = (root / "standards/source-authority.yaml").read_text(encoding="utf-8")
    activation = (root / "mcp/activation.yaml").read_text(encoding="utf-8")
    boot_control = (root / "mcp/boot-architecture-control.yaml").read_text(encoding="utf-8")
    boot_architecture = (root / "standards/boot-critical-path-architecture.yaml").read_text(encoding="utf-8")
    terminology = (root / "modules/terminology/terms.yaml").read_text(encoding="utf-8")
    interaction = (root / "engines/interaction/rules.yaml").read_text(encoding="utf-8")
    handoff_standard = (root / "standards/session-handoff.yaml").read_text(encoding="utf-8")
    handoff_generator = (root / "tooling/builder/templates/pshell/cerebro_handoff.ps1").read_text(encoding="utf-8")
    boot_runtime = (root / "tooling/runtime-host/cerebro_boot.ps1").read_text(encoding="utf-8")

    check(
        "N4-same-current-malformed-handoff-remains-fail-closed",
        "invalid_handoff:" in handboot
        and "condition: claims_current_source_but_validation_fails" in handboot
        and "disposition: FAILED" in handboot
        and "boot_must_fail: true" in handboot,
    )

    check(
        "terminal-handoff-contract-bound",
        "disposition: NO_CONTINUATION" in handoff_standard
        and "artifact_write: false" in handoff_standard
        and "execution_tokens_required_when_resumable_or_actual_handoff: true" in handoff_standard,
    )
    check(
        "terminal-powershell-paths-bound",
        "HANDOFF_COMPLETED_EXECUTION_CONFLICT" in handoff_generator
        and "STATE=NO_CONTINUATION" in handoff_generator
        and "BOOT_COMPLETED_EXECUTION_CONFLICT" in boot_runtime
        and "Test-CerebroBootTerminalNoContinuation" in boot_runtime,
    )

    required = [
        "CURRENT-in-a-filename-confers-no-authority",
        "REJECT_INPUT_CONTINUE_CURRENT_SOURCE",
        "boot cerebro",
        "ACTIVE_CONTROL_TRANSFERRED",
    ]
    combined = handboot + source_authority + activation
    check("source-contract-tokens", all(token in combined for token in required))

    check(
        "universal-source-operational-does-not-require-working-source",
        "CEREBRO_SOURCE_OPERATIONAL" in boot_control
        and "It does not require Working Source or local runtime access." in boot_control,
    )
    check(
        "conversation-source-alignment-is-explicit",
        "CONVERSATION_SOURCE_ALIGNMENT" in boot_control
        and "conversation_source_alignment:" in boot_architecture
        and "local_working_source_equality_required: false" in boot_architecture,
    )
    check(
        "strict-cerebro-sync-verified-is-separate",
        "CEREBRO_SYNC_VERIFIED" in boot_control
        and "canonical cerebro_sync" in boot_control
        and "strict_non_equivalence: CEREBRO_SYNC_VERIFIED" in terminology,
    )
    check(
        "legacy-handboot-is-local-machine-extension",
        "class: LOCAL_MACHINE_CAPABILITY_EXTENSION" in handboot
        and "status_dimension: LOCAL_MACHINE_OPERATIONAL_VERIFIED" in handboot
        and "does_not_gate: [CEREBRO_SOURCE_OPERATIONAL, CONVERSATION_SOURCE_ALIGNMENT]" in handboot,
    )
    check(
        "ordinary-status-routes-source-first",
        "INT-OPS-001" in interaction
        and "resolve_primary_dimension: CEREBRO_SOURCE_OPERATIONAL" in interaction,
    )
    check(
        "source-sync-phrase-routes-conversation-alignment",
        "INT-OPS-002" in interaction
        and "resolve_primary_dimension: CONVERSATION_SOURCE_ALIGNMENT" in interaction
        and "do_not_promote_to: CEREBRO_SYNC_VERIFIED" in interaction,
    )
    check(
        "local-caveat-is-relevance-conditioned",
        "INT-OPS-004" in interaction
        and "suppress_automatic_working_source_caveat: true" in interaction
        and "surface_only_when_explicitly_asked_or_material: true" in interaction,
    )
    check(
        "success-semantics-are-available",
        "Yes — this conversation is now running against the current authoritative Cerebro Source, and Cerebro is actively being used for my responses." in interaction,
    )
    check(
        "birth-kernel-contract-identities-and-consumption",
        all(token in boot_architecture for token in (
            "CEREBRO-HMI-BIRTH-KERNEL-001","CEREBRO-ROLE-BIRTH-KERNEL-001",
            "receipt_required_before_first_operational_response: true",
            "required_receipt_fields: [id, version, fingerprint, consumed]",
        ))
        and all(token in boot_runtime for token in (
            "$hmiKernelFingerprint","$roleKernelFingerprint","birth_kernels = [ordered]@{",
            "consumed = $true","handboot-receipt/v0.2",
        )),
    )
    succession_order_tokens=(
        "identity","HMI-birth-kernel","ROLE-birth-kernel","Fresh-World-currentness",
        "lineage-wisdom","reconcile","canaries","Arvetone-last","Identitetshilsen","READY",
    )
    check(
        "fresh-world-succession-order-frozen",
        "zero_live_state_inheritance: true" in boot_architecture
        and "zero_live_state_inheritance = $true" in boot_runtime
        and all(token in boot_architecture for token in succession_order_tokens)
        and all(token in boot_runtime for token in succession_order_tokens)
        and "$successionFingerprint" in boot_runtime
        and "succession.completed = ($principalSuccession.result" in boot_runtime,
    )
    check(
        "P669-principal-succession-permit-gates-ready",
        all(token in boot_runtime for token in (
            "function Test-CerebroPrincipalSuccessionPermit",
            "PRINCIPAL_SUCCESSION_BOUND_READER_AND_EXACT_BINDING_REQUIRED",
            "PRINCIPAL_SUCCESSION_PERMIT_CURRENT_READBACK_REQUIRED",
            "PRINCIPAL_SUCCESSION_PERMIT_FINGERPRINT_MISMATCH",
            "PRINCIPAL_SUCCESSION_LIVED_CONTINUITY_DEBT_BLOCK",
            "PRINCIPAL_SUCCESSION_COLD_SUCCESSOR_CANARY_NONPASS",
            "PRINCIPAL_SUCCESSION_PRIVATE_CONTENT_OR_LOCATOR_PROHIBITED",
            "principal_permit = $principalSuccession",
            "succession.final_state = $(if ($principalSuccession.result",
        )),
    )
    check(
        "provider-global-presemantic-effect-remains-open",
        "provider_global_presemantic_effect: OPEN_NOT_PROVEN" in boot_architecture,
    )
    check(
        "bounded-adminpulse-and-nerve-authority-boundary",
        all(token in boot_architecture for token in (
            "bounded_operational_pulses:",
            "purpose: CURRENT_PM_CONTROL_AND_PROTOBOX_DELTA_SCAN",
            "provider_tails_required: [PM_CONTROL, PROTOBOX]",
            "missing_either_provider_tail_effect: UNKNOWN_HOLD",
            "output_vocabulary: [delta, NONE]",
            "purpose: SIGNAL_AND_TRANSPORT_ONLY",
            "may_create_control_state: false",
            "provider_global_or_dormant_peer_wake_effect: NOT_PROVEN",
        ))
        and all(token in boot_runtime for token in (
            "function Invoke-CerebroFreshWorldOperationalPulse",
            "PROVIDER_TAIL_READER_UNBOUND",
            "ADMINPULSE_BOTH_PROVIDER_TAILS_REQUIRED",
            "ADMINPULSE_CURRENT_EXACT_WATERMARKS_REQUIRED",
            "operational_pulse = $operationalPulse",
        )),
    )

    check(
        "K157-single-profile-projection-awakening-contract",
        all(token in boot_architecture for token in (
            "current_civilization_method_profile:", "CURRENT_CIVILIZATION_METHOD_PROFILE",
            "ROLE_METHOD_PROJECTION", "AWAKENING_RECEIPT", "new_receipt_schema: false",
        )) and "MCP-079" in boot_control and "MCP-080" in boot_control,
    )
    check(
        "K157-readback-before-ready-and-complete",
        boot_runtime.index("$runtimeState.runtime.awakening_readback = Test-CerebroAwakeningReadback")
        < boot_runtime.index("$runtimeState.runtime.succession.final_state =")
        < boot_runtime.index("$finalAwakeningReadback = Test-CerebroAwakeningReadback")
        < boot_runtime.index("$bootState = 'COMPLETE'"),
    )

    if bootengine_path is not None:
        bootengine = bootengine_path.read_text(encoding="utf-8")
        bootengine_required = [
            "Command.NaturalLanguageAlias := boot cerebro",
            "Command.Match := CASE_INSENSITIVE_EXACT_TRIMMED",
            "Authority.First := github:morgul-tech/Cerebro-Source-1.0/main/cerebro.yaml",
            "FilenameMarker.CURRENT.Authority := NONE",
            "StaleDerivedState.Action := REJECT_INPUT_CONTINUE_CURRENT_SOURCE",
            "OperationalClaim.Requires := ACTIVE_CONTROL_TRANSFERRED_RECEIPT_AT_CURRENT_SOURCE_COMMIT",
        ]
        check("bootengine-earliest-gate-contract", all(token in bootengine for token in bootengine_required))

    passed = all(item["result"] == "PASS" for item in tests)
    return {
        "schema": "cerebro-boot-authority-selftest/v0.2",
        "result": "PASS" if passed else "FAIL",
        "tests": tests,
    }


def activation_probe(root: Path) -> dict[str, object]:
    report = selftest(root=root)
    passed = report["result"] == "PASS"
    return {
        "schema": ACTIVATION_SCHEMA,
        "result": "PASS" if passed else "FAIL",
        "authority": "DERIVED_OPERATIONAL_EVIDENCE",
        "binding_id": "",
        "proves_bindings": [],
        "normal_call_path_exercised": True,
        "source_status_semantics_verified": passed,
        "basis_files": [
            "mcp/boot-architecture-control.yaml",
            "standards/boot-critical-path-architecture.yaml",
            "standards/runtime/handboot.yaml",
            "modules/terminology/terms.yaml",
            "engines/interaction/rules.yaml",
        "tooling/runtime-host/boot_authority_selftest.py",
        "standards/session-handoff.yaml",
        "tooling/builder/templates/pshell/cerebro_handoff.ps1",
        "tooling/runtime-host/cerebro_boot.ps1",
        ],
        "source_state_fingerprint": "",
        "selftest": report,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="?", choices=("selftest", "activation-probe", "succession-canaries", "method-canaries"), default="selftest")
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--bootengine-path", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    root = args.source_root.resolve()
    if args.command in ("succession-canaries", "method-canaries"):
        tests = (principal_permit_runtime_canaries(root) if args.command == "succession-canaries"
                 else civilization_method_runtime_canaries(root))
        result = ("FAIL" if any(test["result"] == "FAIL" for test in tests)
                  else "HOLD_CAPABILITY" if any(test["result"] != "PASS" for test in tests)
                  else "PASS")
        report = {"schema": ("cerebro-principal-permit-runtime-canaries/v1" if args.command == "succession-canaries"
                             else "cerebro-civilization-method-canaries/v1"),
                  "result": result, "tests": tests}
    elif args.command == "activation-probe":
        report = activation_probe(root)
    else:
        report = selftest(root, args.bootengine_path)
    text = json.dumps(report, indent=2, ensure_ascii=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
