# Central-backend deployment

This guide describes how to host **the canonical, public** Padrino leaderboard
— the shared backend that other Padrino deployments push their game-export
bundles to (via the `POST /ingest/game` endpoint from US-062) and that
spectators read from (via the `/public/*` routes from US-063).

If you only want to run Padrino on your own laptop or on a single host, you
do **not** need this guide. See [`self-host.md`](./self-host.md) instead.

## Threat model and scope

The central backend is a **public ingestion target**. Anyone who points
their submitter key at it can push game bundles, and anyone with network
reach can read the federated leaderboard. The deployment is therefore
designed around the following assumptions:

- Submitters are not fully trusted. Every ingested bundle is replayed and
  hash-chain-verified before it is persisted (US-062). Submitters that
  registered a `submission_public_key` get their bundles checked against
  that key; unsigned bundles still ingest but with
  `verification_status='unverified'`.
- The hash chain on every event row excludes `event_hash`, `prev_event_hash`,
  and `created_at` from the hash input (see AGENTS.md "Hard rule 4"), so the
  same bundle is byte-identical across deployments and tampering surfaces as
  a `hash_chain_mismatch` error.
- Public reads NEVER expose model identity, ratings, gauntlet clone index,
  or transcripts from other concurrent games (AGENTS.md "Hard rule 3"). The
  `/public/*` projection re-applies the privacy filter on every response.
- No submitter can write to another submitter's row. Each ingested game
  carries the originating submitter's api-key id; the federated leaderboard
  rolls up by `entity_id` (a sha256 of display name + provider + model +
  version) so two submitters who happen to run the same agent build are
  ranked as one entity by intent.

## Minimum viable deployment

The smallest production-grade footprint is:

| Component        | Recommendation                                                                  |
|------------------|---------------------------------------------------------------------------------|
| Database         | Managed Postgres 14+ (RDS / Cloud SQL / Supabase). Padrino targets Postgres as a first-class dialect (US-057). |
| Compute          | One container host with TLS termination upstream (Fly.io, Render, GKE, ECS).    |
| Storage          | The DB is the source of truth — the API container is stateless and can be replaced at will. |
| TLS              | Terminate at the load balancer / reverse proxy. The API container speaks HTTP on `0.0.0.0:8000`. |
| DNS              | One A/AAAA record (e.g. `padrino.example.org`). No subdomain layout is required for v1. |

You will run two long-lived processes against the same Postgres database:

- `padrino serve` — the FastAPI app on `:8000`. This is what submitters point
  at and what scrapers read.
- `padrino scheduler` — the async gauntlet drain loop (US-054). Required so
  any game queued via `POST /gauntlets` actually executes; not strictly
  required if the central backend is **read-only** and never schedules its
  own games. For a pure leaderboard host you can skip this process.

### Step 1 — provision Postgres and inject the URL

Set `PADRINO_DB_URL` to the asyncpg-flavored connection string. Padrino's
engine factory (`padrino.db.base.create_engine`) auto-detects the scheme:

```
PADRINO_DB_URL=postgresql+asyncpg://padrino:s3cr3t@db.example.org:5432/padrino
```

Tune the pool ceilings via `PADRINO_DB_POOL_SIZE` / `PADRINO_DB_POOL_MAX_OVERFLOW`
if you expect heavy concurrent ingestion.

### Step 2 — run bootstrap, mint the admin key

`padrino bootstrap` is idempotent (US-058): re-running it after a partial
failure is safe.

```bash
uv run padrino bootstrap --with-admin-key
```

The command emits one JSON document. The `admin_raw_key` field appears
**once** — only its sha256 is stored — so record it in your secrets manager
immediately. Subsequent invocations of `--with-admin-key` mint additional
admin keys; previous keys remain valid until explicitly disabled.

### Step 3 — enable anonymous public reads

The shared leaderboard is intended to be readable by anyone. Flip
`PADRINO_PUBLIC_LEADERBOARD_ANONYMOUS=true` so the `/public/*` routes (US-063)
short-circuit auth with a synthetic spectator context:

```
PADRINO_PUBLIC_LEADERBOARD_ANONYMOUS=true
```

With the flag off (the default), public reads still require a spectator-
or admin-scope api key — appropriate for an "early access" hosted backend
where you want to gate by invitation.

### Step 3.5 — pick the rate-limit store for your replica count

As of US-074, `padrino.api.app.create_app(...)` ships with
`auth_required=True` by default — anonymous submitters are 401'd instead of
silently authenticating as admin. The per-key rate limiter (US-056) now
factors its counter behind a `RateLimitStore`:

| Topology                                  | Auto-selected store           |
|-------------------------------------------|-------------------------------|
| Postgres + `PADRINO_API_WORKERS == 1`     | `InMemoryRateLimitStore`      |
| Postgres + `PADRINO_API_WORKERS > 1`      | `DatabaseRateLimitStore`      |
| Any SQLite deployment                     | `InMemoryRateLimitStore`      |

The `DatabaseRateLimitStore` writes to the `rate_limit_buckets` table
(migration 0012) so multiple uvicorn workers / replicas share a single
ceiling per key. **Set `PADRINO_API_WORKERS` to match your replica
count** (across all containers, not just per-container workers) when you
scale past one — otherwise each replica enforces its own ceiling and the
effective limit silently multiplies.

```
# Two-replica deployment, default uvicorn workers (1) per replica.
PADRINO_API_WORKERS=2
```

The shared bucket eviction runs on every write, so the table stays
bounded to one row per active key per window. If you need to flush
manually, `TRUNCATE rate_limit_buckets` is safe.

### Step 4 — start the API container

The official Padrino image (built from the repo `Dockerfile`, US-064) runs
`padrino` as PID 1; pass the subcommand as the command:

```
docker run -d --name padrino-api \
    -e PADRINO_DB_URL \
    -e PADRINO_PUBLIC_LEADERBOARD_ANONYMOUS=true \
    -p 8000:8000 \
    ghcr.io/tommyyzhao/padrino:latest \
    serve --host 0.0.0.0 --port 8000
```

The image carries a built-in `HEALTHCHECK` against `/healthz`; the same
endpoint is exposed for upstream load balancers. `/metrics` (US-059)
exposes Prometheus exposition format and is unauthenticated by default
(flip `PADRINO_METRICS_REQUIRE_AUTH=true` if your scrapers can carry a
spectator-scope api key).

## Submitter onboarding

1. The submitter generates an Ed25519 keypair locally:

   ```
   uv run python -c "
   from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
   from base64 import urlsafe_b64encode
   k = Ed25519PrivateKey.generate()
   seed = k.private_bytes_raw()
   pub = k.public_key().public_bytes_raw()
   print('PADRINO_EXPORT_PRIVATE_KEY=', urlsafe_b64encode(seed).rstrip(b'=').decode())
   print('public:', urlsafe_b64encode(pub).rstrip(b'=').decode())
   "
   ```

2. The submitter sends the public key string to the operator (out of band).
3. The operator mints a submitter-scope api key against the central backend:

   ```
   curl -X POST https://padrino.example.org/admin/keys \
        -H "Authorization: Bearer <admin-raw-key>" \
        -H "Content-Type: application/json" \
        -d '{"label": "acme-corp", "scopes": ["submitter"],
             "submission_public_key": "<public-key-string>"}'
   ```

   The response carries the raw api key once. Forward it to the submitter.

4. The submitter exports a completed game with `--sign`, then POSTs the
   bundle to `https://padrino.example.org/ingest/game` with `Authorization:
   Bearer <api-key>`. Verified bundles land with
   `verification_status='verified'`; unsigned-but-valid bundles land as
   `'unverified'`.

## Backup and restore

Padrino's source of truth is the Postgres database; the API container is
stateless. Use `pg_dump` for hot backups:

```
pg_dump --format=custom --no-owner --no-acl \
        "$PADRINO_DB_URL_SYNC" > padrino-$(date -u +%Y%m%dT%H%M%S).dump
```

Note the URL transform: `pg_dump` is a sync tool and uses
`postgresql://` (no `+asyncpg`). Keep both URLs in your secret store.

Restore into a fresh database with `pg_restore`:

```
pg_restore --clean --if-exists --no-owner --no-acl \
           --dbname "$PADRINO_DB_URL_SYNC" padrino-20260518T120000.dump
```

Verify restoration with a smoke check (no game state mutated):

```
uv run padrino bootstrap   # idempotent — re-asserts schema and seed rows
curl https://padrino.example.org/healthz
curl https://padrino.example.org/public/leaderboard?ruleset_id=mini7_v1
```

Schedule daily logical backups + weekly physical (or rely on your managed
Postgres provider's continuous backup). The hash chain on the event log
makes per-game tampering detectable but does NOT recover lost rows; the
backup tier is mandatory.

## Verifying the published image

Each `v*` tag is published to GHCR as
`ghcr.io/<owner>/padrino:<version>` and `:latest`, and the image is signed
with [cosign](https://docs.sigstore.dev/cosign/overview/) keyless OIDC via
GitHub's Sigstore-backed identity flow (see
`.github/workflows/release.yml`). There is no long-lived signing key — the
chain of trust is anchored to the workflow's certificate identity, which
operators can pin verbatim.

The expected certificate identity for an upstream release is the workflow
URL plus the tag ref:

```
identity      = https://github.com/<owner>/padrino/.github/workflows/release.yml@refs/tags/<tag>
identity_re   = ^https://github\.com/<owner>/padrino/\.github/workflows/release\.yml@refs/tags/v.*$
oidc_issuer   = https://token.actions.githubusercontent.com
```

Pin the issuer and the identity regex when verifying — that pair is the
cosign "fingerprint" for a keyless OIDC release and is what operators
record alongside the image digest. Replace `<owner>` with the org or user
that owns the GHCR namespace; for the upstream Padrino repo it is
`tommyyzhao`.

Verify a pulled image:

```
cosign verify ghcr.io/<owner>/padrino:<version> \
    --certificate-identity-regexp "^https://github\.com/<owner>/padrino/\.github/workflows/release\.yml@refs/tags/v.*$" \
    --certificate-oidc-issuer "https://token.actions.githubusercontent.com"
```

A successful verify prints the signing certificate's SAN (the workflow
identity) and the transparency-log entry id. Pin those values in your
deployment runbook so a future image with a different identity is rejected.

## Verified runbook

The block below is executed end-to-end by `tests/docs/test_runbooks.py`.
It exercises the same bootstrap path the central-backend operator runs
on day one — the second invocation also asserts that running bootstrap a
second time is a no-op, matching US-058's idempotency contract.

```bash
# verified
uv run padrino bootstrap
uv run padrino bootstrap --with-admin-key
PADRINO_PUBLIC_LEADERBOARD_ANONYMOUS=true uv run python -c "
from padrino.settings import Settings
s = Settings()
assert s.padrino_public_leaderboard_anonymous, 'flag should be honored'
print('public-leaderboard anonymous mode:', s.padrino_public_leaderboard_anonymous)
"
```
