param(
    [ValidateSet("Preview", "Apply")]
    [string] $Mode = "Preview"
)

$ErrorActionPreference = "Stop"

# Claude Code runner for the daily contextual Actions + Active Accounts Next
# step review (docs/claude-crm-review.md). Windows twin of
# run_claude_crm_review.sh; both share the prompt and permission settings.
#
#   scripts\run_claude_crm_review.ps1                # preview: no Sheet writes
#   scripts\run_claude_crm_review.ps1 -Mode Apply    # scheduled mode

$repoRoot = Split-Path -Parent $PSScriptRoot
$python = ".venv/Scripts/python.exe"
$promptFile = Join-Path $repoRoot "scripts\claude_crm_review_prompt.md"
$settingsFile = Join-Path $repoRoot "scripts\claude_crm_review_settings.json"
$outRoot = Join-Path $repoRoot "artifacts\claude-review"
$lockPath = Join-Path $outRoot ".lock"
$logPath = Join-Path $outRoot "runs.log"
$codexAutomation = Join-Path $env:USERPROFILE ".codex\automations\review-followups-across-linkedin-and-email\automation.toml"
$modeUpper = $Mode.ToUpperInvariant()
$runId = "{0}-{1}" -f (Get-Date -Format "yyyyMMddTHHmmss"), $Mode.ToLowerInvariant()
$reportDir = Join-Path $outRoot (Get-Date -Format "yyyy-MM-dd")
$reportPath = Join-Path $reportDir "$runId.md"

New-Item -ItemType Directory -Force -Path $reportDir | Out-Null
Set-Location -LiteralPath $repoRoot

function Write-Log {
    param([string] $Message)
    $stamp = Get-Date -Format "yyyy-MM-ddTHH:mm:ssK"
    Add-Content -Path $logPath -Value "[$stamp] $Message"
}

if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    throw "claude CLI not found on PATH"
}
if (-not (Test-Path -LiteralPath (Join-Path $repoRoot $python))) {
    throw "$python not found; run from an OpenOutreach checkout"
}

# One scheduled runner only: refuse to write while the Codex automation is live.
if ($Mode -eq "Apply" -and (Test-Path -LiteralPath $codexAutomation) -and
    (Select-String -LiteralPath $codexAutomation -Pattern '^status *= *"ACTIVE"' -Quiet)) {
    Write-Log "refused run_id=$runId : Codex automation is ACTIVE"
    # Write-Error would throw under "Stop" and lose the exit code.
    [Console]::Error.WriteLine("Codex review automation is ACTIVE; pause it before running Apply.")
    exit 75
}

try {
    New-Item -ItemType Directory -Path $lockPath -ErrorAction Stop | Out-Null
}
catch {
    Write-Log "skipped run_id=$runId : another review holds $lockPath"
    [Console]::Error.WriteLine("Another review is running (remove $lockPath if it is stale).")
    exit 75
}

try {
    $prompt = (Get-Content -LiteralPath $promptFile -Raw -Encoding UTF8).
        Replace("{{MODE}}", $modeUpper).Replace("{{PYTHON}}", $python)

    # Windows PowerShell pipes ASCII to native programs unless told otherwise.
    $utf8 = New-Object System.Text.UTF8Encoding $false
    $OutputEncoding = $utf8
    [Console]::OutputEncoding = $utf8

    $claudeArgs = @(
        "-p",
        "--model", "opus",
        "--permission-mode", "dontAsk",
        "--setting-sources", "project",
        "--settings", $settingsFile,
        "--strict-mcp-config",
        "--add-dir", $env:TEMP,
        "--output-format", "text"
    )

    Write-Log "starting run_id=$runId"
    $previousPreference = $ErrorActionPreference
    try {
        # Native stderr must not become a terminating error (see run_sync_sheets.ps1).
        $ErrorActionPreference = "Continue"
        $report = $prompt | & claude @claudeArgs 2>> $logPath
        $claudeExit = if ($null -eq $LASTEXITCODE) { 0 } else { $LASTEXITCODE }
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }

    $report | Set-Content -LiteralPath $reportPath -Encoding UTF8
    Copy-Item -LiteralPath $reportPath -Destination (Join-Path $outRoot "latest.md") -Force

    $status = ($report | Select-String -Pattern '^STATUS: ([A-Z]+)' | Select-Object -First 1)
    $statusName = if ($status) { $status.Matches[0].Groups[1].Value } else { "MISSING" }
    Write-Log "finished run_id=$runId claude_exit=$claudeExit status=$statusName report=$reportPath"

    $exitCode = if ($claudeExit -ne 0) { $claudeExit }
        elseif ($statusName -in "QUIET", "CHANGES", "PREVIEW") { 0 }
        elseif ($statusName -eq "BLOCKED") { 2 }
        else { 3 }
}
finally {
    Remove-Item -LiteralPath $lockPath -Force -ErrorAction SilentlyContinue
}
exit $exitCode
