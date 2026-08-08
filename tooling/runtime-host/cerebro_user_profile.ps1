Set-StrictMode -Version 2.0

function Write-CerebroProfileJsonAtomic {
    param([Parameter(Mandatory)][string]$Path,[Parameter(Mandatory)][object]$Value)
    $dir=Split-Path -Parent $Path
    [IO.Directory]::CreateDirectory($dir)|Out-Null
    $tmp=Join-Path $dir ('.'+[IO.Path]::GetFileName($Path)+'.tmp-'+[guid]::NewGuid().ToString('N'))
    try {
        [IO.File]::WriteAllText($tmp,(($Value|ConvertTo-Json -Depth 32)+"`n"),[Text.UTF8Encoding]::new($false))
        if(Test-Path -LiteralPath $Path){[IO.File]::Delete($Path)}
        [IO.File]::Move($tmp,$Path)
    }
    finally { if(Test-Path -LiteralPath $tmp){Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue} }
}

function Read-CerebroUserOperatingProfile {
    [CmdletBinding()]
    param([string]$Path='D:\Cerebro\User\user-operating-profile.json')
    if(-not(Test-Path -LiteralPath $Path -PathType Leaf)){
        return [pscustomobject]@{state='PROFILE_ABSENT';path=$Path;profile=$null;error=$null}
    }
    try {
        $profile=[IO.File]::ReadAllText($Path)|ConvertFrom-Json
        foreach($f in @('schema','profile_id','scope','owner','preferences','updated_at')){
            if(-not($profile.PSObject.Properties.Name -contains $f)){throw "PROFILE_FIELD_MISSING:$f"}
        }
        if([string]$profile.owner -ne 'USER'){throw 'PROFILE_OWNER_INVALID'}
        if([string]$profile.scope -notin @('GLOBAL','PROJECT')){throw 'PROFILE_SCOPE_INVALID'}
        return [pscustomobject]@{state='PROFILE_LOADED';path=$Path;profile=$profile;error=$null}
    }
    catch {
        return [pscustomobject]@{state='PROFILE_DEGRADED';path=$Path;profile=$null;error=$_.Exception.Message}
    }
}

function Set-CerebroUserPreference {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$CanonicalDomain,
        [Parameter(Mandatory)][string]$CanonicalKey,
        [Parameter(Mandatory)]$Value,
        [ValidateSet('GLOBAL','PROJECT')][string]$Scope='GLOBAL',
        [ValidateSet('EXPLICIT_USER','REPEATED_CONFIRMED','MIGRATED')][string]$Origin='EXPLICIT_USER',
        [string[]]$EvidenceRefs=@(),
        [string]$Path='D:\Cerebro\User\user-operating-profile.json'
    )
    if($Origin -eq 'REPEATED_CONFIRMED' -and $EvidenceRefs.Count -eq 0){throw 'REPEATED_CONFIRMED_REQUIRES_EVIDENCE'}
    $loaded=Read-CerebroUserOperatingProfile -Path $Path
    if($loaded.state -eq 'PROFILE_DEGRADED'){throw ('PROFILE_DEGRADED_WRITE_BLOCKED:'+ $loaded.error)}
    $now=[DateTimeOffset]::UtcNow.ToString('o')
    if($loaded.state -eq 'PROFILE_ABSENT'){
        $profile=[ordered]@{
            schema='cerebro-user-operating-profile/v0.1'
            profile_id=('UOP-'+[guid]::NewGuid().ToString('N'))
            scope=$Scope
            owner='USER'
            preferences=@()
            updated_at=$now
        }
    } else {
        $profile=$loaded.profile
        if([string]$profile.scope -ne $Scope){throw 'PROFILE_SCOPE_PATH_MISMATCH'}
    }
    $prefs=@($profile.preferences)
    $same=@($prefs|Where-Object{[string]$_.canonical_domain -eq $CanonicalDomain -and [string]$_.canonical_key -eq $CanonicalKey -and [string]$_.status -eq 'ACTIVE'})
    foreach($p in $same){$p.status='SUPERSEDED';$p.updated_at=$now}
    $new=[ordered]@{
        preference_id=('PREF-'+[guid]::NewGuid().ToString('N'))
        canonical_domain=$CanonicalDomain
        canonical_key=$CanonicalKey
        value=$Value
        scope=$Scope
        origin=$Origin
        created_at=$now
        updated_at=$now
        status='ACTIVE'
        supersedes=@($same|ForEach-Object{$_.preference_id})
        evidence_refs=@($EvidenceRefs)
    }
    $profile.preferences=@($prefs)+@([pscustomobject]$new)
    $profile.updated_at=$now
    Write-CerebroProfileJsonAtomic -Path $Path -Value $profile
    Read-CerebroUserOperatingProfile -Path $Path
}

function Revoke-CerebroUserPreference {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$PreferenceId,[string]$Path='D:\Cerebro\User\user-operating-profile.json')
    $loaded=Read-CerebroUserOperatingProfile -Path $Path
    if($loaded.state -ne 'PROFILE_LOADED'){throw ('PROFILE_NOT_LOADED:'+ $loaded.state)}
    $match=@($loaded.profile.preferences|Where-Object{[string]$_.preference_id -eq $PreferenceId})
    if($match.Count -ne 1){throw 'PREFERENCE_ID_NOT_UNIQUE_OR_MISSING'}
    $match[0].status='REVOKED';$match[0].updated_at=[DateTimeOffset]::UtcNow.ToString('o')
    $loaded.profile.updated_at=$match[0].updated_at
    Write-CerebroProfileJsonAtomic -Path $Path -Value $loaded.profile
    Read-CerebroUserOperatingProfile -Path $Path
}

function Resolve-CerebroEffectiveUserConfiguration {
    [CmdletBinding()]
    param(
        [object]$GlobalProfile,
        [object]$ProjectProfile,
        [hashtable]$SessionTaskOverlay=@{},
        [hashtable]$CurrentExplicitInstruction=@{},
        [hashtable]$CerebroDefault=@{}
    )
    $effective=[ordered]@{}
    foreach($k in $CerebroDefault.Keys){$effective[$k]=$CerebroDefault[$k]}
    foreach($profile in @($GlobalProfile,$ProjectProfile)){
        if($null -eq $profile){continue}
        foreach($p in @($profile.preferences)){
            if([string]$p.status -eq 'ACTIVE'){
                $effective[([string]$p.canonical_domain+'.'+[string]$p.canonical_key)]=$p.value
            }
        }
    }
    foreach($k in $SessionTaskOverlay.Keys){$effective[$k]=$SessionTaskOverlay[$k]}
    foreach($k in $CurrentExplicitInstruction.Keys){$effective[$k]=$CurrentExplicitInstruction[$k]}
    [pscustomobject]@{state='EFFECTIVE_USER_CONFIGURATION_RESOLVED';values=$effective}
}