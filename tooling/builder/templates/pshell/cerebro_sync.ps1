[CmdletBinding()]
param(
    [string]$RepoPath = 'D:\Cerebro\Source\Cerebro_Source_v1.0',
    [string]$Remote = 'origin',
    [string]$Branch = 'main',
    [string]$CommitMessage = 'Sync local Working Source to authoritative Source',
    [string[]]$Paths = @(),
    [switch]$AllowRemainingChanges
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$repo = [IO.Path]::GetFullPath($RepoPath)
$git = (Get-Command git -ErrorAction Stop | Select-Object -First 1).Source

function Invoke-CerebroGit {
    param(
        [Parameter(Mandatory)]
        [string[]]$Arguments,

        [int[]]$AllowedExitCodes = @(0)
    )

    $stdoutFile = [IO.Path]::GetTempFileName()
    $stderrFile = [IO.Path]::GetTempFileName()
    $previousPreference = $ErrorActionPreference

    try {
        $ErrorActionPreference = 'Continue'
        & $git @Arguments 1> $stdoutFile 2> $stderrFile
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }

    try {
        $stdout = [IO.File]::ReadAllText($stdoutFile).TrimEnd([char[]]"`r`n")
        $stderr = [IO.File]::ReadAllText($stderrFile).TrimEnd([char[]]"`r`n")
    }
    finally {
        Remove-Item -LiteralPath $stdoutFile, $stderrFile -Force -ErrorAction SilentlyContinue
    }

    $result = [pscustomobject]@{
        arguments = @($Arguments)
        exit_code = $exitCode
        stdout = $stdout
        stderr = $stderr
    }

    if ($AllowedExitCodes -notcontains $exitCode) {
        throw (@(
            'GIT_EXIT_NOT_ALLOWED'
            "exit_code=$exitCode"
            "arguments=$($Arguments -join ' ')"
            "stdout=$stdout"
            "stderr=$stderr"
        ) -join "`n")
    }

    return $result
}

function Get-CerebroShortReceipt {
    param([Parameter(Mandatory)][string]$Text)

    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($Text)
        return ([BitConverter]::ToString(
            $sha.ComputeHash($bytes)
        )).Replace('-', '').ToLowerInvariant().Substring(0, 12)
    }
    finally {
        $sha.Dispose()
    }
}

$baselineCommit = 'UNKNOWN'
$baselineStatus = 'NOT_RUN'
$fileChangeStatus = 'NOT_ASSESSED'
$gitAddStatus = 'NOT_RUN'
$commitStatus = 'NOT_RUN'
$pushStatus = 'NOT_RUN'
$remoteVerification = 'NOT_RUN'
$localCommit = 'UNKNOWN'
$remoteCommit = 'UNKNOWN'
$worktreeStatus = 'UNKNOWN'
$finalState = 'FAILED_NO_MUTATION'
$userActionRequired = 'ATTENTION_REQUIRED'
$nextCommand = 'git status --short'
$errorMessage = $null
$commitCreated = $false
$pushCompleted = $false
$stagingStarted = $false
$locationPushed = $false

try {
    if (-not (Test-Path -LiteralPath $repo -PathType Container)) {
        throw "REPOSITORY_PATH_NOT_FOUND:$repo"
    }

    Push-Location $repo
    $locationPushed = $true

    $actualRoot = [IO.Path]::GetFullPath(
        (Invoke-CerebroGit @('rev-parse', '--show-toplevel')).stdout
    )

    if (-not $actualRoot.Equals(
        $repo,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "REPOSITORY_BINDING_MISMATCH expected=$repo actual=$actualRoot"
    }

    $actualBranch = (Invoke-CerebroGit @(
        'branch',
        '--show-current'
    )).stdout

    if ($actualBranch -ne $Branch) {
        throw "BRANCH_MISMATCH expected=$Branch actual=$actualBranch"
    }

    [void](Invoke-CerebroGit @(
        'remote',
        'get-url',
        $Remote
    ))

    $stagedBefore = Invoke-CerebroGit `
        -Arguments @('diff', '--cached', '--quiet') `
        -AllowedExitCodes @(0, 1)

    if ($stagedBefore.exit_code -eq 1) {
        throw 'PREEXISTING_STAGED_CHANGES'
    }

    [void](Invoke-CerebroGit @(
        'fetch',
        '--no-tags',
        $Remote,
        $Branch
    ))

    $localBefore = (Invoke-CerebroGit @(
        'rev-parse',
        'HEAD'
    )).stdout

    $remoteBefore = (Invoke-CerebroGit @(
        'rev-parse',
        "refs/remotes/$Remote/$Branch"
    )).stdout

    $baselineCommit = $remoteBefore

    if ($localBefore -eq $remoteBefore) {
        $baselineStatus = 'MATCH'
    }
    else {
        $ancestor = Invoke-CerebroGit `
            -Arguments @(
                'merge-base',
                '--is-ancestor',
                $remoteBefore,
                $localBefore
            ) `
            -AllowedExitCodes @(0, 1)

        if ($ancestor.exit_code -ne 0) {
            throw "BASELINE_DIVERGED local=$localBefore remote=$remoteBefore"
        }

        $baselineStatus = 'LOCAL_AHEAD'
    }

    foreach ($pathItem in @($Paths)) {
        if (
            [string]::IsNullOrWhiteSpace($pathItem) -or
            [IO.Path]::IsPathRooted($pathItem) -or
            $pathItem -match '(^|[\\/])\.\.([\\/]|$)'
        ) {
            throw "UNSAFE_RELATIVE_PATH:$pathItem"
        }
    }

    $statusBefore = (Invoke-CerebroGit @(
        'status',
        '--porcelain'
    )).stdout

    if ([string]::IsNullOrWhiteSpace($statusBefore)) {
        $fileChangeStatus = 'NO_WORKTREE_CHANGES'
    }
    else {
        $fileChangeStatus = 'WORKTREE_CHANGES_DETECTED'
        $stagingStarted = $true

        if (@($Paths).Count -gt 0) {
            [void](Invoke-CerebroGit (
                @('add', '-A', '--') + @($Paths)
            ))
        }
        else {
            [void](Invoke-CerebroGit @(
                'add',
                '-A'
            ))
        }

        $gitAddStatus = 'SUCCESS'
    }

    $stagedNow = Invoke-CerebroGit `
        -Arguments @('diff', '--cached', '--quiet') `
        -AllowedExitCodes @(0, 1)

    if ($stagedNow.exit_code -eq 1) {
        [void](Invoke-CerebroGit @(
            'commit',
            '-m',
            $CommitMessage
        ))

        $commitCreated = $true
        $commitStatus = 'SUCCESS_CREATED'
    }
    else {
        $commitStatus = 'NO_NEW_COMMIT'
    }

    $localCommit = (Invoke-CerebroGit @(
        'rev-parse',
        'HEAD'
    )).stdout

    if ($localCommit -eq $remoteBefore) {
        $pushStatus = 'NOT_REQUIRED'
    }
    else {
        $lease = "--force-with-lease=refs/heads/${Branch}:$remoteBefore"

        [void](Invoke-CerebroGit @(
            'push',
            $lease,
            $Remote,
            "HEAD:refs/heads/$Branch"
        ))

        $pushCompleted = $true
        $pushStatus = 'SUCCESS_FORCE_WITH_LEASE'
    }

    [void](Invoke-CerebroGit @(
        'fetch',
        '--no-tags',
        $Remote,
        $Branch
    ))

    $remoteCommit = (Invoke-CerebroGit @(
        'rev-parse',
        "refs/remotes/$Remote/$Branch"
    )).stdout

    $localCommit = (Invoke-CerebroGit @(
        'rev-parse',
        'HEAD'
    )).stdout

    if ($remoteCommit -ne $localCommit) {
        throw "REMOTE_VERIFICATION_MISMATCH local=$localCommit remote=$remoteCommit"
    }

    $remoteVerification = 'SUCCESS'

    $statusAfter = (Invoke-CerebroGit @(
        'status',
        '--porcelain'
    )).stdout

    if ([string]::IsNullOrWhiteSpace($statusAfter)) {
        $worktreeStatus = 'CLEAN'
        $userActionRequired = 'NONE'
        $nextCommand = 'NONE'
    }
    else {
        $worktreeStatus = 'DIRTY'

        if (-not $AllowRemainingChanges) {
            throw "REMAINING_WORKTREE_CHANGES:$statusAfter"
        }

        $userActionRequired = 'REVIEW_REMAINING_CHANGES'
        $nextCommand = 'git status --short'
    }

    if ($pushStatus -eq 'NOT_REQUIRED') {
        $finalState = 'ALREADY_APPLIED'
    }
    else {
        $finalState = 'SUCCESS_PUSHED'
    }
}
catch {
    $errorMessage = $_.Exception.Message

    if ($stagingStarted -and -not $commitCreated) {
        try {
            [void](Invoke-CerebroGit @(
                'reset',
                '--mixed',
                'HEAD'
            ))
        }
        catch {
        }
    }

    try {
        $localCommit = (Invoke-CerebroGit @(
            'rev-parse',
            'HEAD'
        )).stdout
    }
    catch {
    }

    try {
        $remoteCommit = (Invoke-CerebroGit @(
            'rev-parse',
            "refs/remotes/$Remote/$Branch"
        )).stdout
    }
    catch {
    }

    try {
        $failureStatus = (Invoke-CerebroGit @(
            'status',
            '--porcelain'
        )).stdout

        if ([string]::IsNullOrWhiteSpace($failureStatus)) {
            $worktreeStatus = 'CLEAN'
        }
        else {
            $worktreeStatus = 'DIRTY'
        }
    }
    catch {
        $worktreeStatus = 'UNKNOWN'
    }

    if ($commitCreated -and -not $pushCompleted) {
        $finalState = 'SUCCESS_LOCAL_COMMIT'
        $userActionRequired = 'PUSH_RETRY_REQUIRED'
        $nextCommand = "& 'D:\Cerebro\Run\Operations\Publication\LegacyRootScripts\cerebro_sync.ps1'"
    }
    elseif ($pushCompleted) {
        $finalState = 'FAILED_PARTIAL_REQUIRES_ATTENTION'
        $nextCommand = 'git status'
    }
    else {
        $finalState = 'FAILED_NO_MUTATION'
    }
}
finally {
    if ($locationPushed) {
        Pop-Location -ErrorAction SilentlyContinue
    }
}

$receipt = Get-CerebroShortReceipt (
    @(
        'CEREBRO_SOURCE_SYNC'
        $Branch
        $localCommit
        $remoteCommit
        $worktreeStatus
        $finalState
    ) -join '|'
)

Write-Host ''
Write-Host '======================================================'
Write-Host 'CEREBRO SOURCE SYNC RESULT'
Write-Host '======================================================'
Write-Host ("repo_path:             {0}" -f $repo)
Write-Host ("remote:                {0}" -f $Remote)
Write-Host ("branch:                {0}" -f $Branch)
Write-Host ("baseline_commit:       {0}" -f $baselineCommit)
Write-Host ("baseline_status:       {0}" -f $baselineStatus)
Write-Host ("file_change_status:    {0}" -f $fileChangeStatus)
Write-Host ("git_add_status:        {0}" -f $gitAddStatus)
Write-Host ("commit_status:         {0}" -f $commitStatus)
Write-Host ("push_status:           {0}" -f $pushStatus)
Write-Host ("remote_verification:   {0}" -f $remoteVerification)
Write-Host ("local_commit:          {0}" -f $localCommit)
Write-Host ("remote_commit:         {0}" -f $remoteCommit)
Write-Host ("worktree_status:       {0}" -f $worktreeStatus)
Write-Host ("final_state:           {0}" -f $finalState)
Write-Host ("user_action_required:  {0}" -f $userActionRequired)
Write-Host ("next_command:          {0}" -f $nextCommand)

if ($errorMessage) {
    Write-Host ("error:                 {0}" -f $errorMessage)
}

Write-Host '======================================================'
Write-Host (
    "CEREBRO_SOURCE_SYNC MODE=PSHELL STATE={0} BRANCH={1} COMMIT={2} WORKTREE={3} RECEIPT={4}" -f
    $finalState,
    $Branch,
    $localCommit,
    $worktreeStatus,
    $receipt
)

if ($finalState -notin @(
    'SUCCESS_PUSHED',
    'ALREADY_APPLIED',
    'SUCCESS_LOCAL_COMMIT'
)) {
    throw "CEREBRO_SOURCE_SYNC_FAILED:$finalState"
}
