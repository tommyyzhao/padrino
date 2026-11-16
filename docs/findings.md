# Findings from the first real-model games

What running Padrino against real models taught us so far. The strongest result is about the harness, not the models: **every failure we found was silent**, and each one produced a plausible-looking game. Model-behaviour observations come second and are explicitly anecdotal.

See [design-decisions.md](./design-decisions.md) (entry 13) for the reliability design that came out of this, and [architecture.md](./architecture.md) for the system map.

## 1. Scope and caveats

Read these before any number below.

- **The datasets are tiny.** One exploratory game, a 4-game shakedown ("batch 001"), and a 10-game batch ("batch 002", section 5).
- **Setup.** Seven models, one per seat, seat-rotated between games, ruleset `mini7_v1`, unranked showcase mode, all calls through one OpenAI-compatible gateway. The batch lineup is `claude-sonnet-5-5`, `claude-haiku-5-5`, `gpt-6-luna`, `grok-4.7`, `MiniMax-M3`, `MiniMax-M3.1-Flash` and `muse-spark-1.3-contributor`. The exploratory game used a different lineup (section 3.3).
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

### The pattern

All ten share a shape: a failure is absorbed by a default, the default looks like normal play, and the game completes. The remedy was the same each time: make failure a first-class, countable event; bound every loop by one deadline; prefer no-contest over a misleading result.

## 3. Model behaviour (anecdotal)

n = 4 games in batch-001 (2 completed, 2 no-contest) plus one earlier game. Roles differ per seat per game, so none of this separates model skill from role luck. Fewer than 30 games, no confidence intervals, no ranking.

### 3.1 Per-model call profile, batch-001

Source: batch-001 database, 4 games, 251 seat-attributed calls from `llm_calls`. `ok%` is the share of calls with status `ok`. Latencies are successful calls only. The engine changed during this batch, so MiniMax failure rates in particular are pre-fix.

| Model | Calls | ok% | Timeout% | Invalid JSON% | Schema viol.% | p50 ms | p95 ms |
|---|---|---|---|---|---|---|---|
| `claude-sonnet-5-5` | 38 | 100.0 | 0.0 | 0.0 | 0.0 | 4274 | 10004 |
| `gpt-6-luna` | 32 | 100.0 | 0.0 | 0.0 | 0.0 | 5223 | 9046 |
| `claude-haiku-5-5` | 34 | 91.2 | 0.0 | 0.0 | 8.8 | 5010 | 9544 |
| `grok-4.7` | 29 | 86.2 | 13.8 | 0.0 | 0.0 | 15781 | 50880 |
| `muse-spark-1.3-contributor` | 25 | 88.0 | 12.0 | 0.0 | 0.0 | 13937 | 27505 |
| `MiniMax-M3.1-Flash` | 50 | 52.0 | 16.0 | 2.0 | 0.0 | 17580 | 50801 |
| `MiniMax-M3` | 43 | 37.2 | 20.9 | 7.0 | 0.0 | 26906 | 43599 |

Three models were fast and clean; two were slower with some timeouts; the MiniMax pair mostly failed on overload errors (finding 6), timeouts and truncation (finding 7) under the earlier engine. `claude-haiku-5-5` had 3 schema violations followed by 3 re-asks. Provider-error rates mix model, provider capacity and time of day; do not compare them as model quality.

### 3.2 Day-vote accuracy

Votes cast on Days 1 to 2 in the four games, as Town, and whether the target was actually Mafia.

| Model | Town day votes | Landed on a Mafia seat |
|---|---|---|
| `claude-haiku-5-5` | 5 | 2 |
| `gpt-6-luna` | 5 | 1 |
| `muse-spark-1.3-contributor` | 4 | 1 |
| `grok-4.7` | 4 | 0 (3 of 4 were abstains) |
| `MiniMax-M3` | 3 | 0 |
| `MiniMax-M3.1-Flash` | 1 | 0 |

This says very little: counts are 0 to 5, and abstains (including coerced ones) count as misses. As Mafia, `claude-sonnet-5-5` voted for a Town seat 5 of 5 times and `MiniMax-M3.1-Flash` 3 of 4, which mostly reflects that Mafia win by getting Town to eliminate Town.

### 3.3 What one game showed (first 7-model game, 2026-11-15)

Seed `smoke-001`, 51 calls, Mafia won on parity after Night 2. The lineup differed from the batch (it included `gemini-3.8-flash`, `kimi-k3`, `glm-5.3` and `deepseek-v4.1-flash` alongside `gpt-6-luna`, `MiniMax-M3` and `claude-haiku-5-5`). The raw database was lost to a temp-directory cleanup; these notes were taken before that and cannot be re-checked.

- **Mafia coordination with an AFK partner.** `kimi-k3` carried the Mafia team alone: its partner was the dead seat from finding 1. It steered Town into mislynching a Villager on Day 1, killed the loudest Town voice on Night 1, framed a Town player with true information on Day 2, and on Night 2 correctly inferred the Doctor was guarding the self-claimed Detective, so it killed the Doctor.
- **Mislynch steering.** Town's own early leader (`deepseek-v4.1-flash`) drove the Day-1 mislynch while two other Town seats correctly suspected the silent Mafia seat. The silent seat survived because it could not defend itself, not because it hid.
- **Detective tempo.** The Detective's Night-1 check went to a player who died that same night, and its Day-2 vote was the invalid-JSON abstain from finding 2.

### 3.4 Public-chat samples

Public events only, 160 characters or fewer, identified by game seed and event sequence in `game_events`.

| Model | Seed | Seq | Quote |
|---|---|---|---|
| `gpt-6-luna` | `b001-g04` | 10 | "Let's hear everyone's initial thoughts before settling on a vote. Share any specific suspicions and the reasons behind them." |

Day-1 openers were similar across models: mostly some version of "no info yet, let's hear reads".

### 3.5 Style and language drift are not model findings

The first game's terse forum-slang dialogue ("scum", "wagon", "soft-claim") and one model switching to Chinese from its second message are **confounded** by the gateway prompt injection found afterwards (finding 4). We do not attribute them to the models. In batch-001, with the opt-out header and English rule on, a crude non-English heuristic (more than 20% non-ASCII letters) flagged 0 of 138 public messages.

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

## 5. Batch 002 results

<!-- BATCH-002 RESULTS: lead fills in -->

Pending: the 10-game batch on the fixed engine is still running.

## 6. Open questions and next experiments

- **Regression coverage.** Turn the shakedown failure classes (dead seat, overload burst, truncated reasoning, inline think block) into fault-injection tests against a mock adapter, so regressions are caught without a real gateway.
- **Seat and role effects.** At 7 players and 2 Mafia, win rate is dominated by role assignment. A balanced design (each model in each role equally often) is needed before any per-model claim, which is the 400-game scale in section 1.
- **Gateway transparency.** Record the gateway's effective configuration, or a hash of the final outgoing request, in each `llm_calls` row so a rewriting proxy is detectable from the data alone.
- **Reasoning budget.** Is 16384 output tokens enough for every reasoning model, and does a larger thinking budget change play or only latency?
- **Invalid-output rate with the re-ask on.** Measure first-attempt and post-re-ask invalid rates separately.
- **Style confound.** Repeat an English-rule-on versus English-rule-off comparison on a verified transparent gateway to see whether any of the first game's slang and language drift survives.
- **Dead-seat counterfactual.** How much does an unresponsive seat shift win rate if it plays on? The current answer, invalidate the game, avoids measuring it.
