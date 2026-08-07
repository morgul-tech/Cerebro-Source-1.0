param(
  [Parameter(Mandatory=$true)][string]$RepoPath,
  [Parameter(Mandatory=$true)][string]$ExpectedBranch,
  [Parameter(Mandatory=$true)][string]$ExpectedBase,
  [Parameter(Mandatory=$true)][string]$PackagePath
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

function Sha256-Bytes {
  param([byte[]]$Bytes)
  $sha = [Security.Cryptography.SHA256]::Create()
  try { ([BitConverter]::ToString($sha.ComputeHash($Bytes))).Replace('-','').ToLowerInvariant() }
  finally { $sha.Dispose() }
}

function Invoke-Git {
  param([string[]]$Arguments)
  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = 'git.exe'
  $psi.UseShellExecute = $false
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError = $true
  $psi.CreateNoWindow = $true
  $psi.Arguments = (($Arguments | ForEach-Object {
    if ($_ -match '[\s"]') { '"' + ($_ -replace '"','\"') + '"' } else { $_ }
  }) -join ' ')
  $p = New-Object System.Diagnostics.Process
  $p.StartInfo = $psi
  if (-not $p.Start()) { throw 'GIT_START_FAILED' }
  $stdout = $p.StandardOutput.ReadToEnd()
  $stderr = $p.StandardError.ReadToEnd()
  $p.WaitForExit()
  [pscustomobject]@{ ExitCode=$p.ExitCode; StdOut=$stdout; StdErr=$stderr }
}

function Require-Git {
  param([string[]]$Arguments,[string]$Code)
  $r = Invoke-Git $Arguments
  if ($r.ExitCode -ne 0) { throw ($Code + ':' + $r.StdErr.Trim()) }
  $r.StdOut.Trim()
}

function Decode-Base64Url {
  param([string]$Token)
  $t = ($Token -replace '\s','').Trim()
  if ($t -notmatch '^[A-Za-z0-9_-]+$') { throw 'BASE64URL_INVALID' }
  $b64 = $t.Replace('-','+').Replace('_','/')
  switch ($b64.Length % 4) {
    0 { }
    2 { $b64 += '==' }
    3 { $b64 += '=' }
    default { throw 'BASE64URL_LENGTH_INVALID' }
  }
  [Convert]::FromBase64String($b64)
}

$repo = [IO.Path]::GetFullPath($RepoPath)
$root = Require-Git @('-C',$repo,'rev-parse','--show-toplevel') 'NOT_GIT_REPOSITORY'
if ([IO.Path]::GetFullPath($root).TrimEnd('\') -ne $repo.TrimEnd('\')) { throw 'REPO_ROOT_MISMATCH' }
if ((Require-Git @('-C',$repo,'branch','--show-current') 'BRANCH_READ_FAILED') -ne $ExpectedBranch) { throw 'WRONG_BRANCH' }
if ((Require-Git @('-C',$repo,'rev-parse','HEAD') 'HEAD_READ_FAILED') -ne $ExpectedBase) { throw 'WRONG_BASELINE' }
if (-not [string]::IsNullOrWhiteSpace((Require-Git @('-C',$repo,'status','--porcelain') 'STATUS_FAILED'))) { throw 'DIRTY_WORKTREE' }

$package = Get-Content -LiteralPath $PackagePath -Raw | ConvertFrom-Json
if ($package.schema -ne 'cerebro-level3-package/v1') { throw 'PACKAGE_SCHEMA_INVALID' }
$ops = @($package.operations)
if ($ops.Count -lt 1) { throw 'PACKAGE_EMPTY' }

$backups = @{}
$created = New-Object System.Collections.ArrayList
$changed = New-Object System.Collections.ArrayList

try {
  foreach ($op in $ops) {
    $rel = [string]$op.path
    if ([string]::IsNullOrWhiteSpace($rel) -or [IO.Path]::IsPathRooted($rel) -or $rel -match '(^|[\\/])\.\.([\\/]|$)' -or $rel -match ':') {
      throw 'UNSAFE_PATH'
    }

    $dest = Join-Path $repo ($rel -replace '/', '\')
    $parent = Split-Path -Parent $dest
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) { throw 'PARENT_NOT_FOUND' }

    $exists = Test-Path -LiteralPath $dest -PathType Leaf
    $kind = [string]$op.operation
    if ($kind -notin @('create','replace')) { throw 'OPERATION_INVALID' }
    if ($kind -eq 'create' -and $exists) { throw 'CREATE_TARGET_EXISTS' }
    if ($kind -eq 'replace' -and -not $exists) { throw 'REPLACE_TARGET_MISSING' }

    if ($exists) {
      $before = [IO.File]::ReadAllBytes($dest)
      if ((Sha256-Bytes $before) -ne ([string]$op.expected_before_sha256).ToLowerInvariant()) { throw 'BEFORE_DIGEST_MISMATCH' }
      $backups[$dest] = $before
    }

    $after = Decode-Base64Url ([string]$op.content_base64url)
    if ((Sha256-Bytes $after) -ne ([string]$op.expected_after_sha256).ToLowerInvariant()) { throw 'AFTER_DIGEST_MISMATCH' }

    [IO.File]::WriteAllBytes($dest,$after)
    if (-not $exists) { [void]$created.Add($dest) }
    [void]$changed.Add($rel)

    if ((Sha256-Bytes ([IO.File]::ReadAllBytes($dest))) -ne ([string]$op.expected_after_sha256).ToLowerInvariant()) { throw 'POST_WRITE_DIGEST_MISMATCH' }
  }

  $actual = @(Require-Git @('-C',$repo,'status','--porcelain') 'STATUS_FAILED')
  if ($actual.Count -lt 1) { throw 'NO_WORKTREE_CHANGE' }

  [ordered]@{
    runner='CEREBRO-LEVEL3-RUNNER-001'
    state='PASS'
    validation=$true
    expected_base=$ExpectedBase
    changed_paths=@($changed)
  } | ConvertTo-Json -Compress
}
catch {
  foreach ($dest in $backups.Keys) { [IO.File]::WriteAllBytes($dest,[byte[]]$backups[$dest]) }
  foreach ($dest in $created) { if (Test-Path -LiteralPath $dest) { Remove-Item -LiteralPath $dest -Force } }
  throw
}