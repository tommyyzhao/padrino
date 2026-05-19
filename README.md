# Padrino

> **Padrino** — a deterministic LLM benchmark and league engine for Mafia-style social deduction games.

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

Padrino runs **fair, reproducible, audit-quality** Mafia games among LLM agents and produces statistically meaningful league rankings. It is backend-only by design: every game is a deterministic, event-sourced, hash-chained log that can be replayed bit-for-bit. Chat is strictly separated from mechanical actions so models can lie and bluff in natural language without ever directly triggering kills, votes, or investigations.

## Status

🚧 **Pre-alpha — v1 in active development via autonomous coding loops.**
See [`ralph/prd.json`](./ralph/prd.json) for the user-story plan and [`ralph/progress.txt`](./ralph/progress.txt) for iteration history.

## v1 scope at a glance

- **Ruleset**: `mini7_v1` — exactly 7 players (2 Mafia, 1 Detective, 1 Doctor, 3 Villagers).
- **Engine**: pure-Python, async, single-process, deterministic. SQLite + asyncio (Postgres is a first-class target via asyncpg). No Celery, no Redis.
- **LLM providers**: any provider supported by [LiteLLM](https://docs.litellm.ai/docs/providers) (Cerebras, DeepInfra, OpenAI, Anthropic, Ollama, …) — see [Deployment options](#deployment-options) below for how to wire one in.
- **Output**: append-only hash-chained event log per game + OpenSkill ratings (global + faction) over clone gauntlets, plus a signed export bundle suitable for federated ingestion.
- **No frontend** in v1, but the REST API and data layer are designed to back a cloud spectator hub.

See [`prd.md`](./prd.md) (vision) and [`ralph/prd.json`](./ralph/prd.json) (executable plan) for the full specification.

## Quickstart

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/tommyyzhao/padrino.git
cd padrino
uv sync --all-extras
cp .env.example .env   # fill in provider keys for real games

# Run quality gates
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest -m "not integration"

# Bring an empty database up to schema-head + seed canonical prompts
uv run padrino bootstrap

# Smoke-test the engine end-to-end (deterministic mock adapter, no API key needed)
uv run padrino demo-gauntlet --seed demo-seed-001 --clones 5
```

### Localhost smoke

`padrino smoke localhost` is the release-gate end-to-end check: it walks a
fresh database through `padrino bootstrap --with-admin-key`, brings up the
API + scheduler as child processes, drives a mini-7 gauntlet through the
deterministic mock adapter, exports + ingests one completed game, and
asserts the documented response shape on the per-league leaderboard plus
the public leaderboard / per-model leaderboard / public events endpoints.
A non-zero exit code prints a structured JSON failure report including the
failing step and the last 50 lines of API + scheduler stderr.

```bash
uv run padrino smoke localhost --db-url sqlite+aiosqlite:///./padrino-smoke.db
```

The full quality-gate matrix plus a SQLite-backed self-host walkthrough lives
in [`docs/deployment/self-host.md`](./docs/deployment/self-host.md).

## Deployment options

| Path                                                                       | Use when                                                                                          |
|----------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------|
| [`docs/deployment/self-host.md`](./docs/deployment/self-host.md)           | You want to run Padrino on a laptop, home-lab box, or single VM via `docker compose up`.          |
| [`docs/deployment/central-backend.md`](./docs/deployment/central-backend.md) | You're hosting the canonical / shared public leaderboard that other Padrino deployments submit into. |
| [`docs/deployment/byo-model.md`](./docs/deployment/byo-model.md)           | You're adding a new LLM provider, recording cassettes, or customizing per-role prompts.            |

Each guide ends with a "Verified runbook" section whose `# verified` bash
blocks are executed end-to-end by `tests/docs/test_runbooks.py` — the docs
cannot silently rot.

## Architecture (v1)

```
              ┌────────────────────────────────────────────────────────┐
              │  FastAPI HTTP layer  (admin, gauntlets, transcripts,   │
              │  /metrics, /ingest, /public/*)                         │
              └─────────────────────────┬──────────────────────────────┘
                                        │
              ┌─────────────────────────┴──────────────────────────────┐
              │              GauntletScheduler / GameRunner            │
              │  asyncio.gather() tick barrier, hard timeout, event    │
              │  hash chain, frozen-response replay                    │
              └─────────────────────────┬──────────────────────────────┘
                                        │
        ┌───────────────────────────────┼───────────────────────────────┐
        │                               │                               │
   ┌────┴────┐                  ┌───────┴────────┐              ┌───────┴───────┐
   │ Core    │                  │ LLM Adapter    │              │ SQLAlchemy 2  │
   │ engine  │                  │ LiteLLM router │              │ aiosqlite /   │
   │ (pure)  │                  │ + secrets seam │              │ asyncpg       │
   │         │                  │                │              │ Alembic       │
   └─────────┘                  └────────────────┘              └───────────────┘
```

`src/padrino/core/` is pure and deterministic: no DB, no network, no wall-clock reads, no `random` module. This is what makes Padrino agent-developable, replayable, and trustworthy as a benchmark substrate.

## License

Apache-2.0. See [LICENSE](./LICENSE).
