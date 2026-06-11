# Padrino v2 Public League — Deployment Runbook

Wave 7 deployment: broadcaster API + consumer web + continuous scheduler.

## Quick start

```bash
cp .env.example .env          # fill in secrets
docker compose up --build -d
```

## Required secrets

| Variable | Description |
|---|---|
| `POSTGRES_PASSWORD` | Postgres superuser password (default: `padrino`) |
| `DEEPINFRA_API_KEY` | DeepInfra key — used for the moderation guard model (`Llama-Guard-3-8B`) |
| `CEREBRAS_API_KEY` | Cerebras key — primary LLM (`cerebras/zai-glm-4.7`) |
| `DEEPINFRA_API_KEY` | DeepInfra key — fallback LLM (`deepinfra/deepseek-ai/DeepSeek-V4-Flash`) |

## Cost / safety rails

| Variable | Default | Notes |
|---|---|---|
| `PADRINO_GLOBAL_SPEND_CAP_USD` | `200.0` | Hard cumulative ceiling; game admission denied at this threshold |
| `PADRINO_MAX_GAMES_PER_DAY` | `20` | Daily game cap (independent of spend) |
| `PADRINO_MAX_CONCURRENT_GAMES` | `3` | Concurrent game cap |
| `PADRINO_ENABLE_CONTINUOUS_MATCHMAKING` | `false` | Set `true` to start the compounding game bank; defaults off for safety |

## Broadcast cadence (ms between SSE frames)

| Variable | Default | Description |
|---|---|---|
| `PADRINO_BROADCAST_CADENCE_CHAT_MS` | `2500` | Chat-line frames |
| `PADRINO_BROADCAST_CADENCE_PHASE_MS` | `3000` | Phase-change frames |
| `PADRINO_BROADCAST_CADENCE_ELIMINATION_MS` | `4000` | Elimination frames |
| `PADRINO_BROADCAST_CADENCE_RESOLUTION_MS` | `3500` | Resolution frames |
| `PADRINO_BROADCAST_CADENCE_DEFAULT_MS` | `1500` | All other event types |

## Retention / archival

| Variable | Default | Description |
|---|---|---|
| `PADRINO_RAW_PAYLOAD_TTL_DAYS` | `30` | Scrub `llm_call` heavy columns (`request_json`, `raw_response`) after N days for all completed games |
| `PADRINO_NON_BROADCASTABLE_GAME_TTL_DAYS` | `7` | Hard-delete non-broadcastable game rows (+ cascades) after N days; ratings and broadcastable replay data are kept indefinitely |

## Public surface URLs

| Service | Default port | Path |
|---|---|---|
| API | `8000` | `/public/*`, `/healthz`, `/metrics` |
| Dashboard | `5173` | `/` (lobby), `/watch/[id]`, `/ladder`, `/models/[id]` |

## Judge enrichment (optional)

The behavioral judge runs offline via the sampled batch job:

| Variable | Default | Description |
|---|---|---|
| `PADRINO_ENABLE_BEHAVIORAL_EVALUATION` | `false` | Enable judge enrichment runs |
| `PADRINO_BEHAVIORAL_JUDGE_MODEL` | `xiaomi/mimo-v2.5-pro` | LLM judge model |
| `PADRINO_JUDGE_SAMPLE_RATE` | `0.1` | Fraction of completed games evaluated per batch |
| `PADRINO_JUDGE_MAX_GAMES_PER_RUN` | `5` | Hard per-run cap (controls per-run cost) |

## CDN / caching notes

RECENT game analytics responses carry `Cache-Control: public, max-age=31536000, immutable`.
These are safe to cache at CDN/edge indefinitely — analytics never change once a game is RECENT.

The `/public/recent` list carries `Cache-Control: public, max-age=60, s-maxage=60` (1-minute CDN TTL).

Live SSE streams (`/public/games/{id}/live`) carry `Cache-Control: no-cache` and cannot be CDN-cached.

## Services

```
postgres   → canonical DB (Postgres 17)
bootstrap  → one-shot migration runner (exits 0 on success)
api        → FastAPI server (port 8000)
scheduler  → in-process gauntlet scheduler + continuous matchmaker
dashboard  → SvelteKit consumer web (port 5173)
```

## Human-gated steps (not automated by the loop)

1. Domain + TLS provisioning
2. Scaled Postgres (RDS / Cloud SQL) — replace compose postgres with `PADRINO_DB_URL`
3. CDN configuration (Cloudflare / Fastly in front of the API)
4. Legal / ToS review before public launch
5. Billing setup for provider keys
