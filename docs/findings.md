# Findings from the first real-model games

What running Padrino against real models taught us so far. The strongest result is about the harness, not the models: **every failure we found was silent**, and each one produced a plausible-looking game. Model-behaviour observations come second and are explicitly anecdotal.

See [design-decisions.md](./design-decisions.md) (entry 13) for the reliability design that came out of this, and [architecture.md](./architecture.md) for the system map.

## 1. Scope and caveats

Read these before any number below.

- **The datasets are tiny.** The play statistics in this document come from one 10-game batch ("batch 005", section 3) on the fully fixed engine. Earlier runs (an exploratory game, a 4-game shakedown "batch 001", and a first 10-game attempt "batch 002") are used only for the engineering findings in section 2: they ran without a working Doctor (finding 11), so **their play statistics are invalid** and are not reported.
- **Setup.** Seven seats, seat order rotated by one position per game, ruleset `mini7_v1`, unranked showcase mode, all calls through one OpenAI-compatible gateway. The lineup is the cheapest current model from each provider we had access to, plus `claude-sonnet-5-5`: `claude-sonnet-5-5`, `claude-haiku-5-5` (two seats, to fill seven), `gpt-6-luna`, `grok-4.7`, `MiniMax-M3.1-Flash` and `muse-ember-1.0`. Because `claude-haiku-5-5` has two seats, some games had two haiku seats on the same team; results are aggregated per model, not per seat.
- **Model names are as stored in `llm_calls`.** Gateway provider prefixes (`cc/`, `cx/`, `minimax/`, ...) are routing details of one particular gateway and are dropped in prose.
- **Nothing here is a ranking.** n < 30 is anecdotal. Latency, timeout and 5xx rates measure model plus provider plus gateway plus time of day, not the model alone.
- **The shakedown ran across engine versions.** The engine was being fixed between and during batch-001 games (findings 5 to 8). Its failure rates describe the pre-fix engine for part of the traffic and must not be read as current behaviour.
- **Sample sizes needed for real claims** (standard two-sided 5% tests; our arithmetic, not a measurement):
  - A ~10 percentage-point win-rate difference (50% vs 60%) at 80% power needs about **400 games per arm**.
  - Estimating an invalid-output rate to within ±2pp, if the true rate is around 5%, needs about **450 calls per model**.
  - Role, seat and teammate effects in a 7-player game are large, so the 400-game figure is a floor.

## 2. Engineering findings: every failure was silent

Each item is symptom, root cause, fix, lesson. Commit hashes are in this repository (`git show <sha>`). Evidence for items 1 and 2 is the first 7-model game (2026-11-15). Items 3 and 4 were found by a code audit of the real-model path and an A/B check. Items 5 to 8 come from the 4-game shakedown. Items 9 and 10 come from the shakedown plus a code review.

### 1. A dead model became an "AFK" player

- **Symptom.** One seat timed out on every call (45s each) and never spoke. The game finished normally; its coerced no-ops and abstentions looked like quiet play, and that seat's team went on to win.
- **Cause.** The adapter marked the call as failed, but the tick loop only treated a fully `exhausted` result as a failure, so the coerced `NOOP`/`ABSTAIN` was logged like any legitimate move. Timeouts were also not in the retry set.
- **Fix.** `dc8ebf5`: every provider failure is a first-class `ActionTimedOut` event with a fixed reason and error-kind vocabulary; a seat that fails three consecutive times ends the game as no-contest. `4d60984`: showcase preflight probes every distinct model before creating a game and refuses to start if any fails.
- **Lesson.** A coerced action must be distinguishable from a chosen one in the log, and a game with an unresponsive seat is a no-contest, not a result.

### 2. An invalid-JSON vote decided a tied lynch

- **Symptom.** Day 2 ended 1-1-1 and nobody was eliminated. One of the three ballots that could have broken the tie was invalid JSON, silently coerced to abstain; the other abstainer was the dead seat from item 1.
- **Cause.** Invalid output was never repaired or retried; the safe-action coercion (design-decisions entry 8) is correct but was invisible.
- **Fix.** `dc8ebf5`: one re-ask with a JSON-only reminder, and the coercion is logged.
- **Lesson.** "Fail to a safe default" is only safe if the default is visible. A silent abstain is a vote with consequences.

### 3. Inline `<think>` blocks would have scored 100% invalid

- **Symptom.** Found by audit before it showed up in results. A reasoning model that puts `<think>...</think>` ahead of its JSON in the message content failed strict parsing, so every one of its votes would have been an abstain.
- **Cause.** The parser handled only a leading code fence. This was the second instance of the same class: an earlier provider that wrapped JSON in fences hit 100% `invalid_json`.
- **Fix.** `dd349af`: tolerate think blocks, fences and prose around the JSON object.
- **Lesson.** Provider output formats are an open set. Handle them once, in the parser, and test with recorded real replies.

### 4. The gateway was part of the experiment

- **Symptom.** After the first game we found the local gateway had a global "token saver" feature that injects extra style system prompts into every request unless a request opts out by header.
- **Evidence.** One A/B on a single English greeting prompt (one trial per arm, a Gemini flash-lite model): with injection on, the reply came back in Vietnamese and clipped; with the opt-out header, normal English. `claude-haiku-5-5` was roughly unchanged either way.
- **Fix.** `4d60984` sends the opt-out header and adds an English-only language rule for showcase prompts; `d81582f` makes the gateway settings generic. The README tells operators to verify their own gateway.
- **Lesson.** Any proxy in the path is part of the experiment. Verify it is a transparent pipe, or record its configuration, before attributing behaviour to a model.

### 5. The retry budget was dead inside the deadline

- **Symptom.** A model timing out was effectively never retried; a cancelled call left no `llm_calls` row.
- **Cause.** The outer phase deadline equalled the per-attempt timeout (45s), so attempt 2 could never finish. After attempts became deadline-aware, a pilot game showed two more problems: the OpenAI SDK's own retry slept 30s on `Retry-After` inside our budget, and an even three-way split of the deadline (36s per attempt) timed out slow-but-healthy models whose replies took 20 to 35s.
- **Fix.** `dc8ebf5` (per-attempt timeouts share the phase deadline; one row per attempt, including cancelled ones), `8570f6c` (give attempt 1 most of the deadline; disable SDK-internal retries), `709f73b` (outer deadline never shorter than the adapter budget plus persistence slack; a returned reply is never cancelled while being recorded).
- **Lesson.** Two layers of retry with independent clocks will fight each other. Own one deadline and derive every layer from it.

### 6. Overload bursts burned all retries in about 1.5 seconds

- **Symptom.** The two MiniMax models returned `ServiceUnavailableError` in 0.0 to 0.4s each, so three retries were gone in about 1.5s and the seat was coerced.
- **Cause.** Backoff tuned for transient network errors, not for "the cluster is busy, retry after a short wait".
- **Fix.** `709f73b`: overload errors back off from a 2s base, honour a numeric `Retry-After`, and are clamped to the remaining deadline.
- **Lesson.** Fast failure is not the same as retryable failure. Classify errors by what the provider is telling you.

### 7. Reasoning models spent the whole output budget thinking

- **Symptom.** Replies cut off with no answer. In the shakedown, 4 calls ended with `error = "truncated"` after 4096 of 4096 output tokens, all on the two MiniMax models.
- **Cause.** `max_tokens` sized for a chat model; hidden reasoning consumed it. No `finish_reason` was stored, so the cause went undiagnosed.
- **Fix.** `dc8ebf5` records truncation explicitly as `error = "truncated"`; `709f73b` raises the showcase budget to 16384 output tokens.
- **Lesson.** A reasoning model needs a far larger output budget than a chat model, and "empty content" must be diagnosed, not just counted.

### 8. The circuit breaker first mistook truncation for unresponsiveness

- **Symptom.** Shakedown game `b001-g04` ended as no-contest because the seat's truncated replies counted toward the three-strikes breaker. That seat was responding; it was thinking too long.
- **Fix.** `709f73b`: truncated output no longer trips the breaker. `b001-g02` was a correct trip: its seat really was unresponsive at the day-1 vote.
- **Lesson.** A safety mechanism needs its own failure taxonomy. "Slow and over budget" and "not answering" call for different responses.

### 9. "Unpriced = NULL" made every spend cap fail open

- **Symptom.** Gateway-routed subscription providers report no cost. In batch-001 only `gpt-6-luna` had reported costs (32 of 32 calls); the other six models' 219 calls had `cost_usd` NULL.
- **Cause.** Spend caps summed `coalesce(cost, 0)`, so unknown cost counted as free. An earlier fallback price table had the opposite flaw: it invented a price and summed it as if real.
- **Fix.** `709f73b`: unknown models record a conservative catch-all estimate stamped `price_basis = "DEFAULT_ESTIMATE"`. Reports show reported and estimated cost separately, never a combined total.
- **Lesson.** In a cost-governed system, unknown must be pessimistic, and estimates must be labelled as estimates.

### 10. Tolerance is an attack surface

- **Symptom.** Found in review: the tolerant JSON extractor took about 10s on a 20,000-character run of `{` and blocked the event loop; deeply nested output raised `RecursionError` out of a parser documented as never raising.
- **Cause.** Quadratic candidate scanning with no input bounds.
- **Fix.** `709f73b`: 1 MiB raw and 64 KiB scan caps, 32 candidate start positions, linear think-tag stripping, `RecursionError` caught.
- **Lesson.** The more forgiving the parser, the more it must assume hostile input. Model output is untrusted input.

### 11. The Doctor never saved anyone, and 3,500 tests agreed

- **Symptom.** Mafia won all 6 completed games across the shakedown and the first 10-game attempt. Reading the night events: on Night 1 of one game the Doctor protected the player both Mafia targeted, yet that player died and the night result recorded `protected: null`. Across all 15 recorded nights, `protected` was never set.
- **Cause.** The game loop folded each `ProtectSubmitted` event through the reducer, which records `last_protected_target`, *before* resolving the night. The resolver then saw tonight's protect as a repeat of "last night's" and rejected it under the no-consecutive-protect rule. Every protect was invalid, in every ruleset with a Doctor.
- **Why the tests missed it.** Resolver unit tests built state by hand with the correct `last_protected_target`, so the ordering bug only existed in the integrated loop. Full-game tests scripted a Doctor save but asserted only the winner and pinned event-log hashes, so the golden hashes froze the buggy outcome as truth. One test even asserted `protected` was `None`.
- **Fix.** `b026cc1`: resolve the night against the pre-submission state (5 lines). Recorded logs still verify, because `NightResolved` is a recorded payload and replay never re-resolves. Golden hashes were re-pinned to the corrected outcomes, and new full-game tests assert that a save prevents a death.
- **Consequence.** Four recorded nights were mis-resolved (the Doctor protected the actual kill target and the victim died). All play statistics from the shakedown and the first batch attempt are discarded; their availability and latency data stand.
- **Lesson.** The data caught what the tests didn't. A 100% win rate for one faction is a bug signal before it is a finding. Snapshot tests pin whatever the code does today; pair them with semantic invariants ("a successful save prevents a death") checked through the real loop.

### The pattern

The first ten share a shape: a failure is absorbed by a default, the default looks like normal play, and the game completes. The remedy was the same each time: make failure a first-class, countable event; bound every loop by one deadline; prefer no-contest over a misleading result. The eleventh is the same shape one level down: a rule silently rejected every action, and the game still looked like Mafia.

## 3. Model behaviour (anecdotal)

Source: batch 005, run after every fix in section 2. <!-- B005-COUNTS -->10 completed games and 3 no-contests<!-- /B005-COUNTS -->, ruleset `mini7_v1`, seat order rotated per game. Roles differ per seat per game, so none of this separates model skill from role luck. Fewer than 30 games, no confidence intervals, no ranking.

### 3.1 Outcomes and availability

<!-- B005-OUTCOMES -->Town won 7 of 10 completed games and Mafia 3.<!-- /B005-OUTCOMES --> With the Doctor working, Town wins mostly by eliminating both Mafia by Day 3 (`ALL_MAFIA_ELIMINATED`); Mafia wins were on parity. The earlier "Mafia wins everything" pattern was the Doctor bug (finding 11), not model behaviour.

Every no-contest was a seat that stopped answering three turns in a row (finding 1's circuit breaker): `grok-4.7` once (overload errors) and `MiniMax-M3.1-Flash` twice (180-second hangs). The breaker voided those games rather than letting a silent seat skew them, so **availability is reported separately from play**: a model that is often unavailable costs games, which is a property of the model's serving, not its Mafia skill.

### 3.2 Per-model call profile

From `llm_calls`, all attempts in batch 005 including no-contest games. `ok%` is the share of calls with status `ok`; latency is successful calls only; re-asks are second attempts after an unparseable or schema-violating reply.

<!-- B005-CALLS -->
| Model | Calls | ok% | Provider error% | Schema viol.% | Re-asks | p50 ms | p95 ms | Mean output tokens |
|---|---|---|---|---|---|---|---|---|
| `gpt-6-luna` | 130 | 100.0 | 0.0 | 0.0 | 0 | 5782 | 10971 | 268 |
| `muse-ember-1.0` | 168 | 100.0 | 0.0 | 0.0 | 0 | 4215 | 12739 | 1819 |
| `claude-haiku-5-5` (2 seats) | 356 | 97.2 | 0.0 | 2.8 | 10 | 5840 | 11938 | 932 |
| `claude-sonnet-5-5` | 154 | 96.8 | 0.0 | 3.2 | 4 | 5060 | 14848 | 725 |
| `grok-4.7` | 136 | 93.4 | 6.6 | 0.0 | 0 | 21276 | 99554 | 1924 |
| `MiniMax-M3.1-Flash` | 143 | 72.7 | 27.3 | 0.0 | 0 | 27173 | 149364 | 2021 |
<!-- /B005-CALLS -->

No model produced invalid JSON once think blocks and fences were tolerated (finding 3), and no reply was truncated at the 16384-token budget (finding 7). The two Claude models' occasional schema violations triggered a re-ask (14 re-asks for 15 violations). Provider-error rates mix model, provider capacity, gateway and time of day; read them as "how this serving path behaved that night".

### 3.3 Play

Small counts; read as descriptive, not comparative.

<!-- B005-PLAY -->
| Model | Town day votes | …on an actual Mafia seat | Abstain % (all votes) | Detective checks hitting Mafia | Doctor saves (kills prevented / protects) |
|---|---|---|---|---|---|
| `gpt-6-luna` | 20 | 55.0% | 0.0% | 2 of 3 | – |
| `muse-ember-1.0` | 24 | 54.2% | 28.1% | 0 of 1 | 2 / 4 |
| `claude-sonnet-5-5` | 23 | 43.5% | 17.9% | 4 of 8 | 3 / 4 |
| `claude-haiku-5-5` (2 seats) | 36 | 41.7% | 23.8% | – | 2 / 5 |
| `grok-4.7` | 20 | 35.0% | 23.1% | – | 3 / 7 |
| `MiniMax-M3.1-Flash` | 15 | 26.7% | 18.2% | 2 of 3 | 0 / 3 |
<!-- /B005-PLAY -->

A random Town vote lands on Mafia roughly a third of the time (2 Mafia among the 5 or 6 other living players), so most models are only modestly above chance and none is clearly separated at this n. Mafia night kills went to Villagers 65% of the time, Detectives 22% and Doctors 13%; 10 of 26 nights ended with a save.

### 3.4 Moments worth reading

All quotes are public chat, identified by game seed and event sequence in `game_events`.

- **A fabricated Detective claim that worked** (`b005-g04-a1`). Under pressure on Day 3, `claude-sonnet-5-5`, playing Mafia, fake-claimed Detective with invented results: "I'm the investigator. Night 1 I checked P03 and got town. Night 2 I checked P01 and got MAFIA." (seq 125). The accused, `muse-ember-1.0` and actually a Villager, objected: "I know my own alignment, so P05's claimed Night 2 'MAFIA' result on me is impossible" (seq 129). Three Town players voted with the claim as "the only power information on the table", and the Villager was eliminated 4 to 1. The game was later voided as no-contest when another seat's provider went unavailable on Day 4, so the batch's best deception is in a game that does not count.
- **Openers converge.** Nearly every Day-1 opener was a variant of "no information yet, share reads with reasons, don't vote on vibes", e.g. `gpt-6-luna`: "With no information yet, let's avoid locking onto arbitrary suspects." (`b005-g02-a1`, seq 12).
- **Pressure on non-committal players** is the main Day-1 Mafia tool: Mafia seats repeatedly targeted Town players who asked for claims or gave only meta-commentary, and several of those players were later voted out.

### 3.5 Style and language drift are not model findings

The first exploratory game's terse forum-slang dialogue ("scum", "wagon") and one model switching to Chinese are **confounded** by the gateway prompt injection found afterwards (finding 4). With the opt-out header and English rule on, a crude non-English heuristic (more than 20% non-ASCII letters) flagged 0 of 801 public messages in batch 005, and messages averaged 314 to 475 characters per model.

### 3.6 The first exploratory game (pre-fix, invalid for play statistics)

Kept for its story, not its numbers. A different lineup, the gateway injection active, and no working Doctor. One Mafia model (`kimi-k3`) carried its team alone because its partner's provider timed out on every call (finding 1): it steered a Day-1 mislynch, killed the loudest Town voice, and framed a Town player with true information. The raw database was lost to a temp-directory cleanup, so these notes cannot be re-checked.

## 4. How to reproduce

```bash
export GATEWAY_API_KEY=...        # never paste a key into a command or file
uv run padrino showcase \
  <model-1> <model-2> <model-3> <model-4> <model-5> <model-6> <model-7> \
  --base-url http://127.0.0.1:4000/v1 \
  --key-ref env:GATEWAY_API_KEY \
  --seed my-game-01 \
  --db-url sqlite+aiosqlite:///./padrino-showcase.db
```

- **Seat order is the argument order.** The batches reused one seven-model list and rotated it by one position between games, so each model sat in a different seat from game to game, and each game got its own `--seed`.
- **`--seed`** fixes role assignment and seating at the engine level. It does not fix model replies, so the same seed does not reproduce a transcript. Replaying a stored log does (design-decisions entry 1).
- **Preflight** probes each distinct model once and creates no game if any fails. A tripped seat breaker ends a showcase game as `FAILED`.
- **Before any batch, check the gateway** for prompt rewriting (see the README showcase section).
- **Where results live.** One SQLite file. `llm_calls` has one row per attempt (`status`, `error`, `latency_ms`, `input_tokens`, `output_tokens`, `cost_usd`, `price_basis`); `game_events` is the hash-chained event log; `games` has `status`, `terminal_result` and `last_error`. For example:

```bash
sqlite3 padrino-showcase.db \
  "select status, count(*), round(avg(latency_ms)) from llm_calls group by status"
```

## 5. Batch 005 games

<!-- B005-GAMES -->
| Seed | Status | Winner | Reason | Days | Events | Calls | No-contest cause |
|---|---|---|---|---|---|---|---|
| `b005-g01-a1` | COMPLETED | MAFIA | PARITY_REACHED | 3 | 141 | 92 | - |
| `b005-g02-a1` | COMPLETED | TOWN | ALL_MAFIA_ELIMINATED | 3 | 140 | 83 | - |
| `b005-g03-a1` | COMPLETED | TOWN | ALL_MAFIA_ELIMINATED | 3 | 136 | 80 | - |
| `b005-g04-a1` | FAILED | - | - | 4 | 163 | 110 | seat_unresponsive: seat P03 stopped responding |
| `b005-g04-a2` | COMPLETED | TOWN | ALL_MAFIA_ELIMINATED | 3 | 132 | 75 | - |
| `b005-g05-a1` | COMPLETED | MAFIA | PARITY_REACHED | 2 | 113 | 72 | - |
| `b005-g06-a1` | COMPLETED | TOWN | ALL_MAFIA_ELIMINATED | 3 | 149 | 88 | - |
| `b005-g07-a1` | COMPLETED | TOWN | ALL_MAFIA_ELIMINATED | 3 | 135 | 79 | - |
| `b005-g08-a1` | COMPLETED | TOWN | ALL_MAFIA_ELIMINATED | 3 | 154 | 96 | - |
| `b005-g09-a1` | COMPLETED | TOWN | ALL_MAFIA_ELIMINATED | 3 | 139 | 82 | - |
| `b005-g10-a1` | FAILED | - | - | 3 | 134 | 99 | seat_unresponsive: seat P01 stopped responding |
| `b005-g10-a2` | FAILED | - | - | 2 | 78 | 59 | seat_unresponsive: seat P01 stopped responding |
| `b005-g10-a3` | COMPLETED | MAFIA | PARITY_REACHED | 2 | 116 | 72 | - |
<!-- /B005-GAMES -->

Earlier batches (001 shakedown, 002 first attempt, and short 003/004 lineup wrap-ups) are kept in the raw data for availability and latency analysis only.

## 6. Open questions and next experiments

- **Regression coverage.** Turn the shakedown failure classes (dead seat, overload burst, truncated reasoning, inline think block) into fault-injection tests against a mock adapter, so regressions are caught without a real gateway.
- **Seat and role effects.** At 7 players and 2 Mafia, win rate is dominated by role assignment. A balanced design (each model in each role equally often) is needed before any per-model claim, which is the 400-game scale in section 1.
- **Gateway transparency.** Record the gateway's effective configuration, or a hash of the final outgoing request, in each `llm_calls` row so a rewriting proxy is detectable from the data alone.
- **Reasoning budget.** Is 16384 output tokens enough for every reasoning model, and does a larger thinking budget change play or only latency?
- **Invalid-output rate with the re-ask on.** Measure first-attempt and post-re-ask invalid rates separately.
- **Style confound.** Repeat an English-rule-on versus English-rule-off comparison on a verified transparent gateway to see whether any of the first game's slang and language drift survives.
- **Dead-seat counterfactual.** How much does an unresponsive seat shift win rate if it plays on? The current answer, invalidate the game, avoids measuring it.
