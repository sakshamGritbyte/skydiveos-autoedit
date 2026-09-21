#Requires -Version 5.1
<#
.SYNOPSIS
  The wrapper the Scheduled Task invokes to run the ingest backend on Windows.

.DESCRIPTION
  Counterpart of deploy/mac/run.sh. Kept as a script rather than putting the command
  in the task definition so the working directory and PATH are resolved here, where
  they can be reasoned about: a Scheduled Task starts with a minimal environment and
  in C:\Windows\System32, and RAW_STORAGE_ROOT defaults to the RELATIVE ./raw-storage,
  so a wrong working directory stages footage somewhere nobody will ever look.

  Binds 127.0.0.1 by default. Nothing needs to dial in: the "copying / safe to remove"
  snapshot is PUSHED outbound to SkydiveOS (ingest.discovery.publish_card_status), and
  a dropzone box is behind NAT anyway. Set INGEST_HOST=0.0.0.0 only if the operator
  screen (scripts/watch_cards.py) runs on a different machine on the LAN.
#>
[CmdletBinding()]
param(
  [string]$BindHost = $(if ($env:INGEST_HOST) { $env:INGEST_HOST } else { '127.0.0.1' }),
  [int]$Port        = $(if ($env:INGEST_PORT) { [int]$env:INGEST_PORT } else { 8000 })
)

$ErrorActionPreference = 'Stop'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Set-Location $RepoRoot

# A Scheduled Task inherits the machine PATH only; uv installs per-user, by either of
# two routes (astral installer, or WinGet's Links shim directory).
$env:Path = "$env:USERPROFILE\.local\bin;$env:LOCALAPPDATA\Microsoft\WinGet\Links;" +
            "$env:LOCALAPPDATA\Microsoft\WindowsApps;$env:Path"

$logDir = Join-Path $RepoRoot 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$outLog = Join-Path $logDir 'ingest.out.log'
$errLog = Join-Path $logDir 'ingest.err.log'

# uvicorn logs to STDERR, so ingest.err.log is the interesting one - same split as the
# Mac's launchd plist (StandardOutPath / StandardErrorPath).
#
# Deliberately NOT `uv ... 2>&1 | Tee-Object`: Tee holds its file stream open for the
# life of the pipeline, and a server pipeline never ends, so the log stayed empty while
# the service ran perfectly (observed on the North Shore box). Start-Process hands the
# child real file handles, so lines land as they are written.
#
# Start-Process truncates its redirect targets, so rotate first rather than appending.
foreach ($f in @($outLog, $errLog)) {
  if ((Test-Path $f) -and ((Get-Item $f).Length -gt 0)) { Move-Item $f "$f.1" -Force }
}

"=== ingest start $(Get-Date -Format o) host=$BindHost port=$Port ===" |
  Out-File $outLog -Encoding UTF8

# Fail loudly on the one misconfiguration that looks exactly like "no cards today":
# a scanner set to something other than sdcard, or discovery switched off entirely.
$scannerMatch = Select-String -Path .env -Pattern '^\s*CAMERA_SCANNER\s*=\s*(\S+)' -ErrorAction SilentlyContinue |
                Select-Object -Last 1
$scanner = if ($scannerMatch) { $scannerMatch.Matches[0].Groups[1].Value } else { $null }
if ($scanner -and $scanner -ne 'sdcard') {
  "WARNING: CAMERA_SCANNER=$scanner on a card-reader box - sdcard was expected" |
    Out-File $outLog -Append -Encoding UTF8
}

# Resolve uv by full path. A Scheduled Task's PATH is not the shell the operator
# installed it from, and uv arrives by two different routes: the astral installer
# (%USERPROFILE%\.local\bin) or WinGet (%LOCALAPPDATA%\Microsoft\WinGet\Links).
$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) {
  foreach ($candidate in @(
    (Join-Path $env:USERPROFILE '.local\bin\uv.exe'),
    (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\uv.exe')
  )) {
    if (Test-Path $candidate) { $uv = $candidate; break }
  }
}
if (-not $uv) {
  "FATAL: uv not found. Install it, or add its directory to the machine PATH." |
    Out-File $errLog -Append -Encoding UTF8
  exit 9
}

# Python block-buffers stdout when it is a pipe rather than a console; without this the
# log lags reality by however long it takes to fill a buffer.
$env:PYTHONUNBUFFERED = '1'

# --no-sync reuses the .venv install.ps1 populated, without re-resolving the lockfile
# on every boot (a re-sync on a flaky dropzone uplink is a boot that never finishes).
$proc = Start-Process -FilePath $uv -PassThru -NoNewWindow -WorkingDirectory $RepoRoot `
  -ArgumentList @('run', '--no-sync', 'uvicorn', 'api.app:app',
                  '--host', $BindHost, '--port', "$Port") `
  -RedirectStandardOutput $outLog -RedirectStandardError $errLog

$proc.WaitForExit()
exit $proc.ExitCode
