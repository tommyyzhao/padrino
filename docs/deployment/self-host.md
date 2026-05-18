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
