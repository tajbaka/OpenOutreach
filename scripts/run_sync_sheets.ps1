$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $repoRoot "data\logs"
$logPath = Join-Path $logDir "sync_sheets_task.log"
$pythonPath = Join-Path $repoRoot ".venv\Scripts\python.exe"

New-Item -ItemType Directory -Force -Path $logDir | Out-Null
Set-Location -LiteralPath $repoRoot

function Write-Log {
    param([string] $Message)
    $stamp = Get-Date -Format "yyyy-MM-ddTHH:mm:ssK"
    Add-Content -Path $logPath -Value "[$stamp] $Message"
}

try {
    Write-Log "starting sync_sheets"
    & $pythonPath manage.py sync_sheets *>> $logPath
    $exitCode = if ($null -eq $LASTEXITCODE) { 0 } else { $LASTEXITCODE }
    Write-Log "finished sync_sheets exit_code=$exitCode"
    exit $exitCode
}
catch {
    Write-Log "sync_sheets failed: $($_.Exception.Message)"
    throw
}
