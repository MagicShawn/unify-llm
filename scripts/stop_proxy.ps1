#Requires -Version 5.1
<#
.SYNOPSIS
  Stop the Unify LLM proxy listening on a TCP port (default 8787).

.DESCRIPTION
  Finds processes listening on -Port and stops them. Optionally also stops
  the "UnifyLLM" scheduled task if it is running.

  Supports -WhatIf / -Confirm. Stopping a process you do not own may require
  elevation.

.EXAMPLE
  .\scripts\stop_proxy.ps1
  # Kill whatever is listening on 8787.

.EXAMPLE
  .\scripts\stop_proxy.ps1 -Port 8788 -WhatIf

.EXAMPLE
  .\scripts\stop_proxy.ps1 -StopTask
#>
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [Parameter()]
    [ValidateRange(1, 65535)]
    [int]$Port = 8787,

    [Parameter()]
    [ValidatePattern('^[A-Za-z0-9._-]+$')]
    [string]$TaskName = 'UnifyLLM',

    # Also Stop-ScheduledTask for $TaskName if it exists.
    [Parameter()]
    [switch]$StopTask
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-ListeningPids {
    param([int]$LocalPort)
    $found = @()
    try {
        $conns = Get-NetTCPConnection -LocalPort $LocalPort -State Listen -ErrorAction Stop
        foreach ($c in @($conns)) {
            if ($null -ne $c.OwningProcess) {
                $found += [int]$c.OwningProcess
            }
        }
    } catch {
        $netstat = & netstat -ano -p tcp 2>$null
        if ($netstat) {
            foreach ($line in $netstat) {
                if ($line -match "^\s*TCP\s+\S+:$LocalPort\s+\S+\s+LISTENING\s+(\d+)\s*$") {
                    $found += [int]$Matches[1]
                }
            }
        }
    }
    return @($found | Sort-Object -Unique)
}

if ($StopTask) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Host "Scheduled task '$TaskName' is not registered."
    } elseif ($task.State -eq 'Running') {
        if ($PSCmdlet.ShouldProcess($TaskName, 'Stop Scheduled Task')) {
            Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
            Write-Host "Stopped scheduled task '$TaskName'."
        }
    } else {
        Write-Host "Scheduled task '$TaskName' is not running (State=$($task.State))."
    }
}

$listening = @(Get-ListeningPids -LocalPort $Port)
if ($listening.Count -eq 0) {
    Write-Host "No process listening on port $Port."
    return
}

foreach ($procId in $listening) {
    $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
    $desc = if ($proc) {
        "PID $procId ($($proc.ProcessName), started $($proc.StartTime -as [datetime]))"
    } else {
        "PID $procId"
    }

    if ($PSCmdlet.ShouldProcess($desc, "Stop listener on port $Port")) {
        try {
            Stop-Process -Id $procId -Force -ErrorAction Stop
            Write-Host "Stopped $desc."
        } catch {
            Write-Warning "Failed to stop ${desc}: $($_.Exception.Message) (try elevated shell)"
        }
    }
}

# Confirm the port is free when not in WhatIf mode
if (-not $WhatIfPreference) {
    Start-Sleep -Milliseconds 300
    $after = @(Get-ListeningPids -LocalPort $Port)
    if ($after.Count -gt 0) {
        Write-Warning "Port $Port still has listeners: $($after -join ', ')"
    } else {
        Write-Host "Port $Port is free."
    }
}
