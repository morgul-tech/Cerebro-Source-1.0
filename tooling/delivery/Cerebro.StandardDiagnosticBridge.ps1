Set-StrictMode -Version 2.0

function Get-CerebroDiagnosticPython {
    foreach($name in @('python.exe','python','py.exe','py')){
        $command=Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
        if($null -ne $command){ return [string]$command.Source }
    }
    throw 'DIAGNOSTIC_PYTHON_NOT_FOUND'
}

function Invoke-CerebroDiagnosticNative {
    param([Parameter(Mandatory)][string]$Executable,[Parameter(Mandatory)][string[]]$ArgumentList)
    $stdoutFile=[IO.Path]::GetTempFileName()
    $stderrFile=[IO.Path]::GetTempFileName()
    $previous=$ErrorActionPreference
    $exitCode=$null
    try {
        $ErrorActionPreference='Continue'
        & $Executable @ArgumentList 1> $stdoutFile 2> $stderrFile
        $exitCode=$LASTEXITCODE
    }
    finally { $ErrorActionPreference=$previous }
    try {
        $stdout=[IO.File]::ReadAllText($stdoutFile)
        $stderr=[IO.File]::ReadAllText($stderrFile)
    }
    finally {
        Remove-Item -LiteralPath $stdoutFile,$stderrFile -Force -ErrorAction SilentlyContinue
    }
    [pscustomobject]@{ExitCode=$exitCode;Stdout=$stdout;Stderr=$stderr}
}

function Write-CerebroDiagnosticFallback {
    param($Seed,[Parameter(Mandatory)][string]$StableHandoffPath,[string]$DiagnosticError='')
    $fallback=[ordered]@{
        schema='cerebro-diagnostic-fallback-seed/v1'
        state='PATCH_FAIL'
        authority='EVIDENCE_ONLY'
        diagnostic_degraded=$true
        diagnostic_error=$DiagnosticError
        attempt_id=[string]$Seed.attempt_id
        patch_id=[string]$Seed.patch_id
        expected_base_commit=[string]$Seed.expected_base_commit
        phase=[string]$Seed.phase
        reached_stage=[string]$Seed.reached_stage
        failure_family=[string]$Seed.failure_family
        error=[string]$Seed.error
        child_exit_code=$Seed.child_exit_code
        source_mutation_assessment=[string]$Seed.source_mutation_assessment
        created_at_utc=[DateTime]::UtcNow.ToString('o')
    }
    [IO.File]::WriteAllText(
        $StableHandoffPath,
        (($fallback | ConvertTo-Json -Depth 8)+"`r`n"),
        [Text.UTF8Encoding]::new($false)
    )
    [pscustomobject]@{
        State='FALLBACK'
        CapsuleId=''
        CanonicalPath=$StableHandoffPath
        TransportPath=$StableHandoffPath
        HandoffPath=$StableHandoffPath
        Degraded=$true
    }
}

function Invoke-CerebroStandardDiagnosticBridge {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateSet('Capture','Resolve')][string]$Mode,
        [Parameter(Mandatory)][string]$WorkingSourcePath,
        [Parameter(Mandatory)][string]$StableHandoffPath,
        $Seed=$null,
        [string]$PatchId='',
        [string]$ResultingCommit=''
    )

    $diagnosticTool=Join-Path $WorkingSourcePath 'tooling\host\diagnostic_capsule.py'

    if($Mode -eq 'Resolve'){
        try {
            if(-not(Test-Path -LiteralPath $diagnosticTool -PathType Leaf)){ throw 'CANONICAL_DIAGNOSTIC_TOOL_NOT_FOUND' }
            if([string]::IsNullOrWhiteSpace($PatchId)){ throw 'DIAGNOSTIC_RESOLUTION_PATCH_ID_MISSING' }
            if([string]::IsNullOrWhiteSpace($ResultingCommit)){ throw 'DIAGNOSTIC_RESOLUTION_COMMIT_MISSING' }

            $python=Get-CerebroDiagnosticPython
            $native=Invoke-CerebroDiagnosticNative -Executable $python -ArgumentList @(
                $diagnosticTool,'resolve',
                '--patch-id',$PatchId,
                '--resulting-commit',$ResultingCommit,
                '--resolution-summary','STANDARD delivery patch succeeded'
            )
            if($native.ExitCode -ne 0){
                throw ('CANONICAL_DIAGNOSTIC_RESOLUTION_FAILED:{0}:{1}' -f $native.ExitCode,$native.Stderr)
            }
            if(Test-Path -LiteralPath $StableHandoffPath -PathType Leaf){
                Remove-Item -LiteralPath $StableHandoffPath -Force -ErrorAction SilentlyContinue
            }
            return [pscustomobject]@{State='RESOLVED';Degraded=$false;Output=[string]$native.Stdout}
        }
        catch {
            return [pscustomobject]@{State='RESOLUTION_DEGRADED';Degraded=$true;Error=$_.Exception.Message}
        }
    }

    if($null -eq $Seed){ throw 'DIAGNOSTIC_CAPTURE_SEED_MISSING' }

    $temporaryRoot=$null
    try {
        if(-not(Test-Path -LiteralPath $diagnosticTool -PathType Leaf)){ throw 'CANONICAL_DIAGNOSTIC_TOOL_NOT_FOUND' }
        $python=Get-CerebroDiagnosticPython
        $temporaryRoot=Join-Path ([IO.Path]::GetTempPath()) ('CerebroDiagnosticBridge-'+[guid]::NewGuid().ToString('N'))
        [IO.Directory]::CreateDirectory($temporaryRoot) | Out-Null

        $transcriptPath=Join-Path $temporaryRoot 'runner-transcript.txt'
        $transcript=@(
            '=== CHILD STDOUT ==='
            [string]$Seed.child_stdout
            ''
            '=== CHILD STDERR ==='
            [string]$Seed.child_stderr
        ) -join "`r`n"
        [IO.File]::WriteAllText($transcriptPath,$transcript,[Text.UTF8Encoding]::new($false))

        $event=[ordered]@{
            subject=[ordered]@{
                patch_id=[string]$Seed.patch_id
                revision='STANDARD'
                baseline_commit=[string]$Seed.expected_base_commit
                attempt_id=[string]$Seed.attempt_id
            }
            failure=[ordered]@{
                stage=[string]$Seed.reached_stage
                detection=[string]$Seed.error
                exception_type='STANDARD_DELIVERY_FAILURE'
                message=[string]$Seed.error
                exit_code=$Seed.child_exit_code
                probe_status='COMPLETE'
                subject_result='FAIL'
                raw_error_bounded=(([string]$Seed.child_stdout)+"`r`n"+([string]$Seed.child_stderr))
                failure_family=[string]$Seed.failure_family
            }
            execution=[ordered]@{
                phase=[string]$Seed.phase
                attempt_id=[string]$Seed.attempt_id
                source_mutation_assessment=[string]$Seed.source_mutation_assessment
                bundle_path=[string]$Seed.bundle_path
                bundle_sha256=[string]$Seed.bundle_sha256
                kernel_sha256=[string]$Seed.kernel_sha256
            }
            artifacts=[ordered]@{transcript_path=$transcriptPath}
            evidence_refs=@(
                ('attempt:{0}' -f [string]$Seed.attempt_id),
                ('bundle-sha256:{0}' -f [string]$Seed.bundle_sha256),
                ('kernel-sha256:{0}' -f [string]$Seed.kernel_sha256)
            )
        }

        $eventPath=Join-Path $temporaryRoot 'event.json'
        [IO.File]::WriteAllText(
            $eventPath,
            (($event | ConvertTo-Json -Depth 12)+"`r`n"),
            [Text.UTF8Encoding]::new($false)
        )

        $native=Invoke-CerebroDiagnosticNative -Executable $python -ArgumentList @(
            $diagnosticTool,'capture','--event-file',$eventPath,'--repo',$WorkingSourcePath
        )
        if($native.ExitCode -ne 0){
            throw ('CANONICAL_DIAGNOSTIC_CAPTURE_FAILED:{0}:{1}' -f $native.ExitCode,$native.Stderr)
        }

        $match=[regex]::Match(
            [string]$native.Stdout,
            '(?m)^CEREBRO_DIAGNOSTIC_CAPTURE STATE=UNRESOLVED CAPSULE=(\S+) PATH=(.+)$'
        )
        if(-not $match.Success){ throw 'CANONICAL_DIAGNOSTIC_CAPTURE_OUTPUT_UNPARSEABLE' }

        $capsuleId=$match.Groups[1].Value.Trim()
        $fullPath=$match.Groups[2].Value.Trim()
        $transportPath=Join-Path (Split-Path -Parent $fullPath) 'transport.json'

        if(-not(Test-Path -LiteralPath $fullPath -PathType Leaf)){ throw 'CANONICAL_DIAGNOSTIC_FULL_CAPSULE_MISSING' }
        if(-not(Test-Path -LiteralPath $transportPath -PathType Leaf)){ throw 'CANONICAL_DIAGNOSTIC_TRANSPORT_MISSING' }

        Copy-Item -LiteralPath $transportPath -Destination $StableHandoffPath -Force

        return [pscustomobject]@{
            State='CAPTURED'
            CapsuleId=$capsuleId
            CanonicalPath=$fullPath
            TransportPath=$transportPath
            HandoffPath=$StableHandoffPath
            Degraded=$false
        }
    }
    catch {
        return Write-CerebroDiagnosticFallback -Seed $Seed -StableHandoffPath $StableHandoffPath -DiagnosticError $_.Exception.Message
    }
    finally {
        if($null -ne $temporaryRoot -and (Test-Path -LiteralPath $temporaryRoot)){
            Remove-Item -LiteralPath $temporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}
