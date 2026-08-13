param(
    [switch]$SelfTest,

    [ValidateSet('select', 'status', 'explain')]
    [string]$CliCommand,

    [string]$CliProfile,

    [ValidateSet('replace', 'create', 'delete')]
    [string[]]$CliOperations = @(),

    [switch]$CliDirectWorkspaceAccess,

    [string]$CliWorkingSourcePath =
        'D:\Cerebro\Source\Cerebro_Source_v1.0',

    [string]$CliStatePath =
        'D:\Cerebro\Run\State\Active\CEREBRO_DELIVERY_SELECTION.json',

    [string]$CliHistoryRoot =
        'D:\Cerebro\Run\Operations\Delivery\selections'
)

Set-StrictMode -Version 2.0

$script:CerebroDeliverySelectionSchema = 'cerebro-delivery-selection/v0.3'
$script:CerebroDeliveryLegacySelectionSchemas = @(
    'cerebro-delivery-selection/v0.1',
    'cerebro-delivery-selection/v0.2'
)
$script:CerebroDeliverySelectorVersion = '0.4.0'
$script:CerebroDeliveryProfiles = @(
    'LIMITED',
    'STANDARD',
    'FULL'
)
$script:CerebroDeliveryProfileAliases = @{
    STANDARD_A = 'LIMITED'
    STANDARD_B = 'STANDARD'
    STANDARD_C = 'FULL'
}

function ConvertTo-CerebroCanonicalDeliveryProfile {
    param(
        [Parameter(Mandatory)]
        [string]$Profile
    )

    $normalized = $Profile.ToUpperInvariant()

    if ($script:CerebroDeliveryProfileAliases.ContainsKey($normalized)) {
        return [string]$script:CerebroDeliveryProfileAliases[$normalized]
    }

    return $normalized
}

function Get-CerebroDeliveryProfileControls {
    param(
        [Parameter(Mandatory)]
        [string]$Profile
    )

    switch (ConvertTo-CerebroCanonicalDeliveryProfile -Profile $Profile) {
        'LIMITED' {
            return [ordered]@{
                execution_owner = 'USER'
                agent_local_access = 'PROHIBITED'
                access_request_budget = 0
                artifact_format = 'FILES'
            }
        }
        'STANDARD' {
            return [ordered]@{
                execution_owner = 'USER_LOCAL_RUNNER'
                agent_local_access = 'PROHIBITED'
                access_request_budget = 0
                artifact_format = 'PAYLOAD_PLUS_INSTALLER'
            }
        }
        'FULL' {
            return [ordered]@{
                execution_owner = 'AGENT_CONTROLLED'
                agent_local_access = 'EXPLICIT_GRANT_REQUIRED'
                access_request_budget = 1
                artifact_format = 'CONTROLLED_TRANSACTION'
            }
        }
        default {
            throw 'CEREBRO_DELIVERY_PROFILE_UNKNOWN'
        }
    }
}

function Write-CerebroDeliveryJsonAtomic {
    param(
        [Parameter(Mandatory)]
        [string]$Path,

        [Parameter(Mandatory)]
        [object]$Value
    )

    $directory = Split-Path -Parent $Path
    [IO.Directory]::CreateDirectory($directory) | Out-Null
    $temporary = Join-Path $directory (
        '.' + [IO.Path]::GetFileName($Path) +
        '.tmp-' + [guid]::NewGuid().ToString('N')
    )
    $utf8 = New-Object Text.UTF8Encoding($false)

    try {
        $json = ($Value | ConvertTo-Json -Depth 32) + "`n"
        [IO.File]::WriteAllText($temporary, $json, $utf8)

        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            $replaceBackup = Join-Path $directory (
                '.' + [IO.Path]::GetFileName($Path) +
                '.replace-' + [guid]::NewGuid().ToString('N') + '.bak'
            )
            try {
                [IO.File]::Replace(
                    $temporary,
                    $Path,
                    $replaceBackup,
                    $true
                )
            }
            finally {
                if (Test-Path -LiteralPath $replaceBackup) {
                    Remove-Item -LiteralPath $replaceBackup -Force `
                        -ErrorAction SilentlyContinue
                }
            }
        }
        else {
            [IO.File]::Move($temporary, $Path)
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

function Get-CerebroDeliveryTextSha256 {
    param(
        [Parameter(Mandatory)]
        [string]$Text
    )

    $bytes = [Text.Encoding]::UTF8.GetBytes($Text)
    $sha = [Security.Cryptography.SHA256]::Create()

    try {
        return ([BitConverter]::ToString(
            $sha.ComputeHash($bytes)
        )).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
}

function Invoke-CerebroDeliveryNative {
    param(
        [Parameter(Mandatory)]
        [string]$FilePath,

        [Parameter(Mandatory)]
        [string]$WorkingDirectory,

        [Parameter(Mandatory)]
        [string]$Arguments
    )

    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = $FilePath
    $start.WorkingDirectory = $WorkingDirectory
    $start.Arguments = $Arguments
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true

    $process = New-Object Diagnostics.Process
    $process.StartInfo = $start

    try {
        if (-not $process.Start()) {
            throw 'CEREBRO_DELIVERY_NATIVE_PROCESS_NOT_STARTED'
        }

        $stdout = $process.StandardOutput.ReadToEnd()
        $stderr = $process.StandardError.ReadToEnd()
        $process.WaitForExit()

        return [pscustomobject]@{
            process_started = $true
            exit_code = [int]$process.ExitCode
            stdout = $stdout.Trim()
            stderr = $stderr.Trim()
            timed_out = $false
        }
    }
    finally {
        $process.Dispose()
    }
}

function Resolve-CerebroDeliveryPythonRunner {
    foreach ($name in @('python.exe', 'python')) {
        try {
            $command = Get-Command $name -ErrorAction Stop |
                Select-Object -First 1
            if (-not [string]::IsNullOrWhiteSpace($command.Source)) {
                return [pscustomobject]@{
                    executable = $command.Source
                    prefix_arguments = @('-B')
                }
            }
        }
        catch {}
    }
    foreach ($name in @('py.exe', 'py')) {
        try {
            $command = Get-Command $name -ErrorAction Stop |
                Select-Object -First 1
            if (-not [string]::IsNullOrWhiteSpace($command.Source)) {
                return [pscustomobject]@{
                    executable = $command.Source
                    prefix_arguments = @('-3', '-B')
                }
            }
        }
        catch {}
    }
    throw 'CEREBRO_DELIVERY_MCP_CONTROL_PYTHON_NOT_FOUND'
}

function Invoke-CerebroDeliveryNativeArguments {
    param(
        [Parameter(Mandatory)]
        [string]$Executable,

        [string[]]$ArgumentList = @()
    )

    $stdoutPath = [IO.Path]::GetTempFileName()
    $stderrPath = [IO.Path]::GetTempFileName()
    $previousErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $Executable @ArgumentList 1> $stdoutPath 2> $stderrPath
        $nativeExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorAction
    }
    try {
        return [pscustomobject]@{
            exit_code = [int]$nativeExitCode
            stdout = [IO.File]::ReadAllText($stdoutPath).Trim()
            stderr = [IO.File]::ReadAllText($stderrPath).Trim()
        }
    }
    finally {
        Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force `
            -ErrorAction SilentlyContinue
    }
}

function Get-CerebroDeliverySourceIdentity {
    param(
        [Parameter(Mandatory)]
        [string]$WorkingSourcePath,

        [string]$ExpectedRepository =
            'morgul-tech/Cerebro-Source-1.0',

        [string]$ExpectedBranch = 'main'
    )

    $source = [IO.Path]::GetFullPath($WorkingSourcePath)

    if (
        -not (
            Test-Path `
                -LiteralPath (Join-Path $source 'cerebro.yaml') `
                -PathType Leaf
        )
    ) {
        throw 'CEREBRO_DELIVERY_SOURCE_NOT_FOUND'
    }

    $git = (
        Get-Command git.exe, git `
            -ErrorAction SilentlyContinue |
            Select-Object -First 1
    )

    if ($null -eq $git) {
        throw 'CEREBRO_DELIVERY_GIT_NOT_FOUND'
    }

    $head = Invoke-CerebroDeliveryNative `
        -FilePath $git.Source `
        -WorkingDirectory $source `
        -Arguments 'rev-parse HEAD'

    $branch = Invoke-CerebroDeliveryNative `
        -FilePath $git.Source `
        -WorkingDirectory $source `
        -Arguments 'branch --show-current'

    $remote = Invoke-CerebroDeliveryNative `
        -FilePath $git.Source `
        -WorkingDirectory $source `
        -Arguments 'remote get-url origin'

    foreach ($result in @($head, $branch, $remote)) {
        if ($result.exit_code -ne 0) {
            throw (
                'CEREBRO_DELIVERY_GIT_QUERY_FAILED:' +
                $result.exit_code + ':' + $result.stderr
            )
        }
    }

    $commit = [string]$head.stdout
    $actualBranch = [string]$branch.stdout
    $actualRemote = ([string]$remote.stdout).ToLowerInvariant()
    $normalizedExpected = $ExpectedRepository.ToLowerInvariant()
    $normalizedRemote = $actualRemote.TrimEnd('/').Replace('.git', '')

    if ($commit -notmatch '^[a-fA-F0-9]{40}$') {
        throw 'CEREBRO_DELIVERY_SOURCE_COMMIT_INVALID'
    }

    if ($actualBranch -ne $ExpectedBranch) {
        throw (
            'CEREBRO_DELIVERY_SOURCE_BRANCH_MISMATCH:' +
            $actualBranch
        )
    }

    if (-not $normalizedRemote.EndsWith($normalizedExpected)) {
        throw (
            'CEREBRO_DELIVERY_SOURCE_REMOTE_MISMATCH:' +
            $actualRemote
        )
    }

    return [pscustomobject]@{
        repository = $ExpectedRepository
        branch = $actualBranch
        commit = $commit.ToLowerInvariant()
        working_source_path = $source
        remote = [string]$remote.stdout
    }
}

function Resolve-CerebroDeliveryProfile {
    param(
        [Parameter(Mandatory)]
        [string]$RequestedProfile,

        [string[]]$Operations = @(),

        [switch]$DirectWorkspaceAccess,

        [string]$WorkingSourcePath =
            'D:\Cerebro\Source\Cerebro_Source_v1.0',

        [object]$SourceIdentity
    )

    if ($null -eq $SourceIdentity) {
        try {
            $SourceIdentity = Get-CerebroDeliverySourceIdentity `
                -WorkingSourcePath $WorkingSourcePath
        }
        catch {
            return [pscustomobject]@{
                result = 'BLOCKED'
                classification = 'SOURCE_IDENTITY_NOT_VERIFIED'
                requested_profile = $(
                    ConvertTo-CerebroCanonicalDeliveryProfile `
                        -Profile $RequestedProfile
                )
                resolved_profile = $null
                reason = $_.Exception.Message
                decision_owner = 'MCP'
                adapter_recomputed = $false
            }
        }
    }

    $sourceRoot = [IO.Path]::GetFullPath(
        [string]$SourceIdentity.working_source_path
    )
    $resolver = Join-Path $sourceRoot 'mcp\control_resolution.py'
    if (-not (Test-Path -LiteralPath $resolver -PathType Leaf)) {
        return [pscustomobject]@{
            result = 'BLOCKED'
            classification = 'MCP_CONTROL_RESOLVER_NOT_FOUND'
            requested_profile = $(
                ConvertTo-CerebroCanonicalDeliveryProfile `
                    -Profile $RequestedProfile
            )
            resolved_profile = $null
            reason = $resolver
            decision_owner = 'MCP'
            adapter_recomputed = $false
        }
    }

    try {
        $python = Resolve-CerebroDeliveryPythonRunner
    }
    catch {
        return [pscustomobject]@{
            result = 'BLOCKED'
            classification = 'MCP_CONTROL_PYTHON_NOT_FOUND'
            requested_profile = $(
                ConvertTo-CerebroCanonicalDeliveryProfile `
                    -Profile $RequestedProfile
            )
            resolved_profile = $null
            reason = $_.Exception.Message
            decision_owner = 'MCP'
            adapter_recomputed = $false
        }
    }

    $requestPath = Join-Path ([IO.Path]::GetTempPath()) (
        'cerebro-mcp-delivery-request-' +
        [guid]::NewGuid().ToString('N') + '.json'
    )
    $outputPath = Join-Path ([IO.Path]::GetTempPath()) (
        'cerebro-mcp-delivery-result-' +
        [guid]::NewGuid().ToString('N') + '.json'
    )
    $utf8 = New-Object Text.UTF8Encoding($false)
    $request = [ordered]@{
        objective_ref = 'CEREBRO-DELIVERY-PROFILE-SELECTION'
        stage = 'UNDERSTAND_FRAME'
        material = $false
        consequence = 'LOW'
        uncertainty = 'LOW'
        requested_delivery_profile = $RequestedProfile.ToUpperInvariant()
        delivery_operations = @(
            $Operations |
                ForEach-Object { ([string]$_).ToLowerInvariant() }
        )
        direct_workspace_access_declared = [bool]$DirectWorkspaceAccess
        authoritative_source_commit = [string]$SourceIdentity.commit
        governing_basis_refs = @(
            'STD-CHANGE-DELIVERY',
            'CEREBRO-MCP-CONTROL-RESOLUTION-001'
        )
    }

    try {
        [IO.File]::WriteAllText(
            $requestPath,
            (($request | ConvertTo-Json -Depth 16) + "`n"),
            $utf8
        )
        $arguments = @($python.prefix_arguments) + @(
            $resolver,
            'resolve',
            '--request',
            $requestPath,
            '--output',
            $outputPath,
            '--source-root',
            $sourceRoot
        )
        $native = Invoke-CerebroDeliveryNativeArguments `
            -Executable $python.executable `
            -ArgumentList $arguments
        if (
            $native.exit_code -ne 0 -or
            -not (Test-Path -LiteralPath $outputPath -PathType Leaf)
        ) {
            return [pscustomobject]@{
                result = 'BLOCKED'
                classification = 'MCP_CONTROL_RESOLUTION_FAILED'
                requested_profile = $(
                    ConvertTo-CerebroCanonicalDeliveryProfile `
                        -Profile $RequestedProfile
                )
                resolved_profile = $null
                reason = ($native.stderr + ' ' + $native.stdout).Trim()
                decision_owner = 'MCP'
                adapter_recomputed = $false
            }
        }
        $control = [IO.File]::ReadAllText($outputPath) |
            ConvertFrom-Json
        $delivery = $control.mcp_delivery_profile_resolution
        $decision = $control.mcp_control_decision
        if ($null -eq $delivery -or $null -eq $decision) {
            throw 'CEREBRO_MCP_DELIVERY_CONTRACT_MISSING'
        }
        return [pscustomobject]@{
            result = [string]$delivery.result
            classification = [string]$delivery.classification
            requested_profile = [string]$delivery.requested_profile
            resolved_profile = $(
                if ($null -eq $delivery.resolved_profile) {
                    $null
                }
                else {
                    [string]$delivery.resolved_profile
                }
            )
            reason = [string]$delivery.reason
            controls = $delivery.controls
            delivery_basis_fingerprint = `
                [string]$delivery.basis_fingerprint
            control_decision_id = [string]$decision.control_decision_id
            control_decision_outcome = [string]$decision.outcome
            control_decision_basis_fingerprint = `
                [string]$decision.basis_fingerprint
            execution_profile_id = $(
                if ($null -eq $control.execution_profile) {
                    $null
                }
                else {
                    [string]$control.execution_profile.execution_profile_id
                }
            )
            execution_profile_basis_fingerprint = $(
                if ($null -eq $control.execution_profile) {
                    $null
                }
                else {
                    [string]$control.execution_profile.basis_fingerprint
                }
            )
            control_resolution_surface = `
                [string]$decision.control_resolution_surface
            decision_owner = 'MCP'
            adapter_recomputed = $false
        }
    }
    catch {
        return [pscustomobject]@{
            result = 'BLOCKED'
            classification = 'MCP_CONTROL_RESOLUTION_CONTRACT_INVALID'
            requested_profile = $(
                ConvertTo-CerebroCanonicalDeliveryProfile `
                    -Profile $RequestedProfile
            )
            resolved_profile = $null
            reason = $_.Exception.Message
            decision_owner = 'MCP'
            adapter_recomputed = $false
        }
    }
    finally {
        foreach ($temporaryPath in @($requestPath, $outputPath)) {
            if (Test-Path -LiteralPath $temporaryPath -PathType Leaf) {
                Remove-Item -LiteralPath $temporaryPath -Force `
                    -ErrorAction SilentlyContinue
            }
        }
    }
}

function Get-CerebroDeliveryProfileExplanation {
    param([string]$Profile)

    $descriptions = [ordered]@{
        LIMITED = [ordered]@{
            name = 'USER_MANAGED_FILE_DELIVERY'
            legacy_alias = 'STANDARD_A'
            use_when = @(
                'only existing files are replaced',
                'no create, move, or delete operation is required'
            )
            execution_owner = 'USER'
            agent_local_access = 'PROHIBITED'
            access_request_budget = 0
            artifact_format = 'FILES'
        }
        STANDARD = [ordered]@{
            name = 'USER_LOCAL_RUNNER_DELIVERY'
            legacy_alias = 'STANDARD_B'
            use_when = @(
                'directories or files are created',
                'files are moved or deleted',
                'bounded backup automation reduces risk'
            )
            execution_owner = 'USER_LOCAL_RUNNER'
            agent_local_access = 'PROHIBITED'
            access_request_budget = 0
            artifact_format = 'PAYLOAD_PLUS_INSTALLER'
            user_handoff_format = 'BUNDLE_ZIP_PLUS_LAUNCHER'
        }
        FULL = [ordered]@{
            name = 'CONTROLLED_WORKSPACE_TRANSACTION'
            legacy_alias = 'STANDARD_C'
            use_when = @(
                'the implementation agent has direct workspace access',
                'an exact Change Capsule and controlled transaction are available'
            )
            execution_owner = 'AGENT_CONTROLLED'
            agent_local_access = 'EXPLICIT_GRANT_REQUIRED'
            access_request_budget = 1
            artifact_format = 'CONTROLLED_TRANSACTION'
        }
        AUTO = [ordered]@{
            name = 'DETERMINISTIC_RESOLUTION'
            use_when = @(
                'current operations or direct-workspace evidence are supplied',
                'missing evidence must block instead of causing silent fallback'
            )
        }
    }

    if ([string]::IsNullOrWhiteSpace($Profile)) {
        return [pscustomobject]@{
            state = 'DELIVERY_PROFILES_EXPLAINED'
            profiles = $descriptions
            selection_mutates_source = $false
            silent_fallback = $false
        }
    }

    $selected = ConvertTo-CerebroCanonicalDeliveryProfile `
        -Profile $Profile

    if (-not $descriptions.Contains($selected)) {
        throw 'CEREBRO_DELIVERY_PROFILE_UNKNOWN'
    }

    return [pscustomobject]@{
        state = 'DELIVERY_PROFILE_EXPLAINED'
        profile = $selected
        definition = $descriptions[$selected]
        selection_mutates_source = $false
        silent_fallback = $false
    }
}

function Set-CerebroDeliverySelection {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Profile,

        [string[]]$Operations = @(),

        [switch]$DirectWorkspaceAccess,

        [string]$WorkingSourcePath =
            'D:\Cerebro\Source\Cerebro_Source_v1.0',

        [string]$StatePath =
            'D:\Cerebro\Run\State\Active\CEREBRO_DELIVERY_SELECTION.json',

        [string]$HistoryRoot =
            'D:\Cerebro\Run\Operations\Delivery\selections'
    )

    try {
        $source = Get-CerebroDeliverySourceIdentity `
            -WorkingSourcePath $WorkingSourcePath
    }
    catch {
        return [pscustomobject]@{
            state = 'BLOCKED'
            classification = 'SOURCE_IDENTITY_NOT_VERIFIED'
            requested_profile = $(
                ConvertTo-CerebroCanonicalDeliveryProfile `
                    -Profile $Profile
            )
            resolved_profile = $null
            reason = $_.Exception.Message
            state_changed = $false
            source_mutation = $false
            silent_fallback = $false
        }
    }

    $resolution = Resolve-CerebroDeliveryProfile `
        -RequestedProfile $Profile `
        -Operations $Operations `
        -DirectWorkspaceAccess:$DirectWorkspaceAccess `
        -WorkingSourcePath $WorkingSourcePath `
        -SourceIdentity $source

    if ($resolution.result -ne 'PASS') {
        $blockedDecisionId = $null
        $blockedDecisionOutcome = 'BLOCK'
        if ($null -ne $resolution.PSObject.Properties[
            'control_decision_id'
        ]) {
            $blockedDecisionId = $resolution.control_decision_id
        }
        if ($null -ne $resolution.PSObject.Properties[
            'control_decision_outcome'
        ]) {
            $blockedDecisionOutcome = `
                $resolution.control_decision_outcome
        }
        return [pscustomobject]@{
            state = 'BLOCKED'
            classification = $resolution.classification
            requested_profile = $resolution.requested_profile
            resolved_profile = $null
            reason = $resolution.reason
            control_decision_id = $blockedDecisionId
            control_decision_outcome = `
                $blockedDecisionOutcome
            decision_owner = 'MCP'
            adapter_recomputed = $false
            state_changed = $false
            source_mutation = $false
            silent_fallback = $false
        }
    }

    $selectedAt = [DateTimeOffset]::UtcNow.ToString('o')
    $fingerprintMaterial = (
        '{0}|{1}|{2}|{3}|{4}|{5}' -f
        $script:CerebroDeliverySelectionSchema,
        $resolution.resolved_profile,
        $source.commit,
        'STD-CHANGE-DELIVERY@0.11.0',
        $resolution.control_decision_id,
        $resolution.delivery_basis_fingerprint
    )
    $fingerprint = Get-CerebroDeliveryTextSha256 `
        -Text $fingerprintMaterial
    $selectionId = (
        'DELIVERY-{0}-{1}' -f
        ([DateTimeOffset]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')),
        ([guid]::NewGuid().ToString('N').Substring(0, 8))
    )
    $profileControls = $resolution.controls

    $state = [ordered]@{
        schema = $script:CerebroDeliverySelectionSchema
        selector_version = $script:CerebroDeliverySelectorVersion
        selection_id = $selectionId
        state = 'LOCKED'
        requested_profile = $resolution.requested_profile
        resolved_profile = $resolution.resolved_profile
        resolution_reason = $resolution.reason
        selected_at_utc = $selectedAt
        binding_fingerprint = $fingerprint
        decision_fingerprint = $fingerprint
        source = [ordered]@{
            repository = $source.repository
            branch = $source.branch
            commit = $source.commit
            working_source_path = $source.working_source_path
        }
        evidence = [ordered]@{
            explicit_user_terminal_selection = $true
            selection_input = $Profile.ToUpperInvariant()
            legacy_alias_used = $(
                $script:CerebroDeliveryProfileAliases.ContainsKey(
                    $Profile.ToUpperInvariant()
                )
            )
            operations = @(
                $Operations |
                    ForEach-Object {
                        ([string]$_).ToLowerInvariant()
                    }
            )
            direct_workspace_access_declared = [bool]$DirectWorkspaceAccess
        }
        applicability = [ordered]@{
            state = 'PENDING_PATCH_SCOPE_VALIDATION'
            revalidate_before_payload_design = $true
            revalidate_before_delivery = $true
        }
        mcp_control_binding = [ordered]@{
            decision_owner = 'MCP'
            control_resolution_surface = `
                $resolution.control_resolution_surface
            control_decision_id = $resolution.control_decision_id
            control_decision_outcome = `
                $resolution.control_decision_outcome
            control_decision_basis_fingerprint = `
                $resolution.control_decision_basis_fingerprint
            execution_profile_id = $resolution.execution_profile_id
            execution_profile_basis_fingerprint = `
                $resolution.execution_profile_basis_fingerprint
            delivery_profile_resolution_fingerprint = `
                $resolution.delivery_basis_fingerprint
            adapter_role = 'CAPABILITY_BINDING_AND_STATE_PROJECTION'
            adapter_recomputed = $false
        }
        controls = [ordered]@{
            source_mutation = $false
            publication_performed = $false
            commit_performed = $false
            silent_fallback = $false
            execution_owner = $profileControls.execution_owner
            agent_local_access = $profileControls.agent_local_access
            access_request_budget = $profileControls.access_request_budget
            artifact_format = $profileControls.artifact_format
        }
        invalidated_by = @(
            'source-commit-change',
            'explicit-user-reselection',
            'verified-capability-change',
            'selected-profile-validation-failure'
        )
    }

    $historyPath = Join-Path $HistoryRoot ($selectionId + '.json')
    Write-CerebroDeliveryJsonAtomic `
        -Path $historyPath `
        -Value $state
    Write-CerebroDeliveryJsonAtomic `
        -Path $StatePath `
        -Value $state

    return [pscustomobject]@{
        state = 'LOCKED'
        classification = 'DELIVERY_PROFILE_SELECTED'
        requested_profile = $state.requested_profile
        resolved_profile = $state.resolved_profile
        source_commit = $state.source.commit
        decision_fingerprint = $state.decision_fingerprint
        binding_fingerprint = $state.binding_fingerprint
        control_decision_id = `
            $state.mcp_control_binding.control_decision_id
        decision_owner = 'MCP'
        adapter_recomputed = $false
        state_path = [IO.Path]::GetFullPath($StatePath)
        history_path = [IO.Path]::GetFullPath($historyPath)
        state_changed = $true
        source_mutation = $false
        silent_fallback = $false
        execution_owner = $state.controls.execution_owner
        agent_local_access = $state.controls.agent_local_access
        access_request_budget = $state.controls.access_request_budget
        artifact_format = $state.controls.artifact_format
        next_action = 'RESEARCH_AND_VALIDATE_PATCH_SCOPE'
        receipt_line = (
            'CEREBRO_DELIVERY_SELECTION PROFILE={0} STATE=LOCKED COMMIT={1} FINGERPRINT={2}' -f
            $state.resolved_profile,
            $state.source.commit,
            $state.decision_fingerprint
        )
    }
}

function Get-CerebroDeliverySelectionStatus {
    [CmdletBinding()]
    param(
        [string]$WorkingSourcePath =
            'D:\Cerebro\Source\Cerebro_Source_v1.0',

        [string]$StatePath =
            'D:\Cerebro\Run\State\Active\CEREBRO_DELIVERY_SELECTION.json'
    )

    if (-not (Test-Path -LiteralPath $StatePath -PathType Leaf)) {
        return [pscustomobject]@{
            state = 'NOT_SELECTED'
            classification = 'DELIVERY_SELECTION_ABSENT'
            state_path = [IO.Path]::GetFullPath($StatePath)
            source_mutation = $false
            next_action = 'cerebro delivery select LIMITED|STANDARD|FULL|AUTO'
        }
    }

    try {
        $selection = [IO.File]::ReadAllText($StatePath) |
            ConvertFrom-Json
    }
    catch {
        return [pscustomobject]@{
            state = 'DEGRADED'
            classification = 'DELIVERY_SELECTION_INVALID'
            reason = $_.Exception.Message
            state_path = [IO.Path]::GetFullPath($StatePath)
            source_mutation = $false
        }
    }

    if (
        [string]$selection.schema -ne
            $script:CerebroDeliverySelectionSchema -and
        [string]$selection.schema -notin
            $script:CerebroDeliveryLegacySelectionSchemas
    ) {
        return [pscustomobject]@{
            state = 'DEGRADED'
            classification = 'DELIVERY_SELECTION_SCHEMA_UNKNOWN'
            state_path = [IO.Path]::GetFullPath($StatePath)
            source_mutation = $false
        }
    }

    try {
        $source = Get-CerebroDeliverySourceIdentity `
            -WorkingSourcePath $WorkingSourcePath
    }
    catch {
        return [pscustomobject]@{
            state = 'BLOCKED'
            classification = 'SOURCE_IDENTITY_NOT_VERIFIED'
            resolved_profile = $selection.resolved_profile
            reason = $_.Exception.Message
            state_path = [IO.Path]::GetFullPath($StatePath)
            source_mutation = $false
        }
    }

    $canonicalRequested = ConvertTo-CerebroCanonicalDeliveryProfile `
        -Profile ([string]$selection.requested_profile)
    $canonicalResolved = ConvertTo-CerebroCanonicalDeliveryProfile `
        -Profile ([string]$selection.resolved_profile)
    $profileControls = Get-CerebroDeliveryProfileControls `
        -Profile $canonicalResolved

    $effectiveState = 'LOCKED'
    $classification = 'DELIVERY_SELECTION_CURRENT'

    if ([string]$selection.source.commit -ne $source.commit) {
        $effectiveState = 'STALE'
        $classification = 'DELIVERY_SELECTION_SOURCE_CHANGED'
    }

    return [pscustomobject]@{
        state = $effectiveState
        classification = $classification
        requested_profile = $canonicalRequested
        resolved_profile = $canonicalResolved
        stored_schema = $selection.schema
        compatibility_projection = $(
            [string]$selection.schema -in
            $script:CerebroDeliveryLegacySelectionSchemas
        )
        selected_source_commit = $selection.source.commit
        current_source_commit = $source.commit
        decision_fingerprint = $selection.decision_fingerprint
        binding_fingerprint = $(
            if ($null -ne $selection.PSObject.Properties[
                'binding_fingerprint'
            ]) {
                $selection.binding_fingerprint
            }
            else {
                $selection.decision_fingerprint
            }
        )
        decision_owner = $(
            if ($null -ne $selection.PSObject.Properties[
                'mcp_control_binding'
            ]) {
                $selection.mcp_control_binding.decision_owner
            }
            else {
                'LEGACY_DELIVERY_SELECTOR'
            }
        )
        applicability = $selection.applicability.state
        state_path = [IO.Path]::GetFullPath($StatePath)
        source_mutation = $false
        silent_fallback = $false
        execution_owner = $profileControls.execution_owner
        agent_local_access = $profileControls.agent_local_access
        access_request_budget = $profileControls.access_request_budget
        artifact_format = $profileControls.artifact_format
    }
}

function Invoke-CerebroDeliverySelectionSelfTest {
    [CmdletBinding()]
    param()

    $tests = New-Object Collections.ArrayList

    function Add-TestResult {
        param([string]$Name, [bool]$Passed, [string]$Detail = '')
        $scriptTest = [pscustomobject]@{
            name = $Name
            result = $(if ($Passed) { 'PASS' } else { 'FAIL' })
            detail = $Detail
        }
        $tests.Add($scriptTest) | Out-Null
    }

    $selfTestSource = [IO.Path]::GetFullPath(
        (Join-Path $PSScriptRoot '..\..')
    )
    $selfTestGit = (
        Get-Command git.exe, git -ErrorAction SilentlyContinue |
            Select-Object -First 1
    )
    if ($null -eq $selfTestGit) {
        throw 'CEREBRO_DELIVERY_SELFTEST_GIT_NOT_FOUND'
    }
    $selfTestHead = Invoke-CerebroDeliveryNative `
        -FilePath $selfTestGit.Source `
        -WorkingDirectory $selfTestSource `
        -Arguments 'rev-parse HEAD'
    if (
        $selfTestHead.exit_code -ne 0 -or
        [string]$selfTestHead.stdout -notmatch '^[a-fA-F0-9]{40}$'
    ) {
        throw 'CEREBRO_DELIVERY_SELFTEST_SOURCE_COMMIT_INVALID'
    }
    $selfTestSourceIdentity = [pscustomobject]@{
        commit = ([string]$selfTestHead.stdout).ToLowerInvariant()
        working_source_path = $selfTestSource
    }

    $autoBlocked = Resolve-CerebroDeliveryProfile `
        -RequestedProfile 'AUTO' `
        -WorkingSourcePath $selfTestSource `
        -SourceIdentity $selfTestSourceIdentity
    Add-TestResult `
        -Name 'auto_without_evidence_fails_closed' `
        -Passed (
            $autoBlocked.result -eq 'BLOCKED' -and
            $null -eq $autoBlocked.resolved_profile
        )

    $autoA = Resolve-CerebroDeliveryProfile `
        -RequestedProfile 'AUTO' `
        -Operations @('replace', 'replace') `
        -WorkingSourcePath $selfTestSource `
        -SourceIdentity $selfTestSourceIdentity
    Add-TestResult `
        -Name 'auto_replacement_scope_resolves_limited' `
        -Passed ($autoA.resolved_profile -eq 'LIMITED')

    $autoB = Resolve-CerebroDeliveryProfile `
        -RequestedProfile 'AUTO' `
        -Operations @('replace', 'create') `
        -WorkingSourcePath $selfTestSource `
        -SourceIdentity $selfTestSourceIdentity
    Add-TestResult `
        -Name 'auto_structured_scope_resolves_standard' `
        -Passed ($autoB.resolved_profile -eq 'STANDARD')

    $autoC = Resolve-CerebroDeliveryProfile `
        -RequestedProfile 'AUTO' `
        -Operations @('create') `
        -DirectWorkspaceAccess `
        -WorkingSourcePath $selfTestSource `
        -SourceIdentity $selfTestSourceIdentity
    Add-TestResult `
        -Name 'auto_direct_workspace_resolves_full' `
        -Passed ($autoC.resolved_profile -eq 'FULL')

    $invalidA = Resolve-CerebroDeliveryProfile `
        -RequestedProfile 'LIMITED' `
        -Operations @('create') `
        -WorkingSourcePath $selfTestSource `
        -SourceIdentity $selfTestSourceIdentity
    Add-TestResult `
        -Name 'limited_rejects_create_scope' `
        -Passed (
            $invalidA.result -eq 'BLOCKED' -and
            $null -eq $invalidA.resolved_profile
        )

    $fingerprint1 = Get-CerebroDeliveryTextSha256 `
        -Text 'schema|FULL|commit|contract'
    $fingerprint2 = Get-CerebroDeliveryTextSha256 `
        -Text 'schema|FULL|commit|contract'
    Add-TestResult `
        -Name 'adapter_binding_fingerprint_is_deterministic' `
        -Passed ($fingerprint1 -eq $fingerprint2)

    $legacyB = Resolve-CerebroDeliveryProfile `
        -RequestedProfile 'STANDARD_B' `
        -Operations @('create') `
        -WorkingSourcePath $selfTestSource `
        -SourceIdentity $selfTestSourceIdentity
    Add-TestResult `
        -Name 'legacy_standard_b_resolves_canonical_standard' `
        -Passed (
            $legacyB.result -eq 'PASS' -and
            $legacyB.requested_profile -eq 'STANDARD' -and
            $legacyB.resolved_profile -eq 'STANDARD'
        )

    $limitedControls = Get-CerebroDeliveryProfileControls `
        -Profile 'LIMITED'
    $standardControls = Get-CerebroDeliveryProfileControls `
        -Profile 'STANDARD'
    $fullControls = Get-CerebroDeliveryProfileControls `
        -Profile 'FULL'
    Add-TestResult `
        -Name 'limited_and_standard_have_zero_access_request_budget' `
        -Passed (
            $limitedControls.access_request_budget -eq 0 -and
            $standardControls.access_request_budget -eq 0 -and
            $limitedControls.agent_local_access -eq 'PROHIBITED' -and
            $standardControls.agent_local_access -eq 'PROHIBITED'
        )
    Add-TestResult `
        -Name 'full_has_one_aggregated_access_request_budget' `
        -Passed (
            $fullControls.access_request_budget -eq 1 -and
            $fullControls.agent_local_access -eq
                'EXPLICIT_GRANT_REQUIRED'
        )
    Add-TestResult `
        -Name 'delivery_profile_resolution_is_mcp_owned' `
        -Passed (
            $autoB.decision_owner -eq 'MCP' -and
            $autoB.adapter_recomputed -eq $false -and
            $autoB.control_decision_outcome -eq 'CONTINUE' -and
            $autoB.control_resolution_surface -eq
                'CEREBRO-MCP-CONTROL-RESOLUTION-001'
        )
    Add-TestResult `
        -Name 'delivery_profile_namespaces_remain_distinct' `
        -Passed (
            $autoB.resolved_profile -eq 'STANDARD' -and
            $autoB.controls.artifact_format -eq
                'PAYLOAD_PLUS_INSTALLER' -and
            $autoB.execution_profile_id -match '^EXECP-'
        )

    $passed = @(
        @($tests) | Where-Object { $_.result -ne 'PASS' }
    ).Count -eq 0

    return [pscustomobject]@{
        schema = 'cerebro-delivery-adapter-selftest/v0.3'
        result = $(if ($passed) { 'PASS' } else { 'FAIL' })
        selector_version = $script:CerebroDeliverySelectorVersion
        tests = @($tests)
        source_mutation = $false
    }
}

function Invoke-CerebroDeliveryCommand {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateSet('select', 'status', 'explain', 'selftest')]
        [string]$Action,

        [string]$Profile,

        [string[]]$Operations = @(),

        [switch]$DirectWorkspaceAccess,

        [string]$WorkingSourcePath =
            'D:\Cerebro\Source\Cerebro_Source_v1.0',

        [string]$StatePath =
            'D:\Cerebro\Run\State\Active\CEREBRO_DELIVERY_SELECTION.json',

        [string]$HistoryRoot =
            'D:\Cerebro\Run\Operations\Delivery\selections'
    )

    switch ($Action) {
        'select' {
            if ([string]::IsNullOrWhiteSpace($Profile)) {
                throw 'CEREBRO_DELIVERY_SELECT_REQUIRES_PROFILE'
            }

            return Set-CerebroDeliverySelection `
                -Profile $Profile `
                -Operations $Operations `
                -DirectWorkspaceAccess:$DirectWorkspaceAccess `
                -WorkingSourcePath $WorkingSourcePath `
                -StatePath $StatePath `
                -HistoryRoot $HistoryRoot
        }
        'status' {
            return Get-CerebroDeliverySelectionStatus `
                -WorkingSourcePath $WorkingSourcePath `
                -StatePath $StatePath
        }
        'explain' {
            return Get-CerebroDeliveryProfileExplanation `
                -Profile $Profile
        }
        'selftest' {
            return Invoke-CerebroDeliverySelectionSelfTest
        }
    }
}

if ($SelfTest) {
    $selfTestReport = Invoke-CerebroDeliverySelectionSelfTest
    $selfTestReport | ConvertTo-Json -Depth 10

    if ($selfTestReport.result -ne 'PASS') {
        exit 1
    }
}

if (-not [string]::IsNullOrWhiteSpace($CliCommand)) {
    $cliResult = Invoke-CerebroDeliveryCommand `
        -Action $CliCommand `
        -Profile $CliProfile `
        -Operations $CliOperations `
        -DirectWorkspaceAccess:$CliDirectWorkspaceAccess `
        -WorkingSourcePath $CliWorkingSourcePath `
        -StatePath $CliStatePath `
        -HistoryRoot $CliHistoryRoot
    $cliResult | ConvertTo-Json -Depth 10

    if (
        $cliResult.PSObject.Properties.Name -contains 'state' -and
        [string]$cliResult.state -in @(
            'BLOCKED',
            'DEGRADED',
            'STALE',
            'NOT_SELECTED'
        )
    ) {
        exit 2
    }
}
