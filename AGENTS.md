# AGENTS.md — Padrino project conventions

This file is read by autonomous coding agents (Ralph / Claude Code / Codex / Cursor) before each iteration. Keep it short, factual, and current.

---

## What Padrino is

A **deterministic, event-sourced backend league engine** for benchmarking LLM agents in Mafia-style hidden-information social deduction games. v1 is **backend-only** — no frontend, no browser verification. Every game is replayable bit-for-bit from its archived event log.

The full vision lives in `prd.md`. The executable v1 plan lives in `ralph/prd.json`.

---

## Hard rules

### 1. The core engine is pure

Everything under `src/padrino/core/` MUST be deterministic:

- ❌ No database access
- ❌ No network calls
- ❌ No wall-clock reads (no `datetime.utcnow()`, `time.time()`, etc.)
- ❌ No `random` / `secrets` / any non-seeded RNG
- ❌ No provider SDK imports
- ❌ No logging that affects behavior

Use the seeded `padrino.core.rng.SeededRng` (sha256-based) for any randomness. Wall-clock and IDs come in from outside the core.

### 2. Mechanical actions and chat are separated

The game engine resolves only validated structured `action` fields. Chat text (`public_message`, `private_message`) is NEVER parsed for votes, kills, protects, or investigations. Tests must verify this.

### 3. Ranked observations are identity-blind

In ranked mode, agents NEVER see:

- Model / provider names of any participant
- Agent build IDs
- Ratings or historical performance
- Gauntlet clone index
- Other games' transcripts
- Hidden roles (except their own role; mafia also see their teammates)

### 4. Every event hash-chains the previous

`event_hash = sha256(prev_event_hash + canonical_json(event_without_hash_or_timestamp))`

Canonical JSON: UTF-8, sorted keys, no insignificant whitespace, no floats in core game events (encode as strings if needed). Server timestamps and the hash itself are EXCLUDED from the hash input.

### 5. Rulesets are resolved dynamically by `ruleset_id`

Two rulesets ship today, resolved via `padrino.core.rulesets.get_ruleset(ruleset_id)`:

- `mini7_v1` — 7 players: 2 Mafia, 1 Detective, 1 Doctor, 3 Villagers.
- `bench10_v1` — 10 players: 3 Mafia, 1 Detective, 1 Doctor, 5 Villagers.

Both use `MAX_DAYS = 5`. Add a new ruleset as a module under `core/rulesets/` satisfying the `Ruleset` Protocol, then register it in `get_ruleset`; never hardcode a ruleset in routes, scheduler, tournaments, evaluations, or leaderboards. Every rating is stamped with `ruleset_id` so each variant gets its own leaderboard.

### 6. Backend-only — no browser verification

Ignore the default Ralph "Verify in browser using dev-browser skill" criterion. Padrino has no UI in v1. Quality gates are `ruff` + `mypy` + `pytest`.

---

## Quality gates (must pass before every commit)

```bash
uv run ruff check .                         # lint
uv run ruff format --check .                # format
uv run mypy src tests                       # strict typecheck
uv run pytest -m "not integration"          # unit + integration (no real LLM)
```

CI runs the same on Python 3.11 and 3.12. Don't commit broken code. If a check fails: fix the root cause, re-stage, create a NEW commit (never `--amend` after a failed hook).

---

## Project layout

```
padrino/
├── src/padrino/
│   ├── core/              # PURE deterministic engine (no I/O)
│   │   ├── rulesets/      # ruleset modules (mini7_v1, bench10_v1) + get_ruleset resolver
│   │   ├── engine/        # state, events, reducer, resolver, replay, rng, hashing
│   │   ├── agents/        # contract, sanitizer, response schema (pure validation)
│   │   └── observations.py
│   ├── llm/               # provider adapters (LiteLLM, mock, scripted)
│   ├── ratings/           # openskill service
│   ├── gauntlets/         # scheduler, role assignment, seed derivation
│   ├── db/                # SQLAlchemy 2.x async models, repositories, alembic
│   ├── api/               # FastAPI routes
│   ├── runner/            # GameRunner async orchestration (tick barrier)
│   ├── settings.py        # pydantic-settings .env loader
│   ├── logging.py         # structlog setup
│   └── cli.py             # typer CLI entry point
├── tests/
│   ├── core/              # pure-engine unit tests
│   ├── agents/            # contract / sanitizer tests
│   ├── ratings/           # openskill tests
│   ├── gauntlets/         # scheduler tests
│   ├── integration/       # end-to-end scripted games
│   ├── api/               # FastAPI route tests
│   └── llm/               # adapter tests (real provider marked `@pytest.mark.integration`)
├── ralph/                 # autonomous-loop plan + progress log
└── prd.md                 # human-readable v1 vision
```

---

## Dependencies (v1)

Runtime: `pydantic>=2.7`, `pydantic-settings`, `fastapi`, `uvicorn[standard]`, `sqlalchemy[asyncio]>=2`, `aiosqlite`, `alembic`, `litellm`, `openskill>=6`, `structlog`, `httpx`, `typer`.

Dev: `pytest`, `pytest-asyncio`, `pytest-cov`, `hypothesis`, `mypy`, `ruff`.

**Do not add new runtime dependencies** without justification in the progress log. Prefer stdlib.

---

## LLM provider configuration

v1 supports two providers via LiteLLM, with multi-provider abstractions designed in from day one:

| Tier     | Provider  | Model identifier (LiteLLM)                  | Env var              |
|----------|-----------|---------------------------------------------|----------------------|
| Primary  | Cerebras  | `cerebras/zai-glm-4.7`                      | `CEREBRAS_API_KEY`   |
| Fallback | DeepInfra | `deepinfra/deepseek-ai/DeepSeek-V4-Flash`   | `DEEPINFRA_API_KEY`  |

The exact LiteLLM model strings live in `padrino.settings` defaults and are overridable per `agent_build`. The adapter MUST attempt the primary, and on a non-retryable failure or timeout, fall back to the secondary. Both response bodies are archived to `llm_calls`.

Real-provider tests are marked `@pytest.mark.integration` and skipped by default. CI runs `-m "not integration"`.

---

## Test discipline

1. **Test-first.** Each Ralph user story has clear acceptance criteria; write the failing test before the implementation.
2. **Pure tests use `SeededRng` directly.** No `random`, no `time.time`, no DB.
3. **Engine tests must round-trip through the event log.** Build state by replaying events, never by mutating state directly.
4. **Hypothesis stateful tests** guard invariants: alive-count monotonicity, no double-deaths, no zombie messages, replay-equals-live.
5. **No real LLM calls in default test run.** Use `ScriptedAgent` or `DeterministicMockAdapter` for unit/integration tests. Real provider hits are `@pytest.mark.integration`.

---

## Style

- Python 3.11+ syntax (`X | None`, `list[int]`, `from __future__ import annotations` at the top of every module).
- Pydantic v2 models with `model_config = ConfigDict(frozen=True)` for state/event types.
- Async everywhere on the I/O boundary (DB, HTTP, LLM). Pure core stays sync.
- Module docstrings are required. Inline comments only when WHY is non-obvious — naming carries the WHAT.
- No emojis in source code or commit messages.
- Conventional commits: `feat: US-XXX - title`, `fix: …`, `chore: …`, `test: …`.

---

## When you finish a story

1. All quality gates pass locally.
2. Commit: `feat: US-XXX - <story title>` (no `--no-verify`, no `--amend`).
3. Update `ralph/prd.json` — set `passes: true` on the story you completed.
4. Append a dated entry to `ralph/progress.txt` with the thread URL, file list, and any non-obvious learnings for future iterations.
5. If you discovered a **reusable** pattern, add it to the `## Codebase Patterns` section at the top of `progress.txt`.

If you discover that an acceptance criterion is wrong, fix it in `prd.json` and note it in `progress.txt` — don't silently skip.
