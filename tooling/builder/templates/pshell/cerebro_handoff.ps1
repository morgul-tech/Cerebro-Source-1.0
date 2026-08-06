Set-StrictMode -Version Latest

function Get-CerebroFileSha256 {
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

function Get-CerebroYamlScalar {
    param(
        [Parameter(Mandatory)][string]$Block,
        [Parameter(Mandatory)][string]$Name
    )

    $pattern = '(?m)^\s*' + [regex]::Escape($Name) +
        ':\s*(?:"([^"]*)"|([^\r\n#]+))\s*$'

    $match = [regex]::Match($Block, $pattern)

    if (-not $match.Success) {
        throw "HANDOFF_FIELD_NOT_FOUND:$Name"
    }

    if ($match.Groups[1].Success) {
        return $match.Groups[1].Value
    }

    return $match.Groups[2].Value.Trim()
}

function Get-CerebroExecutionBlock {
    param(
        [Parameter(Mandatory)][string]$Roadmap,
        [Parameter(Mandatory)][ValidateSet('current', 'next')]
        [string]$Name
    )

    $pattern = (
        '(?ms)^\s{4}' + [regex]::Escape($Name) +
        ':\s*\r?\n(?<block>(?:^\s{6,}.*\r?\n?)+)'
    )

    $match = [regex]::Match($Roadmap, $pattern)

    if (-not $match.Success) {
        throw "HANDOFF_EXECUTION_BLOCK_NOT_FOUND:$Name"
    }

    return $match.Groups['block'].Value
}

function Get-CerebroContextIndexValues {
    param(
        [Parameter(Mandatory)][string]$Context,
        [Parameter(Mandatory)][string]$Name
    )

    $inlinePattern = (
        '(?m)^\s{4}' +
        [regex]::Escape($Name) +
        ':\s*\[\s*(?<items>[^\]]*)\s*\]\s*$'
    )

    $inlineMatch = [regex]::Match(
        $Context,
        $inlinePattern
    )

    if ($inlineMatch.Success) {
        $inlineItems = $inlineMatch.Groups['items'].Value

        if ([string]::IsNullOrWhiteSpace($inlineItems)) {
            return @()
        }

        return @(
            [regex]::Matches(
                $inlineItems,
                '"([^"]+)"'
            ) |
                ForEach-Object {
                    $_.Groups[1].Value
                }
        )
    }

    $blockPattern = (
        '(?ms)^\s{4}' +
        [regex]::Escape($Name) +
        ':\s*\r?\n' +
        '(?<items>(?:^\s{6}-\s+"[^"]+"\s*\r?\n?)*)'
    )

    $blockMatch = [regex]::Match(
        $Context,
        $blockPattern
    )

    if (-not $blockMatch.Success) {
        throw "HANDOFF_CONTEXT_INDEX_NOT_FOUND:$Name"
    }

    return @(
        [regex]::Matches(
            $blockMatch.Groups['items'].Value,
            '"([^"]+)"'
        ) |
            ForEach-Object {
                $_.Groups[1].Value
            }
    )
}

function Invoke-CerebroHandoffCore {
    [CmdletBinding()]
    param(
        [string]$RepoPath = 'D:\Cerebro\Source\Cerebro_Source_v1.0',
        [string]$OutputPath =
            'D:\Cerebro\Run\handoff\CEREBRO_SESSION_HANDOFF_v1.json',
        [switch]$Force
    )

    $ErrorActionPreference = 'Stop'

    if (-not (Test-Path -LiteralPath $RepoPath -PathType Container)) {
        throw "HANDOFF_REPOSITORY_NOT_FOUND:$RepoPath"
    }

    if (
        (Test-Path -LiteralPath $OutputPath -PathType Leaf) -and
        -not $Force
    ) {
        throw "HANDOFF_OUTPUT_EXISTS_USE_FORCE:$OutputPath"
    }

    Push-Location -LiteralPath $RepoPath

    try {
        if ((git rev-parse --is-inside-work-tree).Trim() -ne 'true') {
            throw 'HANDOFF_NOT_GIT_REPOSITORY'
        }

        $branch = (git branch --show-current).Trim()

        if ([string]::IsNullOrWhiteSpace($branch)) {
            throw 'HANDOFF_DETACHED_HEAD'
        }

        $head = (git rev-parse HEAD).Trim()
        $status = @(git status --porcelain --untracked-files=all)

        if ($status.Count -ne 0) {
            throw "HANDOFF_WORKTREE_NOT_CLEAN:$($status -join '|')"
        }

        git rev-parse --abbrev-ref '@{upstream}' 2>$null | Out-Null

        if ($LASTEXITCODE -ne 0) {
            throw 'HANDOFF_UPSTREAM_NOT_CONFIGURED'
        }

        $upstream = (git rev-parse '@{upstream}').Trim()

        if ($head -ne $upstream) {
            throw "HANDOFF_LOCAL_NOT_EQUAL_UPSTREAM:LOCAL=$head`:UPSTREAM=$upstream"
        }

        $roadmapRelative = 'engines/project/roadmap.yaml'
        $contextRelative = 'engines/context/working-context.yaml'
        $roadmapPath = Join-Path $RepoPath 'engines\project\roadmap.yaml'
        $contextPath = Join-Path $RepoPath 'engines\context\working-context.yaml'

        foreach ($path in @($roadmapPath, $contextPath)) {
            if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
                throw "HANDOFF_REQUIRED_FILE_MISSING:$path"
            }
        }

        $roadmap = [IO.File]::ReadAllText($roadmapPath)
        $context = [IO.File]::ReadAllText($contextPath)

        $currentBlock = Get-CerebroExecutionBlock `
            -Roadmap $roadmap `
            -Name current

        $nextBlock = Get-CerebroExecutionBlock `
            -Roadmap $roadmap `
            -Name next

        $current = [ordered]@{
            phase_ref = Get-CerebroYamlScalar $currentBlock 'phase_ref'
            milestone_ref = Get-CerebroYamlScalar $currentBlock 'milestone_ref'
            patch_ref = Get-CerebroYamlScalar $currentBlock 'patch_ref'
            canonical_command =
                Get-CerebroYamlScalar $currentBlock 'canonical_command'
            status = Get-CerebroYamlScalar $currentBlock 'status'
        }

        $next = [ordered]@{
            phase_ref = Get-CerebroYamlScalar $nextBlock 'phase_ref'
            milestone_ref = Get-CerebroYamlScalar $nextBlock 'milestone_ref'
            patch_ref = Get-CerebroYamlScalar $nextBlock 'patch_ref'
            canonical_command =
                Get-CerebroYamlScalar $nextBlock 'canonical_command'
            status = Get-CerebroYamlScalar $nextBlock 'status'
        }

        $material = (
            '{0}|{1}|{2}|{3}' -f
            'morgul-tech/Cerebro-Source-1.0',
            $branch,
            $head,
            $current.patch_ref
        )

        $sha = [Security.Cryptography.SHA256]::Create()

        try {
            $handoffId = (
                [BitConverter]::ToString(
                    $sha.ComputeHash(
                        [Text.Encoding]::UTF8.GetBytes($material)
                    )
                )
            ).Replace('-', '').ToLowerInvariant().Substring(0, 24)
        }
        finally {
            $sha.Dispose()
        }

        $artifact = [ordered]@{
            schema = 'cerebro-session-handoff/v0.1'
            handoff = [ordered]@{
                id = "HANDOFF-$handoffId"
                generated_at_utc = [DateTime]::UtcNow.ToString('o')
                authority = [ordered]@{
                    repository = 'morgul-tech/Cerebro-Source-1.0'
                    branch = $branch
                    source_commit = $head
                    authority = 'derived_from_authoritative_source'
                }
                integrity = [ordered]@{
                    roadmap_path = $roadmapRelative
                    roadmap_sha256 = Get-CerebroFileSha256 $roadmapPath
                    working_context_path = $contextRelative
                    working_context_sha256 =
                        Get-CerebroFileSha256 $contextPath
                }
                execution = [ordered]@{
                    current = $current
                    next = $next
                }
                context = [ordered]@{
                    current_basis_refs = @(
                        Get-CerebroContextIndexValues `
                            $context `
                            'current_basis_refs'
                    )
                    decision_refs = @(
                        Get-CerebroContextIndexValues `
                            $context `
                            'decision_refs'
                    )
                    override_refs = @(
                        Get-CerebroContextIndexValues `
                            $context `
                            'override_refs'
                    )
                    wisdom_refs = @(
                        Get-CerebroContextIndexValues `
                            $context `
                            'current_wisdom_refs'
                    )
                }
                repository_state = [ordered]@{
                    worktree_clean = $true
                    local_head = $head
                    upstream_head = $upstream
                    local_equals_upstream = $true
                }
                continuation = [ordered]@{
                    resume_command =
                        "cerebro_resume -HandoffPath `"$OutputPath`""
                    first_required_action = $current.canonical_command
                    fail_closed_conditions = @(
                        'source_commit_divergence',
                        'dirty_worktree',
                        'local_upstream_divergence',
                        'roadmap_hash_mismatch',
                        'working_context_hash_mismatch',
                        'execution_token_mismatch'
                    )
                }
            }
        }

        $directory = Split-Path -Parent $OutputPath
        [IO.Directory]::CreateDirectory($directory) | Out-Null

        $artifact |
            ConvertTo-Json -Depth 12 |
            Set-Content `
                -LiteralPath $OutputPath `
                -Encoding utf8

        Write-Host (
            (
                'CEREBRO_HANDOFF STATE=SUCCESS_GENERATED HANDOFF_ID={0} ' +
                'SOURCE_COMMIT={1} CURRENT_PATCH={2} OUTPUT={3}'
            ) -f
            $artifact.handoff.id,
            $head,
            $current.patch_ref,
            $OutputPath
        )

        return [pscustomobject]$artifact.handoff
    }
    finally {
        Pop-Location
    }
}
