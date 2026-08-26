$ErrorActionPreference = "Stop"

# Keep this filename so the existing Windows Scheduled Task action remains
# valid; the authoritative job it launches is now the two-phase CRM v2 refresh.

$repoRoot = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $repoRoot "data\logs"
$logPath = Join-Path $logDir "crm_v2_task.log"
$pythonPath = Join-Path $repoRoot ".venv\Scripts\python.exe"
$runId = [guid]::NewGuid().ToString("N")

New-Item -ItemType Directory -Force -Path $logDir | Out-Null
Set-Location -LiteralPath $repoRoot

function Write-Log {
    param([string] $Message)
    $stamp = Get-Date -Format "yyyy-MM-ddTHH:mm:ssK"
    Add-Content -Path $logPath -Value "[$stamp] $Message"
}

try {
    Write-Log "starting crm_v2_workflow run_id=$runId"

    Write-Log "starting sync_crm_v2_context run_id=$runId"
    & $pythonPath manage.py sync_crm_v2_context --apply *>> $logPath
    $contextExitCode = if ($null -eq $LASTEXITCODE) { 0 } else { $LASTEXITCODE }
    Write-Log "finished sync_crm_v2_context run_id=$runId exit_code=$contextExitCode"
    if ($contextExitCode -ne 0) {
        throw "sync_crm_v2_context exited with code $contextExitCode"
    }

    Write-Log "starting refresh_crm_v2 run_id=$runId"
    & $pythonPath manage.py refresh_crm_v2 --apply --routine `
        --manual-pin StackArmor `
        --owner-override Ramp=Arian `
        --owner-override StackArmor=Arian *>> $logPath
    $refreshExitCode = if ($null -eq $LASTEXITCODE) { 0 } else { $LASTEXITCODE }
    Write-Log "finished refresh_crm_v2 run_id=$runId exit_code=$refreshExitCode"
    if ($refreshExitCode -ne 0) {
        throw "refresh_crm_v2 exited with code $refreshExitCode"
    }

    Write-Log "finished crm_v2_workflow run_id=$runId exit_code=0"
    exit 0
}
catch {
    Write-Log "crm_v2_workflow failed run_id=${runId}: $($_.Exception.Message)"
    exit 1
}
