Set-StrictMode -Version 2.0

function Invoke-AscNative {
    param(
        [Parameter(Mandatory)][string]$Executable,
        [Parameter(Mandatory)][string[]]$ArgumentList,
        [Parameter(Mandatory)][string]$WorkingDirectory
    )

    $stdoutFile=[IO.Path]::GetTempFileName()
    $stderrFile=[IO.Path]::GetTempFileName()
    $previous=$ErrorActionPreference
    $exitCode=$null

    try {
        $ErrorActionPreference='Continue'
        Push-Location $WorkingDirectory
        try {
            & $Executable @ArgumentList 1> $stdoutFile 2> $stderrFile
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
        $stdout=[IO.File]::ReadAllText($stdoutFile)
        $stderr=[IO.File]::ReadAllText($stderrFile)
    }
    finally {
        Remove-Item -LiteralPath $stdoutFile,$stderrFile -Force -ErrorAction SilentlyContinue
    }

    [pscustomobject]@{ExitCode=$exitCode;Stdout=$stdout;Stderr=$stderr}
}

function Get-CerebroActiveTrackedFiles {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Root)

    $git=Get-Command git.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if($null -eq $git){
        $git=Get-Command git -ErrorAction SilentlyContinue | Select-Object -First 1
    }
    if($null -eq $git){ throw 'ASC_GIT_NOT_FOUND' }

    $native=Invoke-AscNative -Executable ([string]$git.Source) -ArgumentList @('ls-files','--cached','--others','--exclude-standard') -WorkingDirectory $Root
    if($native.ExitCode -ne 0){
        throw ('ASC_GIT_LS_FILES_FAILED:{0}:{1}' -f $native.ExitCode,$native.Stderr)
    }

    $allowedExtensions=@('.yaml','.yml','.json','.ps1','.py','.md','.txt','.toml','.ini','.cfg','.csv')
    $files=@()

    foreach($raw in ([string]$native.Stdout -split '\r?\n')){
        $relative=$raw.Trim().Replace('\','/')
        if([string]::IsNullOrWhiteSpace($relative)){continue}
        if($relative.StartsWith('history/',[StringComparison]::OrdinalIgnoreCase)){continue}

        $full=Join-Path $Root ($relative -replace '/','\')
        if(-not(Test-Path -LiteralPath $full -PathType Leaf)){continue}

        $extension=[IO.Path]::GetExtension($relative).ToLowerInvariant()
        if($allowedExtensions -notcontains $extension){continue}
        $files += $relative
    }

    return @($files | Sort-Object -Unique)
}

function Invoke-CerebroActiveSourceIntegrityClosure {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Root,
        [string]$ExpectedSourceCommit=''
    )

    $rootPath=[IO.Path]::GetFullPath($Root)
    if(-not(Test-Path -LiteralPath $rootPath -PathType Container)){
        throw ('ASC_INTEGRITY_ROOT_NOT_FOUND:{0}' -f $rootPath)
    }

    $validator=Join-Path $rootPath 'tooling\validator\cerebro_active_source_closure.ps1'
    if(-not(Test-Path -LiteralPath $validator -PathType Leaf)){
        throw 'ASC_DETERMINISTIC_VALIDATOR_NOT_FOUND'
    }

    $activeFiles=@(Get-CerebroActiveTrackedFiles -Root $rootPath)
    if($activeFiles.Count -eq 0){ throw 'ASC_ACTIVE_SET_EMPTY' }

    . $validator
    $inner=Invoke-CerebroActiveSourceClosure `
        -Root $rootPath `
        -ActiveFiles $activeFiles `
        -GeneratedFiles @() `
        -ExpectedSourceCommit $ExpectedSourceCommit

    [pscustomobject]@{
        schema='cerebro-active-source-integrity-closure-result/v1'
        result=[string]$inner.result
        checked_active_files=$activeFiles.Count
        history_excluded=$true
        generated_active_inputs_checked=0
        deterministic_validator='tooling/validator/cerebro_active_source_closure.ps1'
        findings=@($inner.findings)
        full_system_release_closure_invoked=$false
    }
}
