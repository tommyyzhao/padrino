# AGENTS.md — Wave 9 change-set (apply at v9 swap-in)

Staged so the live `AGENTS.md` is untouched during any in-flight Wave 8 run.
At Wave 9 start, apply these surgical edits to `AGENTS.md` (keep everything else
as-is, including whatever Wave 8 left). Then commit as
`docs: AGENTS.md - Wave 9 human-multiplayer conventions`.

---

### Edit 1 — "What Padrino is" (append a paragraph)

> Padrino now also has a **human-multiplayer layer** (`prd-v3.md`): humans play
> with/against models in **private friend lobbies**, default **anonymous**,
> always **casual**, and **strictly segregated** from the scientific benchmark.
> Vision docs: `prd.md` (v1 backend), `prd-v2.md` (v2 spectator site), `prd-v3.md`
> (v3 human multiplayer). The executable plan is `ralph/prd.json`.

### Edit 2 — REPLACE Hard rule 6

Replace:

> ### 6. Backend-only — no browser verification
> Ignore the default Ralph "Verify in browser using dev-browser skill" criterion.
> Padrino has no UI in v1. Quality gates are `ruff` + `mypy` + `pytest`.

with:

> ### 6. Frontend is first-class (since Wave 7)
> The SvelteKit dashboard under `web/dashboard/` is part of the product. Frontend
> stories run the pnpm gates (`pnpm -C web/dashboard lint` / `check` /
> `test:e2e` via Playwright) in addition to the four backend gates. The
> pure-core firewall still applies only to `src/padrino/core/**`; it never blocks
> the TypeScript project under `web/`.

### Edit 3 — ADD Hard rules 7–10 (after rule 6)

> ### 7. Anonymity is identity-blind for humans too (Wave 9)
> On any live / observation / spectator surface, never reveal which seats are
> human vs AI, nor model/provider identity, before the **endgame reveal**. Guards
> **fail CLOSED** (a missing/None `identity_mode` coerces to ANONYMOUS). The
> guard covers DB **columns**, not only payload keys. Composition is disclosed as
> **counts only** ("N humans, M AI"), frozen at game start.
>
> ### 8. Human games are segregated from the benchmark (Wave 9)
> A human-lane game writes **ZERO** rows to the scientific `Rating`/`RatingEvent`
> tables. Human ELO lives in a **dormant** sibling table under the
> "Humans-Included League" and is not written in v1 (casual).
>
> ### 9. Human chat is stored out-of-band of the hash chain (Wave 9)
> Human free-text chat lives in a **sidecar** table; the paired core event
> carries only a reference/hash. This preserves the chat-firewall and makes GDPR
> erasure possible without breaking the hash chain or deterministic replay.
>
> ### 10. Human games run on a separate worker lane (Wave 9)
> Minutes-to-hours human games run on a dedicated runner lane with **durable,
> rehydratable Postgres-snapshot** state, isolated from the benchmark scheduler's
> concurrency cap. **No Redis** (stack rule).

### Edit 4 — Quality gates section (append a note)

> Frontend stories additionally run: `pnpm -C web/dashboard lint`,
> `pnpm -C web/dashboard check`, `pnpm -C web/dashboard test:e2e`.

### Edit 5 — Rulesets (Hard rule 5, append a note)

> The human-multiplayer lane reuses `mini7_v1` / `bench10_v1` (decision 15); a
> buffered-cadence variant (`hcad7_v1` / `hcad10_v1`) is **deferred** past v1.

### Edit 6 — Dependencies section (append a note)

> Wave 9 adds `authlib` (one-provider OAuth, US-129). Justify any other new
> runtime dependency in `progress.txt`. The durable human-game state uses the
> existing async DB (Postgres snapshots) — **do not** add Redis.
