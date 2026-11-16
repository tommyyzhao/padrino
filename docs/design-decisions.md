# Design decisions

Short records of the choices that make Padrino usable as a benchmark. Each entry lists the context, the decision, and the consequences, with the code that implements it. See [architecture.md](./architecture.md) for the surrounding system.

## 1. Pure deterministic core with a seeded RNG

**Context.** A benchmark is only credible if anyone can re-run a game and get the same result. Hidden clocks, global RNGs, or database reads inside the rules make that impossible.
**Decision.** Everything under `src/padrino/core/` is pure: no DB, network, wall clock, `random` or `secrets`. Randomness comes from `SeededRng` (`core/engine/rng.py`), which expands `sha256(seed + counter)`. Per-game seeds derive from the gauntlet seed with SHA-256 (`gauntlets/scheduler.py:derive_game_seed`), and seat order from `seat_permutation` (`core/seating.py`). `tests/core/test_purity.py` parses every core module and fails on forbidden imports.
**Consequences.** Replays are portable across machines and Python versions. The cost is ceremony: anything impure (IDs, timestamps, providers) must be injected from outside, and the core is synchronous while the shell is async.

## 2. Event sourcing with a hash chain

**Context.** Results from many runs, and from other people's machines, need tamper evidence and a canonical record.
**Decision.** Game state is a fold over an append-only event log (`core/engine/reducer.py:apply_event`). Each event is sealed as `sha256(prev_event_hash + canonical_json(body))`, excluding `event_hash`, `prev_event_hash` and `created_at` (`core/engine/hashing.py:22`). Canonical JSON sorts keys, strips whitespace, and rejects floats and non-string keys (`core/engine/canonical_json.py:14`). `replay_event_log` (`core/engine/replay.py:66`) re-seals a stored log and raises at the first mismatching sequence.
**Consequences.** Any tampering is detectable, exports and ingestion re-verify the chain (`export/bundle.py:337`), and restores can be checked (`padrino game verify-restore`). Any change to an event schema or to canonical bytes changes hashes, so changes are deliberate; `tests/integration/test_canonical_byte_stability.py` pins golden hashes for full games.

## 3. Chat never triggers mechanics

**Context.** Models should be free to lie and bluff in prose. If prose could cast a vote or name a kill, a stray sentence would change the game.
**Decision.** Every response has chat fields (`public_message`, `private_message`) and a separate structured `action` (`core/agents/contract.py:32`). `_submission_events_for` (`runner/game_runner.py:1007`) emits mechanical events only from `action`, and the resolvers read only those events. Each resolver has a `test_chat_field_is_never_read` test (for example `tests/core/test_day_vote.py:192`).
**Consequences.** Models must produce two channels at once, and malformed structured output has to be handled (decisions 8 and 9). In return, a model can never be penalised or rewarded for how it phrases things.

## 4. Frozen-response replay

**Context.** Provider models change and are not reproducible, but a benchmark record should be.
**Decision.** Each model call is archived in `llm_calls` with the request, raw response, parsed response, status, tokens and cost (`runner/game_runner.py:860`, `db/models.py:517`). The engine consumes `AgentResponse` values through the same adapter protocol whether they come from a model or a script, and `DeterministicMockAdapter` (`llm/mock.py:28`) replays scripted responses keyed by `(phase, seat)`. The golden-hash tests drive full games this way and compare the final event hash and a digest of the whole chain against pinned values (`tests/integration/test_canonical_byte_stability.py:261`).
**Consequences.** What is guaranteed today: given the same seed and the same responses, the engine produces the same event log, and replay of a stored log never calls a model. What is not built: there is no command that re-drives a completed game from its `llm_calls` rows. Replay reproduces the transcript, not the model.

## 5. Anonymity modes fail closed

**Context.** If a model can see another model's name, or a spectator can see a hidden role, results and games are compromised.
**Decision.** Ranked observations are checked before dispatch by `assert_ranked_observation_safe` (`core/observation_privacy.py:151`) against a shared deny list of keys (`FORBIDDEN_PAYLOAD_KEYS`, line 89). Spectator views reuse the same list (`core/spectator_projection.py`). Identity mode is coerced by `coerce_identity_mode` (`core/observation_privacy.py:305`): a missing, null or unrecognised value becomes `ANONYMOUS`; only an explicit `TRANSPARENT` opts out. Guards cover database columns as well as payload keys, and disclose composition as counts only.
**Consequences.** A bug in mode propagation hides information instead of leaking it, at some cost in UI detail. A check that trips in ranked mode aborts the tick, because it indicates an engine bug.

## 6. Clone gauntlets, OpenSkill, provisional thresholds

**Context.** A single game is noisy, and seat position and faction (Town vs Mafia) carry large luck components.
**Decision.** A gauntlet plays the same roster across many games, rotating which roster slot sits in which seat (`gauntlets/tournament.py:92`, `core/seating.py:22`). Ratings use OpenSkill Plackett-Luce with `mu=25`, `sigma=25/3` and a conservative score `mu - 3*sigma` (`ratings/openskill_service.py:42-52`), with global and per-faction scopes. Wilson 95% intervals are in `core/statistics.py`. A build is provisional until it has 30 games, 15 as Town and 5 as Mafia (`gauntlets/completion.py:52-54`); the public leaderboard also applies a simpler default threshold of 10 games (`ratings/provisional_and_decay.py:11`). Every rating is stamped with `ruleset_id` so each ruleset has its own leaderboard.
**Consequences.** Rankings take many games to settle and are flagged until they do. Team outcomes are shared inside a faction, so individual skill within a team is not separated.

## 7. Ranked vs unranked segregation

**Context.** Mixing casual, human, and demo games into the scientific ratings would make the benchmark meaningless.
**Decision.** `run_game` takes an explicit `ranked` flag and writes ratings only when it is true and a league is bound (`runner/game_runner.py:449`). `padrino showcase` always runs `ranked=False` and anonymous (`showcase.py:352`). Private lobbies reject ranked configuration with `ranked_lobbies_deprecated` (`api/routes/lobbies.py:503`), so human lobbies are always casual. Human ratings, where enabled, live in separate tables (`human_rating`, `human_rating_event`), sit behind a server kill switch that defaults off (`padrino_ranked_human_rating_enabled`, `settings.py`), and never write scientific `ratings` or `rating_events` rows.
**Consequences.** Three tiers (scientific, human, casual) multiply the test surface, but the benchmark tables only ever contain benchmark games.

## 8. Safe-action coercion, and why it is visible

**Context.** Models time out, emit invalid JSON, or choose illegal actions. The game must continue, and the failure must not be hidden.
**Decision.** On timeout, parse failure, schema violation, exhausted retries or an illegal action, the seat is coerced to a safe action: abstain on the day vote, no-op elsewhere (`core/agents/coercion.py:29`). The coercion is recorded as an `ActionTimedOut` or `OutputInvalid` event with the reason and the action it defaulted to (`runner/tick.py:230`), and counted by a metric (`record_invalid_action`). The leaderboard reports per-model timeout and invalid rates (`leaderboards/service.py:392`).
**Consequences.** A broken model cannot crash or stall a game, but its handicap is part of the permanent log and its own statistics rather than a silent default. Coercion depends only on the phase, not on the cause.

## 9. Tolerant JSON extraction

**Context.** Real models wrap their answer in `<think>` blocks, Markdown fences, or prose. Rejecting those would measure formatting habits instead of play.
**Decision.** `parse_agent_response` (`core/agents/contract.py:116`) first tries the whole text as JSON. If that fails it calls `extract_json_object` (line 89): strip closed `<think>`/`<thinking>` blocks (an unclosed block is dropped up to the next `{`), then scan for the first balanced `{...}` span, honouring string literals and escapes, skipping spans that are not valid JSON objects. It never raises; it returns an `AgentResponse` or a tagged `ResponseError` (`INVALID_JSON` or `SCHEMA_VIOLATION`). The result must still pass the strict schema and the legal-action check.
**Consequences.** Format noise no longer costs a model its turn. Extraction is deliberately simple: reasoning text containing braces can still confuse it (noted in code), and then the seat is coerced under decision 8.

## 10. Broadcast gate and spoiler safety

**Context.** Spectators should be able to watch games live without learning hidden roles, and nothing should be broadcast that has not passed moderation.
**Decision.** Public reads of a non-terminal game go only through `project_events_for_spectator` (`core/spectator_projection.py:100`): PRIVATE and SYSTEM events are dropped whole, and forbidden keys such as `role` and `faction` are stripped from PUBLIC payloads. The same rule applies to `GET /games/{id}/events?visibility=public` (`api/routes/games.py:272`). A game is served by `/public/games/{id}/live` only if `is_broadcastable` is set and its broadcast state is `LIVE` or `RECENT` (`api/routes/public.py:1183`; `public/broadcast_index.py`). The index hides the result of a `LIVE` game. The moderation gate `is_broadcastable` (`public/moderation.py:201`) is fail-closed: no guard model, a guard error, or a filter hit all mean "not broadcastable". The local showcase skips moderation for its own SQLite database and says so in a comment; public deployments must not.
**Consequences.** Live views are slightly delayed by cadence pacing and show less than the raw log. Roles appear only after the game ends.

## 11. Loopback-only TTS with server-derived text

**Context.** Narration is a useful demo feature, but a speech endpoint that accepts arbitrary text is an open synthesis proxy, and one that depends on cloud keys is hard to ship.
**Decision.** `GET /public/games/{id}/speech/{sequence}` (`api/routes/speech.py`) is off unless `PADRINO_TTS_PROVIDER` is set, answers only to loopback clients (a forwarded client address must also be loopback, line 296), and takes no text from the request. The text comes from the identity-stripped public projection of the committed event, voices are picked by seat number only, never by model, provider or role, and the broadcast gate runs again on every request including cache hits. Text is capped at 1,500 characters (`MAX_TEXT_CHARS`), and the macOS `say` path receives it on stdin, never in argv.
**Consequences.** It is a local, single-machine feature (macOS `say`, GCP or ElevenLabs), not a public service. Narration cannot leak anything the public frame does not already show.

## 12. SQLite first, Postgres supported

**Context.** A benchmark should be easy to try on a laptop, but shared deployments need concurrency and durability.
**Decision.** The engine is written to the SQLAlchemy 2 async API and picks the dialect from the URL (`db/base.py:29`). SQLite (`sqlite+aiosqlite`) is the default for local use and the showcase; Postgres (`postgresql+asyncpg`) is the target for the Docker Compose stack. Dialect differences are kept small: `SKIP LOCKED` row claims on Postgres (`db/repositories/gauntlets.py:129`), a database-backed rate-limit store when running several workers on Postgres (`api/app.py:75`), and pool sizing. There is no Redis and no message queue; the database is the work queue and each role (API, scheduler, human lane) is its own process.
**Consequences.** The same migrations run on both. SQLite is effectively single-writer, so multi-replica deployments should use Postgres. Tests that need a real Postgres are opt-in (`postgres` marker).

## 13. Engine reliability: loud failures, bounded retries, one re-ask, seat circuit breaker

**Context.** In the first real multi-model game, one model timed out on every call and silently became an "AFK" seat whose coerced abstentions looked like play. Another seat's unparseable vote became an abstain that decided a tied lynch. Earlier, a provider that wrapped JSON in code fences scored 100% invalid output without anyone noticing. Every one of these failed *quietly* into a plausible-looking game.
**Decision.**
- Transient provider errors (timeout, 5xx gateway, rate limit) are retried; permanent ones (not found, auth, bad request) are not (`llm/retry.py`). Overload errors (503, 502, 429) back off from a 2s base and honour a numeric `Retry-After`; no sleep or new attempt is allowed to eat the last `max(2s, 10%)` of the budget, since that would be a wasted billed call. Each attempt gets a share of the phase deadline, so retries actually fit inside it (`llm/litellm_adapter.py`). The runner's outer deadline is never shorter than the adapter's own budget plus persistence slack (`effective_phase_timeout_s`), and recording a reply that already came back is not cancellable.
- Every provider-side failure is a first-class `ActionTimedOut` event with a fixed reason vocabulary (`TIMEOUT`, `PROVIDER_ERROR`) and a fixed error-kind vocabulary (`TIMEOUT`, `RATE_LIMITED`, `UNAVAILABLE`, `CONNECTION`, `AUTH`, `NOT_FOUND`, `BAD_REQUEST`, `OTHER`), never provider or model text or raw exception class names, so the log stays identity-blind (`runner/tick.py`).
- An unparseable or schema-violating reply gets one re-ask with a JSON-only reminder (`padrino_llm_reask_invalid`, default 1). Output truncated by reasoning tokens is recorded as `error="truncated"`, not as generic invalid JSON, and does not count toward the unresponsive-seat breaker. Parsing of model text is bounded (64 KiB scan window, 32 candidate starts, linear think-tag stripping) so a hostile reply cannot stall the event loop.
- Every attempt, including deadline-cancelled calls, gets its own `llm_calls` row.
- Three consecutive provider failures on an AI seat end the game as no-contest (`padrino_llm_seat_failure_limit`, default 3, 0 disables; `runner/game_runner.py:1547`). Human seats are never counted. Outside the showcase, a tripped breaker marks the game failed (`last_error_kind = "provider_no_contest"`, never retried by the scheduler), and a tournament carries on with its next game.
- Models with no listed price and no provider-reported cost record the conservative catch-all estimate in `cost_usd` so spend caps stay fail-closed, stamped `price_basis = "DEFAULT_ESTIMATE"` (never `FALLBACK_TABLE` or `PROVIDER_RESPONSE_COST`; `economics/human_cost_governance.py:64`).
**Consequences.** A flaky model now costs a few extra calls instead of silently skewing results, and a dead one invalidates the game rather than handing its team a handicap. Replay is unaffected: only the final action enters the event log. Estimated costs are never presented as real: the showcase summary reports `cost_reported`, `cost_estimated` and `estimated_calls` separately and offers no combined total.
