#Requires -Version 5.1
<#
.SYNOPSIS
  Install / remove the ingest backend as a Windows Scheduled Task, and set the box
  settings that decide whether "just insert the card" actually works unattended.

.DESCRIPTION
  Counterpart of deploy/mac/load-service.sh. A Scheduled Task, not a Windows Service:
  the task runs as a real user account, so %USERPROFILE% resolves and the per-user
  `uv` install is found - the thing that makes an NSSM-wrapped service fail on a box
  that installed fine by hand.

  Principal is -LogonType S4U: the task starts AT BOOT with no one logged in and
  without storing a password. It has no network *credentials*, which costs nothing
  here (S3 and SkydiveOS are authenticated by keys in .env, not by Windows), and
  removable drive letters are assigned machine-wide, so the card is visible from a
  non-interactive session.

  With -HardenBox it also applies the three box settings that silently break an
  unattended reader, all of which need admin:
    * never sleep / never spin down disks on AC - a sleeping box scans nothing
    * AutoPlay off - the popup does nothing harmful, but staff click through it
      and eject the card mid-copy
    * long paths enabled - deep staging paths under a 260-char limit truncate

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File deploy\windows\install-service.ps1 -HardenBox
  powershell -ExecutionPolicy Bypass -File deploy\windows\install-service.ps1 -Action show
  powershell -ExecutionPolicy Bypass -File deploy\windows\install-service.ps1 -Action remove
#>
[CmdletBinding()]
param(
  [ValidateSet('install','remove','show')]
  [string]$Action = 'install',
  # Matches the name the July hand-rolled install used, so -Force REPLACES that task
  # rather than registering a second one beside it. Two ingest tasks means two uvicorns
  # racing for port 8000 on the next boot, and the loser dies with a bind error that
  # says nothing about there being a twin.
  [string]$TaskName = 'SkydiveOS-Ingest',
  # The account the task runs as. Defaults to whoever is installing it.
  [string]$User = "$env:USERDOMAIN\$env:USERNAME",
  [switch]$HardenBox
)

$ErrorActionPreference = 'Stop'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$RunScript = Join-Path $RepoRoot 'deploy\windows\run.ps1'

function Test-Admin {
  $id = [Security.Principal.WindowsIdentity]::GetCurrent()
  (New-Object Security.Principal.WindowsPrincipal $id).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
}

switch ($Action) {

  'show' {
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $t) { Write-Host "Task '$TaskName' is not installed."; break }
    $t | Format-List TaskName, State
    Get-ScheduledTaskInfo -TaskName $TaskName |
      Format-List LastRunTime, LastTaskResult, NextRunTime
    Write-Host "Log: $RepoRoot\logs\ingest.log"
    break
  }

  'remove' {
    if (-not (Test-Admin)) { throw "Run this from an elevated PowerShell." }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "==> Removed task '$TaskName'"
    break
  }

  'install' {
    if (-not (Test-Admin)) { throw "Run this from an elevated PowerShell (Right-click > Run as administrator)." }
    if (-not (Test-Path $RunScript)) { throw "Missing $RunScript" }

    # Refuse to add a twin. An older install under any other name keeps its own trigger,
    # so both would start at boot and one would lose the port - intermittently, since
    # which one wins is a race.
    $others = Get-ScheduledTask |
      Where-Object { $_.TaskName -match 'ingest|skydive|gopro' -and $_.TaskName -ne $TaskName }
    if ($others) {
      $names = ($others | ForEach-Object { "'" + $_.TaskName + "'" }) -join ', '
      throw @"
Another ingest task already exists: $names
Remove it before installing this one, or both will start at boot and race for port 8000:
  Unregister-ScheduledTask -TaskName '<name>' -Confirm:`$false
"@
    }

    # NOT $action: PowerShell variable names are case-insensitive, so that would
    # reassign the $Action PARAMETER, whose [ValidateSet] then rejects the
    # MSFT_TaskExecAction object on assignment.
    $taskAction = New-ScheduledTaskAction `
      -Execute 'powershell.exe' `
      -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$RunScript`"" `
      -WorkingDirectory $RepoRoot

    # AtStartup is the one that matters (unattended box, power cut, reboot); AtLogOn
    # covers the case where someone signs in before the boot task has settled.
    $triggers = @(
      (New-ScheduledTaskTrigger -AtStartup),
      (New-ScheduledTaskTrigger -AtLogOn -User $User)
    )

    $principal = New-ScheduledTaskPrincipal -UserId $User -LogonType S4U -RunLevel Highest

    # ExecutionTimeLimit 0 = never kill it; this process is meant to run for months.
    # Restart on failure so a transient crash (unplugged reader, wedged mount) recovers
    # without anyone at the dropzone knowing there is a service to restart.
    $settings = New-ScheduledTaskSettingsSet `
      -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
      -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
      -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
      -MultipleInstances IgnoreNew

    Register-ScheduledTask -TaskName $TaskName -Action $taskAction -Trigger $triggers `
      -Principal $principal -Settings $settings -Force | Out-Null

    Write-Host "==> Registered '$TaskName' (runs as $User, starts at boot)"
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "==> Started"

    if ($HardenBox) {
      Write-Host "==> Box settings"
      powercfg /change standby-timeout-ac 0
      powercfg /change hibernate-timeout-ac 0
      powercfg /change disk-timeout-ac 0
      powercfg /change monitor-timeout-ac 20
      Write-Host "    sleep/hibernate/disk-spindown disabled on AC"

      New-Item -Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\Explorer' -Force | Out-Null
      # 0xFF = disable AutoPlay/AutoRun on every drive type.
      Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\Explorer' `
        -Name NoDriveTypeAutoRun -Value 0xFF -Type DWord
      Write-Host "    AutoPlay disabled"

      Set-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' `
        -Name LongPathsEnabled -Value 1 -Type DWord
      Write-Host "    long paths enabled (takes effect after a reboot)"
    }

    Write-Host ""
    Write-Host "Useful:"
    Write-Host "  Get-ScheduledTaskInfo -TaskName '$TaskName'"
    Write-Host "  Get-Content $RepoRoot\logs\ingest.log -Tail 40 -Wait"
    Write-Host "  uv run --no-sync python scripts\watch_cards.py     # operator display"
    Write-Host "  powershell -File deploy\windows\install-service.ps1 -Action remove"
    break
  }
}
