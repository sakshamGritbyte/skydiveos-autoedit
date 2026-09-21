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

# A Scheduled Task inherits the machine PATH only; uv installs per-user.
$env:Path = "$env:USERPROFILE\.local\bin;$env:LOCALAPPDATA\Microsoft\WindowsApps;$env:Path"

$logDir = Join-Path $RepoRoot 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir 'ingest.log'

# Crude rotation: an unattended box runs for months and this log is the only record
# of what every card did.
if ((Test-Path $log) -and ((Get-Item $log).Length -gt 50MB)) {
  Move-Item $log "$log.1" -Force
}

"=== ingest start $(Get-Date -Format o) host=$BindHost port=$Port ===" | Out-File $log -Append -Encoding UTF8

# Fail loudly on the one misconfiguration that looks exactly like "no cards today":
# a scanner set to something other than sdcard, or discovery switched off entirely.
$scannerMatch = Select-String -Path .env -Pattern '^\s*CAMERA_SCANNER\s*=\s*(\S+)' -ErrorAction SilentlyContinue |
                Select-Object -Last 1
$scanner = if ($scannerMatch) { $scannerMatch.Matches[0].Groups[1].Value } else { $null }
if ($scanner -and $scanner -ne 'sdcard') {
  $msg = "WARNING: CAMERA_SCANNER=$scanner on a card-reader box - sdcard was expected"
  Write-Warning $msg; $msg | Out-File $log -Append -Encoding UTF8
}

# --no-sync reuses the .venv install.ps1 populated, without re-resolving the lockfile
# on every boot (a re-sync on a flaky dropzone uplink is a boot that never finishes).
uv run --no-sync uvicorn api.app:app --host $BindHost --port $Port 2>&1 |
  Tee-Object -FilePath $log -Append

exit $LASTEXITCODE
