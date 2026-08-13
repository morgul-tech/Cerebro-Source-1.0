Set-StrictMode -Version 2.0

function New-CacFinding {
    param(
        [string]$Code,
        [string]$Scope,
        [string]$Subject,
        [string]$Message,
        [bool]$Blocking
    )
    [pscustomobject]@{
        code=$Code
        scope=$Scope
        subject=$Subject
        message=$Message
        blocking=$Blocking
    }
}

function Get-CacProperty {
    param($Object,[string]$PropertyName,$Default=$null)

    if($null -eq $Object){return $Default}
    if($Object.PSObject.Properties.Name -notcontains $PropertyName){return $Default}

    $value=$Object.$PropertyName
    if($null -eq $value){return $Default}
    return $value
}

function Get-CacOptionalValues {
    param($Object,[string]$PropertyName)

    $value=Get-CacProperty -Object $Object -PropertyName $PropertyName -Default $null
    if($null -eq $value){return @()}
    return @($value)
}

function Test-CacFileTokens {
    param(
        [string]$Root,
        [string]$RelativePath,
        [object[]]$Tokens,
        [string]$Binding,
        [string]$Stage
    )

    $findings=@()
    if([string]::IsNullOrWhiteSpace($RelativePath)){
        return @(New-CacFinding -Code 'CAC_PATH_MISSING' -Scope 'STRICT_CONTRACT' -Subject $Binding -Message ($Stage + ' path is missing.') -Blocking $true)
    }

    $full=Join-Path $Root ($RelativePath -replace '/','\')
    if(-not(Test-Path -LiteralPath $full -PathType Leaf)){
        return @(New-CacFinding -Code 'CAC_FILE_MISSING' -Scope 'STRICT_CONTRACT' -Subject $Binding -Message ($Stage + ' file missing: ' + $RelativePath) -Blocking $true)
    }

    $text=[IO.File]::ReadAllText($full)
    foreach($token in @($Tokens)){
        if(-not [string]::IsNullOrWhiteSpace([string]$token)){
            if(-not $text.Contains([string]$token)){
                $findings += New-CacFinding -Code 'CAC_TOKEN_MISSING' -Scope 'STRICT_CONTRACT' -Subject $Binding -Message ($Stage + ' token missing: ' + [string]$token) -Blocking $true
            }
        }
    }
    return @($findings)
}

function Get-CacSha256Text {
    param([string]$Text)
    $sha=[Security.Cryptography.SHA256]::Create()
    try {
        $bytes=[Text.Encoding]::UTF8.GetBytes($Text)
        return ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-','').ToLowerInvariant()
    }
    finally {$sha.Dispose()}
}

function Get-CacEvidenceBasisFingerprint {
    param([string]$Root,[object[]]$RelativePaths)
    $rows=@()
    foreach($relative in @($RelativePaths | Sort-Object)){
        $path=Join-Path $Root (([string]$relative) -replace '/','\\')
        if(-not(Test-Path -LiteralPath $path -PathType Leaf)){
            throw ('CAC_RUNTIME_EVIDENCE_BASIS_FILE_MISSING:{0}' -f [string]$relative)
        }
        $hash=(Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        $rows += ('{0}|{1}' -f [string]$relative,$hash)
    }
    return Get-CacSha256Text -Text ($rows -join "`n")
}

function Test-CacRuntimeEvidence {
    param([string]$Root,$Binding)
    $id=[string](Get-CacProperty $Binding 'id' '')
    $spec=Get-CacProperty $Binding 'runtime_evidence' $null
    if($null -eq $spec){
        return [pscustomobject]@{state='MISSING';findings=@(New-CacFinding -Code 'RUNTIME_EVIDENCE_SPEC_MISSING' -Scope 'RUNTIME_EVIDENCE' -Subject $id -Message 'RUNTIME_EVIDENCE binding requires runtime_evidence specification.' -Blocking $true)}
    }
    $path=[string](Get-CacProperty $spec 'path' '')
    if([string]::IsNullOrWhiteSpace($path)){
        return [pscustomobject]@{state='MISSING';findings=@(New-CacFinding -Code 'RUNTIME_EVIDENCE_PATH_MISSING' -Scope 'RUNTIME_EVIDENCE' -Subject $id -Message 'runtime_evidence.path is required.' -Blocking $true)}
    }
    $full=$path
    if(-not[IO.Path]::IsPathRooted($full)){$full=Join-Path $Root ($full -replace '/','\\')}
    if(-not(Test-Path -LiteralPath $full -PathType Leaf)){
        return [pscustomobject]@{state='MISSING';findings=@(New-CacFinding -Code 'RUNTIME_EVIDENCE_FILE_MISSING' -Scope 'RUNTIME_EVIDENCE' -Subject $id -Message ('Runtime evidence missing: ' + $path) -Blocking $true)}
    }
    try {$evidence=Get-Content -LiteralPath $full -Raw | ConvertFrom-Json}
    catch {
        return [pscustomobject]@{state='INVALID';findings=@(New-CacFinding -Code 'RUNTIME_EVIDENCE_JSON_INVALID' -Scope 'RUNTIME_EVIDENCE' -Subject $id -Message $_.Exception.Message -Blocking $true)}
    }
    $findings=@()
    $requiredSchema=[string](Get-CacProperty $spec 'schema' '')
    if(-not[string]::IsNullOrWhiteSpace($requiredSchema) -and [string](Get-CacProperty $evidence 'schema' '') -ne $requiredSchema){
        $findings += New-CacFinding -Code 'RUNTIME_EVIDENCE_SCHEMA_MISMATCH' -Scope 'RUNTIME_EVIDENCE' -Subject $id -Message 'Runtime evidence schema mismatch.' -Blocking $true
    }
    if([string](Get-CacProperty $evidence 'result' '') -ne 'PASS'){
        $findings += New-CacFinding -Code 'RUNTIME_EVIDENCE_RESULT_NOT_PASS' -Scope 'RUNTIME_EVIDENCE' -Subject $id -Message 'Runtime evidence result must be PASS.' -Blocking $true
    }
    $acceptedBindings=@(Get-CacOptionalValues $spec 'accepted_binding_ids')
    if($acceptedBindings.Count -gt 0 -and $acceptedBindings -notcontains [string](Get-CacProperty $evidence 'binding_id' '')){
        $findings += New-CacFinding -Code 'RUNTIME_EVIDENCE_BINDING_MISMATCH' -Scope 'RUNTIME_EVIDENCE' -Subject $id -Message 'Runtime evidence binding_id is not accepted.' -Blocking $true
    }
    $requiredProvesBinding=[string](Get-CacProperty $spec 'required_proves_binding' '')
    if(-not[string]::IsNullOrWhiteSpace($requiredProvesBinding)){
        $provesBindings=@(Get-CacOptionalValues $evidence 'proves_bindings')
        if($provesBindings -notcontains $requiredProvesBinding){
            $findings += New-CacFinding -Code 'RUNTIME_EVIDENCE_PROVEN_BINDING_MISSING' -Scope 'RUNTIME_EVIDENCE' -Subject $id -Message ('Runtime evidence does not prove binding: ' + $requiredProvesBinding) -Blocking $true
        }
    }
    $basisFiles=@(Get-CacOptionalValues $spec 'basis_files')
    try {$expectedFingerprint=Get-CacEvidenceBasisFingerprint -Root $Root -RelativePaths $basisFiles}
    catch {
        $findings += New-CacFinding -Code 'RUNTIME_EVIDENCE_BASIS_INVALID' -Scope 'RUNTIME_EVIDENCE' -Subject $id -Message $_.Exception.Message -Blocking $true
        $expectedFingerprint=''
    }
    if(-not[string]::IsNullOrWhiteSpace($expectedFingerprint) -and [string](Get-CacProperty $evidence 'source_state_fingerprint' '') -ne $expectedFingerprint){
        $findings += New-CacFinding -Code 'RUNTIME_EVIDENCE_STALE' -Scope 'RUNTIME_EVIDENCE' -Subject $id -Message 'Runtime evidence source-state fingerprint does not match installed Source.' -Blocking $true
    }
    foreach($field in @(Get-CacOptionalValues $spec 'required_true_fields')){
        if(-not[bool](Get-CacProperty $evidence ([string]$field) $false)){
            $findings += New-CacFinding -Code 'RUNTIME_EVIDENCE_REQUIRED_PROOF_MISSING' -Scope 'RUNTIME_EVIDENCE' -Subject $id -Message ('Required runtime proof is not true: ' + [string]$field) -Blocking $true
        }
    }
    return [pscustomobject]@{state=($(if($findings.Count -eq 0){'PROVEN'}else{'INVALID'}));findings=@($findings)}
}

function Get-CacRequiredStandards {
    param([string]$Root,[string]$StandardsManifest='standards/standards.yaml')

    $manifestFull=Join-Path $Root ($StandardsManifest -replace '/','\')
    if(-not(Test-Path -LiteralPath $manifestFull -PathType Leaf)){
        throw 'CAC_STANDARDS_MANIFEST_NOT_FOUND'
    }

    $lines=[IO.File]::ReadAllLines($manifestFull)
    $result=@()
    $currentId=''
    $currentPath=''

    foreach($line in $lines){
        if($line -match '^\s*-\s+id:\s*(.+?)\s*$'){
            $currentId=$matches[1].Trim()
            $currentPath=''
            continue
        }

        if(-not [string]::IsNullOrWhiteSpace($currentId) -and $line -match '^\s*path:\s*(.+?)\s*$'){
            $currentPath=$matches[1].Trim()
            continue
        }

        if(-not [string]::IsNullOrWhiteSpace($currentId) -and $line -match '^\s*required:\s*(true|false)\s*$'){
            if($matches[1].ToLowerInvariant() -eq 'true'){
                $result += [pscustomobject]@{
                    standard_id=$currentId
                    path=$currentPath
                }
            }
            $currentId=''
            $currentPath=''
        }
    }

    return @($result)
}

function Get-CacStrictContractStates {
    param([string]$Root,$Registry)

    $states=@()
    $findings=@()
    $allowedProofKinds=@('STATIC_CALLSITE','EXPLICIT_ADAPTER','RUNTIME_EVIDENCE')

    foreach($binding in @(Get-CacOptionalValues -Object $Registry -PropertyName 'bindings')){
        $id=[string](Get-CacProperty $binding 'id' '')
        $classification=[string](Get-CacProperty $binding 'classification' '')

        if($classification -ne 'OPERATIONAL'){
            $findings += New-CacFinding -Code 'STRICT_BINDING_NOT_OPERATIONAL' -Scope 'STRICT_CONTRACT' -Subject $id -Message ('Strict binding classification must be OPERATIONAL, got ' + $classification) -Blocking $true
            $states += [pscustomobject]@{binding=$id;state='ACTIVATION_GAP';proof_level='NONE';runtime_evidence='UNKNOWN'}
            continue
        }

        $local=@()
        $local += Test-CacFileTokens -Root $Root -RelativePath ([string](Get-CacProperty $binding 'declaration' '')) -Tokens (Get-CacOptionalValues $binding 'declaration_tokens') -Binding $id -Stage 'DECLARED'
        $local += Test-CacFileTokens -Root $Root -RelativePath ([string](Get-CacProperty $binding 'registration' '')) -Tokens (Get-CacOptionalValues $binding 'registration_token') -Binding $id -Stage 'REGISTERED'
        $local += Test-CacFileTokens -Root $Root -RelativePath ([string](Get-CacProperty $binding 'implementation' '')) -Tokens (Get-CacOptionalValues $binding 'implementation_tokens') -Binding $id -Stage 'IMPLEMENTED'
        $local += Test-CacFileTokens -Root $Root -RelativePath ([string](Get-CacProperty $binding 'wiring' '')) -Tokens (Get-CacOptionalValues $binding 'wiring_tokens') -Binding $id -Stage 'WIRED'
        $local += Test-CacFileTokens -Root $Root -RelativePath ([string](Get-CacProperty $binding 'validation' '')) -Tokens (Get-CacOptionalValues $binding 'validation_tokens') -Binding $id -Stage 'VALIDATED'

        $proofKind=[string](Get-CacProperty $binding 'wiring_proof_kind' '')
        if($allowedProofKinds -notcontains $proofKind){
            $local += New-CacFinding -Code 'ACTIVATION_WIRING_PROOF_MISSING' -Scope 'STRICT_CONTRACT' -Subject $id -Message ('Invalid or missing wiring_proof_kind: ' + $proofKind) -Blocking $true
        }

        $runtimeState=[string](Get-CacProperty $binding 'runtime_evidence_state' 'UNKNOWN')
        if($proofKind -eq 'RUNTIME_EVIDENCE'){
            $runtimeProof=Test-CacRuntimeEvidence -Root $Root -Binding $binding
            $local += @($runtimeProof.findings)
            $runtimeState=[string]$runtimeProof.state
        }
        if(@($local).Count -eq 0){
            $states += [pscustomobject]@{
                binding=$id
                standard_id=[string](Get-CacProperty $binding 'standard_id' '')
                state='OPERATIONAL'
                proof_level=$proofKind
                runtime_evidence=$runtimeState
            }
        }
        else {
            $findings += @($local)
            $states += [pscustomobject]@{
                binding=$id
                standard_id=[string](Get-CacProperty $binding 'standard_id' '')
                state='ACTIVATION_GAP'
                proof_level=$proofKind
                runtime_evidence=$runtimeState
            }
        }
    }

    return [pscustomobject]@{
        states=@($states)
        findings=@($findings)
    }
}

function Get-CacClassificationCoverage {
    param([string]$Root,$Registry,$StrictStates)

    $required=@(Get-CacRequiredStandards -Root $Root)
    $entries=@(Get-CacOptionalValues -Object $Registry -PropertyName 'standard_classifications')
    $findings=@()
    $inventory=@()
    $allowed=@(
        'OPERATIONAL',
        'POLICY_ONLY',
        'DORMANT',
        'SUPERSEDED',
        'PARTIALLY_OPERATIONAL',
        'ACTIVATION_GAP',
        'SEMANTIC_REVIEW_REQUIRED'
    )

    $byId=@{}
    foreach($entry in $entries){
        $id=[string](Get-CacProperty $entry 'standard_id' '')
        if([string]::IsNullOrWhiteSpace($id)){
            $findings += New-CacFinding -Code 'ACTIVATION_CLASSIFICATION_ID_MISSING' -Scope 'STANDARD_COVERAGE' -Subject '' -Message 'standard_id is required.' -Blocking $true
            continue
        }
        if($byId.ContainsKey($id)){
            $findings += New-CacFinding -Code 'ACTIVATION_CLASSIFICATION_DUPLICATE' -Scope 'STANDARD_COVERAGE' -Subject $id -Message 'Required standard has duplicate classifications.' -Blocking $true
            continue
        }
        $byId[$id]=$entry
    }

    foreach($standard in $required){
        $id=[string]$standard.standard_id
        $path=[string]$standard.path

        if(-not $byId.ContainsKey($id)){
            $findings += New-CacFinding -Code 'ACTIVATION_CLASSIFICATION_COVERAGE_GAP' -Scope 'STANDARD_COVERAGE' -Subject $id -Message 'Required standard has no activation classification.' -Blocking $true
            $inventory += [pscustomobject]@{standard_id=$id;path=$path;classification='UNCLASSIFIED';state='BLOCKED'}
            continue
        }

        $entry=$byId[$id]
        $classification=[string](Get-CacProperty $entry 'classification' '')
        $declaredPath=[string](Get-CacProperty $entry 'path' '')

        if($allowed -notcontains $classification){
            $findings += New-CacFinding -Code 'ACTIVATION_CLASSIFICATION_UNKNOWN' -Scope 'STANDARD_COVERAGE' -Subject $id -Message ('Unknown classification: ' + $classification) -Blocking $true
        }

        if($declaredPath -ne $path){
            $findings += New-CacFinding -Code 'ACTIVATION_CLASSIFICATION_PATH_MISMATCH' -Scope 'STANDARD_COVERAGE' -Subject $id -Message ('expected=' + $path + '; classified=' + $declaredPath) -Blocking $true
        }

        $state=$classification

        if($classification -eq 'OPERATIONAL'){
            $refs=@(Get-CacOptionalValues $entry 'strict_contract_refs')
            if($refs.Count -eq 0){
                $findings += New-CacFinding -Code 'OPERATIONAL_STANDARD_WITHOUT_STRICT_CONTRACT' -Scope 'STANDARD_COVERAGE' -Subject $id -Message 'OPERATIONAL standard requires strict_contract_refs.' -Blocking $true
            }
            foreach($ref in $refs){
                $matched=@($StrictStates | Where-Object {$_.binding -eq [string]$ref})
                if($matched.Count -ne 1 -or [string]$matched[0].state -ne 'OPERATIONAL'){
                    $findings += New-CacFinding -Code 'OPERATIONAL_STANDARD_STRICT_CONTRACT_NOT_PROVEN' -Scope 'STANDARD_COVERAGE' -Subject $id -Message ('Strict contract not OPERATIONAL: ' + [string]$ref) -Blocking $true
                }
            }
        }
        elseif($classification -eq 'DORMANT'){
            if([string]::IsNullOrWhiteSpace([string](Get-CacProperty $entry 'activation_condition' ''))){
                $findings += New-CacFinding -Code 'DORMANT_ACTIVATION_CONDITION_MISSING' -Scope 'STANDARD_COVERAGE' -Subject $id -Message 'DORMANT requires activation_condition.' -Blocking $true
            }
        }
        elseif($classification -eq 'SUPERSEDED'){
            if([string]::IsNullOrWhiteSpace([string](Get-CacProperty $entry 'replacement_ref' ''))){
                $findings += New-CacFinding -Code 'SUPERSEDED_REPLACEMENT_MISSING' -Scope 'STANDARD_COVERAGE' -Subject $id -Message 'SUPERSEDED requires replacement_ref.' -Blocking $true
            }
        }
        elseif($classification -eq 'PARTIALLY_OPERATIONAL' -or $classification -eq 'ACTIVATION_GAP'){
            if(@(Get-CacOptionalValues $entry 'known_gaps').Count -eq 0){
                $findings += New-CacFinding -Code 'ACTIVATION_DEBT_DETAIL_MISSING' -Scope 'STANDARD_COVERAGE' -Subject $id -Message ($classification + ' requires known_gaps.') -Blocking $true
            }
        }
        elseif($classification -eq 'SEMANTIC_REVIEW_REQUIRED'){
            if([string]::IsNullOrWhiteSpace([string](Get-CacProperty $entry 'review_scope' ''))){
                $findings += New-CacFinding -Code 'SEMANTIC_REVIEW_SCOPE_MISSING' -Scope 'STANDARD_COVERAGE' -Subject $id -Message 'SEMANTIC_REVIEW_REQUIRED requires review_scope.' -Blocking $true
            }
        }

        $inventory += [pscustomobject]@{
            standard_id=$id
            path=$path
            classification=$classification
            rationale=[string](Get-CacProperty $entry 'rationale' '')
            known_gaps=@(Get-CacOptionalValues $entry 'known_gaps')
            review_scope=[string](Get-CacProperty $entry 'review_scope' '')
            strict_contract_refs=@(Get-CacOptionalValues $entry 'strict_contract_refs')
        }
    }

    return [pscustomobject]@{
        required_count=$required.Count
        classified_count=@($inventory | Where-Object {$_.classification -ne 'UNCLASSIFIED'}).Count
        inventory=@($inventory)
        findings=@($findings)
    }
}

function Get-CacCanonicalResponsibilityFindings {
    param($Registry)

    $findings=@()
    $responsibilities=@()

    foreach($responsibility in @(Get-CacOptionalValues $Registry 'canonical_responsibilities')){
        $id=[string](Get-CacProperty $responsibility 'id' '')
        $claims=@(Get-CacOptionalValues $responsibility 'claims')
        $canonical=@($claims | Where-Object {[bool](Get-CacProperty $_ 'canonical' $false)})
        $expected=[int](Get-CacProperty $responsibility 'expected_canonical_claims' 1)
        $blocking=[bool](Get-CacProperty $responsibility 'blocking' $true)
        $state='PASS'

        if($canonical.Count -gt $expected){
            $state='CANONICAL_CONFLICT'
            $findings += New-CacFinding -Code 'CANONICAL_IMPLEMENTATION_CONFLICT' -Scope 'CANONICAL_RESPONSIBILITY' -Subject $id -Message ('canonical_claims=' + $canonical.Count + '; expected=' + $expected) -Blocking $blocking
        }
        elseif($canonical.Count -lt $expected){
            $state='CANONICAL_OWNER_MISSING'
            $findings += New-CacFinding -Code 'CANONICAL_OWNER_MISSING' -Scope 'CANONICAL_RESPONSIBILITY' -Subject $id -Message ('canonical_claims=' + $canonical.Count + '; expected=' + $expected) -Blocking $blocking
        }

        $responsibilities += [pscustomobject]@{
            id=$id
            state=$state
            expected_canonical_claims=$expected
            canonical_claim_count=$canonical.Count
            blocking=$blocking
            claims=@($claims)
        }
    }

    return [pscustomobject]@{
        responsibilities=@($responsibilities)
        findings=@($findings)
    }
}

function Get-CacKnownDebtFindings {
    param($Registry)

    $findings=@()
    $debt=@()

    foreach($item in @(Get-CacOptionalValues $Registry 'known_activation_debt')){
        $blocking=[bool](Get-CacProperty $item 'blocking' $false)
        $id=[string](Get-CacProperty $item 'id' '')
        $classification=[string](Get-CacProperty $item 'classification' '')
        $rationale=[string](Get-CacProperty $item 'rationale' '')

        if([string]::IsNullOrWhiteSpace($rationale)){
            $findings += New-CacFinding -Code 'ACTIVATION_DEBT_RATIONALE_MISSING' -Scope 'ACTIVATION_DEBT' -Subject $id -Message 'Known activation debt requires rationale.' -Blocking $true
        }
        else {
            $findings += New-CacFinding -Code $classification -Scope 'ACTIVATION_DEBT' -Subject $id -Message $rationale -Blocking $blocking
        }

        $debt += $item
    }

    return [pscustomobject]@{
        debt=@($debt)
        findings=@($findings)
    }
}

function Get-CacReferenceCandidates {
    param([string]$Root,$StandardInventory)

    $candidates=@()
    foreach($standard in @($StandardInventory)){
        $relative=[string]$standard.path
        $full=Join-Path $Root ($relative -replace '/','\')
        if(-not(Test-Path -LiteralPath $full -PathType Leaf)){continue}

        $text=[IO.File]::ReadAllText($full)
        $signals=@()

        foreach($pattern in @(
            'canonical_implementation:\s*([^\r\n#]+)',
            'implementation_ref:\s*([^\r\n#]+)',
            'implementation:\s*(tooling/[^\r\n#]+)'
        )){
            foreach($match in [regex]::Matches($text,$pattern)){
                $target=$match.Groups[1].Value.Trim().Trim('"').Trim("'")
                if($target -match '^(tooling|engines|modules|mcp)/'){
                    $signals += $target
                }
            }
        }

        foreach($match in [regex]::Matches($text,'(?m)(tooling/[A-Za-z0-9_./\\-]+\.(?:ps1|py|json|yaml))')){
            $signals += $match.Groups[1].Value
        }

        $signals=@($signals | Select-Object -Unique)
        if($signals.Count -eq 0){continue}

        $missing=@()
        foreach($signal in $signals){
            $signalFull=Join-Path $Root ($signal -replace '/','\')
            if(-not(Test-Path -LiteralPath $signalFull -PathType Leaf)){
                $missing += $signal
            }
        }

        $candidates += [pscustomobject]@{
            standard_id=[string]$standard.standard_id
            declaration=$relative
            activation_classification=[string]$standard.classification
            referenced_implementations=$signals
            missing_references=$missing
            reference_integrity=if($missing.Count -eq 0){'PASS'}else{'MISSING_REFERENCE'}
            operational_proof='NOT_PROVEN_BY_REFERENCE_EXISTENCE'
        }
    }

    return @($candidates)
}

function Invoke-CerebroContractActivationClosure {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Root,
        [string]$RegistryPath='tooling/validator/contract-activation-bindings.json',
        [switch]$PassThru
    )

    $rootPath=[IO.Path]::GetFullPath($Root)
    $registryFull=Join-Path $rootPath ($RegistryPath -replace '/','\')
    if(-not(Test-Path -LiteralPath $registryFull -PathType Leaf)){
        throw ('CAC_REGISTRY_NOT_FOUND:{0}' -f $registryFull)
    }

    $registry=Get-Content -LiteralPath $registryFull -Raw | ConvertFrom-Json

    $strict=Get-CacStrictContractStates -Root $rootPath -Registry $registry
    $coverage=Get-CacClassificationCoverage -Root $rootPath -Registry $registry -StrictStates $strict.states
    $canonical=Get-CacCanonicalResponsibilityFindings -Registry $registry
    $debt=Get-CacKnownDebtFindings -Registry $registry

    $allFindings=@($strict.findings)+@($coverage.findings)+@($canonical.findings)+@($debt.findings)
    $blocking=@($allFindings | Where-Object {$_.blocking -eq $true})
    $nonblocking=@($allFindings | Where-Object {$_.blocking -ne $true})

    $counts=@{}
    foreach($item in @($coverage.inventory)){
        $key=[string]$item.classification
        if(-not $counts.ContainsKey($key)){$counts[$key]=0}
        $counts[$key]=[int]$counts[$key]+1
    }

    $result=if($blocking.Count -eq 0){'PASS'}else{'FAIL'}
    $health=if($blocking.Count -gt 0){'BLOCKED'}elseif($nonblocking.Count -gt 0 -or @($coverage.inventory | Where-Object {$_.classification -ne 'OPERATIONAL' -and $_.classification -ne 'POLICY_ONLY'}).Count -gt 0){'REVIEW_REQUIRED'}else{'CLEAN'}

    $object=[pscustomobject]@{
        schema='cerebro-contract-activation-closure-result/v2'
        result=$result
        health=$health
        proof_semantics='SOURCE_ACTIVATION_STATIC_OR_DECLARED_PROOF;RUNTIME_EXECUTION_IS_SEPARATE'
        required_standard_count=$coverage.required_count
        classified_standard_count=$coverage.classified_count
        unclassified_standard_count=$coverage.required_count-$coverage.classified_count
        classification_counts=$counts
        standard_inventory=@($coverage.inventory)
        strict_contract_states=@($strict.states)
        canonical_responsibilities=@($canonical.responsibilities)
        known_activation_debt=@($debt.debt)
        blocking_findings=@($blocking)
        nonblocking_findings=@($nonblocking)
    }

    if($PassThru){return $object}
    $object | ConvertTo-Json -Depth 16

    if($result -ne 'PASS'){
        throw ('CONTRACT_ACTIVATION_CLOSURE_V2_FAILED:{0}' -f $blocking.Count)
    }
}

function Invoke-CerebroContractActivationAudit {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Root,
        [string]$OutputPath='D:\Cerebro\Run\Evidence\Audits\CEREBRO_CONTRACT_ACTIVATION_AUDIT.json'
    )

    $rootPath=[IO.Path]::GetFullPath($Root)
    $closure=Invoke-CerebroContractActivationClosure -Root $rootPath -PassThru
    $references=Get-CacReferenceCandidates -Root $rootPath -StandardInventory $closure.standard_inventory

    $output=[ordered]@{
        schema='cerebro-contract-activation-audit/v2'
        generated_at_utc=[DateTime]::UtcNow.ToString('o')
        source_root=$rootPath
        result=$closure.result
        health=$closure.health
        proof_semantics=$closure.proof_semantics
        required_standard_count=$closure.required_standard_count
        classified_standard_count=$closure.classified_standard_count
        unclassified_standard_count=$closure.unclassified_standard_count
        classification_counts=$closure.classification_counts
        standard_inventory=$closure.standard_inventory
        strict_contract_states=$closure.strict_contract_states
        canonical_responsibilities=$closure.canonical_responsibilities
        known_activation_debt=$closure.known_activation_debt
        blocking_findings=$closure.blocking_findings
        nonblocking_findings=$closure.nonblocking_findings
        reference_candidates=$references
        note='Reference existence is supplemental evidence only and never proves OPERATIONAL by itself.'
    }

    [IO.Directory]::CreateDirectory((Split-Path -Parent $OutputPath)) | Out-Null
    [IO.File]::WriteAllText(
        $OutputPath,
        (($output | ConvertTo-Json -Depth 16)+"`r`n"),
        [Text.UTF8Encoding]::new($false)
    )

    return [pscustomobject]$output
}
