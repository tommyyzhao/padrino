# Padrino — Production Readiness & Gap Analysis

_Last updated: 2026-05-28, after Wave 4 (US-079…US-085) merged to `main` at `c9b1856`._

This document assesses Padrino against three production goals and enumerates the
prioritized gaps to close. It is the canonical reference for planning "Wave 5".

**The three goals**

1. **Anonymous viewers can spectate ongoing (in-progress) games** in real time.
2. The **core foundation is robust enough for a proper Elo system** on top.
3. We can do **scientific tracking of the Elo of different LLM models** at Mafia.

**Verdict.** The foundation is strong and production-ready *today* as a
**single-node, authenticated** service. The gaps to the three goals are additive,
not a rewrite. There is **one existing privacy bug** that must be fixed regardless
(mid-game role leak) and a **central trust gap** (forgeable ingested results) that
gates any "scientific" Elo claim.

---

## What is already solid

- **Determinism end-to-end** — seeded RNG (`core/engine/rng.py`), `sha256` game
  seeds (`gauntlets/scheduler.py:42`), seat permutations (`core/seating.py`), no
  `random`/wallclock in core (enforced by `tests/core/test_purity.py`). Games
  replay bit-for-bit.
- **Rating math** — OpenSkill Plackett-Luce, `INITIAL_MU=25`, `sigma=25/3`,
  conservative score `mu-3σ` (`ratings/openskill_service.py:23-39`). Correct
  win/loss/**draw→rank** mapping and tie encoding (`:51-57`). Atomic, audited
  `RatingEvent` writes committed with the terminal event
  (`runner/game_runner.py:288-314`).
- **Statistical rigor** — US-084 seat/role rotation neutralizes positional &
  faction luck (`gauntlets/tournament.py:90` + `core/seating.py`); Wilson 95% CIs
  (`core/statistics.py:37`); provisional thresholds `<30 total / <15 town / <5
  mafia` (`gauntlets/completion.py:38-40`).
- **Tamper-evident hash chain** — `sha256(prev + canonical_json(body))`, excludes
  `event_hash`/`prev_event_hash`/`created_at` (`core/engine/hashing.py:19-31`),
  verified on export, ingest, and replay (`export/bundle.py:392`,
  `api/routes/ingest.py:163-177`, `api/routes/games.py:329`).
- **Agent privacy (US-078)** — runtime guard `assert_ranked_observation_safe` +
  offline `audit_observation_log_for_seat`, one shared `FORBIDDEN_PAYLOAD_KEYS`
  (`core/observation_privacy.py:64-135`).
- **Ops** — multi-stage non-root `Dockerfile` with `/healthz` HEALTHCHECK;
  `docker-compose.yml` (postgres + one-shot bootstrap + api + separate scheduler
  + dashboard); Postgres-first DB layer (`db/base.py`); scoped auth
  (`api/auth.py`); cross-worker `DatabaseRateLimitStore`
  (`api/rate_limit_store.py`); Prometheus domain metrics
  (`observability/metrics.py`); crash recovery via stale-heartbeat reset
  (`runner/scheduler.py:258,348`); `/healthz` + `/readyz` + `/healthz/scheduler`;
  114 test files; CI matrix (ubuntu/macos × py3.12/3.13) with ruff/mypy/pytest and
  a `--fail-under=85` **core** coverage gate (`.github/workflows/ci.yml`).

---

## P0 — must fix before any public / anonymous launch

### 1. Mid-game role/faction leak (existing bug)
`GET /games/{id}/events?visibility=public` (`api/routes/games.py:210-247`) returns
raw `PlayerEliminated` payloads with `role`+`faction` intact, **no terminal-gating
and no stripping**. A spectator learns who was mafia *while the game is live*.
The correct projector (`_redact_event_for_non_terminal` + `_strip_forbidden`,
`api/routes/public.py:264-297`) is only wired to post-completion ingested bundles.

**Fix:** extract a single `core/spectator_projection.py` (drop SYSTEM+PRIVATE,
strip `FORBIDDEN_PAYLOAD_KEYS ∪ {role,faction}` from PUBLIC payloads); make it the
*only* path that renders a non-terminal game to any non-player. Add a property test
over all ~18 event types asserting no role/faction/PRIVATE content survives
mid-game. Event visibility model: `core/engine/events.py:21` (PUBLIC/PRIVATE/SYSTEM);
hidden-info carriers = `RolesAssigned` (SYSTEM, `:148-154`), `NightResolved`
(SYSTEM, `:107-111`), all PRIVATE events, and `role`/`faction` inside PUBLIC
`PlayerEliminated` (`:118-123`).

### 2. No per-IP rate limiting
Limits are per-API-key; anonymous traffic shares one `id=None` bucket
(`api/routes/public.py:86-91`), and `/metrics`/`/healthz` are unthrottled. With
`PADRINO_PUBLIC_LEADERBOARD_ANONYMOUS=true` the read surface is open to scraping/DoS.
**Fix:** per-IP app-layer buckets, or mandate CDN/reverse-proxy edge limiting in the runbook.

### 3. Live API keys on disk
The working-tree `.env` holds real Cerebras/DeepInfra/Zai/Xiaomi keys (gitignored,
history-clean, but FS/backup-exposed). **Fix:** rotate; adopt `file:`-scheme secrets
(`llm/secrets.py` already enforces owner-only perms) or a secrets manager.

### 4. TLS external-only
App speaks plain HTTP on `0.0.0.0:8000`. **Fix:** make TLS termination + HSTS a hard
runbook requirement.

### 5. Forgeable ingested results (trust gate)
A `submitter`-scoped key can fabricate a fully valid, self-signed, hash-consistent
bundle — "verified" means *submitter-vouched*, not *honestly played*
(`api/routes/ingest.py:182-208`). Unverified bundles may also flow into the public
leaderboard (no `verification_status` filter in `ratings/public_leaderboard.py`).
**Fix (minimum):** filter Elo on `verification_status=='verified'`. **Fix (for real
science):** gate rated games on **frozen-LLM-response replay** (planned mode noted in
`core/engine/replay.py`), or distinguish "operator-attested" vs "centrally-replayed"
rating tiers.

### 6. Rating re-count idempotency
`_should_apply_ratings` (`runner/game_runner.py:256-265`) has no "already rated?"
guard and `rating_events` has no uniqueness constraint
(`db/migrations/versions/0003_ratings.py`). A manual re-run double-counts.
**Fix:** `UniqueConstraint(game_id, agent_build_id, scope_type, scope_value)` +
skip-if-COMPLETED precondition. Cheap insurance.

---

## Goal 1 — Anonymous live spectating

The hard parts already exist: `game_events` is written incrementally per phase
(`runner/game_runner.py:602-621`), and the redaction projector exists. Missing:

| Gap | Detail | Effort |
|---|---|---|
| No public live read path | `/public/games/*` read `ingested_games` (post-completion only). Add `GET /public/games/{id}/live?since_sequence=N` over `game_events` through the shared projection. | ~1d |
| `games.status`/`current_phase` not maintained mid-run | Only written at terminal (`game_runner.py:288-297`). Set `RUNNING`+`current_phase` per `PhaseStarted`; update `game_seats.alive`/`death_phase` on elimination. | ~0.5d |
| No live discovery | Add anonymous `GET /public/games?status=RUNNING` (identity-blind). | ~0.5d |
| No streaming | Start with **long-poll** (`since_sequence` + frontend interval); SSE (`sse-starlette`) later. No real-time infra exists today (no SSE/WS anywhere). | ~0.5d BE + 1d FE |
| Frontend has no live view | Dashboard `/games/[id]` is a one-shot fetch, no polling (`web/dashboard/src/routes/games/[id]/+page.svelte`). Add live mode + `/live` index. | ~1–1.5d |

**~4–5 engineer-days for a long-poll MVP.**

---

## Goals 2 & 3 — Robust Elo + scientific model tracking

The engine is sound; the **aggregation and trust layers** hold the gaps.

| Priority | Gap | Fix |
|---|---|---|
| P0 | Forgeable results / unverified bundles in Elo (see P0 #5). | verification filter + frozen-LLM-replay tier. |
| P0 | Re-rate idempotency (see P0 #6). | unique constraint + guard. |
| P1 | **Model rollup sigma formula is statistically wrong** and re-aggregates posteriors instead of replaying events — not a true posterior, not auditable (`ratings/model_rollup.py:168-186`). | Replay the game log through one PlackettLuce run keyed on model identity (pattern already in `ratings/public_leaderboard.py:197-238`). |
| P1 | **No head-to-head significance** — can't say "A > B with confidence." | Probability-of-superiority from mu/σ posteriors (OpenSkill `predict_win`) or two-proportion test on faction-balanced win rates. |
| P1 | **League-scoped only** — no cross-league/cross-gauntlet longitudinal aggregation (`model_rollup.py:282,307` filter on `league_id`). | Longitudinal aggregation across gauntlets sharing a ruleset; optional time-windowing. |
| P2 | **Model-identity drift** — silent provider weight updates pollute one bucket; rollup key omits prompt/adapter/temperature, merging experimental conditions. | Date-windowed rollups + drift canary; decide explicitly what the identity key marginalizes over. |
| P2 | No rating decay; leaderboard caches are process-local (diverge across workers, `model_rollup.py:235`); no analysis export of `rating_events`/trajectories. | Optional decay; shared cache or explicit recompute endpoint; first-class CSV/JSON export. |

**Note on the team model (intrinsic):** the 7 seats collapse into TOWN vs MAFIA
teams sharing one outcome (`openskill_service.py:106-108`) — individual skill within
a faction is not separated, and mini7's 5v2 asymmetry is baked into the update. This
is a defensible simplification (faction outcome is the only ground truth) but worth
documenting for any published methodology.

---

## Cross-cutting ops (P1–P2)

- **API single-process** (no `--workers` flag in `cli.py serve`) and **scheduler is
  single-writer** — `claim_oldest_pending` (`db/repositories/gauntlets.py:101`) lacks
  `FOR UPDATE SKIP LOCKED`; two scheduler replicas would double-claim. Both are SPOFs
  for a long-running benchmark. Document "exactly one scheduler" or add row-locking.
- **No alerting/error-reporting** — Prometheus metrics exist but nothing fires; no
  Sentry/OTel. Add alert rules (scheduler down/degraded, 5xx rate, LLM
  `provider_error` rate).
- **Coverage gate covers only `core/`** — impure layers (api/runner/llm/db), where
  prod risk concentrates, have no floor.
- **Minor:** `PADRINO_LOG_LEVEL` ignored (hardcoded "INFO" at `cli.py:47`); manual
  `pg_dump` backups; no data-retention policy (rate_limit_buckets, bundles, event
  logs) for a long-running benchmark; dashboard API base URL baked at build time.

---

## Recommended sequencing (Wave 5)

- **Sprint A — Safe public surface (P0):** shared spectator projection + leak fix
  (#1), per-IP rate limiting (#2), key rotation + TLS runbook (#3,#4),
  `verification_status` filter + rating idempotency constraint (#5 min, #6).
- **Sprint B — Live spectating MVP:** mid-game `games`-row state,
  `/public/games/{id}/live` + `?status=RUNNING`, long-poll, frontend live view.
- **Sprint C — Scientific Elo:** replay-based model rating, head-to-head
  significance, longitudinal aggregation, analysis export, frozen-LLM-replay trust
  gate (the big one — turns a leaderboard into a citable benchmark).
- **Sprint D — Scale/ops hardening:** API workers, scheduler locking, alerting,
  broader coverage gate, backups/retention.

**Highest leverage:** the mid-game leak fix (small, live defect) and the
forgeable-results trust gate (the difference between "operators vouch for these
numbers" and "anyone can reproduce these numbers").
