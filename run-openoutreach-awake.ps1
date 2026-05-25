param(
    [switch]$AllowDisplaySleep
)

$ErrorActionPreference = "Stop"

Set-Location -LiteralPath $PSScriptRoot

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Virtualenv Python not found at $python. Run setup first."
}

Add-Type @"
using System;
using System.Runtime.InteropServices;

public static class Awake {
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern uint SetThreadExecutionState(uint esFlags);
}
"@

[uint32]$ES_CONTINUOUS = 2147483648
[uint32]$ES_SYSTEM_REQUIRED = 0x00000001
[uint32]$ES_DISPLAY_REQUIRED = 0x00000002

[uint32]$flags = $ES_CONTINUOUS -bor $ES_SYSTEM_REQUIRED
if (-not $AllowDisplaySleep) {
    [uint32]$flags = $flags -bor $ES_DISPLAY_REQUIRED
}

Write-Host "Keeping this Windows laptop awake while OpenOutreach runs..."
Write-Host "Press Ctrl+C to stop OpenOutreach and release the awake hold."

$result = [Awake]::SetThreadExecutionState($flags)
if ($result -eq 0) {
    Write-Warning "Windows did not accept the awake request. OpenOutreach will still start."
}

try {
    & $python manage.py
    exit $LASTEXITCODE
}
finally {
    [void][Awake]::SetThreadExecutionState($ES_CONTINUOUS)
    Write-Host "Released awake hold."
}
