# Padrino v4 — The Playable Open Model Social-Deduction Platform

## 1. Product outcome

Waves 15 and 16 turn Padrino from a strong deterministic league engine with a
functional play harness into a consumer-quality social-deduction game and a
credible human/AI research platform.

Wave 15 ships the complete casual product: truthful multi-human lobbies,
OpenRouter model discovery and selection, player-funded and promotional-credit
inference, configurable role decks, a 20–30 minute paced game, and a cohesive
neo-noir occult-laboratory interface with sleek technical instrumentation.

Wave 16 ships ranked products on top of that foundation: standardized ranked
solo human-vs-AI, season-scoped AI-only scientific OpenRouter campaigns, and a
strangers-only mixed human/AI ranked queue with calibrated ratings, integrity
controls, reports/blocks, shadow evaluation, and staged activation.

The executable, dependency-ordered plan for both waves lives in
`ralph/prd.json` and runs on one branch through one Ralph loop.

## 2. Locked product decisions

1. Casual custom play may use any text-generation model returned by OpenRouter.
   Models are visibly tiered as unverified, playable, verified-structured, or
   ranked-qualified; unreliable models remain casual-only.
2. A custom-lobby host funds every AI seat through either a connected OpenRouter
   account or Padrino promotional credit.
3. OpenRouter connection uses OAuth PKCE when possible. Any server-held player
   credential is encrypted at rest, write-only, never logged, never placed in
   browser storage, and never enters events, replay exports, or analytics.
4. Promotional credit is a one-time USD 5.00 grant per verified account.
   Admission reserves conservatively. Once admitted, a game finishes without
   changing models; a tightly bounded operator completion reserve absorbs an
   estimate miss without making the player balance negative.
5. Normal casual games target 20–30 minutes using several short, deterministic
   conversation cycles per day plus phase-specific deadlines.
6. The visual identity combines neo-noir occult mystery with a sleek futuristic
   AI laboratory: atmospheric and dramatic, but legible, accessible, and
   technically informative.
7. Custom hosts choose the role deck, not a particular player's secret role.
   Role assignment remains seeded, hidden, and replayable. Custom decks are
   always casual.
8. Duplicate AI models are allowed in casual custom games and visibly disclosed
   at reveal. Ranked and scientific pools control duplicates server-side.
9. Ranked opponents, model routing, role decks, and cadence are selected by the
   server from immutable season snapshots. Players never cherry-pick ranked
   opponents or roles.
10. Ranked parties are out of scope initially. Mixed ranked is a solo-entrant,
    strangers-only queue; private/invite lobbies remain casual.
11. Human leaderboards expose one headline rating per ranked mode, season, and
    ruleset, with role/faction diagnostics. Role-specific public ladders wait for
    adequate sample sizes.
12. AI-only scientific ratings, ranked-human ratings, and casual game records
    remain separate products and separate write paths.

## 3. Game modes

### 3.1 Casual Quick Play

One human enters an anonymous table immediately. The server chooses a
budget-compatible, low-latency roster. The player may use connected OpenRouter
funding or promotional credit. The result updates personal casual statistics
but no rating.

### 3.2 Custom Lobby / Laboratory

The host chooses the supported role deck, cadence preset, identity mode, theme,
human/AI seat mix, and the OpenRouter model assigned to every AI slot. Humans
join by invitation and each occupy exactly one real game seat. The lobby shows
human members and selected model roster before launch, but never binds either
to in-game public seat identity before reveal. Custom games are always casual.

### 3.3 Ranked Solo Human vs AI

A verified, consented, non-banned solo entrant receives a server-selected role
and a balanced schedule drawn from the active season's frozen AI pool. Models,
providers, rules, and cadence cannot be selected by the player. Ratings are
season/ruleset scoped and calibrated for role/faction difficulty and frozen AI
anchor strength.

### 3.4 Ranked Mixed Human + AI

Verified solo entrants join a public queue. The matchmaker creates a table when
the configured human threshold is reached, then fills remaining seats from the
season AI pool. Human and AI identities remain hidden until reveal. Rating uses
the actual faction teams, human ratings, frozen AI anchors, role calibration,
and explicit no-show/disconnect/forfeit rules. Parties and invite seating are
not permitted.

### 3.5 Scientific AI League

AI-only gauntlets and campaigns continue to own the canonical scientific
`Rating` / `RatingEvent` tables. OpenRouter expands the eligible model field,
but scientific requests freeze model slug, provider endpoint, routing policy,
prompt/schema hashes, inference parameters, and catalog snapshot. Provider
fallback is disabled unless an experiment explicitly defines and labels it.

## 4. Determinism and persisted configuration

Every launched game freezes a hash-addressed `GameRulesSnapshot` containing the
role multiset, faction mapping, win-condition identifiers, message/token limits,
discussion-cycle count, and cadence profile. Built-in rulesets project to the
same snapshot type. A custom role deck produces `custom:<sha256>` and persists
the complete canonical snapshot on the lobby/game; replay never needs the live
catalog or database to infer old rules.

Every AI seat freezes an `AgentRuntimeSnapshot`: requested OpenRouter canonical
slug, catalog snapshot id, prompt/schema hashes, inference parameters, routing
preferences, qualification tier, funding source, and credential owner reference
(never credential material). Runtime responses archive actual model/provider,
OpenRouter generation id, router-attempt metadata, tokens, cost, and status.

## 5. OpenRouter policy

The catalog is synchronized into immutable snapshots and exposed through a
search/filter API. Casual selection supports every text model, but the UI shows
compatibility and price risk. Platform-funded play is limited to models whose
conservative whole-game estimate fits available credit and operator policy.
BYOK players may choose more expensive models after an explicit maximum-spend
confirmation.

Human chat requests enforce Zero Data Retention where available. Ranked and
scientific calls require the parameters they declare, record router metadata,
and use explicit provider policies. Scientific calls disable provider fallback
by default; casual calls may optimize for price or latency within the frozen
lobby policy.

## 6. Money and credential invariants

- Monetary state is stored as integer USD micros, never binary floats.
- Grants, reservations, charges, releases, reconciliation adjustments, and
  completion-reserve spend are append-only ledger entries with idempotency keys.
- A verified account receives the promotional grant at most once under races.
- Admission reserves before game creation. Actual calls atomically consume the
  reservation; unused value is released at terminal settlement.
- Estimate misses may consume only the configured operator completion reserve.
  They never make a player balance negative and never swap the selected model.
- BYOK calls never consume promotional credit. Platform calls never use another
  principal's credential or reservation.
- Raw credentials are excluded from logs, exceptions, API responses after the
  one-time exchange, DB reprs, exports, analytics, events, and frontend state.

## 7. Ranked rating design

Wave 16 introduces independent rating scopes:

- `SOLO_HUMAN_VS_AI`
- `MIXED_HUMAN_AI`

Both are keyed by season and ruleset. Each season freezes an AI pool plus
calibrated, read-only AI anchor ratings and role/faction baselines. Human-game
rating code receives these values as snapshot data; it never reads or writes
scientific `Rating` / `RatingEvent` rows.

Solo updates compare the human's result against the expected result for their
role, faction, and scheduled AI table. Mixed updates use OpenSkill team math
over humans plus frozen AI anchors, then apply tested role calibration and
participation/forfeit rules. Only human rows are updated. Rating events persist
all inputs needed for byte-identical recomputation.

Any rollback in mixed ranked must recompute the affected season/ruleset stream
in canonical game order. Per-principal local reversal is insufficient once
human opponents influence later priors.

## 8. Interface principles

- The home screen begins with three unmistakable choices: Quick Play, Custom
  Lobby, Ranked.
- Model selection is a visual searchable catalog with provider/model identity,
  context, structured-output compatibility, latency/throughput when available,
  price per million tokens, estimated game cost, privacy tier, and qualification
  badges.
- Custom setup is a table builder: human invitation slots and AI model cards
  occupy visible pre-game positions, while in-game public identities are
  reshuffled and hidden.
- The role-deck builder uses supported role cards, validates composition live,
  explains win conditions, and never assigns a chosen player a chosen role.
- The play table emphasizes portrait, name, alive/dead state, chat, vote motion,
  role card, phase drama, and clear actions. Scientific metadata stays available
  in a secondary inspection layer rather than dominating play.
- Motion and sound are optional, respect reduced-motion and mute preferences,
  and never carry hidden information.
- Mobile is a primary layout, not a collapsed desktop table. Keyboard and screen
  reader paths receive equivalent game state and controls.

## 9. Wave boundaries

Wave 15 is complete only when a real two-or-more-human custom game can be
configured with named OpenRouter models and a custom supported-role deck,
funded through either path, played to terminal on desktop and mobile, revealed,
settled, replayed byte-identically, and proven to write zero ranked/scientific
ratings.

Wave 16 may begin in the shared Ralph queue only after the Wave 15 gate story is
green. Wave 16 is complete only after ranked solo and mixed queues pass shadow
calibration, load, integrity, rollback, privacy, and end-to-end gates. Production
flags remain off until an operator explicitly executes the go/no-go checklist.

## 10. Explicit non-goals through Wave 16

- Native iOS/Android applications
- Cash prizes, wagering, or paid tournaments
- Ranked parties or invite-based ranked games
- Arbitrary user-authored executable roles or win-condition code
- User-authored model system prompts in public/ranked play
- Voice or video chat
- Runtime-generated avatars
- Automated cheating penalties without human review
- Merging scientific AI ratings into human ratings
- Redis or another queue/cache dependency
