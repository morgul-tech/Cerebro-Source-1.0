[CmdletBinding()]
param(
    [string]$BundlePath = '',
    [string]$WorkingSourcePath = 'D:\Cerebro\Source\Cerebro_Source_v1.0',
    [string]$HumanExecutionHandoffFingerprint = ''
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$ExpectedBundleSha256 = '__BUNDLE_SHA256__'
$ExpectedBundleFilename = '__BUNDLE_FILENAME__'
$LauncherFile = $MyInvocation.MyCommand.Path
$LauncherDirectory = Split-Path -Parent ([IO.Path]::GetFullPath($LauncherFile))
$StableFailureHandoff = Join-Path -Path $LauncherDirectory -ChildPath 'CEREBRO_PATCH_FAIL.json'
$StableSuccessHandoff = Join-Path -Path $LauncherDirectory -ChildPath 'CEREBRO_PATCH_SUCCESS.json'

function Get-Sha256 {
    param([string]$LiteralPath)
    return (Get-FileHash -LiteralPath $LiteralPath -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-Sha256FromText {
    param([Parameter(Mandatory=$true)][string]$Text)
    $algorithm=[Security.Cryptography.SHA256]::Create()
    try {
        $bytes=[Text.UTF8Encoding]::new($false).GetBytes($Text)
        return ([BitConverter]::ToString($algorithm.ComputeHash($bytes))).Replace('-','').ToLowerInvariant()
    }
    finally {
        $algorithm.Dispose()
    }
}

function Get-HumanExecutionHandoffFingerprint {
    param(
        [Parameter(Mandatory=$true)][string]$LauncherSha256,
        [Parameter(Mandatory=$true)][string]$BundleSha256
    )
    $subject=('cerebro-human-execution-handoff/v1|HASH_BOUND_POWERSHELL|{0}|{1}' -f $LauncherSha256.ToLowerInvariant(),$BundleSha256.ToLowerInvariant())
    return Get-Sha256FromText -Text $subject
}

function Assert-HumanExecutionHandoff {
    param(
        [Parameter(Mandatory=$true)]$Manifest,
        [Parameter(Mandatory=$true)][string]$BundleSha256
    )

    if($Manifest.PSObject.Properties.Name -notcontains 'human_execution_handoff'){
        throw 'HUMAN_EXECUTION_HANDOFF_CONTRACT_MISSING'
    }
    $contract=$Manifest.human_execution_handoff
    $validContract=(
        [bool]$contract.required -and
        [string]$contract.schema -eq 'cerebro-human-execution-handoff/v1' -and
        [string]$contract.profile -eq 'HASH_BOUND_POWERSHELL' -and
        [string]$contract.fingerprint_parameter -eq 'HumanExecutionHandoffFingerprint'
    )
    if(-not$validContract){throw 'HUMAN_EXECUTION_HANDOFF_CONTRACT_INVALID'}
    if([string]::IsNullOrWhiteSpace($HumanExecutionHandoffFingerprint)){
        throw 'HUMAN_EXECUTION_HANDOFF_FINGERPRINT_REQUIRED'
    }
    $launcherSha=Get-Sha256 -LiteralPath $LauncherFile
    $expected=Get-HumanExecutionHandoffFingerprint -LauncherSha256 $launcherSha -BundleSha256 $BundleSha256
    if(-not[string]::Equals($HumanExecutionHandoffFingerprint,$expected,[StringComparison]::OrdinalIgnoreCase)){
        throw 'HUMAN_EXECUTION_HANDOFF_FINGERPRINT_MISMATCH'
    }
    return @{
        schema='cerebro-human-execution-handoff-launch-proof/v1'
        result='PASS'
        profile='HASH_BOUND_POWERSHELL'
        launcher_sha256=$launcherSha
        bundle_sha256=$BundleSha256
        handoff_fingerprint=$expected
        artifact_identity_consumed=$true
    }
}

function New-AttemptContext {
    $started=[DateTime]::UtcNow
    $attemptId=$started.ToString('yyyyMMddTHHmmssfffZ')+'-'+[guid]::NewGuid().ToString('N')
    $root='D:\Cerebro\Run\Evidence\Attempts'

    try {
        [IO.Directory]::CreateDirectory($root) | Out-Null
    }
    catch {
        $root=Join-Path ([IO.Path]::GetTempPath()) 'Cerebro\attempts'
        [IO.Directory]::CreateDirectory($root) | Out-Null
    }

    $directory=Join-Path $root $attemptId
    [IO.Directory]::CreateDirectory($directory) | Out-Null

    return [ordered]@{
        attempt_id=$attemptId
        directory=$directory
        events_path=(Join-Path $directory 'events.jsonl')
        attempt_path=(Join-Path $directory 'attempt.json')
        started=$started
        patch_id=''
        expected_base_commit=''
        bundle_sha256=''
        kernel_sha256=''
        start_commit=''
    }
}

function Write-AttemptEvent {
    param($Context,[string]$Event,[hashtable]$Data=@{})

    $row=[ordered]@{
        attempt_id=$Context.attempt_id
        timestamp_utc=[DateTime]::UtcNow.ToString('o')
        event=$Event
        data=$Data
    }

    [IO.File]::AppendAllText(
        $Context.events_path,
        (($row | ConvertTo-Json -Compress -Depth 8)+"`r`n"),
        [Text.UTF8Encoding]::new($false)
    )
}

function Complete-Attempt {
    param(
        $Context,
        [string]$Result,
        [string]$FailedPhase='',
        [string]$ReachedStage='',
        [string]$FailureFamily='',
        [string]$MutationAssessment='',
        [string]$ReceiptRef='',
        [string]$DiagnosticRef='',
        [string]$SuccessTransportRef=''
    )

    $completed=[DateTime]::UtcNow
    $snapshot=Get-WorkingSourceSnapshot -Path $WorkingSourcePath

    $receiptCopy=''
    if(-not [string]::IsNullOrWhiteSpace($ReceiptRef)){
        if(Test-Path -LiteralPath $ReceiptRef -PathType Leaf){
            $receiptCopy=Join-Path $Context.directory 'receipt.json'
            Copy-Item -LiteralPath $ReceiptRef -Destination $receiptCopy -Force
        }
    }

    $diagnosticCopy=''
    if(-not [string]::IsNullOrWhiteSpace($DiagnosticRef)){
        if(Test-Path -LiteralPath $DiagnosticRef -PathType Leaf){
            $diagnosticCopy=Join-Path $Context.directory 'diagnostic.json'
            Copy-Item -LiteralPath $DiagnosticRef -Destination $diagnosticCopy -Force
        }
    }

    $record=[ordered]@{
        schema='cerebro-patch-attempt/v1'
        attempt_id=$Context.attempt_id
        patch_id=$Context.patch_id
        started_at_utc=$Context.started.ToString('o')
        completed_at_utc=$completed.ToString('o')
        duration_ms=[int64]($completed-$Context.started).TotalMilliseconds
        result=$Result
        expected_base_commit=$Context.expected_base_commit
        start_commit=$Context.start_commit
        result_commit=$snapshot.head
        working_tree_result=$snapshot.working_tree
        failed_phase=$FailedPhase
        reached_stage=$ReachedStage
        failure_family=$FailureFamily
        source_mutation_assessment=$MutationAssessment
        receipt_ref=$ReceiptRef
        receipt_copy=$receiptCopy
        success_transport_ref=$SuccessTransportRef
        diagnostic_ref=$DiagnosticRef
        diagnostic_copy=$diagnosticCopy
        bundle_sha256=$Context.bundle_sha256
        kernel_sha256=$Context.kernel_sha256
    }

    [IO.File]::WriteAllText(
        $Context.attempt_path,
        (($record | ConvertTo-Json -Depth 10)+"`r`n"),
        [Text.UTF8Encoding]::new($false)
    )

    if($Result -eq 'PASS'){
        Write-AttemptEvent -Context $Context -Event 'ATTEMPT_PASS'
    }
    else {
        Write-AttemptEvent -Context $Context -Event 'ATTEMPT_FAIL' -Data @{
            failure_family=$FailureFamily
            reached_stage=$ReachedStage
        }
    }
}

$Attempt=New-AttemptContext
Write-AttemptEvent -Context $Attempt -Event 'ATTEMPT_STARTED'

function Invoke-ChildCaptured {
    param(
        [string]$PowerShellPath,
        [string[]]$ArgumentList,
        [string]$Phase
    )

    $stdoutFile = [IO.Path]::GetTempFileName()
    $stderrFile = [IO.Path]::GetTempFileName()
    $previousPreference = $ErrorActionPreference
    $childExitCode = $null
    try {
        $ErrorActionPreference = 'Continue'
        & $PowerShellPath @ArgumentList 1> $stdoutFile 2> $stderrFile
        $childExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }

    try {
        $stdoutText = [IO.File]::ReadAllText($stdoutFile)
        $stderrText = [IO.File]::ReadAllText($stderrFile)
    }
    finally {
        Remove-Item -LiteralPath $stdoutFile,$stderrFile -Force -ErrorAction SilentlyContinue
    }

    return [pscustomobject]@{
        Phase = $Phase
        ExitCode = $childExitCode
        Stdout = $stdoutText
        Stderr = $stderrText
    }
}

function Get-CapturedField {
    param(
        [string]$Text,
        [string]$Name,
        [ValidateSet('FIRST','LAST','UNIQUE')][string]$Selection='FIRST'
    )
    $pattern = '(?m)^' + [regex]::Escape($Name) + '=(.*)$'
    $fieldMatches = [regex]::Matches($Text,$pattern)
    if ($fieldMatches.Count -eq 0) { return '' }
    if ($Selection -eq 'UNIQUE' -and $fieldMatches.Count -ne 1) {
        throw ('CAPTURED_FIELD_CARDINALITY_INVALID:{0}:{1}' -f $Name,$fieldMatches.Count)
    }
    $index = if ($Selection -eq 'LAST') { $fieldMatches.Count - 1 } else { 0 }
    return $fieldMatches[$index].Groups[1].Value.Trim()
}

function Assert-SuccessReceiptProtocol {
    $fixture = "TARGET_RUNTIME_RECEIPT=trv.json`r`nDELIVERY_RECEIPT=delivery.json`r`nRECEIPT=legacy.json"
    $selected = Get-CapturedField -Text $fixture -Name 'DELIVERY_RECEIPT' -Selection 'UNIQUE'
    if ($selected -ne 'delivery.json') { throw 'SUCCESS_RECEIPT_PROTOCOL_SELECTION_FAILED' }

    $duplicateRejected = $false
    try {
        [void](Get-CapturedField -Text ($fixture + "`r`nDELIVERY_RECEIPT=duplicate.json") `
            -Name 'DELIVERY_RECEIPT' -Selection 'UNIQUE')
    }
    catch {
        if ($_.Exception.Message -like 'CAPTURED_FIELD_CARDINALITY_INVALID:DELIVERY_RECEIPT:*') {
            $duplicateRejected = $true
        }
        else { throw }
    }
    if (-not $duplicateRejected) { throw 'SUCCESS_RECEIPT_PROTOCOL_DUPLICATE_NOT_REJECTED' }
}

function Publish-SuccessHandoff {
    param(
        [Parameter(Mandatory=$true)]$Manifest,
        [Parameter(Mandatory=$true)][string]$ReceiptPath,
        [Parameter(Mandatory=$true)][string]$ResultingCommit
    )

    if([string]::IsNullOrWhiteSpace($ReceiptPath) -or -not(Test-Path -LiteralPath $ReceiptPath -PathType Leaf)){
        throw 'SUCCESS_RECEIPT_NOT_FOUND'
    }
    try {$receipt=Get-Content -LiteralPath $ReceiptPath -Raw|ConvertFrom-Json}
    catch {throw ('SUCCESS_RECEIPT_JSON_INVALID:{0}' -f $_.Exception.Message)}

    $valid=(
        [string]$receipt.schema -eq 'cerebro-delivery-kernel-receipt/v2' -and
        [string]$receipt.result -eq 'PASS' -and
        [string]$receipt.patch_id -eq [string]$Manifest.patch_id -and
        [string]$receipt.attempt_id -eq [string]$Attempt.attempt_id -and
        [string]$receipt.authoritative_source -eq 'origin/main' -and
        [string]$receipt.authoritative_commit -eq $ResultingCommit -and
        [string]$receipt.working_source_commit -eq $ResultingCommit -and
        [bool]$receipt.authoritative_source_equals_working_source -and
        [string]$receipt.source_equality -eq 'VERIFIED' -and
        [string]$receipt.working_tree -eq 'CLEAN' -and
        [string]$receipt.sync_action -eq 'CEREBRO_SYNC_EXECUTED' -and
        [bool]$receipt.cerebro_sync_verified
    )
    if (-not $valid) { throw 'SUCCESS_RECEIPT_CONTRACT_MISMATCH' }

    $archivePath=[string]$receipt.full_recovery_snapshot.archive_path
    $archiveSha=[string]$receipt.full_recovery_snapshot.archive_sha256
    $snapshotReceipt=[string]$receipt.full_recovery_snapshot.receipt_path
    if([string]::IsNullOrWhiteSpace($archivePath) -or -not(Test-Path -LiteralPath $archivePath -PathType Leaf) -or
       [string]::IsNullOrWhiteSpace($snapshotReceipt) -or -not(Test-Path -LiteralPath $snapshotReceipt -PathType Leaf) -or
       [string]::IsNullOrWhiteSpace($archiveSha) -or (Get-Sha256 -LiteralPath $archivePath) -ne $archiveSha){
        throw 'SUCCESS_RECEIPT_RECOVERY_SNAPSHOT_PROOF_INVALID'
    }

    Copy-Item -LiteralPath $ReceiptPath -Destination $StableSuccessHandoff -Force
    if(-not(Test-Path -LiteralPath $StableSuccessHandoff -PathType Leaf) -or
       (Get-Sha256 -LiteralPath $StableSuccessHandoff) -ne (Get-Sha256 -LiteralPath $ReceiptPath)){
        throw 'SUCCESS_RECEIPT_TRANSPORT_COPY_VERIFICATION_FAILED'
    }

    return [pscustomobject]@{
        CanonicalPath=$ReceiptPath
        TransportPath=$StableSuccessHandoff
        ResultCommit=$ResultingCommit
        SourceEquality='VERIFIED'
        CerebroSyncVerified=$true
    }
}

function Get-WorkingSourceSnapshot {
    param([string]$Path)
    $snapshot = [ordered]@{
        head = ''
        branch = ''
        working_tree = 'UNAVAILABLE'
    }

    try {
        if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
            return $snapshot
        }
        $gitPath = (Get-Command git.exe -ErrorAction Stop | Select-Object -First 1).Source
        $previousPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            $head = & $gitPath -C $Path rev-parse HEAD 2>$null
            $headExit = $LASTEXITCODE
            $branch = & $gitPath -C $Path branch --show-current 2>$null
            $branchExit = $LASTEXITCODE
            $status = & $gitPath -C $Path status --porcelain --untracked-files=all 2>$null
            $statusExit = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousPreference
        }

        if ($headExit -eq 0) { $snapshot.head = ([string]$head).Trim() }
        if ($branchExit -eq 0) { $snapshot.branch = ([string]$branch).Trim() }
        if ($statusExit -eq 0) {
            if ([string]::IsNullOrWhiteSpace(([string]$status))) {
                $snapshot.working_tree = 'CLEAN'
            }
            else {
                $snapshot.working_tree = 'DIRTY'
            }
        }
    }
    catch {}

    return $snapshot
}

function Write-LocalFallbackHandoff {
    param($Seed,[string]$DiagnosticError='')
    $fallback=[ordered]@{
        schema='cerebro-diagnostic-fallback-seed/v1'
        state='PATCH_FAIL'
        authority='EVIDENCE_ONLY'
        diagnostic_degraded=$true
        diagnostic_error=$DiagnosticError
        attempt_id=[string]$Seed.attempt_id
        patch_id=[string]$Seed.patch_id
        expected_base_commit=[string]$Seed.expected_base_commit
        phase=[string]$Seed.phase
        reached_stage=[string]$Seed.reached_stage
        failure_family=[string]$Seed.failure_family
        error=[string]$Seed.error
        child_exit_code=$Seed.child_exit_code
        source_mutation_assessment=[string]$Seed.source_mutation_assessment
        created_at_utc=[DateTime]::UtcNow.ToString('o')
    }
    [IO.File]::WriteAllText(
        $StableFailureHandoff,
        (($fallback | ConvertTo-Json -Depth 8)+"`r`n"),
        [Text.UTF8Encoding]::new($false)
    )
    [pscustomobject]@{
        State='FALLBACK'
        CapsuleId=''
        CanonicalPath=$StableFailureHandoff
        TransportPath=$StableFailureHandoff
        HandoffPath=$StableFailureHandoff
        Degraded=$true
    }
}

function Write-FailureCapsule {
    param($Manifest,[string]$KernelSha256,[string]$BundleSha256,$ChildResult)

    $combined=([string]$ChildResult.Stdout)+"`r`n"+([string]$ChildResult.Stderr)
    $reachedStage=Get-CapturedField -Text $combined -Name 'REACHED_STAGE'
    $failureFamily=Get-CapturedField -Text $combined -Name 'FAILURE_FAMILY'
    $errorText=Get-CapturedField -Text $combined -Name 'ERROR'

    if([string]::IsNullOrWhiteSpace($failureFamily)){ $failureFamily='CHILD_PROCESS_FAILURE' }
    if([string]::IsNullOrWhiteSpace($reachedStage)){ $reachedStage=[string]$ChildResult.Phase }
    if([string]::IsNullOrWhiteSpace($errorText)){ $errorText='See canonical diagnostic capsule for bounded child output.' }

    $snapshot=Get-WorkingSourceSnapshot -Path $WorkingSourcePath
    $mutationAssessment='UNKNOWN'
    if($snapshot.working_tree -eq 'CLEAN'){ $mutationAssessment='NO_UNCOMMITTED_SOURCE_MUTATION_PRESENT' }
    elseif($snapshot.working_tree -eq 'DIRTY'){ $mutationAssessment='UNCOMMITTED_SOURCE_STATE_PRESENT' }

    $patchId=''
    $expectedBase=''
    if($null -ne $Manifest){
        $patchId=[string]$Manifest.patch_id
        $expectedBase=[string]$Manifest.expected_base_commit
    }

    $seed=[ordered]@{
        attempt_id=$Attempt.attempt_id
        patch_id=$patchId
        expected_base_commit=$expectedBase
        phase=[string]$ChildResult.Phase
        reached_stage=$reachedStage
        failure_family=$failureFamily
        error=$errorText
        child_exit_code=$ChildResult.ExitCode
        child_stdout=[string]$ChildResult.Stdout
        child_stderr=[string]$ChildResult.Stderr
        source_mutation_assessment=$mutationAssessment
        bundle_path=$BundlePath
        bundle_sha256=$BundleSha256
        kernel_sha256=$KernelSha256
    }

    $bridgePath=Join-Path $WorkingSourcePath 'tooling\delivery\Cerebro.StandardDiagnosticBridge.ps1'
    try {
        if(-not(Test-Path -LiteralPath $bridgePath -PathType Leaf)){ throw 'STANDARD_DIAGNOSTIC_BRIDGE_NOT_FOUND' }
        . $bridgePath
        $diagnostic=Invoke-CerebroStandardDiagnosticBridge `
            -Mode Capture -WorkingSourcePath $WorkingSourcePath `
            -StableHandoffPath $StableFailureHandoff -Seed $seed
    }
    catch {
        $diagnostic=Write-LocalFallbackHandoff -Seed $seed -DiagnosticError $_.Exception.Message
    }

    Write-Host ''
    Write-Host 'PATCH FAIL'
    Write-Host ('FAILURE_FAMILY={0}' -f $failureFamily)
    Write-Host ('DIAGNOSTIC_FILE={0}' -f $StableFailureHandoff)
    Write-Host ('SOURCE_STATE={0}' -f $mutationAssessment)

    [pscustomobject]@{
        CanonicalPath=[string]$diagnostic.CanonicalPath
        HandoffPath=$StableFailureHandoff
        FailureFamily=$failureFamily
        ReachedStage=$reachedStage
        MutationAssessment=$mutationAssessment
        DiagnosticDegraded=[bool]$diagnostic.Degraded
    }
}

function Resolve-CanonicalDiagnostics {
    param([string]$PatchId,[string]$ResultingCommit)

    if([string]::IsNullOrWhiteSpace($PatchId) -or [string]::IsNullOrWhiteSpace($ResultingCommit)){ return }
    $bridgePath=Join-Path $WorkingSourcePath 'tooling\delivery\Cerebro.StandardDiagnosticBridge.ps1'
    if(-not(Test-Path -LiteralPath $bridgePath -PathType Leaf)){ return }

    try {
        . $bridgePath
        [void](Invoke-CerebroStandardDiagnosticBridge `
            -Mode Resolve -WorkingSourcePath $WorkingSourcePath `
            -StableHandoffPath $StableFailureHandoff `
            -PatchId $PatchId -ResultingCommit $ResultingCommit)
    }
    catch {}
}

if ([string]::IsNullOrWhiteSpace($BundlePath)) {
    $BundlePath = Join-Path -Path $LauncherDirectory -ChildPath $ExpectedBundleFilename
}

$temporaryRoot = $null
$manifest = $null
try {
    if (-not (Test-Path -LiteralPath $BundlePath -PathType Leaf)) {
        throw ('BUNDLE_NOT_FOUND:{0}' -f $BundlePath)
    }
    $bundleSha = Get-Sha256 $BundlePath
    if ($bundleSha -ne $ExpectedBundleSha256) {
        throw 'BUNDLE_SHA256_MISMATCH'
    }

    $temporaryRoot = Join-Path -Path ([IO.Path]::GetTempPath()) -ChildPath ('CerebroDelivery-' + [guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($temporaryRoot) | Out-Null
    Expand-Archive -LiteralPath $BundlePath -DestinationPath $temporaryRoot -Force

    $manifestPath = Join-Path -Path $temporaryRoot -ChildPath 'manifest.json'
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    Assert-SuccessReceiptProtocol
    $humanExecutionProof=Assert-HumanExecutionHandoff -Manifest $manifest -BundleSha256 $bundleSha
    $Attempt.patch_id=[string]$manifest.patch_id
    $Attempt.expected_base_commit=[string]$manifest.expected_base_commit
    $Attempt.bundle_sha256=$bundleSha
    $Attempt.start_commit=(Get-WorkingSourceSnapshot -Path $WorkingSourcePath).head
    Write-AttemptEvent -Context $Attempt -Event 'HUMAN_EXECUTION_HANDOFF_VERIFIED' -Data $humanExecutionProof
    Write-AttemptEvent -Context $Attempt -Event 'BUNDLE_VERIFIED'
    $kernelPath = Join-Path -Path $temporaryRoot -ChildPath 'kernel\Cerebro.StandardDeliveryKernel.ps1'
    $kernelSha = Get-Sha256 $kernelPath
    $Attempt.kernel_sha256=$kernelSha
    if ($kernelSha -ne [string]$manifest.kernel_sha256) {
        throw 'KERNEL_SHA256_MISMATCH'
    }

    $powershellPath = (Get-Command powershell.exe -ErrorAction Stop | Select-Object -First 1).Source
    $common = @(
        '-NoProfile','-ExecutionPolicy','Bypass','-File',$kernelPath,
        '-BundleRoot',$temporaryRoot,
        '-WorkingSourcePath',$WorkingSourcePath,
        '-LauncherPath',$LauncherFile,
        '-AttemptId',$Attempt.attempt_id
    )

    Write-AttemptEvent -Context $Attempt -Event 'SELFTEST_STARTED'
    $selfTest = Invoke-ChildCaptured -PowerShellPath $powershellPath `
        -ArgumentList ($common + @('-Mode','SelfTest')) -Phase 'SELFTEST'
    if ($selfTest.ExitCode -ne 0) {
        $diagnostic=Write-FailureCapsule -Manifest $manifest -KernelSha256 $kernelSha -BundleSha256 $bundleSha -ChildResult $selfTest
        Complete-Attempt -Context $Attempt -Result 'FAIL' -FailedPhase 'SELFTEST' `
            -ReachedStage $diagnostic.ReachedStage -FailureFamily $diagnostic.FailureFamily `
            -MutationAssessment $diagnostic.MutationAssessment `
            -DiagnosticRef $diagnostic.CanonicalPath
        exit 1
    }

    Write-AttemptEvent -Context $Attempt -Event 'SELFTEST_PASS'
    Write-AttemptEvent -Context $Attempt -Event 'APPLY_STARTED'

    $apply = Invoke-ChildCaptured -PowerShellPath $powershellPath `
        -ArgumentList ($common + @('-Mode','Apply')) -Phase 'APPLY'
    if ($apply.ExitCode -ne 0) {
        $diagnostic=Write-FailureCapsule -Manifest $manifest -KernelSha256 $kernelSha -BundleSha256 $bundleSha -ChildResult $apply
        Complete-Attempt -Context $Attempt -Result 'FAIL' -FailedPhase 'APPLY' `
            -ReachedStage $diagnostic.ReachedStage -FailureFamily $diagnostic.FailureFamily `
            -MutationAssessment $diagnostic.MutationAssessment `
            -DiagnosticRef $diagnostic.CanonicalPath
        exit 1
    }

    Write-AttemptEvent -Context $Attempt -Event 'APPLY_PASS'

    $resultingCommit=Get-CapturedField -Text ([string]$apply.Stdout) -Name 'AUTHORITATIVE_COMMIT' -Selection 'LAST'
    Resolve-CanonicalDiagnostics -PatchId ([string]$manifest.patch_id) -ResultingCommit $resultingCommit

    $receipt = Get-CapturedField -Text ([string]$apply.Stdout) -Name 'DELIVERY_RECEIPT' -Selection 'UNIQUE'
    try {
        $successHandoff=Publish-SuccessHandoff -Manifest $manifest -ReceiptPath $receipt -ResultingCommit $resultingCommit
    }
    catch {
        Write-Host ''
        Write-Host 'PATCH SUCCESS'
        Write-Host 'CEREBRO_SYNC_VERIFIED=TRUE'
        Write-Host ('RESULT_COMMIT={0}' -f $resultingCommit)
        Write-Host ('RECEIPT={0}' -f $receipt)
        Write-Host 'SUCCESS_HANDOFF=FAIL'
        Write-Host ('SUCCESS_HANDOFF_ERROR={0}' -f $_.Exception.Message)
        Complete-Attempt -Context $Attempt -Result 'PASS' -ReceiptRef $receipt
        exit 2
    }
    Write-Host ''
    Write-Host 'PATCH SUCCESS'
    Write-Host 'CEREBRO_SYNC_VERIFIED=TRUE'
    Write-Host 'AUTHORITATIVE_SOURCE_EQUALS_WORKING_SOURCE=TRUE'
    Write-Host ('RESULT_COMMIT={0}' -f $successHandoff.ResultCommit)
    Write-Host ('RECEIPT={0}' -f $successHandoff.CanonicalPath)
    Write-Host ('UPLOAD_FILE={0}' -f $successHandoff.TransportPath)
    Complete-Attempt -Context $Attempt -Result 'PASS' -ReceiptRef $receipt -SuccessTransportRef $successHandoff.TransportPath
    exit 0
}
catch {
    $transportResult = [pscustomobject]@{
        Phase = 'LAUNCHER'
        ExitCode = 1
        Stdout = ''
        Stderr = ($_ | Out-String)
    }
    $kernelShaForFailure = ''
    if ($null -ne $manifest -and $manifest.PSObject.Properties.Name -contains 'kernel_sha256') {
        $kernelShaForFailure = [string]$manifest.kernel_sha256
    }
    $bundleShaForFailure = ''
    if (Test-Path -LiteralPath $BundlePath -PathType Leaf) {
        try { $bundleShaForFailure = Get-Sha256 $BundlePath } catch {}
    }
    $diagnostic=Write-FailureCapsule -Manifest $manifest -KernelSha256 $kernelShaForFailure `
        -BundleSha256 $bundleShaForFailure -ChildResult $transportResult
    Complete-Attempt -Context $Attempt -Result 'FAIL' -FailedPhase 'LAUNCHER' `
        -ReachedStage $diagnostic.ReachedStage -FailureFamily $diagnostic.FailureFamily `
        -MutationAssessment $diagnostic.MutationAssessment `
        -DiagnosticRef $diagnostic.CanonicalPath
    exit 1
}
finally {
    if ($null -ne $temporaryRoot -and (Test-Path -LiteralPath $temporaryRoot)) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
