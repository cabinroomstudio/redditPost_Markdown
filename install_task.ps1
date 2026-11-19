# Registers the bot to start at logon. Run once, after .env is filled in:
#   powershell -ExecutionPolicy Bypass -File install_task.ps1
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Definition
# Prefer the project venv, so the task survives a reinstall of the global
# Python (a PC reset removed C:\Python314 and the bot silently stopped).
# Create it with:  py -3.14 -m venv venv
$python = Join-Path $root "venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $python) { throw "no venv\Scripts\python.exe and no python on PATH" }
# pythonw.exe (no console window) sits next to it.
$pythonw = Join-Path (Split-Path -Parent $python) "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $python }
Write-Host "Using $pythonw"

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "bot.py" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName "reddit-md" -Action $action -Trigger $trigger `
    -Settings $settings -Description "Reddit thread to markdown capture bot" -Force | Out-Null

Write-Host "Registered scheduled task 'reddit-md'."
Write-Host "Start it now with:  Start-ScheduledTask -TaskName reddit-md"
Write-Host "Stop it with:       Stop-ScheduledTask -TaskName reddit-md"
Write-Host "Remove it with:     Unregister-ScheduledTask -TaskName reddit-md -Confirm:0"
