# Scheduled recurring gauntlets (US-085)

Padrino's in-process scheduler can fire a heterogeneous N-game tournament
(US-084) on a cron schedule, so the public leaderboard reflects fresh
head-to-head data without manual `padrino gauntlet run` invocations.

## How it works

A `scheduled_gauntlets` row holds a 5-field cron expression, a serialized
roster spec, an `n_games` count, and a `cost_cap_usd`. On each scheduler tick
the gauntlet job (`padrino.scheduler.gauntlet_job.run_due_scheduled_gauntlets`,
wired in by `padrino.scheduler.bootstrap.build_scheduled_gauntlet_tick_hook`):

1. selects enabled rows whose `next_run_at` is `NULL` or already due,
2. runs `run_tournament_from_roster(n_games=…, cost_cap_usd=…)` for each,
3. records `last_run_at`, `last_run_gauntlet_id`, and recomputes `next_run_at`
   from the cron expression via the pure-core helper
   `padrino.core.scheduling.next_run_at`.

The scheduler runs in **UTC**; cron fields match UTC calendar fields, so a
`0 2 * * *` schedule fires at 02:00 UTC every day with no DST shifts.

## Cron syntax

Standard 5-field cron: `minute hour day-of-month month day-of-week`.
Supported: `*`, `*/n`, `a`, `a-b`, `a-b/n`, and comma lists. Day-of-week is
`0-6` (Sunday = 0; `7` is also Sunday). When both day-of-month and day-of-week
are restricted, a day matches if **either** does (the standard cron OR rule).

Examples: `*/15 * * * *` (every 15 min), `0 2 * * *` (daily 02:00 UTC),
`0 */6 * * *` (every 6 hours), `0 0 1 * *` (first of each month).

## Roster spec

The roster spec mirrors US-084's shape, plus the league to rate under:

```json
{
  "league_id": "<league-uuid>",
  "roster": {
    "P01": "<agent_build_id>",
    "P02": "<agent_build_id>",
    "P03": "<agent_build_id>",
    "P04": "<agent_build_id>",
    "P05": "<agent_build_id>",
    "P06": "<agent_build_id>",
    "P07": "<agent_build_id>"
  }
}
```

All seven seats must be present; `agent_build_id`s may repeat. Every referenced
`agent_build_id` (and the `league_id`) must already exist.

## Admin API

```
POST   /admin/scheduled-gauntlets         # create; returns {id, next_run_at}
PATCH  /admin/scheduled-gauntlets/{id}     # update enabled / schedule_cron / cost_cap_usd
DELETE /admin/scheduled-gauntlets/{id}     # soft-delete: enabled=false, next_run_at cleared
```

Create body:

```json
{
  "name": "nightly-benchmark",
  "schedule_cron": "0 2 * * *",
  "roster_spec": { "league_id": "…", "roster": { "P01": "…", "…": "…" } },
  "n_games": 10,
  "cost_cap_usd": 20.0,
  "enabled": true
}
```

Only `enabled`, `schedule_cron`, and `cost_cap_usd` are mutable. To change the
roster or `n_games`, create a new schedule. `DELETE` keeps the row for audit and
simply disables it; nothing fires for a disabled schedule.

## Public API

```
GET /public/scheduled-gauntlets
```

Returns a **scrubbed** view — no raw cron expression and no cost cap:

```json
{
  "schedules": [
    {
      "name": "nightly-benchmark",
      "schedule_cron_human": "every day at 02:00 UTC",
      "last_run_at": "2026-05-28T02:00:00Z",
      "next_run_at": "2026-05-29T02:00:00Z",
      "last_gauntlet_id": "…",
      "status": "COMPLETED"
    }
  ]
}
```

`status` is `scheduled` (never run yet), `disabled`, or the most recent
gauntlet's status (`COMPLETED`, `cost_capped`, …).

## Cost cap

`cost_cap_usd` bounds each run: cumulative cost is checked between games, and a
run that crosses the cap stops before the next game. The partial gauntlet is
left with `status='cost_capped'` (rather than `COMPLETED`) so an operator can
tell a budget-aborted run from a clean one.

## Update cadence

The public leaderboard refreshes whenever a scheduled run completes. Pick a
cadence that balances freshness against spend: a `0 2 * * *` daily 10-game run
at a $20 cap is a reasonable default for a live benchmark.
