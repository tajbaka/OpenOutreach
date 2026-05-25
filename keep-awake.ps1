param(
    [switch]$AllowDisplaySleep
)

$ErrorActionPreference = "Stop"

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

$mode = if ($AllowDisplaySleep) { "system awake; display may sleep" } else { "system and display awake" }
Write-Host "Keeping Windows awake ($mode). Close this window or press Ctrl+C to stop."

$result = [Awake]::SetThreadExecutionState($flags)
if ($result -eq 0) {
    Write-Warning "Windows did not accept the awake request."
}

try {
    while ($true) {
        Start-Sleep -Seconds 60
        [void][Awake]::SetThreadExecutionState($flags)
    }
}
finally {
    [void][Awake]::SetThreadExecutionState($ES_CONTINUOUS)
    Write-Host "Released awake hold."
}
