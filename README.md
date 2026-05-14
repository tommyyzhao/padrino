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
- **Engine**: pure-Python, async, single-process, deterministic. SQLite + asyncio. No Celery, no Redis.
- **LLM providers**: Cerebras `zai-glm-4.7` (primary) + DeepInfra `deepseek-ai/DeepSeek-V4-Flash` (fallback), routed through LiteLLM. Multi-provider abstraction from day one.
- **Output**: append-only hash-chained event log per game + OpenSkill ratings (global + faction) over clone gauntlets.
- **No frontend** in v1, but the REST API and data layer are designed to back a cloud spectator hub later.

See [`prd.md`](./prd.md) (vision) and [`ralph/prd.json`](./ralph/prd.json) (executable plan) for the full specification.

## Quickstart

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/tommyyzhao/padrino.git
cd padrino
uv sync --all-extras
cp .env.example .env   # fill in CEREBRAS_API_KEY for real games

# Run quality gates
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest -m "not integration"
```

### Run the demo gauntlet

A self-contained gauntlet using the deterministic mock adapter — no API keys
needed. Writes a SQLite database to `./padrino-demo.db` and prints the
resulting leaderboard JSON on stdout:

```bash
uv run padrino demo-gauntlet --seed demo-seed-001 --clones 5
```

Pass `--real` to switch to the LiteLLM adapter with Cerebras + DeepInfra
routing (requires `CEREBRAS_API_KEY` and optionally `DEEPINFRA_API_KEY`):

```bash
uv run padrino demo-gauntlet --seed demo-seed-001 --clones 5 --real
```

Expected mock output (truncated):

```json
{
  "entries": [
    {
      "agent_build_id": "…",
      "display_name": "demo-build",
      "games": 35,
      "draws": 35,
      "wins": 0,
      "losses": 0,
      "provisional": true,
      …
    }
  ],
  "ruleset_id": "mini7_v1",
  "rating_model": "openskill_plackett_luce_v1",
  …
}
```

## Architecture (v1)

```
              ┌────────────────────────────────────────────────────────┐
              │  FastAPI HTTP layer  (admin, gauntlets, transcripts)   │
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
   │ engine  │                  │ LiteLLM router │              │ aiosqlite     │
   │ (pure)  │                  │ Cerebras +     │              │ Alembic       │
   │         │                  │ DeepInfra      │              │ migrations    │
   └─────────┘                  └────────────────┘              └───────────────┘
```

`src/padrino/core/` is pure and deterministic: no DB, no network, no wall-clock reads, no `random` module. This is what makes Padrino agent-developable, replayable, and trustworthy as a benchmark substrate.

## License

Apache-2.0. See [LICENSE](./LICENSE).
