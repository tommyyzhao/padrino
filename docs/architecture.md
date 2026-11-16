# Architecture

How Padrino is put together, with file references you can check. For the reasoning behind the main choices see [design-decisions.md](./design-decisions.md).

The one rule that shapes everything: **`src/padrino/core/` is a pure, deterministic engine.** It has no database, network, clock, or `random`; `tests/core/test_purity.py` walks every module under `core/` with `ast` and fails on forbidden imports. Impure packages (runner, llm, db, api) call into it and inject whatever it needs.

## Component map

### Python package, `src/padrino/`

| Package or module | Role | Path |
|---|---|---|
| `core/` | Pure engine: state, events, reducer, win conditions, RNG, hashing, observations, spectator projection, rating-context rules, human-lobby policy | `src/padrino/core/` |
| `core/engine/` | Event models, hash-chained `EventLog`, `apply_event` reducer, resolvers, replay, legal actions, role assignment, `SeededRng`, canonical JSON | `src/padrino/core/engine/` |
| `core/rulesets/` | One module per ruleset, the `get_ruleset` resolver, and `GameRulesSnapshot` for casual custom decks | `src/padrino/core/rulesets/` |
| `core/agents/` | Response contract (`AgentResponse`), tolerant JSON extraction, text sanitizer, safe-action coercion | `src/padrino/core/agents/` |
| `runner/` | Async `GameRunner`, tick barrier with hard timeout, benchmark scheduler, human lane, takeover, durability | `src/padrino/runner/` |
| `llm/` | Provider adapters (LiteLLM, mock, human), per-seat multiplexer, retry, secret resolution, canonical prompts | `src/padrino/llm/` |
| `gauntlets/` | Gauntlet creation, per-game seed derivation, seat-rotated tournaments, completion and provisional flags, campaign reports | `src/padrino/gauntlets/` |
| `scheduler/` | In-process recurring jobs: gauntlet job, campaign tick, continuous matchmaking | `src/padrino/scheduler/` |
| `matchmaking/` | Curated-roster matchmaker | `src/padrino/matchmaking/` |
| `ratings/` | OpenSkill services (team, placement, solo-rate, human), provisional/decay, model rollup, public leaderboard | `src/padrino/ratings/` |
| `leaderboards/` | Per-league leaderboard aggregation | `src/padrino/leaderboards/` |
| `db/` | SQLAlchemy 2 async models, repositories, Alembic migrations, retention | `src/padrino/db/` |
| `api/` | FastAPI app factory, auth and scopes, rate limiting, human auth and consent | `src/padrino/api/` |
| `api/routes/` | Route modules: admin, admin_anticheat, admin_keys, games, gauntlets, health, human, ingest, leagues, legal, lobbies, model_catalog, public, scheduled_gauntlets, speech, sprites | `src/padrino/api/routes/` |
| `public/` | Spoiler-safe projection (`to_public_event_v1`), broadcast cadence, live-tail SSE, broadcast index, moderation gate | `src/padrino/public/` |
| `export/` | Canonical game-export bundle with optional Ed25519 signature | `src/padrino/export/bundle.py` |
| `openrouter/` | OpenRouter catalog snapshots, bring-your-own-key routing, funding preflight | `src/padrino/openrouter/` |
| `economics/` | Integer-micro-USD ledger, reservations, spend governor | `src/padrino/economics/` |
| `analytics/` | Deterministic game analytics and sampled LLM-judge enrichment | `src/padrino/analytics/` |
| `observability/` | Prometheus metrics, alert notifier, privacy audit | `src/padrino/observability/` |
| `ops/`, `diagnostics/` | Backup/restore verification, read-side diagnostics | `src/padrino/ops/`, `src/padrino/diagnostics/` |
| `legal/`, `assets/` | Versioned legal documents; bundled sprite library | `src/padrino/legal/`, `src/padrino/assets/` |
| `cli.py` | Typer entry point (`padrino = "padrino.cli:app"`, `pyproject.toml`) | `src/padrino/cli.py` |
| `showcase.py` | One anonymous, unranked game through an OpenAI-compatible gateway, with preflight | `src/padrino/showcase.py` |
| `bootstrap.py`, `demo_gauntlet.py`, `smoke.py` | Fresh-database bootstrap, mock demo gauntlet, localhost end-to-end smoke | `src/padrino/` |
| `settings.py`, `clock.py`, `logging.py` | pydantic-settings loader, shared UTC/JSON helpers for shell code, structlog setup | `src/padrino/` |

CLI commands (`uv run padrino --help`): `version`, `serve`, `metrics`, `ranked-audit`, `showcase`, `demo-gauntlet`, `scheduler`, `human-lane`, `bootstrap`, `catalog`, `gauntlet`, `campaign`, `export`, `game`, `smoke`.

### Dashboard, `web/dashboard/`

SvelteKit with the static adapter. It contains no server routes; all server logic is in FastAPI. API base URL comes from `VITE_PADRINO_API_BASE_URL` (`web/dashboard/src/lib/api/client.ts`, default `http://localhost:8000`).

| Route | Purpose |
|---|---|
| `/` | Home: key numbers and entry points |
| `/leaderboard`, `/ladder`, `/models`, `/models/[id]` | Benchmark leaderboards and model explorer |
| `/games`, `/games/[id]` | Game list and one-shot game detail |
| `/watch/[id]`, `/watch/[id]/recap` | Live spectator view over Server-Sent Events; post-match recap |
| `/gauntlets/[id]/report` | Gauntlet report |
| `/lobby`, `/lobby/[id]` | Private friend lobbies (casual) |
| `/play/match`, `/play/[gameId]`, `/play/[gameId]/reveal`, `/play/ranked-consent` | Human play, endgame reveal, consent |
| `/how-to-play`, `/privacy`, `/terms`, `/profile`, `/laboratory` | Onboarding, legal, profile, design-system page |

Source: `find web/dashboard/src/routes -name '+page.svelte'`.

### Ops

- `docker-compose.yml` services: `postgres`, `bootstrap`, `api`, `public-api`, `scheduler`, `human-lane`, `dashboard`, on `edge` and `internal` networks. See [deployment/v2-public.md](./deployment/v2-public.md).
- `.github/workflows/ci.yml`: ruff, mypy, pytest on `ubuntu-latest` and `macos-latest` with Python 3.12, a core coverage gate (`--fail-under=85` on `src/padrino/core/*`), and dashboard lint/check/e2e.
- `public-surface-only` mode (`padrino_public_surface_only`, `src/padrino/api/app.py`) does not register the private routers or `/metrics` at all, so a proxy mistake cannot expose them.

## Data flow of one game

The driver is `drive_game_loop` / `run_game` in `src/padrino/runner/game_runner.py`. Per phase:

1. **Observation.** For each eligible seat, `build_observation` (`core/observations.py`) assembles what that seat may know: its own role (mafia also see teammates), public events, its private events, its memory. In ranked mode `assert_ranked_observation_safe` (`core/observation_privacy.py`) runs on every observation before dispatch and aborts on a leak.
2. **Adapter.** `run_tick` (`runner/tick.py`) sends all observations concurrently through the `LlmAdapter` protocol (`llm/adapter.py`) under one `asyncio.wait_for` hard timeout, and returns only when every seat has answered or timed out. Adapters are the LiteLLM adapter, a deterministic mock, or a human seat; a `SeatMultiplexAdapter` can rebind a seat between ticks.
3. **Parse and coerce.** The adapter calls `parse_agent_response` (`core/agents/contract.py`), which tolerates think blocks, code fences and prose around the JSON and never raises. Then the runner checks the action against `legal_actions_for`. Timeout, invalid JSON, schema violation, provider exhaustion, or an illegal action becomes a coerced safe action (`core/agents/coercion.py`: abstain on day vote, no-op elsewhere) plus a visible `ActionTimedOut` or `OutputInvalid` event (`runner/tick.py`, `_build_failure_outcome`).
4. **Events.** `_submission_events_for` turns each response into events. Messages become `PublicMessageSubmitted` / `PrivateMessageSubmitted`; only the structured `action` becomes vote, kill, protect, investigate and similar events. Chat text is never read for mechanics.
5. **Reducer.** `_emit` appends each event body to the `EventLog` and folds it through `apply_event` (`core/engine/reducer.py`). Resolvers compute day-vote and night outcomes; `check_win` decides termination.
6. **Hash chain.** `EventLog.append` seals every event: `event_hash = sha256(prev_event_hash + canonical_json(body))`, excluding `event_hash`, `prev_event_hash` and `created_at` (`core/engine/hashing.py`). The runner persists each event row and one `llm_calls` row per call (raw response, parsed response, tokens, cost, status) through `GamePersistence`.
7. **Ratings.** On `GameTerminated`, in the same transaction as the terminal row, the runner applies ratings if the game is ranked and bound to a league (`_should_apply_ratings`, `_persist_terminated_event`). `update_ratings_for_game` (`ratings/openskill_service.py`) runs OpenSkill Plackett-Luce at team level; placement and solo-rate rulesets use their own services. Unranked games (showcase, custom lobbies) and human-lane games write no scientific rating rows.

Replay runs the same chain without any model: `replay_event_log` (`core/engine/replay.py`) re-seals each stored body and raises `ReplayHashMismatchError` at the first sequence whose hash differs. `verify_chain` in `export/bundle.py` and `padrino game verify-restore` use it.

## Persistence

- Async SQLAlchemy 2 models in `src/padrino/db/models.py` (67 tables), Alembic migrations in `src/padrino/db/migrations/versions/` (75).
- SQLite (`sqlite+aiosqlite`) is the default for local use; Postgres (`postgresql+asyncpg`) for shared deployments (`db/base.py:create_engine`). Postgres-specific behaviour is limited to row locking with `SKIP LOCKED` in the claim queries (for example `db/repositories/gauntlets.py`) and pool sizing.
- Core tables: `games`, `game_events` (the hash-chained log), `game_seats`, `llm_calls`, `ratings` / `rating_events` (scientific), `gauntlets`, `campaigns`, `ingested_games`. Human-lane data lives in sibling tables (`human_rating`, `human_chat_messages`, `lobbies`, ...); human free text is stored out of band and the event carries only a content reference.
- No Redis and no queue: the database is the work queue (`claim_oldest_pending`), with one process per role (API, scheduler, human lane).

## Public and broadcast path

1. A finished or running game is marked broadcastable and given a broadcast state `HIDDEN`, `LIVE` or `RECENT` (`public/broadcast_index.py`). Scheduler-driven games pass the moderation gate first (`public/moderation.py:is_broadcastable`, fail-closed). The local showcase skips the gate and says so in code.
2. Public reads go through `/public/*` (`api/routes/public.py`). Anonymous access is off unless `PADRINO_PUBLIC_LEADERBOARD_ANONYMOUS=true`, and is then rate limited per client.
3. Non-terminal games are rendered only via `project_events_for_spectator` (`core/spectator_projection.py`): PRIVATE and SYSTEM events are dropped whole and forbidden keys (including `role` and `faction`) are stripped from PUBLIC payloads. `GET /games/{id}/events?visibility=public` applies the same rule (`api/routes/games.py:272`).
4. `GET /public/games/{id}/live` streams `public_event_v1` frames (`public/projection.py`) as Server-Sent Events. With `?tail=true` it keeps polling the growing log (`public/live_tail.py`) with heartbeats; otherwise `plan_broadcast` (`public/broadcaster.py`) paces frames by cadence. Per-IP concurrent streams are capped (`padrino_sse_max_connections_per_ip`).
5. The dashboard's `/watch/[id]` consumes that stream with `EventSource`. Narrated mode (`?tail=1&narrated=1`) drives pacing client-side and, if `PADRINO_TTS_PROVIDER` is set, fetches audio from `/public/games/{id}/speech/{sequence}`, which answers only to loopback clients (`api/routes/speech.py:276`).

## Measured stats

All measured on this working tree on 2026-11-15, base commit `dff9f80`. Re-run the command to refresh.

| Stat | Value | Command |
|---|---|---|
| Python source lines (`src/padrino`) | 73,225 | `git ls-files 'src/padrino/*.py' \| xargs cat \| wc -l` |
| Python test lines | 98,525 | `git ls-files 'tests/*.py' \| xargs cat \| wc -l` |
| Python test files | 312 | `git ls-files 'tests/*test_*.py' \| wc -l` |
| Tests collected (default run) | 3,498 selected, 13 deselected | `uv run pytest --collect-only -q -m "not integration" \| tail -1` |
| Alembic migrations | 75 | `git ls-files 'src/padrino/db/migrations/versions/0*.py' \| wc -l` |
| ORM tables | 67 | `grep -c '__tablename__' src/padrino/db/models.py` |
| HTTP routes in the app | 113 (includes 4 FastAPI doc routes: `/openapi.json`, `/docs`, `/docs/oauth2-redirect`, `/redoc`); 0 WebSocket | `uv run python -I -c "import padrino.api.app as m; a=m.create_app(); print(sum(1 for r in a.routes if getattr(r,'methods',None)))"` |
| Route modules | 16 | `git ls-files 'src/padrino/api/routes/*.py' \| grep -v __init__ \| wc -l` |
| Registered rulesets | 8 | `uv run python -I -c "from padrino.core.rulesets import BUILTIN_RULESET_IDS as r; print(len(r))"` |
| Dashboard TS/Svelte lines | 10,722 | `git ls-files 'web/dashboard/src/*.svelte' 'web/dashboard/src/*.ts' \| xargs cat \| wc -l` |
| Dashboard components / page routes | 38 `.svelte` files / 21 `+page.svelte` | `git ls-files 'web/dashboard/src/*.svelte' \| wc -l`; `git ls-files 'web/dashboard/src/routes/*+page.svelte' \| wc -l` |
| Playwright spec files | 14 | `git ls-files 'web/dashboard/tests/e2e/*.spec.ts' \| wc -l` |

The test count includes tests that need only SQLite and mocks. Tests marked `integration` (real providers), `live_llm`, `postgres` and `docker` are opt-in (`pyproject.toml`, `[tool.pytest.ini_options]`).
