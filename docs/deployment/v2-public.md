# Padrino v2 Public League — Deployment Runbook

Wave 8 deployment: a **public/private split topology** — a surface-only public
API + static dashboard at the edge, with the full API, scheduler, and Postgres
on an internal-only network.

## Topology: public edge vs private backend (US-119)

The product shape is "website public, everything else private". The compose
file encodes this by construction:

```
                 internet
                    │  (TLS terminated by your reverse-proxy / CDN)
        ┌───────────┴───────────┐
   edge network            edge network
   ┌──────────┐            ┌──────────┐
   │dashboard │            │public-api│   PADRINO_PUBLIC_SURFACE_ONLY=true
   │ :5173    │            │ :8000    │   mounts ONLY /public/* + /healthz
   └──────────┘            └────┬─────┘
                                │ (also on internal)
        ─ ─ ─ ─ ─ ─ ─ ─ ─ internal network ─ ─ ─ ─ ─ ─ ─ ─ ─
   ┌──────────┐   ┌──────────┐   ┌──────────┐
   │ postgres │   │   api    │   │scheduler │   NO published host ports
   │ (no port)│   │(full surf│   │(matchmkr)│   never on the edge network
   └──────────┘   │ no port) │   └──────────┘
                  └──────────┘
```

- **public-api** is the ONLY backend process the internet can reach. Because it
  runs with `PADRINO_PUBLIC_SURFACE_ONLY=true` (US-110), the private routers
  (admin, ingest, games, leagues, gauntlets, scheduled-gauntlets) and `/metrics`
  are not even registered — a misconfigured reverse-proxy cannot leak them.
- **api / scheduler / postgres** publish no host ports and live only on the
  `internal` network. Scrape `/metrics` against the internal `api` from inside
  the network (e.g. a sidecar Prometheus on `internal`).
- **dashboard** is built against the public-api URL (`VITE_PADRINO_API_BASE_URL`,
  default `http://public-api:8000`), so it functions with zero access to private
  routes (US-111).

### Reverse-proxy / CDN expectations

- Terminate TLS at the edge proxy (Caddy / nginx / Cloudflare) and forward to
  `public-api:8000` and `dashboard:5173`.
- Cache aggressively: RECENT analytics responses are `Cache-Control: public,
  max-age=31536000, immutable` — safe to cache at the CDN forever. `/public/recent`
  is `max-age=60, s-maxage=60`. Live SSE (`/public/games/{id}/live`) is
  `no-cache` and must bypass the CDN.
- Do NOT proxy `/admin`, `/ingest`, `/games`, `/leagues`, `/gauntlets`, or
  `/metrics` from the edge — they are not served by public-api anyway, but keep
  the proxy allowlist scoped to `/public/*`, `/healthz`, `/readyz`.

## Quick start (split topology)

```bash
cp .env.example .env          # fill in secrets
docker compose up --build -d  # public-api + dashboard at the edge; rest internal
```

The published host port (`PADRINO_PUBLIC_API_PORT`, default `8000`) belongs to
**public-api**. The full `api` is reachable only as `http://api:8000` from inside
the compose network.

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
| `PADRINO_GUARD_MODEL` | `deepinfra/meta-llama/Llama-Guard-3-8B` | Moderation guard model (gates public chat) |
| `PADRINO_ALERT_WEBHOOK_URL` | _(unset)_ | Slack/Discord webhook for ops alerts (spend cap, dead scheduler, degraded guard); unset = structured-log-only |

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

| Service | Default port | Path | Network |
|---|---|---|---|
| public-api | `8000` (`PADRINO_PUBLIC_API_PORT`) | `/public/*`, `/healthz`, `/readyz` | edge + internal |
| Dashboard | `5173` | `/` (lobby), `/watch/[id]`, `/ladder`, `/models/[id]` | edge |
| api (private) | _(no host port)_ | full surface incl. `/metrics`, admin, ingest | internal only |
| scheduler | _(no host port)_ | matchmaker / gauntlet runner | internal only |
| postgres | _(no host port)_ | — | internal only |

`/metrics` is served only by the private `api` (not by public-api) — scrape it
from inside the internal network.

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
postgres   → canonical DB (Postgres 17)            [internal]
bootstrap  → one-shot migration runner (exits 0)    [internal]
api        → full FastAPI surface (no host port)    [internal]
public-api → surface-only FastAPI (PADRINO_PUBLIC_SURFACE_ONLY=true), port 8000  [edge+internal]
scheduler  → gauntlet scheduler + continuous matchmaker (no host port)  [internal]
dashboard  → SvelteKit consumer web (port 5173)     [edge]
```

## Human-gated steps (not automated by the loop)

1. Domain + TLS provisioning
2. Scaled Postgres (RDS / Cloud SQL) — replace compose postgres with `PADRINO_DB_URL`
3. CDN configuration (Cloudflare / Fastly in front of the API)
4. Legal / ToS review before public launch
5. Billing setup for provider keys
