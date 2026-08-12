[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)]
    [ValidateSet('SelfTest','Apply')]
    [string]$Mode,
    [Parameter(Mandatory=$true)]
    [string]$BundleRoot,
    [string]$WorkingSourcePath = 'D:\Cerebro\Source\Cerebro_Source_v1.0',
    [string]$LauncherPath = '',
    [string]$AttemptId = '',
    [string]$TargetRuntimeValidationReceipt = ''
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$KernelId = 'CEREBRO-STANDARD-DELIVERY-KERNEL-001'
$State = [ordered]@{
    FailureFamily = 'UNCLASSIFIED'
    ReachedStage = 'START'
    MutationStarted = $false
    SyncStarted = $false
    BackupDirectory = $null
    Manifest = $null
    ActivationProofs = @()
    TransientCleanup = @()
    FullRecoverySnapshot = $null
    TargetRuntimeValidationReceipt = ''
}

function Get-Sha256 {
    param([Parameter(Mandatory=$true)][string]$LiteralPath)
    return (Get-FileHash -LiteralPath $LiteralPath -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-GitBlobShaFromFile {
    param([Parameter(Mandatory=$true)][string]$LiteralPath)
    $bytes = [IO.File]::ReadAllBytes($LiteralPath)
    $header = [Text.Encoding]::ASCII.GetBytes(('blob {0}' -f $bytes.Length) + [char]0)
    $all = [byte[]]::new($header.Length + $bytes.Length)
    [Array]::Copy($header,0,$all,0,$header.Length)
    [Array]::Copy($bytes,0,$all,$header.Length,$bytes.Length)
    $sha1 = [Security.Cryptography.SHA1]::Create()
    try {
        return ([BitConverter]::ToString($sha1.ComputeHash($all))).Replace('-','').ToLowerInvariant()
    }
    finally {
        $sha1.Dispose()
    }
}

function Resolve-Executable {
    param([Parameter(Mandatory=$true)][string]$Name)
    $command = Get-Command $Name -ErrorAction Stop | Select-Object -First 1
    if ([string]::IsNullOrWhiteSpace($command.Source)) {
        throw ('EXECUTABLE_NOT_RESOLVED:{0}' -f $Name)
    }
    return $command.Source
}

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory=$true)][string]$Executable,
        [Parameter(Mandatory=$true)][string[]]$ArgumentList,
        [int[]]$AllowedExitCodes = @(0)
    )
    $stdoutFile = [IO.Path]::GetTempFileName()
    $stderrFile = [IO.Path]::GetTempFileName()
    $previousPreference = $ErrorActionPreference
    $nativeExitCode = $null
    try {
        $ErrorActionPreference = 'Continue'
        & $Executable @ArgumentList 1> $stdoutFile 2> $stderrFile
        $nativeExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
    try {
        $stdoutText = [IO.File]::ReadAllText($stdoutFile).TrimEnd([char[]]"`r`n")
        $stderrText = [IO.File]::ReadAllText($stderrFile).TrimEnd([char[]]"`r`n")
    }
    finally {
        Remove-Item -LiteralPath $stdoutFile,$stderrFile -Force -ErrorAction SilentlyContinue
    }
    if ($AllowedExitCodes -notcontains $nativeExitCode) {
        throw ('NATIVE_EXIT_NOT_ALLOWED executable={0} exit={1} args={2} stderr={3}' -f
            $Executable,$nativeExitCode,($ArgumentList -join ' '),$stderrText)
    }
    return [pscustomobject]@{ExitCode=$nativeExitCode;Stdout=$stdoutText;Stderr=$stderrText}
}

function Invoke-Git {
    param(
        [Parameter(Mandatory=$true)][string]$GitPath,
        [Parameter(Mandatory=$true)][string[]]$ArgumentList,
        [int[]]$AllowedExitCodes = @(0)
    )
    return Invoke-NativeCommand -Executable $GitPath -ArgumentList $ArgumentList -AllowedExitCodes $AllowedExitCodes
}

function Assert-ParserClean {
    param([Parameter(Mandatory=$true)][string]$LiteralPath)
    $parserTokens = $null
    $parserErrors = $null
    [System.Management.Automation.Language.Parser]::ParseFile(
        $LiteralPath,[ref]$parserTokens,[ref]$parserErrors
    ) | Out-Null
    $errorArray = @($parserErrors)
    if ($errorArray.Count -gt 0) {
        $messages = @($errorArray | ForEach-Object { $_.Message }) -join ' | '
        throw ('POWERSHELL_PARSE_FAILED:{0}:{1}' -f $LiteralPath,$messages)
    }
}

function Read-Manifest {
    $manifestPath = Join-Path -Path $BundleRoot -ChildPath 'manifest.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw ('MANIFEST_NOT_FOUND:{0}' -f $manifestPath)
    }
    return (Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json)
}

function Assert-PayloadIntegrity {
    param([Parameter(Mandatory=$true)]$PatchManifest)
    foreach ($fileEntry in @($PatchManifest.files)) {
        $operation=[string](Get-KernelOptionalProperty -Object $fileEntry -Name 'operation' -Default '')
        if(@('create','replace','delete') -notcontains $operation){
            throw ('PAYLOAD_OPERATION_INVALID:{0}:{1}' -f $fileEntry.path,$operation)
        }
        if($operation -eq 'delete'){
            if([string]::IsNullOrWhiteSpace([string](Get-KernelOptionalProperty $fileEntry 'expected_git_blob_sha' ''))){
                throw ('DELETE_BASELINE_IDENTITY_MISSING:{0}' -f $fileEntry.path)
            }
            foreach($forbiddenField in @('payload_path','sha256','final_git_blob_sha')){
                if(-not[string]::IsNullOrWhiteSpace([string](Get-KernelOptionalProperty $fileEntry $forbiddenField ''))){
                    throw ('DELETE_PAYLOAD_FIELD_FORBIDDEN:{0}:{1}' -f $fileEntry.path,$forbiddenField)
                }
            }
            continue
        }
        foreach($requiredField in @('payload_path','sha256','final_git_blob_sha')){
            if([string]::IsNullOrWhiteSpace([string](Get-KernelOptionalProperty $fileEntry $requiredField ''))){
                throw ('PAYLOAD_FIELD_MISSING:{0}:{1}' -f $fileEntry.path,$requiredField)
            }
        }
        if($operation -eq 'replace' -and
           [string]::IsNullOrWhiteSpace([string](Get-KernelOptionalProperty $fileEntry 'expected_git_blob_sha' ''))){
            throw ('REPLACE_BASELINE_IDENTITY_MISSING:{0}' -f $fileEntry.path)
        }
        $payloadPath = Join-Path -Path $BundleRoot -ChildPath ([string]$fileEntry.payload_path)
        if (-not (Test-Path -LiteralPath $payloadPath -PathType Leaf)) {
            throw ('PAYLOAD_FILE_MISSING:{0}' -f $fileEntry.path)
        }
        if ((Get-Sha256 $payloadPath) -ne [string]$fileEntry.sha256) {
            throw ('PAYLOAD_SHA256_MISMATCH:{0}' -f $fileEntry.path)
        }
        if ((Get-GitBlobShaFromFile $payloadPath) -ne [string]$fileEntry.final_git_blob_sha) {
            throw ('PAYLOAD_GIT_BLOB_MISMATCH:{0}' -f $fileEntry.path)
        }
        if ([string]$fileEntry.path -match '(?i)\.ps1$') {
            Assert-ParserClean -LiteralPath $payloadPath
        }
        if ([string]$fileEntry.path -match '(?i)\.py$') {
            $pythonForCompile=Resolve-PythonRunner
            $compileArgs=@($pythonForCompile.PrefixArgs)+@('-m','py_compile',$payloadPath)
            [void](Invoke-NativeCommand -Executable $pythonForCompile.Executable -ArgumentList $compileArgs)
        }
    }
}

function Test-AllFinalBlobsAtHead {
    param([string]$GitPath,$PatchManifest)
    foreach ($fileEntry in @($PatchManifest.files)) {
        if([string]$fileEntry.operation -eq 'delete'){
            $listing=Invoke-Git -GitPath $GitPath -ArgumentList @('ls-tree','HEAD','--',[string]$fileEntry.path)
            if(-not[string]::IsNullOrWhiteSpace($listing.Stdout)){return $false}
            continue
        }
        $spec = ('HEAD:{0}' -f [string]$fileEntry.path)
        $result = Invoke-Git -GitPath $GitPath -ArgumentList @('rev-parse',$spec) -AllowedExitCodes @(0,128)
        if ($result.ExitCode -ne 0 -or $result.Stdout -ne [string]$fileEntry.final_git_blob_sha) {
            return $false
        }
    }
    return $true
}

function Install-ExactPayloadFile {
    param([string]$PayloadPath,[string]$TargetPath,[string]$ExpectedSha256)
    $targetDirectory = Split-Path -Parent $TargetPath
    if ([string]::IsNullOrWhiteSpace($targetDirectory)) {
        throw ('TARGET_PARENT_EMPTY:{0}' -f $TargetPath)
    }
    [IO.Directory]::CreateDirectory($targetDirectory) | Out-Null
    $temporaryPath = Join-Path -Path $targetDirectory -ChildPath ('.cerebro-install-' + [guid]::NewGuid().ToString('N'))
    try {
        Copy-Item -LiteralPath $PayloadPath -Destination $temporaryPath -Force
        if ((Get-Sha256 $temporaryPath) -ne $ExpectedSha256) {
            throw ('TEMP_INSTALL_HASH_MISMATCH:{0}' -f $TargetPath)
        }
        if (Test-Path -LiteralPath $TargetPath -PathType Leaf) {
            [IO.File]::Copy($temporaryPath,$TargetPath,$true)
        }
        else {
            [IO.File]::Move($temporaryPath,$TargetPath)
        }
    }
    finally {
        if (Test-Path -LiteralPath $temporaryPath) {
            Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
        }
    }
}

function Remove-ExactTargetFile {
    param([Parameter(Mandatory=$true)][string]$TargetPath)
    if(Test-Path -LiteralPath $TargetPath -PathType Container){
        throw ('DELETE_TARGET_IS_DIRECTORY:{0}' -f $TargetPath)
    }
    if(-not(Test-Path -LiteralPath $TargetPath -PathType Leaf)){
        throw ('DELETE_TARGET_FILE_MISSING:{0}' -f $TargetPath)
    }
    Remove-Item -LiteralPath $TargetPath -Force
    if(Test-Path -LiteralPath $TargetPath){
        throw ('DELETE_TARGET_STILL_EXISTS:{0}' -f $TargetPath)
    }
}

function Get-Sha256FromStream {
    param([Parameter(Mandatory=$true)][IO.Stream]$Stream)
    $sha=[Security.Cryptography.SHA256]::Create()
    try {return ([BitConverter]::ToString($sha.ComputeHash($Stream))).Replace('-','').ToLowerInvariant()}
    finally {$sha.Dispose()}
}

function Get-Sha256FromText {
    param([Parameter(Mandatory=$true)][string]$Text)
    $stream=[IO.MemoryStream]::new([Text.Encoding]::UTF8.GetBytes($Text),$false)
    try {return Get-Sha256FromStream -Stream $stream}
    finally {$stream.Dispose()}
}

function Get-KernelRelativeFilePath {
    param([Parameter(Mandatory=$true)][string]$Root,[Parameter(Mandatory=$true)][string]$FullPath)
    $rootFull=[IO.Path]::GetFullPath($Root).TrimEnd([char[]]'\/')+[IO.Path]::DirectorySeparatorChar
    $fileFull=[IO.Path]::GetFullPath($FullPath)
    if(-not$fileFull.StartsWith($rootFull,[StringComparison]::OrdinalIgnoreCase)){
        throw ('SNAPSHOT_PATH_ESCAPE:{0}' -f $fileFull)
    }
    return $fileFull.Substring($rootFull.Length).Replace('\','/')
}

function New-VerifiedFullRecoverySnapshot {
    param(
        [Parameter(Mandatory=$true)][string]$SourceRoot,
        [Parameter(Mandatory=$true)][string]$BackupRoot,
        [Parameter(Mandatory=$true)][string]$SourceCommit,
        [int]$RetentionCount=10
    )
    if($RetentionCount -lt 1){throw 'SNAPSHOT_RETENTION_INVALID'}
    [IO.Directory]::CreateDirectory($BackupRoot)|Out-Null
    $backupFull=[IO.Path]::GetFullPath($BackupRoot)
    $sourceFull=[IO.Path]::GetFullPath($SourceRoot)
    if($backupFull.StartsWith($sourceFull.TrimEnd([char[]]'\/')+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)){
        throw 'SNAPSHOT_ROOT_INSIDE_WORKING_SOURCE_FORBIDDEN'
    }

    [string[]]$sourceFiles=@([IO.Directory]::EnumerateFiles($sourceFull,'*',[IO.SearchOption]::AllDirectories))
    [Array]::Sort($sourceFiles,[StringComparer]::Ordinal)
    if($sourceFiles.Count -eq 0){throw 'SNAPSHOT_SOURCE_EMPTY'}
    $inventory=@()
    [int64]$totalBytes=0
    foreach($file in $sourceFiles){
        $info=[IO.FileInfo]::new($file)
        $relative=Get-KernelRelativeFilePath -Root $sourceFull -FullPath $file
        $inventory += [pscustomobject]@{path=$relative;bytes=[int64]$info.Length;sha256=(Get-Sha256 -LiteralPath $file)}
        $totalBytes += [int64]$info.Length
    }
    $inventoryRows=@($inventory|ForEach-Object{('{0}|{1}|{2}' -f [string]$_.path,[int64]$_.bytes,[string]$_.sha256)})
    $inventoryIdentity=Get-Sha256FromText -Text ($inventoryRows -join "`n")

    $driveRoot=[IO.Path]::GetPathRoot($backupFull)
    $drive=[IO.DriveInfo]::new($driveRoot)
    if(-not$drive.IsReady){throw ('SNAPSHOT_DRIVE_NOT_READY:{0}' -f $driveRoot)}
    $requiredFree=[int64]$totalBytes+67108864
    if([int64]$drive.AvailableFreeSpace -lt $requiredFree){
        throw ('SNAPSHOT_INSUFFICIENT_SPACE required={0} available={1}' -f $requiredFree,$drive.AvailableFreeSpace)
    }

    Add-Type -AssemblyName System.IO.Compression -ErrorAction Stop
    Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction Stop
    $stamp=(Get-Date).ToString('yyyyMMdd-HHmmss')
    $suffix=[guid]::NewGuid().ToString('N').Substring(0,8)
    $shortCommit=$SourceCommit.Substring(0,[Math]::Min(12,$SourceCommit.Length))
    $baseName=('CEREBRO_FULL_SOURCE_{0}_{1}_{2}' -f $shortCommit,$stamp,$suffix)
    $archivePath=Join-Path $backupFull ($baseName+'.zip')
    $receiptPath=Join-Path $backupFull ($baseName+'.receipt.json')

    $archive=[IO.Compression.ZipFile]::Open($archivePath,[IO.Compression.ZipArchiveMode]::Create)
    try {
        foreach($item in $inventory){
            $sourcePath=Join-Path $sourceFull (([string]$item.path)-replace '/','\')
            $entry=$archive.CreateEntry([string]$item.path,[IO.Compression.CompressionLevel]::Optimal)
            $input=[IO.File]::OpenRead($sourcePath)
            $output=$entry.Open()
            try {$input.CopyTo($output)}
            finally {$output.Dispose();$input.Dispose()}
        }
    }
    finally {$archive.Dispose()}

    $verifiedArchive=[IO.Compression.ZipFile]::OpenRead($archivePath)
    try {
        if($verifiedArchive.Entries.Count -ne $inventory.Count){
            throw ('SNAPSHOT_ENTRY_COUNT_MISMATCH expected={0} actual={1}' -f $inventory.Count,$verifiedArchive.Entries.Count)
        }
        $entryMap=@{}
        foreach($entry in $verifiedArchive.Entries){
            if($entryMap.ContainsKey($entry.FullName)){throw ('SNAPSHOT_DUPLICATE_ENTRY:{0}' -f $entry.FullName)}
            $entryMap[$entry.FullName]=$entry
        }
        foreach($item in $inventory){
            $relative=[string]$item.path
            if(-not$entryMap.ContainsKey($relative)){throw ('SNAPSHOT_ENTRY_MISSING:{0}' -f $relative)}
            $entry=$entryMap[$relative]
            if([int64]$entry.Length -ne [int64]$item.bytes){throw ('SNAPSHOT_ENTRY_LENGTH_MISMATCH:{0}' -f $relative)}
            $entryStream=$entry.Open()
            try {$entrySha=Get-Sha256FromStream -Stream $entryStream}
            finally {$entryStream.Dispose()}
            if($entrySha -ne [string]$item.sha256){throw ('SNAPSHOT_ENTRY_SHA256_MISMATCH:{0}' -f $relative)}
        }
        foreach($requiredEntry in @('cerebro.yaml','.git/HEAD')){
            if(-not$entryMap.ContainsKey($requiredEntry)){throw ('SNAPSHOT_REQUIRED_ENTRY_MISSING:{0}' -f $requiredEntry)}
        }
    }
    finally {$verifiedArchive.Dispose()}

    $receipt=[ordered]@{
        schema='cerebro-full-source-recovery-snapshot-receipt/v1'
        result='PASS'
        authority='RECOVERY_EVIDENCE_ONLY'
        source_root=$sourceFull
        source_commit=$SourceCommit
        archive_path=$archivePath
        archive_sha256=(Get-Sha256 -LiteralPath $archivePath)
        file_count=$inventory.Count
        total_uncompressed_bytes=$totalBytes
        inventory_sha256=$inventoryIdentity
        includes_dot_git=$true
        archive_entries_verified=$true
        created_at_utc=[DateTime]::UtcNow.ToString('o')
    }
    [IO.File]::WriteAllText($receiptPath,(($receipt|ConvertTo-Json -Depth 8)+"`r`n"),[Text.UTF8Encoding]::new($false))
    $readBack=Get-Content -LiteralPath $receiptPath -Raw|ConvertFrom-Json
    if([string]$readBack.result -ne 'PASS' -or [string]$readBack.archive_sha256 -ne (Get-Sha256 -LiteralPath $archivePath)){
        throw 'SNAPSHOT_RECEIPT_REREAD_FAILED'
    }

    $verifiedReceipts=@()
    foreach($candidateReceipt in @(Get-ChildItem -LiteralPath $backupFull -Filter 'CEREBRO_FULL_SOURCE_*.receipt.json' -File)){
        try {
            $candidate=Get-Content -LiteralPath $candidateReceipt.FullName -Raw|ConvertFrom-Json
            if([string]$candidate.schema -ne 'cerebro-full-source-recovery-snapshot-receipt/v1' -or [string]$candidate.result -ne 'PASS'){continue}
            if(-not(Test-Path -LiteralPath ([string]$candidate.archive_path) -PathType Leaf)){continue}
            $verifiedReceipts += [pscustomobject]@{receipt=$candidateReceipt.FullName;archive=[string]$candidate.archive_path;created=[DateTime]::Parse([string]$candidate.created_at_utc).ToUniversalTime()}
        }
        catch {continue}
    }
    $expired=@($verifiedReceipts|Sort-Object -Property created -Descending|Select-Object -Skip $RetentionCount)
    foreach($item in $expired){
        Remove-Item -LiteralPath $item.archive -Force
        Remove-Item -LiteralPath $item.receipt -Force
    }
    return [pscustomobject]@{archive_path=$archivePath;receipt_path=$receiptPath;archive_sha256=[string]$receipt.archive_sha256;inventory_sha256=$inventoryIdentity}
}

function Assert-DeclaredTargetMutationCapabilities {
    param([Parameter(Mandatory=$true)][string]$SourceRoot,[Parameter(Mandatory=$true)]$PatchManifest)
    $parents=@{}
    foreach($entry in @($PatchManifest.files)){
        $target=Join-Path $SourceRoot (([string]$entry.path)-replace '/','\')
        $parent=Split-Path -Parent $target
        while(-not[string]::IsNullOrWhiteSpace($parent) -and -not(Test-Path -LiteralPath $parent -PathType Container)){
            $parent=Split-Path -Parent $parent
        }
        if([string]::IsNullOrWhiteSpace($parent)){throw ('CAPABILITY_PARENT_NOT_FOUND:{0}' -f $entry.path)}
        $parents[[IO.Path]::GetFullPath($parent)]=$true
    }
    foreach($parent in @($parents.Keys)){
        $probe=Join-Path $parent ('.cerebro-capability-'+[guid]::NewGuid().ToString('N')+'.tmp')
        try {
            [IO.File]::WriteAllText($probe,'CEREBRO_CAPABILITY_PROBE',[Text.UTF8Encoding]::new($false))
            if(-not(Test-Path -LiteralPath $probe -PathType Leaf)){throw ('CAPABILITY_CREATE_FAILED:{0}' -f $parent)}
            Remove-Item -LiteralPath $probe -Force
            if(Test-Path -LiteralPath $probe){throw ('CAPABILITY_DELETE_FAILED:{0}' -f $parent)}
        }
        finally {
            if(Test-Path -LiteralPath $probe -PathType Leaf){Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue}
        }
    }
}

function Invoke-LocalGitFixture {
    param([string]$GitPath)
    $fixtureRoot = Join-Path -Path ([IO.Path]::GetTempPath()) -ChildPath ('CerebroKernelFixture-' + [guid]::NewGuid().ToString('N'))
    $remotePath = Join-Path $fixtureRoot 'remote.git'
    $seedPath = Join-Path $fixtureRoot 'seed'
    $workPath = Join-Path $fixtureRoot 'work'
    try {
        [IO.Directory]::CreateDirectory($fixtureRoot) | Out-Null
        [void](Invoke-Git -GitPath $GitPath -ArgumentList @('init','--bare',$remotePath))
        [void](Invoke-Git -GitPath $GitPath -ArgumentList @('init',$seedPath))
        [void](Invoke-Git -GitPath $GitPath -ArgumentList @('-C',$seedPath,'config','user.name','Cerebro Kernel SelfTest'))
        [void](Invoke-Git -GitPath $GitPath -ArgumentList @('-C',$seedPath,'config','user.email','cerebro-selftest@example.invalid'))
        [IO.File]::WriteAllText((Join-Path $seedPath 'baseline.txt'),'BASELINE',[Text.UTF8Encoding]::new($false))
        [IO.File]::WriteAllText((Join-Path $seedPath 'cerebro.yaml'),"schema: cerebro-selftest-fixture/v1`n",[Text.UTF8Encoding]::new($false))
        [void](Invoke-Git -GitPath $GitPath -ArgumentList @('-C',$seedPath,'add','--','baseline.txt','cerebro.yaml'))
        [void](Invoke-Git -GitPath $GitPath -ArgumentList @('-C',$seedPath,'commit','-m','fixture baseline'))
        [void](Invoke-Git -GitPath $GitPath -ArgumentList @('-C',$seedPath,'branch','-M','main'))
        [void](Invoke-Git -GitPath $GitPath -ArgumentList @('-C',$seedPath,'remote','add','origin',$remotePath))
        [void](Invoke-Git -GitPath $GitPath -ArgumentList @('-C',$seedPath,'push','-u','origin','main'))
        [void](Invoke-Git -GitPath $GitPath -ArgumentList @('--git-dir',$remotePath,'symbolic-ref','HEAD','refs/heads/main'))
        [void](Invoke-Git -GitPath $GitPath -ArgumentList @('clone',$remotePath,$workPath))
        $fetchResult = Invoke-Git -GitPath $GitPath -ArgumentList @('-C',$workPath,'fetch','origin','main')
        if ($fetchResult.ExitCode -ne 0) { throw 'FIXTURE_FETCH_NONZERO' }

        $backup = Join-Path $fixtureRoot 'backup.txt'
        Copy-Item -LiteralPath (Join-Path $workPath 'baseline.txt') -Destination $backup
        $replacement = Join-Path $fixtureRoot 'replacement.txt'
        [IO.File]::WriteAllText($replacement,'REPLACEMENT',[Text.UTF8Encoding]::new($false))
        $replacementHash = Get-Sha256 $replacement
        Install-ExactPayloadFile -PayloadPath $replacement -TargetPath (Join-Path $workPath 'baseline.txt') -ExpectedSha256 $replacementHash
        Copy-Item -LiteralPath $backup -Destination (Join-Path $workPath 'baseline.txt') -Force
        $status = Invoke-Git -GitPath $GitPath -ArgumentList @('-C',$workPath,'status','--porcelain')
        if (-not [string]::IsNullOrWhiteSpace($status.Stdout)) {
            throw ('FIXTURE_ROLLBACK_NOT_CLEAN:{0}' -f $status.Stdout)
        }

        Remove-ExactTargetFile -TargetPath (Join-Path $workPath 'baseline.txt')
        if(Test-Path -LiteralPath (Join-Path $workPath 'baseline.txt')){
            throw 'FIXTURE_BOUNDED_DELETE_FAILED'
        }
        Copy-Item -LiteralPath $backup -Destination (Join-Path $workPath 'baseline.txt') -Force
        $deleteRollbackStatus=Invoke-Git -GitPath $GitPath -ArgumentList @('-C',$workPath,'status','--porcelain')
        if(-not[string]::IsNullOrWhiteSpace($deleteRollbackStatus.Stdout)){
            throw ('FIXTURE_DELETE_ROLLBACK_NOT_CLEAN:{0}' -f $deleteRollbackStatus.Stdout)
        }

        $fixtureHead=(Invoke-Git -GitPath $GitPath -ArgumentList @('-C',$workPath,'rev-parse','HEAD')).Stdout
        $snapshot=New-VerifiedFullRecoverySnapshot -SourceRoot $workPath -BackupRoot (Join-Path $fixtureRoot 'snapshots') -SourceCommit $fixtureHead -RetentionCount 10
        if(-not(Test-Path -LiteralPath $snapshot.archive_path -PathType Leaf) -or
           -not(Test-Path -LiteralPath $snapshot.receipt_path -PathType Leaf)){
            throw 'FIXTURE_FULL_RECOVERY_SNAPSHOT_FAILED'
        }
    }
    finally {
        if (Test-Path -LiteralPath $fixtureRoot) {
            Remove-Item -LiteralPath $fixtureRoot -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

function Resolve-PythonRunner {
    foreach($name in @('python.exe','python')){
        try {
            $command=Get-Command $name -ErrorAction Stop | Select-Object -First 1
            if(-not[string]::IsNullOrWhiteSpace($command.Source)){
                return [pscustomobject]@{Executable=$command.Source;PrefixArgs=@('-B')}
            }
        }
        catch {}
    }
    try {
        $command=Get-Command 'py.exe' -ErrorAction Stop | Select-Object -First 1
        if(-not[string]::IsNullOrWhiteSpace($command.Source)){
            return [pscustomobject]@{Executable=$command.Source;PrefixArgs=@('-3','-B')}
        }
    }
    catch {}
    throw 'PYTHON_EXECUTABLE_NOT_RESOLVED_FOR_ACTIVATION_PROOF'
}

function Get-KernelOptionalProperty {
    param($Object,[string]$Name,$Default=$null)
    if($null -eq $Object){return $Default}
    if($Object.PSObject.Properties.Name -notcontains $Name){return $Default}
    $value=$Object.$Name
    if($null -eq $value){return $Default}
    return $value
}

function Test-ManifestCreatesPath {
    param($PatchManifest,[string]$RelativePath)
    foreach($entry in @($PatchManifest.files)){
        if([string]$entry.operation -eq 'create' -and [string]$entry.path -eq $RelativePath){return $true}
    }
    return $false
}


function Get-UntrackedPythonBytecodeArtifacts {
    param([Parameter(Mandatory=$true)][string]$GitPath)

    $status=(Invoke-Git -GitPath $GitPath -ArgumentList @('status','--porcelain','--untracked-files=all')).Stdout
    $result=@()
    foreach($line in @($status -split "`r?`n")){
        if([string]::IsNullOrWhiteSpace($line) -or -not $line.StartsWith('?? ')){continue}
        $relative=$line.Substring(3).Trim()
        $normalized=$relative.Replace('\','/')
        if($normalized -match '(^|/)__pycache__/[^/]+\.py[co]$'){
            $result += $normalized
        }
    }
    return @($result | Sort-Object -Unique)
}

function Remove-UntrackedPythonBytecodeArtifacts {
    param([Parameter(Mandatory=$true)][string]$GitPath)

    $removed=@()
    foreach($relative in @(Get-UntrackedPythonBytecodeArtifacts -GitPath $GitPath)){
        $tracked=Invoke-Git -GitPath $GitPath -ArgumentList @('ls-files','--error-unmatch','--',$relative) -AllowedExitCodes @(0,1,128)
        if($tracked.ExitCode -eq 0){
            $State.FailureFamily='SOURCE_HYGIENE_SAFETY'
            throw ('REFUSE_TO_DELETE_TRACKED_PYTHON_BYTECODE:{0}' -f $relative)
        }

        $candidate=[IO.Path]::GetFullPath((Join-Path $WorkingSourcePath ($relative -replace '/','\')))
        $root=[IO.Path]::GetFullPath($WorkingSourcePath).TrimEnd('\') + '\'
        if(-not $candidate.StartsWith($root,[StringComparison]::OrdinalIgnoreCase)){
            $State.FailureFamily='SOURCE_HYGIENE_SAFETY'
            throw ('PYTHON_BYTECODE_PATH_ESCAPES_SOURCE:{0}' -f $relative)
        }

        if(Test-Path -LiteralPath $candidate -PathType Leaf){
            Remove-Item -LiteralPath $candidate -Force
            $removed += $relative
        }
    }
    return @($removed)
}

function Assert-NoUntrackedPythonBytecodeArtifacts {
    param([Parameter(Mandatory=$true)][string]$GitPath,[Parameter(Mandatory=$true)][string]$Stage)

    $hits=@(Get-UntrackedPythonBytecodeArtifacts -GitPath $GitPath)
    if($hits.Count -gt 0){
        $State.FailureFamily='PYTHON_SOURCE_HYGIENE'
        throw ('PYTHON_BYTECODE_WRITTEN_TO_WORKING_SOURCE:{0}:{1}' -f $Stage,($hits -join ','))
    }
}

function Invoke-MaterialCommitmentPreflightGate {
    param(
        $PatchManifest,
        [Parameter(Mandatory=$true)][string]$Stage,
        [Parameter(Mandatory=$true)][string]$SourceIdentity,
        [Parameter(Mandatory=$true)][string]$EvidencePath,
        [switch]$AllowBootstrapDefer
    )

    $spec=Get-KernelOptionalProperty -Object $PatchManifest -Name 'material_commitment_preflight' -Default $null
    if($null -eq $spec){
        $State.FailureFamily='MATERIAL_COMMITMENT_PREFLIGHT_MISSING'
        throw 'SEALED_STANDARD_PACKAGE_MISSING_MATERIAL_COMMITMENT_PREFLIGHT'
    }

    $implementation=Join-Path $WorkingSourcePath 'mcp\material_commitment_preflight.py'
    $bootstrap=[bool](Get-KernelOptionalProperty -Object $spec -Name 'bootstrap_activation' -Default $false)
    if(-not(Test-Path -LiteralPath $implementation -PathType Leaf)){
        if($AllowBootstrapDefer -and $bootstrap -and (Test-ManifestCreatesPath -PatchManifest $PatchManifest -RelativePath 'mcp/material_commitment_preflight.py')){
            return [pscustomobject]@{state='DEFERRED_BOOTSTRAP';stage=$Stage}
        }
        $State.FailureFamily='MATERIAL_COMMITMENT_PREFLIGHT_MISSING'
        throw 'MATERIAL_COMMITMENT_PREFLIGHT_IMPLEMENTATION_NOT_FOUND'
    }

    $request=($spec | ConvertTo-Json -Depth 32 | ConvertFrom-Json)
    foreach($pair in @(
        [pscustomobject]@{name='stage';value=$Stage},
        [pscustomobject]@{name='material';value=$true},
        [pscustomobject]@{name='commitment_target';value=[string]$PatchManifest.patch_id},
        [pscustomobject]@{name='authoritative_source_commit';value=$SourceIdentity},
        [pscustomobject]@{name='current_decision_state';value=('SEALED_STANDARD_' + $Stage)}
    )){
        $request | Add-Member -NotePropertyName ([string]$pair.name) -NotePropertyValue $pair.value -Force
    }

    $python=Resolve-PythonRunner
    $requestPath=[IO.Path]::GetTempFileName()
    $resolvePath=[IO.Path]::GetTempFileName()
    $receiptPath=[IO.Path]::GetTempFileName()
    $consumePath=[IO.Path]::GetTempFileName()
    try {
        [IO.File]::WriteAllText($requestPath,(($request | ConvertTo-Json -Depth 32)+"`r`n"),[Text.UTF8Encoding]::new($false))
        $resolveArgs=@($python.PrefixArgs)+@($implementation,'resolve','--request',$requestPath,'--source-root',$WorkingSourcePath,'--output',$resolvePath)
        $resolvedNative=Invoke-NativeCommand -Executable $python.Executable -ArgumentList $resolveArgs -AllowedExitCodes @(0,1)
        if(-not(Test-Path -LiteralPath $resolvePath -PathType Leaf)){
            $State.FailureFamily='MATERIAL_COMMITMENT_PREFLIGHT'
            throw ('MATERIAL_PREFLIGHT_RESULT_MISSING:{0}' -f $Stage)
        }
        $resolved=Get-Content -LiteralPath $resolvePath -Raw | ConvertFrom-Json
        if($resolvedNative.ExitCode -ne 0 -or [string]$resolved.result -ne 'PASS' -or [string]$resolved.mcp_control_decision.outcome -ne 'CONTINUE'){
            $State.FailureFamily='MATERIAL_COMMITMENT_PREFLIGHT'
            throw ('MATERIAL_PREFLIGHT_BLOCKED:{0}:{1}' -f $Stage,[string]$resolved.mcp_control_decision.outcome)
        }
        [IO.File]::WriteAllText($receiptPath,(($resolved.receipt | ConvertTo-Json -Depth 32)+"`r`n"),[Text.UTF8Encoding]::new($false))
        $consumeArgs=@($python.PrefixArgs)+@($implementation,'consume','--request',$requestPath,'--receipt',$receiptPath,'--source-root',$WorkingSourcePath,'--output',$consumePath)
        $consumedNative=Invoke-NativeCommand -Executable $python.Executable -ArgumentList $consumeArgs -AllowedExitCodes @(0,1)
        if(-not(Test-Path -LiteralPath $consumePath -PathType Leaf)){
            $State.FailureFamily='MATERIAL_COMMITMENT_CONSUMPTION'
            throw ('MATERIAL_CONSUMPTION_RESULT_MISSING:{0}' -f $Stage)
        }
        $consumed=Get-Content -LiteralPath $consumePath -Raw | ConvertFrom-Json
        if($consumedNative.ExitCode -ne 0 -or [string]$consumed.result -ne 'PASS' -or -not[bool]$consumed.receipt_consumed -or -not[bool]$consumed.freshness_verified){
            $State.FailureFamily='MATERIAL_COMMITMENT_CONSUMPTION'
            throw ('MATERIAL_CONSUMPTION_BLOCKED:{0}' -f $Stage)
        }
        if(-not[string]::IsNullOrWhiteSpace($EvidencePath)){
            $parent=Split-Path -Parent $EvidencePath
            if(-not[string]::IsNullOrWhiteSpace($parent)){[IO.Directory]::CreateDirectory($parent) | Out-Null}
            [IO.File]::Copy($consumePath,$EvidencePath,$true)
        }
        return [pscustomobject]@{state='PASS';stage=$Stage;receipt_id=[string]$resolved.receipt.control_decision_ref;evidence_path=$EvidencePath}
    }
    finally {
        Remove-Item -LiteralPath $requestPath,$resolvePath,$receiptPath,$consumePath -Force -ErrorAction SilentlyContinue
    }
}

function Get-DeclaredActivationProbes {
    param($PatchManifest)
    if($null -eq $PatchManifest){return @()}
    if($PatchManifest.PSObject.Properties.Name -notcontains 'activation_probes'){return @()}
    if($null -eq $PatchManifest.activation_probes){return @()}
    return @($PatchManifest.activation_probes)
}

function Assert-ActivationProbeManifest {
    param($PatchManifest)
    foreach($probe in @(Get-DeclaredActivationProbes -PatchManifest $PatchManifest)){
        $probeId='UNKNOWN'
        if($probe.PSObject.Properties.Name -contains 'id'){$probeId=[string]$probe.id}
        foreach($field in @('id','implementation_path','evidence_path','required_schema')){
            $value=''
            if($probe.PSObject.Properties.Name -contains $field){$value=[string]$probe.$field}
            if([string]::IsNullOrWhiteSpace($value)){
                throw ('ACTIVATION_PROBE_MANIFEST_FIELD_MISSING:{0}:{1}' -f $probeId,$field)
            }
        }
        if([IO.Path]::IsPathRooted([string]$probe.implementation_path)){
            throw ('ACTIVATION_PROBE_IMPLEMENTATION_MUST_BE_SOURCE_RELATIVE:{0}' -f $probeId)
        }
    }
}

function Get-CandidateRelativePath {
    param([Parameter(Mandatory=$true)][string]$RelativePath)

    if([IO.Path]::IsPathRooted($RelativePath)){
        throw ('CANDIDATE_VIEW_ROOTED_PATH_FORBIDDEN:{0}' -f $RelativePath)
    }
    $normalized=$RelativePath.Replace('\','/').Trim()
    if([string]::IsNullOrWhiteSpace($normalized)){
        throw 'CANDIDATE_VIEW_EMPTY_PATH_FORBIDDEN'
    }
    foreach($segment in @($normalized.Split('/'))){
        if([string]::IsNullOrWhiteSpace($segment) -or $segment -eq '.' -or $segment -eq '..'){
            throw ('CANDIDATE_VIEW_PATH_SEGMENT_FORBIDDEN:{0}' -f $RelativePath)
        }
    }
    return $normalized
}

function Get-CandidateViewTargetPath {
    param(
        [Parameter(Mandatory=$true)][string]$CandidateRoot,
        [Parameter(Mandatory=$true)][string]$RelativePath
    )

    $normalized=Get-CandidateRelativePath -RelativePath $RelativePath
    $rootFull=[IO.Path]::GetFullPath($CandidateRoot).TrimEnd([char[]]'\/')+[IO.Path]::DirectorySeparatorChar
    $targetFull=[IO.Path]::GetFullPath((Join-Path $CandidateRoot ($normalized -replace '/','\')))
    if(-not$targetFull.StartsWith($rootFull,[StringComparison]::OrdinalIgnoreCase)){
        throw ('CANDIDATE_VIEW_PATH_ESCAPE:{0}' -f $RelativePath)
    }
    return $targetFull
}

function New-SealedCandidateSourceView {
    param(
        [Parameter(Mandatory=$true)]$PatchManifest,
        [string]$SourceRepositoryPath=$WorkingSourcePath,
        [string]$PayloadRoot=$BundleRoot
    )

    if(-not(Test-Path -LiteralPath $SourceRepositoryPath -PathType Container)){
        throw ('CANDIDATE_VIEW_WORKING_SOURCE_NOT_FOUND:{0}' -f $SourceRepositoryPath)
    }
    $gitPath=Resolve-Executable 'git.exe'
    $candidateRoot=Join-Path ([IO.Path]::GetTempPath()) ('CerebroCandidateView-'+[guid]::NewGuid().ToString('N'))
    try {
        [void](Invoke-Git -GitPath $gitPath -ArgumentList @('clone','--local','--no-hardlinks','--no-checkout',$SourceRepositoryPath,$candidateRoot))
        [void](Invoke-Git -GitPath $gitPath -ArgumentList @('-C',$candidateRoot,'config','core.autocrlf','false'))
        [void](Invoke-Git -GitPath $gitPath -ArgumentList @('-C',$candidateRoot,'config','core.eol','lf'))
        [void](Invoke-Git -GitPath $gitPath -ArgumentList @('-C',$candidateRoot,'checkout','--detach',[string]$PatchManifest.expected_base_commit))
        $actualBase=(Invoke-Git -GitPath $gitPath -ArgumentList @('-C',$candidateRoot,'rev-parse','HEAD')).Stdout
        if($actualBase -ne [string]$PatchManifest.expected_base_commit){
            throw ('CANDIDATE_VIEW_BASE_IDENTITY_MISMATCH expected={0} actual={1}' -f [string]$PatchManifest.expected_base_commit,$actualBase)
        }

        $seen=@{}
        foreach($entry in @($PatchManifest.files)){
            $relative=Get-CandidateRelativePath -RelativePath ([string]$entry.path)
            if($seen.ContainsKey($relative)){
                throw ('CANDIDATE_VIEW_DUPLICATE_OPERATION:{0}' -f $relative)
            }
            $seen[$relative]=$true
            $target=Get-CandidateViewTargetPath -CandidateRoot $candidateRoot -RelativePath $relative
            $operation=[string]$entry.operation
            if($operation -eq 'delete'){
                if(Test-Path -LiteralPath $target -PathType Container){
                    throw ('CANDIDATE_VIEW_DELETE_DIRECTORY_FORBIDDEN:{0}' -f $relative)
                }
                if(Test-Path -LiteralPath $target -PathType Leaf){
                    Remove-Item -LiteralPath $target -Force
                }
                continue
            }
            if(@('create','replace') -notcontains $operation){
                throw ('CANDIDATE_VIEW_OPERATION_INVALID:{0}:{1}' -f $relative,$operation)
            }
            $payload=Join-Path $PayloadRoot (([string]$entry.payload_path) -replace '/','\')
            if(-not(Test-Path -LiteralPath $payload -PathType Leaf)){
                throw ('CANDIDATE_VIEW_PAYLOAD_MISSING:{0}' -f $relative)
            }
            $parent=Split-Path -Parent $target
            if(-not[string]::IsNullOrWhiteSpace($parent)){[IO.Directory]::CreateDirectory($parent)|Out-Null}
            [IO.File]::Copy($payload,$target,$true)
            if((Get-Sha256 -LiteralPath $target) -ne [string]$entry.sha256){
                throw ('CANDIDATE_VIEW_PAYLOAD_HASH_MISMATCH:{0}' -f $relative)
            }
        }
        return $candidateRoot
    }
    catch {
        if(Test-Path -LiteralPath $candidateRoot){
            Remove-Item -LiteralPath $candidateRoot -Recurse -Force -ErrorAction SilentlyContinue
        }
        throw
    }
}

function Assert-CandidateSourceCompositionProtocol {
    param([Parameter(Mandatory=$true)]$PatchManifest)

    $fixtureRoot=Join-Path ([IO.Path]::GetTempPath()) ('CerebroCandidateProtocol-'+[guid]::NewGuid().ToString('N'))
    $payloadRoot=Join-Path $fixtureRoot 'bundle'
    $replacement=Join-Path $payloadRoot 'payload\README.md'
    $created=Join-Path $payloadRoot 'payload\candidate-view-created.txt'
    $candidate=''
    try {
        [IO.Directory]::CreateDirectory((Split-Path -Parent $replacement))|Out-Null
        [IO.File]::WriteAllText($replacement,'CANDIDATE_VIEW_REPLACEMENT',[Text.UTF8Encoding]::new($false))
        [IO.File]::WriteAllText($created,'CANDIDATE_VIEW_CREATED',[Text.UTF8Encoding]::new($false))
        $fixtureManifest=[pscustomobject]@{
            expected_base_commit=[string]$PatchManifest.expected_base_commit
            files=@(
                [pscustomobject]@{operation='replace';path='README.md';payload_path='payload/README.md';sha256=(Get-Sha256 -LiteralPath $replacement)},
                [pscustomobject]@{operation='create';path='candidate-view-created.txt';payload_path='payload/candidate-view-created.txt';sha256=(Get-Sha256 -LiteralPath $created)},
                [pscustomobject]@{operation='delete';path='cerebro.yaml'}
            )
        }
        $candidate=New-SealedCandidateSourceView -PatchManifest $fixtureManifest -SourceRepositoryPath $WorkingSourcePath -PayloadRoot $payloadRoot
        $replacementTarget=Get-CandidateViewTargetPath -CandidateRoot $candidate -RelativePath 'README.md'
        $createdTarget=Get-CandidateViewTargetPath -CandidateRoot $candidate -RelativePath 'candidate-view-created.txt'
        $deletedTarget=Get-CandidateViewTargetPath -CandidateRoot $candidate -RelativePath 'cerebro.yaml'
        $baselineTarget=Get-CandidateViewTargetPath -CandidateRoot $candidate -RelativePath 'standards/source-authority.yaml'
        if([IO.File]::ReadAllText($replacementTarget) -ne 'CANDIDATE_VIEW_REPLACEMENT'){
            throw 'CANDIDATE_VIEW_REPLACE_PRECEDENCE_CANARY_FAILED'
        }
        if([IO.File]::ReadAllText($createdTarget) -ne 'CANDIDATE_VIEW_CREATED'){
            throw 'CANDIDATE_VIEW_CREATE_CANARY_FAILED'
        }
        if(Test-Path -LiteralPath $deletedTarget){
            throw 'CANDIDATE_VIEW_DELETE_ABSENCE_CANARY_FAILED'
        }
        if(-not(Test-Path -LiteralPath $baselineTarget -PathType Leaf)){
            throw 'CANDIDATE_VIEW_UNCHANGED_BASELINE_CANARY_FAILED'
        }
    }
    finally {
        if(-not[string]::IsNullOrWhiteSpace($candidate) -and (Test-Path -LiteralPath $candidate)){
            Remove-Item -LiteralPath $candidate -Recurse -Force -ErrorAction SilentlyContinue
        }
        if(Test-Path -LiteralPath $fixtureRoot){
            Remove-Item -LiteralPath $fixtureRoot -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

function Normalize-ActivationEvidenceForCac {
    param($Evidence,[string]$EvidencePath,[string]$ProbeId)
    $basisFiles=@(Get-KernelOptionalProperty -Object $Evidence -Name 'basis_files' -Default @())
    if($basisFiles.Count -eq 0){return $Evidence}
    $cacScript=Join-Path -Path $WorkingSourcePath -ChildPath 'tooling\validator\cerebro_contract_activation_closure.ps1'
    if(-not(Test-Path -LiteralPath $cacScript -PathType Leaf)){
        throw ('ACTIVATION_EVIDENCE_CAC_CONSUMER_MISSING:{0}' -f $ProbeId)
    }
    . $cacScript
    $producerFingerprint=[string](Get-KernelOptionalProperty -Object $Evidence -Name 'source_state_fingerprint' -Default '')
    $consumerFingerprint=Get-CacEvidenceBasisFingerprint -Root $WorkingSourcePath -RelativePaths $basisFiles
    if([string]::IsNullOrWhiteSpace($consumerFingerprint)){
        throw ('ACTIVATION_EVIDENCE_CONSUMER_FINGERPRINT_EMPTY:{0}' -f $ProbeId)
    }
    $Evidence|Add-Member -NotePropertyName producer_source_state_fingerprint -NotePropertyValue $producerFingerprint -Force
    $Evidence|Add-Member -NotePropertyName fingerprint_consumer -NotePropertyValue 'CEREBRO_CONTRACT_ACTIVATION_CLOSURE' -Force
    $Evidence|Add-Member -NotePropertyName fingerprint_consumer_normalized -NotePropertyValue ($producerFingerprint -ne $consumerFingerprint) -Force
    $Evidence.source_state_fingerprint=$consumerFingerprint
    [IO.File]::WriteAllText($EvidencePath,(($Evidence|ConvertTo-Json -Depth 64)+"`r`n"),[Text.UTF8Encoding]::new($false))
    return $Evidence
}

function Invoke-DeclaredActivationProbes {
    param($PatchManifest)
    $probes=@(Get-DeclaredActivationProbes -PatchManifest $PatchManifest)
    if($probes.Count -eq 0){return}
    Assert-ActivationProbeManifest -PatchManifest $PatchManifest
    $python=Resolve-PythonRunner
    foreach($probe in $probes){
        $State.ReachedStage='ACTIVATION_RUNTIME_PROOF'
        $implementation=Join-Path -Path $WorkingSourcePath -ChildPath (([string]$probe.implementation_path) -replace '/','\\')
        if(-not(Test-Path -LiteralPath $implementation -PathType Leaf)){
            $State.FailureFamily='ACTIVATION_RUNTIME_PROOF'
            throw ('ACTIVATION_PROBE_IMPLEMENTATION_MISSING:{0}' -f [string]$probe.implementation_path)
        }
        $evidencePath=[string]$probe.evidence_path
        if(-not[IO.Path]::IsPathRooted($evidencePath)){$evidencePath=Join-Path $WorkingSourcePath ($evidencePath -replace '/','\\')}
        $evidenceParent=Split-Path -Parent $evidencePath
        if(-not[string]::IsNullOrWhiteSpace($evidenceParent)){[IO.Directory]::CreateDirectory($evidenceParent) | Out-Null}
        Remove-Item -LiteralPath $evidencePath -Force -ErrorAction SilentlyContinue
        $args=@($python.PrefixArgs)+@($implementation,'activation-probe','--source-root',$WorkingSourcePath,'--output',$evidencePath)
        try {$probeResult=Invoke-NativeCommand -Executable $python.Executable -ArgumentList $args}
        catch {
            $State.FailureFamily='ACTIVATION_RUNTIME_PROOF'
            throw
        }
        if($probeResult.ExitCode -ne 0 -or -not(Test-Path -LiteralPath $evidencePath -PathType Leaf)){
            $State.FailureFamily='ACTIVATION_RUNTIME_PROOF'
            throw ('ACTIVATION_PROBE_EXECUTION_FAILED:{0}' -f [string]$probe.id)
        }
        try {$evidence=Get-Content -LiteralPath $evidencePath -Raw | ConvertFrom-Json}
        catch {
            $State.FailureFamily='ACTIVATION_RUNTIME_PROOF'
            throw ('ACTIVATION_PROBE_EVIDENCE_INVALID:{0}' -f [string]$probe.id)
        }
        if([string]$evidence.schema -ne [string]$probe.required_schema -or [string]$evidence.result -ne 'PASS'){
            $State.FailureFamily='ACTIVATION_RUNTIME_PROOF'
            throw ('ACTIVATION_PROBE_NOT_PASS:{0}' -f [string]$probe.id)
        }
        $evidence=Normalize-ActivationEvidenceForCac -Evidence $evidence -EvidencePath $evidencePath -ProbeId ([string]$probe.id)
        $State.ActivationProofs += [pscustomobject]@{id=[string]$probe.id;evidence_path=$evidencePath;source_state_fingerprint=[string]$evidence.source_state_fingerprint}
    }
}

function Invoke-SelfTest {
    $State.ReachedStage = 'SELFTEST_PARSE'
    if (-not [string]::IsNullOrWhiteSpace($LauncherPath)) { Assert-ParserClean $LauncherPath }
    $kernelPath = Join-Path -Path $BundleRoot -ChildPath 'kernel\Cerebro.StandardDeliveryKernel.ps1'
    Assert-ParserClean $kernelPath

    $State.ReachedStage = 'SELFTEST_STATIC_REGRESSION'
    foreach ($scanPath in @($LauncherPath,$kernelPath)) {
        if ([string]::IsNullOrWhiteSpace($scanPath)) { continue }
        $scanText = [IO.File]::ReadAllText($scanPath)
        if ($scanText -match '\$[A-Za-z_][A-Za-z0-9_]*:') {
            throw ('UNBRACED_VARIABLE_COLON_PATTERN:{0}' -f $scanPath)
        }
        $scriptRootPattern = '\$PSScript' + 'Root'
        if ($scanText -match $scriptRootPattern) {
            throw ('PSSCRIPTROOT_DEPENDENCY_FORBIDDEN:{0}' -f $scanPath)
        }
    }

    $State.ReachedStage = 'SELFTEST_PAYLOAD'
    $manifestObject = Read-Manifest
    Assert-PayloadIntegrity $manifestObject
    Assert-ActivationProbeManifest -PatchManifest $manifestObject
    Assert-CandidateSourceCompositionProtocol -PatchManifest $manifestObject

    $State.ReachedStage = 'SELFTEST_CANDIDATE_SOURCE_VIEW'
    $candidateView=''
    try {
        $candidateView=New-SealedCandidateSourceView -PatchManifest $manifestObject
        $State.ReachedStage = 'SELFTEST_HUMAN_CONTINUATION_SURFACE'
        $continuationValidator = Get-CandidateViewTargetPath -CandidateRoot $candidateView -RelativePath 'tooling/validator/continuation_surface_validation.py'
        if (-not (Test-Path -LiteralPath $continuationValidator -PathType Leaf)) {
            throw 'HUMAN_CONTINUATION_SURFACE_VALIDATOR_MISSING_FROM_CANDIDATE_VIEW'
        }
        $continuationPython = Resolve-PythonRunner
        $continuationArgs = @($continuationPython.PrefixArgs) + @($continuationValidator,'selftest')
        $continuationResult = Invoke-NativeCommand -Executable $continuationPython.Executable -ArgumentList $continuationArgs
        try { $continuationEvidence = $continuationResult.Stdout | ConvertFrom-Json }
        catch { throw 'HUMAN_CONTINUATION_SURFACE_SELFTEST_OUTPUT_INVALID' }
        if ($continuationResult.ExitCode -ne 0 -or [string]$continuationEvidence.result -ne 'PASS') {
            throw 'HUMAN_CONTINUATION_SURFACE_SELFTEST_FAILED'
        }

        $State.ReachedStage = 'SELFTEST_MCP_DELIVERY_PROFILE_ADAPTER'
        $deliveryAdapter = Get-CandidateViewTargetPath -CandidateRoot $candidateView -RelativePath 'tooling/delivery/cerebro_delivery.ps1'
        if (-not (Test-Path -LiteralPath $deliveryAdapter -PathType Leaf)) {
            throw 'MCP_DELIVERY_PROFILE_ADAPTER_MISSING_FROM_CANDIDATE_VIEW'
        }
        $adapterPowerShell = Resolve-Executable 'powershell.exe'
        $adapterResult = Invoke-NativeCommand -Executable $adapterPowerShell -ArgumentList @(
            '-NoLogo','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass',
            '-File',$deliveryAdapter,'-SelfTest'
        )
        try { $adapterEvidence = $adapterResult.Stdout | ConvertFrom-Json }
        catch { throw 'MCP_DELIVERY_PROFILE_ADAPTER_SELFTEST_OUTPUT_INVALID' }
        if ($adapterResult.ExitCode -ne 0 -or
            [string]$adapterEvidence.result -ne 'PASS' -or
            [string]$adapterEvidence.schema -ne 'cerebro-delivery-adapter-selftest/v0.3') {
            throw 'MCP_DELIVERY_PROFILE_ADAPTER_SELFTEST_FAILED'
        }
        foreach($requiredTest in @(
            'auto_without_evidence_fails_closed',
            'auto_replacement_scope_resolves_limited',
            'auto_structured_scope_resolves_standard',
            'auto_direct_workspace_resolves_full',
            'limited_rejects_create_scope',
            'delivery_profile_resolution_is_mcp_owned',
            'delivery_profile_namespaces_remain_distinct'
        )) {
            $matches=@($adapterEvidence.tests|Where-Object{
                [string]$_.name -eq $requiredTest -and [string]$_.result -eq 'PASS'
            })
            if($matches.Count -ne 1){
                throw ('MCP_DELIVERY_PROFILE_ADAPTER_CANARY_FAILED:{0}' -f $requiredTest)
            }
        }

        $State.ReachedStage = 'SELFTEST_HUMAN_EXECUTION_HANDOFF'
        $executionHandoffValidator = Get-CandidateViewTargetPath -CandidateRoot $candidateView -RelativePath 'tooling/validator/human_execution_handoff.py'
        if (-not (Test-Path -LiteralPath $executionHandoffValidator -PathType Leaf)) {
            throw 'HUMAN_EXECUTION_HANDOFF_VALIDATOR_MISSING_FROM_CANDIDATE_VIEW'
        }
        $executionHandoffArgs = @($continuationPython.PrefixArgs) + @($executionHandoffValidator,'selftest')
        $executionHandoffResult = Invoke-NativeCommand -Executable $continuationPython.Executable -ArgumentList $executionHandoffArgs
        try { $executionHandoffEvidence = $executionHandoffResult.Stdout | ConvertFrom-Json }
        catch { throw 'HUMAN_EXECUTION_HANDOFF_SELFTEST_OUTPUT_INVALID' }
        if ($executionHandoffResult.ExitCode -ne 0 -or [string]$executionHandoffEvidence.result -ne 'PASS') {
            throw 'HUMAN_EXECUTION_HANDOFF_SELFTEST_FAILED'
        }
    }
    finally {
        if(-not[string]::IsNullOrWhiteSpace($candidateView) -and (Test-Path -LiteralPath $candidateView)){
            Remove-Item -LiteralPath $candidateView -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    $State.ReachedStage = 'SELFTEST_NATIVE'
    $cmdPath = Resolve-Executable 'cmd.exe'
    $benign = Invoke-NativeCommand -Executable $cmdPath -ArgumentList @('/d','/s','/c','echo BENIGN_STDERR 1>&2 & exit /b 0')
    if ($benign.ExitCode -ne 0 -or $benign.Stderr -notmatch 'BENIGN_STDERR') {
        throw 'BENIGN_STDERR_ZERO_EXIT_TEST_FAILED'
    }
    $nonZeroWasRejected = $false
    try {
        [void](Invoke-NativeCommand -Executable $cmdPath -ArgumentList @('/d','/s','/c','echo EXPECTED_FAILURE 1>&2 & exit /b 7'))
    }
    catch {
        $nonZeroWasRejected = $true
    }
    if (-not $nonZeroWasRejected) { throw 'NATIVE_NONZERO_EXIT_TEST_FAILED' }

    $State.ReachedStage = 'SELFTEST_GIT_FIXTURE'
    $gitPath = Resolve-Executable 'git.exe'
    Invoke-LocalGitFixture $gitPath

    Write-Host 'CEREBRO_DELIVERY_KERNEL_SELFTEST=PASS'
    Write-Host 'POWERSHELL_PARSE_PASS=TRUE'
    Write-Host 'NATIVE_STDERR_ZERO_EXIT_PASS=TRUE'
    Write-Host 'NATIVE_NONZERO_EXIT_PASS=TRUE'
    Write-Host 'LOCAL_GIT_FIXTURE_PASS=TRUE'
    Write-Host 'BOUNDED_DELETE_ROLLBACK_PASS=TRUE'
    Write-Host 'FULL_RECOVERY_SNAPSHOT_PASS=TRUE'
    Write-Host 'PAYLOAD_HASH_PASS=TRUE'
    Write-Host 'CANDIDATE_SOURCE_COMPOSITION_PASS=TRUE'
    Write-Host 'HUMAN_CONTINUATION_SURFACE_SELFTEST_PASS=TRUE'
    Write-Host 'MCP_DELIVERY_PROFILE_ADAPTER_SELFTEST_PASS=TRUE'
}

function Get-KernelOrdinalStrings {
    param([object[]]$Values)
    [string[]]$items=@($Values | ForEach-Object {[string]$_})
    [Array]::Sort($items,[StringComparer]::Ordinal)
    return @($items)
}

function Get-KernelCandidateIdentity {
    param($PatchManifest)
    [string[]]$orderedPaths=Get-KernelOrdinalStrings -Values @($PatchManifest.files | ForEach-Object {[string]$_.path})
    $rows=@(
        foreach($path in $orderedPaths){
            $matches=@($PatchManifest.files | Where-Object {[string]$_.path -eq $path})
            if($matches.Count -ne 1){throw ('KERNEL_CANDIDATE_PATH_CARDINALITY_INVALID:{0}:{1}' -f $path,$matches.Count)}
            $item=$matches[0]
            ('{0}|{1}|{2}|{3}|{4}' -f
                [string]$item.path,
                [string](Get-KernelOptionalProperty $item 'operation' ''),
                [string](Get-KernelOptionalProperty $item 'expected_git_blob_sha' ''),
                [string](Get-KernelOptionalProperty $item 'final_git_blob_sha' ''),
                [string](Get-KernelOptionalProperty $item 'sha256' ''))
        }
    )
    $text=$rows -join "`n"
    $sha=[Security.Cryptography.SHA256]::Create()
    try {
        $bytes=[Text.Encoding]::UTF8.GetBytes($text)
        return ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-','').ToLowerInvariant()
    }
    finally {$sha.Dispose()}
}

function Assert-TargetRuntimeValidationReceipt {
    param($PatchManifest,[string]$ReceiptPath)
    $spec=Get-KernelOptionalProperty -Object $PatchManifest -Name 'target_runtime_validation' -Default $null
    if($null -eq $spec -or -not[bool](Get-KernelOptionalProperty $spec 'required' $false)){return}
    if([string]::IsNullOrWhiteSpace($ReceiptPath) -or -not(Test-Path -LiteralPath $ReceiptPath -PathType Leaf)){
        $State.FailureFamily='TARGET_RUNTIME_VALIDATION_REQUIRED'
        throw 'REQUIRED_TARGET_RUNTIME_NOT_EXECUTED_BEFORE_HANDOFF:RECEIPT_MISSING'
    }
    try {$receipt=Get-Content -LiteralPath $ReceiptPath -Raw | ConvertFrom-Json}
    catch {
        $State.FailureFamily='TARGET_RUNTIME_VALIDATION_REQUIRED'
        throw ('TARGET_RUNTIME_VALIDATION_RECEIPT_INVALID:{0}' -f $_.Exception.Message)
    }
    $expectedIdentity=Get-KernelCandidateIdentity -PatchManifest $PatchManifest
    [string[]]$expectedPaths=Get-KernelOrdinalStrings -Values @($PatchManifest.files | ForEach-Object {[string]$_.path})
    [string[]]$actualPaths=Get-KernelOrdinalStrings -Values @($receipt.changed_paths | ForEach-Object {[string]$_})
    if([string]$receipt.schema -ne [string]$spec.receipt_schema -or
       [string]$receipt.result -ne 'PASS' -or
       [string]$receipt.patch_id -ne [string]$PatchManifest.patch_id -or
       [string]$receipt.source_base_commit -ne [string]$PatchManifest.expected_base_commit -or
       [string]$receipt.candidate_identity -ne $expectedIdentity -or
       [string]$receipt.target_profile -ne [string]$spec.profile -or
       -not[bool]$receipt.target_runtime_execution -or
       [bool]$receipt.authoritative_source_mutated -or
       [string]$receipt.producer_consumer_compatibility -ne 'PASS' -or
       [string]$receipt.cac.result -ne 'PASS' -or
       [string]$receipt.deep_assurance.result -ne 'PASS' -or
       [int]$receipt.deep_assurance.required_runs -lt 3 -or
       (($expectedPaths -join "`n") -ne ($actualPaths -join "`n"))){
        $State.FailureFamily='TARGET_RUNTIME_VALIDATION_REQUIRED'
        throw 'TARGET_RUNTIME_VALIDATION_RECEIPT_DOES_NOT_MATCH_EXACT_CANDIDATE'
    }
}

function Invoke-RequiredTargetRuntimeValidation {
    param($PatchManifest)
    $spec=Get-KernelOptionalProperty -Object $PatchManifest -Name 'target_runtime_validation' -Default $null
    if($null -eq $spec -or -not[bool](Get-KernelOptionalProperty $spec 'required' $false)){return ''}
    if(-not[string]::IsNullOrWhiteSpace($TargetRuntimeValidationReceipt)){
        Assert-TargetRuntimeValidationReceipt -PatchManifest $PatchManifest -ReceiptPath $TargetRuntimeValidationReceipt
        $State.TargetRuntimeValidationReceipt=$TargetRuntimeValidationReceipt
        return $TargetRuntimeValidationReceipt
    }

    $validator=Join-Path $WorkingSourcePath 'tooling\validator\target-runtime\Invoke-CerebroWindowsPowerShellValidation.ps1'
    if(-not(Test-Path -LiteralPath $validator -PathType Leaf)){
        $State.FailureFamily='TARGET_RUNTIME_VALIDATION_REQUIRED'
        throw 'TARGET_RUNTIME_VALIDATOR_NOT_FOUND'
    }
    $capsuleRoot=Join-Path $BundleRoot 'capsule'
    if(-not(Test-Path -LiteralPath (Join-Path $capsuleRoot 'capsule.json') -PathType Leaf)){
        $State.FailureFamily='TARGET_RUNTIME_VALIDATION_REQUIRED'
        throw 'TARGET_RUNTIME_CAPSULE_NOT_FOUND'
    }
    $receiptRoot='D:\Cerebro\Run\receipts'
    [IO.Directory]::CreateDirectory($receiptRoot)|Out-Null
    $receiptPath=Join-Path $receiptRoot ('CEREBRO_TARGET_RUNTIME_'+(Get-Date -Format 'yyyyMMdd-HHmmss')+'.json')
    $manifestPath=Join-Path $BundleRoot 'manifest.json'
    $State.ReachedStage='TARGET_RUNTIME_VALIDATION_EXECUTE'
    try {
        $trvOutput=& $validator -CandidateRoot $WorkingSourcePath -ManifestPath $manifestPath `
            -CapsuleRoot $capsuleRoot -RepositoryRoot $WorkingSourcePath `
            -OutputPath $receiptPath -ProfileId ([string]$spec.profile)
    }
    catch {
        $State.FailureFamily='TARGET_RUNTIME_VALIDATION_REQUIRED'
        throw
    }
    Assert-TargetRuntimeValidationReceipt -PatchManifest $PatchManifest -ReceiptPath $receiptPath
    $State.TargetRuntimeValidationReceipt=$receiptPath
    return $receiptPath
}

function Invoke-Apply {
    $State.Manifest = Read-Manifest
    Assert-PayloadIntegrity $State.Manifest
    if(-not[string]::IsNullOrWhiteSpace($TargetRuntimeValidationReceipt)){
        Assert-TargetRuntimeValidationReceipt -PatchManifest $State.Manifest -ReceiptPath $TargetRuntimeValidationReceipt
    }

    $State.ReachedStage = 'SEALED_DELIVERY_PROFILE'
    if([string]$State.Manifest.delivery_profile -ne 'STANDARD'){
        $State.FailureFamily = 'DELIVERY_PROFILE_IDENTITY'
        throw ('DELIVERY_PROFILE_MISMATCH expected=STANDARD actual={0}' -f [string]$State.Manifest.delivery_profile)
    }
    if([string]$State.Manifest.delivery_execution_contract -ne 'CEREBRO-STANDARD-DELIVERY-KERNEL-001'){
        $State.FailureFamily = 'DELIVERY_EXECUTION_CONTRACT_IDENTITY'
        throw ('DELIVERY_EXECUTION_CONTRACT_MISMATCH:{0}' -f [string]$State.Manifest.delivery_execution_contract)
    }

    $gitPath = Resolve-Executable 'git.exe'

    $State.ReachedStage = 'SOURCE_PREFLIGHT'
    if (-not (Test-Path -LiteralPath $WorkingSourcePath -PathType Container)) {
        $State.FailureFamily = 'WORKING_SOURCE_PATH'
        throw ('WORKING_SOURCE_NOT_FOUND:{0}' -f $WorkingSourcePath)
    }

    Push-Location $WorkingSourcePath
    try {
        $root = [IO.Path]::GetFullPath((Invoke-Git -GitPath $gitPath -ArgumentList @('rev-parse','--show-toplevel')).Stdout)
        if (-not $root.Equals([IO.Path]::GetFullPath($WorkingSourcePath),[StringComparison]::OrdinalIgnoreCase)) {
            $State.FailureFamily = 'WORKING_SOURCE_BINDING'
            throw ('WORKING_SOURCE_BINDING_MISMATCH:{0}' -f $root)
        }
        $branch = (Invoke-Git -GitPath $gitPath -ArgumentList @('branch','--show-current')).Stdout
        if ($branch -ne [string]$State.Manifest.branch) {
            $State.FailureFamily = 'WRONG_BRANCH'
            throw ('BRANCH_MISMATCH expected={0} actual={1}' -f $State.Manifest.branch,$branch)
        }
        $remoteUrl = (Invoke-Git -GitPath $gitPath -ArgumentList @('remote','get-url','origin')).Stdout
        if ($remoteUrl -notmatch 'morgul-tech/Cerebro-Source-1\.0') {
            $State.FailureFamily = 'WRONG_REMOTE'
            throw ('REMOTE_MISMATCH:{0}' -f $remoteUrl)
        }
        $State.ReachedStage = 'TRANSIENT_SOURCE_HYGIENE'
        $cleanedBytecode=@(Remove-UntrackedPythonBytecodeArtifacts -GitPath $gitPath)
        if($cleanedBytecode.Count -gt 0){
            $State.TransientCleanup=$cleanedBytecode
        }

        $dirty = (Invoke-Git -GitPath $gitPath -ArgumentList @('status','--porcelain','--untracked-files=all')).Stdout
        if (-not [string]::IsNullOrWhiteSpace($dirty)) {
            $State.FailureFamily = 'DIRTY_WORKTREE'
            throw ('WORKTREE_NOT_CLEAN:{0}' -f $dirty)
        }

        $State.ReachedStage = 'FRESH_REMOTE_FETCH'
        [void](Invoke-Git -GitPath $gitPath -ArgumentList @('fetch','--no-tags','origin',[string]$State.Manifest.branch))
        $remoteHead = (Invoke-Git -GitPath $gitPath -ArgumentList @('rev-parse',('refs/remotes/origin/{0}' -f $State.Manifest.branch))).Stdout
        $localHead = (Invoke-Git -GitPath $gitPath -ArgumentList @('rev-parse','HEAD')).Stdout

        if ($localHead -eq $remoteHead -and (Test-AllFinalBlobsAtHead -GitPath $gitPath -PatchManifest $State.Manifest)) {
            $receiptRoot='D:\Cerebro\Run\receipts'
            [IO.Directory]::CreateDirectory($receiptRoot)|Out-Null
            $receiptPath=Join-Path $receiptRoot ('CEREBRO_DELIVERY_KERNEL_'+(Get-Date -Format 'yyyyMMdd-HHmmss')+'.json')
            $receipt=[ordered]@{
                schema='cerebro-delivery-kernel-receipt/v2'
                result='ALREADY_APPLIED'
                kernel=$KernelId
                patch_id=[string]$State.Manifest.patch_id
                attempt_id=$AttemptId
                authoritative_source='origin/main'
                working_source=$WorkingSourcePath
                authoritative_commit=$remoteHead
                working_source_commit=$localHead
                authoritative_source_equals_working_source=$true
                source_equality='VERIFIED'
                working_tree='CLEAN'
                sync_action='NOT_REQUIRED'
                cerebro_sync_verified=$true
                completed_at_utc=[DateTime]::UtcNow.ToString('o')
            }
            [IO.File]::WriteAllText($receiptPath,(($receipt|ConvertTo-Json -Depth 8)+"`r`n"),[Text.UTF8Encoding]::new($false))
            Write-Host 'CEREBRO_DELIVERY_KERNEL_RESULT=ALREADY_APPLIED'
            Write-Host ('AUTHORITATIVE_COMMIT={0}' -f $remoteHead)
            Write-Host 'SOURCE_EQUALITY=VERIFIED'
            Write-Host 'CEREBRO_SYNC_VERIFIED=TRUE'
            Write-Host ('DELIVERY_RECEIPT={0}' -f $receiptPath)
            Write-Host ('RECEIPT={0}' -f $receiptPath)
            return
        }

        if ($remoteHead -ne [string]$State.Manifest.expected_base_commit) {
            $State.FailureFamily = 'WRONG_BASE_COMMIT'
            throw ('SOURCE_BASE_CHANGED expected={0} actual={1}' -f $State.Manifest.expected_base_commit,$remoteHead)
        }

        if ($localHead -ne $remoteHead) {
            $ancestor = Invoke-Git -GitPath $gitPath -ArgumentList @('merge-base','--is-ancestor',$localHead,$remoteHead) -AllowedExitCodes @(0,1)
            if ($ancestor.ExitCode -ne 0) {
                $State.FailureFamily = 'LOCAL_AHEAD_OR_DIVERGED'
                throw ('LOCAL_NOT_SAFE_TO_FAST_FORWARD local={0} remote={1}' -f $localHead,$remoteHead)
            }
            [void](Invoke-Git -GitPath $gitPath -ArgumentList @('merge','--ff-only',('refs/remotes/origin/{0}' -f $State.Manifest.branch)))
            $localHead = (Invoke-Git -GitPath $gitPath -ArgumentList @('rev-parse','HEAD')).Stdout
        }
        if ($localHead -ne [string]$State.Manifest.expected_base_commit) {
            $State.FailureFamily = 'WRONG_BASE_COMMIT'
            throw ('LOCAL_BASE_MISMATCH:{0}' -f $localHead)
        }

        $State.ReachedStage = 'BASELINE_FILE_IDENTITY'
        foreach ($fileEntry in @($State.Manifest.files)) {
            if (@('replace','delete') -contains [string]$fileEntry.operation) {
                $blob = (Invoke-Git -GitPath $gitPath -ArgumentList @('rev-parse',('HEAD:{0}' -f [string]$fileEntry.path))).Stdout
                if ($blob -ne [string]$fileEntry.expected_git_blob_sha) {
                    $State.FailureFamily = 'PATCH_BASE_IDENTITY'
                    throw ('BASELINE_BLOB_MISMATCH:{0}:expected={1}:actual={2}' -f $fileEntry.path,$fileEntry.expected_git_blob_sha,$blob)
                }
                $physical=Join-Path -Path $WorkingSourcePath -ChildPath (([string]$fileEntry.path) -replace '/','\')
                if(-not(Test-Path -LiteralPath $physical -PathType Leaf)){
                    $State.FailureFamily='PATCH_BASE_IDENTITY'
                    throw ('BASELINE_FILE_MISSING:{0}' -f $fileEntry.path)
                }
            }
            else {
                $listing = Invoke-Git -GitPath $gitPath -ArgumentList @('ls-tree','HEAD','--',[string]$fileEntry.path)
                if (-not [string]::IsNullOrWhiteSpace($listing.Stdout)) {
                    $State.FailureFamily = 'PATCH_BASE_IDENTITY'
                    throw ('CREATE_TARGET_ALREADY_TRACKED:{0}' -f $fileEntry.path)
                }
                $physical = Join-Path -Path $WorkingSourcePath -ChildPath (([string]$fileEntry.path) -replace '/','\')
                if (Test-Path -LiteralPath $physical) {
                    $State.FailureFamily = 'PATCH_BASE_IDENTITY'
                    throw ('CREATE_TARGET_ALREADY_EXISTS:{0}' -f $fileEntry.path)
                }
            }
        }

        $dryRunLease=('--force-with-lease=refs/heads/{0}:{1}' -f [string]$State.Manifest.branch,$remoteHead)
        [void](Invoke-Git -GitPath $gitPath -ArgumentList @('push','--dry-run',$dryRunLease,'origin',('HEAD:refs/heads/{0}' -f [string]$State.Manifest.branch)))

        $State.ReachedStage = 'MATERIAL_COMMITMENT_PREFLIGHT_EXECUTE'
        $executeEvidence=Join-Path 'D:\Cerebro\Run\audits' 'CEREBRO_STANDARD_MATERIAL_EXECUTE_PREFLIGHT.json'
        [void](Invoke-MaterialCommitmentPreflightGate -PatchManifest $State.Manifest -Stage 'MATERIAL_EXECUTE' -SourceIdentity $localHead -EvidencePath $executeEvidence -AllowBootstrapDefer)
        Assert-NoUntrackedPythonBytecodeArtifacts -GitPath $gitPath -Stage 'MATERIAL_EXECUTE'

        $State.ReachedStage='FULL_RECOVERY_SNAPSHOT'
        $State.FullRecoverySnapshot=New-VerifiedFullRecoverySnapshot `
            -SourceRoot $WorkingSourcePath -BackupRoot 'D:\Cerebro\Backups' `
            -SourceCommit $localHead -RetentionCount 10

        $State.ReachedStage='LOCAL_EXECUTION_ENVIRONMENT_PREFLIGHT'
        Assert-DeclaredTargetMutationCapabilities -SourceRoot $WorkingSourcePath -PatchManifest $State.Manifest
        $postCapabilityStatus=(Invoke-Git -GitPath $gitPath -ArgumentList @('status','--porcelain','--untracked-files=all')).Stdout
        if(-not[string]::IsNullOrWhiteSpace($postCapabilityStatus)){
            $State.FailureFamily='LOCAL_EXECUTION_ENVIRONMENT'
            throw ('CAPABILITY_PROBE_DIRTIED_SOURCE:{0}' -f $postCapabilityStatus)
        }

        $State.ReachedStage = 'BACKUP'
        $backupRoot = 'D:\Cerebro\Backups'
        [IO.Directory]::CreateDirectory($backupRoot) | Out-Null
        $State.BackupDirectory = Join-Path -Path $backupRoot -ChildPath ('delivery-kernel-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
        [IO.Directory]::CreateDirectory($State.BackupDirectory) | Out-Null
        foreach ($fileEntry in @($State.Manifest.files)) {
            if (@('replace','delete') -notcontains [string]$fileEntry.operation) { continue }
            $target = Join-Path -Path $WorkingSourcePath -ChildPath (([string]$fileEntry.path) -replace '/','\')
            $backup = Join-Path -Path $State.BackupDirectory -ChildPath (([string]$fileEntry.path) -replace '/','\')
            [IO.Directory]::CreateDirectory((Split-Path -Parent $backup)) | Out-Null
            Copy-Item -LiteralPath $target -Destination $backup -Force
            if ((Get-Sha256 $backup) -ne (Get-Sha256 $target)) {
                $State.FailureFamily = 'BACKUP_INTEGRITY'
                throw ('BACKUP_HASH_MISMATCH:{0}' -f $fileEntry.path)
            }
        }

        $State.ReachedStage = 'EXACT_BYTE_INSTALL'
        foreach ($fileEntry in @($State.Manifest.files)) {
            $target = Join-Path -Path $WorkingSourcePath -ChildPath (([string]$fileEntry.path) -replace '/','\')
            if([string]$fileEntry.operation -eq 'delete'){
                Remove-ExactTargetFile -TargetPath $target
            }
            else{
                $payload = Join-Path -Path $BundleRoot -ChildPath ([string]$fileEntry.payload_path)
                Install-ExactPayloadFile -PayloadPath $payload -TargetPath $target -ExpectedSha256 ([string]$fileEntry.sha256)
            }
            $State.MutationStarted = $true
        }

        $State.ReachedStage = 'INSTALLED_BYTE_VERIFY'
        foreach ($fileEntry in @($State.Manifest.files)) {
            $target = Join-Path -Path $WorkingSourcePath -ChildPath (([string]$fileEntry.path) -replace '/','\')
            if([string]$fileEntry.operation -eq 'delete'){
                if(Test-Path -LiteralPath $target){
                    $State.FailureFamily='INSTALLED_BYTE_INTEGRITY'
                    throw ('DELETED_TARGET_STILL_EXISTS:{0}' -f $fileEntry.path)
                }
                continue
            }
            if ((Get-Sha256 $target) -ne [string]$fileEntry.sha256 -or
                (Get-GitBlobShaFromFile $target) -ne [string]$fileEntry.final_git_blob_sha) {
                $State.FailureFamily = 'INSTALLED_BYTE_INTEGRITY'
                throw ('INSTALLED_BYTE_IDENTITY_MISMATCH:{0}' -f $fileEntry.path)
            }
        }

        $State.ReachedStage = 'SOURCE_DIFF_VALIDATION'
        [void](Invoke-Git -GitPath $gitPath -ArgumentList @('diff','--check'))
        foreach ($assertion in @($State.Manifest.required_text_assertions)) {
            $assertPath = Join-Path -Path $WorkingSourcePath -ChildPath (([string]$assertion.path) -replace '/','\')
            $assertContent = [IO.File]::ReadAllText($assertPath)
            foreach ($requiredText in @($assertion.contains)) {
                if (-not $assertContent.Contains([string]$requiredText)) {
                    $State.FailureFamily = 'SEMANTIC_ASSERTION'
                    throw ('REQUIRED_TEXT_MISSING:{0}:{1}' -f $assertion.path,$requiredText)
                }
            }
        }

        $State.ReachedStage = 'ACTIVE_SOURCE_INTEGRITY_CLOSURE'
        $ascScript = Join-Path -Path $WorkingSourcePath -ChildPath 'tooling\validator\Cerebro.ActiveSourceIntegrityClosure.ps1'
        if(-not(Test-Path -LiteralPath $ascScript -PathType Leaf)){
            $State.FailureFamily = 'ACTIVE_SOURCE_CLOSURE_MISSING'
            throw 'ACTIVE_SOURCE_INTEGRITY_CLOSURE_NOT_FOUND'
        }

        . $ascScript
        $ascResult = Invoke-CerebroActiveSourceIntegrityClosure -Root $WorkingSourcePath
        if([string]$ascResult.result -ne 'PASS'){
            $State.FailureFamily = 'ACTIVE_SOURCE_CLOSURE_FAILURE'
            throw ('ACTIVE_SOURCE_INTEGRITY_CLOSURE_FAILED:{0}' -f @($ascResult.findings).Count)
        }

        [void](Invoke-RequiredTargetRuntimeValidation -PatchManifest $State.Manifest)

        $State.ReachedStage = 'MATERIAL_COMMITMENT_PREFLIGHT_PUBLISH'
        $publishEvidence=Join-Path 'D:\Cerebro\Run\audits' 'CEREBRO_STANDARD_MATERIAL_PREFLIGHT_CALL_PATH.json'
        [void](Invoke-MaterialCommitmentPreflightGate -PatchManifest $State.Manifest -Stage 'GOVERNING_PUBLISH' -SourceIdentity $localHead -EvidencePath $publishEvidence)
        Assert-NoUntrackedPythonBytecodeArtifacts -GitPath $gitPath -Stage 'GOVERNING_PUBLISH'

        Invoke-DeclaredActivationProbes -PatchManifest $State.Manifest
        Assert-NoUntrackedPythonBytecodeArtifacts -GitPath $gitPath -Stage 'ACTIVATION_PROBES'

        $State.ReachedStage = 'CONTRACT_ACTIVATION_CLOSURE'
        $cacScript = Join-Path -Path $WorkingSourcePath -ChildPath 'tooling\validator\cerebro_contract_activation_closure.ps1'
        if (Test-Path -LiteralPath $cacScript -PathType Leaf) {
            . $cacScript
            $cacResult = Invoke-CerebroContractActivationClosure -Root $WorkingSourcePath -PassThru
            if ([string]$cacResult.result -ne 'PASS') {
                $State.FailureFamily = 'CONTRACT_ACTIVATION_GAP'
                $blockingFindings = @($cacResult.blocking_findings)
                $blockingCount = $blockingFindings.Count
                $blockingSummary = @($blockingFindings | ForEach-Object { ('{0}|{1}|{2}|{3}' -f [string]$_.code,[string]$_.scope,[string]$_.subject,[string]$_.message) }) -join '; '
                throw ('CONTRACT_ACTIVATION_CLOSURE_FAILED count={0}; findings={1}' -f $blockingCount,$blockingSummary)
            }

            try {
                [void](Invoke-CerebroContractActivationAudit -Root $WorkingSourcePath)
            }
            catch {
            }
        }

        $State.ReachedStage = 'CANONICAL_SYNC'
        $syncScript = Join-Path -Path $WorkingSourcePath -ChildPath 'tooling\builder\templates\pshell\cerebro_sync.ps1'
        if (-not (Test-Path -LiteralPath $syncScript -PathType Leaf)) {
            $State.FailureFamily = 'CANONICAL_SYNC'
            throw ('CEREBRO_SYNC_NOT_FOUND:{0}' -f $syncScript)
        }
        $changedPaths = @($State.Manifest.files | ForEach-Object { [string]$_.path })
        $State.SyncStarted = $true
        & $syncScript -RepoPath $WorkingSourcePath -Remote 'origin' -Branch ([string]$State.Manifest.branch) `
            -CommitMessage ([string]$State.Manifest.commit_message) -Paths $changedPaths

        $State.ReachedStage = 'POST_SYNC_EQUALITY'
        [void](Invoke-Git -GitPath $gitPath -ArgumentList @('fetch','--no-tags','origin',[string]$State.Manifest.branch))
        $finalRemote = (Invoke-Git -GitPath $gitPath -ArgumentList @('rev-parse',('refs/remotes/origin/{0}' -f $State.Manifest.branch))).Stdout
        $finalLocal = (Invoke-Git -GitPath $gitPath -ArgumentList @('rev-parse','HEAD')).Stdout
        $finalDirty = (Invoke-Git -GitPath $gitPath -ArgumentList @('status','--porcelain','--untracked-files=all')).Stdout
        if ($finalRemote -ne $finalLocal -or -not [string]::IsNullOrWhiteSpace($finalDirty) -or
            -not (Test-AllFinalBlobsAtHead -GitPath $gitPath -PatchManifest $State.Manifest)) {
            $State.FailureFamily = 'POST_SYNC_EQUALITY_FAILURE'
            throw ('POST_SYNC_PROOF_FAILED local={0} remote={1} dirty={2}' -f $finalLocal,$finalRemote,$finalDirty)
        }

        $State.ReachedStage = 'RECEIPT'
        $receiptRoot = 'D:\Cerebro\Run\receipts'
        [IO.Directory]::CreateDirectory($receiptRoot) | Out-Null
        $receiptPath = Join-Path -Path $receiptRoot -ChildPath ('CEREBRO_DELIVERY_KERNEL_' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.json')
        $receipt = [ordered]@{
            schema='cerebro-delivery-kernel-receipt/v2'
            result='PASS'
            kernel=$KernelId
            patch_id=[string]$State.Manifest.patch_id
            attempt_id=$AttemptId
            authoritative_source='origin/main'
            working_source=$WorkingSourcePath
            authoritative_commit=$finalRemote
            working_source_commit=$finalLocal
            authoritative_source_equals_working_source=$true
            source_equality='VERIFIED'
            working_tree='CLEAN'
            sync_action='CEREBRO_SYNC_EXECUTED'
            cerebro_sync_verified=$true
            full_recovery_snapshot=[ordered]@{
                archive_path=[string]$State.FullRecoverySnapshot.archive_path
                receipt_path=[string]$State.FullRecoverySnapshot.receipt_path
                archive_sha256=[string]$State.FullRecoverySnapshot.archive_sha256
                inventory_sha256=[string]$State.FullRecoverySnapshot.inventory_sha256
            }
            target_runtime_validation_receipt=$State.TargetRuntimeValidationReceipt
            operation_counts=[ordered]@{
                create=@($State.Manifest.files | Where-Object {[string]$_.operation -eq 'create'}).Count
                replace=@($State.Manifest.files | Where-Object {[string]$_.operation -eq 'replace'}).Count
                delete=@($State.Manifest.files | Where-Object {[string]$_.operation -eq 'delete'}).Count
            }
            deleted_paths=@($State.Manifest.files | Where-Object {[string]$_.operation -eq 'delete'} | ForEach-Object {[string]$_.path})
            activation_proofs=@($State.ActivationProofs)
            completed_at_utc=[DateTime]::UtcNow.ToString('o')
        }
        [IO.File]::WriteAllText($receiptPath,(($receipt | ConvertTo-Json -Depth 8) + "`r`n"),[Text.UTF8Encoding]::new($false))

        Write-Host ''
        Write-Host 'CEREBRO_STANDARD_DELIVERY_KERNEL=PASS'
        Write-Host ('PATCH_ID={0}' -f $State.Manifest.patch_id)
        Write-Host ('AUTHORITATIVE_COMMIT={0}' -f $finalRemote)
        Write-Host ('WORKING_SOURCE_COMMIT={0}' -f $finalLocal)
        Write-Host 'SOURCE_EQUALITY=VERIFIED'
        Write-Host 'CEREBRO_SYNC_VERIFIED=TRUE'
        Write-Host ('DELIVERY_RECEIPT={0}' -f $receiptPath)
        Write-Host ('RECEIPT={0}' -f $receiptPath)
    }
    finally {
        Pop-Location -ErrorAction SilentlyContinue
    }
}

try {
    if ($Mode -eq 'SelfTest') { Invoke-SelfTest } else { Invoke-Apply }
}
catch {
    $errorText = $_.Exception.Message
    if ($State.MutationStarted -and -not $State.SyncStarted -and $null -ne $State.Manifest -and
        -not [string]::IsNullOrWhiteSpace($State.BackupDirectory)) {
        try {
            foreach ($fileEntry in @($State.Manifest.files)) {
                $target = Join-Path -Path $WorkingSourcePath -ChildPath (([string]$fileEntry.path) -replace '/','\')
                if (@('replace','delete') -contains [string]$fileEntry.operation) {
                    $backup = Join-Path -Path $State.BackupDirectory -ChildPath (([string]$fileEntry.path) -replace '/','\')
                    if (Test-Path -LiteralPath $backup -PathType Leaf) {
                        [IO.Directory]::CreateDirectory((Split-Path -Parent $target)) | Out-Null
                        Copy-Item -LiteralPath $backup -Destination $target -Force
                    }
                }
                else {
                    if (Test-Path -LiteralPath $target -PathType Leaf) {
                        Remove-Item -LiteralPath $target -Force
                    }
                }
            }
        }
        catch {}
    }
    Write-Host ''
    Write-Host 'CEREBRO_STANDARD_DELIVERY_KERNEL=FAIL'
    Write-Host ('REACHED_STAGE={0}' -f $State.ReachedStage)
    Write-Host ('FAILURE_FAMILY={0}' -f $State.FailureFamily)
    Write-Host ('ERROR={0}' -f $errorText)
    if (-not [string]::IsNullOrWhiteSpace($State.BackupDirectory)) {
        Write-Host ('BACKUP={0}' -f $State.BackupDirectory)
    }
    if($null -ne $State.FullRecoverySnapshot){
        Write-Host ('FULL_RECOVERY_SNAPSHOT={0}' -f [string]$State.FullRecoverySnapshot.archive_path)
        Write-Host ('FULL_RECOVERY_SNAPSHOT_RECEIPT={0}' -f [string]$State.FullRecoverySnapshot.receipt_path)
    }
    throw
}
