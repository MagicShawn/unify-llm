#Requires -Version 5.1
<#
.SYNOPSIS
  Unregister the UnifyLLM Windows Scheduled Task and optionally stop the proxy.

.DESCRIPTION
  Removes the task registered by install_windows_service.ps1 (default name
  "UnifyLLM"). Also stops a running proxy on -Port if present.

  Supports -WhatIf / -Confirm. Admin rights are NOT required for a
  current-user task.

.EXAMPLE
  .\scripts\uninstall_windows_service.ps1 -WhatIf

.EXAMPLE
  .\scripts\uninstall_windows_service.ps1

.EXAMPLE
  .\scripts\uninstall_windows_service.ps1 -TaskName UnifyLLM -Port 8787 -KeepProcess
#>
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [Parameter()]
    [ValidatePattern('^[A-Za-z0-9._-]+$')]
    [string]$TaskName = 'UnifyLLM',

    [Parameter()]
    [ValidateRange(1, 65535)]
    [int]$Port = 8787,

    # Leave any process listening on -Port running.
    [Parameter()]
    [switch]$KeepProcess
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-ListeningPids {
    param([int]$LocalPort)
    $pids = @()
    try {
        $conns = Get-NetTCPConnection -LocalPort $LocalPort -State Listen -ErrorAction Stop
        foreach ($c in @($conns)) {
            if ($null -ne $c.OwningProcess) {
                $pids += [int]$c.OwningProcess
            }
        }
    } catch {
        # Fallback for older systems / permission edge cases
        $netstat = & netstat -ano -p tcp 2>$null
        if ($netstat) {
            foreach ($line in $netstat) {
                if ($line -match "^\s*TCP\s+\S+:$LocalPort\s+\S+\s+LISTENING\s+(\d+)\s*$") {
                    $pids += [int]$Matches[1]
                }
            }
        }
    }
    return @($pids | Sort-Object -Unique)
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $existing) {
    Write-Host "Scheduled task '$TaskName' is not registered (nothing to uninstall)."
} elseif ($PSCmdlet.ShouldProcess($TaskName, 'Unregister Scheduled Task')) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Unregistered scheduled task '$TaskName'."
}

if ($KeepProcess) {
    Write-Host "KeepProcess set - leaving anything on port $Port alone."
    return
}

$listening = @(Get-ListeningPids -LocalPort $Port)
if ($listening.Count -eq 0) {
    Write-Host "No process listening on port $Port."
    return
}

foreach ($procId in $listening) {
    $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
    $desc = if ($proc) { "PID $procId ($($proc.ProcessName))" } else { "PID $procId" }
    if ($PSCmdlet.ShouldProcess($desc, "Stop process on port $Port")) {
        try {
            Stop-Process -Id $procId -Force -ErrorAction Stop
            Write-Host "Stopped $desc listening on port $Port."
        } catch {
            Write-Warning "Failed to stop ${desc}: $($_.Exception.Message)"
        }
    }
}
