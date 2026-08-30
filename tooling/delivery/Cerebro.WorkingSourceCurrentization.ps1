[CmdletBinding()]
param(
    [string]$RepoPath = 'D:\Cerebro\Source\Cerebro_Source_v1.0',
    [string]$Remote = 'origin',
    [string]$Branch = 'main',
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$FrozenRemoteOid,
    [string]$ExpectedRemote = 'https://github.com/morgul-tech/Cerebro-Source-1.0.git',
    [string]$Generation = 'UNBOUND',
    [string]$Claim = 'UNBOUND',
    [string]$ReceiptPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$counters = [ordered]@{
    fetch = 0; head_move = 0; ff_merge = 0; pull = 0; reset = 0
    checkout = 0; switch = 0; rebase = 0; non_ff_merge = 0; commit = 0
    add = 0; restore = 0; stash = 0; clean = 0; update_ref = 0; force = 0
    push = 0; source_write = 0; runtime_write = 0; cerebro_sync_inbound = 0
    attempt_epoch = 0; host_dispatch = 0; physical_effect = 0; human_git_courier = 0
}
$state = [ordered]@{
    schema = 'cerebro-working-source-currentization-receipt/v1'
    generation = $Generation
    claim = $Claim
    repository = [IO.Path]::GetFullPath($RepoPath)
    remote = $Remote
    branch = $Branch
    expected_remote = $ExpectedRemote
    actual_remote = $null
    before_oid = $null
    frozen_remote_oid = $FrozenRemoteOid.ToLowerInvariant()
    observed_remote_oid = $null
    after_oid = $null
    topology = 'UNOBSERVED'
    verdict = 'HOLD_PREQUALIFICATION'
    actions = @()
    clean_before = $false
    clean_after = $false
    counters = $counters
    timestamp_utc = [DateTime]::UtcNow.ToString('o')
}

function Invoke-Git {
    param([Parameter(Mandatory = $true)][string[]]$Arguments, [switch]$AllowFailure)
    $priorErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = & git.exe -C $RepoPath @Arguments 2>&1
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $priorErrorAction
    }
    $text = (($output | ForEach-Object { [string]$_ }) -join "`n").Trim()
    if (($exitCode -ne 0) -and (-not $AllowFailure)) {
        throw ('GIT_FAILED:' + ($Arguments -join '_') + ':' + $text)
    }
    return [pscustomobject]@{ ExitCode = $exitCode; Text = $text }
}

function Normalize-Remote([string]$Value) {
    $v = $Value.Trim().TrimEnd('/')
    if ($v.EndsWith('.git')) { $v = $v.Substring(0, $v.Length - 4) }
    if ($v -match '^git@github\.com:(.+)$') { $v = 'https://github.com/' + $Matches[1] }
    return $v.ToLowerInvariant()
}

function Write-Receipt {
    if ([string]::IsNullOrWhiteSpace($ReceiptPath)) {
        $root = Join-Path $env:LOCALAPPDATA 'Cerebro\operations\working-source-currentization'
        $script:ReceiptPath = Join-Path $root (('currentization-{0}-{1}.json' -f ([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffffffZ')), ([Guid]::NewGuid().ToString('N'))))
    }
    $parent = Split-Path -Parent $ReceiptPath
    if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
    $payload = [ordered]@{}
    foreach ($key in $state.Keys) { $payload[$key] = $state[$key] }
    $canonical = $payload | ConvertTo-Json -Depth 8 -Compress
    $bytes = [Text.Encoding]::UTF8.GetBytes($canonical)
    $sha = ([Security.Cryptography.SHA256]::Create().ComputeHash($bytes) | ForEach-Object { $_.ToString('x2') }) -join ''
    $document = [ordered]@{ payload = $payload; payload_sha256 = $sha } | ConvertTo-Json -Depth 10
    $stream = [IO.File]::Open($ReceiptPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
    try {
        $writer = New-Object IO.StreamWriter($stream, (New-Object Text.UTF8Encoding($false)))
        try { $writer.Write($document) } finally { $writer.Dispose() }
    } finally { $stream.Dispose() }
    Write-Output $document
}

$exitCode = 2
try {
    if (-not (Test-Path -LiteralPath (Join-Path $RepoPath '.git'))) { throw 'HOLD_NOT_GIT_WORKTREE' }
    $inside = (Invoke-Git @('rev-parse', '--is-inside-work-tree')).Text
    if ($inside -ne 'true') { throw 'HOLD_NOT_GIT_WORKTREE' }

    $branchResult = Invoke-Git @('symbolic-ref', '--quiet', '--short', 'HEAD') -AllowFailure
    if ($branchResult.ExitCode -ne 0) { $state.topology = 'DETACHED'; $state.verdict = 'HOLD_DETACHED'; throw 'HOLD_DETACHED' }
    if ($branchResult.Text -ne $Branch) { $state.topology = 'WRONG_BRANCH'; $state.verdict = 'HOLD_WRONG_BRANCH'; throw 'HOLD_WRONG_BRANCH' }

    $state.actual_remote = (Invoke-Git @('remote', 'get-url', $Remote)).Text
    if ((Normalize-Remote $state.actual_remote) -ne (Normalize-Remote $ExpectedRemote)) {
        $state.topology = 'WRONG_REMOTE'; $state.verdict = 'HOLD_WRONG_REMOTE'; throw 'HOLD_WRONG_REMOTE'
    }

    $dirty = (Invoke-Git @('status', '--porcelain=v1', '--untracked-files=all')).Text
    if ($dirty.Length -ne 0) { $state.topology = 'DIRTY'; $state.verdict = 'HOLD_DIRTY'; throw 'HOLD_DIRTY' }
    $state.clean_before = $true
    $state.before_oid = (Invoke-Git @('rev-parse', 'HEAD')).Text.ToLowerInvariant()

    Invoke-Git @('fetch', '--no-tags', $Remote, $Branch) | Out-Null
    $counters.fetch = 1
    $state.actions += 'git fetch --no-tags <remote> <branch>'
    $remoteRef = 'refs/remotes/' + $Remote + '/' + $Branch
    $state.observed_remote_oid = (Invoke-Git @('rev-parse', $remoteRef)).Text.ToLowerInvariant()
    if ($state.observed_remote_oid -ne $state.frozen_remote_oid) {
        $state.topology = 'FROZEN_REMOTE_MOVED'; $state.verdict = 'HOLD_REQUALIFY'; throw 'HOLD_REQUALIFY'
    }

    if ($state.before_oid -eq $state.observed_remote_oid) {
        $state.topology = 'EQUAL_CLEAN'; $state.verdict = 'PASS_NOOP'
    } else {
        $behind = Invoke-Git @('merge-base', '--is-ancestor', $state.before_oid, $state.observed_remote_oid) -AllowFailure
        if ($behind.ExitCode -eq 0) {
            $state.topology = 'LOCAL_BEHIND'
            Invoke-Git @('merge', '--ff-only', $remoteRef) | Out-Null
            $counters.ff_merge = 1; $counters.head_move = 1
            $state.actions += 'git merge --ff-only <remote-ref>'
            $state.verdict = 'PASS_FF_ONLY'
        } else {
            $ahead = Invoke-Git @('merge-base', '--is-ancestor', $state.observed_remote_oid, $state.before_oid) -AllowFailure
            if ($ahead.ExitCode -eq 0) { $state.topology = 'LOCAL_AHEAD'; $state.verdict = 'HOLD_LOCAL_AHEAD' }
            else { $state.topology = 'DIVERGED'; $state.verdict = 'HOLD_DIVERGED_HISTORY' }
            throw $state.verdict
        }
    }

    $state.after_oid = (Invoke-Git @('rev-parse', 'HEAD')).Text.ToLowerInvariant()
    if ($state.after_oid -ne $state.observed_remote_oid) { $state.verdict = 'HOLD_POSTSTATE_MISMATCH'; throw 'HOLD_POSTSTATE_MISMATCH' }
    $afterDirty = (Invoke-Git @('status', '--porcelain=v1', '--untracked-files=all')).Text
    if ($afterDirty.Length -ne 0) { $state.verdict = 'HOLD_POSTSTATE_DIRTY'; throw 'HOLD_POSTSTATE_DIRTY' }
    $state.clean_after = $true
    $exitCode = 0
} catch {
    if ($state.verdict -like 'PASS_*') { $state.verdict = 'HOLD_INTERNAL_ERROR' }
    $state['error'] = $_.Exception.Message
    if ($state.before_oid) {
        $after = Invoke-Git @('rev-parse', 'HEAD') -AllowFailure
        if ($after.ExitCode -eq 0) { $state.after_oid = $after.Text.ToLowerInvariant() }
    }
} finally {
    Write-Receipt
}
if ($exitCode -ne 0) {
    throw ('CEREBRO_CURRENTIZATION_HOLD:' + $state.verdict + ':' + $ReceiptPath)
}
