[CmdletBinding()]
param(
    [string]$BundlePath = '',
    [string]$WorkingSourcePath = 'D:\Cerebro\Source\Cerebro_Source_v1.0'
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$ExpectedBundleSha256 = '__BUNDLE_SHA256__'
$ExpectedBundleFilename = '__BUNDLE_FILENAME__'
$LauncherFile = $MyInvocation.MyCommand.Path
$LauncherDirectory = Split-Path -Parent ([IO.Path]::GetFullPath($LauncherFile))
$StableFailureHandoff = Join-Path -Path $LauncherDirectory -ChildPath 'CEREBRO_PATCH_FAIL.json'

function Get-Sha256 {
    param([string]$LiteralPath)
    return (Get-FileHash -LiteralPath $LiteralPath -Algorithm SHA256).Hash.ToLowerInvariant()
}

function New-AttemptContext {
    $started=[DateTime]::UtcNow
    $attemptId=$started.ToString('yyyyMMddTHHmmssfffZ')+'-'+[guid]::NewGuid().ToString('N')
    $root='D:\Cerebro\Run\attempts'

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
        [string]$DiagnosticRef=''
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
    param([string]$Text,[string]$Name)
    $pattern = '(?m)^' + [regex]::Escape($Name) + '=(.*)$'
    $match = [regex]::Match($Text,$pattern)
    if ($match.Success) { return $match.Groups[1].Value.Trim() }
    return ''
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

function Write-FailureCapsule {
    param(
        $Manifest,
        [string]$KernelSha256,
        [string]$BundleSha256,
        $ChildResult
    )

    $combined = ([string]$ChildResult.Stdout) + "`r`n" + ([string]$ChildResult.Stderr)
    $reachedStage = Get-CapturedField -Text $combined -Name 'REACHED_STAGE'
    $failureFamily = Get-CapturedField -Text $combined -Name 'FAILURE_FAMILY'
    $errorText = Get-CapturedField -Text $combined -Name 'ERROR'

    if ([string]::IsNullOrWhiteSpace($failureFamily)) {
        $failureFamily = 'CHILD_PROCESS_FAILURE'
    }
    if ([string]::IsNullOrWhiteSpace($reachedStage)) {
        $reachedStage = [string]$ChildResult.Phase
    }
    if ([string]::IsNullOrWhiteSpace($errorText)) {
        $errorText = 'See child_stdout and child_stderr in diagnostic capsule.'
    }

    $snapshot = Get-WorkingSourceSnapshot -Path $WorkingSourcePath
    $mutationAssessment = 'UNKNOWN'
    if ($snapshot.working_tree -eq 'CLEAN') {
        $mutationAssessment = 'NO_UNCOMMITTED_SOURCE_MUTATION_PRESENT'
    }
    elseif ($snapshot.working_tree -eq 'DIRTY') {
        $mutationAssessment = 'UNCOMMITTED_SOURCE_STATE_PRESENT'
    }

    $diagnosticRoot = 'D:\Cerebro\Run\diagnostics'
    try {
        [IO.Directory]::CreateDirectory($diagnosticRoot) | Out-Null
    }
    catch {
        $diagnosticRoot = Join-Path -Path ([IO.Path]::GetTempPath()) -ChildPath 'Cerebro\diagnostics'
        [IO.Directory]::CreateDirectory($diagnosticRoot) | Out-Null
    }

    $timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $capsulePath = Join-Path -Path $diagnosticRoot -ChildPath ('CEREBRO_PATCH_FAIL_' + $timestamp + '.json')

    $patchId = ''
    $expectedBase = ''
    if ($null -ne $Manifest) {
        $patchId = [string]$Manifest.patch_id
        $expectedBase = [string]$Manifest.expected_base_commit
    }

    $capsule = [ordered]@{
        schema = 'cerebro-patch-failure-diagnostic/v1'
        result = 'FAIL'
        attempt_id = $Attempt.attempt_id
        patch_id = $patchId
        failed_phase = [string]$ChildResult.Phase
        reached_stage = $reachedStage
        failure_family = $failureFamily
        error = $errorText
        child_exit_code = [int]$ChildResult.ExitCode
        child_stdout = [string]$ChildResult.Stdout
        child_stderr = [string]$ChildResult.Stderr
        expected_base_commit = $expectedBase
        working_source_path = $WorkingSourcePath
        working_head_after_failure = $snapshot.head
        working_branch_after_failure = $snapshot.branch
        working_tree_after_failure = $snapshot.working_tree
        source_mutation_assessment = $mutationAssessment
        bundle_path = $BundlePath
        bundle_sha256 = $BundleSha256
        kernel_sha256 = $KernelSha256
        canonical_diagnostic_path = $capsulePath
        stable_handoff_path = $StableFailureHandoff
        created_at_utc = [DateTime]::UtcNow.ToString('o')
    }

    $json = ($capsule | ConvertTo-Json -Depth 12) + "`r`n"
    [IO.File]::WriteAllText($capsulePath,$json,[Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText($StableFailureHandoff,$json,[Text.UTF8Encoding]::new($false))

    Write-Host ''
    Write-Host 'PATCH FAIL'
    Write-Host ('FAILURE_FAMILY={0}' -f $failureFamily)
    Write-Host ('DIAGNOSTIC_FILE={0}' -f $StableFailureHandoff)
    Write-Host ('SOURCE_STATE={0}' -f $mutationAssessment)
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
    $Attempt.patch_id=[string]$manifest.patch_id
    $Attempt.expected_base_commit=[string]$manifest.expected_base_commit
    $Attempt.bundle_sha256=$bundleSha
    $Attempt.start_commit=(Get-WorkingSourceSnapshot -Path $WorkingSourcePath).head
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
        Write-FailureCapsule -Manifest $manifest -KernelSha256 $kernelSha -BundleSha256 $bundleSha -ChildResult $selfTest
        $combined=([string]$selfTest.Stdout)+"`r`n"+([string]$selfTest.Stderr)
        Complete-Attempt -Context $Attempt -Result 'FAIL' -FailedPhase 'SELFTEST' `
            -ReachedStage (Get-CapturedField -Text $combined -Name 'REACHED_STAGE') `
            -FailureFamily (Get-CapturedField -Text $combined -Name 'FAILURE_FAMILY') `
            -DiagnosticRef $StableFailureHandoff
        exit 1
    }

    Write-AttemptEvent -Context $Attempt -Event 'SELFTEST_PASS'
    Write-AttemptEvent -Context $Attempt -Event 'APPLY_STARTED'

    $apply = Invoke-ChildCaptured -PowerShellPath $powershellPath `
        -ArgumentList ($common + @('-Mode','Apply')) -Phase 'APPLY'
    if ($apply.ExitCode -ne 0) {
        Write-FailureCapsule -Manifest $manifest -KernelSha256 $kernelSha -BundleSha256 $bundleSha -ChildResult $apply
        $combined=([string]$apply.Stdout)+"`r`n"+([string]$apply.Stderr)
        Complete-Attempt -Context $Attempt -Result 'FAIL' -FailedPhase 'APPLY' `
            -ReachedStage (Get-CapturedField -Text $combined -Name 'REACHED_STAGE') `
            -FailureFamily (Get-CapturedField -Text $combined -Name 'FAILURE_FAMILY') `
            -DiagnosticRef $StableFailureHandoff
        exit 1
    }

    Write-AttemptEvent -Context $Attempt -Event 'APPLY_PASS'

    if (Test-Path -LiteralPath $StableFailureHandoff -PathType Leaf) {
        Remove-Item -LiteralPath $StableFailureHandoff -Force -ErrorAction SilentlyContinue
    }

    $receipt = Get-CapturedField -Text ([string]$apply.Stdout) -Name 'RECEIPT'
    Write-Host ''
    Write-Host 'PATCH SUCCESS'
    if (-not [string]::IsNullOrWhiteSpace($receipt)) {
        Write-Host ('RECEIPT={0}' -f $receipt)
    }
    Complete-Attempt -Context $Attempt -Result 'PASS' -ReceiptRef $receipt
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
    Write-FailureCapsule -Manifest $manifest -KernelSha256 $kernelShaForFailure `
        -BundleSha256 $bundleShaForFailure -ChildResult $transportResult
    Complete-Attempt -Context $Attempt -Result 'FAIL' -FailedPhase 'LAUNCHER' `
        -ReachedStage 'LAUNCHER' -FailureFamily 'LAUNCHER_FAILURE' `
        -DiagnosticRef $StableFailureHandoff
    exit 1
}
finally {
    if ($null -ne $temporaryRoot -and (Test-Path -LiteralPath $temporaryRoot)) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
