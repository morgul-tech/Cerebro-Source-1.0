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
        'D:\Cerebro\Run\active\CEREBRO_DELIVERY_SELECTION.json',

    [string]$CliHistoryRoot =
        'D:\Cerebro\Run\delivery\selections'
)

Set-StrictMode -Version 2.0

$script:CerebroDeliverySelectionSchema = 'cerebro-delivery-selection/v0.2'
$script:CerebroDeliveryLegacySelectionSchema = 'cerebro-delivery-selection/v0.1'
$script:CerebroDeliverySelectorVersion = '0.3.0'
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

        [switch]$DirectWorkspaceAccess
    )

    $requestedInput = $RequestedProfile.ToUpperInvariant()
    $requested = ConvertTo-CerebroCanonicalDeliveryProfile `
        -Profile $requestedInput
    $normalizedOperations = @(
        $Operations |
            ForEach-Object { ([string]$_).ToLowerInvariant() }
    )
    $unknownOperations = @(
        $normalizedOperations |
            Where-Object { $_ -notin @('replace', 'create', 'delete') }
    )

    if ($unknownOperations.Count -gt 0) {
        return [pscustomobject]@{
            result = 'BLOCKED'
            classification = 'UNKNOWN_PATCH_OPERATION'
            requested_profile = $requested
            resolved_profile = $null
            reason = ($unknownOperations -join ',')
        }
    }

    if ($requested -eq 'AUTO') {
        if ($DirectWorkspaceAccess) {
            return [pscustomobject]@{
                result = 'PASS'
                classification = 'DELIVERY_PROFILE_RESOLVED'
                requested_profile = 'AUTO'
                resolved_profile = 'FULL'
                reason = 'direct-workspace-access-declared'
            }
        }

        if ($normalizedOperations.Count -eq 0) {
            return [pscustomobject]@{
                result = 'BLOCKED'
                classification = 'INSUFFICIENT_CAPABILITY_EVIDENCE'
                requested_profile = 'AUTO'
                resolved_profile = $null
                reason = 'AUTO requires patch operations or declared direct workspace access'
            }
        }

        if (
            @(
                $normalizedOperations |
                    Where-Object { $_ -ne 'replace' }
            ).Count -eq 0
        ) {
            return [pscustomobject]@{
                result = 'PASS'
                classification = 'DELIVERY_PROFILE_RESOLVED'
                requested_profile = 'AUTO'
                resolved_profile = 'LIMITED'
                reason = 'existing-file-replacements-only'
            }
        }

        return [pscustomobject]@{
            result = 'PASS'
            classification = 'DELIVERY_PROFILE_RESOLVED'
            requested_profile = 'AUTO'
            resolved_profile = 'STANDARD'
            reason = 'structured-file-operations-required'
        }
    }

    if ($requested -notin $script:CerebroDeliveryProfiles) {
        return [pscustomobject]@{
            result = 'BLOCKED'
            classification = 'UNKNOWN_DELIVERY_PROFILE'
            requested_profile = $requested
            resolved_profile = $null
            reason = 'allowed=LIMITED,STANDARD,FULL,AUTO; aliases=STANDARD_A,STANDARD_B,STANDARD_C'
        }
    }

    if (
        $requested -eq 'LIMITED' -and
        @(
            $normalizedOperations |
                Where-Object { $_ -ne 'replace' }
        ).Count -gt 0
    ) {
        return [pscustomobject]@{
            result = 'BLOCKED'
            classification = 'DELIVERY_PROFILE_NOT_APPLICABLE'
            requested_profile = $requested
            resolved_profile = $null
            reason = 'LIMITED permits replacement of existing files only'
        }
    }

    return [pscustomobject]@{
        result = 'PASS'
        classification = 'DELIVERY_PROFILE_RESOLVED'
        requested_profile = $requested
        resolved_profile = $requested
        reason = $(
            if ($requestedInput -eq $requested) {
                'explicit-user-terminal-selection'
            }
            else {
                'legacy-alias-resolved-to-canonical-profile'
            }
        )
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
            'D:\Cerebro\Run\active\CEREBRO_DELIVERY_SELECTION.json',

        [string]$HistoryRoot =
            'D:\Cerebro\Run\delivery\selections'
    )

    $resolution = Resolve-CerebroDeliveryProfile `
        -RequestedProfile $Profile `
        -Operations $Operations `
        -DirectWorkspaceAccess:$DirectWorkspaceAccess

    if ($resolution.result -ne 'PASS') {
        return [pscustomobject]@{
            state = 'BLOCKED'
            classification = $resolution.classification
            requested_profile = $resolution.requested_profile
            resolved_profile = $null
            reason = $resolution.reason
            state_changed = $false
            source_mutation = $false
            silent_fallback = $false
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
            requested_profile = $resolution.requested_profile
            resolved_profile = $null
            reason = $_.Exception.Message
            state_changed = $false
            source_mutation = $false
            silent_fallback = $false
        }
    }

    $selectedAt = [DateTimeOffset]::UtcNow.ToString('o')
    $fingerprintMaterial = (
        '{0}|{1}|{2}|{3}' -f
        $script:CerebroDeliverySelectionSchema,
        $resolution.resolved_profile,
        $source.commit,
        'STD-CHANGE-DELIVERY@0.7.0'
    )
    $fingerprint = Get-CerebroDeliveryTextSha256 `
        -Text $fingerprintMaterial
    $selectionId = (
        'DELIVERY-{0}-{1}' -f
        ([DateTimeOffset]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')),
        ([guid]::NewGuid().ToString('N').Substring(0, 8))
    )
    $profileControls = Get-CerebroDeliveryProfileControls `
        -Profile $resolution.resolved_profile

    $state = [ordered]@{
        schema = $script:CerebroDeliverySelectionSchema
        selector_version = $script:CerebroDeliverySelectorVersion
        selection_id = $selectionId
        state = 'LOCKED'
        requested_profile = $resolution.requested_profile
        resolved_profile = $resolution.resolved_profile
        resolution_reason = $resolution.reason
        selected_at_utc = $selectedAt
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
            'D:\Cerebro\Run\active\CEREBRO_DELIVERY_SELECTION.json'
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
        [string]$selection.schema -notin @(
            $script:CerebroDeliverySelectionSchema,
            $script:CerebroDeliveryLegacySelectionSchema
        )
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
            [string]$selection.schema -eq
            $script:CerebroDeliveryLegacySelectionSchema
        )
        selected_source_commit = $selection.source.commit
        current_source_commit = $source.commit
        decision_fingerprint = $selection.decision_fingerprint
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

    $autoBlocked = Resolve-CerebroDeliveryProfile `
        -RequestedProfile 'AUTO'
    Add-TestResult `
        -Name 'auto_without_evidence_fails_closed' `
        -Passed (
            $autoBlocked.result -eq 'BLOCKED' -and
            $null -eq $autoBlocked.resolved_profile
        )

    $autoA = Resolve-CerebroDeliveryProfile `
        -RequestedProfile 'AUTO' `
        -Operations @('replace', 'replace')
    Add-TestResult `
        -Name 'auto_replacement_scope_resolves_limited' `
        -Passed ($autoA.resolved_profile -eq 'LIMITED')

    $autoB = Resolve-CerebroDeliveryProfile `
        -RequestedProfile 'AUTO' `
        -Operations @('replace', 'create')
    Add-TestResult `
        -Name 'auto_structured_scope_resolves_standard' `
        -Passed ($autoB.resolved_profile -eq 'STANDARD')

    $autoC = Resolve-CerebroDeliveryProfile `
        -RequestedProfile 'AUTO' `
        -Operations @('create') `
        -DirectWorkspaceAccess
    Add-TestResult `
        -Name 'auto_direct_workspace_resolves_full' `
        -Passed ($autoC.resolved_profile -eq 'FULL')

    $invalidA = Resolve-CerebroDeliveryProfile `
        -RequestedProfile 'LIMITED' `
        -Operations @('create')
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
        -Name 'decision_fingerprint_is_deterministic' `
        -Passed ($fingerprint1 -eq $fingerprint2)

    $legacyB = Resolve-CerebroDeliveryProfile `
        -RequestedProfile 'STANDARD_B' `
        -Operations @('create')
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

    $passed = @(
        @($tests) | Where-Object { $_.result -ne 'PASS' }
    ).Count -eq 0

    return [pscustomobject]@{
        schema = 'cerebro-delivery-selector-selftest/v0.2'
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
            'D:\Cerebro\Run\active\CEREBRO_DELIVERY_SELECTION.json',

        [string]$HistoryRoot =
            'D:\Cerebro\Run\delivery\selections'
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
