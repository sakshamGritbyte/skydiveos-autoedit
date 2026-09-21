#Requires -Version 5.1
<#
.SYNOPSIS
  One-shot installer for the SkydiveOS SD-card ingest box on WINDOWS.

.DESCRIPTION
  The Windows counterpart of deploy/mac/install.sh, for a dropzone box whose ONLY
  job is: card goes in the reader -> clips staged -> uploaded to S3 -> SkydiveOS
  notified. It renders nothing, so it needs no Redis, no Celery worker and no
  GPU (see deploy/windows/README.md).

  Idempotent - safe to re-run. What it does:
    1. Installs Python 3.12 (matching .python-version), FFmpeg, Git via winget
    2. Installs `uv`
    3. `uv sync` - the locked deps into .venv (there is no requirements.txt)
    4. Scaffolds .env from .env.example and DEFUSES the one Windows trap in it
       (a literal POSIX SDCARD_MOUNT_ROOTS - see below)
    5. Verifies the pieces the SD-card path actually uses: ffmpeg >= 6, cv2
       (the QR decoder), and that the drive-letter probing default is live

  It does NOT install the Open GoPro SDK: sdcard mode never touches a radio.
  It does NOT start anything - that is install-service.ps1.

.EXAMPLE
  cd C:\skydiveos-autoedit
  powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1
#>
[CmdletBinding()]
param(
  # Skip the winget stage (already-provisioned box, or no winget available).
  [switch]$SkipPackages
)

$ErrorActionPreference = 'Stop'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Set-Location $RepoRoot

Write-Host "==> SkydiveOS ingest - Windows installer"
Write-Host "    repo: $RepoRoot"

function Test-Cmd([string]$Name) {
  $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Add-ToPathForThisSession([string]$Dir) {
  if ((Test-Path $Dir) -and ($env:Path -notlike "*$Dir*")) { $env:Path = "$Dir;$env:Path" }
}

# uv and winget-shimmed tools land in these; a freshly-installed tool is not on the
# PATH of the shell that installed it until they are added by hand.
Add-ToPathForThisSession (Join-Path $env:USERPROFILE '.local\bin')
Add-ToPathForThisSession (Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps')

# -- 1. System packages -----------------------------------------------------
if (-not $SkipPackages) {
  if (Test-Cmd winget) {
    foreach ($pkg in @(
      # .python-version pins 3.12; `uv sync` downloads a managed CPython if none
      # matches, so this is a convenience (a `python` on PATH for ad-hoc probing),
      # not a requirement.
      @{ Id = 'Python.Python.3.12'; Probe = 'py' },
      @{ Id = 'Gyan.FFmpeg';        Probe = 'ffmpeg' },
      @{ Id = 'Git.Git';            Probe = 'git' }
    )) {
      if (Test-Cmd $pkg.Probe) {
        Write-Host "==> $($pkg.Id) present"
      } else {
        Write-Host "==> Installing $($pkg.Id)"
        winget install --id $pkg.Id --silent --accept-package-agreements --accept-source-agreements
      }
    }
    # winget puts new shims on the MACHINE path; re-read it so this session sees them.
    $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' +
                [Environment]::GetEnvironmentVariable('Path','User')
  } else {
    Write-Warning "winget not found - install FFmpeg 6+ and Git by hand, then re-run with -SkipPackages (uv brings its own Python)"
  }
}

# -- 2. uv ------------------------------------------------------------------
if (Test-Cmd uv) {
  Write-Host "==> uv present"
} else {
  Write-Host "==> Installing uv"
  Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
  Add-ToPathForThisSession (Join-Path $env:USERPROFILE '.local\bin')
}
if (-not (Test-Cmd uv)) { throw "uv is still not on PATH - open a new shell and re-run" }

# -- 3. Python deps ---------------------------------------------------------
# Deps live in pyproject.toml + uv.lock; `uv sync` builds .venv from the lock.
# run.ps1 later launches with `uv run --no-sync` so it reuses THIS .venv.
Write-Host "==> Syncing Python dependencies into .venv (uv sync)"
uv sync
if ($LASTEXITCODE -ne 0) { throw "uv sync failed" }

# -- 4. .env ----------------------------------------------------------------
if (-not (Test-Path .env)) {
  Write-Host "==> Creating .env from .env.example (EDIT IT before starting the service)"
  Copy-Item .env.example .env
} else {
  Write-Host "==> .env already exists - leaving it untouched"
}

# The one Windows trap in .env.example: it ships SDCARD_MOUNT_ROOTS set to the POSIX
# roots. Set, it WINS over the platform default - so the scanner would poll /media,
# /run/media and /Volumes on a machine that has none of them, find no card, and say
# nothing. Unset, ingest/sdcard.py probes the drive letters C:..Z: instead, which is
# what makes "just insert the card" work when the reader is E: today and F: tomorrow.
$envLines = Get-Content .env
$posixRoots = $envLines | Where-Object { $_ -match '^\s*SDCARD_MOUNT_ROOTS\s*=' -and $_ -notmatch '[A-Za-z]:\\' }
if ($posixRoots) {
  Write-Host "==> Commenting out POSIX SDCARD_MOUNT_ROOTS in .env (Windows probes drive letters)"
  $patched = $envLines | ForEach-Object {
    if ($_ -match '^\s*SDCARD_MOUNT_ROOTS\s*=' -and $_ -notmatch '[A-Za-z]:\\') {
      "# (disabled on Windows - the drive letters are probed, not configured)"
      "# $_"
    } else { $_ }
  }
  # UTF-8 WITHOUT a BOM. Windows PowerShell 5.1's `Set-Content -Encoding UTF8` writes
  # one, and python-dotenv reads the file as plain utf-8 (not utf-8-sig), so the BOM
  # becomes part of the first line's key name - a variable that is silently unset.
  # Harmless while line 1 is a comment; invisible and confusing the moment it is not.
  [System.IO.File]::WriteAllLines(
    (Join-Path $RepoRoot '.env'), $patched, (New-Object System.Text.UTF8Encoding $false))
}

# -- 5. Verify what the SD-card path actually needs --------------------------
Write-Host ""
Write-Host "==> Verification"

$ff = (ffmpeg -version 2>$null | Select-Object -First 1)
if (-not $ff) {
  Write-Warning "ffmpeg NOT FOUND - QR marker decoding and captured_at both shell out to it"
} else {
  Write-Host "    ffmpeg: $ff"
  if ($ff -match 'version\s+n?(\d+)\.') {
    if ([int]$Matches[1] -lt 6) { Write-Warning "ffmpeg is older than 6.0" }
  }
}

uv run --no-sync python -c "import cv2, numpy; print('    cv2:', cv2.__version__)"
if ($LASTEXITCODE -ne 0) { Write-Warning "OpenCV failed to import - the QR session marker cannot be decoded" }

uv run --no-sync python -c "from ingest.sdcard import DEFAULT_MOUNT_ROOTS; from api.config import get_settings; r=get_settings().sdcard_mount_roots; print('    mount roots:', ', '.join(r)); print('    (platform default)' if tuple(r)==DEFAULT_MOUNT_ROOTS else '    !! OVERRIDDEN by SDCARD_MOUNT_ROOTS - must name real Windows drives')"

Write-Host @"

============================================================
 Install done. Next steps (deploy\windows\README.md):

 1. Edit .env - the ingest box needs exactly these:
      ENABLE_AUTO_DISCOVERY=1
      CAMERA_SCANNER=sdcard
      SKYDIVEOS_API_BASE=https://northshore-api.parachutemontreal.ultimatedzm.com
      AUTO_EDIT_API_KEY=<North Shore's key - same value on all three hosts>
      MONGO_URL=... / MONGO_DB=skydivingos_northshore
      AWS_S3_BUCKET_NAME=skydiveos-northshore-media  (AWS_REGION=ca-central-1)
      CAMERA_CLOCK_TZ=America/Toronto
      DELETE_AFTER_TRANSFER=true   (once you trust the ledger)

 2. Probe a card with nothing running:
      uv run --no-sync python scripts\check_sdcard.py --decode

 3. Check the DATA prerequisites (this, not the code, is what blocks a dropzone):
      uv run --no-sync python scripts\check_match.py --readiness

 4. Install as a boot service:
      powershell -ExecutionPolicy Bypass -File deploy\windows\install-service.ps1
============================================================
"@
