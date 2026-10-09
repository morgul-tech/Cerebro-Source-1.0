[CmdletBinding()]
param(
    [string]$KernelPath=(Join-Path $PSScriptRoot '..\delivery\Cerebro.StandardDeliveryKernel.ps1'),
    [string]$EvidenceRoot=''
)
$ErrorActionPreference='Stop'
Set-StrictMode -Version 2.0
$tokens=$null
$parseErrors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($KernelPath,[ref]$tokens,[ref]$parseErrors)
if(@($parseErrors).Count){throw ('KERNEL_PARSE_FAILED:'+(@($parseErrors|ForEach-Object{$_.Message}) -join '|'))}
# Load function definitions only. Never run the kernel's mode/apply dispatch.
foreach($statement in $ast.EndBlock.Statements){
    if($statement -is [System.Management.Automation.Language.FunctionDefinitionAst]){
        . ([scriptblock]::Create($statement.Extent.Text))
    }
}
if([string]::IsNullOrWhiteSpace($EvidenceRoot)){
    $EvidenceRoot=Join-Path ([IO.Path]::GetTempPath()) ('A3KernelRollbackTests-'+[guid]::NewGuid().ToString('N'))
}
if(Test-Path -LiteralPath $EvidenceRoot){throw 'TEST_EVIDENCE_ROOT_MUST_BE_NEW'}
[IO.Directory]::CreateDirectory($EvidenceRoot)|Out-Null
$results=@()
function Assert-Test {param([bool]$Condition,[string]$Message) if(-not$Condition){throw $Message}}
function Write-TestBytes {param([string]$Path,[string]$Text) [IO.File]::WriteAllText($Path,$Text,[Text.UTF8Encoding]::new($false))}
function New-TestCase {
    param([string]$Name,[string]$Operation='replace')
    $root=Join-Path $EvidenceRoot $Name
    $work=Join-Path $root 'work'
    $back=Join-Path $root 'backup'
    [IO.Directory]::CreateDirectory($work)|Out-Null
    [IO.Directory]::CreateDirectory($back)|Out-Null
    $target=Join-Path $work 'target.txt'
    $backup=Join-Path $back 'target.txt'
    if($Operation -ne 'create'){Write-TestBytes $target $baseline}
    $payloadFile=Join-Path $root 'payload.txt'
    Write-TestBytes $payloadFile $payload
    $patch=[pscustomobject]@{patch_id=$Name;files=@([pscustomobject]@{path='target.txt';operation=$Operation;sha256=(Get-Sha256 $payloadFile);payload_path='payload.txt'})}
    $bound=New-KernelBackupManifest -PatchManifest $patch -SourceRoot $work -BackupDirectory $back -SourceCommit ('a'*40) -PayloadRoot $root -BoundAttemptId ('TEST-'+$Name) -KernelSha256 (Get-Sha256 $KernelPath)
    return [pscustomobject]@{name=$Name;root=$root;work=$work;back=$back;target=$target;backup=$backup;payload=$payloadFile;manifest=$patch;bound=$bound}
}
function Run-TestRollback {
    param($Case)
    $script:WorkingSourcePath=$Case.work
    $script:AttemptId='TEST-'+$Case.name
    $script:State=@{Manifest=$Case.manifest;BackupDirectory=$Case.back;BackupManifestSha256=$Case.bound.sha256}
    return Invoke-KernelBoundedRollback -OriginalFailure ('APPLY_FAILED:'+ $Case.name)
}
function Record-Test {
    param($Case,$Receipt,[string]$ExpectedResult,[string]$ExpectedText)
    Assert-Test ([string]$Receipt.result -eq $ExpectedResult) ('RESULT:'+ $Case.name+':'+($Receipt|ConvertTo-Json -Depth 10 -Compress))
    Assert-Test ([string]$Receipt.original_failure -eq ('APPLY_FAILED:'+ $Case.name)) 'ORIGINAL_FAILURE_LOST'
    $read=Get-Content -LiteralPath $Receipt.recovery_evidence -Raw|ConvertFrom-Json
    Assert-Test ([string]$read.result -eq $ExpectedResult) 'RECOVERY_RECEIPT_NOT_PERSISTED'
    Assert-Test ([string]$Receipt.targets[0].target -eq $Case.target) 'TARGET_EVIDENCE_LOST'
    Assert-Test ([string]$Receipt.targets[0].backup -eq $Case.backup) 'BACKUP_EVIDENCE_LOST'
    if($ExpectedText -eq 'ABSENT'){Assert-Test (-not(Test-Path -LiteralPath $Case.target)) ('TARGET_NOT_ABSENT:'+ $Case.name)}
    elseif($ExpectedText -eq 'DIRECTORY' -or $ExpectedText -eq 'JUNCTION'){
        Assert-Test (Test-Path -LiteralPath $Case.target -PathType Container) 'UNKNOWN_TYPE_NOT_PRESERVED'
        Assert-Test ([IO.File]::ReadAllText((Join-Path $Case.target 'sentinel.txt')) -eq 'UNKNOWN_DIRECTORY_CONTENT') 'UNKNOWN_DIRECTORY_CHANGED'
        if($ExpectedText -eq 'JUNCTION'){Assert-Test (([IO.File]::GetAttributes($Case.target) -band [IO.FileAttributes]::ReparsePoint) -ne 0) 'JUNCTION_NOT_PRESERVED'}
    }
    else{Assert-Test ([IO.File]::ReadAllText($Case.target) -ceq $ExpectedText) ('TARGET_CHANGED:'+ $Case.name)}
    $script:results += [ordered]@{case=$Case.name;result='PASS';recovery_result=$ExpectedResult;evidence=$Receipt.recovery_evidence}
}
$baseline='BASELINE'+[char]13+[char]10+'BYTE-EXACT'
$payload='PAYLOAD'+[char]10+'BYTE-EXACT'
# Positive controls exercise exact raw preimage, delivered and absent states.
$c=New-TestCase 'known-baseline'
Record-Test $c (Run-TestRollback $c) 'ROLLED_BACK' $baseline
$c=New-TestCase 'known-payload'
Copy-Item -LiteralPath $c.payload -Destination $c.target -Force
Record-Test $c (Run-TestRollback $c) 'ROLLED_BACK' $baseline
$c=New-TestCase 'known-create-payload' 'create'
Copy-Item -LiteralPath $c.payload -Destination $c.target
Record-Test $c (Run-TestRollback $c) 'ROLLED_BACK' 'ABSENT'
$c=New-TestCase 'known-create-absent' 'create'
Record-Test $c (Run-TestRollback $c) 'ROLLED_BACK' 'ABSENT'
$c=New-TestCase 'known-delete-absent' 'delete'
Remove-Item -LiteralPath $c.target
Record-Test $c (Run-TestRollback $c) 'ROLLED_BACK' $baseline
$c=New-TestCase 'known-delete-baseline' 'delete'
Record-Test $c (Run-TestRollback $c) 'ROLLED_BACK' $baseline
# The three original falsifiers must now preserve unknown bytes or expose failure.
foreach($operation in @('replace','create')){
    $c=New-TestCase ('concurrent-unknown-'+$operation) $operation
    Write-TestBytes $c.target 'CONCURRENT_UNKNOWN'
    $r=Run-TestRollback $c
    Assert-Test ($r.targets[0].error -match 'CURRENT_TARGET_IDENTITY_UNKNOWN') 'UNKNOWN_REASON_MISSING'
    Record-Test $c $r 'FAILED_RECOVERY_REQUIRED' 'CONCURRENT_UNKNOWN'
}
$c=New-TestCase 'restore-locked'
Copy-Item -LiteralPath $c.payload -Destination $c.target -Force
$lock=[IO.File]::Open($c.target,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::None)
try{$r=Run-TestRollback $c}finally{$lock.Dispose()}
Assert-Test (-not[string]::IsNullOrWhiteSpace($r.targets[0].error)) 'RESTORE_EXCEPTION_SWALLOWED'
Record-Test $c $r 'FAILED_RECOVERY_REQUIRED' $payload
foreach($failure in @('missing','corrupt')){
    $c=New-TestCase ('backup-'+$failure)
    Copy-Item -LiteralPath $c.payload -Destination $c.target -Force
    if($failure -eq 'missing'){Remove-Item -LiteralPath $c.backup}else{Write-TestBytes $c.backup 'CORRUPTED_BACKUP'}
    Record-Test $c (Run-TestRollback $c) 'FAILED_RECOVERY_REQUIRED' $payload
}
$c=New-TestCase 'manifest-corrupt'
Copy-Item -LiteralPath $c.payload -Destination $c.target -Force
Write-TestBytes $c.bound.path '{}'
$r=Run-TestRollback $c
Assert-Test ($r.manifest_error -match 'BACKUP_MANIFEST_IDENTITY_MISMATCH') 'MANIFEST_CORRUPTION_NOT_REJECTED'
Record-Test $c $r 'FAILED_RECOVERY_REQUIRED' $payload
$c=New-TestCase 'replace-missing-target'
Remove-Item -LiteralPath $c.target
Record-Test $c (Run-TestRollback $c) 'FAILED_RECOVERY_REQUIRED' 'ABSENT'
foreach($type in @('DIRECTORY','JUNCTION')){
    $c=New-TestCase ('unknown-type-'+$type)
    Remove-Item -LiteralPath $c.target
    $directory=if($type -eq 'JUNCTION'){Join-Path $c.root 'other-directory'}else{$c.target}
    [IO.Directory]::CreateDirectory($directory)|Out-Null
    Write-TestBytes (Join-Path $directory 'sentinel.txt') 'UNKNOWN_DIRECTORY_CONTENT'
    if($type -eq 'JUNCTION'){New-Item -ItemType Junction -Path $c.target -Target $directory|Out-Null}
    Record-Test $c (Run-TestRollback $c) 'FAILED_RECOVERY_REQUIRED' $type
}
# A writer attempting to change known payload after the handle hash must fail
# while the real rollback retains that exact handle through restore/deletion.
$originalHash=(Get-Item Function:Get-KernelRecoveryStreamHash).ScriptBlock
$script:raceTarget=''
$script:raceDenied=$false
$script:renameDenied=$false
function Get-KernelRecoveryStreamHash {
    param([IO.Stream]$Stream)
    $hash=& $originalHash $Stream
    if($Stream -is [IO.FileStream] -and $Stream.CanWrite -and -not$script:raceDenied){
        try{[IO.File]::WriteAllText($script:raceTarget,'LATE_CONCURRENT_WRITE')}
        catch{$script:raceDenied=$true}
        Assert-Test $script:raceDenied 'HASH_TO_RESTORE_WRITE_RACE_NOT_FENCED'
        try{[IO.File]::Move($script:raceTarget,$script:raceTarget+'.renamed')}
        catch{$script:renameDenied=$true}
        Assert-Test $script:renameDenied 'HASH_TO_RESTORE_REPLACEMENT_RACE_NOT_FENCED'
    }
    return $hash
}
foreach($operation in @('replace','create')){
    $c=New-TestCase ('hash-to-restore-race-'+$operation) $operation
    Copy-Item -LiteralPath $c.payload -Destination $c.target -Force
    $script:raceTarget=$c.target;$script:raceDenied=$false;$script:renameDenied=$false
    $r=Run-TestRollback $c
    Assert-Test $script:raceDenied 'RACE_NOT_EXERCISED'
    $expected=if($operation -eq 'create'){'ABSENT'}else{$baseline}
    Record-Test $c $r 'ROLLED_BACK' $expected
}
Set-Item Function:Get-KernelRecoveryStreamHash -Value $originalHash
# Verify a damaged restore is not promoted to success.
$c=New-TestCase 'post-restore-hash-failure'
Copy-Item -LiteralPath $c.payload -Destination $c.target -Force
$script:restoreHashCalls=0
$script:restoreTarget=$c.target
function Get-KernelRecoveryStreamHash {
    param([IO.Stream]$Stream)
    $hash=& $originalHash $Stream
    if($Stream -is [IO.FileStream] -and $Stream.CanWrite){
        $script:restoreHashCalls++
        if($script:restoreHashCalls -eq 2){return ('0'*64)}
    }
    return $hash
}
$r=Run-TestRollback $c
Assert-Test ($r.targets[0].error -match 'RESTORE_HASH_VERIFY_FAILED') 'RESTORE_HASH_FAILURE_NOT_REPORTED'
Record-Test $c $r 'FAILED_RECOVERY_REQUIRED' $baseline
Set-Item Function:Get-KernelRecoveryStreamHash -Value $originalHash
# The actual terminal catch must retain original error and emit recovery status.
$c=New-TestCase 'terminal-catch-locked'
Copy-Item -LiteralPath $c.payload -Destination $c.target -Force
$script:WorkingSourcePath=$c.work
$script:AttemptId='TEST-'+$c.name
$script:State=@{Manifest=$c.manifest;BackupDirectory=$c.back;BackupManifestSha256=$c.bound.sha256;MutationStarted=$true;SyncStarted=$false;ReachedStage='OFFLINE_POSTWRITE';FailureFamily='TEST';FullRecoverySnapshot=$null;RecoveryResult=$null}
$source=[IO.File]::ReadAllText($KernelPath)
$catchText=$source.Substring($source.LastIndexOf(([string][char]10+'catch {'))+1)
$lock=[IO.File]::Open($c.target,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::None)
try{
    $log=@(& {try{. ([scriptblock]::Create("try {throw 'TERMINAL_APPLY_FAILURE'}"+[Environment]::NewLine+$catchText))}catch{Write-Output ('THROWN:'+ $_.Exception.Message)}} *>&1)
}finally{$lock.Dispose()}
$logText=$log -join [Environment]::NewLine
Assert-Test ($logText -match 'RECOVERY_RESULT=FAILED_RECOVERY_REQUIRED') 'TERMINAL_RECOVERY_STATUS_MISSING'
Assert-Test ($logText -match 'THROWN:TERMINAL_APPLY_FAILURE') 'ORIGINAL_THROW_LOST'
Assert-Test ($logText -match 'FAILURE_FAMILY=FAILED_RECOVERY_REQUIRED') 'TERMINAL_FAMILY_MISSING'
[IO.File]::WriteAllText((Join-Path $c.root 'terminal-output.txt'),$logText)
$results += [ordered]@{case=$c.name;result='PASS';recovery_result='FAILED_RECOVERY_REQUIRED';evidence=(Join-Path $c.root 'terminal-output.txt')}
$receipt=[ordered]@{schema='cerebro-kernel-rollback-offline-test/v1';result='PASS';tests=$results.Count;kernel_sha256=(Get-Sha256 $KernelPath);effect='DISPOSABLE_FIXTURES_ONLY';evidence_root=$EvidenceRoot;cases=$results}
$json=$receipt|ConvertTo-Json -Depth 10
[IO.File]::WriteAllText((Join-Path $EvidenceRoot 'test-receipt.json'),$json,[Text.UTF8Encoding]::new($false))
$json
