# Single-host Postgres backup and restore

This runbook covers the production `docker-compose.yml` stack on one host:
`postgres`, `bootstrap`, `api`, `public-api`, `scheduler`, `human-lane`, and
`dashboard`. It is intentionally single-host only. It does not cover HA,
streaming replication, PITR orchestration, distributed workers, or Redis.

Padrino's source of truth is Postgres. Containers are replaceable; the
`padrino-postgres-data` volume and its backups are not. Every game event row is
hash-chained, so a restored completed game can be checked after recovery:

```
event_hash = sha256(prev_event_hash + canonical_json(event_without_hash_or_timestamp))
```

The in-tree verifier for this runbook is:

```
padrino game verify-restore <game-id> --db-url <restored-db-url>
```

It reads restored `games` and `game_events` rows, replays the chain, and checks
the recomputed tip against `games.event_hash_head`.

## Routine logical dump

Run backups from the repo root on the compose host. The bundled Postgres
service does not publish a host port, so use `docker compose exec` and stream
the custom-format dump to the host filesystem.

```bash
mkdir -p backups
BACKUP_FILE="backups/padrino-$(date -u +%Y%m%dT%H%M%SZ).dump"

docker compose exec -T postgres \
  pg_dump -U padrino -d padrino \
    --format=custom \
    --no-owner \
    --no-acl \
  > "$BACKUP_FILE"

sha256sum "$BACKUP_FILE" > "$BACKUP_FILE.sha256"
```

Store the `.dump` and `.sha256` outside the host, encrypted at rest. Keep at
least one recent dump plus the last known-good restore drill artifact.

Before a planned maintenance restore, record one completed game to verify
after restore:

```bash
docker compose exec -T postgres \
  psql -U padrino -d padrino -Atc \
  "SELECT id || ' ' || event_hash_head
   FROM games
   WHERE status = 'COMPLETED' AND event_hash_head IS NOT NULL
   ORDER BY completed_at DESC NULLS LAST, created_at DESC
   LIMIT 1;"
```

Save both values with the backup ticket. The post-restore verifier prints a
`tip_hash`; it must equal the saved `event_hash_head`.

## Non-destructive restore drill

Use a scratch database name so the live `padrino` database is untouched.

```bash
BACKUP_FILE="backups/padrino-20260623T120000Z.dump"
RESTORE_DB="padrino_restore_drill"
VERIFY_GAME_ID="<completed-game-id-from-before-the-dump>"

docker compose up -d postgres
docker compose exec -T postgres dropdb --if-exists -U padrino "$RESTORE_DB"
docker compose exec -T postgres createdb -U padrino "$RESTORE_DB"
docker compose exec -T postgres \
  pg_restore -U padrino -d "$RESTORE_DB" \
    --clean \
    --if-exists \
    --no-owner \
    --no-acl \
  < "$BACKUP_FILE"

docker compose run --rm --no-deps \
  -e PADRINO_DB_URL="postgresql+asyncpg://padrino:${POSTGRES_PASSWORD:-padrino}@postgres:5432/${RESTORE_DB}" \
  api game verify-restore "$VERIFY_GAME_ID"
```

The verifier exits non-zero on a missing game, non-completed game, missing
events, broken hash chain, or a tip mismatch with `games.event_hash_head`.
Compare the printed `tip_hash` to the value recorded before the dump.

Clean up the drill database after recording the result:

```bash
docker compose exec -T postgres dropdb --if-exists -U padrino "$RESTORE_DB"
```

## Production restore

Stop all Padrino writers before replacing the production database. The
`postgres` service stays up so `pg_restore` can run.

```bash
BACKUP_FILE="backups/padrino-20260623T120000Z.dump"
VERIFY_GAME_ID="<completed-game-id-from-before-the-dump>"

docker compose stop api public-api scheduler human-lane dashboard
docker compose up -d postgres

docker compose exec -T postgres dropdb --if-exists -U padrino padrino
docker compose exec -T postgres createdb -U padrino padrino
docker compose exec -T postgres \
  pg_restore -U padrino -d padrino \
    --clean \
    --if-exists \
    --no-owner \
    --no-acl \
  < "$BACKUP_FILE"

docker compose run --rm --no-deps \
  -e PADRINO_DB_URL="postgresql+asyncpg://padrino:${POSTGRES_PASSWORD:-padrino}@postgres:5432/padrino" \
  api game verify-restore "$VERIFY_GAME_ID"

docker compose up -d bootstrap api public-api scheduler human-lane dashboard
docker compose ps
```

Do not bring `scheduler` or `human-lane` back until the verifier succeeds.
If the verifier fails, keep the writer services stopped, preserve the failed
database for inspection, and restore from a different dump.

## Managed Postgres variant

For a managed single-host-style deployment, run the same logical backup with
the provider's synchronous Postgres URL. `pg_dump` and `pg_restore` do not use
SQLAlchemy's `+asyncpg` suffix.

```bash
pg_dump --dbname "$PADRINO_DB_URL_SYNC" \
  --format=custom \
  --no-owner \
  --no-acl \
  > "$BACKUP_FILE"

pg_restore --dbname "$PADRINO_RESTORE_DB_URL_SYNC" \
  --clean \
  --if-exists \
  --no-owner \
  --no-acl \
  "$BACKUP_FILE"

uv run padrino game verify-restore "$VERIFY_GAME_ID" \
  --db-url "$PADRINO_RESTORE_DB_URL_ASYNC"
```

The same single-game verifier is the acceptance gate: the restored
`game_events` chain must be contiguous and must fold to the pre-backup final
hash.
