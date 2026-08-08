[CmdletBinding()]
param(
    [string]$SourcePath = 'D:\Cerebro\Source\Cerebro_Source_v1.0',
    [string]$RunRoot = 'D:\Cerebro\Run',
    [string]$Remote = 'origin',
    [string]$Branch = 'main',
    [string]$EventPath,
    [switch]$Start,
    [switch]$NoPause
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$finalState = 'FAILED_CLOSED'
$verificationState = 'FAILED'
$resultObject = $null
$release = $null
$resolvedEventPath = $null
$eventId = 'UNRESOLVED'
$receiptPath = $null
$statePath = $null

try {
    if ($Start -and -not [string]::IsNullOrWhiteSpace($EventPath)) {
        throw 'RUNTIME_HOST_ACCEPTS_ONE_EVENT_SOURCE_ONLY'
    }

    if (-not $Start -and [string]::IsNullOrWhiteSpace($EventPath)) {
        $Start = $true
    }

    $source = [IO.Path]::GetFullPath($SourcePath)
    $run = [IO.Path]::GetFullPath($RunRoot)

    $builderPath = Join-Path $source 'tooling\builder\cerebro_runtime_release.ps1'
    if (-not (Test-Path -LiteralPath $builderPath -PathType Leaf)) {
        throw 'RUNTIME_RELEASE_BUILDER_MISSING'
    }

    . $builderPath

    $release = New-CerebroRuntimeRelease0_1 `
        -SourcePath $source `
        -RunRoot $run `
        -Remote $Remote `
        -Branch $Branch

    if ($release.state -ne 'PINNED_RELEASE_VERIFIED') {
        throw "RUNTIME_RELEASE_NOT_VERIFIED:$($release.state)"
    }

    $profile = Get-Content -LiteralPath $release.profile_path -Raw | ConvertFrom-Json
    $entrypoint = Join-Path `
        $release.release_path `
        ([string]$profile.entrypoint -replace '/', '\')

    if (-not (Test-Path -LiteralPath $entrypoint -PathType Leaf)) {
        throw "RUNTIME_PROFILE_ENTRYPOINT_UNRESOLVED:$entrypoint"
    }

    . $entrypoint

    [IO.Directory]::CreateDirectory((Join-Path $run 'events')) | Out-Null
    [IO.Directory]::CreateDirectory((Join-Path $run 'active')) | Out-Null
    [IO.Directory]::CreateDirectory((Join-Path $run 'receipts')) | Out-Null
    [IO.Directory]::CreateDirectory((Join-Path $run 'ledger')) | Out-Null

    if ($Start) {
        $eventId = (
            'RUNTIME-START-' +
            [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ') + '-' +
            [guid]::NewGuid().ToString('N').Substring(0,8)
        )

        $resolvedEventPath = Join-Path `
            (Join-Path $run 'events') `
            ($eventId + '.json')

        $event = [ordered]@{
            event_id = $eventId
            event_type = 'RUNTIME_START'
            issued_at = [DateTime]::UtcNow.ToString('o')
            source = 'PATCH-003'
            authority = 'USER'
            payload = [ordered]@{
                action = 'START_RUNTIME_0_1'
                source_commit = $release.source_commit
                release_sha256 = $release.release_sha256
            }
            correlation_ref = 'PATCH-003'
        }

        $json = ($event | ConvertTo-Json -Depth 16) + "`n"
        [IO.File]::WriteAllText(
            $resolvedEventPath,
            $json,
            [Text.UTF8Encoding]::new($false)
        )
    }
    else {
        $resolvedEventPath = [IO.Path]::GetFullPath($EventPath)
        if (-not (Test-Path -LiteralPath $resolvedEventPath -PathType Leaf)) {
            throw "RUNTIME_EVENT_FILE_MISSING:$resolvedEventPath"
        }

        $event = Get-Content -LiteralPath $resolvedEventPath -Raw | ConvertFrom-Json
        if (-not ($event.PSObject.Properties.Name -contains 'event_id')) {
            throw 'RUNTIME_EVENT_ID_MISSING'
        }
        $eventId = [string]$event.event_id
    }

    $safeEventId = $eventId -replace '[^A-Za-z0-9_.-]', '_'
    $statePath = Join-Path $run 'active\CEREBRO_RUNTIME_0_1_STATE.json'
    $receiptPath = Join-Path `
        (Join-Path $run 'receipts') `
        ('CEREBRO_RUNTIME_0_1_' + $safeEventId + '.json')
    $failureLedgerPath = Join-Path $run 'ledger\CEREBRO_RUNTIME_0_1_FAILURE.json'

    $resultObject = Invoke-CerebroRuntimeCore `
        -ReleasePath $release.release_path `
        -PinnedReleaseSha256 $release.release_sha256 `
        -RuntimeProfilePath $release.profile_path `
        -EventPath $resolvedEventPath `
        -StatePath $statePath `
        -ReceiptPath $receiptPath `
        -FailureLedgerPath $failureLedgerPath

    $finalState = [string]$resultObject.state
    $verificationState = [string]$resultObject.verification_state

    if ($Start) {
        if ($finalState -ne 'COMPLETED') {
            throw "RUNTIME_START_NOT_COMPLETED:$finalState"
        }
        if ($verificationState -ne 'PASSED') {
            throw "RUNTIME_START_VERIFICATION_NOT_PASSED:$verificationState"
        }
    }

    Write-Host ''
    Write-Host '======================================================'
    Write-Host 'CEREBRO RUNTIME 0.1 RESULT'
    Write-Host '======================================================'
    Write-Host ("source_commit:       {0}" -f $release.source_commit)
    Write-Host ("release_sha256:      {0}" -f $release.release_sha256)
    Write-Host ("event_id:            {0}" -f $eventId)
    Write-Host ("final_state:         {0}" -f $finalState)
    Write-Host ("verification_state:  {0}" -f $verificationState)
    Write-Host ("state_path:          {0}" -f $statePath)
    Write-Host ("receipt_path:        {0}" -f $receiptPath)
    Write-Host '======================================================'
    Write-Host (
        "CEREBRO_RUNTIME VERSION=0.1 STATE={0} VERIFY={1} SOURCE_COMMIT={2} RELEASE_SHA256={3} EVENT={4} RECEIPT={5}" -f
        $finalState,
        $verificationState,
        $release.source_commit,
        $release.release_sha256,
        $eventId,
        $receiptPath
    )

    [pscustomobject]@{
        runtime_version = '0.1'
        final_state = $finalState
        verification_state = $verificationState
        source_commit = $release.source_commit
        release_sha256 = $release.release_sha256
        release_path = $release.release_path
        profile_path = $release.profile_path
        event_id = $eventId
        event_path = $resolvedEventPath
        state_path = $statePath
        receipt_path = $receiptPath
    }
}
catch {
    Write-Host ''
    Write-Host '======================================================'
    Write-Host 'CEREBRO RUNTIME 0.1 FAILURE'
    Write-Host '======================================================'
    Write-Host ("final_state:         FAILED_CLOSED")
    Write-Host ("failure:             {0}" -f $_.Exception.Message)
    if ($release) {
        Write-Host ("source_commit:       {0}" -f $release.source_commit)
        Write-Host ("release_sha256:      {0}" -f $release.release_sha256)
    }
    if ($receiptPath) {
        Write-Host ("receipt_path:        {0}" -f $receiptPath)
    }
    Write-Host '======================================================'
    throw
}
finally {
    if (
        -not $NoPause -and
        $env:CEREBRO_CPATCH_HOST -ne '1' -and
        $Host.Name -eq 'ConsoleHost'
    ) {
        [void](Read-Host 'Press Enter to close')
    }
}
