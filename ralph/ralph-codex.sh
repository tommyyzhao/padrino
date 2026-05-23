#!/bin/bash
# Ralph Wiggum — long-running AI agent loop, Codex CLI edition.
# Usage: ./ralph-codex.sh [max_iterations]
#
# Mirrors ralph.sh but invokes `codex exec` with the `gpt-5.5` model at
# the `xhigh` reasoning effort. Codex's authentication is via ChatGPT Pro
# OAuth (no API key required); see `codex login` if you've never run it.
#
# Sister script: ralph.sh — same loop on the `claude` CLI.

set -e

# Default high enough to clear every remaining `passes: false` story in
# ralph/prd.json with retry headroom. The loop exits early on the
# <promise>COMPLETE</promise> sentinel, so over-allocating is free.
MAX_ITERATIONS=40

# Parse arguments — only positional max-iterations for now; pass-through
# `-c key=value` overrides via CODEX_EXTRA_ARGS env var if needed.
while [[ $# -gt 0 ]]; do
  case $1 in
    *)
      if [[ "$1" =~ ^[0-9]+$ ]]; then
        MAX_ITERATIONS="$1"
      fi
      shift
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRD_FILE="$SCRIPT_DIR/prd.json"
PROGRESS_FILE="$SCRIPT_DIR/progress.txt"
ARCHIVE_DIR="$SCRIPT_DIR/archive"
LAST_BRANCH_FILE="$SCRIPT_DIR/.last-branch"

# Abort immediately if codex isn't on PATH — otherwise the loop would
# spin through MAX_ITERATIONS in milliseconds with `command not found`
# and trigger the branch-change archive logic below with no real work
# behind it. Fail loud, fail fast. (Same guard pattern as ralph.sh.)
if ! command -v codex >/dev/null 2>&1; then
  echo "Error: 'codex' is not installed or not on PATH."
  echo "Install Codex CLI and run 'codex login' before using this script."
  echo "See: https://docs.codex.com/cli  (or use ./ralph.sh for Claude.)"
  exit 1
fi

# Archive previous run if branch changed.
#
# IMPORTANT: this auto-archive only fires when the destination folder is
# brand new. If the maintainer already archived `prd.json` / `progress.txt`
# manually (the wave-3 -> wave-4 transition did this), `.last-branch`
# lagging behind a manual `branchName` edit in `prd.json` previously caused
# the script to OVERWRITE the manual archive with the new wave's files and
# nuke the wave-N progress log into an empty stub. Skip the copy + reset
# when the archive folder already has content — the maintainer's manual
# archive wins.
if [ -f "$PRD_FILE" ] && [ -f "$LAST_BRANCH_FILE" ]; then
  CURRENT_BRANCH=$(jq -r '.branchName // empty' "$PRD_FILE" 2>/dev/null || echo "")
  LAST_BRANCH=$(cat "$LAST_BRANCH_FILE" 2>/dev/null || echo "")

  if [ -n "$CURRENT_BRANCH" ] && [ -n "$LAST_BRANCH" ] && [ "$CURRENT_BRANCH" != "$LAST_BRANCH" ]; then
    DATE=$(date +%Y-%m-%d)
    FOLDER_NAME=$(echo "$LAST_BRANCH" | sed 's|^ralph/||')
    ARCHIVE_FOLDER="$ARCHIVE_DIR/$DATE-$FOLDER_NAME"

    if [ -e "$ARCHIVE_FOLDER/prd.json" ] || [ -e "$ARCHIVE_FOLDER/progress.txt" ]; then
      echo "Skipping archive: $ARCHIVE_FOLDER already populated (manual archive)."
      echo "   The current ralph/progress.txt stays intact."
    else
      echo "Archiving previous run: $LAST_BRANCH"
      mkdir -p "$ARCHIVE_FOLDER"
      [ -f "$PRD_FILE" ] && cp "$PRD_FILE" "$ARCHIVE_FOLDER/"
      [ -f "$PROGRESS_FILE" ] && cp "$PROGRESS_FILE" "$ARCHIVE_FOLDER/"
      echo "   Archived to: $ARCHIVE_FOLDER"

      echo "# Ralph Progress Log" > "$PROGRESS_FILE"
      echo "Started: $(date)" >> "$PROGRESS_FILE"
      echo "---" >> "$PROGRESS_FILE"
    fi
  fi
fi

# Track current branch.
if [ -f "$PRD_FILE" ]; then
  CURRENT_BRANCH=$(jq -r '.branchName // empty' "$PRD_FILE" 2>/dev/null || echo "")
  if [ -n "$CURRENT_BRANCH" ]; then
    echo "$CURRENT_BRANCH" > "$LAST_BRANCH_FILE"
  fi
fi

# Initialize progress file if missing.
if [ ! -f "$PROGRESS_FILE" ]; then
  echo "# Ralph Progress Log" > "$PROGRESS_FILE"
  echo "Started: $(date)" >> "$PROGRESS_FILE"
  echo "---" >> "$PROGRESS_FILE"
fi

# Codex invocation: gpt-5.5 at xhigh reasoning, workspace-write sandbox
# (so it can edit files in the repo), approvals bypassed for autonomous
# operation. `-c` overrides come AFTER user config so the script is
# reproducible regardless of ~/.codex/config.toml defaults.
CODEX_ARGS=(
  exec
  --dangerously-bypass-approvals-and-sandbox
  -m gpt-5.5
  -c model_reasoning_effort=xhigh
  -C "$(pwd)"
  ${CODEX_EXTRA_ARGS:-}
  -
)

echo "Starting Ralph (Codex edition) — model: gpt-5.5/xhigh — Max iterations: $MAX_ITERATIONS"

for i in $(seq 1 $MAX_ITERATIONS); do
  echo ""
  echo "==============================================================="
  echo "  Ralph Iteration $i of $MAX_ITERATIONS (codex/gpt-5.5/xhigh)"
  echo "==============================================================="

  OUTPUT=$(cat "$SCRIPT_DIR/prompt.md" | codex "${CODEX_ARGS[@]}" 2>&1 | tee /dev/stderr) || true

  # Check for completion signal.
  if echo "$OUTPUT" | grep -q "<promise>COMPLETE</promise>"; then
    echo ""
    echo "Ralph completed all tasks!"
    echo "Completed at iteration $i of $MAX_ITERATIONS"
    exit 0
  fi

  # Re-check unfinished-story count after each iteration; bail early
  # if prd.json says everything is green even when the model forgot to
  # emit the <promise>COMPLETE</promise> sentinel.
  if command -v jq >/dev/null 2>&1 && [ -f "$PRD_FILE" ]; then
    REMAINING=$(jq '[.userStories[] | select(.passes==false)] | length' "$PRD_FILE" 2>/dev/null || echo "?")
    if [ "$REMAINING" = "0" ]; then
      echo ""
      echo "Ralph completed all tasks (prd.json shows 0 unfinished stories)."
      echo "Completed at iteration $i of $MAX_ITERATIONS"
      exit 0
    fi
    echo "Iteration $i complete. Remaining stories: $REMAINING. Continuing..."
  else
    echo "Iteration $i complete. Continuing..."
  fi
done

echo ""
echo "Ralph reached max iterations ($MAX_ITERATIONS) without completing all tasks."
echo "Check $PROGRESS_FILE for status."
exit 1
