param(
    [string] $At = "09:00",
    [switch] $Unregister
)

$ErrorActionPreference = "Stop"

# Registers (or removes) the Windows Scheduled Task that runs the Claude CRM
# review daily in Apply mode. See docs/claude-crm-review.md before enabling.

$taskName = "OpenOutreach Claude CRM Review"
$repoRoot = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $repoRoot "scripts\run_claude_crm_review.ps1"
$codexAutomation = Join-Path $env:USERPROFILE ".codex\automations\review-followups-across-linkedin-and-email\automation.toml"

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Output "Removed scheduled task '$taskName'."
    exit 0
}

if ((Test-Path -LiteralPath $codexAutomation) -and
    (Select-String -LiteralPath $codexAutomation -Pattern '^status *= *"ACTIVE"' -Quiet)) {
    throw "The Codex review automation is ACTIVE. Pause it in Codex first; only one runner may be enabled."
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$runner`" -Mode Apply" `
    -WorkingDirectory $repoRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

# Runs as the signed-in user, who owns the Claude login and the checkout's credentials.
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "Daily contextual Actions + Active Accounts Next step review (Claude Code)." | Out-Null

Write-Output "Registered '$taskName' daily at $At (local time). Next run: $((Get-ScheduledTask -TaskName $taskName | Get-ScheduledTaskInfo).NextRunTime)"
