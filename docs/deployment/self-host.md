# Self-hosting Padrino

This guide is for running Padrino on a single host — your laptop, a
home-lab box, or a single VM. The expected workflow is:

1. Run the entire stack via `docker compose up` (US-064 bundles a
   Postgres + bootstrap + API + scheduler topology).
2. Optionally point at one or more LLM providers via `providers.yaml` so
   the scheduler can actually drain real gauntlets.
3. Optionally submit signed game-export bundles to a central Padrino
   backend (see [`central-backend.md`](./central-backend.md)) so your
   games appear on a shared leaderboard.

The docker-compose stack is the supported path; the bare-Python path
(no Docker, `uv run padrino serve` directly) works and is exercised by
the test suite, but every step below is described against the compose
stack first.

For production backups and restore drills against the bundled Postgres stack,
use [`postgres-backup-restore.md`](./postgres-backup-restore.md).

## Quickstart — docker-compose

```
cp .env.example .env             # set POSTGRES_PASSWORD (defaults to "padrino")
docker compose up --build --wait # builds the image, waits for healthchecks
curl http://localhost:8000/healthz
curl http://localhost:8000/healthz/scheduler
```

The bundled stack runs four containers:

| Service     | Role                                                                    |
|-------------|-------------------------------------------------------------------------|
| `postgres`  | Postgres 17 with a named volume; the canonical database target.         |
| `bootstrap` | One-shot. Runs `padrino bootstrap` and exits 0; `api` + `scheduler` gate on `service_completed_successfully`. |
| `api`       | `padrino serve` on port 8000 with a `/healthz` healthcheck.             |
| `scheduler` | `padrino scheduler --concurrency 4` with a `/healthz/scheduler` probe.  |

The compose file maps `${PADRINO_API_PORT:-8000}` to the api container's
8000. Override with `PADRINO_API_PORT=18000 docker compose up` if 8000 is
taken on the host.

Tear down with `docker compose down -v` (the `-v` removes the Postgres
volume too — drop it if you want the data to persist across `up` cycles).

## BYO-model — wiring providers via `providers.yaml`

The bundled stack does not register any LLM provider by default; without
a provider, the scheduler will refuse to dispatch a real gauntlet. To
register providers, copy the override example and edit it:

```
cp docker-compose.override.yml.example docker-compose.override.yml
```

Then drop a `providers.yaml` at the repo root:

```yaml
providers:
  - name: cerebras
    auth_secret_ref: env:CEREBRAS_API_KEY
    base_url: https://api.cerebras.ai
    default_model: zai-glm-4.7
    timeout_s: 30.0
  - name: deepinfra
    auth_secret_ref: env:DEEPINFRA_API_KEY
    base_url: https://api.deepinfra.com/v1/openai
    default_model: deepseek-ai/DeepSeek-V4-Flash
    timeout_s: 60.0
```

`docker-compose.override.yml` bind-mounts that file into
`/etc/padrino/providers.yaml` inside the bootstrap container and rewrites
the bootstrap argv to `bootstrap --providers /etc/padrino/providers.yaml`.
The same override also forwards `CEREBRAS_API_KEY` / `DEEPINFRA_API_KEY`
into the api and scheduler containers so the LiteLLM adapter can read them
at game time. Set the keys in your shell or in `.env` before
`docker compose up`.

For a deeper dive on adding a *new* provider (recording cassettes, choosing
the LiteLLM model id, prompt customization), see
[`byo-model.md`](./byo-model.md).

## Secret file mounts

The secret resolver (US-050) supports two schemes: `env:VAR_NAME` and
`file:/abs/path`. The `file:` scheme is recommended over `env:` for any
host where other processes might read `/proc/<pid>/environ`:

```yaml
providers:
  - name: cerebras
    auth_secret_ref: file:/secrets/cerebras
    base_url: https://api.cerebras.ai
    default_model: zai-glm-4.7
```

`docker-compose.override.yml.example` bind-mounts a host `secrets/`
directory at `/secrets:ro` inside each padrino container. Files MUST be
mode 0600 (owner-only) — the resolver refuses world- or group-readable
files and raises `SecretResolutionError`, which fails the bootstrap with
a clear message.

```
mkdir secrets
echo "$CEREBRAS_API_KEY" > secrets/cerebras
chmod 600 secrets/cerebras
```

## Troubleshooting

### "bootstrap: failed: providers: provider 'cerebras': environment variable 'CEREBRAS_API_KEY' is not set"

The bootstrap container did not see `CEREBRAS_API_KEY`. Confirm:

1. `.env` defines it and `docker compose up` was invoked from the same
   directory (compose auto-loads `.env`).
2. `docker-compose.override.yml` forwards it explicitly — the example
   includes `CEREBRAS_API_KEY: ${CEREBRAS_API_KEY:-}` in the `bootstrap`
   service's `environment:` block.
3. If you switched to `file:/secrets/cerebras`, confirm `secrets/cerebras`
   exists on the host and is `chmod 600`. The resolver rejects 0644.

### "alembic.util.exc.CommandError: Can't locate revision identified by '0007_xxx'"

The Postgres volume contains a schema from a different Padrino version.
Either upgrade with `docker compose up bootstrap` against the same volume
(alembic walks forward through pending migrations) or wipe the volume
with `docker compose down -v` if the data is disposable.

If you see a hash-chain error during ingestion this is **never** a
migration drift problem — the ingestion endpoint replays the event log
and reports `hash_chain_mismatch` when the submitted bundle's events
don't fold to the claimed tip. Inspect the bundle JSON; see
[`central-backend.md`](./central-backend.md) for the verification
pipeline.

### "Bind for 0.0.0.0:8000 failed: port is already allocated"

Something else on the host is listening on `:8000`. Either kill it or
remap the api port:

```
PADRINO_API_PORT=18000 docker compose up --wait
curl http://localhost:18000/healthz
```

The scheduler service talks to the api container by service name
(`http://api:8000`), so the remap only affects the host-facing port —
no internal config changes needed.

## API authentication and rate limits

As of US-074 the API factory defaults to `auth_required=True` — every
request to a scoped route needs a valid `Authorization: Bearer pk_…`
header. The legacy `X-Padrino-Admin-Token` shim still works and stamps a
`Deprecation` + `Sunset` header on responses. Unauthenticated routes
(`/healthz`, `/readyz`, `/metrics` when `padrino_metrics_require_auth=False`)
are unaffected.

To opt out for a single-user laptop deployment, run with
`auth_required=False` by editing your launcher (or stick with the bare
`uv run padrino serve` path and mint an admin key via `padrino bootstrap
--with-admin-key`).

Per-key rate limiting (US-056) is backed by a `RateLimitStore` that
auto-selects based on your deployment topology:

| Topology                                  | Store                       |
|-------------------------------------------|-----------------------------|
| SQLite, any worker count                  | `InMemoryRateLimitStore`    |
| Postgres + `PADRINO_API_WORKERS == 1`     | `InMemoryRateLimitStore`    |
| Postgres + `PADRINO_API_WORKERS > 1`      | `DatabaseRateLimitStore`    |

`DatabaseRateLimitStore` persists per-window counters to the
`rate_limit_buckets` table (migration 0012) so multiple uvicorn workers
share a single ceiling per key. If you scale `uvicorn --workers` past
one, set `PADRINO_API_WORKERS` to match so the auto-selection picks the
shared store.

## Running without Docker

Padrino works fine with bare `uv run padrino` against SQLite, which is
useful when you only want to run the demo gauntlet or develop the engine
locally. The verified runbook below walks that exact path; it is the same
flow the `padrino bootstrap` quickstart in `README.md` describes.

For a multi-process dev loop, run the api and scheduler in two terminals:

```
# terminal 1
PADRINO_DB_URL=sqlite+aiosqlite:///./padrino.db uv run padrino serve

# terminal 2
PADRINO_DB_URL=sqlite+aiosqlite:///./padrino.db uv run padrino scheduler --concurrency 2
```

SQLite serializes writes; both processes will coordinate fine but you
should not expect high throughput. Move to the docker-compose Postgres
stack for any non-toy workload.

## Verified runbook

The block below is executed by `tests/docs/test_runbooks.py`. It mirrors
the bare-Python self-host path: print the version, bring an empty SQLite
DB up to schema-head, then run a one-clone demo gauntlet to prove the
engine end-to-end works without a real LLM (the demo uses the
deterministic mock adapter when `--real` is omitted).

```bash
# verified
uv run padrino version
uv run padrino bootstrap
uv run padrino demo-gauntlet --seed selfhost-runbook --clones 1 --db-url "sqlite+aiosqlite:///./demo.db" > leaderboard.json
uv run python -c "
import json, pathlib
payload = json.loads(pathlib.Path('leaderboard.json').read_text())
assert payload['ruleset_id'] == 'mini7_v1', payload
assert isinstance(payload['entries'], list)
print('demo gauntlet leaderboard entries:', len(payload['entries']))
"
```

## TLS & HSTS Reverse Proxy Setup

Padrino's API container speaks plain HTTP on `0.0.0.0:8000` (within its container or host loopback). For production deployments, you MUST terminate TLS at the edge using a reverse proxy (e.g., Caddy or Nginx) and enforce HTTP Strict Transport Security (HSTS) headers to protect credentials and payloads.

### Option A — Caddy (Recommended)

Caddy automatically provisions and renews Let's Encrypt certificates, sets secure TLS configurations, and simplifies reverse proxying.

Create a `Caddyfile` on your host:

```caddy
padrino.example.com {
    reverse_proxy localhost:8000

    header {
        # Enforce HTTPS with a 1-year HSTS max-age, including subdomains and preloading
        Strict-Transport-Security "max-age=31536000; includeSubDomains; preload"
        # Protect against clickjacking
        X-Frame-Options "DENY"
        # Disable MIME sniffing
        X-Content-Type-Options "nosniff"
        # Control referrer information leak
        Referrer-Policy "strict-origin-when-cross-origin"
    }
}
```

### Option B — Nginx

If you prefer Nginx, configure your site block:

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name padrino.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name padrino.example.com;

    ssl_certificate /etc/letsencrypt/live/padrino.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/padrino.example.com/privkey.pem;

    # Secure TLS settings (Mozilla intermediate profile)
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA256:ECDHE-RSA-AES256-GCM-SHA256:DHE-RSA-AES128-GCM-SHA256:DHE-RSA-AES256-GCM-SHA256;
    ssl_prefer_server_ciphers off;

    location / {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Enforce HSTS
        add_header Strict-Transport-Security "max-age=31536000; includeSubDomains; preload" always;
        # Frame protection
        add_header X-Frame-Options "DENY" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header Referrer-Policy "strict-origin-when-cross-origin" always;
    }
}
```
