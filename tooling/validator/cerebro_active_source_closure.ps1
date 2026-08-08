Set-StrictMode -Version 2.0

function New-AscFinding {
    param(
        [Parameter(Mandatory)][string]$Code,
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Message
    )
    [pscustomobject]@{code=$Code;path=$Path;message=$Message}
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