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

function cerebro_tools_status {
    $config = Get-CerebroToolsConfig

    [pscustomobject]@{
        module_version = '1.0.0'
        module_path = $PSScriptRoot
        config_path = 'D:\Cerebro\Config\Cerebro.Tools.json'
        working_source_path = $config.working_source_path
        scripts_root = $config.scripts_root
        cerebro_receive = [bool](
            Get-Command cerebro_receive -ErrorAction SilentlyContinue
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
                '..\..\..\loader\cerebro_resume.ps1'
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
                '..\..\..\loader\cerebro_boot.ps1'
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
    -Function cerebro_receive, cerebro_sync, cerebro_handoff, cerebro_resume, bootCerebro, bootini, cerebro_tools_status
