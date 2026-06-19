# Padrino v3 — Human Multiplayer & Community Layer

Lineage: `prd.md` (v1 backend benchmark) → `prd-v2.md` (v2 public spectator website) → **`prd-v3.md` (v3 human multiplayer).**
Executable plan: `ralph/prd-v9.json` (Wave 9). Pre-run checklist: `ralph/PREP-v9-human-multiplayer.md`.

> This is the **trimmed "prove-the-fun" v1** of human multiplayer, after a 4-round product interview and a 3-model adversarial review (GPT-5.5, Claude Opus 4.6, Gemini-flash) that unanimously judged the original 15-epic scope **too big**. The full ambition is preserved as **deferred fast-follows** (§6); v1 is the smallest slice that proves humans enjoy playing Mafia against the models.

---

## 1. Vision

Turn Padrino from a public **scientific LLM benchmark** into *also* a **playable, multiplayer social-deduction community** — humans play with/against the models in **private friend lobbies**, default **anonymous** ("guess who's the AI"), with a Town-of-Salem-flavored anachronistic aesthetic — **without ever compromising the benchmark.**

Organizing principle: **three strictly-separated rating contexts.**
1. **Scientific benchmark** — model-vs-model only. *Untouched/sacred.* Zero human-game writes.
2. **Humans-Included League** — separate for-fun ELO; humans + opted-in models. *Schema built, dormant in v1.*
3. **Human personal ELO** — inside (2). *Dormant in v1.*

---

## 2. The 20 locked product decisions

(From the product interview; full rationale in chat history. v1-relevant deltas noted.)

1. Fully segregated benchmark; separate Humans-Included League ELO. **(v1: schema only, dormant.)**
2. MVP = private friend lobbies (invite, multi-human, AI auto-fill).
3. Default **anonymous** "imitation game"; transparent as a per-lobby toggle. **(v1: anonymous-default ships; transparent polish deferred.)**
4. Guest quickplay + optional sign-in. **(v1: guest + one OAuth provider for stat persistence; friends-graph/blocking/merge cut.)**
5. Hybrid buffered real-time discussion. **(v1: simplified to a fixed release delay applied symmetrically to humans AND AI — no seeded jitter / multi-message windows yet.)**
6. Formal scored spot-the-AI. **(v1: keep the guess + reveal + personal detection-accuracy stat; competitive Turing leaderboards deferred.)**
7. Show composition counts, hide identities until endgame.
8. Mode-dependent avatars. **(v1: static pre-generated themed sprite library, anonymity-safe; runtime generation pipeline deferred.)**
9. Reconnect grace → silent AI takeover; ranked-penalty hooks dormant.
10. v1 always casual; ELO infra designed-now-dormant.
11. Host may pre-pick exact models, else curated auto-fill; exact models + human seats **always revealed at endgame**.
12. **Block-before-release** live moderation in the buffer hold window — **hardened** fail path (deterministic backstop, never halts the game).
13. Coherent per-game theme packs (1930s Noir / Retro-Future Robots / Victorian Gothic…). **(v1: static library; gemini-3.1-flash-image used offline at authoring time, not at runtime.)**
14. Free with caps, platform-absorbed.
15. Reuse the 4 existing roles on existing rulesets.
16. Live identity-blind spectating. **(Deferred to fast-follow — rides the live-transport spine but is not on the fun-validation critical path.)**
17. One-tap combined "agree to Terms & Privacy + 16+" gate on first action.
18. Minimum age 16+.
19. Budget: Moderate ($500–$2k/mo); caps + circuit breakers sized to it.
20. BYOK for your own private games first; pooled sponsorship deferred. **(v1: only the `funding_source` accounting column ships; full BYOK/KMS deferred.)**

---

## 3. Architectural truths the build must honor

1. **Live streaming is net-new.** Today's `/public/games/{id}/live` SSE is *post-hoc paced replay of a finished game*; `mark_live` only runs after completion. Live two-way transport for an in-progress game is a foundational milestone, not a story.
2. **Transport = SSE-out + authenticated POST-in** (fits the adapter-static SPA; adequate for buffered, turn-cadenced play) — *not* a raw WebSocket/Redis backplane.
3. **Human chat is stored OUT-OF-BAND of the hash chain.** Chat text lives in a sidecar keyed by sequence; the core event carries only a reference/hash. This is GDPR-erasure-safe (redact the sidecar, the chain stays valid) *and* aligns with the existing chat-firewall (chat was never part of deterministic mechanics/replay).
4. **Human games run on a separate worker lane with durable, rehydratable state.** Human phases last minutes–hours; they must not hold slots on the benchmark scheduler (concurrency cap 3, sized for ~45s turns), and a process restart must not lose a live game (the current runner is in-memory).
5. **Timing symmetry.** Anonymity requires the release delay to apply to AI messages too — AI chat waits in the same buffer before hitting the chain.
6. **Anonymity & segregation are CI-enforced invariants.** One canonical composition-count function; `FORBIDDEN_PAYLOAD_KEYS` extended with human markers; a DB-column-level projection guard (new identity *columns* bypass the payload-key guard); guards **fail closed → ANONYMOUS**; a property test and a zero-scientific-write segregation test gate every PR.
7. **Pure-core hard rule preserved.** All randomness/clocks/jitter live in the impure shell; new core modules (events, scorer, composition fn) stay pure and pass the purity-firewall test.
8. **Cost breaker throttles NEW lobbies; never kills an active game.** "AI-only continuation" must not boot live humans.

---

## 4. v1 scope ("prove the fun")

The core loop, end to end:

> **join via invite link → (guest or optional sign-in) → one-tap consent → ready up → curated cheap-model auto-fill of empty seats → play (vote / night actions / buffered chat, anonymous) → endgame reveal (always shows models + which seats were human) → personal spot-the-AI guess + your detection-accuracy stat.**

Supporting systems in v1: minimal identity (guest + one OAuth provider) · durable human-game state on a separate worker lane · SSE+POST live transport · block-before-release moderation (hardened) · simple reconnect-grace + AI takeover · cost caps that throttle new lobbies · static themed sprite library · per-player stats captured (no leaderboards) · anonymity + segregation CI gates.

---

## 5. Build phases (→ `ralph/prd-v9.json`, US-121…US-157)

- **P0 — Foundations & invariants** (US-121–126): GameSeat `seat_kind`/human-occupant + `SeatTakenOver` event, out-of-band chat sidecar, anonymity guard + fail-closed default, dormant sibling `human_rating` + segregation guard, `IdentityMode` + canonical composition-count fn.
- **P1 — Identity, consent, durable state** (US-127–132): principals + sessions + `get_human_context`, guest quickplay, one OAuth provider, one-tap consent + 16+, durable rehydratable game state, separate human worker lane.
- **P2 — Live transport spine** (US-133–136): live-tail SSE for in-progress games, authenticated POST action + chat channels, per-seat observation stream + phase-deadline frame.
- **P3 — Human-play loop + moderation** (US-137–140): `HumanAdapter`, human-aware tick + symmetric release delay, mixed human+AI + replay-determinism test, block-before-release moderation (hardened) + out-of-band persistence.
- **P4 — Disclosure, endgame, stats** (US-141–146): mode-aware observation/projection, composition counts, endgame reveal, spot-the-AI guess + personal stat, per-human stats, anonymity+segregation CI gates.
- **P5 — Lobbies, takeover, cost, sprites, frontend** (US-147–157): lobby tables + create/config, invite/roster/ready/presence, deterministic auto-fill + launch handoff, reconnect + AI takeover, cost governance, static sprite library, frontend transport client + lobby UI + in-game surface + endgame/guess/profile UI, end-to-end human-game smoke test.

---

## 6. Deferred fast-follows (retention-gated; NOT in v1)

Friends graph / blocking / guest→account merge · competitive leaderboards + ELO activation (all three contexts) · live identity-blind spectating + account-linked replays · transparent-mode polish · full buffered-cadence engine (seeded jitter / multi-message `hcad7_v1`/`hcad10_v1`) — only if the simple symmetric delay proves insufficient · richer Town-of-Salem roles · anti-cheat telemetry · BYOK/sponsorship (only the `funding_source` column ships in v1) · runtime image-generation pipeline (the static library covers v1).

---

## 7. Known open items / accepted risks

- Out-of-band coaching via any future spectating (a friend who knows your seat can coach over Discord) is structurally unpreventable for casual play — accepted; a ranked-phase blocker.
- Concrete cap numbers (per-user/day games, per-lobby USD, hold-window latency budget ms, human phase deadlines s) are **human-set** at deploy — see `PREP-v9`.
- Age-gate is self-asserted (band, not verified DOB).
- v1 withholds the seat map server-side (trust the server), not via commit-reveal cryptography.
