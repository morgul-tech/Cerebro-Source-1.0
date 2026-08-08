Set-StrictMode -Version 2.0

function Invoke-CerebroRuntimeReleaseGit {
    param(
        [Parameter(Mandatory)][string]$Git,
        [Parameter(Mandatory)][string[]]$Arguments,
        [int[]]$AllowedExitCodes = @(0)
    )

    $stdoutFile = [IO.Path]::GetTempFileName()
    $stderrFile = [IO.Path]::GetTempFileName()
    $oldPreference = $ErrorActionPreference

    try {
        $ErrorActionPreference = 'Continue'
        & $Git @Arguments 1> $stdoutFile 2> $stderrFile
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $oldPreference
    }

    try {
        $stdout = [IO.File]::ReadAllText($stdoutFile).TrimEnd([char[]]"`r`n")
        $stderr = [IO.File]::ReadAllText($stderrFile).TrimEnd([char[]]"`r`n")
    }
    finally {
        Remove-Item -LiteralPath $stdoutFile, $stderrFile -Force -ErrorAction SilentlyContinue
    }

    if ($AllowedExitCodes -notcontains $exitCode) {
        throw (
            'RUNTIME_RELEASE_GIT_FAILURE:' +
            $exitCode + ':' +
            ($Arguments -join ' ') + ':' +
            $stderr
        )
    }

    [pscustomobject]@{
        exit_code = $exitCode
        stdout = $stdout
        stderr = $stderr
    }
}

function Get-CerebroRuntimeReleaseSha256Text {
    param([Parameter(Mandatory)][string]$Text)

    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($Text)
        ([BitConverter]::ToString(
            $sha.ComputeHash($bytes)
        )).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
}

function Get-CerebroRuntimeReleaseFileSha256 {
    param([Parameter(Mandatory)][string]$Path)

    (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-CerebroRuntimeReleaseDigest {
    param([Parameter(Mandatory)][string]$ReleasePath)

    if (-not (Test-Path -LiteralPath $ReleasePath -PathType Container)) {
        throw "RUNTIME_RELEASE_NOT_FOUND:$ReleasePath"
    }

    $root = ([IO.Path]::GetFullPath($ReleasePath)).TrimEnd('\', '/')
    $files = @(Get-ChildItem -LiteralPath $root -File -Recurse | Sort-Object FullName)

    if ($files.Count -eq 0) {
        throw 'RUNTIME_RELEASE_EMPTY'
    }

    $material = @()
    foreach ($file in $files) {
        $relative = (
            $file.FullName.Substring($root.Length)
        ).TrimStart([char]'\', [char]'/').Replace('\', '/')

        $material += (
            '{0}|{1}|{2}' -f
            $relative,
            $file.Length,
            (Get-CerebroRuntimeReleaseFileSha256 -Path $file.FullName)
        )
    }

    Get-CerebroRuntimeReleaseSha256Text -Text ($material -join "`n")
}

function Write-CerebroRuntimeReleaseJson {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][object]$Value
    )

    $directory = Split-Path -Parent $Path
    [IO.Directory]::CreateDirectory($directory) | Out-Null

    $json = ($Value | ConvertTo-Json -Depth 32) + "`n"
    [IO.File]::WriteAllText(
        $Path,
        $json,
        [Text.UTF8Encoding]::new($false)
    )
}

function Assert-CerebroRuntimeReleaseSource {
    param(
        [Parameter(Mandatory)][string]$SourcePath,
        [Parameter(Mandatory)][string]$Remote,
        [Parameter(Mandatory)][string]$Branch
    )

    $source = [IO.Path]::GetFullPath($SourcePath)
    if (-not (Test-Path -LiteralPath $source -PathType Container)) {
        throw "RUNTIME_SOURCE_NOT_FOUND:$source"
    }

    $git = (Get-Command git -ErrorAction Stop | Select-Object -First 1).Source
    Push-Location -LiteralPath $source

    try {
        $root = [IO.Path]::GetFullPath(
            (Invoke-CerebroRuntimeReleaseGit -Git $git -Arguments @(
                'rev-parse','--show-toplevel'
            )).stdout
        )

        if (-not $root.Equals($source, [StringComparison]::OrdinalIgnoreCase)) {
            throw "RUNTIME_SOURCE_BINDING_MISMATCH:$root"
        }

        $currentBranch = (
            Invoke-CerebroRuntimeReleaseGit -Git $git -Arguments @(
                'branch','--show-current'
            )
        ).stdout

        if ($currentBranch -ne $Branch) {
            throw "RUNTIME_SOURCE_BRANCH_INVALID:$currentBranch"
        }

        $remoteUrl = (
            Invoke-CerebroRuntimeReleaseGit -Git $git -Arguments @(
                'remote','get-url',$Remote
            )
        ).stdout

        if ($remoteUrl -notmatch 'morgul-tech/Cerebro-Source-1\.0') {
            throw "RUNTIME_SOURCE_REMOTE_INVALID:$remoteUrl"
        }

        $status = (
            Invoke-CerebroRuntimeReleaseGit -Git $git -Arguments @(
                'status','--porcelain','--untracked-files=all'
            )
        ).stdout

        if (-not [string]::IsNullOrWhiteSpace($status)) {
            throw "RUNTIME_SOURCE_NOT_CLEAN:$status"
        }

        [void](Invoke-CerebroRuntimeReleaseGit -Git $git -Arguments @(
            'fetch','--no-tags',$Remote,$Branch
        ))

        $localCommit = (
            Invoke-CerebroRuntimeReleaseGit -Git $git -Arguments @(
                'rev-parse','HEAD'
            )
        ).stdout

        $remoteCommit = (
            Invoke-CerebroRuntimeReleaseGit -Git $git -Arguments @(
                'rev-parse',"refs/remotes/$Remote/$Branch"
            )
        ).stdout

        if ($localCommit -ne $remoteCommit) {
            throw (
                'RUNTIME_SOURCE_NOT_AUTHORITATIVE_HEAD:' +
                "LOCAL=${localCommit}:REMOTE=${remoteCommit}"
            )
        }

        $contractPath = Join-Path $source 'standards\runtime\runtime-0.1-contracts.yaml'
        if (-not (Test-Path -LiteralPath $contractPath -PathType Leaf)) {
            throw 'RUNTIME_CONTRACT_SET_MISSING'
        }

        $contract = [IO.File]::ReadAllText($contractPath)
        foreach ($token in @(
            'status: "LOCKED"',
            'patch_ref: "PATCH-002"',
            'implementation_patch_ref: "PATCH-003"',
            'ONE_INVOCATION_ONE_EVENT_ONE_RECEIPT'
        )) {
            if (-not $contract.Contains($token)) {
                throw "RUNTIME_CONTRACT_TOKEN_MISSING:$token"
            }
        }

        return $localCommit
    }
    finally {
        Pop-Location
    }
}

function New-CerebroRuntimeRelease0_1 {
    [CmdletBinding()]
    param(
        [string]$SourcePath = 'D:\Cerebro\Source\Cerebro_Source_v1.0',
        [string]$RunRoot = 'D:\Cerebro\Run',
        [string]$Remote = 'origin',
        [string]$Branch = 'main'
    )

    $source = [IO.Path]::GetFullPath($SourcePath)
    $run = [IO.Path]::GetFullPath($RunRoot)
    $sourceCommit = Assert-CerebroRuntimeReleaseSource `
        -SourcePath $source `
        -Remote $Remote `
        -Branch $Branch

    $releaseRoot = Join-Path $run 'releases'
    $profileRoot = Join-Path $run 'profiles'
    [IO.Directory]::CreateDirectory($releaseRoot) | Out-Null
    [IO.Directory]::CreateDirectory($profileRoot) | Out-Null

    $temporary = Join-Path $releaseRoot (
        '.runtime-0.1-build-' + [guid]::NewGuid().ToString('N')
    )
    [IO.Directory]::CreateDirectory($temporary) | Out-Null

    try {
        $relativeFiles = @(
            'cerebro.yaml',
            'mcp/identity.yaml',
            'mcp/authority.yaml',
            'mcp/architecture.yaml',
            'mcp/priorities.yaml',
            'mcp/activation.yaml',
            'standards/runtime/minimal-runtime-bootstrap.yaml',
            'standards/runtime/runtime-0.1-contracts.yaml',
            'standards/runtime/idea-object-pipeline.yaml',
            'tooling/builder/component.yaml',
            'tooling/builder/cerebro_runtime_release.ps1',
            'tooling/loader/component.yaml',
            'tooling/loader/cerebro_runtime.ps1',
            'tooling/loader/cerebro_runtime_host.ps1',
            'engines/dialog/component.yaml',
            'engines/context/component.yaml',
            'engines/collaboration/component.yaml',
            'engines/quality/component.yaml',
            'engines/presentation/component.yaml',
            'modules/terminology/component.yaml',
            'modules/core-rules/component.yaml',
            'modules/visual-language/component.yaml'
        )

        foreach ($relative in $relativeFiles) {
            $sourceFile = Join-Path $source ($relative -replace '/', '\')
            if (-not (Test-Path -LiteralPath $sourceFile -PathType Leaf)) {
                throw "RUNTIME_RELEASE_INPUT_MISSING:$relative"
            }

            $target = Join-Path $temporary ('source\' + ($relative -replace '/', '\'))
            [IO.Directory]::CreateDirectory((Split-Path -Parent $target)) | Out-Null
            Copy-Item -LiteralPath $sourceFile -Destination $target -Force
        }

        $identity = "github:morgul-tech/Cerebro-Source-1.0/$Branch@$sourceCommit`n"
        [IO.File]::WriteAllText(
            (Join-Path $temporary 'source-identity.txt'),
            $identity,
            [Text.UTF8Encoding]::new($false)
        )

        $bindings = [ordered]@{
            schema = 'cerebro-runtime-capability-bindings/v0.1'
            bindings = @(
                [ordered]@{
                    binding_id = 'BIND-RUNTIME-START'
                    capability_id = 'RUNTIME-START'
                    implementation_ref = 'builtin:echo'
                    input_contract = [ordered]@{
                        event_types = @('RUNTIME_START')
                        payload_required = $true
                    }
                    output_contract = [ordered]@{
                        result_type = 'ECHO_RESULT'
                        require_success = $true
                    }
                    allowed_side_effects = @()
                    authority_requirement = 'USER'
                    timeout_policy = [ordered]@{ milliseconds = 5000 }
                    exit_policy = [ordered]@{ success_states = @('SUCCESS') }
                    verification_policy = [ordered]@{ mode = 'exact-payload-echo' }
                },
                [ordered]@{
                    binding_id = 'BIND-RUNTIME-ECHO'
                    capability_id = 'RUNTIME-ECHO'
                    implementation_ref = 'builtin:echo'
                    input_contract = [ordered]@{
                        event_types = @('RUNTIME_ECHO')
                        payload_required = $true
                    }
                    output_contract = [ordered]@{
                        result_type = 'ECHO_RESULT'
                        require_success = $true
                    }
                    allowed_side_effects = @()
                    authority_requirement = 'USER'
                    timeout_policy = [ordered]@{ milliseconds = 5000 }
                    exit_policy = [ordered]@{ success_states = @('SUCCESS') }
                    verification_policy = [ordered]@{ mode = 'exact-payload-echo' }
                },
                [ordered]@{
                    binding_id = 'BIND-IDEA-CAPTURE'
                    capability_id = 'IDEA-CAPTURE'
                    implementation_ref = 'builtin:idea-capture'
                    input_contract = [ordered]@{
                        event_types = @('IDEA_CAPTURE')
                        payload_required = $true
                        required_payload_fields = @('content')
                    }
                    output_contract = [ordered]@{
                        result_type = 'IDEA_OBJECT_CAPTURED'
                        require_success = $true
                    }
                    allowed_side_effects = @('runtime-artifact:ideas')
                    authority_requirement = 'USER'
                    timeout_policy = [ordered]@{ milliseconds = 5000 }
                    exit_policy = [ordered]@{ success_states = @('SUCCESS') }
                    verification_policy = [ordered]@{ mode = 'idea-object-persisted' }
                },
                [ordered]@{
                    binding_id = 'BIND-RUNTIME-CONTROL-STOP'
                    capability_id = 'RUNTIME-CONTROL-STOP'
                    implementation_ref = 'builtin:control-stop'
                    input_contract = [ordered]@{
                        event_types = @('RUNTIME_CONTROL_STOP')
                        payload_required = $true
                    }
                    output_contract = [ordered]@{
                        result_type = 'CONTROL_STOP'
                        require_success = $false
                    }
                    allowed_side_effects = @()
                    authority_requirement = 'USER'
                    timeout_policy = [ordered]@{ milliseconds = 5000 }
                    exit_policy = [ordered]@{ success_states = @('CONTROL_STOP') }
                    verification_policy = [ordered]@{ mode = 'result-state-only' }
                }
            )
        }

        Write-CerebroRuntimeReleaseJson `
            -Path (Join-Path $temporary 'capability-bindings.json') `
            -Value $bindings

        $descriptor = [ordered]@{
            schema = 'cerebro-runtime-release/v0.1'
            runtime_version = '0.1'
            source = [ordered]@{
                repository = 'morgul-tech/Cerebro-Source-1.0'
                branch = $Branch
                commit = $sourceCommit
            }
            contract_set = 'CEREBRO-RUNTIME-0-1-CONTRACT-SET-001'
            purpose = 'Runtime 0.1 pinned deterministic release with declared capabilities'
        }

        Write-CerebroRuntimeReleaseJson `
            -Path (Join-Path $temporary 'release-descriptor.json') `
            -Value $descriptor

        $digest = Get-CerebroRuntimeReleaseDigest -ReleasePath $temporary
        $releaseName = (
            'runtime-0.1-' +
            $sourceCommit.Substring(0,12) + '-' +
            $digest.Substring(0,12)
        )
        $releasePath = Join-Path $releaseRoot $releaseName

        if (Test-Path -LiteralPath $releasePath -PathType Container) {
            $existingDigest = Get-CerebroRuntimeReleaseDigest -ReleasePath $releasePath
            if ($existingDigest -ne $digest) {
                throw "RUNTIME_RELEASE_COLLISION:$releasePath"
            }
            Remove-Item -LiteralPath $temporary -Recurse -Force
        }
        else {
            [IO.Directory]::Move($temporary, $releasePath)
        }

        $finalDigest = Get-CerebroRuntimeReleaseDigest -ReleasePath $releasePath
        if ($finalDigest -ne $digest) {
            throw 'RUNTIME_RELEASE_POST_BUILD_DIGEST_MISMATCH'
        }

        $profileId = 'runtime-0.1-main-' + $sourceCommit.Substring(0,12)
        $profilePath = Join-Path $profileRoot ($profileId + '.json')
        $profile = [ordered]@{
            profile_id = $profileId
            runtime_version = '0.1'
            release_ref = "sha256:$finalDigest"
            entrypoint = 'source/tooling/loader/cerebro_runtime.ps1'
            supported_event_types = @(
                'RUNTIME_START',
                'RUNTIME_ECHO',
                'IDEA_CAPTURE',
                'RUNTIME_CONTROL_STOP'
            )
            capability_binding_refs = @(
                'BIND-RUNTIME-START',
                'BIND-RUNTIME-ECHO',
                'BIND-IDEA-CAPTURE',
                'BIND-RUNTIME-CONTROL-STOP'
            )
            projection_requirements = @(
                'IDENTITY','BOOT','GOVERNANCE',
                'SESSION','EXECUTION','EVIDENCE'
            )
            state_contract_ref = 'RUNTIME-STATE-TRANSITION-TABLE-001'
            receipt_contract_ref = 'RUNTIME-RECEIPT-CONTRACT-001'
            failure_policy_ref = 'FAILURE-LEDGER-CONTRACT-001'
        }

        Write-CerebroRuntimeReleaseJson -Path $profilePath -Value $profile

        [pscustomobject]@{
            state = 'PINNED_RELEASE_VERIFIED'
            source_commit = $sourceCommit
            release_path = $releasePath
            release_sha256 = $finalDigest
            profile_id = $profileId
            profile_path = $profilePath
        }
    }
    finally {
        if (Test-Path -LiteralPath $temporary -PathType Container) {
            Remove-Item -LiteralPath $temporary -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}
