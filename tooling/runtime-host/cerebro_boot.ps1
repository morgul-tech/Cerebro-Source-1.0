Set-StrictMode -Version Latest

function Get-CerebroBootSha256Text {
    param(
        [Parameter(Mandatory)]
        [string]$Text
    )

    $sha = [Security.Cryptography.SHA256]::Create()

    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($Text)

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

function Get-CerebroBootExecutionValue {
    param(
        [Parameter(Mandatory)]
        [string]$Roadmap,

        [Parameter(Mandatory)]
        [ValidateSet('current', 'next')]
        [string]$Section,

        [Parameter(Mandatory)]
        [string]$Name
    )

    $lines = $Roadmap -split "\r?\n"
    $executionFound = $false
    $sectionFound = $false
    $sectionLine = ('    {0}:' -f $Section)
    $namePrefix = ('      {0}:' -f $Name)

    foreach ($line in $lines) {
        if (-not $executionFound) {
            if ($line -eq '  execution:') {
                $executionFound = $true
            }
            continue
        }

        if (-not $sectionFound) {
            if ($line -eq $sectionLine) {
                $sectionFound = $true
                continue
            }
            if ($line -match '^\s{0,2}\S') {
                break
            }
            continue
        }

        if ($line.StartsWith($namePrefix)) {
            $raw = $line.Substring($namePrefix.Length).Trim()
            if ($raw -match '^"(?<value>[^"]*)"$') {
                return $Matches['value']
            }
            $comment = $raw.IndexOf('#')
            if ($comment -ge 0) {
                $raw = $raw.Substring(0, $comment).Trim()
            }
            if (-not [string]::IsNullOrWhiteSpace($raw)) {
                return $raw
            }
        }

        if ($line -match '^\s{0,4}\S') {
            break
        }
    }

    if (-not $sectionFound) {
        throw "BOOT_EXECUTION_SECTION_NOT_FOUND:$Section"
    }
    throw "BOOT_EXECUTION_VALUE_NOT_FOUND:$Section`:$Name"
}

function Test-CerebroBootRequiredTokens {
    param(
        [Parameter(Mandatory)]
        [string]$Path,

        [Parameter(Mandatory)]
        [string[]]$Tokens,

        [Parameter(Mandatory)]
        [string]$FailurePrefix
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$FailurePrefix`_FILE_MISSING:$Path"
    }

    $content = [IO.File]::ReadAllText($Path)

    foreach ($token in $Tokens) {
        if (-not $content.Contains($token)) {
            throw "$FailurePrefix`_TOKEN_MISSING:$token"
        }
    }

    return $content
}

function Write-CerebroBootRuntimeState {
    param(
        [Parameter(Mandatory)]
        [string]$Path,

        [Parameter(Mandatory)]
        [object]$RuntimeState
    )

    $directory = Split-Path -Parent $Path

    [IO.Directory]::CreateDirectory($directory) |
        Out-Null

    $temporary = Join-Path `
        $directory `
        (
            '.CEREBRO_RUNTIME_STATE_v1.tmp-' +
            [guid]::NewGuid().ToString('N') +
            '.json'
        )

    try {
        $json = $RuntimeState |
            ConvertTo-Json -Depth 16

        [IO.File]::WriteAllText(
            $temporary,
            $json + "`n",
            [Text.UTF8Encoding]::new($false)
        )

        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            $backup = (
                $Path +
                '.backup-' +
                (Get-Date -Format 'yyyyMMdd-HHmmss')
            )

            [IO.File]::Replace(
                $temporary,
                $Path,
                $backup,
                $true
            )
        }
        else {
            [IO.File]::Move(
                $temporary,
                $Path
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
}

function Invoke-CerebroBootCore {
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
            'D:\Cerebro\Run\active\CEREBRO_BOOTSTRAP_STATE_v1.json',

        [switch]$SkipHandoff
    )

    $ErrorActionPreference = 'Stop'

    $bootState = 'IDLE'
    $failureStage = 'NONE'
    $failureCode = 'NONE'

    $runtimeProfile =
        'cerebro-controlled-session/v0.1'

    $bootEngineHash = $null
    $localCommit = $null
    $authoritativeCommit = $null
    $alignmentState = $null
    $handoffState = 'NONE'
    $handoffId = $null
    $resumeReceipt = $null
    $currentPatch = $null
    $nextPatch = $null
    $canonicalCommand = $null

    try {
        $bootState = 'FETCHING'
        $failureStage = 'BOOTENGINE_FETCH'

        try {
            $bootResponse = Invoke-WebRequest `
                -Uri $BootEngineUrl `
                -UseBasicParsing `
                -TimeoutSec 30
        }
        catch {
            throw (
                'BOOTENGINE_FETCH_FAILED:' +
                $_.Exception.Message
            )
        }

        if ($bootResponse.StatusCode -ne 200) {
            throw (
                'BOOTENGINE_HTTP_FAILURE:' +
                $bootResponse.StatusCode
            )
        }

        $bootEngine = [string]$bootResponse.Content

        if ([string]::IsNullOrWhiteSpace($bootEngine)) {
            throw 'BOOTENGINE_EMPTY'
        }

        foreach ($token in @(
            'BOOTSTRAP ENGINE',
            'Configuration := IMMUTABLE',
            'CurrentMaster.Repository := morgul-tech/Cerebro-Source-1.0',
            'CurrentMaster.Branch := main',
            'CurrentMaster.Path := cerebro.yaml',
            'Command.NaturalLanguageAlias := boot cerebro',
            'Authority.First := github:morgul-tech/Cerebro-Source-1.0/main/cerebro.yaml',
            'FilenameMarker.CURRENT.Authority := NONE',
            'StaleDerivedState.Action := REJECT_INPUT_CONTINUE_CURRENT_SOURCE',
            'OperationalClaim.Requires := ACTIVE_CONTROL_TRANSFERRED_RECEIPT_AT_CURRENT_SOURCE_COMMIT',
            'IDLE',
            'FETCHING',
            'VERIFYING',
            'ANALYZING',
            'ACTIVATING',
            'COMPLETE',
            'FAILED',
            'UndefinedTransition',
            'FORBIDDEN'
        )) {
            if (-not $bootEngine.Contains($token)) {
                throw "BOOTENGINE_TOKEN_MISSING:$token"
            }
        }

        $bootEngineHash =
            Get-CerebroBootSha256Text $bootEngine

        $failureStage = 'AUTHORITATIVE_SOURCE_FETCH'

        if (
            -not (
                Test-Path `
                    -LiteralPath $WorkingSourcePath `
                    -PathType Container
            )
        ) {
            throw "WORKING_SOURCE_NOT_FOUND:$WorkingSourcePath"
        }

        Push-Location -LiteralPath $WorkingSourcePath

        try {
            if (
                (git rev-parse --is-inside-work-tree).Trim() -ne
                'true'
            ) {
                throw 'WORKING_SOURCE_NOT_GIT_REPOSITORY'
            }

            $bootState = 'VERIFYING'
            $failureStage = 'WORKING_SOURCE_VERIFY'

            $currentBranch = (
                git branch --show-current
            ).Trim()

            if ([string]::IsNullOrWhiteSpace($currentBranch)) {
                throw 'WORKING_SOURCE_DETACHED_HEAD'
            }

            if ($currentBranch -ne $Branch) {
                throw (
                    "WORKING_SOURCE_BRANCH_INVALID:" +
                    "EXPECTED=$Branch`:ACTUAL=$currentBranch"
                )
            }

            $status = @(
                git status --porcelain --untracked-files=all
            )

            if ($status.Count -ne 0) {
                throw (
                    'WORKING_SOURCE_NOT_CLEAN:' +
                    ($status -join '|')
                )
            }

            git remote get-url $Remote 2>$null |
                Out-Null

            if ($LASTEXITCODE -ne 0) {
                throw "AUTHORITATIVE_REMOTE_MISSING:$Remote"
            }

            $remoteUrl = (
                git remote get-url $Remote
            ).Trim()

            if (
                $remoteUrl -notmatch
                'morgul-tech/Cerebro-Source-1\.0'
            ) {
                throw "AUTHORITATIVE_REMOTE_INVALID:$remoteUrl"
            }

            git fetch `
                $Remote `
                $Branch `
                --prune

            if ($LASTEXITCODE -ne 0) {
                throw 'AUTHORITATIVE_SOURCE_FETCH_FAILED'
            }

            $authoritativeRef = "$Remote/$Branch"

            $localCommit = (
                git rev-parse HEAD
            ).Trim()

            $authoritativeCommit = (
                git rev-parse $authoritativeRef
            ).Trim()

            if ($localCommit -eq $authoritativeCommit) {
                $alignmentState = 'ALIGNED'
            }
            else {
                git merge-base `
                    --is-ancestor `
                    $localCommit `
                    $authoritativeCommit

                $localIsAncestor = (
                    $LASTEXITCODE -eq 0
                )

                git merge-base `
                    --is-ancestor `
                    $authoritativeCommit `
                    $localCommit

                $remoteIsAncestor = (
                    $LASTEXITCODE -eq 0
                )

                if (
                    $localIsAncestor -and
                    -not $remoteIsAncestor
                ) {
                    $failureStage =
                        'WORKING_SOURCE_FAST_FORWARD'

                    git merge `
                        --ff-only `
                        $authoritativeRef

                    if ($LASTEXITCODE -ne 0) {
                        throw 'WORKING_SOURCE_FAST_FORWARD_FAILED'
                    }

                    $localCommit = (
                        git rev-parse HEAD
                    ).Trim()

                    if (
                        $localCommit -ne
                        $authoritativeCommit
                    ) {
                        throw 'WORKING_SOURCE_FAST_FORWARD_INCOMPLETE'
                    }

                    $alignmentState =
                        'FAST_FORWARDED_AND_ALIGNED'
                }
                elseif (
                    $remoteIsAncestor -and
                    -not $localIsAncestor
                ) {
                    throw (
                        'WORKING_SOURCE_LOCAL_AHEAD:' +
                        "LOCAL=${localCommit}:" +
                        "AUTHORITATIVE=$authoritativeCommit"
                    )
                }
                else {
                    throw (
                        'WORKING_SOURCE_DIVERGED:' +
                        "LOCAL=${localCommit}:" +
                        "AUTHORITATIVE=$authoritativeCommit"
                    )
                }
            }

            $postAlignmentStatus = @(
                git status --porcelain --untracked-files=all
            )

            if ($postAlignmentStatus.Count -ne 0) {
                throw 'WORKING_SOURCE_NOT_CLEAN_AFTER_ALIGNMENT'
            }

            $bootState = 'ANALYZING'
            $failureStage = 'SOURCE_COMPONENT_VALIDATION'

            $requiredRelativePaths = @(
                'cerebro.yaml',
                'mcp/activation.yaml',
                'standards/runtime/minimal-runtime-bootstrap.yaml',
                'standards/runtime/handboot.yaml',
                'standards/runtime/handboot-bootstrap-state-schema.yaml',
                'standards/session-handoff.yaml',
                'standards/session-handoff-schema.yaml',
                'engines/context/working-context.yaml',
                'engines/project/roadmap.yaml',
                'tooling/builder/templates/pshell/Cerebro.Tools.psd1',
                'tooling/builder/templates/pshell/Cerebro.Tools.psm1',
                'tooling/builder/templates/pshell/cerebro_handoff.ps1',
                'tooling/runtime-host/cerebro_resume.ps1',
                'tooling/runtime-host/cerebro_boot.ps1',
                'tooling/runtime-host/cerebro_machine_proof.ps1',
                'tooling/validator/checks.yaml'
            )

            foreach ($relativePath in $requiredRelativePaths) {
                $absolutePath = Join-Path `
                    $WorkingSourcePath `
                    ($relativePath -replace '/', '\')

                if (
                    -not (
                        Test-Path `
                            -LiteralPath $absolutePath `
                            -PathType Leaf
                    )
                ) {
                    throw "REQUIRED_COMPONENT_MISSING:$relativePath"
                }
            }

            $manifestPath = Join-Path `
                $WorkingSourcePath `
                'cerebro.yaml'

            $manifest = Test-CerebroBootRequiredTokens `
                -Path $manifestPath `
                -Tokens @(
                    'schema: cerebro-manifest/v1',
                    'cerebro:',
                    'source:',
                    'mcp:',
                    'engines:',
                    'modules:',
                    'standards:',
                    'tooling:',
                    'governance:',
                    'integrity:',
                    'source_status:'
                ) `
                -FailurePrefix 'MANIFEST_CONTRACT'

            $activationPath = Join-Path `
                $WorkingSourcePath `
                'mcp\activation.yaml'

            Test-CerebroBootRequiredTokens `
                -Path $activationPath `
                -Tokens @(
                    'external_protocol: Oppstartsprotokoll',
                    'entrypoint: cerebro.yaml',
                    'fetch_manifest',
                    'verify_source',
                    'verify_release',
                    'load_mcp',
                    'load_required_engines',
                    'load_required_modules',
                    'initialize_runtime',
                    'activate'
                ) `
                -FailurePrefix 'ACTIVATION_CONTRACT' |
                Out-Null

            $bootstrapPath = Join-Path `
                $WorkingSourcePath `
                'standards\runtime\minimal-runtime-bootstrap.yaml'

            Test-CerebroBootRequiredTokens `
                -Path $bootstrapPath `
                -Tokens @(
                    'persistent_authority: "Cerebro Source"',
                    'boot_authority: "orchestration_only"',
                    'resolve_authoritative_source',
                    'validate_required_contracts',
                    'construct_derived_runtime_state',
                    'activate_runtime',
                    'transfer_control'
                ) `
                -FailurePrefix 'BOOTSTRAP_CONTRACT' |
                Out-Null

            $roadmapPath = Join-Path `
                $WorkingSourcePath `
                'engines\project\roadmap.yaml'

            $roadmap = [IO.File]::ReadAllText(
                $roadmapPath
            )

            $currentPatch =
                Get-CerebroBootExecutionValue `
                    -Roadmap $roadmap `
                    -Section current `
                    -Name patch_ref

            $nextPatch =
                Get-CerebroBootExecutionValue `
                    -Roadmap $roadmap `
                    -Section next `
                    -Name patch_ref

            $canonicalCommand =
                Get-CerebroBootExecutionValue `
                    -Roadmap $roadmap `
                    -Section current `
                    -Name canonical_command

            $bootState = 'ACTIVATING'
            $failureStage = 'RUNTIME_CONSTRUCTION'

            $createdAt =
                [DateTime]::UtcNow.ToString('o')

            $runtimeIdMaterial = (
                '{0}|{1}|{2}|{3}' -f
                $runtimeProfile,
                $localCommit,
                $bootEngineHash,
                $currentPatch
            )

            $runtimeId = (
                'RUNTIME-' +
                (
                    Get-CerebroBootSha256Text `
                        $runtimeIdMaterial
                ).Substring(0, 24)
            )

            $runtimeState = [ordered]@{
                schema =
                    'cerebro-handboot-runtime-state/v0.1'

                runtime = [ordered]@{
                    runtime_id = $runtimeId
                    profile = $runtimeProfile
                    created_at_utc = $createdAt
                    activated_at_utc = $null
                    state = 'CONSTRUCTED_INACTIVE'
                    operational = $false

                    authority = [ordered]@{
                        persistent_authority =
                            'github:morgul-tech/Cerebro-Source-1.0/main'

                        boot_authority =
                            'external_orchestration_only'

                        runtime_authority =
                            'derived_non_authoritative'

                        handoff_authority =
                            'derived_non_authoritative'
                    }

                    bootengine = [ordered]@{
                        source = $BootEngineUrl
                        sha256 = $bootEngineHash
                        validation_state = 'VERIFIED'
                    }

                    working_source = [ordered]@{
                        path = $WorkingSourcePath
                        branch = $currentBranch
                        commit = $localCommit
                        worktree_state = 'CLEAN'
                        alignment_state = $alignmentState
                    }

                    authoritative_source = [ordered]@{
                        repository =
                            'morgul-tech/Cerebro-Source-1.0'

                        remote = $Remote
                        branch = $Branch
                        commit = $authoritativeCommit
                        fetch_state = 'FETCHED'
                    }

                    source_validation = [ordered]@{
                        manifest_state = 'VERIFIED'
                        activation_contract_state = 'VERIFIED'
                        bootstrap_contract_state = 'VERIFIED'
                        required_components_state = 'VERIFIED'
                    }

                    components = [ordered]@{
                        required = $requiredRelativePaths
                        loaded_profile =
                            'controlled-session-minimum'
                    }

                    execution = [ordered]@{
                        current_patch = $currentPatch
                        next_patch = $nextPatch
                        canonical_command = $canonicalCommand
                    }

                    handoff = [ordered]@{
                        requested = (
                            -not $SkipHandoff
                        )

                        path = $HandoffPath
                        state = 'PENDING'
                        handoff_id = $null
                        resume_receipt = $null
                    }

                    control = [ordered]@{
                        runtime_constructed = $true
                        runtime_activated = $false
                        control_transferred = $false
                        failure_stage = 'NONE'
                        failure_code = 'NONE'
                    }

                    receipt = [ordered]@{
                        algorithm = 'sha256'
                        value = $null
                        material_version =
                            'handboot-receipt/v0.1'
                    }
                }
            }

            $failureStage = 'RUNTIME_ACTIVATION'

            $runtimeState.runtime.state = 'ACTIVE'
            $runtimeState.runtime.activated_at_utc =
                [DateTime]::UtcNow.ToString('o')

            $runtimeState.runtime.control.runtime_activated =
                $true

            Write-CerebroBootRuntimeState `
                -Path $RuntimeStatePath `
                -RuntimeState $runtimeState

            $failureStage = 'HANDOFF_PROCESSING'

            if ($SkipHandoff) {
                $handoffState = 'SKIPPED_EXPLICITLY'
            }
            elseif (
                Test-Path `
                    -LiteralPath $HandoffPath `
                    -PathType Leaf
            ) {
                $resumeScriptPath = Join-Path `
                    $WorkingSourcePath `
                    'tooling\runtime-host\cerebro_resume.ps1'

                . $resumeScriptPath

                $resumeResult = Invoke-CerebroResumeCore `
                    -HandoffPath $HandoffPath `
                    -RepoPath $WorkingSourcePath

                if (
                    $resumeResult.state -ne
                    'SUCCESS_READY'
                ) {
                    throw (
                        'HANDOFF_RESUME_STATE_INVALID:' +
                        $resumeResult.state
                    )
                }

                $handoffState = 'RESUMED'
                $handoffId = $resumeResult.handoff_id
                $resumeReceipt = $resumeResult.receipt

                $currentPatch =
                    $resumeResult.current_patch

                $nextPatch =
                    $resumeResult.next_patch

                $canonicalCommand =
                    $resumeResult.canonical_command
            }
            else {
                $handoffState = 'NONE'
            }

            $failureStage = 'CONTROL_TRANSFER'

            $receiptMaterial = (
                '{0}|{1}|{2}|{3}|{4}|{5}|{6}|{7}' -f
                'bootCerebro',
                $bootEngineHash,
                $localCommit,
                $runtimeProfile,
                'ACTIVE_CONTROL_TRANSFERRED',
                $handoffState,
                $currentPatch,
                $canonicalCommand
            )

            $receipt =
                Get-CerebroBootSha256Text `
                    $receiptMaterial

            $runtimeState.runtime.state =
                'ACTIVE_CONTROL_TRANSFERRED'

            $runtimeState.runtime.operational =
                $true

            $runtimeState.runtime.execution.current_patch =
                $currentPatch

            $runtimeState.runtime.execution.next_patch =
                $nextPatch

            $runtimeState.runtime.execution.canonical_command =
                $canonicalCommand

            $runtimeState.runtime.handoff.state =
                $handoffState

            $runtimeState.runtime.handoff.handoff_id =
                $handoffId

            $runtimeState.runtime.handoff.resume_receipt =
                $resumeReceipt

            $runtimeState.runtime.control.control_transferred =
                $true

            $runtimeState.runtime.receipt.value =
                $receipt

            Write-CerebroBootRuntimeState `
                -Path $RuntimeStatePath `
                -RuntimeState $runtimeState

            $bootState = 'COMPLETE'
            $failureStage = 'NONE'
            $failureCode = 'NONE'

            Write-Host ''
            Write-Host '======================================================'
            Write-Host 'CEREBRO HANDBOOT RESULT'
            Write-Host '======================================================'
            Write-Host 'command:                bootCerebro'
            Write-Host 'state:                  ACTIVE_CONTROL_TRANSFERRED'
            Write-Host 'bootengine:             VERIFIED'
            Write-Host 'authoritative_source:   VERIFIED'
            Write-Host (
                'working_source:         {0}' -f
                $alignmentState
            )
            Write-Host (
                'source_commit:          {0}' -f
                $localCommit
            )
            Write-Host 'manifest:               VERIFIED'
            Write-Host 'components:             VERIFIED'
            Write-Host 'runtime:                ACTIVE'
            Write-Host (
                'handoff:                {0}' -f
                $handoffState
            )
            Write-Host (
                'current_patch:          {0}' -f
                $currentPatch
            )
            Write-Host (
                'canonical_command:      {0}' -f
                $canonicalCommand
            )
            Write-Host 'operational:            TRUE'
            Write-Host (
                'runtime_state_path:     {0}' -f
                $RuntimeStatePath
            )
            Write-Host (
                'receipt:                {0}' -f
                $receipt
            )
            Write-Host '======================================================'

            Write-Host (
                (
                    'CEREBRO_BOOT COMMAND=bootCerebro ' +
                    'STATE=ACTIVE_CONTROL_TRANSFERRED ' +
                    'BOOTENGINE=VERIFIED ' +
                    'AUTHORITATIVE_SOURCE=VERIFIED ' +
                    'WORKING_SOURCE={0} ' +
                    'SOURCE_COMMIT={1} ' +
                    'MANIFEST=VERIFIED COMPONENTS=VERIFIED ' +
                    'RUNTIME=ACTIVE HANDOFF={2} ' +
                    'CURRENT_PATCH={3} OPERATIONAL=TRUE ' +
                    'RECEIPT={4}'
                ) -f
                $alignmentState,
                $localCommit,
                $handoffState,
                $currentPatch,
                $receipt
            )

            return [pscustomobject]@{
                command = 'bootCerebro'
                state = 'ACTIVE_CONTROL_TRANSFERRED'
                bootengine = 'VERIFIED'
                authoritative_source = 'VERIFIED'
                working_source = $alignmentState
                source_commit = $localCommit
                manifest = 'VERIFIED'
                components = 'VERIFIED'
                runtime = 'ACTIVE'
                handoff = $handoffState
                handoff_id = $handoffId
                resume_receipt = $resumeReceipt
                current_patch = $currentPatch
                next_patch = $nextPatch
                canonical_command = $canonicalCommand
                operational = $true
                runtime_state_path = $RuntimeStatePath
                receipt = $receipt
            }
        }
        finally {
            Pop-Location
        }
    }
    catch {
        $failureCode = $_.Exception.Message
        $bootState = 'FAILED'

        $failedRuntimeState = [ordered]@{
            schema =
                'cerebro-handboot-runtime-state/v0.1'

            runtime = [ordered]@{
                runtime_id = $null
                profile = $runtimeProfile
                created_at_utc =
                    [DateTime]::UtcNow.ToString('o')

                activated_at_utc = $null
                state = 'INACTIVE_CONTROL_RETAINED'
                operational = $false

                authority = [ordered]@{
                    persistent_authority =
                        'github:morgul-tech/Cerebro-Source-1.0/main'

                    boot_authority =
                        'external_orchestration_only'

                    runtime_authority =
                        'derived_non_authoritative'

                    handoff_authority =
                        'derived_non_authoritative'
                }

                bootengine = [ordered]@{
                    source = $BootEngineUrl
                    sha256 = $bootEngineHash
                    validation_state = $(
                        if ($bootEngineHash) {
                            'VERIFIED'
                        }
                        else {
                            'FAILED'
                        }
                    )
                }

                working_source = [ordered]@{
                    path = $WorkingSourcePath
                    branch = $Branch
                    commit = $localCommit
                    worktree_state = 'UNKNOWN'
                    alignment_state = $alignmentState
                }

                authoritative_source = [ordered]@{
                    repository =
                        'morgul-tech/Cerebro-Source-1.0'

                    remote = $Remote
                    branch = $Branch
                    commit = $authoritativeCommit
                    fetch_state = 'UNKNOWN'
                }

                source_validation = [ordered]@{
                    manifest_state = 'UNKNOWN'
                    activation_contract_state = 'UNKNOWN'
                    bootstrap_contract_state = 'UNKNOWN'
                    required_components_state = 'UNKNOWN'
                }

                components = [ordered]@{
                    required = @()
                    loaded_profile = $null
                }

                execution = [ordered]@{
                    current_patch = $currentPatch
                    next_patch = $nextPatch
                    canonical_command = $canonicalCommand
                }

                handoff = [ordered]@{
                    requested = (-not $SkipHandoff)
                    path = $HandoffPath
                    state = 'FAILED'
                    handoff_id = $handoffId
                    resume_receipt = $resumeReceipt
                }

                control = [ordered]@{
                    runtime_constructed = $false
                    runtime_activated = $false
                    control_transferred = $false
                    failure_stage = $failureStage
                    failure_code = $failureCode
                }

                receipt = [ordered]@{
                    algorithm = 'sha256'
                    value = $null
                    material_version =
                        'handboot-receipt/v0.1'
                }
            }
        }

        try {
            Write-CerebroBootRuntimeState `
                -Path $RuntimeStatePath `
                -RuntimeState $failedRuntimeState
        }
        catch {
            # Preserve original boot failure.
        }

        Write-Host (
            (
                'CEREBRO_BOOT COMMAND=bootCerebro ' +
                'STATE=INACTIVE_CONTROL_RETAINED ' +
                'FAILURE_STAGE={0} FAILURE_CODE={1} ' +
                'OPERATIONAL=FALSE'
            ) -f
            ($failureStage -replace '\s+', '_'),
            ($failureCode -replace '\s+', '_')
        )

        throw
    }
}
