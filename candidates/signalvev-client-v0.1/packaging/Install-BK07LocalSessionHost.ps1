param(
    [Parameter(Mandatory = $true)][string]$ReviewedSourceScript,
    [Parameter(Mandatory = $true)][string]$PythonExe,
    [string]$Root = 'D:\Cerebro\Run\Signalvev\BK07\normal-use',
    [switch]$StartNow
)

$ErrorActionPreference = 'Stop'
$taskName = 'Cerebro-BK07-LocalSessionHost'
$source = (Resolve-Path -LiteralPath $ReviewedSourceScript).Path
$expectedHash = '44515BD8C69A733EAF906C006ED536B0C2BDE155F95896ECEA13AF9326254F34'
if ((Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash -ne $expectedHash) {
    throw 'SOURCE_SCRIPT_NOT_REVIEWED_SHA256'
}
$python = (Resolve-Path -LiteralPath $PythonExe).Path
$rootPath = [IO.Path]::GetFullPath($Root)
$profile = Join-Path $rootPath 'profile.json'
$installedDir = Join-Path $rootPath 'host'
$installed = Join-Path $installedDir 'local_runtime_session_host.py'
$stateDir = Join-Path $rootPath 'session-host-state'

if (-not (Test-Path -LiteralPath $profile -PathType Leaf)) { throw 'BK07_PROFILE_REQUIRED' }
$profileValue = Get-Content -LiteralPath $profile -Raw | ConvertFrom-Json
if ($profileValue.schema -ne 'cerebro-bk07-normal-use-profile/v1' -or $profileValue.enabled -ne $false) {
    throw 'BK07_PROFILE_MUST_REMAIN_OFF'
}
if ((Get-Command Get-ScheduledTask -ErrorAction SilentlyContinue) -eq $null) {
    throw 'TASK_SCHEDULER_REQUIRED'
}
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    throw 'BK07_TASK_ALREADY_EXISTS_REVIEW_BEFORE_REPLACE'
}

New-Item -ItemType Directory -Path $installedDir,$stateDir -Force | Out-Null
if (Test-Path -LiteralPath $installed -PathType Leaf) {
    if ((Get-FileHash -LiteralPath $installed -Algorithm SHA256).Hash -ne $expectedHash) {
        throw 'INSTALLED_SCRIPT_DIFFERS_REVIEW_BEFORE_REPLACE'
    }
} else {
    Copy-Item -LiteralPath $source -Destination $installed
}
if ((Get-FileHash -LiteralPath $installed -Algorithm SHA256).Hash -ne $expectedHash) {
    throw 'INSTALLED_SCRIPT_HASH_MISMATCH'
}

$arguments = ('-B "{0}" --profile "{1}" --state-dir "{2}"' -f $installed,$profile,$stateDir)
$action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $installedDir
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings | Out-Null

if ($StartNow) { Start-ScheduledTask -TaskName $taskName }
[pscustomobject]@{
    task = $taskName
    installed_sha256 = $expectedHash
    profile_enabled = $false
    started_now = [bool]$StartNow
    identity_state = 'LOCAL_ONLY_NOT_CONTEXT_BOUND'
}
