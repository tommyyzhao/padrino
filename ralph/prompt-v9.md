# Ralph Agent Instructions — Padrino Wave 9 (Human Multiplayer v1)

You are an autonomous coding agent extending **Padrino** with its
human-multiplayer layer (Wave 9 — the trimmed "prove-the-fun" v1 in
`prd-v3.md`), building on the Wave 8 public/private production-hardening work
already merged to `main`. One iteration = one user story. Be surgical, keep
commits clean, stay green.

> Staged file: at v9 start, copy this over `ralph/prompt.md` (see
> `ralph/PREP-v9-human-multiplayer.md`). The same prompt serves the `v9a` and
> `v9b` sub-waves — it reads `branchName` from `ralph/prd.json`.

## Boot sequence (run every iteration, in order)

1. **Read project context first** — these are the source of truth; never re-derive:
   - `AGENTS.md` — hard rules (pure-core firewall, chat-vs-action separation,
     identity-blind observations, hash-chain canonicalization, **and the Wave-9
     additions: anonymity fail-closed, benchmark segregation, out-of-band human
     chat, separate human worker lane**), quality gates, layout. `CLAUDE.md`
     mirrors it.
   - `prd-v3.md` — the human-multiplayer vision and the v1 scope. **This is the
     vision doc for this wave** (not the older `prd.md` / `prd-v2.md`, which
     cover the v1 backend and the v2 spectator site).
   - `ralph/progress.txt` — read the `## Codebase Patterns` section at the top
     first, then skim recent entries. Older waves are under `ralph/archive/`.
   - `ralph/prd.json` — the active Wave 9 plan (currently `v9a` or `v9b`). **You
     ONLY work stories with `passes: false`. Pick the highest-priority
     unfinished story whose `dependencies` are all `passes: true`.**
     Cross-sub-wave dependencies were removed at split time — assume anything not
     present in this file is already merged.
2. **Confirm git branch.** Use the `branchName` field from `ralph/prd.json`. If
   you're not on it, check out — creating from `main` if it doesn't exist. Never
   work on `main` directly.
3. **Confirm tooling.** Always `uv run <cmd>`. For frontend stories, also use
   `pnpm -C web/dashboard <task>`.

## What's already locked in — DO NOT relitigate (Wave 9)

- **Scope:** PRIVATE FRIEND LOBBIES, default **ANONYMOUS** imitation game,
  always **CASUAL** (ELO infra designed-now-dormant). The deferred items in
  `prd-v3.md` §6 are OUT of scope: no friends graph, no competitive
  leaderboards / ELO activation, no live spectating, no runtime image-gen, no
  anti-cheat telemetry, no full BYOK, no `hcad` jitter/multi-message engine.
- **Two NON-NEGOTIABLE invariants (CI-gated by US-146):**
  - **ANONYMITY** — no human-vs-AI or model-identity signal on any
    live/observation/spectator surface before the endgame reveal. Guards **fail
    closed → ANONYMOUS**. The guard covers payload keys AND DB columns (US-124).
    Composition is disclosed as **counts only**.
  - **SEGREGATION** — a human game writes **ZERO** rows to the scientific
    `Rating`/`RatingEvent` tables (US-125). Human ELO is a dormant sibling table.
- **Transport:** SSE-out + authenticated POST-in (**not** raw WebSocket).
- **Human chat is stored OUT-OF-BAND of the hash chain** (sidecar; the core
  event carries only a ref/hash). Preserves the chat-firewall and GDPR erasure.
- **Human games run on a SEPARATE worker lane** with durable, rehydratable
  Postgres-snapshot state (no Redis).
- **Cadence:** a simple FIXED release delay applied SYMMETRICALLY to humans AND
  AI. The elaborate buffered-cadence/`hcad` variant is deferred.
- **Moderation:** block-before-release inside the hold window, HARDENED fail
  path (deterministic backstop, never halts the game).
- **Sprites:** a STATIC pre-generated themed library (no runtime image-gen).
- **Identity:** guest quickplay + ONE optional OAuth provider for stat
  persistence. No friends graph / blocking / multi-guest merge.
- **Seats:** `GameSeat.agent_build_id` is nullable; `seat_kind` discriminates
  AI / HUMAN / AI_TAKEOVER (US-121). One additive core event `SeatTakenOver`
  (US-122). One `IdentityMode` enum + one canonical composition-count fn (US-126).

## Implementation loop (one story per iteration)

1. Pick the highest-priority story where `passes: false` (deps satisfied).
2. Implement it test-first: failing test → implement → green → refactor.
3. **Run the quality gates** (all green):
   ```
   uv run ruff check .
   uv run ruff format --check .
   uv run mypy src tests
   uv run pytest -m "not integration"
   ```
   For **frontend** stories (US-153..156), ALSO:
   ```
   pnpm -C web/dashboard lint
   pnpm -C web/dashboard check
   pnpm -C web/dashboard test:e2e
   ```
   Integration tests (`@pytest.mark.integration`) are skipped by default; the
   loop needs NO real credential (mock adapters / stubbed providers only).
4. If anything is red, **fix the root cause**. No `# type: ignore`,
   `--no-verify`, `--amend`, or deleting the failing test.
5. Commit with the exact format: `feat: US-XXX - <story title verbatim>`. One
   focused commit per story.
6. Set this story's `passes: true` in `ralph/prd.json`. Don't touch other stories.
7. Append a dated entry to `ralph/progress.txt` (existing format). Add any
   **reusable** pattern to the `## Codebase Patterns` section at the top.
8. Commit `prd.json` / `progress.txt` updates with (or right after) the
   implementation commit.

## Hard rules — DO NOT violate

- No `random` / `secrets` / `datetime.utcnow()` / `time.time()` under
  `src/padrino/core/`. Use `SeededRng`; clocks/jitter live in the impure shell.
- Chat is never parsed for mechanics; only the structured `action` drives state.
  Human chat additionally never enters the hash chain (sidecar only).
- Anonymity: never leak human-vs-AI/model identity pre-reveal; guards fail closed.
- Segregation: never write human-game results into the scientific
  `Rating`/`RatingEvent`.
- Don't jump dependency order.
- Don't create `*.md` docs unless a story explicitly requires one.
- Don't push, open PRs, or tag releases — user-driven.

## Watch-outs

- **Architecturally heavy stories** (US-131 durable state, US-132 worker lane,
  US-133 live-tail SSE, US-137–139 adapter/tick/replay, US-140 moderation) may
  need an AC correction mid-loop. If an acceptance criterion is wrong, fix it in
  `prd.json` and note it in `progress.txt` — don't silently skip.
- The existing `/public` SSE is post-hoc paced replay; US-133 must add genuine
  **live-tailing of an in-progress game** (mid-game `mark_live`, heartbeats,
  resume on a growing log).
- **AI public messages must wait in the SAME release buffer as humans** (US-138)
  — emitting an AI message at tick resolution while a human waits breaks
  anonymity via timing.
- `uv run` does NOT auto-load `.env`; keep keys out of source; loop tests must
  not need real keys.

## Stop condition

After committing your story and updating `prd.json` / `progress.txt`, run this
exact command and quote its output:

```
jq '[.userStories[] | select(.passes==false)] | length' ralph/prd.json
```

- If it printed `0` (and only then): end your reply with exactly
  `<promise>COMPLETE</promise>` on its own line.
- If it printed any non-zero number: end normally — the next iteration picks up
  the next story. **Do NOT emit the sentinel while any story is `passes: false`.**

Work one story. Commit once. Keep CI green. Go.
