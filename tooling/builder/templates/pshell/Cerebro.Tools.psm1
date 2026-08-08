Set-StrictMode -Version 2.0

function Get-CerebroToolsConfig {
    $path = 'D:\Cerebro\Config\Cerebro.Tools.json'

    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Cerebro Tools configuration is missing: $path"
    }

    Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
}

function Get-CerebroSha256 {
    param(
        [Parameter(Mandatory)]
        [byte[]]$Bytes
    )

    $sha = [System.Security.Cryptography.SHA256]::Create()

    try {
        ([BitConverter]::ToString(
            $sha.ComputeHash($Bytes)
        )).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
}

function Test-CerebroAllowedTarget {
    param(
        [Parameter(Mandatory)]
        [string]$TargetPath,

        [Parameter(Mandatory)]
        [object[]]$AllowedRoots
    )

    $target = [IO.Path]::GetFullPath($TargetPath)

    foreach ($rootValue in $AllowedRoots) {
        $root = [IO.Path]::GetFullPath(
            [string]$rootValue
        ).TrimEnd('\')

        if (
            $target.Equals(
                $root,
                [StringComparison]::OrdinalIgnoreCase
            ) -or
            $target.StartsWith(
                $root + '\',
                [StringComparison]::OrdinalIgnoreCase
            )
        ) {
            return $true
        }
    }

    return $false
}

function cerebro_receive {
    [CmdletBinding()]
    param(
        [string]$Text
    )

    $ErrorActionPreference = 'Stop'

    $startMarker = 'CEREBRO_SEALED_V1 '
    $endMarker = 'CEREBRO_SEALED_END'

    if (-not $PSBoundParameters.ContainsKey('Text')) {
        $Text = Get-Clipboard -Raw
    }

    if ([string]::IsNullOrWhiteSpace($Text)) {
        throw 'Clipboard is empty.'
    }

    $startIndex = $Text.IndexOf(
        $startMarker,
        [StringComparison]::Ordinal
    )

    if ($startIndex -lt 0) {
        throw 'SEALED_START_NOT_FOUND'
    }

    $lineEnd = $Text.IndexOf(
        "`n",
        $startIndex
    )

    if ($lineEnd -lt 0) {
        throw 'SEALED_HEADER_TERMINATOR_NOT_FOUND'
    }

    $headerJson = $Text.Substring(
        $startIndex + $startMarker.Length,
        $lineEnd - ($startIndex + $startMarker.Length)
    ).TrimEnd("`r")

    $payloadStart = $lineEnd + 1

    $endIndex = $Text.IndexOf(
        $endMarker,
        $payloadStart,
        [StringComparison]::Ordinal
    )

    if ($endIndex -lt 0) {
        throw 'SEALED_END_NOT_FOUND'
    }

    $secondStart = $Text.IndexOf(
        $startMarker,
        $endIndex + $endMarker.Length,
        [StringComparison]::Ordinal
    )

    if ($secondStart -ge 0) {
        throw 'MULTIPLE_SEALED_PAYLOADS'
    }

    $header = $headerJson | ConvertFrom-Json

    foreach ($field in @(
        'payload_id',
        'target_path',
        'sha256',
        'bytes',
        'content_type'
    )) {
        if (-not ($header.PSObject.Properties.Name -contains $field)) {
            throw "SEALED_HEADER_FIELD_MISSING:$field"
        }
    }

    $base64 = (
        $Text.Substring(
            $payloadStart,
            $endIndex - $payloadStart
        ) -replace '\s', ''
    )

    try {
        $bytes = [Convert]::FromBase64String($base64)
    }
    catch {
        throw 'SEALED_BASE64_INVALID'
    }

    if ($bytes.Length -ne [int64]$header.bytes) {
        throw (
            "SEALED_LENGTH_MISMATCH expected={0} actual={1}" -f
            $header.bytes,
            $bytes.Length
        )
    }

    $actualHash = Get-CerebroSha256 -Bytes $bytes

    if (
        -not $actualHash.Equals(
            [string]$header.sha256,
            [StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw (
            "SEALED_HASH_MISMATCH expected={0} actual={1}" -f
            $header.sha256,
            $actualHash
        )
    }

    $config = Get-CerebroToolsConfig
    $target = [IO.Path]::GetFullPath(
        [string]$header.target_path
    )

    $allowed = Test-CerebroAllowedTarget `
        -TargetPath $target `
        -AllowedRoots @($config.allowed_target_roots)

    if (-not $allowed) {
        throw "SEALED_TARGET_NOT_ALLOWED:$target"
    }

    $contentType = [string]$header.content_type

    if ($contentType -eq 'powershell') {
        $utf8Strict = New-Object Text.UTF8Encoding(
            $false,
            $true
        )

        try {
            $scriptText = $utf8Strict.GetString($bytes)
        }
        catch {
            throw 'SEALED_UTF8_INVALID'
        }

        $tokens = $null
        $parseErrors = $null

        [Management.Automation.Language.Parser]::ParseInput(
            $scriptText,
            [ref]$tokens,
            [ref]$parseErrors
        ) | Out-Null

        if ($parseErrors.Count -gt 0) {
            $message = (
                $parseErrors |
                    ForEach-Object { $_.Message }
            ) -join ' | '

            throw "SEALED_POWERSHELL_PARSE_FAILED:$message"
        }
    }

    $directory = Split-Path -Parent $target
    [IO.Directory]::CreateDirectory($directory) | Out-Null

    $temporary = Join-Path $directory (
        '.' +
        [IO.Path]::GetFileName($target) +
        '.tmp-' +
        [guid]::NewGuid().ToString('N')
    )

    [IO.File]::WriteAllBytes(
        $temporary,
        $bytes
    )

    $backup = $null

    try {
        if (Test-Path -LiteralPath $target -PathType Leaf) {
            $backup = (
                "$target.backup-{0}" -f
                (Get-Date -Format 'yyyyMMdd-HHmmss')
            )

            [IO.File]::Replace(
                $temporary,
                $target,
                $backup,
                $true
            )
        }
        else {
            [IO.File]::Move(
                $temporary,
                $target
            )
        }
    }
    finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item `
                -LiteralPath $temporary `
                -Force `
                -ErrorAction SilentlyContinue
        }
    }

    $installedBytes = [IO.File]::ReadAllBytes($target)
    $installedHash = Get-CerebroSha256 -Bytes $installedBytes

    if (
        -not $installedHash.Equals(
            $actualHash,
            [StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw 'SEALED_POST_INSTALL_HASH_MISMATCH'
    }

    $receiptMaterial = (
        "{0}|{1}|{2}|{3}" -f
        $header.payload_id,
        $target,
        $installedHash,
        $installedBytes.Length
    )

    $receiptBytes = [Text.Encoding]::UTF8.GetBytes(
        $receiptMaterial
    )

    $receipt = Get-CerebroSha256 -Bytes $receiptBytes

    Write-Host ''
    Write-Host '======================================================'
    Write-Host 'CEREBRO SEALED RECEIVE RESULT'
    Write-Host '======================================================'
    Write-Host ("payload_id:           {0}" -f $header.payload_id)
    Write-Host ("target_path:          {0}" -f $target)
    Write-Host ("content_type:         {0}" -f $contentType)
    Write-Host ("bytes:                {0}" -f $installedBytes.Length)
    Write-Host ("sha256:               {0}" -f $installedHash)

    if ($backup) {
        Write-Host ("backup:               {0}" -f $backup)
    }
    else {
        Write-Host 'backup:               NONE'
    }

    Write-Host 'final_state:          SUCCESS_INSTALLED'
    Write-Host 'user_action_required: NONE'
    Write-Host '======================================================'

    Write-Host (
        "CEREBRO_RECEIVE PAYLOAD={0} STATE=SUCCESS_INSTALLED SHA256={1} RECEIPT={2}" -f
        $header.payload_id,
        $installedHash,
        $receipt
    )
}

function Get-CerebroDownloadsDirectory {
    $downloads = $null

    try {
        $key = Get-ItemProperty `
            -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders' `
            -ErrorAction Stop

        $raw = $key.'{374DE290-123F-4565-9164-39C4925E467B}'

        if (-not [string]::IsNullOrWhiteSpace([string]$raw)) {
            $downloads =
                [Environment]::ExpandEnvironmentVariables(
                    [string]$raw
                )
        }
    }
    catch {}

    if ([string]::IsNullOrWhiteSpace($downloads)) {
        $downloads = Join-Path $env:USERPROFILE 'Downloads'
    }

    return [IO.Path]::GetFullPath($downloads)
}

function Test-CerebroPathUnderRoot {
    param(
        [Parameter(Mandatory)]
        [string]$Path,

        [Parameter(Mandatory)]
        [string]$Root
    )

    $fullPath = [IO.Path]::GetFullPath($Path)
    $fullRoot = [IO.Path]::GetFullPath($Root).TrimEnd('\')

    return (
        $fullPath.Equals(
            $fullRoot,
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        $fullPath.StartsWith(
            $fullRoot + '\',
            [StringComparison]::OrdinalIgnoreCase
        )
    )
}

function Get-CerebroFileSha256 {
    param(
        [Parameter(Mandatory)]
        [string]$Path
    )

    $bytes = [IO.File]::ReadAllBytes($Path)
    Get-CerebroSha256 -Bytes $bytes
}

function cpatch {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory, Position=0)]
        [ValidateNotNullOrEmpty()]
        [string]$FileName,

        [Parameter(Mandatory, Position=1)]
        [ValidatePattern('^[A-Fa-f0-9]{64}$')]
        [string]$Sha256,

        [string]$RunRoot = 'D:\Cerebro\Run'
    )

    $ErrorActionPreference = 'Stop'

    $expectedHash = $Sha256.ToLowerInvariant()
    $runRootFull = [IO.Path]::GetFullPath($RunRoot)
    [IO.Directory]::CreateDirectory($runRootFull) | Out-Null

    $downloadsRoot = Get-CerebroDownloadsDirectory

    $leaf = [IO.Path]::GetFileName($FileName)

    if (
        [string]::IsNullOrWhiteSpace($leaf) -or
        [IO.Path]::GetExtension($leaf) -ne '.ps1'
    ) {
        throw 'CPATCH_FILE_MUST_BE_PS1'
    }

    if ($leaf -notmatch '^CEREBRO_[A-Za-z0-9_.-]+\.ps1$') {
        throw 'CPATCH_FILENAME_NOT_CEREBRO_ARTIFACT'
    }

    $candidates = @()

    if ([IO.Path]::IsPathRooted($FileName)) {
        $fullRequested = [IO.Path]::GetFullPath($FileName)

        $allowed = (
            (Test-CerebroPathUnderRoot -Path $fullRequested -Root $runRootFull) -or
            (Test-CerebroPathUnderRoot -Path $fullRequested -Root $downloadsRoot)
        )

        if (-not $allowed) {
            throw 'CPATCH_ROOTED_PATH_OUTSIDE_ALLOWED_ROOTS'
        }

        if (Test-Path -LiteralPath $fullRequested -PathType Leaf) {
            $candidates += $fullRequested
        }
    }
    else {
        if ($FileName -ne $leaf) {
            throw 'CPATCH_RELATIVE_SUBPATH_NOT_ALLOWED'
        }

        $runCandidate = Join-Path $runRootFull $leaf
        $downloadCandidate = Join-Path $downloadsRoot $leaf

        if (Test-Path -LiteralPath $runCandidate -PathType Leaf) {
            $candidates += $runCandidate
        }

        if (
            -not $downloadCandidate.Equals(
                $runCandidate,
                [StringComparison]::OrdinalIgnoreCase
            ) -and
            (Test-Path -LiteralPath $downloadCandidate -PathType Leaf)
        ) {
            $candidates += $downloadCandidate
        }
    }

    $candidates = @($candidates | Select-Object -Unique)

    if ($candidates.Count -eq 0) {
        throw (
            'CPATCH_FILE_NOT_FOUND:' +
            $leaf +
            ':RUN=' + $runRootFull +
            ':DOWNLOADS=' + $downloadsRoot
        )
    }

    $matching = @()

    foreach ($candidate in $candidates) {
        $actual = Get-CerebroFileSha256 -Path $candidate

        if ($actual -eq $expectedHash) {
            $matching += $candidate
        }
    }

    if ($matching.Count -eq 0) {
        throw 'CPATCH_SHA256_MISMATCH'
    }

    $sourcePath = $null
    $runExact = Join-Path $runRootFull $leaf

    foreach ($candidate in $matching) {
        if (
            [IO.Path]::GetFullPath($candidate).Equals(
                [IO.Path]::GetFullPath($runExact),
                [StringComparison]::OrdinalIgnoreCase
            )
        ) {
            $sourcePath = $candidate
            break
        }
    }

    if (-not $sourcePath) {
        $sourcePath = $matching[0]
    }

    $stagedPath = $runExact

    if (
        -not (
            [IO.Path]::GetFullPath($sourcePath).Equals(
                [IO.Path]::GetFullPath($stagedPath),
                [StringComparison]::OrdinalIgnoreCase
            )
        )
    ) {
        if (Test-Path -LiteralPath $stagedPath -PathType Leaf) {
            $existingHash = Get-CerebroFileSha256 -Path $stagedPath

            if ($existingHash -ne $expectedHash) {
                throw 'CPATCH_RUN_DESTINATION_CONFLICT'
            }
        }
        else {
            $temporary = Join-Path $runRootFull (
                '.' + $leaf + '.stage-' +
                [guid]::NewGuid().ToString('N')
            )

            try {
                Copy-Item `
                    -LiteralPath $sourcePath `
                    -Destination $temporary `
                    -Force

                $temporaryHash =
                    Get-CerebroFileSha256 -Path $temporary

                if ($temporaryHash -ne $expectedHash) {
                    throw 'CPATCH_STAGE_HASH_MISMATCH'
                }

                [IO.File]::Move(
                    $temporary,
                    $stagedPath
                )
            }
            finally {
                if (Test-Path -LiteralPath $temporary) {
                    Remove-Item `
                        -LiteralPath $temporary `
                        -Force `
                        -ErrorAction SilentlyContinue
                }
            }
        }
    }

    $finalHash = Get-CerebroFileSha256 -Path $stagedPath

    if ($finalHash -ne $expectedHash) {
        throw 'CPATCH_FINAL_HASH_MISMATCH'
    }

    try {
        Unblock-File `
            -LiteralPath $stagedPath `
            -ErrorAction SilentlyContinue
    }
    catch {}

    $tokens = $null
    $parseErrors = $null

    [Management.Automation.Language.Parser]::ParseFile(
        $stagedPath,
        [ref]$tokens,
        [ref]$parseErrors
    ) | Out-Null

    if ($parseErrors.Count -gt 0) {
        $parseMessage = (
            $parseErrors |
                ForEach-Object { $_.Message }
        ) -join ' | '

        throw ('CPATCH_POWERSHELL_PARSE_FAILED:' + $parseMessage)
    }

    $powershell = (
        Get-Command powershell.exe -ErrorAction Stop |
            Select-Object -First 1
    ).Source

    $childArgs = @(
        '-NoLogo',
        '-NoProfile',
        '-ExecutionPolicy',
        'Bypass',
        '-File',
        $stagedPath
    )

    $priorCpatchHost = $env:CEREBRO_CPATCH_HOST
    $oldPreference = $ErrorActionPreference
    $childExit = $null

    try {
        $env:CEREBRO_CPATCH_HOST = '1'
        $ErrorActionPreference = 'Continue'

        & $powershell @childArgs
        $childExit = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $oldPreference

        if ($null -eq $priorCpatchHost) {
            Remove-Item Env:CEREBRO_CPATCH_HOST `
                -ErrorAction SilentlyContinue
        }
        else {
            $env:CEREBRO_CPATCH_HOST = $priorCpatchHost
        }
    }

    if ($childExit -ne 0) {
        throw ('CPATCH_CHILD_EXIT_NOT_ZERO:' + $childExit)
    }

    [ordered]@{
        state = 'CPATCH_PROCESS_COMPLETED'
        file = $leaf
        staged_path = $stagedPath
        sha256 = $finalHash
        parse_valid = $true
        child_exit_code = $childExit
        parent_terminal_preserved = $true
        patch_success_claimed = $false
        next_action = 'RETURN_PATCH_RECEIPT_TO_ASSISTANT'
    } | ConvertTo-Json -Compress
}

function cerebro_tools_status {
    $config = Get-CerebroToolsConfig

    [pscustomobject]@{
        module_version = '1.1.0'
        module_path = $PSScriptRoot
        config_path = 'D:\Cerebro\Config\Cerebro.Tools.json'
        working_source_path = $config.working_source_path
        scripts_root = $config.scripts_root
        cerebro_receive = [bool](
            Get-Command cerebro_receive -ErrorAction SilentlyContinue
        )
        cpatch = [bool](
            Get-Command cpatch -ErrorAction SilentlyContinue
        )
        cerebro_sync = [bool](
            Get-Command cerebro_sync -ErrorAction SilentlyContinue
        )
        cerebro_handoff = [bool](
            Get-Command cerebro_handoff -ErrorAction SilentlyContinue
        )
        cerebro_resume = [bool](
            Get-Command cerebro_resume -ErrorAction SilentlyContinue
        )
        bootCerebro = [bool](
            Get-Command bootCerebro -ErrorAction SilentlyContinue
        )
        bootini = [bool](
            Get-Command bootini -ErrorAction SilentlyContinue
        )
    }
}


function cerebro_sync {
    [CmdletBinding()]
    param(
        [string]$RepoPath = 'D:\Cerebro\Source\Cerebro_Source_v1.0',
        [string]$Remote = 'origin',
        [string]$Branch = 'main',
        [string]$CommitMessage = 'Sync local Working Source to authoritative Source',
        [string[]]$Paths = @(),
        [switch]$AllowRemainingChanges
    )

    $scriptPath = 'D:\Cerebro\Scripts\cerebro_sync.ps1'

    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        throw "CEREBRO_SYNC_SCRIPT_NOT_FOUND:$scriptPath"
    }

    & $scriptPath @PSBoundParameters
}

function cerebro_handoff {
    [CmdletBinding()]
    param(
        [string]$RepoPath = 'D:\Cerebro\Source\Cerebro_Source_v1.0',
        [string]$OutputPath =
            'D:\Cerebro\Run\handoff\CEREBRO_SESSION_HANDOFF_v1.json',
        [switch]$Force
    )

    $scriptPath = Join-Path `
        $PSScriptRoot `
        'cerebro_handoff.ps1'

    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        throw "CEREBRO_HANDOFF_SCRIPT_NOT_FOUND:$scriptPath"
    }

    . $scriptPath

    Invoke-CerebroHandoffCore @PSBoundParameters
}

function cerebro_resume {
    [CmdletBinding()]
    param(
        [string]$HandoffPath =
            'D:\Cerebro\Run\handoff\CEREBRO_SESSION_HANDOFF_v1.json',
        [string]$RepoPath = 'D:\Cerebro\Source\Cerebro_Source_v1.0'
    )

    $scriptPath = [IO.Path]::GetFullPath(
        (
            Join-Path `
                $PSScriptRoot `
                '..\..\..\runtime-host\cerebro_resume.ps1'
        )
    )

    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        throw "CEREBRO_RESUME_SCRIPT_NOT_FOUND:$scriptPath"
    }

    . $scriptPath

    Invoke-CerebroResumeCore @PSBoundParameters
}

function bootCerebro {
    [CmdletBinding()]
    param(
        [string]$WorkingSourcePath =
            'D:\Cerebro\Source\Cerebro_Source_v1.0',

        [string]$Remote = 'origin',

        [string]$Branch = 'main',

        [string]$BootEngineUrl =
            'https://raw.githubusercontent.com/morgul-tech/B/main/BootEngine',

        [string]$HandoffPath =
            'D:\Cerebro\Run\handoff\CEREBRO_SESSION_HANDOFF_v1.json',

        [string]$RuntimeStatePath =
            'D:\Cerebro\Run\active\CEREBRO_RUNTIME_STATE_v1.json',

        [switch]$SkipHandoff
    )

    $scriptPath = [IO.Path]::GetFullPath(
        (
            Join-Path `
                $PSScriptRoot `
                '..\..\..\runtime-host\cerebro_boot.ps1'
        )
    )

    if (
        -not (
            Test-Path `
                -LiteralPath $scriptPath `
                -PathType Leaf
        )
    ) {
        throw "CEREBRO_BOOT_SCRIPT_NOT_FOUND:$scriptPath"
    }

    . $scriptPath

    Invoke-CerebroBootCore @PSBoundParameters
}

function bootini {
    [CmdletBinding()]
    param(
        [string]$WorkingSourcePath =
            'D:\Cerebro\Source\Cerebro_Source_v1.0',

        [string]$Remote = 'origin',

        [string]$Branch = 'main',

        [string]$BootEngineUrl =
            'https://raw.githubusercontent.com/morgul-tech/B/main/BootEngine',

        [string]$HandoffPath =
            'D:\Cerebro\Run\handoff\CEREBRO_SESSION_HANDOFF_v1.json',

        [string]$RuntimeStatePath =
            'D:\Cerebro\Run\active\CEREBRO_RUNTIME_STATE_v1.json',

        [switch]$SkipHandoff
    )

    bootCerebro @PSBoundParameters
}
Export-ModuleMember `
    -Function cerebro_receive, cpatch, cerebro_sync, cerebro_handoff, cerebro_resume, bootCerebro, bootini, cerebro_tools_status

function cerebro_profile {
    [CmdletBinding()]
    param(
        [ValidateSet('view','set','revoke')][string]$Action='view',
        [string]$CanonicalDomain,
        [string]$CanonicalKey,
        $Value,
        [ValidateSet('GLOBAL','PROJECT')][string]$Scope='GLOBAL',
        [string]$PreferenceId,
        [string]$Path='D:\Cerebro\User\user-operating-profile.json'
    )
    $scriptPath=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\..\runtime-host\cerebro_user_profile.ps1'))
    if(-not(Test-Path -LiteralPath $scriptPath -PathType Leaf)){throw "CEREBRO_PROFILE_SCRIPT_NOT_FOUND:$scriptPath"}
    . $scriptPath
    switch($Action){
        'view' { Read-CerebroUserOperatingProfile -Path $Path }
        'set' {
            if([string]::IsNullOrWhiteSpace($CanonicalDomain)-or[string]::IsNullOrWhiteSpace($CanonicalKey)){throw 'CEREBRO_PROFILE_SET_REQUIRES_DOMAIN_AND_KEY'}
            Set-CerebroUserPreference -CanonicalDomain $CanonicalDomain -CanonicalKey $CanonicalKey -Value $Value -Scope $Scope -Path $Path
        }
        'revoke' {
            if([string]::IsNullOrWhiteSpace($PreferenceId)){throw 'CEREBRO_PROFILE_REVOKE_REQUIRES_ID'}
            Revoke-CerebroUserPreference -PreferenceId $PreferenceId -Path $Path
        }
    }
}
Export-ModuleMember -Function cerebro_profile