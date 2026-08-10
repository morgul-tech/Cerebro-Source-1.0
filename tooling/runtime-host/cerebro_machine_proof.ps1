Set-StrictMode -Version Latest

function Get-CerebroMachineProofSha256Text {
    param(
        [Parameter(Mandatory)]
        [string]$Text
    )

    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($Text)
        return (
            [BitConverter]::ToString(
                $sha.ComputeHash($bytes)
            )
        ).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
}

function Write-CerebroMachineProofState {
    param(
        [Parameter(Mandatory)]
        [string]$Path,

        [Parameter(Mandatory)]
        [object]$State
    )

    $directory = Split-Path -Parent $Path
    [IO.Directory]::CreateDirectory($directory) | Out-Null

    $temporary = Join-Path $directory (
        '.CEREBRO_MACHINE_PROOF_v1.tmp-' +
        [guid]::NewGuid().ToString('N') +
        '.json'
    )

    try {
        $json = $State | ConvertTo-Json -Depth 16
        [IO.File]::WriteAllText(
            $temporary,
            $json + "`n",
            [Text.UTF8Encoding]::new($false)
        )

        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            $replaceBackup = (
                $Path +
                '.replace-backup-' +
                [guid]::NewGuid().ToString('N')
            )

            try {
                [IO.File]::Replace(
                    $temporary,
                    $Path,
                    $replaceBackup,
                    $true
                )
            }
            finally {
                if (Test-Path -LiteralPath $replaceBackup -PathType Leaf) {
                    Remove-Item `
                        -LiteralPath $replaceBackup `
                        -Force `
                        -ErrorAction SilentlyContinue
                }
            }
        }
        else {
            [IO.File]::Move(
                $temporary,
                $Path
            )
        }
    }
    finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        }
    }
}

function Invoke-CerebroMachineProofCore {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$BootResult,

        [string]$WorkingSourcePath =
            'D:\Cerebro\Source\Cerebro_Source_v1.0',

        [string]$Remote = 'origin',

        [string]$Branch = 'main',

        [string]$RuntimeStatePath =
            'D:\Cerebro\Run\active\CEREBRO_RUNTIME_STATE_v1.json',

        [string]$ProofPath =
            'D:\Cerebro\Run\active\CEREBRO_MACHINE_PROOF_v1.json',

        [switch]$NoClipboard
    )

    $ErrorActionPreference = 'Stop'

    if ($BootResult.state -ne 'ACTIVE_CONTROL_TRANSFERRED') {
        throw 'MACHINE_PROOF_BOOT_STATE_INVALID'
    }

    if (-not [bool]$BootResult.operational) {
        throw 'MACHINE_PROOF_BOOT_NOT_OPERATIONAL'
    }

    if ([string]::IsNullOrWhiteSpace([string]$BootResult.receipt)) {
        throw 'MACHINE_PROOF_BOOT_RECEIPT_MISSING'
    }

    if (-not (Test-Path -LiteralPath $RuntimeStatePath -PathType Leaf)) {
        throw "MACHINE_PROOF_RUNTIME_STATE_MISSING:$RuntimeStatePath"
    }

    if (-not (Test-Path -LiteralPath $WorkingSourcePath -PathType Container)) {
        throw "MACHINE_PROOF_WORKING_SOURCE_MISSING:$WorkingSourcePath"
    }

    Push-Location -LiteralPath $WorkingSourcePath
    try {
        $branchActual = (git branch --show-current).Trim()
        if ($LASTEXITCODE -ne 0 -or $branchActual -ne $Branch) {
            throw "MACHINE_PROOF_BRANCH_MISMATCH expected=$Branch actual=$branchActual"
        }

        $status = @(git status --porcelain --untracked-files=all)
        if ($LASTEXITCODE -ne 0 -or $status.Count -ne 0) {
            throw 'MACHINE_PROOF_WORKTREE_NOT_CLEAN'
        }

        git fetch --no-tags $Remote $Branch
        if ($LASTEXITCODE -ne 0) {
            throw 'MACHINE_PROOF_REMOTE_FETCH_FAILED'
        }

        $localCommit = (git rev-parse HEAD).Trim()
        $remoteCommit = (git rev-parse "refs/remotes/$Remote/$Branch").Trim()

        if ($localCommit -ne $remoteCommit) {
            throw (
                'MACHINE_PROOF_SOURCE_EQUALITY_FAILED ' +
                "local=$localCommit remote=$remoteCommit"
            )
        }
    }
    finally {
        Pop-Location
    }

    $runtimeState = Get-Content -LiteralPath $RuntimeStatePath -Raw |
        ConvertFrom-Json

    if ($runtimeState.runtime.state -ne 'ACTIVE_CONTROL_TRANSFERRED') {
        throw 'MACHINE_PROOF_RUNTIME_STATE_INVALID'
    }

    if (-not [bool]$runtimeState.runtime.operational) {
        throw 'MACHINE_PROOF_RUNTIME_OPERATIONAL_FALSE'
    }

    if (-not [bool]$runtimeState.runtime.control.control_transferred) {
        throw 'MACHINE_PROOF_CONTROL_TRANSFER_FALSE'
    }

    $runtimeWorkingCommit = [string]$runtimeState.runtime.working_source.commit
    $runtimeAuthoritativeCommit = [string]$runtimeState.runtime.authoritative_source.commit
    $runtimeReceipt = [string]$runtimeState.runtime.receipt.value

    foreach ($observed in @(
        [string]$BootResult.source_commit,
        $runtimeWorkingCommit,
        $runtimeAuthoritativeCommit
    )) {
        if ($observed -ne $localCommit) {
            throw (
                'MACHINE_PROOF_COMMIT_BINDING_FAILED ' +
                "expected=$localCommit observed=$observed"
            )
        }
    }

    if ($runtimeReceipt -ne [string]$BootResult.receipt) {
        throw 'MACHINE_PROOF_RUNTIME_RECEIPT_MISMATCH'
    }

    $verifiedAt = [DateTime]::UtcNow.ToString('o')

    $proofMaterial = (
        '{0}|{1}|{2}|{3}|{4}|{5}|{6}' -f
        'CEREBRO_MACHINE_PROOF_V1',
        'morgul-tech/Cerebro-Source-1.0',
        $Branch,
        $localCommit,
        'ACTIVE_CONTROL_TRANSFERRED',
        $runtimeReceipt,
        $verifiedAt
    )

    $proofReceipt = Get-CerebroMachineProofSha256Text $proofMaterial

    $proof = [ordered]@{
        schema = 'cerebro-machine-proof/v1'
        created_at_utc = $verifiedAt
        source = [ordered]@{
            repository = 'morgul-tech/Cerebro-Source-1.0'
            branch = $Branch
            authoritative_commit = $remoteCommit
            working_source_commit = $localCommit
            equality = 'VERIFIED'
            worktree = 'CLEAN'
        }
        runtime = [ordered]@{
            state = 'ACTIVE_CONTROL_TRANSFERRED'
            operational = $true
            control_transferred = $true
            runtime_receipt = $runtimeReceipt
            runtime_state_path = $RuntimeStatePath
        }
        result = 'PASS'
        proof_receipt = $proofReceipt
    }

    Write-CerebroMachineProofState -Path $ProofPath -State $proof

    $proofText = @(
        'CEREBRO_MACHINE_PROOF_V1'
        'SOURCE_REPO=morgul-tech/Cerebro-Source-1.0'
        ("SOURCE_BRANCH={0}" -f $Branch)
        ("AUTHORITATIVE_COMMIT={0}" -f $remoteCommit)
        ("WORKING_SOURCE_COMMIT={0}" -f $localCommit)
        'SOURCE_EQUALITY=VERIFIED'
        'RUNTIME_STATE=ACTIVE_CONTROL_TRANSFERRED'
        'OPERATIONAL=TRUE'
        ("RUNTIME_RECEIPT={0}" -f $runtimeReceipt)
        ("PROOF_RECEIPT={0}" -f $proofReceipt)
        ("VERIFIED_AT_UTC={0}" -f $verifiedAt)
        'RESULT=PASS'
        'END_CEREBRO_MACHINE_PROOF'
    ) -join "`n"

    if (-not $NoClipboard) {
        Set-Clipboard -Value $proofText
    }

    Write-Host ''
    Write-Host '======================================================'
    Write-Host 'CEREBRO MACHINE PROOF'
    Write-Host '======================================================'
    Write-Host 'source_equality:       VERIFIED'
    Write-Host 'runtime_state:         ACTIVE_CONTROL_TRANSFERRED'
    Write-Host 'operational:           TRUE'
    Write-Host ("source_commit:         {0}" -f $localCommit)
    Write-Host ("proof_receipt:         {0}" -f $proofReceipt)
    Write-Host ("proof_path:            {0}" -f $ProofPath)
    if (-not $NoClipboard) {
        Write-Host 'clipboard:             PROOF_COPIED'
    }
    else {
        Write-Host 'clipboard:             SKIPPED'
    }
    Write-Host '======================================================'

    return [pscustomobject]@{
        state = 'PASS'
        source_equality = 'VERIFIED'
        runtime_state = 'ACTIVE_CONTROL_TRANSFERRED'
        operational = $true
        source_commit = $localCommit
        runtime_receipt = $runtimeReceipt
        proof_receipt = $proofReceipt
        proof_path = $ProofPath
        clipboard = $(if ($NoClipboard) { 'SKIPPED' } else { 'PROOF_COPIED' })
        proof_text = $proofText
    }
}
