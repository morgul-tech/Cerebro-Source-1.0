Set-StrictMode -Version Latest

function Get-CerebroRuntimeSha256Text {
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

function Get-CerebroRuntimeFileSha256 {
    param(
        [Parameter(Mandatory)]
        [string]$Path
    )

    return (
        Get-FileHash `
            -LiteralPath $Path `
            -Algorithm SHA256
    ).Hash.ToLowerInvariant()
}

function Get-CerebroRuntimeProperty {
    param(
        [Parameter(Mandatory)]
        [object]$Object,

        [Parameter(Mandatory)]
        [string]$Name,

        [Parameter(Mandatory)]
        [string]$Context
    )

    # RUNTIME_PROPERTY_DICTIONARY_SUPPORT
    if ($Object -is [System.Collections.IDictionary]) {
        if ($Object.Contains($Name)) {
            return $Object[$Name]
        }
    }
    else {
        $property = $Object.PSObject.Properties[$Name]

        if ($null -ne $property) {
            return $property.Value
        }
    }

    throw "RUNTIME_REQUIRED_FIELD_MISSING:$Context`:$Name"
}

function Assert-CerebroRuntimeFields {
    param(
        [Parameter(Mandatory)]
        [object]$Object,

        [Parameter(Mandatory)]
        [string[]]$Fields,

        [Parameter(Mandatory)]
        [string]$Context
    )

    foreach ($field in $Fields) {
        [void](
            Get-CerebroRuntimeProperty `
                -Object $Object `
                -Name $field `
                -Context $Context
        )
    }
}

function Read-CerebroRuntimeJson {
    param(
        [Parameter(Mandatory)]
        [string]$Path,

        [Parameter(Mandatory)]
        [string]$Context
    )

    if (
        -not (
            Test-Path `
                -LiteralPath $Path `
                -PathType Leaf
        )
    ) {
        throw "RUNTIME_FILE_MISSING:$Context`:$Path"
    }

    try {
        return (
            Get-Content `
                -LiteralPath $Path `
                -Raw |
            ConvertFrom-Json
        )
    }
    catch {
        throw (
            "RUNTIME_JSON_INVALID:$Context`:" +
            $_.Exception.Message
        )
    }
}

function Write-CerebroRuntimeJson {
    param(
        [Parameter(Mandatory)]
        [string]$Path,

        [Parameter(Mandatory)]
        [object]$Value
    )

    $directory = Split-Path -Parent $Path

    [IO.Directory]::CreateDirectory(
        $directory
    ) | Out-Null

    $temporary = Join-Path `
        $directory `
        (
            '.cerebro-runtime-' +
            [guid]::NewGuid().ToString('N') +
            '.tmp'
        )

    $backup = $null

    try {
        $json = (
            $Value |
            ConvertTo-Json -Depth 32
        ) + "`n"

        [IO.File]::WriteAllText(
            $temporary,
            $json,
            [Text.UTF8Encoding]::new($false)
        )

        if (
            Test-Path `
                -LiteralPath $Path `
                -PathType Leaf
        ) {
            $backup = (
                $Path +
                '.replace-backup-' +
                [guid]::NewGuid().ToString('N')
            )

            [IO.File]::Replace(
                $temporary,
                $Path,
                $backup,
                $true
            )

            Remove-Item `
                -LiteralPath $backup `
                -Force `
                -ErrorAction SilentlyContinue
        }
        else {
            [IO.File]::Move(
                $temporary,
                $Path
            )
        }
    }
    finally {
        if (
            Test-Path `
                -LiteralPath $temporary `
                -PathType Leaf
        ) {
            Remove-Item `
                -LiteralPath $temporary `
                -Force `
                -ErrorAction SilentlyContinue
        }

        if (
            $backup -and
            (
                Test-Path `
                    -LiteralPath $backup `
                    -PathType Leaf
            )
        ) {
            Remove-Item `
                -LiteralPath $backup `
                -Force `
                -ErrorAction SilentlyContinue
        }
    }
}

function Get-CerebroRuntimeReleaseDigest {
    param(
        [Parameter(Mandatory)]
        [string]$ReleasePath
    )

    if (
        -not (
            Test-Path `
                -LiteralPath $ReleasePath `
                -PathType Container
        )
    ) {
        throw "RUNTIME_RELEASE_NOT_FOUND:$ReleasePath"
    }

    $root = (
        [IO.Path]::GetFullPath($ReleasePath)
    ).TrimEnd('\', '/')

    $files = @(
        Get-ChildItem `
            -LiteralPath $root `
            -File `
            -Recurse |
        Sort-Object FullName
    )

    if ($files.Count -eq 0) {
        throw 'RUNTIME_RELEASE_EMPTY'
    }

    $material = @()

    foreach ($file in $files) {
        $relative = (
            $file.FullName.Substring(
                $root.Length
            )
        ).TrimStart(
            [char]'\',
            [char]'/'
        ).Replace('\', '/')

        $hash = Get-CerebroRuntimeFileSha256 `
            -Path $file.FullName

        $material += (
            '{0}|{1}|{2}' -f
            $relative,
            $file.Length,
            $hash
        )
    }

    return Get-CerebroRuntimeSha256Text `
        -Text ($material -join "`n")
}

function New-CerebroRuntimeId {
    param(
        [Parameter(Mandatory)]
        [string]$Prefix,

        [Parameter(Mandatory)]
        [string]$Material
    )

    return (
        $Prefix +
        '-' +
        (
            Get-CerebroRuntimeSha256Text `
                -Text $Material
        ).Substring(0,24)
    )
}

function Get-CerebroRuntimePayloadControl {
    param(
        [Parameter(Mandatory)]
        [object]$Payload
    )

    $property = $Payload.PSObject.Properties['runtime_control']

    if ($null -eq $property) {
        return $null
    }

    return [string]$property.Value
}

function New-CerebroRuntimeProjections {
    param(
        [object]$Instance,
        [object]$Profile,
        [string]$ReleaseId,
        [string]$SourceIdentity,
        [object]$Event,
        [object]$Decision,
        [object]$Binding,
        [object]$ExecutionResult,
        [object]$VerificationResult,
        [string[]]$EvidenceRefs,
        [string[]]$FailureRefs,
        [string]$ReceiptPath,
        [string]$StartupStage,
        [string]$FinalState
    )

    $bindingId = $null
    $decisionId = $null
    $eventId = $null
    $eventType = $null
    $correlationRef = $null

    if ($null -ne $Binding) {
        $bindingId = [string]$Binding.binding_id
    }

    if ($null -ne $Decision) {
        $decisionId = [string]$Decision.decision_id
    }

    if ($null -ne $Event) {
        $eventId = [string]$Event.event_id
        $eventType = [string]$Event.event_type
        $correlationRef = [string]$Event.correlation_ref
    }

    return [ordered]@{
        IDENTITY = [ordered]@{
            runtime_instance_id = $Instance.runtime_instance_id
            profile_id = $Profile.profile_id
            release_id = $ReleaseId
            source_identity = $SourceIdentity
        }

        BOOT = [ordered]@{
            startup_stage = $StartupStage
            input_validation = 'COMPLETE'
            activation_state = 'ACTIVE'
            control_state = $FinalState
        }

        GOVERNANCE = [ordered]@{
            authority = 'derived-non-authoritative-runtime'
            rules = @(
                'CEREBRO-RUNTIME-0-1-CONTRACT-SET-001',
                'RUNTIME-STATE-TRANSITION-TABLE-001'
            )
            capability_bindings = @(
                $Profile.capability_binding_refs
            )
            control_gates = @(
                'release-pinned',
                'event-valid',
                'binding-declared',
                'verify-before-success'
            )
        }

        SESSION = [ordered]@{
            event_id = $eventId
            event_type = $eventType
            user_boundary = (
                $FinalState -eq 'WAITING_USER'
            )
            correlation_ref = $correlationRef
        }

        EXECUTION = [ordered]@{
            decision_id = $decisionId
            binding_id = $bindingId
            execution_state = $FinalState
            execution_result = $ExecutionResult
        }

        EVIDENCE = [ordered]@{
            verification_result = $VerificationResult
            evidence_refs = @($EvidenceRefs)
            failure_refs = @($FailureRefs)
            receipt_ref = $ReceiptPath
        }
    }
}

function Invoke-CerebroRuntimeCore {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$ReleasePath,

        [Parameter(Mandatory)]
        [string]$PinnedReleaseSha256,

        [Parameter(Mandatory)]
        [string]$RuntimeProfilePath,

        [Parameter(Mandatory)]
        [string]$EventPath,

        [Parameter(Mandatory)]
        [string]$StatePath,

        [Parameter(Mandatory)]
        [string]$ReceiptPath,

        [Parameter(Mandatory)]
        [string]$FailureLedgerPath
    )

    $startedAt = [DateTime]::UtcNow.ToString('o')

    $failureStage = 'LOAD_RELEASE'
    $profile = $null
    $event = $null
    $instance = $null
    $decision = $null
    $binding = $null
    $executionResult = $null
    $verificationResult = $null
    $releaseId = $null
    $sourceIdentity = $null
    $finalState = $null
    $evidenceRefs = @()
    $failureRefs = @()
    $initialState = 'ACTIVE_READY'

    try {
        # LOAD_RELEASE
        $releaseDigest = (
            Get-CerebroRuntimeReleaseDigest `
                -ReleasePath $ReleasePath
        ).ToLowerInvariant()

        if (
            $releaseDigest -ne
            $PinnedReleaseSha256.ToLowerInvariant()
        ) {
            throw (
                'RUNTIME_RELEASE_DIGEST_MISMATCH:' +
                "EXPECTED=$PinnedReleaseSha256`:" +
                "ACTUAL=$releaseDigest"
            )
        }

        $releaseId = "sha256:$releaseDigest"

        $evidenceRefs += $releaseId

        $sourceIdentityPath = Join-Path `
            $ReleasePath `
            'source-identity.txt'

        if (
            -not (
                Test-Path `
                    -LiteralPath $sourceIdentityPath `
                    -PathType Leaf
            )
        ) {
            throw 'RUNTIME_RELEASE_SOURCE_IDENTITY_MISSING'
        }

        $sourceIdentity = (
            [IO.File]::ReadAllText(
                $sourceIdentityPath
            )
        ).Trim()

        if (
            [string]::IsNullOrWhiteSpace(
                $sourceIdentity
            )
        ) {
            throw 'RUNTIME_RELEASE_SOURCE_IDENTITY_EMPTY'
        }

        $bindingsPath = Join-Path `
            $ReleasePath `
            'capability-bindings.json'

        $bindingsDocument = Read-CerebroRuntimeJson `
            -Path $bindingsPath `
            -Context 'CAPABILITY_BINDINGS'

        Assert-CerebroRuntimeFields `
            -Object $bindingsDocument `
            -Fields @('bindings') `
            -Context 'CAPABILITY_BINDINGS'

        $bindings = @($bindingsDocument.bindings)

        $profile = Read-CerebroRuntimeJson `
            -Path $RuntimeProfilePath `
            -Context 'RUNTIME_PROFILE'

        Assert-CerebroRuntimeFields `
            -Object $profile `
            -Fields @(
                'profile_id',
                'runtime_version',
                'release_ref',
                'entrypoint',
                'supported_event_types',
                'capability_binding_refs',
                'projection_requirements',
                'state_contract_ref',
                'receipt_contract_ref',
                'failure_policy_ref'
            ) `
            -Context 'RUNTIME_PROFILE'

        if ([string]$profile.runtime_version -ne '0.1') {
            throw 'RUNTIME_PROFILE_VERSION_INVALID'
        }

        if ([string]$profile.release_ref -ne $releaseId) {
            throw (
                'RUNTIME_PROFILE_RELEASE_REF_INVALID:' +
                "EXPECTED=$releaseId`:" +
                "ACTUAL=$($profile.release_ref)"
            )
        }

        if (
            [string]$profile.state_contract_ref -ne
            'RUNTIME-STATE-TRANSITION-TABLE-001'
        ) {
            throw 'RUNTIME_PROFILE_STATE_CONTRACT_INVALID'
        }

        if (
            [string]$profile.receipt_contract_ref -ne
            'RUNTIME-RECEIPT-CONTRACT-001'
        ) {
            throw 'RUNTIME_PROFILE_RECEIPT_CONTRACT_INVALID'
        }

        if (
            [string]$profile.failure_policy_ref -ne
            'FAILURE-LEDGER-CONTRACT-001'
        ) {
            throw 'RUNTIME_PROFILE_FAILURE_POLICY_INVALID'
        }

        foreach ($requiredProjection in @(
            'IDENTITY',
            'BOOT',
            'GOVERNANCE',
            'SESSION',
            'EXECUTION',
            'EVIDENCE'
        )) {
            if (
                @($profile.projection_requirements) -notcontains
                $requiredProjection
            ) {
                throw (
                    'RUNTIME_PROFILE_PROJECTION_MISSING:' +
                    $requiredProjection
                )
            }
        }

        foreach ($bindingRef in @(
            $profile.capability_binding_refs
        )) {
            $declared = @(
                $bindings |
                Where-Object {
                    [string]$_.binding_id -eq
                    [string]$bindingRef
                }
            )

            if ($declared.Count -ne 1) {
                throw (
                    'RUNTIME_BINDING_REFERENCE_INVALID:' +
                    $bindingRef
                )
            }

            Assert-CerebroRuntimeFields `
                -Object $declared[0] `
                -Fields @(
                    'binding_id',
                    'capability_id',
                    'implementation_ref',
                    'input_contract',
                    'output_contract',
                    'allowed_side_effects',
                    'authority_requirement',
                    'timeout_policy',
                    'exit_policy',
                    'verification_policy'
                ) `
                -Context (
                    'CAPABILITY_BINDING:' +
                    $bindingRef
                )

            if (
                @(
                    'builtin:echo',
                    'builtin:idea-capture',
                    'builtin:control-stop'
                ) -notcontains
                [string]$declared[0].implementation_ref
            ) {
                throw (
                    'RUNTIME_IMPLEMENTATION_REF_UNRESOLVED:' +
                    $declared[0].implementation_ref
                )
            }
        }

        # LOAD_STATE
        $failureStage = 'LOAD_STATE'

        $priorStateHash = $null
        $priorState = $null

        if (
            Test-Path `
                -LiteralPath $StatePath `
                -PathType Leaf
        ) {
            $priorStateHash = `
                Get-CerebroRuntimeFileSha256 `
                    -Path $StatePath

            $priorState = Read-CerebroRuntimeJson `
                -Path $StatePath `
                -Context 'PRIOR_RUNTIME_STATE'

            Assert-CerebroRuntimeFields `
                -Object $priorState `
                -Fields @(
                    'schema',
                    'authority',
                    'last_terminal_state',
                    'last_event_id'
                ) `
                -Context 'PRIOR_RUNTIME_STATE'

            if (
                [string]$priorState.schema -ne
                'cerebro-runtime-derived-state/v0.1'
            ) {
                throw 'RUNTIME_PRIOR_STATE_SCHEMA_INVALID'
            }

            $evidenceRefs += (
                'prior-state-sha256:' +
                $priorStateHash
            )
        }

        # ACCEPT_ONE_EVENT
        $failureStage = 'ACCEPT_ONE_EVENT'

        $event = Read-CerebroRuntimeJson `
            -Path $EventPath `
            -Context 'RUNTIME_EVENT'

        Assert-CerebroRuntimeFields `
            -Object $event `
            -Fields @(
                'event_id',
                'event_type',
                'issued_at',
                'source',
                'authority',
                'payload',
                'correlation_ref'
            ) `
            -Context 'RUNTIME_EVENT'

        if (
            $priorState -and
            [string]$priorState.last_event_id -eq
            [string]$event.event_id
        ) {
            throw (
                'RUNTIME_EVENT_DUPLICATE:' +
                $event.event_id
            )
        }

        $instanceId = New-CerebroRuntimeId `
            -Prefix 'RUNTIME' `
            -Material (
                '{0}|{1}|{2}|{3}' -f
                $profile.profile_id,
                $releaseId,
                $sourceIdentity,
                $event.event_id
            )

        $projectionRefs = @(
            'receipt://projections/IDENTITY',
            'receipt://projections/BOOT',
            'receipt://projections/GOVERNANCE',
            'receipt://projections/SESSION',
            'receipt://projections/EXECUTION',
            'receipt://projections/EVIDENCE'
        )

        $instance = [ordered]@{
            runtime_instance_id = $instanceId
            profile_id = [string]$profile.profile_id
            release_id = $releaseId
            source_identity = $sourceIdentity
            created_at = $startedAt
            current_state = 'ACTIVE_READY'
            projection_refs = $projectionRefs
            active_event_id = [string]$event.event_id
            failure_ledger_ref = $FailureLedgerPath
        }

        if (
            @($profile.supported_event_types) -notcontains
            [string]$event.event_type
        ) {
            throw (
                'RUNTIME_EVENT_TYPE_UNSUPPORTED:' +
                $event.event_type
            )
        }

        # RESOLVE
        $failureStage = 'RESOLVE'
        $instance.current_state = 'EVALUATING'

        $runtimeControl = Get-CerebroRuntimePayloadControl `
            -Payload $event.payload

        if ($runtimeControl -eq 'WAIT_USER') {
            $nextActionProperty = `
                $event.payload.PSObject.Properties[
                    'copyable_next_action'
                ]

            if (
                $null -eq $nextActionProperty -or
                [string]::IsNullOrWhiteSpace(
                    [string]$nextActionProperty.Value
                )
            ) {
                throw 'RUNTIME_USER_BOUNDARY_NEXT_ACTION_MISSING'
            }

            $decision = [ordered]@{
                decision_id = New-CerebroRuntimeId `
                    -Prefix 'DECISION' `
                    -Material (
                        "$instanceId|$($event.event_id)|WAIT_USER"
                    )
                runtime_instance_id = $instanceId
                event_id = [string]$event.event_id
                initial_state = 'ACTIVE_READY'
                selected_transition = 'MATERIAL_USER_BOUNDARY'
                target_state = 'WAITING_USER'
                capability_binding_ref = $null
                decision_basis = 'event.payload.runtime_control'
                resolved_at = [DateTime]::UtcNow.ToString('o')
            }

            $finalState = 'WAITING_USER'

            $verificationResult = [ordered]@{
                state = 'NOT_RUN_USER_BOUNDARY'
                scope = 'NO_EXECUTION'
            }
        }
        elseif ($runtimeControl -eq 'REORIENT') {
            $basisProperty = `
                $event.payload.PSObject.Properties[
                    'new_execution_basis'
                ]

            if (
                $null -eq $basisProperty -or
                [string]::IsNullOrWhiteSpace(
                    [string]$basisProperty.Value
                )
            ) {
                throw 'RUNTIME_REORIENTATION_BASIS_MISSING'
            }

            $decision = [ordered]@{
                decision_id = New-CerebroRuntimeId `
                    -Prefix 'DECISION' `
                    -Material (
                        "$instanceId|$($event.event_id)|REORIENT"
                    )
                runtime_instance_id = $instanceId
                event_id = [string]$event.event_id
                initial_state = 'ACTIVE_READY'
                selected_transition = 'REORIENTATION_REQUIRED'
                target_state = 'REORIENTING'
                capability_binding_ref = $null
                decision_basis = 'event.payload.runtime_control'
                resolved_at = [DateTime]::UtcNow.ToString('o')
            }

            $instance.current_state = 'REORIENTING'
            $finalState = 'CONTROL_STOPPED'

            $verificationResult = [ordered]@{
                state = 'NOT_RUN_REORIENTATION'
                scope = 'NEW_EXECUTION_BASIS_REQUIRED'
            }
        }
        else {
            $matchingBindings = @()

            foreach ($bindingRef in @(
                $profile.capability_binding_refs
            )) {
                $candidate = @(
                    $bindings |
                    Where-Object {
                        [string]$_.binding_id -eq
                        [string]$bindingRef
                    }
                )[0]

                $inputContract = `
                    Get-CerebroRuntimeProperty `
                        -Object $candidate `
                        -Name 'input_contract' `
                        -Context (
                            'CAPABILITY_BINDING:' +
                            $bindingRef
                        )

                $eventTypesProperty = `
                    $inputContract.PSObject.Properties[
                        'event_types'
                    ]

                if (
                    $null -ne $eventTypesProperty -and
                    @($eventTypesProperty.Value) -contains
                    [string]$event.event_type
                ) {
                    $matchingBindings += $candidate
                }
            }

            if ($matchingBindings.Count -eq 0) {
                throw 'RUNTIME_NO_VALID_TRANSITION'
            }

            if ($matchingBindings.Count -ne 1) {
                throw 'RUNTIME_TRANSITION_NOT_DETERMINISTIC'
            }

            $binding = $matchingBindings[0]

            $decision = [ordered]@{
                decision_id = New-CerebroRuntimeId `
                    -Prefix 'DECISION' `
                    -Material (
                        "$instanceId|$($event.event_id)|" +
                        $binding.binding_id
                    )
                runtime_instance_id = $instanceId
                event_id = [string]$event.event_id
                initial_state = 'ACTIVE_READY'
                selected_transition = (
                    'EVENT:' +
                    $event.event_type +
                    ':BIND:' +
                    $binding.binding_id
                )
                target_state = 'DISPATCHING'
                capability_binding_ref = `
                    [string]$binding.binding_id
                decision_basis = `
                    'single-declared-binding-match'
                resolved_at = `
                    [DateTime]::UtcNow.ToString('o')
            }

            # DISPATCH
            $failureStage = 'DISPATCH'
            $instance.current_state = 'DISPATCHING'

            $payloadRequiredProperty = `
                $binding.input_contract.PSObject.Properties['payload_required']

            if (
                $null -ne $payloadRequiredProperty -and
                [bool]$payloadRequiredProperty.Value -and
                $null -eq $event.payload
            ) {
                throw 'RUNTIME_INPUT_PAYLOAD_REQUIRED'
            }

            $requiredPayloadFieldsProperty = `
                $binding.input_contract.PSObject.Properties['required_payload_fields']

            if ($null -ne $requiredPayloadFieldsProperty) {
                foreach ($requiredPayloadField in @($requiredPayloadFieldsProperty.Value)) {
                    $requiredPayloadFieldName = [string]$requiredPayloadField
                    $payloadFieldProperty = `
                        $event.payload.PSObject.Properties[$requiredPayloadFieldName]

                    if ($null -eq $payloadFieldProperty) {
                        throw (
                            'RUNTIME_INPUT_REQUIRED_FIELD_MISSING:' +
                            [string]$requiredPayloadField
                        )
                    }

                    if (
                        $payloadFieldProperty.Value -is [string] -and
                        [string]::IsNullOrWhiteSpace([string]$payloadFieldProperty.Value)
                    ) {
                        throw (
                            'RUNTIME_INPUT_REQUIRED_FIELD_EMPTY:' +
                            [string]$requiredPayloadField
                        )
                    }
                }
            }
            $authorityRequirement = `
                [string]$binding.authority_requirement

            if (
                $authorityRequirement -ne 'ANY' -and
                $authorityRequirement -ne
                [string]$event.authority
            ) {
                throw 'RUNTIME_BINDING_AUTHORITY_REJECTED'
            }

            # EXECUTE
            $failureStage = 'EXECUTE'
            $instance.current_state = 'EXECUTING'

            switch ([string]$binding.implementation_ref) {
                'builtin:echo' {
                    $executionResult = [ordered]@{
                        state = 'SUCCESS'
                        result_type = 'ECHO_RESULT'
                        payload = $event.payload
                        side_effects = @()
                    }
                }

                'builtin:idea-capture' {
                    if (
                        @($binding.allowed_side_effects) -notcontains
                        'runtime-artifact:ideas'
                    ) {
                        throw 'IDEA_CAPTURE_SIDE_EFFECT_NOT_DECLARED'
                    }

                    $content = [string]$event.payload.content
                    if ([string]::IsNullOrWhiteSpace($content)) {
                        throw 'IDEA_CAPTURE_CONTENT_EMPTY'
                    }

                    $stateDirectory = Split-Path -Parent ([IO.Path]::GetFullPath($StatePath))
                    if ((Split-Path -Leaf $stateDirectory) -eq 'active') {
                        $runtimeRoot = Split-Path -Parent $stateDirectory
                    }
                    else {
                        $runtimeRoot = $stateDirectory
                    }

                    $ideaRoot = Join-Path $runtimeRoot 'ideas'
                    [IO.Directory]::CreateDirectory($ideaRoot) | Out-Null

                    $ideaId = New-CerebroRuntimeId `
                        -Prefix 'IDEA' `
                        -Material (
                            '{0}|{1}' -f
                            [string]$event.event_id,
                            $content
                        )

                    $ideaPath = Join-Path $ideaRoot ($ideaId + '.json')
                    if (Test-Path -LiteralPath $ideaPath -PathType Leaf) {
                        throw ('IDEA_OBJECT_ID_COLLISION:' + $ideaId)
                    }

                    $ideaObject = [ordered]@{
                        schema = 'cerebro-idea-object/v0.1'
                        idea_id = $ideaId
                        created_at = [string]$event.issued_at
                        source = [string]$event.source
                        authority = 'CAPTURED_NON_AUTHORITATIVE'
                        content = $content
                        correlation_ref = [string]$event.correlation_ref
                        state = 'CAPTURED'
                    }

                    Write-CerebroRuntimeJson `
                        -Path $ideaPath `
                        -Value $ideaObject

                    $ideaHash = Get-CerebroRuntimeFileSha256 `
                        -Path $ideaPath

                    $evidenceRefs += ('idea-object-sha256:' + $ideaHash)

                    $executionResult = [ordered]@{
                        state = 'SUCCESS'
                        result_type = 'IDEA_OBJECT_CAPTURED'
                        idea_id = $ideaId
                        object_path = $ideaPath
                        object_sha256 = $ideaHash
                        side_effects = @($ideaPath)
                    }
                }
                'builtin:control-stop' {
                    $executionResult = [ordered]@{
                        state = 'CONTROL_STOP'
                        result_type = 'CONTROL_STOP'
                        reason = 'DECLARED_BINDING_CONTROL_STOP'
                        side_effects = @()
                    }

                    $finalState = 'CONTROL_STOPPED'

                    $verificationResult = [ordered]@{
                        state = 'NOT_RUN_CONTROL_STOP'
                        scope = 'DECLARED_CONTROL_STOP'
                    }
                }

                default {
                    throw (
                        'RUNTIME_IMPLEMENTATION_REF_UNRESOLVED:' +
                        $binding.implementation_ref
                    )
                }
            }

            if (-not $finalState) {
                # VERIFY
                $failureStage = 'VERIFY'
                $instance.current_state = 'VERIFYING'

                $expectedResultType = `
                    [string]$binding.output_contract.result_type

                if (
                    [string]$executionResult.state -ne
                    'SUCCESS'
                ) {
                    throw 'RUNTIME_EXECUTION_NOT_SUCCESSFUL'
                }

                if (
                    [string]$executionResult.result_type -ne
                    $expectedResultType
                ) {
                    throw 'RUNTIME_OUTPUT_CONTRACT_FAILED'
                }

                $verificationMode = `
                    [string]$binding.verification_policy.mode

                $verificationScope = 'EXECUTION_RESULT'

                if (
                    $verificationMode -eq
                    'exact-payload-echo'
                ) {
                    $verificationScope = 'EXACT_PAYLOAD_ECHO'
                    $inputJson = (
                        $event.payload |
                        ConvertTo-Json -Depth 32 -Compress
                    )

                    $outputJson = (
                        $executionResult.payload |
                        ConvertTo-Json -Depth 32 -Compress
                    )

                    if ($inputJson -ne $outputJson) {
                        throw 'RUNTIME_VERIFICATION_PAYLOAD_MISMATCH'
                    }
                }
                elseif (
                    $verificationMode -eq
                    'idea-object-persisted'
                ) {
                    $verificationScope = 'IDEA_OBJECT_PERSISTENCE_AND_CONTENT'

                    Assert-CerebroRuntimeFields `
                        -Object $executionResult `
                        -Fields @('idea_id','object_path','object_sha256') `
                        -Context 'IDEA_CAPTURE_EXECUTION_RESULT'

                    $ideaPath = [string]$executionResult.object_path
                    if (-not (Test-Path -LiteralPath $ideaPath -PathType Leaf)) {
                        throw 'IDEA_OBJECT_VERIFICATION_FILE_MISSING'
                    }

                    $actualIdeaHash = Get-CerebroRuntimeFileSha256 -Path $ideaPath
                    if ($actualIdeaHash -ne [string]$executionResult.object_sha256) {
                        throw 'IDEA_OBJECT_VERIFICATION_HASH_MISMATCH'
                    }

                    $ideaObject = Read-CerebroRuntimeJson `
                        -Path $ideaPath `
                        -Context 'IDEA_OBJECT'

                    Assert-CerebroRuntimeFields `
                        -Object $ideaObject `
                        -Fields @('schema','idea_id','created_at','source','authority','content','correlation_ref','state') `
                        -Context 'IDEA_OBJECT'

                    if ([string]$ideaObject.schema -ne 'cerebro-idea-object/v0.1') {
                        throw 'IDEA_OBJECT_SCHEMA_INVALID'
                    }
                    if ([string]$ideaObject.idea_id -ne [string]$executionResult.idea_id) {
                        throw 'IDEA_OBJECT_ID_MISMATCH'
                    }
                    if ([string]$ideaObject.content -ne [string]$event.payload.content) {
                        throw 'IDEA_OBJECT_CONTENT_MISMATCH'
                    }
                    if ([string]$ideaObject.source -ne [string]$event.source) {
                        throw 'IDEA_OBJECT_SOURCE_MISMATCH'
                    }
                    if ([string]$ideaObject.authority -ne 'CAPTURED_NON_AUTHORITATIVE') {
                        throw 'IDEA_OBJECT_AUTHORITY_MISMATCH'
                    }
                    if ([string]$ideaObject.correlation_ref -ne [string]$event.correlation_ref) {
                        throw 'IDEA_OBJECT_CORRELATION_MISMATCH'
                    }
                    if ([string]$ideaObject.state -ne 'CAPTURED') {
                        throw 'IDEA_OBJECT_STATE_INVALID'
                    }
                }
                elseif (
                    $verificationMode -eq
                    'result-state-only'
                ) {
                    $verificationScope = 'RESULT_STATE_ONLY'
                }
                else {
                    throw 'RUNTIME_VERIFICATION_POLICY_UNSUPPORTED'
                }

                $verificationResult = [ordered]@{
                    state = 'PASSED'
                    policy = $verificationMode
                    scope = $verificationScope
                    execution_state = `
                        [string]$executionResult.state
                }

                $finalState = 'COMPLETED'
            }
        }

        # REDUCE_STATE
        $failureStage = 'REDUCE_STATE'
        $instance.current_state = $finalState

        $projections = New-CerebroRuntimeProjections `
            -Instance $instance `
            -Profile $profile `
            -ReleaseId $releaseId `
            -SourceIdentity $sourceIdentity `
            -Event $event `
            -Decision $decision `
            -Binding $binding `
            -ExecutionResult $executionResult `
            -VerificationResult $verificationResult `
            -EvidenceRefs $evidenceRefs `
            -FailureRefs $failureRefs `
            -ReceiptPath $ReceiptPath `
            -StartupStage 'REDUCED' `
            -FinalState $finalState

        $state = [ordered]@{
            schema = 'cerebro-runtime-derived-state/v0.1'
            authority = 'derived_non_authoritative'
            runtime_instance = $instance
            last_terminal_state = $finalState
            last_event_id = [string]$event.event_id
            last_receipt_path = $ReceiptPath
            projections = $projections
        }

        Write-CerebroRuntimeJson `
            -Path $StatePath `
            -Value $state

        # WRITE_RECEIPT
        $failureStage = 'WRITE_RECEIPT'

        $completedAt = [DateTime]::UtcNow.ToString('o')

        $receiptId = New-CerebroRuntimeId `
            -Prefix 'RECEIPT' `
            -Material (
                '{0}|{1}|{2}|{3}' -f
                $instance.runtime_instance_id,
                $event.event_id,
                $startedAt,
                $finalState
            )

        $receipt = [ordered]@{
            receipt_id = $receiptId
            runtime_instance_id = `
                $instance.runtime_instance_id
            runtime_version = '0.1'
            profile_id = [string]$profile.profile_id
            release_id = $releaseId
            source_identity = $sourceIdentity
            event_id = [string]$event.event_id
            initial_state = $initialState
            transition_decision = $decision
            capability_binding = $binding
            execution_result = $executionResult
            verification_result = $verificationResult
            final_state = $finalState
            projection_refs = @(
                $instance.projection_refs
            )
            evidence_refs = @($evidenceRefs)
            failure_refs = @($failureRefs)
            started_at = $startedAt
            completed_at = $completedAt
            projections = $projections
        }

        Write-CerebroRuntimeJson `
            -Path $ReceiptPath `
            -Value $receipt

        return [pscustomobject]@{
            state = $finalState
            receipt_id = $receiptId
            receipt_path = $ReceiptPath
            state_path = $StatePath
            release_id = $releaseId
            runtime_instance_id = `
                $instance.runtime_instance_id
            verification_state = `
                [string]$verificationResult.state
        }
    }
    catch {
        $failureMessage = $_.Exception.Message
        $finalState = 'FAILED_CLOSED'

        $failureId = New-CerebroRuntimeId `
            -Prefix 'FAILURE' `
            -Material (
                '{0}|{1}|{2}' -f
                $failureStage,
                $failureMessage,
                $startedAt
            )

        if ($null -eq $profile) {
            $profile = [pscustomobject]@{
                profile_id = 'UNRESOLVED'
                capability_binding_refs = @()
            }
        }

        if ($null -eq $event) {
            $event = [pscustomobject]@{
                event_id = 'UNRESOLVED'
                event_type = 'UNRESOLVED'
                correlation_ref = 'UNRESOLVED'
            }
        }

        if ($null -eq $instance) {
            $instanceId = New-CerebroRuntimeId `
                -Prefix 'RUNTIME' `
                -Material (
                    'FAILED|' +
                    $startedAt +
                    '|' +
                    $failureMessage
                )

            $instance = [ordered]@{
                runtime_instance_id = $instanceId
                profile_id = [string]$profile.profile_id
                release_id = $releaseId
                source_identity = $sourceIdentity
                created_at = $startedAt
                current_state = 'FAILED_CLOSED'
                projection_refs = @(
                    'receipt://projections/IDENTITY',
                    'receipt://projections/BOOT',
                    'receipt://projections/GOVERNANCE',
                    'receipt://projections/SESSION',
                    'receipt://projections/EXECUTION',
                    'receipt://projections/EVIDENCE'
                )
                active_event_id = [string]$event.event_id
                failure_ledger_ref = $FailureLedgerPath
            }
        }
        else {
            $instance.current_state = 'FAILED_CLOSED'
        }

        $failure = [ordered]@{
            failure_id = $failureId
            runtime_instance_id = `
                $instance.runtime_instance_id
            event_id = [string]$event.event_id
            failure_stage = $failureStage
            failure_class = 'RUNTIME_CONTRACT_OR_EXECUTION_FAILURE'
            failure_code = $failureMessage
            message = $failureMessage
            evidence_refs = @($evidenceRefs)
            mutation_state = 'NO_SOURCE_MUTATION'
            authority_state = 'PRESERVED'
            recorded_at = [DateTime]::UtcNow.ToString('o')
            failure_family = $failureStage
            prior_failure_refs = @()
            reorientation_required = $false
            new_implementation_identity_required = $false
        }

        $failureRefs = @($FailureLedgerPath)

        Write-CerebroRuntimeJson `
            -Path $FailureLedgerPath `
            -Value $failure

        $verificationResult = [ordered]@{
            state = 'FAILED'
            scope = $failureStage
            evidence = $failureMessage
        }

        $projections = New-CerebroRuntimeProjections `
            -Instance $instance `
            -Profile $profile `
            -ReleaseId $releaseId `
            -SourceIdentity $sourceIdentity `
            -Event $event `
            -Decision $decision `
            -Binding $binding `
            -ExecutionResult $executionResult `
            -VerificationResult $verificationResult `
            -EvidenceRefs $evidenceRefs `
            -FailureRefs $failureRefs `
            -ReceiptPath $ReceiptPath `
            -StartupStage $failureStage `
            -FinalState 'FAILED_CLOSED'

        $failedState = [ordered]@{
            schema = 'cerebro-runtime-derived-state/v0.1'
            authority = 'derived_non_authoritative'
            runtime_instance = $instance
            last_terminal_state = 'FAILED_CLOSED'
            last_event_id = [string]$event.event_id
            last_receipt_path = $ReceiptPath
            projections = $projections
        }

        try {
            Write-CerebroRuntimeJson `
                -Path $StatePath `
                -Value $failedState
        }
        catch {
            # receipt remains the authoritative evidence target
        }

        $completedAt = [DateTime]::UtcNow.ToString('o')

        $receiptId = New-CerebroRuntimeId `
            -Prefix 'RECEIPT' `
            -Material (
                '{0}|{1}|{2}|FAILED_CLOSED' -f
                $instance.runtime_instance_id,
                $event.event_id,
                $startedAt
            )

        $receipt = [ordered]@{
            receipt_id = $receiptId
            runtime_instance_id = `
                $instance.runtime_instance_id
            runtime_version = '0.1'
            profile_id = [string]$profile.profile_id
            release_id = $releaseId
            source_identity = $sourceIdentity
            event_id = [string]$event.event_id
            initial_state = $initialState
            transition_decision = $decision
            capability_binding = $binding
            execution_result = $executionResult
            verification_result = $verificationResult
            final_state = 'FAILED_CLOSED'
            projection_refs = @(
                $instance.projection_refs
            )
            evidence_refs = @($evidenceRefs)
            failure_refs = @($failureRefs)
            started_at = $startedAt
            completed_at = $completedAt
            projections = $projections
        }

        try {
            Write-CerebroRuntimeJson `
                -Path $ReceiptPath `
                -Value $receipt
        }
        catch {
            $emergencyPath = Join-Path `
                ([IO.Path]::GetTempPath()) `
                (
                    'CEREBRO_RUNTIME_EMERGENCY_RECEIPT_' +
                    $receiptId +
                    '.json'
                )

            Write-CerebroRuntimeJson `
                -Path $emergencyPath `
                -Value $receipt

            $ReceiptPath = $emergencyPath
        }

        return [pscustomobject]@{
            state = 'FAILED_CLOSED'
            receipt_id = $receiptId
            receipt_path = $ReceiptPath
            state_path = $StatePath
            failure_id = $failureId
            failure_stage = $failureStage
            failure_code = $failureMessage
            verification_state = 'FAILED'
        }
    }
}

function Invoke-CerebroRuntimeCanary {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$OutputRoot
    )

    if (
        Test-Path `
            -LiteralPath $OutputRoot
    ) {
        Remove-Item `
            -LiteralPath $OutputRoot `
            -Recurse `
            -Force
    }

    [IO.Directory]::CreateDirectory(
        $OutputRoot
    ) | Out-Null

    $releasePath = Join-Path `
        $OutputRoot `
        'release'

    [IO.Directory]::CreateDirectory(
        $releasePath
    ) | Out-Null

    [IO.File]::WriteAllText(
        (Join-Path $releasePath 'source-identity.txt'),
        "canary-source/PATCH-003`n",
        [Text.UTF8Encoding]::new($false)
    )

    $bindings = [ordered]@{
        schema = 'cerebro-runtime-capability-bindings/v0.1'
        bindings = @(
            [ordered]@{
                binding_id = 'BIND-CANARY-ECHO'
                capability_id = 'CANARY-ECHO'
                implementation_ref = 'builtin:echo'
                input_contract = [ordered]@{
                    event_types = @('CANARY_ECHO')
                    payload_required = $true
                }
                output_contract = [ordered]@{
                    result_type = 'ECHO_RESULT'
                    require_success = $true
                }
                allowed_side_effects = @()
                authority_requirement = 'TEST'
                timeout_policy = [ordered]@{
                    milliseconds = 1000
                }
                exit_policy = [ordered]@{
                    success_states = @('SUCCESS')
                }
                verification_policy = [ordered]@{
                    mode = 'exact-payload-echo'
                }
            }
        )
    }

    Write-CerebroRuntimeJson `
        -Path (
            Join-Path `
                $releasePath `
                'capability-bindings.json'
        ) `
        -Value $bindings

    $releaseDigest = `
        Get-CerebroRuntimeReleaseDigest `
            -ReleasePath $releasePath

    $profilePath = Join-Path `
        $OutputRoot `
        'runtime-profile.json'

    $profile = [ordered]@{
        profile_id = 'runtime-0.1-canary'
        runtime_version = '0.1'
        release_ref = "sha256:$releaseDigest"
        entrypoint = 'tooling/runtime-host/cerebro_runtime.ps1'
        supported_event_types = @(
            'CANARY_ECHO'
        )
        capability_binding_refs = @(
            'BIND-CANARY-ECHO'
        )
        projection_requirements = @(
            'IDENTITY',
            'BOOT',
            'GOVERNANCE',
            'SESSION',
            'EXECUTION',
            'EVIDENCE'
        )
        state_contract_ref = `
            'RUNTIME-STATE-TRANSITION-TABLE-001'
        receipt_contract_ref = `
            'RUNTIME-RECEIPT-CONTRACT-001'
        failure_policy_ref = `
            'FAILURE-LEDGER-CONTRACT-001'
    }

    Write-CerebroRuntimeJson `
        -Path $profilePath `
        -Value $profile

    $successEventPath = Join-Path `
        $OutputRoot `
        'event-success.json'

    $successEvent = [ordered]@{
        event_id = 'EVENT-CANARY-SUCCESS'
        event_type = 'CANARY_ECHO'
        issued_at = '2026-01-01T00:00:00Z'
        source = 'PATCH-003-CANARY'
        authority = 'TEST'
        payload = [ordered]@{
            message = 'runtime-0.1-success'
        }
        correlation_ref = 'PATCH-003'
    }

    Write-CerebroRuntimeJson `
        -Path $successEventPath `
        -Value $successEvent

    $success = Invoke-CerebroRuntimeCore `
        -ReleasePath $releasePath `
        -PinnedReleaseSha256 $releaseDigest `
        -RuntimeProfilePath $profilePath `
        -EventPath $successEventPath `
        -StatePath (
            Join-Path $OutputRoot 'success-state.json'
        ) `
        -ReceiptPath (
            Join-Path $OutputRoot 'success-receipt.json'
        ) `
        -FailureLedgerPath (
            Join-Path $OutputRoot 'success-failure-ledger.json'
        )

    if ($success.state -ne 'COMPLETED') {
        throw (
            'RUNTIME_SUCCESS_CANARY_FAILED:' +
            $success.state
        )
    }

    if (
        $success.verification_state -ne
        'PASSED'
    ) {
        throw (
            'RUNTIME_SUCCESS_VERIFICATION_FAILED:' +
            $success.verification_state
        )
    }

    $failureEventPath = Join-Path `
        $OutputRoot `
        'event-failure.json'

    $failureEvent = [ordered]@{
        event_id = 'EVENT-CANARY-FAILURE'
        event_type = 'UNSUPPORTED_EVENT'
        issued_at = '2026-01-01T00:00:01Z'
        source = 'PATCH-003-CANARY'
        authority = 'TEST'
        payload = [ordered]@{
            message = 'runtime-0.1-failure'
        }
        correlation_ref = 'PATCH-003'
    }

    Write-CerebroRuntimeJson `
        -Path $failureEventPath `
        -Value $failureEvent

    $failure = Invoke-CerebroRuntimeCore `
        -ReleasePath $releasePath `
        -PinnedReleaseSha256 $releaseDigest `
        -RuntimeProfilePath $profilePath `
        -EventPath $failureEventPath `
        -StatePath (
            Join-Path $OutputRoot 'failure-state.json'
        ) `
        -ReceiptPath (
            Join-Path $OutputRoot 'failure-receipt.json'
        ) `
        -FailureLedgerPath (
            Join-Path $OutputRoot 'failure-ledger.json'
        )

    if ($failure.state -ne 'FAILED_CLOSED') {
        throw (
            'RUNTIME_FAILURE_CANARY_FAILED:' +
            $failure.state
        )
    }

    if (
        -not (
            Test-Path `
                -LiteralPath (
                    Join-Path `
                        $OutputRoot `
                        'failure-ledger.json'
                ) `
                -PathType Leaf
        )
    ) {
        throw 'RUNTIME_FAILURE_LEDGER_CANARY_MISSING'
    }

    return [pscustomobject]@{
        state = 'VERIFIED'
        release_sha256 = $releaseDigest
        success_state = $success.state
        success_receipt = $success.receipt_path
        failure_state = $failure.state
        failure_receipt = $failure.receipt_path
        failure_ledger = (
            Join-Path `
                $OutputRoot `
                'failure-ledger.json'
        )
    }
}
