$ErrorActionPreference = "Stop"

# Keep this filename so the existing Windows Scheduled Task action remains
# valid; the authoritative job refreshes CRM context, Actions, the
# Active-Accounts-scoped People view, and LinkedIn Pending.

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

function Invoke-LoggedPython {
    param([string[]] $Arguments)

    # Windows PowerShell can promote native stderr to a terminating error when
    # ErrorActionPreference is Stop, truncating the redirected traceback.
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $pythonPath @Arguments *>> $logPath
        return $(if ($null -eq $LASTEXITCODE) { 0 } else { $LASTEXITCODE })
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
}

try {
    Write-Log "starting crm_v2_workflow run_id=$runId"

    Write-Log "starting sync_crm_v2_context run_id=$runId"
    $contextExitCode = Invoke-LoggedPython -Arguments @(
        "manage.py", "sync_crm_v2_context", "--apply"
    )
    Write-Log "finished sync_crm_v2_context run_id=$runId exit_code=$contextExitCode"
    if ($contextExitCode -ne 0) {
        throw "sync_crm_v2_context exited with code $contextExitCode"
    }

    Write-Log "starting refresh_crm_v2 run_id=$runId"
    $refreshExitCode = Invoke-LoggedPython -Arguments @(
        "manage.py",
        "refresh_crm_v2",
        "--apply",
        "--routine",
        "--manual-pin",
        "StackArmor",
        "--owner-override",
        "Ramp=Arian",
        "--owner-override",
        "StackArmor=Arian"
    )
    Write-Log "finished refresh_crm_v2 run_id=$runId exit_code=$refreshExitCode"
    if ($refreshExitCode -ne 0) {
        throw "refresh_crm_v2 exited with code $refreshExitCode"
    }

    Write-Log "starting sync_active_account_people run_id=$runId"
    $peopleExitCode = Invoke-LoggedPython -Arguments @(
        "manage.py", "sync_active_account_people", "--apply"
    )
    Write-Log "finished sync_active_account_people run_id=$runId exit_code=$peopleExitCode"
    if ($peopleExitCode -ne 0) {
        throw "sync_active_account_people exited with code $peopleExitCode"
    }

    Write-Log "starting sync_linkedin_pending run_id=$runId"
    $linkedinPendingExitCode = Invoke-LoggedPython -Arguments @(
        "manage.py", "sync_linkedin_pending", "--apply"
    )
    Write-Log "finished sync_linkedin_pending run_id=$runId exit_code=$linkedinPendingExitCode"
    if ($linkedinPendingExitCode -ne 0) {
        throw "sync_linkedin_pending exited with code $linkedinPendingExitCode"
    }

    Write-Log "finished crm_v2_workflow run_id=$runId exit_code=0"
    exit 0
}
catch {
    Write-Log "crm_v2_workflow failed run_id=${runId}: $($_.Exception.Message)"
    exit 1
}
