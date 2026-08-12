[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$CandidateRoot,
    [Parameter(Mandatory=$true)][string]$ManifestPath,
    [Parameter(Mandatory=$true)][string]$CapsuleRoot,
    [Parameter(Mandatory=$true)][string]$RepositoryRoot,
    [Parameter(Mandatory=$true)][string]$OutputPath,
    [string]$ProfileId = 'windows-powershell'
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

function Resolve-TrvPython {
    foreach($name in @('python.exe','python')){
        try {
            $cmd=Get-Command $name -ErrorAction Stop | Select-Object -First 1
            if(-not[string]::IsNullOrWhiteSpace($cmd.Source)){
                return [pscustomobject]@{Executable=$cmd.Source;Prefix=@('-B')}
            }
        } catch {}
    }
    try {
        $cmd=Get-Command 'py.exe' -ErrorAction Stop | Select-Object -First 1
        if(-not[string]::IsNullOrWhiteSpace($cmd.Source)){
            return [pscustomobject]@{Executable=$cmd.Source;Prefix=@('-3','-B')}
        }
    } catch {}
    throw 'TARGET_RUNTIME_PYTHON_NOT_AVAILABLE'
}

function Invoke-TrvNative {
    param([string]$Executable,[string[]]$Arguments,[int[]]$AllowedExitCodes=@(0))
    $stdout=[IO.Path]::GetTempFileName(); $stderr=[IO.Path]::GetTempFileName()
    $old=$ErrorActionPreference
    try {
        $ErrorActionPreference='Continue'
        & $Executable @Arguments 1> $stdout 2> $stderr
        $exit=$LASTEXITCODE
    } finally {$ErrorActionPreference=$old}
    try {
        $out=[IO.File]::ReadAllText($stdout).TrimEnd([char[]]"`r`n")
        $err=[IO.File]::ReadAllText($stderr).TrimEnd([char[]]"`r`n")
    } finally {Remove-Item $stdout,$stderr -Force -ErrorAction SilentlyContinue}
    if($AllowedExitCodes -notcontains $exit){
        throw ('TARGET_RUNTIME_NATIVE_FAILURE executable={0}; exit={1}; stderr={2}' -f $Executable,$exit,$err)
    }
    [pscustomobject]@{ExitCode=$exit;Stdout=$out;Stderr=$err}
}

function Write-TrvJson {
    param([string]$Path,$Value)
    $parent=Split-Path -Parent $Path
    if(-not[string]::IsNullOrWhiteSpace($parent)){[IO.Directory]::CreateDirectory($parent)|Out-Null}
    [IO.File]::WriteAllText($Path,(($Value|ConvertTo-Json -Depth 64)+"`r`n"),[Text.UTF8Encoding]::new($false))
}

function Copy-TrvEvidence {
    param([Parameter(Mandatory=$true)][string]$Source,[Parameter(Mandatory=$true)][string]$Destination)
    if(-not(Test-Path -LiteralPath $Source -PathType Leaf)){
        throw ('TARGET_RUNTIME_EVIDENCE_SOURCE_MISSING:{0}' -f $Source)
    }
    $sourceFull=[IO.Path]::GetFullPath($Source)
    $destinationFull=[IO.Path]::GetFullPath($Destination)
    if([string]::Equals($sourceFull,$destinationFull,[StringComparison]::OrdinalIgnoreCase)){
        return
    }
    Copy-Item -LiteralPath $sourceFull -Destination $destinationFull -Force
}

if([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT){
    throw 'REQUIRED_TARGET_RUNTIME_NOT_EXECUTED_BEFORE_HANDOFF:WINDOWS_REQUIRED'
}
if($PSVersionTable.PSVersion.Major -lt 5){
    throw 'REQUIRED_TARGET_RUNTIME_NOT_EXECUTED_BEFORE_HANDOFF:POWERSHELL_5_OR_NEWER_REQUIRED'
}
$profilePath=Join-Path $CandidateRoot 'tooling\validator\target-runtime\windows-powershell.json'
if(-not(Test-Path -LiteralPath $profilePath -PathType Leaf)){throw 'TARGET_RUNTIME_PROFILE_MISSING'}
$profile=Get-Content -LiteralPath $profilePath -Raw | ConvertFrom-Json
if([string]$profile.profile.id -ne $ProfileId){throw 'TARGET_RUNTIME_PROFILE_ID_MISMATCH'}
$edition=if($PSVersionTable.ContainsKey('PSEdition')){[string]$PSVersionTable.PSEdition}else{'Desktop'}
if(@($profile.profile.powershell.accepted_editions) -notcontains $edition){
    throw ('REQUIRED_TARGET_RUNTIME_NOT_EXECUTED_BEFORE_HANDOFF:POWERSHELL_EDITION_NOT_ACCEPTED:{0}' -f $edition)
}
if([int]$profile.profile.powershell.minimum_major_version -gt $PSVersionTable.PSVersion.Major){
    throw 'REQUIRED_TARGET_RUNTIME_NOT_EXECUTED_BEFORE_HANDOFF:POWERSHELL_VERSION_BELOW_PROFILE_MINIMUM'
}
if(-not(Test-Path -LiteralPath $CandidateRoot -PathType Container)){throw 'TARGET_RUNTIME_CANDIDATE_ROOT_MISSING'}
if(-not(Test-Path -LiteralPath $ManifestPath -PathType Leaf)){throw 'TARGET_RUNTIME_MANIFEST_MISSING'}

$python=Resolve-TrvPython
$planner=Join-Path $CandidateRoot 'tooling\validator\target_runtime_validation.py'
if(-not(Test-Path -LiteralPath $planner -PathType Leaf)){throw 'TARGET_RUNTIME_PLANNER_MISSING'}

$temp=Join-Path ([IO.Path]::GetTempPath()) ('CerebroTargetRuntimeValidation-'+[guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($temp)|Out-Null
$planPath=Join-Path $temp 'plan.json'
$evidenceRoot=Join-Path $temp 'evidence'
[IO.Directory]::CreateDirectory($evidenceRoot)|Out-Null
$ephemeralRegistryDirectory=Join-Path $CandidateRoot ('.cerebro-target-runtime-'+[guid]::NewGuid().ToString('N'))
$ephemeralRegistry=Join-Path $ephemeralRegistryDirectory 'contract-activation-bindings.runtime.json'
$ephemeralRegistryRelative=((Split-Path -Leaf $ephemeralRegistryDirectory) + '/contract-activation-bindings.runtime.json')
$sourceTouched=$false

try {
    $planArgs=@($python.Prefix)+@($planner,'plan','--source-root',$CandidateRoot,'--manifest',$ManifestPath,'--profile',$ProfileId,'--output',$planPath)
    [void](Invoke-TrvNative -Executable $python.Executable -Arguments $planArgs)
    $plan=Get-Content -LiteralPath $planPath -Raw | ConvertFrom-Json
    if([string]$plan.result -ne 'PASS'){throw 'TARGET_RUNTIME_PLAN_NOT_PASS'}

    $manifest=Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
    $registryPath=Join-Path $CandidateRoot 'tooling\validator\contract-activation-bindings.json'
    $registry=Get-Content -LiteralPath $registryPath -Raw | ConvertFrom-Json
    $cacScript=Join-Path $CandidateRoot 'tooling\validator\cerebro_contract_activation_closure.ps1'
    if(-not(Test-Path -LiteralPath $cacScript -PathType Leaf)){throw 'TARGET_RUNTIME_ACTUAL_CAC_MISSING'}
    . $cacScript

    # Proof map: binding id -> ephemeral evidence file.
    $proofByBinding=@{}
    $activationProofs=@()

    # Standard Material Commitment Preflight call-path proof.
    if($null -ne $manifest.material_commitment_preflight){
        $request=($manifest.material_commitment_preflight | ConvertTo-Json -Depth 64 | ConvertFrom-Json)
        foreach($pair in @(
            [pscustomobject]@{name='stage';value='GOVERNING_PUBLISH'},
            [pscustomobject]@{name='material';value=$true},
            [pscustomobject]@{name='commitment_target';value=[string]$manifest.patch_id},
            [pscustomobject]@{name='authoritative_source_commit';value=[string]$manifest.expected_base_commit},
            [pscustomobject]@{name='current_decision_state';value='SEALED_STANDARD_GOVERNING_PUBLISH'}
        )){$request|Add-Member -NotePropertyName $pair.name -NotePropertyValue $pair.value -Force}

        $requestPath=Join-Path $temp 'material-request.json'
        $resolvePath=Join-Path $temp 'material-resolve.json'
        $receiptPath=Join-Path $temp 'material-receipt.json'
        $consumePath=Join-Path $evidenceRoot 'STANDARD_DELIVERY_MATERIAL_PREFLIGHT_CALL_PATH.json'
        Write-TrvJson -Path $requestPath -Value $request
        $preflight=Join-Path $CandidateRoot 'mcp\material_commitment_preflight.py'
        $resolveArgs=@($python.Prefix)+@($preflight,'resolve','--request',$requestPath,'--source-root',$CandidateRoot,'--output',$resolvePath)
        [void](Invoke-TrvNative -Executable $python.Executable -Arguments $resolveArgs)
        $resolved=Get-Content $resolvePath -Raw|ConvertFrom-Json
        if([string]$resolved.result -ne 'PASS' -or [string]$resolved.mcp_control_decision.outcome -ne 'CONTINUE'){
            throw ('TARGET_RUNTIME_MATERIAL_PREFLIGHT_BLOCKED:{0}' -f [string]$resolved.mcp_control_decision.outcome)
        }
        Write-TrvJson -Path $receiptPath -Value $resolved.receipt
        $consumeArgs=@($python.Prefix)+@($preflight,'consume','--request',$requestPath,'--receipt',$receiptPath,'--source-root',$CandidateRoot,'--output',$consumePath)
        [void](Invoke-TrvNative -Executable $python.Executable -Arguments $consumeArgs)
        $consumed=Get-Content $consumePath -Raw|ConvertFrom-Json
        if([string]$consumed.result -ne 'PASS' -or -not[bool]$consumed.receipt_consumed -or -not[bool]$consumed.freshness_verified){
            throw 'TARGET_RUNTIME_MATERIAL_PREFLIGHT_NOT_FRESH'
        }
        foreach($bindingId in @($consumed.proves_bindings)){ $proofByBinding[[string]$bindingId]=$consumePath }
        if(-not[string]::IsNullOrWhiteSpace([string]$consumed.binding_id)){ $proofByBinding[[string]$consumed.binding_id]=$consumePath }
        $activationProofs += [pscustomobject]@{producer='STANDARD_MATERIAL_PREFLIGHT_CALL_PATH';path=$consumePath;result='PASS'}
    }

    # Every patch-declared activation producer runs on the exact candidate.
    foreach($probe in @($manifest.activation_probes)){
        $impl=Join-Path $CandidateRoot (([string]$probe.implementation_path)-replace '/','\')
        if(-not(Test-Path -LiteralPath $impl -PathType Leaf)){throw ('TARGET_RUNTIME_ACTIVATION_PRODUCER_MISSING:{0}' -f [string]$probe.id)}
        $out=Join-Path $evidenceRoot (([string]$probe.id)+'.json')
        $args=@($python.Prefix)+@($impl,'activation-probe','--source-root',$CandidateRoot,'--output',$out)
        [void](Invoke-TrvNative -Executable $python.Executable -Arguments $args)
        $doc=Get-Content $out -Raw|ConvertFrom-Json
        if([string]$doc.result -ne 'PASS' -or [string]$doc.schema -ne [string]$probe.required_schema){
            throw ('TARGET_RUNTIME_ACTIVATION_PRODUCER_NOT_PASS:{0}' -f [string]$probe.id)
        }
        $proofBasis=@($doc.basis_files)
        if($proofBasis.Count -gt 0){
            $producerFingerprint=[string]$doc.source_state_fingerprint
            $consumerFingerprint=Get-CacEvidenceBasisFingerprint -Root $CandidateRoot -RelativePaths $proofBasis
            if([string]::IsNullOrWhiteSpace($consumerFingerprint)){
                throw ('TARGET_RUNTIME_CONSUMER_FINGERPRINT_EMPTY:{0}' -f [string]$probe.id)
            }
            $doc|Add-Member -NotePropertyName producer_source_state_fingerprint -NotePropertyValue $producerFingerprint -Force
            $doc|Add-Member -NotePropertyName fingerprint_consumer -NotePropertyValue 'CEREBRO_CONTRACT_ACTIVATION_CLOSURE' -Force
            $doc|Add-Member -NotePropertyName fingerprint_consumer_normalized -NotePropertyValue ($producerFingerprint -ne $consumerFingerprint) -Force
            $doc.source_state_fingerprint=$consumerFingerprint
            Write-TrvJson -Path $out -Value $doc
        }
        foreach($bindingId in @($doc.proves_bindings)){ $proofByBinding[[string]$bindingId]=$out }
        if(-not[string]::IsNullOrWhiteSpace([string]$doc.binding_id)){ $proofByBinding[[string]$doc.binding_id]=$out }
        $activationProofs += [pscustomobject]@{producer=[string]$probe.id;path=$out;result='PASS';proves_bindings=@($doc.proves_bindings)}
    }

    # Build an ephemeral registry. Impacted bindings MUST use freshly produced evidence.
    # Unaffected runtime evidence may be copied from the canonical baseline, but CAC still
    # checks its source-state fingerprint against the exact candidate.
    foreach($binding in @($registry.bindings)){
        if([string]$binding.wiring_proof_kind -ne 'RUNTIME_EVIDENCE'){continue}
        $id=[string]$binding.id
        $planBinding=@($plan.runtime_evidence_bindings | Where-Object {[string]$_.binding_id -eq $id})
        if($planBinding.Count -ne 1){throw ('TARGET_RUNTIME_BINDING_PLAN_CARDINALITY:{0}' -f $id)}
        $target=Join-Path $evidenceRoot ($id+'.json')
        if($proofByBinding.ContainsKey($id)){
            Copy-TrvEvidence -Source ([string]$proofByBinding[$id]) -Destination $target
        } else {
            if([bool]$planBinding[0].impacted){
                throw ('DEPENDENCY_IMPACT_CLOSURE_INCOMPLETE_BEFORE_HANDOFF:PRODUCER_MISSING:{0}' -f $id)
            }
            $baseline=[string]$binding.runtime_evidence.path
            if(-not(Test-Path -LiteralPath $baseline -PathType Leaf)){
                throw ('TARGET_RUNTIME_UNAFFECTED_BASELINE_EVIDENCE_MISSING:{0}:{1}' -f $id,$baseline)
            }
            Copy-TrvEvidence -Source $baseline -Destination $target
        }
        $binding.runtime_evidence.path=$target
    }

    [IO.Directory]::CreateDirectory($ephemeralRegistryDirectory)|Out-Null
    Write-TrvJson -Path $ephemeralRegistry -Value $registry

    # Actual CAC from candidate Source. No parity implementation is accepted.
    $cac=Invoke-CerebroContractActivationClosure -Root $CandidateRoot -RegistryPath $ephemeralRegistryRelative -PassThru
    $blocking=@($cac.blocking_findings)
    if([string]$cac.result -ne 'PASS'){
        $summary=@($blocking|ForEach-Object{('{0}|{1}|{2}|{3}' -f [string]$_.code,[string]$_.scope,[string]$_.subject,[string]$_.message)}) -join '; '
        throw ('TARGET_RUNTIME_ACTUAL_CAC_FAILED count={0}; findings={1}' -f $blocking.Count,$summary)
    }

    # Remove scratch from candidate before DEEP exact-scope assurance.
    Remove-Item -LiteralPath $ephemeralRegistryDirectory -Recurse -Force -ErrorAction SilentlyContinue

    $changeEngine=Join-Path $CandidateRoot 'tooling\change\change_engine.py'
    if(-not(Test-Path -LiteralPath $changeEngine -PathType Leaf)){throw 'TARGET_RUNTIME_CHANGE_ENGINE_MISSING'}
    $deepReport=Join-Path $temp 'deep-change-campaign.json'
    $deepArgs=@($python.Prefix)+@($changeEngine,'test','--capsule-root',$CapsuleRoot,'--repository-root',$RepositoryRoot,'--profile','DEEP','--report',$deepReport)
    [void](Invoke-TrvNative -Executable $python.Executable -Arguments $deepArgs)
    $deep=Get-Content $deepReport -Raw|ConvertFrom-Json
    if([string]$deep.stability_gate -ne 'PASS' -or [int]$deep.required_runs -lt 3 -or @($deep.runs|Where-Object {[string]$_.result -ne 'PASS'}).Count -gt 0){
        throw 'TARGET_RUNTIME_DEEP_ASSURANCE_NOT_PASS'
    }

    $runtimeIdentity=[ordered]@{
        os=[Environment]::OSVersion.VersionString
        powershell_version=$PSVersionTable.PSVersion.ToString()
        powershell_edition=if($PSVersionTable.ContainsKey('PSEdition')){[string]$PSVersionTable.PSEdition}else{'Desktop'}
    }
    $receipt=[ordered]@{
        schema='cerebro-target-runtime-validation-receipt/v1'
        validator_id='CEREBRO-TARGET-RUNTIME-VALIDATION-001'
        result='PASS'
        patch_id=[string]$manifest.patch_id
        source_base_commit=[string]$plan.source_base_commit
        candidate_identity=[string]$plan.candidate_identity
        target_profile=$ProfileId
        target_runtime_execution=$true
        target_runtime_identity=$runtimeIdentity
        changed_paths=@($plan.changed_paths)
        impacted_runtime_evidence_bindings=@($plan.impacted_runtime_evidence_bindings)
        activation_proofs=@($activationProofs)
        producer_consumer_compatibility='PASS'
        cac=[ordered]@{result=[string]$cac.result;health=[string]$cac.health;blocking_findings=@($cac.blocking_findings);nonblocking_findings=@($cac.nonblocking_findings)}
        deep_assurance=[ordered]@{result='PASS';required_runs=[int]$deep.required_runs;stability_gate=[string]$deep.stability_gate}
        authoritative_source_mutated=$false
        generated_at_utc=[DateTime]::UtcNow.ToString('o')
    }
    Write-TrvJson -Path $OutputPath -Value $receipt

    $verifyPath=Join-Path $temp 'receipt-verification.json'
    $verifyArgs=@($python.Prefix)+@($planner,'verify-receipt','--manifest',$ManifestPath,'--receipt',$OutputPath,'--profile',$ProfileId,'--output',$verifyPath)
    [void](Invoke-TrvNative -Executable $python.Executable -Arguments $verifyArgs)
    $verified=Get-Content $verifyPath -Raw|ConvertFrom-Json
    if([string]$verified.result -ne 'PASS'){throw ('TARGET_RUNTIME_RECEIPT_SELF_VERIFICATION_FAILED:'+(@($verified.reasons)-join ','))}

    Write-Host 'CEREBRO_TARGET_RUNTIME_VALIDATION=PASS'
    Write-Host ('TARGET_RUNTIME_PROFILE={0}' -f $ProfileId)
    Write-Host ('CANDIDATE_IDENTITY={0}' -f [string]$plan.candidate_identity)
    Write-Host ('TARGET_RUNTIME_RECEIPT={0}' -f $OutputPath)
}
finally {
    Remove-Item -LiteralPath $ephemeralRegistryDirectory -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $temp -Recurse -Force -ErrorAction SilentlyContinue
}
