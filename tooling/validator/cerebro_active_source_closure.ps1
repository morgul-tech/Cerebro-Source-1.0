Set-StrictMode -Version 2.0

function New-AscFinding {
    param(
        [Parameter(Mandatory)][string]$Code,
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Message
    )
    [pscustomobject]@{code=$Code;path=$Path;message=$Message}
}

function Get-AscGitCanonicalBlobShaFromFile {
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$RelativePath,
        [Parameter(Mandatory)][string]$LiteralPath
    )

    if(-not(Test-Path -LiteralPath $LiteralPath -PathType Leaf)){
        throw ('ASC_CANONICAL_BLOB_SOURCE_MISSING:{0}' -f $RelativePath)
    }

    $git=Get-Command git.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if($null -eq $git){
        $git=Get-Command git -ErrorAction SilentlyContinue | Select-Object -First 1
    }
    if($null -eq $git){throw 'ASC_GIT_NOT_FOUND_FOR_CANONICAL_BLOB'}

    $stdoutFile=[IO.Path]::GetTempFileName()
    $stderrFile=[IO.Path]::GetTempFileName()
    $previous=$ErrorActionPreference
    $exitCode=$null

    try {
        $ErrorActionPreference='Continue'
        Push-Location $Root
        try {
            $normalized=$RelativePath.Replace('\','/')
            & ([string]$git.Source) -C $Root hash-object ('--path={0}' -f $normalized) -- $LiteralPath 1> $stdoutFile 2> $stderrFile
            $exitCode=$LASTEXITCODE
        }
        finally {
            Pop-Location
        }
    }
    finally {
        $ErrorActionPreference=$previous
    }

    try {
        $stdout=[IO.File]::ReadAllText($stdoutFile).Trim().ToLowerInvariant()
        $stderr=[IO.File]::ReadAllText($stderrFile).Trim()
    }
    finally {
        Remove-Item -LiteralPath $stdoutFile,$stderrFile -Force -ErrorAction SilentlyContinue
    }

    if($exitCode -ne 0){
        throw ('ASC_CANONICAL_BLOB_HASH_FAILED:{0}:{1}:{2}' -f $RelativePath,$exitCode,$stderr)
    }
    if([string]::IsNullOrWhiteSpace($stdout)){
        throw ('ASC_CANONICAL_BLOB_HASH_EMPTY:{0}' -f $RelativePath)
    }
    return $stdout
}

function Invoke-CerebroActiveSourceClosure {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Root,
        [string[]]$ActiveFiles=@(),
        [string[]]$GeneratedFiles=@(),
        [string]$ExpectedSourceCommit='',
        [string[]]$ProhibitedActivePaths=@(
            'engines/dialog/',
            'engines/collaboration/',
            'modules/core-rules/',
            'tooling/loader/',
            'standards/runtime/handboot-runtime-state-schema.yaml'
        )
    )

    $rootPath=[IO.Path]::GetFullPath($Root)
    if(-not(Test-Path -LiteralPath $rootPath -PathType Container)){throw "ASC_ROOT_NOT_FOUND:$rootPath"}

    $findings=@()
    $canonicalKeys=@{}

    foreach($relative in @($ActiveFiles)){
        $normalized=$relative.Replace('\','/')
        $full=Join-Path $rootPath ($normalized -replace '/','\')
        if(-not(Test-Path -LiteralPath $full -PathType Leaf)){
            $findings += (New-AscFinding -Code 'ASC_ACTIVE_FILE_MISSING' -Path $normalized -Message 'Declared active file is missing.')
            continue
        }

        foreach($legacy in $ProhibitedActivePaths){
            if($normalized.StartsWith($legacy,[StringComparison]::OrdinalIgnoreCase)){
                $findings += (New-AscFinding -Code 'ASC_PROHIBITED_ACTIVE_LEGACY_PATH' -Path $normalized -Message ('Active path is superseded: '+$legacy))
            }
        }

        $text=[IO.File]::ReadAllText($full)

        if($text -match '(?m)^\s*lifecycle_state:\s*TEMPORARY\s*$'){
            foreach($field in @('introduced_by:','reason:','owner:','replacement_condition:','retirement_trigger:')){
                if(-not $text.Contains($field)){
                    $findings += (New-AscFinding -Code 'ASC_TEMPORARY_FIELD_MISSING' -Path $normalized -Message ('Missing temporary-definition field '+$field))
                }
            }
            if((-not $text.Contains('latest_allowed_milestone:')) -and (-not $text.Contains('review_trigger:'))){
                $findings += (New-AscFinding -Code 'ASC_TEMPORARY_REVIEW_BOUND_MISSING' -Path $normalized -Message 'Temporary definition lacks review bound.')
            }
            if($text -match '(?m)^\s*retirement_trigger_reached:\s*true\s*$'){
                $findings += (New-AscFinding -Code 'ASC_TEMPORARY_EXPIRED' -Path $normalized -Message 'Temporary definition retirement trigger is reached.')
            }
        }

        if($text -match '(?m)^\s*active_reference_to_superseded:\s*true\s*$'){
            $findings += (New-AscFinding -Code 'ASC_ACTIVE_SUPERSEDED_REFERENCE' -Path $normalized -Message 'Active definition references superseded material.')
        }

        foreach($m in [regex]::Matches($text,'(?m)^\s*canonical_definition_key:\s*["'']?([^"''\r\n#]+)')){
            $key=$m.Groups[1].Value.Trim()
            if($canonicalKeys.ContainsKey($key)){
                $findings += (New-AscFinding -Code 'ASC_DUPLICATE_CANONICAL_DEFINITION_KEY' -Path $normalized -Message ('Duplicate canonical_definition_key: '+$key))
            }else{$canonicalKeys[$key]=$normalized}
        }
    }

    foreach($relative in @($GeneratedFiles)){
        $normalized=$relative.Replace('\','/')
        $full=Join-Path $rootPath ($normalized -replace '/','\')
        if(-not(Test-Path -LiteralPath $full -PathType Leaf)){continue}
        $text=[IO.File]::ReadAllText($full)
        if(-not $text.Contains('source_commit')){
            $findings += (New-AscFinding -Code 'ASC_GENERATED_SOURCE_COMMIT_MISSING' -Path $normalized -Message 'Generated metadata lacks source_commit.')
        }
        if(-not $text.Contains('generated_from')){
            $findings += (New-AscFinding -Code 'ASC_GENERATED_FROM_MISSING' -Path $normalized -Message 'Generated metadata lacks generated_from.')
        }
        if(-not [string]::IsNullOrWhiteSpace($ExpectedSourceCommit)){
            $escaped=[regex]::Escape($ExpectedSourceCommit)
            if($text -notmatch $escaped){
                $findings += (New-AscFinding -Code 'ASC_GENERATED_STALE' -Path $normalized -Message 'Generated metadata does not match expected source commit.')
            }
        }
    }

    $wave01Required=@(
        'mcp/execution-surface-resolution.yaml',
        'history/DUALITYARC_WAVE_01_RETIREMENT.json',
        'history/mcp/source-mapping.yaml',
        'history/mcp/physical-target-resolution.yaml',
        'history/FILE_INVENTORY_v1.0.json',
        'history/SOURCE_AUDIT_v1.0.json'
    )
    foreach($relative in $wave01Required){
        $full=Join-Path $rootPath ($relative -replace '/','\')
        if(-not(Test-Path -LiteralPath $full -PathType Leaf)){
            $findings += (New-AscFinding -Code 'ASC_DUALITYARC_WAVE01_REQUIRED_FILE_MISSING' -Path $relative -Message 'DUALITYARC Wave 01 required file is missing.')
        }
    }

    $retirementEvidencePath=Join-Path $rootPath 'history\DUALITYARC_WAVE_01_RETIREMENT.json'
    if(Test-Path -LiteralPath $retirementEvidencePath -PathType Leaf){
        try {$retirementEvidence=Get-Content -LiteralPath $retirementEvidencePath -Raw | ConvertFrom-Json}
        catch {
            $retirementEvidence=$null
            $findings += (New-AscFinding -Code 'ASC_DUALITYARC_WAVE01_RETIREMENT_EVIDENCE_INVALID' -Path 'history/DUALITYARC_WAVE_01_RETIREMENT.json' -Message $_.Exception.Message)
        }
        if($null -ne $retirementEvidence){
            if([string]$retirementEvidence.schema -ne 'cerebro-dualityarc-retirement-evidence/v1'){
                $findings += (New-AscFinding -Code 'ASC_DUALITYARC_WAVE01_RETIREMENT_SCHEMA_INVALID' -Path 'history/DUALITYARC_WAVE_01_RETIREMENT.json' -Message 'Unexpected retirement evidence schema.')
            }
            foreach($entry in @($retirementEvidence.retirements)){
                $retired=[string]$entry.retired_path
                $history=[string]$entry.history_path
                $retiredFull=Join-Path $rootPath ($retired -replace '/','\')
                $historyFull=Join-Path $rootPath ($history -replace '/','\')
                if(Test-Path -LiteralPath $retiredFull){
                    $findings += (New-AscFinding -Code 'ASC_DUALITYARC_WAVE01_RETIRED_ACTIVE_PATH_PRESENT' -Path $retired -Message 'Retired active path must be physically absent.')
                }
                if(-not(Test-Path -LiteralPath $historyFull -PathType Leaf)){
                    $findings += (New-AscFinding -Code 'ASC_DUALITYARC_WAVE01_HISTORY_FILE_MISSING' -Path $history -Message 'Historical byte-preserving copy is missing.')
                }
                elseif((Get-AscGitCanonicalBlobShaFromFile -Root $rootPath -RelativePath $history -LiteralPath $historyFull) -ne ([string]$entry.git_blob_sha).ToLowerInvariant()){
                    $findings += (New-AscFinding -Code 'ASC_DUALITYARC_WAVE01_HISTORY_BLOB_MISMATCH' -Path $history -Message 'Historical copy does not equal the recorded retired baseline blob.')
                }
            }
        }
    }

    foreach($relative in @('mcp/source-mapping.yaml','mcp/physical-target-resolution.yaml','FILE_INVENTORY.json','SOURCE_AUDIT_v1.0.json')){
        $full=Join-Path $rootPath ($relative -replace '/','\')
        if(Test-Path -LiteralPath $full){
            $findings += (New-AscFinding -Code 'ASC_DUALITYARC_WAVE01_RETIRED_ACTIVE_PATH_PRESENT' -Path $relative -Message 'Retired active path must be physically absent.')
        }
    }

    $resolutionPath=Join-Path $rootPath 'mcp\execution-surface-resolution.yaml'
    if(Test-Path -LiteralPath $resolutionPath -PathType Leaf){
        $resolutionText=[IO.File]::ReadAllText($resolutionPath)
        foreach($requiredText in @('CEREBRO-MCP-EXECUTION-SURFACE-RESOLUTION-001','canonical_definition_key: mcp.execution_surface_resolution')){
            if(-not$resolutionText.Contains($requiredText)){
                $findings += (New-AscFinding -Code 'ASC_DUALITYARC_WAVE01_CANONICAL_CONTRACT_INVALID' -Path 'mcp/execution-surface-resolution.yaml' -Message ('Missing canonical contract marker: '+$requiredText))
            }
        }
    }

    $architecturePath=Join-Path $rootPath 'mcp\architecture.yaml'
    if(Test-Path -LiteralPath $architecturePath -PathType Leaf){
        $architectureText=[IO.File]::ReadAllText($architecturePath)
        if($architectureText -match '(?m)^\s*active_component_topology:\s*$'){
            $findings += (New-AscFinding -Code 'ASC_DUALITYARC_WAVE01_DUPLICATE_TOPOLOGY_PRESENT' -Path 'mcp/architecture.yaml' -Message 'Stored duplicate component topology is prohibited.')
        }
        if(-not$architectureText.Contains('topology_authority: cerebro.yaml')){
            $findings += (New-AscFinding -Code 'ASC_DUALITYARC_WAVE01_TOPOLOGY_AUTHORITY_MISSING' -Path 'mcp/architecture.yaml' -Message 'cerebro.yaml must be the declared topology authority.')
        }
    }

    $result=if($findings.Count -eq 0){'PASS'}else{'FAIL'}
    [pscustomobject]@{
        schema='cerebro-active-source-closure-result/v0.1'
        result=$result
        finding_count=$findings.Count
        findings=$findings
        checked_active_files=@($ActiveFiles).Count
        checked_generated_files=@($GeneratedFiles).Count
    }
}
