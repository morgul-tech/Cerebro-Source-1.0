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


function Get-CacCanonicalRows {
    param($Value,[string]$Path='$')
    if($null -eq $Value){return @($Path+'|null')}
    if($Value -is [string]){return @($Path+'|string|'+($Value|ConvertTo-Json -Compress))}
    if($Value -is [bool]){return @($Path+'|bool|'+$Value.ToString().ToLowerInvariant())}
    if($Value -is [ValueType]){
        return @($Path+'|value|'+([Convert]::ToString($Value,[Globalization.CultureInfo]::InvariantCulture)))
    }
    $rows=@()
    if($Value -is [Collections.IDictionary]){
        $keys=@($Value.Keys|ForEach-Object{[string]$_}|Sort-Object)
        $rows += ($Path+'|object|'+$keys.Count)
        foreach($key in $keys){$rows += @(Get-CacCanonicalRows -Value $Value[$key] -Path ($Path+'.'+$key))}
        return @($rows)
    }
    if($Value -is [Collections.IEnumerable]){
        $items=@($Value); $rows += ($Path+'|array|'+$items.Count)
        for($i=0;$i -lt $items.Count;$i++){$rows += @(Get-CacCanonicalRows -Value $items[$i] -Path ($Path+'['+$i+']'))}
        return @($rows)
    }

    $names=@($Value.PSObject.Properties.Name|Sort-Object)
    $rows += ($Path+'|object|'+$names.Count)
    foreach($name in $names){$rows += @(Get-CacCanonicalRows -Value $Value.$name -Path ($Path+'.'+$name))}
    return @($rows)
}

function Get-CacPermanenceSnapshotFingerprint {
    param([Parameter(Mandatory=$true)]$Snapshot)
    $rows=@(Get-CacCanonicalRows -Value $Snapshot -Path '$')
    return Get-CacSha256Text -Text ($rows -join "`n")
}

function Test-CacPermanenceSnapshot {
    param([string]$Root,$Snapshot,[string]$ExpectedFingerprint='')
    if($null -eq $Snapshot){
        return [pscustomobject]@{state='NOT_DECLARED';fingerprint='';item_count=0;inventory=@();findings=@()}
    }
    $findings=@(); $inventory=@(); $seen=@{}
    $fingerprint=Get-CacPermanenceSnapshotFingerprint -Snapshot $Snapshot
    if(-not[string]::IsNullOrWhiteSpace($ExpectedFingerprint) -and $fingerprint -ne $ExpectedFingerprint.ToLowerInvariant()){
        $findings += New-CacFinding -Code 'PERMANENCE_SNAPSHOT_FINGERPRINT_MISMATCH' -Scope 'PERMANENCE' -Subject 'SNAPSHOT' -Message 'Permanence snapshot fingerprint mismatch.' -Blocking $true
    }

    if([string](Get-CacProperty $Snapshot 'schema' '') -ne 'cerebro-permanence-obligation-snapshot/v1'){
        $findings += New-CacFinding -Code 'PERMANENCE_SNAPSHOT_SCHEMA_MISMATCH' -Scope 'PERMANENCE' -Subject 'SNAPSHOT' -Message 'Permanence snapshot schema mismatch.' -Blocking $true
    }
    $items=@(Get-CacOptionalValues $Snapshot 'items')
    if($items.Count -eq 0){
        $findings += New-CacFinding -Code 'PERMANENCE_SNAPSHOT_ITEMS_MISSING' -Scope 'PERMANENCE' -Subject 'SNAPSHOT' -Message 'Permanence snapshot requires at least one item.' -Blocking $true
    }
    $allowedLayers=@('PROVIDER_PERSISTED','PERMANENT_SHARED_CONTINUITY','SOURCE_CANONICAL','RUNTIME_ENFORCED','EFFECT_PROVEN')
    $allowed=@('SOURCE_REQUIRED_NOW','SOURCE_AFTER_DEPENDENCY','RUNTIME_ONLY','SHARED_ONLY_JUSTIFIED','DEFER','SUPERSEDE','RETIRE')
    foreach($item in $items){
        $id=[string](Get-CacProperty $item 'item_id' '')
        $origin=[string](Get-CacProperty $item 'origin_ref' '')
        $owner=[string](Get-CacProperty $item 'owner' '')
        $expected=[string](Get-CacProperty $item 'expected_terminal_layer' '')
        $current=[string](Get-CacProperty $item 'current_layer' '')
        $disposition=[string](Get-CacProperty $item 'disposition' '')
        $sourceRef=[string](Get-CacProperty $item 'source_standard_or_owner_ref' '')
        $rationale=[string](Get-CacProperty $item 'rationale' '')
        if([string]::IsNullOrWhiteSpace($id) -or [string]::IsNullOrWhiteSpace($origin) -or [string]::IsNullOrWhiteSpace($owner)){
            $findings += New-CacFinding -Code 'PERMANENCE_ITEM_IDENTITY_MISSING' -Scope 'PERMANENCE' -Subject $id -Message 'item_id, origin_ref and owner are required.' -Blocking $true
        }

        if(-not[string]::IsNullOrWhiteSpace($id)){
            if($seen.ContainsKey($id)){$findings += New-CacFinding -Code 'PERMANENCE_ITEM_DUPLICATE' -Scope 'PERMANENCE' -Subject $id -Message 'Duplicate permanence item id.' -Blocking $true}
            $seen[$id]=$true
        }
        if($allowedLayers -notcontains $expected -or $allowedLayers -notcontains $current){
            $findings += New-CacFinding -Code 'PERMANENCE_LAYER_INVALID' -Scope 'PERMANENCE' -Subject $id -Message 'Expected/current truth layer is invalid.' -Blocking $true
        }
        if($allowed -notcontains $disposition){
            $findings += New-CacFinding -Code 'PERMANENCE_DISPOSITION_INVALID' -Scope 'PERMANENCE' -Subject $id -Message ('Unsupported disposition: '+$disposition) -Blocking $true
        }
        if($disposition -eq 'SOURCE_REQUIRED_NOW'){
            if([string]::IsNullOrWhiteSpace($sourceRef) -or -not(Test-Path -LiteralPath (Join-Path $Root ($sourceRef -replace '/','\\')) -PathType Leaf)){
                $findings += New-CacFinding -Code 'PERMANENCE_SOURCE_REQUIRED_MISSING' -Scope 'PERMANENCE' -Subject $id -Message ('SOURCE_REQUIRED_NOW missing candidate Source representation: '+$sourceRef) -Blocking $true
            }
        }
        elseif($disposition -eq 'SOURCE_AFTER_DEPENDENCY'){
            $dependency=[string](Get-CacProperty $item 'dependency_ref' '')
            $dependencyState=[string](Get-CacProperty $item 'dependency_state' 'UNKNOWN')
            if([string]::IsNullOrWhiteSpace($dependency)){$findings += New-CacFinding -Code 'PERMANENCE_DEPENDENCY_REF_MISSING' -Scope 'PERMANENCE' -Subject $id -Message 'SOURCE_AFTER_DEPENDENCY requires dependency_ref.' -Blocking $true}
            if(@('OPEN','UNKNOWN') -contains $dependencyState){$findings += New-CacFinding -Code 'PERMANENCE_DEPENDENCY_OPEN' -Scope 'PERMANENCE' -Subject $id -Message 'SOURCE_AFTER_DEPENDENCY remains non-green until dependency resolves.' -Blocking $false}
            elseif($dependencyState -ne 'RESOLVED'){$findings += New-CacFinding -Code 'PERMANENCE_DEPENDENCY_STATE_INVALID' -Scope 'PERMANENCE' -Subject $id -Message 'dependency_state must be OPEN, UNKNOWN or RESOLVED.' -Blocking $true}

            elseif([string]::IsNullOrWhiteSpace($sourceRef) -or -not(Test-Path -LiteralPath (Join-Path $Root ($sourceRef -replace '/','\\')) -PathType Leaf)){
                $findings += New-CacFinding -Code 'PERMANENCE_RESOLVED_SOURCE_MISSING' -Scope 'PERMANENCE' -Subject $id -Message 'Resolved dependency requires candidate Source representation.' -Blocking $true
            }
        }
        elseif($disposition -eq 'SHARED_ONLY_JUSTIFIED'){
            if([string]::IsNullOrWhiteSpace($owner) -or [string]::IsNullOrWhiteSpace($rationale)){
                $findings += New-CacFinding -Code 'PERMANENCE_SHARED_ONLY_JUSTIFICATION_MISSING' -Scope 'PERMANENCE' -Subject $id -Message 'SHARED_ONLY_JUSTIFIED requires owner and rationale.' -Blocking $true
            }
        }
        elseif(@('RUNTIME_ONLY','DEFER','SUPERSEDE','RETIRE') -contains $disposition){
            if([string]::IsNullOrWhiteSpace($rationale)){$findings += New-CacFinding -Code 'PERMANENCE_DISPOSITION_RATIONALE_MISSING' -Scope 'PERMANENCE' -Subject $id -Message ($disposition+' requires rationale.') -Blocking $true}
        }
        $inventory += [pscustomobject]@{item_id=$id;origin_ref=$origin;owner=$owner;expected_terminal_layer=$expected;current_layer=$current;disposition=$disposition;source_ref=$sourceRef}
    }
    return [pscustomobject]@{state=$(if(@($findings|Where-Object{$_.blocking}).Count -eq 0){'PASS'}else{'BLOCKED'});fingerprint=$fingerprint;item_count=$items.Count;inventory=@($inventory);findings=@($findings)}
}

function Test-CacStrictBoolean {
    param($Object,[string]$Name,[bool]$Expected)
    if($null -eq $Object){return $false}
    $property=$Object.PSObject.Properties[$Name]
    return ($null -ne $property -and $property.Value -is [bool] -and [bool]$property.Value -eq $Expected)
}

function Get-CacEvidenceOverridePath {
    param($EvidenceLocationOverrides,[string]$BindingId,[string]$DefaultPath)
    if($null -eq $EvidenceLocationOverrides){return $DefaultPath}
    $property=$EvidenceLocationOverrides.PSObject.Properties[$BindingId]
    if($null -eq $property -or [string]::IsNullOrWhiteSpace([string]$property.Value)){
        throw ('CAC_EVIDENCE_OVERRIDE_MISSING:{0}' -f $BindingId)
    }
    return [string]$property.Value
}

function Test-CacRuntime2CurrentConformance {
    param([string]$Root,$Spec,$Evidence,[string]$EvidencePath,$ExpectedRuntime2CandidateScope)
    $id='RUNTIME2_HUMAN_EXECUTION_HANDOFF_TRANSPORT'
    $findings=@()
    $mode=if($null -eq $ExpectedRuntime2CandidateScope){'CLEAN_COMMITTED'}else{[string](Get-CacProperty $ExpectedRuntime2CandidateScope 'scope_mode' '')}
    if(@('CLEAN_COMMITTED','SEALED_VALIDATION','INSTALLED_CANDIDATE') -notcontains $mode){
        $findings += New-CacFinding -Code 'RUNTIME2_SCOPE_MODE_INVALID' -Scope 'RUNTIME_EVIDENCE' -Subject $id -Message 'Expected Runtime2 scope mode is invalid.' -Blocking $true
    }
    if([string](Get-CacProperty $Evidence 'scope_mode' '') -ne $mode){
        $findings += New-CacFinding -Code 'RUNTIME2_SCOPE_MODE_MISMATCH' -Scope 'RUNTIME_EVIDENCE' -Subject $id -Message 'Runtime2 evidence does not match independently expected scope mode.' -Blocking $true
    }
    $rootFull=[IO.Path]::GetFullPath($Root).TrimEnd('\')
    $evidenceRoot=[IO.Path]::GetFullPath([string](Get-CacProperty $Evidence 'source_root' '')).TrimEnd('\')
    if($evidenceRoot -ne $rootFull){
        $findings += New-CacFinding -Code 'RUNTIME2_SOURCE_ROOT_MISMATCH' -Scope 'RUNTIME_EVIDENCE' -Subject $id -Message 'Runtime2 evidence source_root does not equal evaluated Source.' -Blocking $true
    }
    $head=(& git -C $Root rev-parse HEAD 2>$null | Select-Object -First 1).Trim().ToLowerInvariant()
    if($LASTEXITCODE -ne 0 -or [string](Get-CacProperty $Evidence 'base_head' '').ToLowerInvariant() -ne $head){
        $findings += New-CacFinding -Code 'RUNTIME2_SOURCE_HEAD_MISMATCH' -Scope 'RUNTIME_EVIDENCE' -Subject $id -Message 'Runtime2 evidence base_head is not current.' -Blocking $true
    }
    $before=Get-CacProperty $Evidence 'snapshot_before' $null
    $after=Get-CacProperty $Evidence 'snapshot_after' $null
    if($null -eq $before -or $null -eq $after -or ($before|ConvertTo-Json -Depth 32 -Compress) -cne ($after|ConvertTo-Json -Depth 32 -Compress)){
        $findings += New-CacFinding -Code 'RUNTIME2_SNAPSHOT_NOT_IMMUTABLE' -Scope 'RUNTIME_EVIDENCE' -Subject $id -Message 'Runtime2 before/after whole-source snapshots must be byte-equivalent JSON values.' -Blocking $true
    }
    $livePaths=@(& git -C $Root status --porcelain=v1 --untracked-files=all 2>$null | ForEach-Object { if($_.Length -ge 4){($_.Substring(3) -replace '\\','/')} } | Sort-Object -Unique)
    $proofPaths=@(Get-CacOptionalValues $after 'changed_paths' | ForEach-Object {[string]$_} | Sort-Object -Unique)
    if(($livePaths -join "`n") -cne ($proofPaths -join "`n")){
        $findings += New-CacFinding -Code 'RUNTIME2_LIVE_SNAPSHOT_DRIFT' -Scope 'RUNTIME_EVIDENCE' -Subject $id -Message 'Runtime2 proof dirty pathset differs from the live Source snapshot.' -Blocking $true
    }
    if($mode -eq 'CLEAN_COMMITTED' -and ($livePaths.Count -ne 0 -or $null -ne (Get-CacProperty $Evidence 'candidate_manifest_sha256' $null))){
        $findings += New-CacFinding -Code 'RUNTIME2_CLEAN_SCOPE_NOT_CLEAN' -Scope 'RUNTIME_EVIDENCE' -Subject $id -Message 'CLEAN_COMMITTED requires clean Source and no candidate envelope.' -Blocking $true
    }
    elseif($mode -ne 'CLEAN_COMMITTED'){
        foreach($field in @('candidate_manifest_sha256','candidate_target_bytes_sha256')){
            if([string](Get-CacProperty $Evidence $field '') -ne [string](Get-CacProperty $ExpectedRuntime2CandidateScope $field '')){
                $findings += New-CacFinding -Code 'RUNTIME2_CANDIDATE_SCOPE_MISMATCH' -Scope 'RUNTIME_EVIDENCE' -Subject $id -Message ('Runtime2 evidence mismatch: '+$field) -Blocking $true
            }
        }
    }
    foreach($field in @(Get-CacOptionalValues $Spec 'forbidden_fields')){
        if($null -ne $Evidence.PSObject.Properties[[string]$field]){
            $findings += New-CacFinding -Code 'RUNTIME2_FORBIDDEN_FIELD_PRESENT' -Scope 'RUNTIME_EVIDENCE' -Subject $id -Message ('Forbidden Runtime2 proof field is present: '+[string]$field) -Blocking $true
        }
    }
    $history=Get-CacProperty $Spec 'historical_evidence' $null
    if($null -ne $history){
        $historyPath=[string](Get-CacProperty $history 'path' '')
        $expectedHash=[string](Get-CacProperty $history 'sha256' '')
        if(-not(Test-Path -LiteralPath $historyPath -PathType Leaf) -or (Get-FileHash -LiteralPath $historyPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedHash -or [string](Get-CacProperty $Evidence 'historical_receipt_sha256' '') -ne $expectedHash){
            $findings += New-CacFinding -Code 'RUNTIME2_HISTORICAL_RECEIPT_MISMATCH' -Scope 'RUNTIME_EVIDENCE' -Subject $id -Message 'Historical Runtime2 v1 receipt pin is not preserved.' -Blocking $true
        }
    }
    return @($findings)
}

function Test-CacRuntimeEvidence {
    param([string]$Root,$Binding,$EvidenceLocationOverrides=$null,$ExpectedEvidenceSet=$null,$ExpectedRuntime2CandidateScope=$null)
    $id=[string](Get-CacProperty $Binding 'id' '')
    $spec=Get-CacProperty $Binding 'runtime_evidence' $null
    if($null -eq $spec){
        return [pscustomobject]@{state='MISSING';findings=@(New-CacFinding -Code 'RUNTIME_EVIDENCE_SPEC_MISSING' -Scope 'RUNTIME_EVIDENCE' -Subject $id -Message 'RUNTIME_EVIDENCE binding requires runtime_evidence specification.' -Blocking $true)}
    }
    $registeredPath=[string](Get-CacProperty $spec 'path' '')
    try {$path=Get-CacEvidenceOverridePath -EvidenceLocationOverrides $EvidenceLocationOverrides -BindingId $id -DefaultPath $registeredPath}
    catch {return [pscustomobject]@{state='INVALID';findings=@(New-CacFinding -Code 'RUNTIME_EVIDENCE_OVERRIDE_INVALID' -Scope 'RUNTIME_EVIDENCE' -Subject $id -Message $_.Exception.Message -Blocking $true)}}
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
        if(-not(Test-CacStrictBoolean -Object $evidence -Name ([string]$field) -Expected $true)){
            $findings += New-CacFinding -Code 'RUNTIME_EVIDENCE_REQUIRED_PROOF_MISSING' -Scope 'RUNTIME_EVIDENCE' -Subject $id -Message ('Required runtime proof is not true: ' + [string]$field) -Blocking $true
        }
    }
    foreach($field in @(Get-CacOptionalValues $spec 'required_false_fields')){
        if(-not(Test-CacStrictBoolean -Object $evidence -Name ([string]$field) -Expected $false)){
            $findings += New-CacFinding -Code 'RUNTIME_EVIDENCE_REQUIRED_FALSE_INVALID' -Scope 'RUNTIME_EVIDENCE' -Subject $id -Message ('Required runtime proof is not strict false: ' + [string]$field) -Blocking $true
        }
    }
    if($id -eq 'RUNTIME2_HUMAN_EXECUTION_HANDOFF_TRANSPORT'){
        $findings += @(Test-CacRuntime2CurrentConformance -Root $Root -Spec $spec -Evidence $evidence -EvidencePath $full -ExpectedRuntime2CandidateScope $ExpectedRuntime2CandidateScope)
    }
    if($null -ne $ExpectedEvidenceSet){
        $expected=@(Get-CacOptionalValues $ExpectedEvidenceSet 'items' | Where-Object {[string](Get-CacProperty $_ 'binding_id' '') -eq $id})
        if($expected.Count -ne 1 -or [IO.Path]::GetFullPath([string](Get-CacProperty $expected[0] 'path' '')) -ne [IO.Path]::GetFullPath($full) -or [string](Get-CacProperty $expected[0] 'sha256' '') -ne (Get-FileHash -LiteralPath $full -Algorithm SHA256).Hash.ToLowerInvariant()){
            $findings += New-CacFinding -Code 'RUNTIME_EVIDENCE_EXPECTED_SET_MISMATCH' -Scope 'RUNTIME_EVIDENCE' -Subject $id -Message 'Runtime evidence path/digest is not the independently expected immutable item.' -Blocking $true
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
    param([string]$Root,$Registry,$EvidenceLocationOverrides=$null,$ExpectedEvidenceSet=$null,$ExpectedRuntime2CandidateScope=$null)

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
            $runtimeProof=Test-CacRuntimeEvidence -Root $Root -Binding $binding -EvidenceLocationOverrides $EvidenceLocationOverrides -ExpectedEvidenceSet $ExpectedEvidenceSet -ExpectedRuntime2CandidateScope $ExpectedRuntime2CandidateScope
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
        $PermanenceSnapshot=$null,
        [string]$ExpectedPermanenceSnapshotFingerprint='',
        $ExpectedRuntime2CandidateScope=$null,
        $EvidenceLocationOverrides=$null,
        $ExpectedEvidenceSet=$null,
        [switch]$PassThru
    )

    $rootPath=[IO.Path]::GetFullPath($Root)
    $registryFull=Join-Path $rootPath ($RegistryPath -replace '/','\')
    if(-not(Test-Path -LiteralPath $registryFull -PathType Leaf)){
        throw ('CAC_REGISTRY_NOT_FOUND:{0}' -f $registryFull)
    }

    $registry=Get-Content -LiteralPath $registryFull -Raw | ConvertFrom-Json

    if($null -ne $EvidenceLocationOverrides -or $null -ne $ExpectedEvidenceSet){
        if($null -eq $EvidenceLocationOverrides -or $null -eq $ExpectedEvidenceSet){throw 'CAC_EVIDENCE_OVERRIDE_AND_EXPECTED_SET_REQUIRED_TOGETHER'}
        $runtimeIds=@($registry.bindings | Where-Object {[string](Get-CacProperty $_ 'wiring_proof_kind' '') -eq 'RUNTIME_EVIDENCE'} | ForEach-Object {[string](Get-CacProperty $_ 'id' '')} | Sort-Object -Unique)
        $overrideIds=@($EvidenceLocationOverrides.PSObject.Properties.Name | Sort-Object -Unique)
        $expectedIds=@(Get-CacOptionalValues $ExpectedEvidenceSet 'items' | ForEach-Object {[string](Get-CacProperty $_ 'binding_id' '')} | Sort-Object -Unique)
        if(($runtimeIds -join "`n") -cne ($overrideIds -join "`n") -or ($runtimeIds -join "`n") -cne ($expectedIds -join "`n")){
            throw 'CAC_EVIDENCE_OVERRIDE_SET_NOT_EXACT'
        }
    }

    $strict=Get-CacStrictContractStates -Root $rootPath -Registry $registry -EvidenceLocationOverrides $EvidenceLocationOverrides -ExpectedEvidenceSet $ExpectedEvidenceSet -ExpectedRuntime2CandidateScope $ExpectedRuntime2CandidateScope
    $coverage=Get-CacClassificationCoverage -Root $rootPath -Registry $registry -StrictStates $strict.states
    $canonical=Get-CacCanonicalResponsibilityFindings -Registry $registry
    $debt=Get-CacKnownDebtFindings -Registry $registry
    $permanence=Test-CacPermanenceSnapshot -Root $rootPath -Snapshot $PermanenceSnapshot -ExpectedFingerprint $ExpectedPermanenceSnapshotFingerprint

    $allFindings=@($strict.findings)+@($coverage.findings)+@($canonical.findings)+@($debt.findings)+@($permanence.findings)
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
        permanence_snapshot_state=[string]$permanence.state
        permanence_snapshot_fingerprint=[string]$permanence.fingerprint
        permanence_snapshot_item_count=[int]$permanence.item_count
        permanence_snapshot_inventory=@($permanence.inventory)
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
