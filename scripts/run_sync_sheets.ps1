$ErrorActionPreference = "Stop"

# Keep this filename so the existing Windows Scheduled Task action remains
# valid; the authoritative job it launches is now refresh_crm.

$repoRoot = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $repoRoot "data\logs"
$logPath = Join-Path $logDir "refresh_crm_task.log"
$pythonPath = Join-Path $repoRoot ".venv\Scripts\python.exe"

New-Item -ItemType Directory -Force -Path $logDir | Out-Null
Set-Location -LiteralPath $repoRoot

function Write-Log {
    param([string] $Message)
    $stamp = Get-Date -Format "yyyy-MM-ddTHH:mm:ssK"
    Add-Content -Path $logPath -Value "[$stamp] $Message"
}

try {
    Write-Log "starting refresh_crm"
    & $pythonPath manage.py refresh_crm --apply *>> $logPath
    $exitCode = if ($null -eq $LASTEXITCODE) { 0 } else { $LASTEXITCODE }
    Write-Log "finished refresh_crm exit_code=$exitCode"
    exit $exitCode
}
catch {
    Write-Log "refresh_crm failed: $($_.Exception.Message)"
    throw
}
