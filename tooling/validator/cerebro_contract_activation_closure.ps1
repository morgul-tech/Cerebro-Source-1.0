Set-StrictMode -Version 2.0

function New-CacFinding {
    param([string]$Binding,[string]$Stage,[string]$Code,[string]$Path,[string]$Message)
    [pscustomobject]@{binding=$Binding;stage=$Stage;code=$Code;path=$Path;message=$Message}
}

function Get-CacOptionalValues {
    param($Object,[string]$PropertyName)

    if($null -eq $Object){return @()}
    if($Object.PSObject.Properties.Name -notcontains $PropertyName){return @()}

    $value=$Object.$PropertyName
    if($null -eq $value){return @()}

    return @($value)
}

function Test-CacFileTokens {
    param([string]$Root,[string]$RelativePath,[object[]]$Tokens,[string]$Binding,[string]$Stage)

    $findings=@()
    if([string]::IsNullOrWhiteSpace($RelativePath)){return $findings}

    $full=Join-Path $Root ($RelativePath -replace '/','\')
    if(-not(Test-Path -LiteralPath $full -PathType Leaf)){
        return @(New-CacFinding -Binding $Binding -Stage $Stage -Code 'CAC_FILE_MISSING' -Path $RelativePath -Message 'Required activation-chain file is missing.')
    }

    $text=[IO.File]::ReadAllText($full)
    foreach($token in @($Tokens)){
        if(-not [string]::IsNullOrWhiteSpace([string]$token)){
            if(-not $text.Contains([string]$token)){
                $findings += New-CacFinding -Binding $Binding -Stage $Stage -Code 'CAC_TOKEN_MISSING' -Path $RelativePath -Message ('Missing activation token: '+[string]$token)
            }
        }
    }
    return $findings
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
    $findings=@()
    $states=@()

    foreach($binding in @($registry.bindings)){
        $id=[string]$binding.id
        $classification=[string]$binding.classification

        if($classification -in @('POLICY_ONLY','DORMANT')){
            $states += [pscustomobject]@{binding=$id;classification=$classification;state=$classification}
            continue
        }

        $local=@()
        $local += Test-CacFileTokens -Root $rootPath -RelativePath ([string]$binding.declaration) -Tokens (Get-CacOptionalValues -Object $binding -PropertyName 'declaration_tokens') -Binding $id -Stage 'DECLARED'
        $local += Test-CacFileTokens -Root $rootPath -RelativePath ([string]$binding.registration) -Tokens (Get-CacOptionalValues -Object $binding -PropertyName 'registration_token') -Binding $id -Stage 'REGISTERED'
        $local += Test-CacFileTokens -Root $rootPath -RelativePath ([string]$binding.implementation) -Tokens (Get-CacOptionalValues -Object $binding -PropertyName 'implementation_tokens') -Binding $id -Stage 'IMPLEMENTED'

        if($binding.PSObject.Properties.Name -contains 'wiring'){
            $local += Test-CacFileTokens -Root $rootPath -RelativePath ([string]$binding.wiring) -Tokens (Get-CacOptionalValues -Object $binding -PropertyName 'wiring_tokens') -Binding $id -Stage 'WIRED'
        }

        $local += Test-CacFileTokens -Root $rootPath -RelativePath ([string]$binding.validation) -Tokens (Get-CacOptionalValues -Object $binding -PropertyName 'validation_tokens') -Binding $id -Stage 'VALIDATED'

        if(@($local).Count -eq 0){
            $states += [pscustomobject]@{binding=$id;classification=$classification;state='OPERATIONAL'}
        }
        else {
            $findings += @($local)
            $states += [pscustomobject]@{binding=$id;classification=$classification;state='ACTIVATION_GAP'}
        }
    }

    $result=if(@($findings).Count -eq 0){'PASS'}else{'FAIL'}
    $object=[pscustomobject]@{
        schema='cerebro-contract-activation-closure-result/v1'
        result=$result
        binding_count=@($registry.bindings).Count
        activation_gap_count=@($findings).Count
        states=@($states)
        findings=@($findings)
    }

    if($PassThru){return $object}
    $object | ConvertTo-Json -Depth 12

    if($result -ne 'PASS'){
        throw ('CONTRACT_ACTIVATION_CLOSURE_FAILED:{0}' -f @($findings).Count)
    }
}

function Invoke-CerebroContractActivationAudit {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Root,
        [string]$StandardsManifest='standards/standards.yaml',
        [string]$OutputPath='D:\Cerebro\Run\audits\CEREBRO_CONTRACT_ACTIVATION_AUDIT.json'
    )

    $rootPath=[IO.Path]::GetFullPath($Root)
    $manifestFull=Join-Path $rootPath ($StandardsManifest -replace '/','\')
    if(-not(Test-Path -LiteralPath $manifestFull -PathType Leaf)){
        throw 'CAC_STANDARDS_MANIFEST_NOT_FOUND'
    }

    $manifestText=[IO.File]::ReadAllText($manifestFull)
    $paths=@(
        [regex]::Matches($manifestText,'(?m)^\s*path:\s*(standards/[^\r\n#]+)') |
        ForEach-Object {$_.Groups[1].Value.Trim()}
    ) | Select-Object -Unique

    $candidates=@()

    foreach($relative in $paths){
        $full=Join-Path $rootPath ($relative -replace '/','\')
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
            $signalFull=Join-Path $rootPath ($signal -replace '/','\')
            if(-not(Test-Path -LiteralPath $signalFull -PathType Leaf)){
                $missing += $signal
            }
        }

        $candidates += [pscustomobject]@{
            declaration=$relative
            referenced_implementations=$signals
            missing_references=$missing
            disposition=if($missing.Count -gt 0){'SEMANTIC_REVIEW_REQUIRED'}else{'REFERENCE_INTEGRITY_PASS'}
        }
    }

    $output=[pscustomobject]@{
        schema='cerebro-contract-activation-audit/v1'
        generated_at_utc=[DateTime]::UtcNow.ToString('o')
        source_root=$rootPath
        candidate_count=$candidates.Count
        candidates=$candidates
        note='Discovery audit is non-blocking. Strict closure is limited to explicit bindings.'
    }

    [IO.Directory]::CreateDirectory((Split-Path -Parent $OutputPath)) | Out-Null
    [IO.File]::WriteAllText(
        $OutputPath,
        (($output | ConvertTo-Json -Depth 12)+"`r`n"),
        [Text.UTF8Encoding]::new($false)
    )

    return $output
}
