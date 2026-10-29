#!/usr/bin/env bash
set -euo pipefail

# Claude Code runner for the daily contextual Actions + Active Accounts Next
# step review (docs/claude-crm-review.md). macOS/Linux twin of
# run_claude_crm_review.ps1; both share the prompt and permission settings.
#
#   scripts/run_claude_crm_review.sh            # preview: no Sheet writes
#   scripts/run_claude_crm_review.sh apply      # scheduled mode

MODE="$(printf '%s' "${1:-preview}" | tr '[:lower:]' '[:upper:]')"
case "$MODE" in
  PREVIEW|APPLY) ;;
  *) echo "usage: $0 [preview|apply]" >&2; exit 64 ;;
esac

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON=".venv/bin/python"
PROMPT_FILE="$ROOT_DIR/scripts/claude_crm_review_prompt.md"
SETTINGS_FILE="$ROOT_DIR/scripts/claude_crm_review_settings.json"
OUT_ROOT="$ROOT_DIR/artifacts/claude-review"
LOCK_DIR="$OUT_ROOT/.lock"
LOG_FILE="$OUT_ROOT/runs.log"
CODEX_AUTOMATION="$HOME/.codex/automations/review-followups-across-linkedin-and-email/automation.toml"
RUN_ID="$(date +%Y%m%dT%H%M%S)-$(printf '%s' "$MODE" | tr '[:upper:]' '[:lower:]')"
REPORT="$OUT_ROOT/$(date +%Y-%m-%d)/$RUN_ID.md"

cd "$ROOT_DIR"
mkdir -p "$(dirname "$REPORT")"

log() { printf '[%s] %s\n' "$(date +%Y-%m-%dT%H:%M:%S%z)" "$*" >> "$LOG_FILE"; }

command -v claude >/dev/null || { echo "claude CLI not found on PATH" >&2; exit 69; }
[ -x "$PYTHON" ] || { echo "$PYTHON not found; run from an OpenOutreach checkout" >&2; exit 69; }

# One scheduled runner only: refuse to write while the Codex automation is live.
if [ "$MODE" = APPLY ] && [ -f "$CODEX_AUTOMATION" ] \
   && grep -Eq '^status *= *"ACTIVE"' "$CODEX_AUTOMATION"; then
  log "refused run_id=$RUN_ID: Codex automation is ACTIVE"
  echo "Codex review automation is ACTIVE; pause it before running apply." >&2
  exit 75
fi

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  log "skipped run_id=$RUN_ID: another review holds $LOCK_DIR"
  echo "Another review is running (remove $LOCK_DIR if it is stale)." >&2
  exit 75
fi
trap 'rmdir "$LOCK_DIR"' EXIT

PROMPT="$(sed -e "s|{{MODE}}|$MODE|g" -e "s|{{PYTHON}}|$PYTHON|g" "$PROMPT_FILE")"

log "starting run_id=$RUN_ID"
set +e
printf '%s\n' "$PROMPT" | claude -p \
  --model opus \
  --permission-mode dontAsk \
  --setting-sources project \
  --settings "$SETTINGS_FILE" \
  --strict-mcp-config \
  --add-dir "${TMPDIR:-/tmp}" \
  --output-format text \
  > "$REPORT" 2>> "$LOG_FILE"
CLAUDE_EXIT=$?
set -e

STATUS="$(grep -m1 -Eo '^STATUS: [A-Z]+' "$REPORT" | cut -d' ' -f2 || true)"
cp "$REPORT" "$OUT_ROOT/latest.md"
log "finished run_id=$RUN_ID claude_exit=$CLAUDE_EXIT status=${STATUS:-MISSING} report=$REPORT"

if [ "$CLAUDE_EXIT" -ne 0 ]; then exit "$CLAUDE_EXIT"; fi
case "$STATUS" in
  QUIET|CHANGES|PREVIEW) exit 0 ;;
  BLOCKED) exit 2 ;;
  *) exit 3 ;;
esac
