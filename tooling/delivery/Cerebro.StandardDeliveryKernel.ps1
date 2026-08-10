[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)]
    [ValidateSet('SelfTest','Apply')]
    [string]$Mode,
    [Parameter(Mandatory=$true)]
    [string]$BundleRoot,
    [string]$WorkingSourcePath = 'D:\Cerebro\Source\Cerebro_Source_v1.0',
    [string]$LauncherPath = '',
    [string]$AttemptId = ''
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
        [void](Invoke-Git -GitPath $GitPath -ArgumentList @('-C',$seedPath,'add','baseline.txt'))
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
    Write-Host 'PAYLOAD_HASH_PASS=TRUE'
}

function Invoke-Apply {
    $State.Manifest = Read-Manifest
    Assert-PayloadIntegrity $State.Manifest

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
            Write-Host 'CEREBRO_DELIVERY_KERNEL_RESULT=ALREADY_APPLIED'
            Write-Host ('AUTHORITATIVE_COMMIT={0}' -f $remoteHead)
            Write-Host 'SOURCE_EQUALITY=VERIFIED'
            Write-Host 'CEREBRO_SYNC_VERIFIED=TRUE'
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
            if ([string]$fileEntry.operation -eq 'replace') {
                $blob = (Invoke-Git -GitPath $gitPath -ArgumentList @('rev-parse',('HEAD:{0}' -f [string]$fileEntry.path))).Stdout
                if ($blob -ne [string]$fileEntry.expected_git_blob_sha) {
                    $State.FailureFamily = 'PATCH_BASE_IDENTITY'
                    throw ('BASELINE_BLOB_MISMATCH:{0}:expected={1}:actual={2}' -f $fileEntry.path,$fileEntry.expected_git_blob_sha,$blob)
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

        $State.ReachedStage = 'MATERIAL_COMMITMENT_PREFLIGHT_EXECUTE'
        $executeEvidence=Join-Path 'D:\Cerebro\Run\audits' 'CEREBRO_STANDARD_MATERIAL_EXECUTE_PREFLIGHT.json'
        [void](Invoke-MaterialCommitmentPreflightGate -PatchManifest $State.Manifest -Stage 'MATERIAL_EXECUTE' -SourceIdentity $localHead -EvidencePath $executeEvidence -AllowBootstrapDefer)
        Assert-NoUntrackedPythonBytecodeArtifacts -GitPath $gitPath -Stage 'MATERIAL_EXECUTE'

        $State.ReachedStage = 'BACKUP'
        $backupRoot = 'D:\Cerebro\Backups'
        [IO.Directory]::CreateDirectory($backupRoot) | Out-Null
        $State.BackupDirectory = Join-Path -Path $backupRoot -ChildPath ('delivery-kernel-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
        [IO.Directory]::CreateDirectory($State.BackupDirectory) | Out-Null
        foreach ($fileEntry in @($State.Manifest.files)) {
            if ([string]$fileEntry.operation -ne 'replace') { continue }
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
            $payload = Join-Path -Path $BundleRoot -ChildPath ([string]$fileEntry.payload_path)
            $target = Join-Path -Path $WorkingSourcePath -ChildPath (([string]$fileEntry.path) -replace '/','\')
            Install-ExactPayloadFile -PayloadPath $payload -TargetPath $target -ExpectedSha256 ([string]$fileEntry.sha256)
            $State.MutationStarted = $true
        }

        $State.ReachedStage = 'INSTALLED_BYTE_VERIFY'
        foreach ($fileEntry in @($State.Manifest.files)) {
            $target = Join-Path -Path $WorkingSourcePath -ChildPath (([string]$fileEntry.path) -replace '/','\')
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
                throw ('CONTRACT_ACTIVATION_CLOSURE_FAILED:{0}' -f [string]$cacResult.activation_gap_count)
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
            schema='cerebro-delivery-kernel-receipt/v1'
            result='PASS'
            kernel=$KernelId
            patch_id=[string]$State.Manifest.patch_id
            attempt_id=$AttemptId
            authoritative_commit=$finalRemote
            working_source_commit=$finalLocal
            source_equality='VERIFIED'
            working_tree='CLEAN'
            cerebro_sync_verified=$true
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
                if ([string]$fileEntry.operation -eq 'replace') {
                    $backup = Join-Path -Path $State.BackupDirectory -ChildPath (([string]$fileEntry.path) -replace '/','\')
                    if (Test-Path -LiteralPath $backup -PathType Leaf) {
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
    throw
}
