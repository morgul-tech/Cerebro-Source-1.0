[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)]
    [ValidateSet('Enqueue','Drain','Verify','SelfTest')]
    [string]$Mode,
    [ValidateSet('PASS','FAIL')]
    [string]$Result='PASS',
    [string]$AttemptId='',
    [string]$PatchId='',
    [string]$ClaimId='',
    [string]$SourceBefore='',
    [string]$SourceAfter='',
    [string]$ProductSha256='',
    [string]$FailureFamily='',
    [string]$ReachedStage='',
    [string]$SourceMutationAssessment='',
    [switch]$CerebroSyncVerified,
    [string[]]$ArtifactPaths=@(),
    [string]$ArtifactPathsJson='',
    [string]$OutboxRoot='D:\Cerebro\Run\Outbox',
    [string]$DriveReturnRoot='',
    [string]$PackagePath='',
    [string]$HostId=$env:COMPUTERNAME
)

Set-StrictMode -Version 2.0
$ErrorActionPreference='Stop'

function Get-ReturnBridgeSha256 {
    param([Parameter(Mandatory=$true)][string]$LiteralPath)
    $stream=[IO.File]::OpenRead($LiteralPath)
    $algorithm=[Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace('-','').ToLowerInvariant()
    }
    finally {
        $stream.Dispose()
        $algorithm.Dispose()
    }
}

function Get-ReturnBridgeTextSha256 {
    param([Parameter(Mandatory=$true)][string]$Text)
    $algorithm=[Security.Cryptography.SHA256]::Create()
    try {
        $bytes=[Text.UTF8Encoding]::new($false).GetBytes($Text)
        return ([BitConverter]::ToString($algorithm.ComputeHash($bytes))).Replace('-','').ToLowerInvariant()
    }
    finally {$algorithm.Dispose()}
}

function Write-ReturnBridgeJson {
    param([Parameter(Mandatory=$true)][string]$LiteralPath,[Parameter(Mandatory=$true)]$Value)
    $parent=Split-Path -Parent $LiteralPath
    if(-not[string]::IsNullOrWhiteSpace($parent)){[IO.Directory]::CreateDirectory($parent)|Out-Null}
    $temporary=$LiteralPath+'.tmp-'+[guid]::NewGuid().ToString('N')
    [IO.File]::WriteAllText(
        $temporary,
        (($Value|ConvertTo-Json -Depth 16)+[Environment]::NewLine),
        [Text.UTF8Encoding]::new($false)
    )
    [IO.File]::Move($temporary,$LiteralPath)
}

function Get-ReturnBridgeEnvelopeId {
    param([string]$Attempt,[string]$Patch,[string]$Claim)
    $subject=('cerebro-patch-result-return-envelope/v1|{0}|{1}|{2}' -f $Attempt,$Patch,$Claim)
    return 'RB-'+(Get-ReturnBridgeTextSha256 -Text $subject).Substring(0,24)
}

function Get-SafeArtifactName {
    param([string]$Name,[int]$Ordinal)
    $safe=([IO.Path]::GetFileName($Name) -replace '[^A-Za-z0-9._-]','_')
    if([string]::IsNullOrWhiteSpace($safe)){$safe='artifact.bin'}
    return ('{0:D2}-{1}' -f $Ordinal,$safe)
}

function Resolve-ReturnBridgeDriveRoot {
    param([string]$ExplicitRoot)
    if(-not[string]::IsNullOrWhiteSpace($ExplicitRoot)){
        if(Test-Path -LiteralPath $ExplicitRoot -PathType Container){
            return [IO.Path]::GetFullPath($ExplicitRoot)
        }
        return ''
    }
    $candidates=New-Object System.Collections.Generic.List[string]
    if(-not[string]::IsNullOrWhiteSpace($env:CEREBRO_RETURN_BRIDGE_DRIVE_ROOT)){
        $candidates.Add($env:CEREBRO_RETURN_BRIDGE_DRIVE_ROOT)
    }
    $profile=[Environment]::GetFolderPath('UserProfile')
    foreach($candidate in @(
        (Join-Path $profile 'My Drive\CEREBRO_EVIDENCE\PATCH_RESULT_RETURN'),
        (Join-Path $profile 'Google Drive\My Drive\CEREBRO_EVIDENCE\PATCH_RESULT_RETURN'),
        'G:\My Drive\CEREBRO_EVIDENCE\PATCH_RESULT_RETURN',
        'H:\My Drive\CEREBRO_EVIDENCE\PATCH_RESULT_RETURN'
    )){$candidates.Add($candidate)}
    foreach($candidate in $candidates){
        if(Test-Path -LiteralPath $candidate -PathType Container){
            return [IO.Path]::GetFullPath($candidate)
        }
    }
    return ''
}

function Read-ReturnBridgeJson {
    param([Parameter(Mandatory=$true)][string]$LiteralPath)
    return (Get-Content -LiteralPath $LiteralPath -Raw|ConvertFrom-Json)
}

function Test-ReturnBridgePackage {
    param([Parameter(Mandatory=$true)][string]$LiteralPath)
    $errors=New-Object System.Collections.Generic.List[string]
    $readyPath=Join-Path $LiteralPath 'READY.json'
    $envelopePath=Join-Path $LiteralPath 'envelope.json'
    $manifestPath=Join-Path $LiteralPath 'manifest.json'
    foreach($required in @($readyPath,$envelopePath,$manifestPath)){
        if(-not(Test-Path -LiteralPath $required -PathType Leaf)){$errors.Add('MISSING:'+([IO.Path]::GetFileName($required)))}
    }
    if($errors.Count -gt 0){
        return [pscustomobject]@{Result='INCOMPLETE';Errors=@($errors);EnvelopeId='';EnvelopeSha256=''}
    }
    try {
        $ready=Read-ReturnBridgeJson $readyPath
        $envelope=Read-ReturnBridgeJson $envelopePath
        $manifest=Read-ReturnBridgeJson $manifestPath
    }
    catch {
        return [pscustomobject]@{Result='REJECT';Errors=@('JSON_INVALID');EnvelopeId='';EnvelopeSha256=''}
    }
    if([string]$ready.schema -ne 'cerebro-patch-result-return-ready/v1'){$errors.Add('READY_SCHEMA')}
    if([string]$envelope.schema -ne 'cerebro-patch-result-return-envelope/v1'){$errors.Add('ENVELOPE_SCHEMA')}
    if([string]$manifest.schema -ne 'cerebro-patch-result-return-manifest/v1'){$errors.Add('MANIFEST_SCHEMA')}
    if([string]$ready.envelope_id -ne [string]$envelope.envelope_id){$errors.Add('READY_ENVELOPE_ID')}
    if([string]$manifest.envelope_id -ne [string]$envelope.envelope_id){$errors.Add('MANIFEST_ENVELOPE_ID')}
    $actualManifestSha=Get-ReturnBridgeSha256 $manifestPath
    $actualEnvelopeSha=Get-ReturnBridgeSha256 $envelopePath
    if([string]$envelope.manifest_sha256 -ne $actualManifestSha){$errors.Add('MANIFEST_SHA256')}
    if([string]$ready.manifest_sha256 -ne $actualManifestSha){$errors.Add('READY_MANIFEST_SHA256')}
    if([string]$ready.envelope_sha256 -ne $actualEnvelopeSha){$errors.Add('ENVELOPE_SHA256')}
    $entries=@($manifest.artifacts)
    if([int]$manifest.artifact_count -ne $entries.Count -or [int]$ready.artifact_count -ne $entries.Count){
        $errors.Add('MANIFEST_CARDINALITY')
    }
    $seen=@{}
    foreach($entry in $entries){
        $relative=[string]$entry.name
        if([string]::IsNullOrWhiteSpace($relative) -or $relative -ne [IO.Path]::GetFileName($relative)){
            $errors.Add('ARTIFACT_NAME_INVALID');continue
        }
        if($seen.ContainsKey($relative)){$errors.Add('ARTIFACT_NAME_DUPLICATE');continue}
        $seen[$relative]=$true
        $artifact=Join-Path $LiteralPath $relative
        if(-not(Test-Path -LiteralPath $artifact -PathType Leaf)){$errors.Add('ARTIFACT_MISSING:'+$relative);continue}
        if((Get-ReturnBridgeSha256 $artifact) -ne [string]$entry.sha256){$errors.Add('ARTIFACT_SHA256:'+$relative)}
        if((Get-Item -LiteralPath $artifact).Length -ne [int64]$entry.bytes){$errors.Add('ARTIFACT_BYTES:'+$relative)}
    }
    $state=if($errors.Count -eq 0){'PASS'}else{'REJECT'}
    return [pscustomobject]@{
        Result=$state
        Errors=@($errors)
        EnvelopeId=[string]$envelope.envelope_id
        EnvelopeSha256=$actualEnvelopeSha
        Envelope=$envelope
    }
}

function Invoke-ReturnBridgeEnqueue {
    if([string]::IsNullOrWhiteSpace($AttemptId)){throw 'ATTEMPT_ID_REQUIRED'}
    if([string]::IsNullOrWhiteSpace($PatchId)){throw 'PATCH_ID_REQUIRED'}
    if([string]::IsNullOrWhiteSpace($SourceBefore)){throw 'SOURCE_BEFORE_REQUIRED'}
    if([string]::IsNullOrWhiteSpace($SourceAfter)){throw 'SOURCE_AFTER_REQUIRED'}
    if($ProductSha256 -notmatch '^[0-9a-fA-F]{64}$'){throw 'PRODUCT_SHA256_INVALID'}
    if($Result -eq 'FAIL' -and [string]::IsNullOrWhiteSpace($FailureFamily)){throw 'FAILURE_FAMILY_REQUIRED'}
    $artifacts=@($ArtifactPaths|Where-Object{-not[string]::IsNullOrWhiteSpace($_)})
    if(-not[string]::IsNullOrWhiteSpace($ArtifactPathsJson)){
        try {$decoded=$ArtifactPathsJson|ConvertFrom-Json}
        catch {throw 'ARTIFACT_PATHS_JSON_INVALID'}
        foreach($item in $decoded){
            $decodedPath=[string]$item
            if(-not[string]::IsNullOrWhiteSpace($decodedPath)){$artifacts+=,$decodedPath}
        }
    }
    if($artifacts.Count -eq 0){throw 'ARTIFACT_REQUIRED'}
    foreach($artifact in $artifacts){
        if(-not(Test-Path -LiteralPath $artifact -PathType Leaf)){throw ('ARTIFACT_NOT_FOUND:{0}' -f $artifact)}
    }
    $pending=Join-Path $OutboxRoot 'Pending'
    [IO.Directory]::CreateDirectory($pending)|Out-Null
    $envelopeId=Get-ReturnBridgeEnvelopeId -Attempt $AttemptId -Patch $PatchId -Claim $ClaimId
    $final=Join-Path $pending $envelopeId
    $temporary=Join-Path $pending ('.'+$envelopeId+'.partial-'+[guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($temporary)|Out-Null
    try {
        $manifestEntries=@()
        $ordinal=0
        foreach($artifact in $artifacts){
            $ordinal++
            $name=Get-SafeArtifactName -Name $artifact -Ordinal $ordinal
            $destination=Join-Path $temporary $name
            [IO.File]::Copy([IO.Path]::GetFullPath($artifact),$destination,$false)
            $kind=if($name -match '(?i)diagnostic|fail'){'BOUNDED_DIAGNOSTIC'}elseif($name -match '(?i)receipt|success'){'RECEIPT'}else{'EVIDENCE'}
            $manifestEntries+=,[ordered]@{
                name=$name
                sha256=Get-ReturnBridgeSha256 $destination
                bytes=(Get-Item -LiteralPath $destination).Length
                kind=$kind
            }
        }
        $manifest=[ordered]@{
            schema='cerebro-patch-result-return-manifest/v1'
            envelope_id=$envelopeId
            artifact_count=$manifestEntries.Count
            artifacts=$manifestEntries
        }
        $manifestPath=Join-Path $temporary 'manifest.json'
        Write-ReturnBridgeJson -LiteralPath $manifestPath -Value $manifest
        $envelope=[ordered]@{
            schema='cerebro-patch-result-return-envelope/v1'
            envelope_id=$envelopeId
            created_at_utc=[DateTime]::UtcNow.ToString('o')
            host_id=$HostId
            attempt_id=$AttemptId
            patch_id=$PatchId
            claim_id=$ClaimId
            result=$Result
            failure_family=$FailureFamily
            reached_stage=$ReachedStage
            source_before=$SourceBefore
            source_after=$SourceAfter
            source_mutation_assessment=$SourceMutationAssessment
            cerebro_sync_verified=[bool]$CerebroSyncVerified
            product_sha256=$ProductSha256.ToLowerInvariant()
            manifest_sha256=Get-ReturnBridgeSha256 $manifestPath
            transport_authority='NONE'
            patch_result_final_before_transport=$true
        }
        $envelopePath=Join-Path $temporary 'envelope.json'
        Write-ReturnBridgeJson -LiteralPath $envelopePath -Value $envelope
        $ready=[ordered]@{
            schema='cerebro-patch-result-return-ready/v1'
            envelope_id=$envelopeId
            envelope_sha256=Get-ReturnBridgeSha256 $envelopePath
            manifest_sha256=Get-ReturnBridgeSha256 $manifestPath
            artifact_count=$manifestEntries.Count
            ready_written_at_utc=[DateTime]::UtcNow.ToString('o')
        }
        Write-ReturnBridgeJson -LiteralPath (Join-Path $temporary 'READY.json') -Value $ready
        $verified=Test-ReturnBridgePackage $temporary
        if($verified.Result -ne 'PASS'){throw ('LOCAL_PACKAGE_VERIFICATION_FAILED:{0}' -f ($verified.Errors -join ','))}
        if(Test-Path -LiteralPath $final){
            $existing=Test-ReturnBridgePackage $final
            $sameIdentity=(
                $existing.Result -eq 'PASS' -and
                [string]$existing.Envelope.attempt_id -eq $AttemptId -and
                [string]$existing.Envelope.patch_id -eq $PatchId -and
                [string]$existing.Envelope.claim_id -eq $ClaimId -and
                [string]$existing.Envelope.result -eq $Result -and
                [string]$existing.Envelope.source_before -eq $SourceBefore -and
                [string]$existing.Envelope.source_after -eq $SourceAfter -and
                [string]$existing.Envelope.product_sha256 -eq $ProductSha256.ToLowerInvariant() -and
                [string]$existing.Envelope.manifest_sha256 -eq [string]$verified.Envelope.manifest_sha256
            )
            if($sameIdentity){
                Remove-Item -LiteralPath $temporary -Recurse -Force
                return [pscustomobject]@{State='DUPLICATE';EnvelopeId=$envelopeId;PackagePath=$final;EnvelopeSha256=$existing.EnvelopeSha256}
            }
            throw ('ID_HASH_COLLISION:{0}' -f $envelopeId)
        }
        [IO.Directory]::Move($temporary,$final)
        return [pscustomobject]@{State='PENDING';EnvelopeId=$envelopeId;PackagePath=$final;EnvelopeSha256=$verified.EnvelopeSha256}
    }
    catch {
        if(Test-Path -LiteralPath $temporary){Remove-Item -LiteralPath $temporary -Recurse -Force -ErrorAction SilentlyContinue}
        throw
    }
}

function Invoke-ReturnBridgeDrain {
    $pending=Join-Path $OutboxRoot 'Pending'
    $sent=Join-Path $OutboxRoot 'Sent'
    $rejected=Join-Path $OutboxRoot 'Rejected'
    [IO.Directory]::CreateDirectory($pending)|Out-Null
    [IO.Directory]::CreateDirectory($sent)|Out-Null
    [IO.Directory]::CreateDirectory($rejected)|Out-Null
    $driveRoot=Resolve-ReturnBridgeDriveRoot -ExplicitRoot $DriveReturnRoot
    if([string]::IsNullOrWhiteSpace($driveRoot)){
        return [pscustomobject]@{State='PENDING_DRIVE_UNAVAILABLE';Delivered=0;Pending=@(Get-ChildItem -LiteralPath $pending -Directory).Count;DriveRoot=''}
    }
    $delivered=0
    $duplicates=0
    $rejections=0
    foreach($package in @(Get-ChildItem -LiteralPath $pending -Directory|Sort-Object Name)){
        $validation=Test-ReturnBridgePackage $package.FullName
        if($validation.Result -ne 'PASS'){
            if($validation.Result -eq 'INCOMPLETE'){continue}
            $rejectPath=Join-Path $rejected ($package.Name+'-'+[DateTime]::UtcNow.ToString('yyyyMMddHHmmssfff'))
            [IO.Directory]::Move($package.FullName,$rejectPath)
            $rejections++
            continue
        }
        $destination=Join-Path $driveRoot $package.Name
        if(Test-Path -LiteralPath $destination){
            $existing=Test-ReturnBridgePackage $destination
            if($existing.Result -eq 'PASS' -and $existing.EnvelopeSha256 -eq $validation.EnvelopeSha256){
                $sentPath=Join-Path $sent $package.Name
                if(Test-Path -LiteralPath $sentPath){Remove-Item -LiteralPath $package.FullName -Recurse -Force}
                else{[IO.Directory]::Move($package.FullName,$sentPath)}
                $duplicates++
                continue
            }
            throw ('PROVIDER_ID_HASH_COLLISION:{0}' -f $package.Name)
        }
        $partial=Join-Path $driveRoot ('.'+$package.Name+'.partial-'+[guid]::NewGuid().ToString('N'))
        [IO.Directory]::CreateDirectory($partial)|Out-Null
        try {
            foreach($file in @(Get-ChildItem -LiteralPath $package.FullName -File|Where-Object{$_.Name -ne 'READY.json'}|Sort-Object Name)){
                [IO.File]::Copy($file.FullName,(Join-Path $partial $file.Name),$false)
            }
            [IO.File]::Copy((Join-Path $package.FullName 'READY.json'),(Join-Path $partial 'READY.json'),$false)
            $providerValidation=Test-ReturnBridgePackage $partial
            if($providerValidation.Result -ne 'PASS' -or $providerValidation.EnvelopeSha256 -ne $validation.EnvelopeSha256){
                throw ('PROVIDER_READBACK_FAILED:{0}' -f ($providerValidation.Errors -join ','))
            }
            [IO.Directory]::Move($partial,$destination)
            $sentPath=Join-Path $sent $package.Name
            if(Test-Path -LiteralPath $sentPath){throw ('SENT_ID_COLLISION:{0}' -f $package.Name)}
            [IO.Directory]::Move($package.FullName,$sentPath)
            $delivered++
        }
        catch {
            if(Test-Path -LiteralPath $partial){Remove-Item -LiteralPath $partial -Recurse -Force -ErrorAction SilentlyContinue}
            throw
        }
    }
    $state=if($delivered -gt 0){'DELIVERED'}elseif($duplicates -gt 0){'DUPLICATE_NO_EFFECT'}else{'NO_PENDING_EFFECT'}
    return [pscustomobject]@{
        State=$state
        Delivered=$delivered
        Duplicates=$duplicates
        Rejections=$rejections
        Pending=@(Get-ChildItem -LiteralPath $pending -Directory).Count
        DriveRoot=$driveRoot
    }
}

function Invoke-ReturnBridgeSelfTest {
    $root=Join-Path ([IO.Path]::GetTempPath()) ('CerebroReturnBridgeSelfTest-'+[guid]::NewGuid().ToString('N'))
    $outbox=Join-Path $root 'outbox'
    $drive=Join-Path $root 'drive'
    [IO.Directory]::CreateDirectory($drive)|Out-Null
    try {
        $artifact=Join-Path $root 'result.json'
        [IO.File]::WriteAllText($artifact,'{"result":"PASS"}'+[Environment]::NewLine,[Text.UTF8Encoding]::new($false))
        $product=Get-ReturnBridgeSha256 $artifact
        $script:AttemptId='SELFTEST-ATTEMPT-1'
        $script:PatchId='SELFTEST-PATCH'
        $script:ClaimId='SELFTEST-CLAIM'
        $script:SourceBefore='0'*40
        $script:SourceAfter='1'*40
        $script:ProductSha256=$product
        $script:Result='PASS'
        $script:FailureFamily=''
        $script:ReachedStage='SELFTEST'
        $script:SourceMutationAssessment='NO_UNCOMMITTED_SOURCE_MUTATION_PRESENT'
        $script:ArtifactPaths=@($artifact)
        $script:OutboxRoot=$outbox
        $script:DriveReturnRoot=$drive
        $first=Invoke-ReturnBridgeEnqueue
        $duplicate=Invoke-ReturnBridgeEnqueue
        $drain=Invoke-ReturnBridgeDrain
        $provider=Test-ReturnBridgePackage (Join-Path $drive $first.EnvelopeId)
        $pass=(
            $first.State -eq 'PENDING' -and
            $duplicate.State -eq 'DUPLICATE' -and
            $drain.State -eq 'DELIVERED' -and
            $provider.Result -eq 'PASS'
        )
        $resultText=if($pass){'PASS'}else{'FAIL'}
        return [ordered]@{
            schema='cerebro-patch-result-return-bridge-selftest/v1'
            result=$resultText
            enqueue=$first.State
            duplicate=$duplicate.State
            drain=$drain.State
            provider_readback=$provider.Result
        }
    }
    finally {
        if(Test-Path -LiteralPath $root){Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue}
    }
}

if($Mode -eq 'Enqueue'){
    $resultValue=Invoke-ReturnBridgeEnqueue
    Write-Host ('RETURN_BRIDGE_STATE={0}' -f $resultValue.State)
    Write-Host ('RETURN_BRIDGE_ENVELOPE={0}' -f $resultValue.EnvelopeId)
    Write-Host ('RETURN_BRIDGE_PACKAGE={0}' -f $resultValue.PackagePath)
    Write-Host ('RETURN_BRIDGE_ENVELOPE_SHA256={0}' -f $resultValue.EnvelopeSha256)
    exit 0
}
if($Mode -eq 'Drain'){
    $resultValue=Invoke-ReturnBridgeDrain
    Write-Host ('RETURN_BRIDGE_STATE={0}' -f $resultValue.State)
    Write-Host ('RETURN_BRIDGE_DELIVERED={0}' -f $resultValue.Delivered)
    Write-Host ('RETURN_BRIDGE_PENDING={0}' -f $resultValue.Pending)
    Write-Host ('RETURN_BRIDGE_DRIVE_ROOT={0}' -f $resultValue.DriveRoot)
    exit 0
}
if($Mode -eq 'Verify'){
    if([string]::IsNullOrWhiteSpace($PackagePath)){throw 'PACKAGE_PATH_REQUIRED'}
    $resultValue=Test-ReturnBridgePackage $PackagePath
    $resultValue|ConvertTo-Json -Depth 12
    if($resultValue.Result -eq 'PASS'){exit 0}
    exit 1
}
$selftest=Invoke-ReturnBridgeSelfTest
$selftest|ConvertTo-Json -Depth 8
if($selftest.result -eq 'PASS'){exit 0}
exit 1
