#!/bin/bash
# Ralph Wiggum - Long-running AI agent loop
# Usage: ./ralph.sh [--tool amp|claude] [max_iterations]

set -e

# Parse arguments
TOOL="claude"  # Default to claude; pass --tool amp to use amp.
# Pin the model explicitly: a bare `claude --print` inherits the operator's
# ~/.claude/settings.json model (currently a 1M-context Opus-tier pin), which
# would run the whole loop at ~3x Sonnet cost. Override via RALPH_CLAUDE_MODEL.
CLAUDE_MODEL="${RALPH_CLAUDE_MODEL:-claude-sonnet-4-6}"
# Default high enough to clear every remaining `passes: false` story in
# ralph/prd.json with retry headroom. The loop exits early on the
# <promise>COMPLETE</promise> sentinel, so over-allocating is free.
MAX_ITERATIONS=40

while [[ $# -gt 0 ]]; do
  case $1 in
    --tool)
      TOOL="$2"
      shift 2
      ;;
    --tool=*)
      TOOL="${1#*=}"
      shift
      ;;
    *)
      # Assume it's max_iterations if it's a number
      if [[ "$1" =~ ^[0-9]+$ ]]; then
        MAX_ITERATIONS="$1"
      fi
      shift
      ;;
  esac
done

# Validate tool choice
if [[ "$TOOL" != "amp" && "$TOOL" != "claude" ]]; then
  echo "Error: Invalid tool '$TOOL'. Must be 'amp' or 'claude'."
  exit 1
fi

# Abort immediately if the chosen tool isn't on PATH — otherwise the loop
# would spin through MAX_ITERATIONS in milliseconds with `command not found`
# and the broken iterations would (a) trigger the branch-change archive
# logic below with no real work behind it, and (b) silently consume the
# iteration budget. Fail loud, fail fast.
if ! command -v "$TOOL" >/dev/null 2>&1; then
  echo "Error: '$TOOL' is not installed or not on PATH."
  echo "Install it, or pass --tool with the other supported value."
  exit 1
fi
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRD_FILE="$SCRIPT_DIR/prd.json"
PROGRESS_FILE="$SCRIPT_DIR/progress.txt"
ARCHIVE_DIR="$SCRIPT_DIR/archive"
LAST_BRANCH_FILE="$SCRIPT_DIR/.last-branch"

# Archive previous run if branch changed.
#
# IMPORTANT: only fires when the destination folder is brand new. If the
# maintainer already archived prd.json / progress.txt manually (the
# wave-3 -> wave-4 transition did this), `.last-branch` lagging behind a
# manual branchName edit previously caused the script to OVERWRITE the
# manual archive AND nuke ralph/progress.txt into an empty stub. Skip
# the copy + reset when the archive folder already has content.
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

# Track current branch
if [ -f "$PRD_FILE" ]; then
  CURRENT_BRANCH=$(jq -r '.branchName // empty' "$PRD_FILE" 2>/dev/null || echo "")
  if [ -n "$CURRENT_BRANCH" ]; then
    echo "$CURRENT_BRANCH" > "$LAST_BRANCH_FILE"
  fi
fi

# Initialize progress file if it doesn't exist
if [ ! -f "$PROGRESS_FILE" ]; then
  echo "# Ralph Progress Log" > "$PROGRESS_FILE"
  echo "Started: $(date)" >> "$PROGRESS_FILE"
  echo "---" >> "$PROGRESS_FILE"
fi

echo "Starting Ralph - Tool: $TOOL - Max iterations: $MAX_ITERATIONS"

for i in $(seq 1 $MAX_ITERATIONS); do
  echo ""
  echo "==============================================================="
  echo "  Ralph Iteration $i of $MAX_ITERATIONS ($TOOL)"
  echo "==============================================================="

  # Run the selected tool with the ralph prompt
  if [[ "$TOOL" == "amp" ]]; then
    OUTPUT=$(cat "$SCRIPT_DIR/prompt.md" | amp --dangerously-allow-all 2>&1 | tee /dev/stderr) || true
  else
    # Claude Code: use --dangerously-skip-permissions for autonomous operation, --print for output
    OUTPUT=$(claude --model "$CLAUDE_MODEL" --dangerously-skip-permissions --print < "$SCRIPT_DIR/prompt.md" 2>&1 | tee /dev/stderr) || true
  fi
  
  # Check for completion signal
  # MUST require the sentinel on its OWN LINE; a plain `grep -q` matches
  # the prompt's instructional mention of the tag that echoes back via
  # stdout, false-positively exiting the loop after iteration 1. See
  # ralph-codex.sh for the matching fix + 2026-05-22 / 2026-05-24
  # incidents.
  if echo "$OUTPUT" | grep -qE '^[[:space:]]*<promise>COMPLETE</promise>[[:space:]]*$'; then
    echo ""
    echo "Ralph completed all tasks!"
    echo "Completed at iteration $i of $MAX_ITERATIONS"
    exit 0
  fi
  
  # Re-check unfinished-story count after each iteration; bail early if
  # prd.json says everything is green even when the model forgot to emit
  # the <promise>COMPLETE</promise> sentinel.
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
