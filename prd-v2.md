# Padrino v2 — Public Live League (PRD extension)

> Extends `prd.md` (v1 = backend-only deterministic benchmark). v2 turns the
> engine into a public consumer product: **watch AI-vs-AI Mafia matches "live,"
> a compounding game bank drives a public rating ladder, and rich analytics
> explain what happened** — "chess.com for AI Mafia."
>
> The hard, easy-to-get-wrong part (a fair, reproducible, cheat-resistant
> game + rating core) already exists. v2 is a product layer on top; it must not
> compromise the v1 fairness/determinism guarantees.
>
> The load-bearing decisions below were stress-tested by two independent
> staff-level reviews (Gemini 3.1 Pro, GPT-5.5). Where they reached consensus
> it is noted; that consensus reframed several of the original gap-analysis
> conclusions (see "Corrections" at the end).

---

## 0. v2 MVP scope — locked decisions (2026-06)

Four product decisions, ratified by the owner, fix the MVP's autonomous scope:

- **Frontend → the loop builds the consumer SPA**, gated by Playwright
  acceptance tests; a human does visual/UX polish. `web/` stories enter the
  autonomous set (precedented by US-070/US-077; Hard rule 6's "no browser
  verification" applies only to `src/padrino/core/**`, never `web/`).
- **Ladder → invite/curated roster first.** This **defers most of D4** (open
  submissions, OAuth identity, anti-Sybil, provenance) to a post-MVP wave. The
  MVP ladder runs over a curated roster; OpenSkill + versioning + provisional/
  decay stay, identity/Sybil machinery does not.
- **Accounts → anonymous spectator + ladder MVP.** This **defers all of D7**
  (accounts, profiles, follows, notifications, auth) out of the MVP.
- **Cost → house-funded with hard caps.** No BYOK key-management surface; **D3
  reduces to** global daily game caps + per-game budget ceilings (reuse the
  existing `cost_cap_usd`/`cost_capped` machinery). Requires a human-set dollar
  ceiling + provider billing authorization.

**Net effect:** the MVP autonomous scope is Phases 0–4 **minus** the deferred
D4-trust work and all of D7 — roughly 85% loop-buildable. The hard human gates
that remain are in §6.

---

## 1. Scope delta (v1 → v2)

| Area | v1 (today) | v2 (target) |
|---|---|---|
| Audience | operators / agents via API key | public spectators; agent submitters |
| Game production | operator-scheduled gauntlets | continuous matchmaking over a roster |
| Viewing | poll completed signed bundles (`IngestedGame`) | "live" paced replay + on-demand replay |
| Ratings | OpenSkill per league/ruleset (internal) | public ELO-style ladder with trust controls |
| Analytics | post-game LLM judge (raw, un-aggregated) | deterministic stats + sampled judge enrichment |
| Surface | minimal internal SvelteKit dashboard | consumer SPA: live, ladder, profiles, replays |

**Explicitly still out of scope:** real-money, human-vs-AI play, RL/training
loops, model hosting. v2 is spectating + ranking + analytics.

---

## 2. Load-bearing decisions (ratified)

### D1 — Decouple production from presentation; "live" = paced replay of committed events. **(spine)**
*Consensus: CRITICAL, both reviewers.* The public stream presents **committed,
hash-chained events** (via an identity-blind projection), never the runner's
in-flight state. The engine keeps running at LLM speed (`asyncio.sleep(0)`);
a separate **Broadcaster** reads persisted events and emits them over SSE at a
human-watchable cadence. This sidesteps the determinism-vs-realtime tension
entirely (streaming committed facts is deterministic-safe), isolates LLM
latency/retry/failure from the viewer, and lets us add moderation, commentary,
and analytics overlays. The runner is **not** modified.
*Already supports this:* the runner persists each event incrementally
(`runner/game_runner.py::_persist_stored_event`), and
`core/spectator_projection.py` already strips forbidden fields.

### D2 — A versioned **public projection contract**, distinct from internal core events.
*Consensus: elevate to load-bearing (Codex).* Internal `GameEvent` shapes are an
implementation detail; the moment live views, replays, analytics, and ratings
depend on a public event shape, schema changes become product migrations.
Define a `public_event_v1` projection schema (built on `spectator_projection`)
with an explicit version field; the Broadcaster and analytics read only this.
Internal events may evolve freely behind it.

### D3 — **Economic / capacity governance** lands *before* open matchmaking.
*Consensus: CRITICAL, both reviewers (the "wallet-DDoS" vector).* Continuous
AI-vs-AI games are unbounded LLM spend. Required primitives: global daily game
caps, a per-game cost ledger + budget (the gauntlet path already has
`cost_cap_usd`/`cost_capped` — reuse it), model quotas, queue/admission policy,
retry limits, and a submission throttle. Consider BYOK (submitter-pays) for
open submissions.

### D4 — **Ladder trust** is the real ladder problem (not Elo vs OpenSkill).
*Consensus: CRITICAL, both reviewers.* OpenSkill stays. The missing primitive is
credibility: stable identity + submission provenance (OAuth: GitHub/X),
anti-Sybil (one actor flooding weak bots to farm a main bot's rating), agent
**versioning**, provisional vs established ratings, retirement/decay, and an
audit trail. Gate the public ladder on these.

### D5 — **Moderation / broadcastability gate** before any public display.
*Consensus: first-order, both reviewers.* Deception games will surface toxic /
unsafe / prompt-injected text. A game is shown only after a cheap safety pass
sets `is_broadcastable` (deterministic filters + a guard model). Delayed paced
replay (D1) gives the time window to run this before viewers see it.

### D6 — Analytics: **deterministic stats first; LLM judge = sampled async enrichment.**
*Consensus, both reviewers.* Deterministic metrics (computed from the event log)
are cheap, reproducible, and defensible — they back the public analytics and
never the live path. The judge runs offline, batched, sampled (e.g. top-tier or
N% of games), with a real cost cap, and only enriches — it never feeds ratings.

### D7 — Accounts are load-bearing **only** once submissions/profiles exist.
*Consensus: INFO.* An anonymous spectator + ladder MVP needs no accounts.
Auth becomes core the moment users submit agents or build reputation — sequence
it with D4, not before.

**Cross-cutting design concerns** (not phases, but gates on every phase):
spoiler policy (the outcome is already in the DB while a game streams "live" —
leaderboards/APIs must not leak it to live viewers), watchability/cadence
tuning, and storage growth/retention for a compounding bank.

---

## 3. Phased, dependency-ordered backlog

Each phase has an **exit criterion** that unblocks the next. Build order is
chosen so the riskiest assumption (watchability) is proven cheaply first, and
the safety/cost rails exist before anything goes open.

### Phase 0 — Ratify scope + contract (1 short cycle)
- Approve this doc; fold the scope delta into `prd.md` non-goals.
- **D2 deliverable:** freeze `public_event_v1` projection schema (extend
  `spectator_projection`), with a version field and a golden-file test.
- *Exit:* public event contract is versioned and test-locked.

### Phase 1 — Broadcast spine + consumer live viewer (de-risks Pillar 1)
*Sequence per both reviewers: prove watchability before transport scale.*
1. **Mock Broadcaster** — emit `public_event_v1` SSE at a fixed cadence from a
   single completed `IngestedGame` bundle (no live DB).
2. **Consumer live-viewer UI** — chess.com-style match page (seat board,
   phase-by-phase reveal, vote tally, timeline) against the mock stream.
3. **Wire to Postgres** — Broadcaster reads committed events; resumable by
   `sequence` cursor (reconnect → resume, no replay-from-zero).
4. **"Live now" + "recent" public index.**
- *Exit:* a real completed game is watchable end-to-end at a tuned cadence,
  identity-blind, reconnect-safe. Runner unchanged.

### Phase 2 — Safety + cost rails (D3, D5) — **gates public openness**
- **Moderation:** `is_broadcastable` gate (deterministic filters + guard model);
  only broadcastable games enter the live/recent index.
- **Economics:** global game-rate caps, per-game cost ledger/budget (reuse
  `cost_cap_usd` machinery), model quotas, admission/queue policy.
- *Exit:* no game reaches the public surface unmoderated; a runaway cost or
  abuse spike is bounded by hard caps. **Only now is public launch safe.**

### Phase 3 — Continuous matchmaking + trusted ladder (Pillars 2/4-rating; D4)
- **Matchmaker:** continuous pairing over an agent roster; faction-permutation
  gauntlets for fairness (PRD §: single games too noisy); seed derivation reuses
  the deterministic core.
- **Trust:** identity/OAuth + provenance, anti-Sybil, agent versioning,
  provisional/established, decay/retirement, audit trail.
- **Presentation:** surface OpenSkill ordinal (`mu - 3*sigma`) as an ELO-style
  number with provisional badges; public ladder pages.
- *Exit:* an open (or invite-gated) roster produces a continuously updating,
  Sybil-resistant public ladder.

### Phase 4 — Analytics (Pillar 4; D6)
- **Deterministic analytics layer** materialized from `public_event_v1`:
  per-role win rates, voting accuracy, survival curves, claim/counter-claim
  tracking, head-to-head matrices, role-family ELO.
- **Judge productionized:** offline, batched, sampled, cost-capped; aggregated
  into per-model/per-role trend cards. Enrichment only.
- **Analytics UI:** model profile pages + per-game post-match breakdown.
- *Exit:* every public game has Town-of-Salem-grade deterministic stats; judge
  scores enrich top games without gating ratings or cost.

### Phase 5 — Consumer product hardening (Pillar 3; D7)
- Accounts/profiles/follows/notifications (scope per D7).
- Public hardening (v1 non-goal): CORS at scale, abuse/DDoS, CDN/edge-caching of
  replays (replays are immutable → highly cacheable), public-traffic
  observability, retention/archival policy.
- Submission onboarding UX.
- *Exit:* a stranger can sign up, submit an agent, follow models, and watch
  live — within hard cost/abuse bounds.

---

## 4. Critical-path & sequencing risks
- **Critical path:** D2 contract → Phase 1 spine → Phase 2 rails → public launch.
  Matchmaking/ladder (P3) and analytics (P4) can proceed in parallel *after* the
  rails exist, but **must not** precede Phase 2 (open play before cost/moderation
  rails = the wallet-DDoS + brand-safety failure modes).
- **Biggest risk if we build "streaming spine first" naively:** locking the
  product around transport instead of watchability, and entangling presentation
  with the engine loop. Mitigated by D1 (dumb one-way SSE on committed events,
  runner untouched) and the mock-first Phase 1 sequence.
- **Spoiler leakage:** while a game streams "live," its result is already
  committed; every other public endpoint must hide outcomes for in-broadcast
  games. Design into the projection/index from Phase 1.

---

## 6. Human-dependency register ("HumanAPI")

The autonomous loop **cannot** manufacture these. Each is tagged with how a
story should behave so the loop completes the buildable part and stops cleanly
at the seam (rather than inventing a fake answer per its "if blocked" rule).

| Need | Type | Gates | Loop behavior |
|---|---|---|---|
| Spend ceiling ($/day, per-game) + provider billing | decision + billing | D3 / open play | Build caps against config + mocks; real number set in env; live spend behind integration marker |
| Guard/moderation model account + key | credential | D5 / public launch | Build `is_broadcastable` pipeline; real guard call `@pytest.mark.integration` |
| Moderation + judge **calibration set** (human-labeled) | human verification | D5 / D6 quality | Build scorers + harness; precision/recall validated against a human set the loop can't author |
| Cadence / spoiler-window values | decision (tunable) | D1 watchability | Ship sane defaults in config; human tunes |
| Consumer-SPA visual/UX sign-off | human verification | Phase 1/5 polish | Playwright asserts behavior; human judges look-and-feel |
| Production infra (domain, scaled PG, CDN/edge, secrets, observability) | provisioning | Phase 5 launch | Build deploy config (compose/Docker exist); human provisions + runs |
| Legal / ToS / content + privacy policy | legal | public launch | Out of loop scope entirely |
| *(deferred)* OAuth apps + identity, accounts | credential + decision | post-MVP (D4/D7) | Not in MVP autonomous set per §0 |

Everything else — the public projection contract, broadcaster, mock + wired
SSE, consumer live-viewer + ladder + analytics UI, deterministic analytics,
curated-roster matchmaker, cost-cap plumbing, moderation pipeline scaffolding —
is loop-buildable with the existing test seams.

## 5. Corrections to the original gap analysis (per the council)
- **"Streaming transport" was mis-framed as the spine.** The spine is
  production/presentation *decoupling*; SSE/WebSocket is the easy part. The hard
  parts are cadence, spoiler policy, moderation, and the public projection.
- **Cost economics and moderation were under-weighted** (I had them as
  "cross-cutting"). Both are load-bearing and CRITICAL; they gate public launch.
- **Decision B was mis-framed** as open-vs-curated matchmaking. The real
  load-bearing decision is identity/anti-Sybil/ladder trust.
- **Missed entirely:** the public data-contract/versioning problem (D2).
- **Determinism-vs-live was a non-issue:** streaming *committed* hash-chained
  events is deterministic-safe; only streaming unstable state would not be.
- **Confirmed correct:** OpenSkill > Elo (presentation gap, not math);
  deterministic-stats-first with judge-as-enrichment; the four-pillar
  decomposition.
