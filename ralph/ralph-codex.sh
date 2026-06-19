#!/bin/bash
# Ralph Wiggum - long-running AI agent loop, Codex CLI edition.
# Usage:
#   ./ralph/ralph-codex.sh [max_iterations]
#   ./ralph/ralph-codex.sh --all-remaining [--max-iterations N] [--v9-iterations N]
#
# The default mode runs the active ralph/prd.json until all passes:false
# stories are complete or the iteration budget is exhausted.
#
# --all-remaining is the one-command operator path for the current handoff:
# finish Wave 8, verify/merge it to main, import and swap in Wave 9, then run
# Wave 9 to completion. It does not push, tag, or open PRs.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

PRD_FILE="$SCRIPT_DIR/prd.json"
PROGRESS_FILE="$SCRIPT_DIR/progress.txt"
ARCHIVE_DIR="$SCRIPT_DIR/archive"
LAST_BRANCH_FILE="$SCRIPT_DIR/.last-branch"
V9_DOCS_BRANCH="docs/padrino-v9-human-multiplayer-plan"
V9_BRANCH="ralph/padrino-v9"

MAX_ITERATIONS=40
V9_ITERATIONS=60
RUN_ALL=0
SYNC_TOOLING=1

usage() {
  cat <<'EOF'
Usage:
  ./ralph/ralph-codex.sh [max_iterations]
      Run the active ralph/prd.json plan until complete.

  ./ralph/ralph-codex.sh --all-remaining [--max-iterations N] [--v9-iterations N]
      Finish the active Wave 8 plan, merge it to main, swap in Wave 9, and
      run Wave 9 to completion. No remote push/tag/PR is performed.

Options:
  --max-iterations N   Iteration budget for the active/current wave.
  --v9-iterations N    Iteration budget for Wave 9 in --all-remaining mode.
  --no-sync            Skip uv/pnpm dependency sync preflight.
  -h, --help           Show this help.

Environment:
  CODEX_MODEL                 Defaults to gpt-5.5.
  CODEX_REASONING_EFFORT      Defaults to xhigh.
  CODEX_EXTRA_ARGS            Extra args appended before the prompt stdin dash.
  RALPH_SKIP_PLAYWRIGHT_INSTALL=1  Skip Playwright browser install preflight.
EOF
}

die() {
  echo "Error: $*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --all-remaining|--all-waves)
      RUN_ALL=1
      shift
      ;;
    --max-iterations)
      MAX_ITERATIONS="${2:?missing value for --max-iterations}"
      shift 2
      ;;
    --max-iterations=*)
      MAX_ITERATIONS="${1#*=}"
      shift
      ;;
    --v9-iterations)
      V9_ITERATIONS="${2:?missing value for --v9-iterations}"
      shift 2
      ;;
    --v9-iterations=*)
      V9_ITERATIONS="${1#*=}"
      shift
      ;;
    --no-sync)
      SYNC_TOOLING=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      if [[ "$1" =~ ^[0-9]+$ ]]; then
        MAX_ITERATIONS="$1"
      else
        die "unknown argument '$1'"
      fi
      shift
      ;;
  esac
done

for n in "$MAX_ITERATIONS" "$V9_ITERATIONS"; do
  [[ "$n" =~ ^[0-9]+$ ]] || die "iteration budgets must be positive integers"
  [ "$n" -gt 0 ] || die "iteration budgets must be positive integers"
done

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "'$1' is not installed or not on PATH"
}

require_base_tools() {
  require_command git
  require_command jq
  require_command uv
  require_command codex
}

require_all_mode_tools() {
  require_base_tools
  require_command pnpm
  require_command python3
}

require_clean_tree() {
  if [ -n "$(git status --porcelain)" ]; then
    git status --short
    die "working tree must be clean before this operation"
  fi
}

plan_branch() {
  jq -r '.branchName // empty' "$PRD_FILE"
}

remaining_count() {
  jq '[.userStories[] | select(.passes==false)] | length' "$PRD_FILE"
}

remaining_ids() {
  jq -r '[.userStories[] | select(.passes==false) | .id] | join(", ")' "$PRD_FILE"
}

checkout_or_create_branch() {
  local branch="$1"
  local start="${2:-main}"

  if git show-ref --verify --quiet "refs/heads/$branch"; then
    git checkout "$branch"
  else
    git checkout -b "$branch" "$start"
  fi
}

commit_if_needed() {
  local message="$1"
  if [ -n "$(git status --porcelain)" ]; then
    git add -A
    git commit -m "$message"
  else
    echo "No changes to commit for: $message"
  fi
}

initialize_progress_file() {
  if [ ! -f "$PROGRESS_FILE" ]; then
    {
      echo "# Ralph Progress Log"
      echo "Started: $(date)"
      echo "---"
    } > "$PROGRESS_FILE"
  fi
}

neutralize_auto_archive_for_active_plan() {
  local branch
  branch="$(plan_branch)"
  [ -n "$branch" ] || die "ralph/prd.json has no branchName"
  echo "$branch" > "$LAST_BRANCH_FILE"
}

sync_tooling() {
  [ "$SYNC_TOOLING" -eq 1 ] || return 0

  echo "Syncing Python dev dependencies..."
  uv sync --extra dev

  if [ -f "web/dashboard/package.json" ]; then
    echo "Syncing dashboard dependencies..."
    pnpm -C web/dashboard install --frozen-lockfile

    if [ "${RALPH_SKIP_PLAYWRIGHT_INSTALL:-0}" != "1" ]; then
      echo "Ensuring Playwright browsers are installed..."
      pnpm -C web/dashboard exec playwright install
    fi
  fi
}

run_backend_gates() {
  echo "Running backend quality gates..."
  uv run ruff check .
  uv run ruff format --check .
  uv run mypy src tests
  uv run pytest -m "not integration"
}

build_codex_args() {
  CODEX_ARGS=(
    exec
    --dangerously-bypass-approvals-and-sandbox
    -m "${CODEX_MODEL:-gpt-5.5}"
    -c "model_reasoning_effort=${CODEX_REASONING_EFFORT:-xhigh}"
    -C "$REPO_ROOT"
  )

  if [ -n "${CODEX_EXTRA_ARGS:-}" ]; then
    # shellcheck disable=SC2206
    local extra_args=( ${CODEX_EXTRA_ARGS} )
    CODEX_ARGS+=("${extra_args[@]}")
  fi

  CODEX_ARGS+=(-)
}

emit_iteration_prompt() {
  local branch count ids
  branch="$(plan_branch)"
  count="$(remaining_count)"
  ids="$(remaining_ids)"

  cat <<EOF
# Ralph Agent Instructions - Padrino Active Wave

You are an autonomous coding agent in the Padrino repo:
\`$REPO_ROOT\`

This prompt is generated by \`ralph/ralph-codex.sh\` so it stays wave-agnostic.
One iteration = exactly one user story.

Active branch from \`ralph/prd.json\`: \`$branch\`
Remaining story count: \`$count\`
Remaining story ids: \`$ids\`

## Boot sequence

1. Read these files first:
   - \`AGENTS.md\` and \`CLAUDE.md\`
   - the vision docs referenced by \`AGENTS.md\` and by the active story
   - \`ralph/progress.txt\`, especially \`## Codebase Patterns\`
   - \`ralph/prd.json\`
2. Confirm git branch equals the \`branchName\` in \`ralph/prd.json\`. If not,
   check it out. Never work on \`main\` directly for a story.
3. Pick the highest-priority story whose \`passes\` is \`false\` and whose
   dependencies are all already \`passes: true\`.
4. Work only that story. Satisfy every acceptance criterion for that story.

## Implementation loop

1. Implement test-first: failing test, implementation, green, refactor.
2. Run all backend gates:
   \`\`\`bash
   uv run ruff check .
   uv run ruff format --check .
   uv run mypy src tests
   uv run pytest -m "not integration"
   \`\`\`
3. If the story touches the dashboard/frontend or its acceptance criteria name
   pnpm/Playwright, also run:
   \`\`\`bash
   pnpm -C web/dashboard lint
   pnpm -C web/dashboard check
   pnpm -C web/dashboard test:e2e
   \`\`\`
4. Fix root causes only. Do not use \`--no-verify\`, \`git commit --amend\`,
   broad \`# type: ignore\`, skipped/deleted tests, or weakened assertions to
   force green.
5. Commit the implementation with exactly:
   \`\`\`
   feat: US-XXX - <story title verbatim from ralph/prd.json>
   \`\`\`
6. Set only that story's \`passes\` field to \`true\` in \`ralph/prd.json\`.
7. Append a dated entry to \`ralph/progress.txt\` with files changed, gates run,
   and useful learnings. Add reusable patterns to the Codebase Patterns section.
8. Commit tracking changes with, or immediately after, the implementation
   commit.

## Hard rules

- Obey every hard rule in \`AGENTS.md\`.
- No impure calls under \`src/padrino/core/\`: no DB/network/wall-clock/random.
- Chat text is never parsed for mechanics; only structured actions drive state.
- Keep Alembic migrations linear; if a story needs schema, add one new revision
  after the current head.
- Do not push, tag, open PRs, or work outside the active story.

## Stop condition

After committing the story and updating tracking files, run exactly:

\`\`\`bash
jq '[.userStories[] | select(.passes==false)] | length' ralph/prd.json
\`\`\`

Quote that output. If it printed \`0\`, end your response with exactly:

\`\`\`
<promise>COMPLETE</promise>
\`\`\`

Do not emit the sentinel while any story remains \`passes: false\`.
EOF
}

run_active_plan_loop() {
  local max_iterations="$1"
  local branch count output remaining

  initialize_progress_file
  neutralize_auto_archive_for_active_plan
  build_codex_args

  branch="$(plan_branch)"
  [ -n "$branch" ] || die "ralph/prd.json has no branchName"
  checkout_or_create_branch "$branch" main

  count="$(remaining_count)"
  if [ "$count" = "0" ]; then
    echo "Active plan already complete: $branch"
    return 0
  fi

  echo "Starting Ralph Codex loop on $branch - remaining stories: $count - max iterations: $max_iterations"

  for i in $(seq 1 "$max_iterations"); do
    echo ""
    echo "==============================================================="
    echo "  Ralph Codex Iteration $i of $max_iterations"
    echo "==============================================================="

    output="$(emit_iteration_prompt | codex "${CODEX_ARGS[@]}" 2>&1 | tee /dev/stderr)" || true

    if echo "$output" | grep -qE '^[[:space:]]*<promise>COMPLETE</promise>[[:space:]]*$'; then
      echo ""
      echo "Ralph completed all tasks by sentinel."
      return 0
    fi

    remaining="$(remaining_count)"
    if [ "$remaining" = "0" ]; then
      echo ""
      echo "Ralph completed all tasks (prd.json shows 0 unfinished stories)."
      return 0
    fi

    echo "Iteration $i complete. Remaining stories: $remaining. Continuing..."
  done

  die "Ralph reached max iterations ($max_iterations) without completing all tasks"
}

merge_branch_if_needed() {
  local branch="$1"
  local message="$2"

  if git merge-base --is-ancestor "$branch" HEAD; then
    echo "$branch is already merged into $(git branch --show-current)."
  else
    git merge --no-ff "$branch" -m "$message"
  fi
}

unique_archive_folder() {
  local branch_slug="$1"
  local date base candidate n

  date="$(date +%Y-%m-%d)"
  base="$ARCHIVE_DIR/$date-$branch_slug"
  candidate="$base"
  n=2

  while [ -e "$candidate/prd.json" ] || [ -e "$candidate/progress.txt" ]; do
    candidate="$base-$n"
    n=$((n + 1))
  done

  echo "$candidate"
}

extract_codebase_patterns() {
  awk '
    /^## Codebase Patterns$/ { capture = 1 }
    capture && /^## / && $0 != "## Codebase Patterns" { exit }
    capture { print }
  ' "$PROGRESS_FILE"
}

reset_progress_for_v9() {
  local patterns tmp
  patterns="$(extract_codebase_patterns || true)"
  tmp="$(mktemp)"

  {
    echo "# Ralph Progress Log"
    echo "Started: $(date)"
    echo "---"
    if [ -n "$patterns" ]; then
      echo
      printf '%s\n' "$patterns"
      echo "---"
    fi
  } > "$tmp"

  mv "$tmp" "$PROGRESS_FILE"
}

apply_v9_agents_changes() {
  python3 - <<'PY'
from pathlib import Path

path = Path("AGENTS.md")
text = path.read_text()

human_para = (
    "Padrino now also has a **human-multiplayer layer** (`prd-v3.md`): humans play\n"
    "with/against models in **private friend lobbies**, default **anonymous**,\n"
    "always **casual**, and **strictly segregated** from the scientific benchmark.\n"
    "Vision docs: `prd.md` (v1 backend), `prd-v2.md` (v2 spectator site), `prd-v3.md`\n"
    "(v3 human multiplayer). The executable plan is `ralph/prd.json`."
)

if human_para not in text:
    anchor = "The full vision lives in `prd.md`. The executable v1 plan lives in `ralph/prd.json`."
    if anchor not in text:
        raise SystemExit("AGENTS.md: could not find What Padrino is anchor")
    text = text.replace(anchor, f"{anchor}\n\n{human_para}", 1)

new_rules = """### 6. Frontend is first-class (since Wave 7)

The SvelteKit dashboard under `web/dashboard/` is part of the product. Frontend
stories run the pnpm gates (`pnpm -C web/dashboard lint` / `check` /
`test:e2e` via Playwright) in addition to the four backend gates. The
pure-core firewall still applies only to `src/padrino/core/**`; it never blocks
the TypeScript project under `web/`.

### 7. Anonymity is identity-blind for humans too (Wave 9)

On any live / observation / spectator surface, never reveal which seats are
human vs AI, nor model/provider identity, before the **endgame reveal**. Guards
**fail CLOSED** (a missing/None `identity_mode` coerces to ANONYMOUS). The
guard covers DB **columns**, not only payload keys. Composition is disclosed as
**counts only** ("N humans, M AI"), frozen at game start.

### 8. Human games are segregated from the benchmark (Wave 9)

A human-lane game writes **ZERO** rows to the scientific `Rating`/`RatingEvent`
tables. Human ELO lives in a **dormant** sibling table under the
"Humans-Included League" and is not written in v1 (casual).

### 9. Human chat is stored out-of-band of the hash chain (Wave 9)

Human free-text chat lives in a **sidecar** table; the paired core event
carries only a reference/hash. This preserves the chat-firewall and makes GDPR
erasure possible without breaking the hash chain or deterministic replay.

### 10. Human games run on a separate worker lane (Wave 9)

Minutes-to-hours human games run on a dedicated runner lane with **durable,
rehydratable Postgres-snapshot** state, isolated from the benchmark scheduler's
concurrency cap. **No Redis** (stack rule)."""

if "### 6. Frontend is first-class (since Wave 7)" not in text:
    start = text.find("### 6. Backend-only")
    if start == -1:
        raise SystemExit("AGENTS.md: could not find old hard rule 6")
    end = text.find("\n---", start)
    if end == -1:
        raise SystemExit("AGENTS.md: could not find end of hard rules section")
    text = text[:start] + new_rules + text[end:]

quality_note = (
    "Frontend stories additionally run: `pnpm -C web/dashboard lint`,\n"
    "`pnpm -C web/dashboard check`, `pnpm -C web/dashboard test:e2e`."
)
if quality_note not in text:
    quality_anchor = (
        "uv run pytest -m \"not integration\"          # unit + integration (no real LLM)\n"
        "```"
    )
    if quality_anchor not in text:
        raise SystemExit("AGENTS.md: could not find quality gate block")
    text = text.replace(quality_anchor, f"{quality_anchor}\n\n{quality_note}", 1)

ruleset_note = (
    "The human-multiplayer lane reuses `mini7_v1` / `bench10_v1` (decision 15); a\n"
    "buffered-cadence variant (`hcad7_v1` / `hcad10_v1`) is **deferred** past v1."
)
if ruleset_note not in text:
    ruleset_anchor = (
        "Every rating is stamped with `ruleset_id` so each variant gets its own leaderboard."
    )
    if ruleset_anchor not in text:
        raise SystemExit("AGENTS.md: could not find ruleset anchor")
    text = text.replace(ruleset_anchor, f"{ruleset_anchor}\n\n{ruleset_note}", 1)

deps_note = (
    "Wave 9 adds `authlib` (one-provider OAuth, US-129). Justify any other new\n"
    "runtime dependency in `progress.txt`. The durable human-game state uses the\n"
    "existing async DB (Postgres snapshots) — **do not** add Redis."
)
if deps_note not in text:
    deps_anchor = "**Do not add new runtime dependencies** without justification in the progress log. Prefer stdlib."
    if deps_anchor not in text:
        raise SystemExit("AGENTS.md: could not find dependencies anchor")
    text = text.replace(deps_anchor, f"{deps_anchor}\n\n{deps_note}", 1)

path.write_text(text)
PY
}

prepare_v9_branch() {
  local archive_folder

  git checkout main
  require_clean_tree

  git show-ref --verify --quiet "refs/heads/$V9_DOCS_BRANCH" \
    || die "missing local branch $V9_DOCS_BRANCH"
  merge_branch_if_needed "$V9_DOCS_BRANCH" "docs: merge Wave 9 human-multiplayer plan"

  if git show-ref --verify --quiet "refs/heads/$V9_BRANCH"; then
    git checkout "$V9_BRANCH"
    if ! git merge-base --is-ancestor main HEAD; then
      git merge --no-ff main -m "Merge main into $V9_BRANCH before continuing"
    fi
  else
    git checkout -b "$V9_BRANCH" main
  fi

  if [ "$(plan_branch)" = "$V9_BRANCH" ]; then
    echo "Wave 9 is already active on $V9_BRANCH."
    neutralize_auto_archive_for_active_plan
    return 0
  fi

  [ -f "$SCRIPT_DIR/prd-v9.json" ] || die "missing $SCRIPT_DIR/prd-v9.json"
  [ -f "$SCRIPT_DIR/prompt-v9.md" ] || die "missing $SCRIPT_DIR/prompt-v9.md"

  archive_folder="$(unique_archive_folder "padrino-v8")"
  mkdir -p "$archive_folder"
  cp "$PRD_FILE" "$archive_folder/prd.json"
  cp "$PROGRESS_FILE" "$archive_folder/progress.txt"

  cp "$SCRIPT_DIR/prd-v9.json" "$PRD_FILE"
  cp "$SCRIPT_DIR/prompt-v9.md" "$SCRIPT_DIR/prompt.md"
  reset_progress_for_v9
  apply_v9_agents_changes
  echo "$V9_BRANCH" > "$LAST_BRANCH_FILE"

  git add AGENTS.md "$PRD_FILE" "$PROGRESS_FILE" "$SCRIPT_DIR/prompt.md" "$archive_folder"
  commit_if_needed "docs: prepare Wave 9 human-multiplayer Ralph run"
}

run_all_remaining() {
  local active_branch

  require_all_mode_tools
  require_clean_tree
  sync_tooling
  require_clean_tree

  active_branch="$(plan_branch)"
  [ -n "$active_branch" ] || die "ralph/prd.json has no branchName"
  checkout_or_create_branch "$active_branch" main

  run_active_plan_loop "$MAX_ITERATIONS"
  require_clean_tree
  run_backend_gates

  if [ "$active_branch" = "ralph/padrino-v8" ]; then
    git checkout main
    require_clean_tree
    merge_branch_if_needed "$active_branch" "Merge ralph/padrino-v8: Wave 8 Production Launch Hardening"
    run_backend_gates

    prepare_v9_branch
    require_clean_tree
    run_backend_gates

    run_active_plan_loop "$V9_ITERATIONS"
    require_clean_tree
    run_backend_gates
  fi

  echo ""
  echo "All requested Ralph work is complete."
  echo "Current branch: $(git branch --show-current)"
  echo "Remaining stories: $(remaining_count)"
}

run_default() {
  require_base_tools
  run_active_plan_loop "$MAX_ITERATIONS"
}

if [ "$RUN_ALL" -eq 1 ]; then
  run_all_remaining
else
  run_default
fi
