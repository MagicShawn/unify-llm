#Requires -Version 5.1
<#
.SYNOPSIS
  Start the Unify LLM proxy (default port 8787).

.DESCRIPTION
  Launches python main.py from the project root. With -Detach (used by the
  Scheduled Task installer), starts a hidden background process and returns.
  Without -Detach, runs in the current console (Ctrl+C to stop).

  Supports -WhatIf / -Confirm for the detach path.

.EXAMPLE
  .\scripts\start_proxy.ps1
  # Foreground: console stays attached (Ctrl+C to quit).

.EXAMPLE
  .\scripts\start_proxy.ps1 -Detach
  # Background hidden process.

.EXAMPLE
  .\scripts\start_proxy.ps1 -Port 8788 -HostAddress 127.0.0.1 -Detach -WhatIf
#>
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [Parameter()]
    [ValidateRange(1, 65535)]
    [int]$Port = 8787,

    # main.py --host. Do not name this parameter Host (PowerShell reserved).
    [Parameter()]
    [Alias('ListenHost', 'ServerHost')]
    [string]$HostAddress = '127.0.0.1',

    [Parameter()]
    [string]$ProjectRoot,

    [Parameter()]
    [string]$PythonExe,

    [Parameter()]
    [string]$ConfigPath = 'config.yaml',

    # Start in the background with a hidden window (Scheduled Task style).
    [Parameter()]
    [switch]$Detach,

    # Force-stop any existing listener on -Port before starting.
    [Parameter()]
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-ProjectRoot {
    param([string]$Override)
    if ($Override) {
        return (Resolve-Path -LiteralPath $Override -ErrorAction Stop).ProviderPath
    }
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

$root = Get-ProjectRoot -Override $ProjectRoot
$mainPy = Join-Path $root 'main.py'
if (-not (Test-Path -LiteralPath $mainPy)) {
    throw "main.py not found under project root: $root"
}

$python = Resolve-PythonExe -Override $PythonExe -Root $root
$configFull = if ([System.IO.Path]::IsPathRooted($ConfigPath)) {
    $ConfigPath
} else {
    Join-Path $root $ConfigPath
}
if (-not (Test-Path -LiteralPath $configFull)) {
    Write-Warning "Config not found: $configFull (copy config.example.yaml to config.yaml if this is a fresh checkout)."
}

$existing = @(Get-ListeningPids -LocalPort $Port)
if ($existing.Count -gt 0) {
    $desc = ($existing | ForEach-Object {
        $p = Get-Process -Id $_ -ErrorAction SilentlyContinue
        if ($p) { "PID $_ ($($p.ProcessName))" } else { "PID $_" }
    }) -join ', '

    if (-not $Force) {
        throw "Port $Port already in use ($desc). Stop it first (.\\scripts\\stop_proxy.ps1) or pass -Force."
    }

    foreach ($procId in $existing) {
        if ($PSCmdlet.ShouldProcess("PID $procId", "Stop listener on port $Port (-Force)")) {
            Stop-Process -Id $procId -Force -ErrorAction Stop
            Write-Host "Stopped PID $procId on port $Port."
        }
    }
}

$pyArgs = @(
    $mainPy
    '--config', $configFull
    '--host', $HostAddress
    '--port', "$Port"
)

$target = "$python $($pyArgs -join ' ') [cwd=$root]"

if ($Detach) {
    if (-not $PSCmdlet.ShouldProcess($target, 'Start detached proxy')) {
        return
    }
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $python
    $psi.Arguments = ($pyArgs | ForEach-Object {
        if ($_ -match '\s') { '"{0}"' -f $_ } else { $_ }
    }) -join ' '
    $psi.WorkingDirectory = $root
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $false
    $psi.RedirectStandardError = $false

    $proc = [System.Diagnostics.Process]::Start($psi)
    Write-Host "Started Unify LLM (detached) PID $($proc.Id) on ${HostAddress}:${Port}"
    Write-Host "  Health: http://${HostAddress}:${Port}/healthz"
    Write-Host "  Stop:   .\\scripts\\stop_proxy.ps1 -Port $Port"
} else {
    if (-not $PSCmdlet.ShouldProcess($target, 'Start foreground proxy')) {
        return
    }
    Write-Host "Starting Unify LLM in foreground on ${HostAddress}:${Port} (Ctrl+C to stop)..."
    Push-Location $root
    try {
        & $python @pyArgs
    } finally {
        Pop-Location
    }
}
