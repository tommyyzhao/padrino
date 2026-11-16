# Padrino

> **Padrino** — a deterministic, reproducible benchmark and league engine for LLM agents playing Mafia-style social deduction.

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

Padrino runs fair, replayable Mafia games between LLM agents and turns the results into statistically defensible rankings. Every game is a seeded, event-sourced, hash-chained log that replays bit-for-bit. Models get identity-blind observations, speak in free text, and act only through a validated structured field, so they can lie and bluff without chat ever triggering a kill, vote, or investigation. Ratings are OpenSkill (Plackett-Luce) over seat-rotated "clone" gauntlets. Watchable, narrated spectator games and human-vs-model lobbies are built on top of the same engine, as secondary features.

## Why it is different

- **Determinism and replay.** The engine under `src/padrino/core/` is pure: no clock, no DB, no network, no `random`. All randomness comes from a SHA-256 counter RNG (`core/engine/rng.py`). The same seed and the same agent responses produce the same event log.
- **Hash-chained event log.** `event_hash = sha256(prev_event_hash + canonical_json(event))` (`core/engine/hashing.py`). Any tampering breaks the chain at a known sequence number; exports and ingestion re-verify it.
- **Chat is separate from actions.** Only the validated `action` field is resolved by the engine. `public_message` / `private_message` are never parsed for mechanics.
- **Anonymity.** Ranked observations never contain model or provider names, ratings, build IDs, or other games' transcripts, and are checked at runtime. Spectator surfaces strip hidden roles and identity until the endgame reveal; the guards fail closed.
- **Ratings built for benchmarks.** OpenSkill Plackett-Luce, conservative score `mu - 3*sigma`, per-ruleset leaderboards, seat and role rotation across clone gauntlets, and explicit provisional flags for thin samples.
- **Failures are visible, not hidden.** Timeouts and invalid output are coerced to a safe action (abstain or no-op) and recorded as events, so a flaky model is penalised in its own metrics instead of crashing the game or silently skewing it.

See [docs/design-decisions.md](./docs/design-decisions.md) for the reasoning and the code behind each of these.

## Features

- **Eight rulesets**, resolved by `ruleset_id` (`core/rulesets/`). Six are canonical Town-vs-Mafia variants; two use other scoring contexts.

  | Ruleset | Players | Roles | Scoring |
  |---|---|---|---|
  | `mini7_v1` | 7 | 2 Goon, Detective, Doctor, 3 Villager | team |
  | `bench10_v1` | 10 | 3 Goon, Detective, Doctor, 5 Villager | team |
  | `roleblock10_v1` | 10 | adds a Mafia Roleblocker | team |
  | `visit12_v1` | 12 | adds Tracker and Watcher | team |
  | `ninja13_v1` | 13 | adds Ninja, Tracker, Watcher | team |
  | `deception13_v1` | 13 | Godfather, Roleblocker, Janitor, Goon, 7 Villager | team |
  | `sk12_v1` | 12 | adds a Serial Killer | placement |
  | `jester8_v1` | 8 | adds a Jester | solo-rate |

- **Benchmark pipeline.** Heterogeneous gauntlets (`padrino gauntlet run`), campaigns, a scheduler, OpenSkill ratings, Wilson confidence intervals, provisional flags, and per-model leaderboards.
- **SvelteKit dashboard** (`web/dashboard/`): leaderboard, ladder, model explorer, game list and detail, gauntlet reports, live watch, post-match recap.
- **Live spectating.** Server-Sent Events from `/public/games/{id}/live`, spoiler-safe (hidden roles and private channels never leave the server before the reveal). Optional local text-to-speech narration, loopback-only and off by default.
- **Human lobbies (casual).** Private friend lobbies where humans play with or against models, anonymous by default, kept out of the scientific rating tables.
- **Federated ingestion.** Signed (Ed25519) game-export bundles (`padrino export game --sign`) that a central node verifies by replaying the hash chain (`POST /ingest/game`; accepts `mini7_v1` and `bench10_v1`).
- **Providers.** Any LiteLLM provider, any OpenAI-compatible gateway, and an optional OpenRouter catalog with bring-your-own-key routing.
- **Ops.** Docker Compose stack (Postgres, API, scheduler, public API, dashboard), Prometheus `/metrics`, alert rules, backup/restore runbooks.

## Quickstart

Requires Python 3.11 to 3.13 (CI runs 3.12) and [uv](https://docs.astral.sh/uv/). The dashboard needs Node 22 and pnpm.

```bash
git clone https://github.com/tommyyzhao/padrino.git
cd padrino
uv sync --all-extras
```

### 1. Mock demo (no keys, no network)

```bash
uv run padrino demo-gauntlet --seed demo-seed-001 --clones 5
```

Runs five games through a deterministic mock adapter into `./padrino-demo.db` and prints the leaderboard JSON. The mock agents only abstain, so every game is a draw; this exercises the full pipeline (engine, hash chain, ratings, leaderboard), not model skill. For an end-to-end release check, run `uv run padrino smoke localhost` ([self-host guide](./docs/deployment/self-host.md)).

### 2. Dashboard and API (development)

```bash
# terminal 1: API on 127.0.0.1:8000, reading the demo database
PADRINO_PUBLIC_LEADERBOARD_ANONYMOUS=true \
PADRINO_CORS_ALLOW_ORIGINS=http://localhost:5173 \
  uv run padrino serve --db-url sqlite+aiosqlite:///./padrino-demo.db

# terminal 2: dashboard on http://localhost:5173
pnpm -C web/dashboard install
VITE_PADRINO_API_BASE_URL=http://127.0.0.1:8000 pnpm -C web/dashboard dev
```

Without `PADRINO_PUBLIC_LEADERBOARD_ANONYMOUS=true`, the public read routes answer `401 authentication_required` unless you send an API key. `PADRINO_CORS_ALLOW_ORIGINS` is needed because the browser calls the API cross-origin. `uv run padrino bootstrap --with-admin-key` prints an admin key once if you want the authenticated routes. Settings load from the environment or `.env` (see `.env.example`).

### 3. Real models through an OpenAI-compatible gateway

`padrino showcase` plays one anonymous, unranked `mini7_v1` game and publishes it for live viewing. Point it at any OpenAI-compatible endpoint, for example a local LiteLLM proxy or another gateway:

```bash
export GATEWAY_API_KEY=...        # never paste a key into a command or file
uv run padrino showcase \
  openai/gpt-4o-mini anthropic/claude-3.5-haiku google/gemini-flash-1.5 \
  openai/gpt-4o-mini anthropic/claude-3.5-haiku google/gemini-flash-1.5 openai/gpt-4o-mini \
  --base-url http://127.0.0.1:4000/v1 \
  --key-ref env:GATEWAY_API_KEY \
  --seed my-first-game \
  --language English
```

(Model slugs above are placeholders; use slugs your gateway serves.)

- Exactly **seven** `provider/model` slugs, in seat order; duplicates are allowed.
- `--key-ref` takes `env:VARIABLE` or `file:/absolute/path`, never a raw key. Always pass `--base-url` and `--key-ref`; their defaults point at a specific local gateway.
- `--seed` fixes role assignment and seating so a game is reproducible at the engine level. Model replies are still whatever the models say, so the same seed does not give the same transcript.
- `--language` adds a showcase-only rule that public and private chat be written in that language (default `English`; `none` disables it). Canonical prompts are unchanged.
- **Preflight.** Before creating any game, Padrino probes every distinct slug once, concurrently. If any fails or times out it exits non-zero naming the slug and kind (`timeout` or `error`) and **creates no game**.
- Gateways that rewrite prompts must be disabled for benchmark traffic. Showcase sends a header asking compatible gateways to leave prompts alone, but you are responsible for checking yours.
- `--db-url` must be a local SQLite URL (default `./padrino-showcase.db`). Serve the same file to watch:

```bash
PADRINO_PUBLIC_LEADERBOARD_ANONYMOUS=true PADRINO_CORS_ALLOW_ORIGINS=http://localhost:5173 \
  uv run padrino serve --db-url sqlite+aiosqlite:///./padrino-showcase.db
```

The command prints a watch URL (`http://localhost:5173/watch/<game-id>?tail=1&narrated=1`) as soon as the game is live. For spoken narration set `PADRINO_TTS_PROVIDER` to `say` (macOS), `gcp`, or `elevenlabs` on the API process; the speech endpoint answers only to loopback clients.

### 4. Tests and quality gates

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest -m "not integration"        # no real LLM calls

pnpm -C web/dashboard lint
pnpm -C web/dashboard check
pnpm -C web/dashboard test                # vitest unit tests
pnpm -C web/dashboard test:e2e            # Playwright, boots a real backend
```

CI also enforces at least 85% coverage on `src/padrino/core/`. Tests marked `integration` hit real providers and are skipped by default.

## Architecture

```
  SvelteKit dashboard (web/dashboard)            padrino CLI (serve, showcase, gauntlet, ...)
            │ REST + Server-Sent Events                          │
            ▼                                                    ▼
  ┌─────────────────────────────────────────────────────────────────────────────┐
  │ FastAPI (api/): admin, games, gauntlets, ingest, /public/*, human, lobbies  │
  │ public-surface-only mode omits admin, games, gauntlets, ingest, /metrics    │
  └─────────────────────────────────────────────────────────────────────────────┘
            │                                  │
  ┌─────────▼──────────┐             ┌─────────▼──────────┐
  │ Scheduler          │────────────►│ GameRunner         │◄──── Human lane
  │ gauntlets/, sched. │             │ tick barrier +     │      (runner/human_lane.py)
  └────────────────────┘             │ hard timeout       │
                                     └───┬────────────┬───┘
                      observation        │            │   response: parse, validate,
                 ┌──────────────────┐    │            │   coerce to safe action
                 │ Pure core (core/)│◄───┘            ▼
                 │ rulesets, reducer│        ┌─────────────────────┐
                 │ hash chain, RNG  │        │ LLM adapters (llm/) │── LiteLLM, OpenAI-compatible
                 └────────┬─────────┘        │ mock, human seat    │   gateway, mock
                          │ events           └─────────────────────┘
        ┌─────────────────▼──────────────────┐
        │ SQLAlchemy 2 async (db/), Alembic  │  SQLite or Postgres
        └─────────────────┬──────────────────┘
                          │ read side
        ┌─────────────────▼──────────────────────────────────────────┐
        │ ratings/ OpenSkill · public/ spoiler-safe projection and   │
        │ broadcast · export/ signed bundles · leaderboards/         │
        └────────────────────────────────────────────────────────────┘
```

`src/padrino/core/` has no I/O. Everything impure (database, network, clock, providers) lives outside it and is injected. The package-by-package map and the data flow of one game are in [docs/architecture.md](./docs/architecture.md).

## Documentation

| Document | What it covers |
|---|---|
| [docs/architecture.md](./docs/architecture.md) | Component map, data flow of one game, persistence, public broadcast path, measured stats |
| [docs/design-decisions.md](./docs/design-decisions.md) | Short ADRs for the choices that make the benchmark trustworthy |
| [docs/deployment/self-host.md](./docs/deployment/self-host.md) | Run on a laptop or single VM with `docker compose` or bare `uv run` |
| [docs/deployment/central-backend.md](./docs/deployment/central-backend.md) | Host a shared leaderboard that other deployments submit to |
| [docs/deployment/ingestion-threat-model.md](./docs/deployment/ingestion-threat-model.md) | What ingestion verifies and what it does not |
| [docs/deployment/byo-model.md](./docs/deployment/byo-model.md) | Add providers, record cassettes, customise prompts |
| [docs/deployment/v2-public.md](./docs/deployment/v2-public.md) | Public spectator topology (public-api plus dashboard) |
| [docs/deployment/scheduled-gauntlets.md](./docs/deployment/scheduled-gauntlets.md) | Recurring gauntlets |
| [docs/deployment/postgres-backup-restore.md](./docs/deployment/postgres-backup-restore.md) | Backup and hash-chain-verifying restore |
| [docs/deployment/credential-rotation.md](./docs/deployment/credential-rotation.md) | Key rotation and secret audit |
| [docs/production-readiness.md](./docs/production-readiness.md) | Historical gap analysis (2026-05-28 snapshot) |
| [docs/prd/](./docs/prd) | Original product requirement docs (historical) |
| [CHANGELOG.md](./CHANGELOG.md) | Release notes |

The deployment guides end in a "Verified runbook" section whose `# verified` bash blocks are executed by `tests/docs/test_runbooks.py`, so they cannot silently rot.

## Status

**Alpha, version 0.1.x.** The engine, rulesets, benchmark pipeline, dashboard, and spectator path are implemented and covered by an automated test suite, but APIs, schemas, and ruleset balance can still change before 1.0. See [docs/production-readiness.md](./docs/production-readiness.md) for the known gaps from the last review and [CHANGELOG.md](./CHANGELOG.md) for what changed since 0.1.0.

## License

Apache-2.0. See [LICENSE](./LICENSE). Copyright Tommy Zhao (tommy@surreal.so).
