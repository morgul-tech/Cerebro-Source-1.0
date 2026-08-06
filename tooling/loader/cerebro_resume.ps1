Set-StrictMode -Version Latest

function Get-CerebroResumeFileSha256 {
    param([Parameter(Mandatory)][string]$Path)

    $sha = [Security.Cryptography.SHA256]::Create()

    try {
        $bytes = [IO.File]::ReadAllBytes($Path)

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

function Invoke-CerebroResumeCore {
    [CmdletBinding()]
    param(
        [string]$HandoffPath =
            'D:\Cerebro\Run\handoff\CEREBRO_SESSION_HANDOFF_v1.json',
        [string]$RepoPath = 'D:\Cerebro\Source\Cerebro_Source_v1.0'
    )

    $ErrorActionPreference = 'Stop'

    if (-not (Test-Path -LiteralPath $HandoffPath -PathType Leaf)) {
        throw "RESUME_HANDOFF_NOT_FOUND:$HandoffPath"
    }

    if (-not (Test-Path -LiteralPath $RepoPath -PathType Container)) {
        throw "RESUME_REPOSITORY_NOT_FOUND:$RepoPath"
    }

    $artifact = Get-Content -LiteralPath $HandoffPath -Raw |
        ConvertFrom-Json

    if ($artifact.schema -ne 'cerebro-session-handoff/v0.1') {
        throw "RESUME_SCHEMA_UNSUPPORTED:$($artifact.schema)"
    }

    foreach ($field in @(
        'id',
        'authority',
        'integrity',
        'execution',
        'context',
        'repository_state',
        'continuation'
    )) {
        if (-not ($artifact.handoff.PSObject.Properties.Name -contains $field)) {
            throw "RESUME_HANDOFF_FIELD_MISSING:$field"
        }
    }

    Push-Location -LiteralPath $RepoPath

    try {
        $branch = (git branch --show-current).Trim()
        $head = (git rev-parse HEAD).Trim()
        $status = @(git status --porcelain --untracked-files=all)

        if ($status.Count -ne 0) {
            throw "RESUME_WORKTREE_NOT_CLEAN:$($status -join '|')"
        }

        if ($branch -ne [string]$artifact.handoff.authority.branch) {
            throw (
                "RESUME_BRANCH_MISMATCH:EXPECTED={0}:ACTUAL={1}" -f
                $artifact.handoff.authority.branch,
                $branch
            )
        }

        if ($head -ne [string]$artifact.handoff.authority.source_commit) {
            throw (
                "RESUME_SOURCE_DIVERGENCE:EXPECTED={0}:ACTUAL={1}" -f
                $artifact.handoff.authority.source_commit,
                $head
            )
        }

        git rev-parse --abbrev-ref '@{upstream}' 2>$null | Out-Null

        if ($LASTEXITCODE -ne 0) {
            throw 'RESUME_UPSTREAM_NOT_CONFIGURED'
        }

        $upstream = (git rev-parse '@{upstream}').Trim()

        if ($head -ne $upstream) {
            throw (
                "RESUME_LOCAL_NOT_EQUAL_UPSTREAM:LOCAL={0}:UPSTREAM={1}" -f
                $head,
                $upstream
            )
        }

        $roadmapPath = Join-Path `
            $RepoPath `
            ([string]$artifact.handoff.integrity.roadmap_path -replace '/', '\')

        $contextPath = Join-Path `
            $RepoPath `
            (
                [string]$artifact.handoff.integrity.working_context_path `
                    -replace '/', '\'
            )

        $roadmapHash = Get-CerebroResumeFileSha256 $roadmapPath
        $contextHash = Get-CerebroResumeFileSha256 $contextPath

        if (
            $roadmapHash -ne
            [string]$artifact.handoff.integrity.roadmap_sha256
        ) {
            throw 'RESUME_ROADMAP_HASH_MISMATCH'
        }

        if (
            $contextHash -ne
            [string]$artifact.handoff.integrity.working_context_sha256
        ) {
            throw 'RESUME_WORKING_CONTEXT_HASH_MISMATCH'
        }

        $roadmap = [IO.File]::ReadAllText($roadmapPath)

        foreach ($token in @(
            [string]$artifact.handoff.execution.current.phase_ref,
            [string]$artifact.handoff.execution.current.patch_ref,
            [string]$artifact.handoff.execution.current.canonical_command,
            [string]$artifact.handoff.execution.next.patch_ref
        )) {
            if (-not $roadmap.Contains($token)) {
                throw "RESUME_EXECUTION_TOKEN_MISSING:$token"
            }
        }

        $receiptMaterial = (
            '{0}|{1}|{2}|{3}' -f
            $artifact.handoff.id,
            $head,
            $artifact.handoff.execution.current.patch_ref,
            $roadmapHash
        )

        $sha = [Security.Cryptography.SHA256]::Create()

        try {
            $receipt = (
                [BitConverter]::ToString(
                    $sha.ComputeHash(
                        [Text.Encoding]::UTF8.GetBytes($receiptMaterial)
                    )
                )
            ).Replace('-', '').ToLowerInvariant()
        }
        finally {
            $sha.Dispose()
        }

        Write-Host (
            (
                'CEREBRO_RESUME STATE=SUCCESS_READY HANDOFF_ID={0} ' +
                'SOURCE_COMMIT={1} ROADMAP=VERIFIED CONTEXT=VERIFIED ' +
                'CURRENT_PATCH={2} NEXT_PATCH={3} RECEIPT={4}'
            ) -f
            $artifact.handoff.id,
            $head,
            $artifact.handoff.execution.current.patch_ref,
            $artifact.handoff.execution.next.patch_ref,
            $receipt
        )

        return [pscustomobject]@{
            handoff_id = $artifact.handoff.id
            source_commit = $head
            current_patch =
                $artifact.handoff.execution.current.patch_ref
            next_patch =
                $artifact.handoff.execution.next.patch_ref
            canonical_command =
                $artifact.handoff.execution.current.canonical_command
            receipt = $receipt
            state = 'SUCCESS_READY'
        }
    }
    finally {
        Pop-Location
    }
}
