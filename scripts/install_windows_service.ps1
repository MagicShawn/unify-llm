#Requires -Version 5.1
<#
.SYNOPSIS
  Register (or unregister) the UnifyLLM Windows Scheduled Task for auto-start.

.DESCRIPTION
  Prefer native Task Scheduler over NSSM. Registers a per-user task named
  "UnifyLLM" that runs at logon and launches python main.py with a hidden
  window from the project directory.

  Supports -WhatIf / -Confirm via SupportsShouldProcess. Nothing is installed
  unless you actually run the script without -WhatIf.

  Admin rights: NOT required for a current-user AtLogOn task (default).
  Run elevated only if you later adapt this to a system-wide task.

.EXAMPLE
  .\scripts\install_windows_service.ps1 -WhatIf
  # Preview only - no task is registered.

.EXAMPLE
  .\scripts\install_windows_service.ps1
  # Register UnifyLLM at logon on 127.0.0.1:8787 (defaults).

.EXAMPLE
  .\scripts\install_windows_service.ps1 -Port 8788 -HostAddress 0.0.0.0

.EXAMPLE
  .\scripts\install_windows_service.ps1 -Unregister
#>
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [Parameter()]
    [ValidateRange(1, 65535)]
    [int]$Port = 8787,

    # main.py --host. Default is localhost-only.
    [Parameter()]
    [Alias('ListenHost')]
    [string]$HostAddress = '127.0.0.1',

    [Parameter()]
    [switch]$Unregister,

    [Parameter()]
    [ValidatePattern('^[A-Za-z0-9._-]+$')]
    [string]$TaskName = 'UnifyLLM',

    # Project root containing main.py. Defaults to parent of this scripts/ dir.
    [Parameter()]
    [string]$ProjectRoot,

    # Explicit python.exe path. Defaults to .venv\Scripts\python.exe if present, else PATH python.
    [Parameter()]
    [string]$PythonExe
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-ProjectRoot {
    param([string]$Override)
    if ($Override) {
        $resolved = Resolve-Path -LiteralPath $Override -ErrorAction Stop
        return $resolved.ProviderPath
    }
    # scripts/ -> project root
    return (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).ProviderPath
}

function Resolve-PythonExe {
    param(
        [string]$Override,
        [string]$Root
    )
    if ($Override) {
        if (-not (Test-Path -LiteralPath $Override)) {
            throw "PythonExe not found: $Override"
        }
        return (Resolve-Path -LiteralPath $Override).ProviderPath
    }
    $venvPy = Join-Path $Root '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venvPy) {
        return (Resolve-Path -LiteralPath $venvPy).ProviderPath
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }
    throw 'python not found. Install Python 3.10+ or pass -PythonExe.'
}

$root = Get-ProjectRoot -Override $ProjectRoot

if ($Unregister) {
    $existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $existingTask) {
        Write-Host "Scheduled task '$TaskName' is not registered."
        return
    }
    if ($PSCmdlet.ShouldProcess($TaskName, 'Unregister Scheduled Task')) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Unregistered scheduled task '$TaskName'."
    }
    return
}

$mainPy = Join-Path $root 'main.py'
if (-not (Test-Path -LiteralPath $mainPy)) {
    throw "main.py not found under project root: $root"
}

$python = Resolve-PythonExe -Override $PythonExe -Root $root
$startProxy = Join-Path $PSScriptRoot 'start_proxy.ps1'
if (-not (Test-Path -LiteralPath $startProxy)) {
    throw "start_proxy.ps1 not found next to this script: $startProxy"
}

# Prefer the security token identity (more reliable than env vars under some hosts)
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
if ([string]::IsNullOrWhiteSpace($currentUser)) {
    $currentUser = "$env:USERDOMAIN\$env:USERNAME"
}

# Hidden-window launcher: powershell.exe -File start_proxy.ps1 ...
$psExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$arguments = @(
    '-NoProfile'
    '-NonInteractive'
    '-ExecutionPolicy', 'Bypass'
    '-WindowStyle', 'Hidden'
    '-File', "`"$startProxy`""
    '-Port', "$Port"
    '-HostAddress', $HostAddress
    '-PythonExe', "`"$python`""
    '-ProjectRoot', "`"$root`""
    '-Detach'
) -join ' '

$action = New-ScheduledTaskAction -Execute $psExe -Argument $arguments -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Limited

$target = "task '$TaskName' -> $python main.py --host $HostAddress --port $Port (dir: $root)"

Write-Host @"
Plan:
  Task name : $TaskName
  Trigger   : AtLogOn ($currentUser)
  WorkingDir: $root
  Python    : $python
  Command   : main.py --host $HostAddress --port $Port
  Launcher  : hidden PowerShell -> scripts\start_proxy.ps1 -Detach

Admin: not required for this current-user task.
"@

if ($PSCmdlet.ShouldProcess($target, 'Register Scheduled Task')) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "Task '$TaskName' already exists - replacing registration."
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description "Unify LLM local multi-provider gateway (auto-start at logon)" `
        -Force | Out-Null

    Write-Host "Registered scheduled task '$TaskName'."
    Write-Host "Start now:  Start-ScheduledTask -TaskName '$TaskName'"
    Write-Host "Stop:       .\scripts\stop_proxy.ps1"
    Write-Host "Uninstall:  .\scripts\uninstall_windows_service.ps1"
}
