# Ralph Agent Instructions — Padrino Wave 4

You are an autonomous coding agent extending **Padrino** (wave 4 —
multi-provider model diversity, building on the wave-3 real-LLM
verification work already shipped). One iteration = one user story. Be
surgical, keep commits clean, and stay green.

## Boot sequence (run every iteration, in order)

1. **Read project context first.** These files are the source of truth — never
   re-derive them:
   - `AGENTS.md` — hard rules (pure-core firewall, chat-vs-action separation,
     ranked observation privacy, hash-chain canonicalization), quality gates,
     project layout. `CLAUDE.md` mirrors this for Claude agents.
   - `prd.md` — original v1 vision (Padrino, formerly "Syndicate Forge"). Useful
     for tiebreaker context only; wave-4 scope lives in `ralph/prd.json`.
   - `ralph/progress.txt` — read the `## Codebase Patterns` section at the top
     before doing anything else. Then skim the most recent iteration entries
     for context. Older wave-1, wave-2 and wave-3 learnings sit under
     `ralph/archive/`.
   - `ralph/prd.json` — the wave-4 plan (5 stories, US-079..US-083). **You
     ONLY work on stories with `passes: false`. Pick the highest-priority
     unfinished story whose `dependencies` are all already `passes: true`.**
2. **Confirm git branch.** Use the `branchName` field from `ralph/prd.json`
   (currently `ralph/padrino-v4`). If you're not on it, check out — creating
   from `main` (where wave-3 work landed after fast-forward merge) if it
   doesn't exist yet. Never work on `main` directly.
3. **Confirm tooling.** Always use `uv run <cmd>`. System Python is 3.14 and
   outside our `requires-python` range — bare `python` will fail.

## What's already locked in — DO NOT relitigate

- **Ruleset:** `mini7_v1` exactly. 7 players: 2 Mafia, 1 Detective, 1 Doctor,
  3 Villagers. `MAX_DAYS=5`. (Not 15 players. The user already downsized.)
- **Stack:** SQLAlchemy 2.x async ORM over SQLite (aiosqlite) AND Postgres
  (asyncpg) — Postgres is a first-class target since wave 2 (US-057). No Celery /
  Redis; the async scheduler (wave-2 US-054) is in-process.
- **LLM providers** via LiteLLM. Cerebras (`cerebras/zai-glm-4.7`,
  env `CEREBRAS_API_KEY`) primary and DeepInfra (`deepinfra/deepseek-ai/DeepSeek-V4-Flash`,
  env `DEEPINFRA_API_KEY`) fallback. OpenAI, Anthropic, and Ollama have
  recorded cassettes (wave-2 US-051; wave-3 US-072 re-records them against
  real provider responses). Keys resolve through the `padrino.llm.secrets`
  seam — `env:VAR` and `file:/path` schemes only. Never commit secrets;
  `.env` stays gitignored. Chat-tuned models often wrap JSON in ```json
  ... ``` fences; `padrino.llm.litellm_adapter._strip_code_fence` normalizes
  this in the impure adapter layer (do NOT loosen pure-core parser).
- **Backend lives under `src/padrino/`. Frontend lives under `web/`.** The
  pure-core firewall applies to `src/padrino/core/**` only; it never blocks
  a SvelteKit / TypeScript project under `web/dashboard/`. Story US-077
  explicitly extends the dashboard with a new `/gauntlets/[id]/report` route;
  when on that story, run `pnpm -C web/dashboard <task>` for the frontend
  gates in addition to the four backend gates below. For backend-only
  stories, ignore browser verification.
- **Real-provider integration tests** (`@pytest.mark.integration`) cost real
  money. The default `uv run pytest` deselects them. Wave-4 stories US-079,
  US-080, US-082, US-083 all add or extend integration tests. Before claiming
  a story complete, run the relevant integration test once locally with the
  required provider keys set and confirm the cost cap is met. Every
  real-LLM integration test MUST use the `_PARSED_OK_STATUSES = {"ok",
  "fallback_ok"}` constant when computing parse-rate — counting only `"ok"`
  treats a healthy fallback path as a failure (the bug fixed by commit
  `884076c` post-Wave-3). When US-079's `same_model_fallback_ok` status
  ships, it joins that set too.
- **Pure-core rule:** nothing under `src/padrino/core/**` may import from
  `padrino.db`, `padrino.llm`, `padrino.api`, `padrino.runner`, `sqlalchemy`,
  `litellm`, `httpx`, `random`, `secrets`, or call wall-clock. Use the
  seeded `SeededRng` (US-004) for randomness. Inject impure deps via
  constructor.
- **Hash chain** spec excludes `event_hash`, `prev_event_hash`, AND
  `created_at` from the hash input. See AGENTS.md.
- **Provider HTTP endpoint** is stored on `ModelProvider.base_url`
  (preexisting column). Wave-4 US-082 chose to reuse `base_url` rather
  than add a separate `api_base` column, and `LiteLlmAdapter` accepts
  it as the `api_base` kwarg at construction time. When US-080 etc. say
  "ModelProvider api_base=..." in their AC text, they mean **set
  `ModelProvider.base_url`** — do NOT introduce a parallel column.
  Per-model dispatch identifier overrides live on
  `ModelConfig.litellm_model_id` (US-082, migration 0013).

## Implementation loop (one story per iteration)

1. Pick the highest-priority story where `passes: false`.
2. Implement it test-first: failing test → implement → green → refactor.
3. **Run all four quality gates** (must all be green):
   ```
   uv run ruff check .
   uv run ruff format --check .
   uv run mypy src tests
   uv run pytest -m "not integration"
   ```
   Integration tests (`@pytest.mark.integration`) are skipped by default — CI
   never hits real providers. If you add such a test, it must be gated.
4. If anything is red, **fix the root cause**. Do not bypass with
   `# type: ignore`, `--no-verify`, `--amend`, or by deleting the failing test.
5. Commit with the exact format:
   ```
   feat: US-XXX - <story title verbatim from prd.json>
   ```
   Use a single, focused commit per story. No `--no-verify`, no `--amend`.
   If a hook fails, fix the underlying issue and make a NEW commit.
6. In `ralph/prd.json`, set this story's `passes: true`. Don't touch any
   other story's fields.
7. Append an entry to `ralph/progress.txt` using the existing format:
   ```
   ## YYYY-MM-DDTHH:MM - US-XXX
   - What was implemented (1-3 bullets)
   - Files changed (paths only)
   - **Learnings for future iterations:**
     - Patterns / gotchas / useful context for the next iteration
   ---
   ```
   If you discovered a **general, reusable** pattern, also add a short bullet
   to the `## Codebase Patterns` section at the top of `progress.txt`. Keep
   story-specific noise out of that section.
8. Commit any `prd.json` / `progress.txt` updates together with (or
   immediately after) the implementation commit — these tracking files must
   not lag behind the code.

## Hard rules — DO NOT violate

- No `random` / `secrets` / `datetime.utcnow()` / `time.time()` anywhere under
  `src/padrino/core/`. Period.
- Mafia private chat is **never** parsed for game mechanics. Only the
  structured `action` field drives state transitions.
- Ranked observations exposed to LLMs never include model identity, ratings,
  gauntlet clone index, or transcripts from other concurrent games.
- Don't jump dependency order. If story N depends on N-1 and N-1 is still
  `passes: false`, work on N-1 first.
- Don't create `*.md` documentation files unless a story explicitly requires
  one.
- Don't push to remote, don't open PRs, don't tag releases — those are
  user-driven actions.

## Watch-outs

- **Mypy "unused override" notes** for `litellm.*` and `openskill.*` in
  `pyproject.toml` are expected at the current commit and activate once
  US-035 / US-038 import those packages. Don't strip them.
- **LiteLLM model ID** `cerebras/zai-glm-4.7` is the user's reported string,
  not independently verified. If US-035's integration test fails on model
  resolution, try variants (`cerebras/zai/glm-4.7`, etc.) — adjust in the
  adapter, not in core.
- **`uv run` does NOT auto-load `.env`.** Tests that need API keys should
  either be `@pytest.mark.integration` (skipped by default) or use
  `python-dotenv` explicitly inside the test fixture. Never bake keys into
  source.
- **Conventional commits exactly:** `feat: US-XXX - <title>`. The space-dash-
  space separator is intentional. Don't reformat.

## Stop condition

After committing your story and updating `prd.json` / `progress.txt`,
**you MUST run this exact command and quote its output in your reply**:

```
jq '[.userStories[] | select(.passes==false)] | length' ralph/prd.json
```

Then:

- If the command printed `0` (and only then): end your reply with exactly
  `<promise>COMPLETE</promise>` on its own line.
- If the command printed any non-zero number (`1`, `2`, `3`, ...): end
  normally — the next iteration will pick up the next story. **Do NOT
  emit `<promise>COMPLETE</promise>` while any story is `passes: false`,
  no matter how confident you are about the story you just shipped.**

This guardrail exists because earlier iterations have emitted COMPLETE
after a single story while several `passes: false` stories remained —
that exits the harness prematurely and the wave doesn't finish.

## If you get blocked

- `ralph/progress.txt` learnings section usually has the answer from a prior
  iteration. Read it.
- `prd.md` is the tiebreaker for ambiguity within a story.
- `AGENTS.md` is the tiebreaker for project-wide conventions.
- If both are silent or contradictory, edit `ralph/prd.json` to clarify the
  story's acceptance criteria, note the change in `progress.txt`, then
  proceed.

Work one story. Commit once. Keep CI green. Go.
