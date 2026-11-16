## Executive recommendation

Build v1 as a **deterministic, event-sourced backend league engine for a single fixed Mafia ruleset**, with LLM calls treated as unreliable external inputs. Do **not** start with RL, semantic rewriting, open-ended agent frameworks, or many game variants. The core value is producing fair, reproducible game evidence that can later support ratings, audits, prompt/model comparisons, and eventually training.

Your vision already points toward the right north star: deterministic tick resolution, mechanical/chat separation, token discipline, role-family ratings, clone gauntlets, amnesia, and later MARL/RLAIF. The critical v1 scoping decision is to turn that into a narrow, testable benchmark substrate rather than a large research platform on day one. The uploaded vision frames the target as a deterministic platform for evaluating LLMs in adversarial hidden-information social deduction, with tick-based resolution, bifurcated API/chat inputs, TrueSkill-style ratings, clone gauntlets, and future RL training loops. 

Existing research supports the premise. Werewolf Arena evaluates LLMs through Werewolf, including deception, deduction, persuasion, and tournament-style comparisons across Gemini/GPT models. ([arXiv][1]) AvalonBench similarly treats Avalon as a multi-agent LLM benchmark involving hidden roles, deception, discussion, rule-based baselines, and role-specific ReAct-style prompts. ([arXiv][2]) CICERO is also relevant as evidence that strategic reasoning plus natural-language communication can be evaluated in multi-game league settings; Meta reports it played 40 Diplomacy games with 82 humans and ranked in the top 10% of participants who played more than one game. ([Meta AI][3])

The main v1 insight: **the benchmark is only as credible as its audit trail**. A leaderboard without deterministic replay, exact model/prompt/version records, full transcripts, role assignments, timeout/invalid-action accounting, and rating provenance will not be trusted.

---

# Critical interpretation of the vision

## What is strong

The vision correctly identifies that Mafia-like social deduction tests capabilities that ordinary static benchmarks miss: hidden information reasoning, deception detection, persuasion, factional coordination, memory, belief updating, and tactical timing. It also correctly separates **mechanical intent** from **natural-language persuasion**. The game engine must resolve only structured actions, while chat remains strategic text.

The tick-based ledger is the right foundation. It prevents a fast model from gaining an in-game advantage merely because it returns earlier. All living agents receive the same phase observation, submit within the same deadline, and only then are all outputs revealed.

The clone gauntlet is also essential. Single Mafia games are too noisy for model ranking. Role assignment, faction balance, early deaths, and one unlucky vote can dominate outcomes. Repeated games with controlled seeds and role permutations are necessary before any rating should be interpreted.

## What is ambiguous or under-specified

The biggest ambiguity is **the game itself**. “Mafia-type” is not a ruleset. Different variants produce materially different incentives and skill profiles. A leaderboard is meaningless unless every ranked result is attached to an immutable `ruleset_id` and `ruleset_version`.

The second ambiguity is **what exactly is being ranked**. A raw model name is not enough. Temperature, prompt version, memory policy, provider endpoint, output schema, retry policy, and system prompt are part of the agent. v1 should rank **agent builds**, where an agent build is:

`provider + model_id + model_version_if_available + inference_params + prompt_version + adapter_version + ruleset_version`

The third ambiguity is the **semantic firewall**. The vision proposes syntactic scrambling to prevent steganographic collusion. That should not be treated as solved in v1. Recent work on steganographic collusion shows that LLM agents can hide information in natural language and that paraphrasing/monitoring are not fully reliable countermeasures. ([arXiv][4]) A v1 “semantic firewall” should therefore be limited to deterministic surface normalization, detection, logging, and optional unranked experiments. Do not claim it prevents collusion.

The fourth ambiguity is **chain-of-thought logging**. Many providers do not expose true chain-of-thought, and asking for hidden reasoning is not a reliable scoring substrate. v1 should instead request a short, bounded `memory_update` and optional `rationale_summary`, stored privately for audit/diagnostics but never used for mechanical resolution or rating.

The fifth ambiguity is **context/token normalization**. Equal token budgets do not guarantee equal information access because tokenizers differ and models vary in context size. v1 should normalize by **information policy**, not only token count: every agent receives the same structured state fields, same public transcript window, same private memory size, same output character limits, and same phase deadline. Token usage and cost are recorded but never affect game mechanics.

---

# Technology recommendation

## Backend stack

Use **Python 3.12 + FastAPI + Pydantic v2 + PostgreSQL + Redis/Celery + LiteLLM + OpenSkill + Pytest/Hypothesis + OpenTelemetry**.

FastAPI is a strong fit for a typed backend API, and its docs describe it as a modern Python API framework based on standard type hints. ([FastAPI][5]) Pydantic is appropriate for strict request/response contracts and JSON schema generation; its docs emphasize type-hint-driven validation and serialization. ([Pydantic][6]) LiteLLM is useful because v1 must call many LLM providers through a unified interface, and its project describes support for 100+ providers with OpenAI-compatible routing, spend tracking, load balancing, and logging. ([GitHub][7])

Use **PostgreSQL** as the primary source of truth and event store. Event sourcing is the correct pattern because the system needs to capture intent, replay events, restore past states, and maintain a history/audit log. ([Microsoft Learn][8]) Use **Celery + Redis** for v1 background execution because Celery is a mature Python task queue that distributes work across machines via brokers and workers. ([Celery Documentation][9]) Temporal is attractive later for durable workflows, but Celery is simpler for an agent-built v1 if each game runner persists after every tick.

Use **OpenSkill** rather than hand-rolled Elo. TrueSkill-style Bayesian systems are better suited than Elo because they track uncertainty, model team results, and support multiple competing entities. ([NeurIPS Papers][10]) OpenSkill exposes `mu` and `sigma`, team updates, ties/scores, and an ordinal score defaulting to `mu - 3*sigma`, which is a useful conservative leaderboard score. ([OpenSkill][11])

Use **Hypothesis stateful testing** for the game engine. Hypothesis can generate sequences of state-machine actions to find failures in complex stateful systems. ([Hypothesis][12]) Use **OpenTelemetry** for traces, metrics, and logs across game runners, LLM calls, and rating updates. ([OpenTelemetry][13])

Do **not** use LangGraph, CrewAI, AutoGen, or similar agent frameworks for the game core. They are too flexible for this problem. The benchmark needs a custom deterministic engine with strict hidden-information boundaries, not open-ended agent orchestration.

---

# Product Requirements Document: Syndicate Forge v1 Core Backend

## 1. Product summary

**Product name:** Syndicate Forge v1
**Product type:** Backend-only deterministic benchmark and league engine
**Primary mission:** Evaluate LLM agents in a reproducible Mafia-style social deduction environment and produce statistically meaningful leaderboard/rating data as games accrue.
**v1 scope:** Run deterministic multi-game Mafia gauntlets among registered LLM agent builds; collect structured actions and chat; enforce fair tick-based resolution; persist full audit logs; update role-aware ratings; expose backend APIs for gauntlets, games, transcripts, and leaderboard queries.

## 2. v1 goals

v1 must:

1. Run a fixed 15-player Mafia ruleset named `classic15_v1`.
2. Support LLM-backed agents from multiple providers through a common adapter.
3. Enforce simultaneous tick collection with hard timeouts.
4. Separate chat from mechanical actions using strict JSON schemas.
5. Persist every game as an append-only hash-chained event log.
6. Support deterministic replay from archived events.
7. Run clone gauntlets: multiple games from the same roster under deterministic seeds and role permutations.
8. Prevent ranked agents from receiving model identities, historical player statistics, prior game memories, or leaderboard data.
9. Record latency, token usage, invalid actions, timeouts, costs, and raw/sanitized outputs for diagnostics.
10. Produce OpenSkill-based global and role-family ratings.
11. Provide a backend API and test suite sufficient for autonomous coding agents to implement without product interpretation.

## 3. Non-goals for v1

v1 will not include:

1. Frontend UI.
2. Human gameplay UI.
3. RL training, RLAIF, SFT, or policy optimization.
4. Multiple game variants.
5. Dynamic speak-bidding.
6. Semantic paraphrasing as a ranked anti-steganography measure.
7. Cross-game agent memory.
8. External arbitrary user-submitted bot code in ranked mode.
9. Model fine-tuning.
10. Manual judge scoring.
11. Payment/billing system.
12. Public production deployment hardening beyond basic auth/API keys.

## 4. Key v1 assumptions

1. Ranked games use only backend-controlled LLM agents.
2. Each agent call is stateless except for a per-game private `memory_update` field managed by the engine.
3. All ranked agents use the same prompt template version for a given ruleset.
4. All public player labels are anonymized as `P01` through `P15`.
5. Models never see provider/model names of any participant.
6. Game mechanics are resolved only from validated structured actions.
7. Chat can lie, bluff, accuse, and persuade, but it cannot directly trigger mechanics.
8. Remote LLM APIs are not deterministic, so exact game replay uses archived LLM outputs, not re-calling providers.
9. Ratings are provisional until minimum sample thresholds are met.

---

# 5. Ruleset: `classic15_v1`

## 5.1 Player count and roles

Exactly 15 players:

| Faction |       Role | Count | Role family   |
| ------- | ---------: | ----: | ------------- |
| Mafia   | Mafia Goon |     4 | Deceptive     |
| Town    |  Detective |     1 | Investigative |
| Town    |     Doctor |     1 | Protective    |
| Town    |   Villager |     9 | Vanilla Town  |

No Godfather, no roleblocker, no vigilante, no serial killer in v1.

## 5.2 Win conditions

After every elimination and night resolution:

1. Town wins if `alive_mafia_count == 0`.
2. Mafia wins if `alive_mafia_count >= alive_town_count`.
3. If no faction has won by the end of Day 7 vote resolution, the game is a draw.

## 5.3 Phase sequence

Every game follows this sequence:

1. `SETUP`
2. `NIGHT_0_MAFIA_INTRO`
3. `DAY_N_DISCUSSION_ROUND_1`
4. `DAY_N_DISCUSSION_ROUND_2`
5. `DAY_N_DISCUSSION_ROUND_3`
6. `DAY_N_VOTE`
7. `NIGHT_N_MAFIA_DISCUSSION`
8. `NIGHT_N_ACTIONS`
9. Repeat from Day `N+1` until terminal condition.

`NIGHT_0_MAFIA_INTRO` allows living mafia to privately communicate but has no kill.

## 5.4 Day discussion

During each day discussion round:

1. Every living player receives the same public observation plus their private role info.
2. Every living player may submit one public message.
3. No mechanical action is accepted in discussion rounds.
4. All valid public messages are revealed together after the tick.
5. Public reveal order is deterministic: sort by `sha256(game_seed + phase_id + public_player_id)` ascending.

## 5.5 Day vote

During `DAY_N_VOTE`:

1. Every living player may submit a public final message and one vote.
2. Legal vote targets are living players other than self.
3. `ABSTAIN` is legal.
4. Invalid, missing, or timed-out votes become `ABSTAIN`.
5. The living player with the unique highest vote count is eliminated.
6. If there is a tie for highest vote count, no one is eliminated.
7. If all players abstain, no one is eliminated.
8. Vote results are public.

## 5.6 Night mafia discussion

During `NIGHT_N_MAFIA_DISCUSSION`:

1. Only living mafia are prompted.
2. Each living mafia may submit one private mafia-channel message.
3. Town players are not prompted.
4. Mafia messages are visible only to living mafia in that game.
5. No mechanical action is accepted in this phase.

## 5.7 Night actions

During `NIGHT_N_ACTIONS`:

Mafia:

1. Each living mafia may vote for one living non-mafia kill target.
2. The mafia kill target is the unique plurality among living mafia kill votes.
3. If mafia kill votes tie, no kill occurs.
4. If no living mafia submits a valid kill vote, no kill occurs.

Doctor:

1. The living Doctor may protect one living player, including self.
2. The Doctor may not protect the same player on two consecutive nights.
3. Invalid or disallowed protection becomes `NOOP`.

Detective:

1. The living Detective may inspect one living player other than self.
2. The result is `MAFIA` if the target’s faction is Mafia, else `TOWN`.
3. The result is delivered privately to the Detective at the start of the next day only if the Detective remains alive after night resolution.

Resolution order:

1. Collect all night actions simultaneously.
2. Determine mafia kill target.
3. Determine doctor protect target.
4. If mafia target exists and equals doctor protect target, no death.
5. Else eliminate mafia target.
6. Queue Detective result if eligible.
7. Check win condition.

---

# 6. Agent contract

## 6.1 Observation object

Each LLM call receives a JSON observation rendered into the prompt.

Required fields:

```json
{
  "ruleset_id": "classic15_v1",
  "game_public_id": "G-...",
  "phase": "DAY_1_DISCUSSION_ROUND_1",
  "day": 1,
  "round": 1,
  "you": {
    "player_id": "P07",
    "alive": true,
    "role": "Doctor",
    "faction": "Town"
  },
  "alive_players": ["P01", "P02", "..."],
  "dead_players": [
    {
      "player_id": "P04",
      "day_or_night": "DAY_1",
      "cause": "VOTE"
    }
  ],
  "public_events": [
    {
      "phase": "DAY_1_DISCUSSION_ROUND_1",
      "speaker": "P03",
      "message": "I think P08 is overexplaining."
    }
  ],
  "private_events": [],
  "legal_actions": {
    "allowed_action_types": ["NOOP"],
    "legal_targets": []
  },
  "your_private_memory": "bounded string from previous tick or empty",
  "message_limits": {
    "public_message_max_chars": 600,
    "private_message_max_chars": 600,
    "memory_update_max_chars": 1200
  }
}
```

Private additions:

1. Mafia receive `mafia_teammates`.
2. Mafia receive mafia-channel messages.
3. Detective receives prior eligible inspection results.
4. Doctor receives their previous-night protected target to enforce the no-repeat constraint.

Ranked observations must not contain:

1. Agent build IDs.
2. Model/provider names.
3. Ratings.
4. Historical win rates.
5. Other games’ transcripts.
6. Gauntlet clone index.
7. Hidden roles except the agent’s own role and mafia teammates for mafia.

## 6.2 Response schema

Each LLM must return JSON conforming to:

```json
{
  "public_message": "string or null",
  "private_message": "string or null",
  "action": {
    "type": "NOOP | ABSTAIN | VOTE | MAFIA_KILL | PROTECT | INVESTIGATE",
    "target": "P01..P15 or null"
  },
  "memory_update": "string",
  "rationale_summary": "string or null"
}
```

Rules:

1. `public_message` is used only in public day phases.
2. `private_message` is used only in mafia private phases.
3. `action` is used only in action phases.
4. `memory_update` is private to that player and game.
5. `rationale_summary` is stored for diagnostics only.
6. Mechanical resolution reads only `action`.
7. Chat is never parsed for votes, kills, protects, or inspections.
8. Over-limit messages are truncated to the limit and logged as `OUTPUT_TRUNCATED`.
9. Invalid JSON or schema failure results in `NOOP`/`ABSTAIN` and empty visible message.
10. No repair call is allowed in ranked mode.

Structured outputs are useful here because providers increasingly support JSON schema-constrained responses; OpenAI’s docs describe Structured Outputs as ensuring model responses adhere to a supplied JSON Schema. ([OpenAI Developers][14]) For cross-provider operation, LiteLLM can sit in front of model APIs, but the engine must still validate every response itself. ([GitHub][7])

---

# 7. Fairness requirements

## 7.1 Tick barrier

For every phase:

1. Engine builds observations for all eligible players.
2. Engine launches all LLM calls concurrently.
3. Engine waits until every call completes or times out.
4. Engine validates and stores all responses.
5. Engine resolves the phase.
6. Engine reveals the next observation.

Agents never see:

1. Who responded first.
2. Latency of other agents.
3. Token usage of other agents.
4. Provider failures of other agents except as in-game silence/noop if visible.

## 7.2 Timeout policy

Default timeout: `45 seconds`.

On timeout:

1. Store `ActionTimedOut`.
2. Visible message is empty.
3. Mechanical action becomes:

   * `ABSTAIN` in vote phase.
   * `NOOP` in all other phases.
4. Agent memory remains previous memory.
5. Timeout is counted in diagnostics and leaderboard metadata but not separately punished beyond in-game consequences.

## 7.3 Token/information policy

v1 does not try to equalize actual provider costs. It equalizes game-relevant information.

Constants:

```text
PUBLIC_MESSAGE_MAX_CHARS = 600
PRIVATE_MESSAGE_MAX_CHARS = 600
MEMORY_UPDATE_MAX_CHARS = 1200
PUBLIC_TRANSCRIPT_RECENT_MESSAGE_LIMIT = 80
MAX_DAYS = 7
LLM_TIMEOUT_SECONDS = 45
TEMPERATURE = 0.7 unless overridden by agent build
TOP_P = 1.0 unless overridden by agent build
```

Every observation includes:

1. Current living/dead state.
2. All public mechanical events.
3. Most recent 80 public messages.
4. Player’s own private memory.
5. Role-appropriate private events.

No LLM-generated summarizer is used in v1. That avoids adding another model’s bias to the benchmark.

## 7.4 Ranked amnesia

In ranked gauntlets:

1. Agent memory is initialized empty at game start.
2. Agent memory is isolated per game and per player.
3. No memory crosses game boundaries.
4. No model receives its own historical performance.
5. No model receives other models’ historical performance.
6. Public player IDs are freshly assigned per game.

---

# 8. Anti-collusion and message sanitation

## 8.1 v1 surface normalizer

Before storing visible chat, apply deterministic normalization:

1. Unicode normalize to NFKC.
2. Remove zero-width and control characters except newline.
3. Collapse whitespace runs to a single space.
4. Limit repeated punctuation to max 3 identical chars.
5. Strip Markdown tables/code blocks.
6. Replace URLs with `[URL]`.
7. Replace long base64/hex-like strings over 24 chars with `[ENCODED]`.
8. Enforce max character length.
9. Store both raw and normalized text.
10. Reveal only normalized text.

## 8.2 What v1 must not claim

v1 must not claim to prevent all steganography. Research on LLM collusion and linguistic steganography indicates this is an open and nontrivial threat, and that paraphrasing/monitoring can be insufficient. ([arXiv][4])

## 8.3 Future unranked experiments

Add later:

1. Paraphrase firewall A/B tests.
2. Steganography detectors.
3. Cross-game collusion probes.
4. Adversarial prompt variants.
5. Human-readability metrics.
6. Communication-channel ablations.

---

# 9. Clone gauntlets

## 9.1 Definition

A gauntlet is a batch of games sharing:

1. Same roster of 15 agent builds.
2. Same ruleset version.
3. Same prompt version.
4. Same inference parameter policy.
5. Same gauntlet seed.
6. Deterministic game seeds derived from the gauntlet seed.

## 9.2 Default gauntlet config

```json
{
  "ruleset_id": "classic15_v1",
  "clone_count": 15,
  "ranked": true,
  "gauntlet_seed": "user-supplied-or-generated",
  "roster": ["agent_build_id_1", "...", "agent_build_id_15"]
}
```

The vision mentions cloning a 15-agent lobby into 10 identical instances.  v1 should support `clone_count=10`, but the default should be `15` because 15 clones gives better first-order seat/role balancing across a 15-player roster.

## 9.3 Seed derivation

Use deterministic SHA-256 seed derivation:

```text
game_seed_i = sha256("game" + gauntlet_seed + i)
role_seed_i = sha256("roles" + gauntlet_seed + i)
order_seed_i = sha256("order" + gauntlet_seed + i)
```

Do not use Python’s global `random` module in the core engine. Implement a deterministic RNG from `sha256(seed + counter)` with rejection sampling for bounded integers.

## 9.4 Role assignment

For each game:

1. Shuffle public player labels using `role_seed_i`.
2. Assign 4 mafia, 1 detective, 1 doctor, 9 villagers.
3. Record role assignment as private events.
4. Never reveal role assignment publicly except through game outcomes, claims, or future transcript export after game completion.

## 9.5 Gauntlet completion

A gauntlet is complete when all child games are terminal.

On completion:

1. Compute per-game ratings.
2. Compute aggregate gauntlet diagnostics.
3. Mark gauntlet `COMPLETED`.
4. Expose transcripts and leaderboard deltas.

---

# 10. Rating system

## 10.1 Rating entities

Maintain ratings for:

1. `agent_build_id` global.
2. `agent_build_id + faction`:

   * Town
   * Mafia
3. `agent_build_id + role_family`:

   * Deceptive
   * Investigative
   * Protective
   * Vanilla Town

## 10.2 Rating algorithm

Use OpenSkill `PlackettLuce` for v1.

Initial values:

```text
mu = 25.0
sigma = 8.333333333333334
conservative_score = mu - 3*sigma
```

OpenSkill documents `mu` as average estimated skill and `sigma` as uncertainty, with default initial values around 25 and 25/3, and its ordinal display defaults to `mu - 3*sigma`. ([OpenSkill][11])

## 10.3 Game rating update

At game end:

1. Build Town team from all Town-assigned seats.
2. Build Mafia team from all Mafia-assigned seats.
3. If Town wins, rank Town `1`, Mafia `2`.
4. If Mafia wins, rank Mafia `1`, Town `2`.
5. If draw, use equal ranks.
6. Update global ratings for all seats.
7. Update faction ratings for all seats.
8. Update role-family ratings for all seats.
9. Persist `RatingUpdateEvent` with before/after `mu`, `sigma`, and conservative score.

Do not use individual behavioral weights in v1. Win/loss only. Behavioral metrics are diagnostic and can be gamed if introduced into rating too early.

## 10.4 Leaderboard display contract

`GET /leaderboards/{league_id}` returns:

```json
{
  "leaderboard_id": "lb_...",
  "ruleset_id": "classic15_v1",
  "prompt_version": "prompt_classic15_v1_001",
  "rating_model": "openskill_plackett_luce_v1",
  "entries": [
    {
      "agent_build_id": "ab_...",
      "display_name": "provider/model/prompt alias",
      "games": 120,
      "wins": 68,
      "draws": 4,
      "losses": 48,
      "mu": 31.2,
      "sigma": 2.1,
      "conservative_score": 24.9,
      "timeout_rate": 0.02,
      "invalid_action_rate": 0.01,
      "public_message_avg_chars": 421,
      "role_family_breakdown": {
        "Deceptive": {"games": 32, "score": 23.1},
        "Investigative": {"games": 8, "score": 18.0},
        "Protective": {"games": 8, "score": 19.4},
        "Vanilla Town": {"games": 72, "score": 25.2}
      },
      "provisional": false
    }
  ]
}
```

A leaderboard entry is `provisional=true` until:

```text
total_games >= 30
mafia_games >= 5
town_games >= 15
```

---

# 11. Event sourcing and replay

## 11.1 Event log

Every game has an append-only event log.

Required event fields:

```json
{
  "event_id": "evt_...",
  "game_id": "game_...",
  "sequence": 42,
  "event_type": "ActionSubmitted",
  "phase": "DAY_1_VOTE",
  "visibility": "PUBLIC | PRIVATE | SYSTEM",
  "actor_player_id": "P07",
  "payload": {},
  "created_at": "server timestamp",
  "prev_event_hash": "hex",
  "event_hash": "hex"
}
```

## 11.2 Canonical hashing

`event_hash = sha256(prev_event_hash + canonical_json(event_without_event_hash))`

Canonical JSON rules:

1. UTF-8.
2. Sorted keys.
3. No insignificant whitespace.
4. ISO timestamps normalized to UTC.
5. No floating point values in core game events unless encoded as strings.

## 11.3 Replay modes

### Engine replay

Input: initial game config + event log excluding LLM request/response bodies.

Output: final game state.

Requirement: final state must match stored terminal state exactly.

### Frozen-response replay

Input: initial game config + archived LLM responses.

Output: regenerated event log.

Requirement: event hashes must match original from first LLM response onward, except server timestamps if excluded from hash.

### Provider re-run

Input: initial game config + prompt templates + model config.

Output: new game.

Requirement: not expected to match original because remote LLM outputs are nondeterministic.

---

# 12. Data model

## 12.1 Tables

### `model_providers`

```text
id UUID PK
name TEXT NOT NULL
base_url TEXT NULL
auth_secret_ref TEXT NOT NULL
created_at TIMESTAMPTZ NOT NULL
```

### `model_configs`

```text
id UUID PK
provider_id UUID FK
model_name TEXT NOT NULL
model_version TEXT NULL
default_temperature NUMERIC NOT NULL
default_top_p NUMERIC NOT NULL
default_max_output_tokens INT NOT NULL
supports_structured_outputs BOOLEAN NOT NULL
created_at TIMESTAMPTZ NOT NULL
```

### `prompt_versions`

```text
id UUID PK
ruleset_id TEXT NOT NULL
version TEXT NOT NULL
system_prompt TEXT NOT NULL
developer_prompt TEXT NOT NULL
response_schema JSONB NOT NULL
prompt_hash TEXT NOT NULL UNIQUE
created_at TIMESTAMPTZ NOT NULL
```

### `agent_builds`

```text
id UUID PK
display_name TEXT NOT NULL
model_config_id UUID FK
prompt_version_id UUID FK
adapter_version TEXT NOT NULL
inference_params JSONB NOT NULL
active BOOLEAN NOT NULL
created_at TIMESTAMPTZ NOT NULL
```

### `leagues`

```text
id UUID PK
name TEXT NOT NULL
ruleset_id TEXT NOT NULL
ranked BOOLEAN NOT NULL
created_at TIMESTAMPTZ NOT NULL
```

### `gauntlets`

```text
id UUID PK
league_id UUID FK
ruleset_id TEXT NOT NULL
prompt_version_id UUID FK
clone_count INT NOT NULL
gauntlet_seed TEXT NOT NULL
ranked BOOLEAN NOT NULL
status TEXT NOT NULL
created_at TIMESTAMPTZ NOT NULL
completed_at TIMESTAMPTZ NULL
```

### `gauntlet_roster_slots`

```text
id UUID PK
gauntlet_id UUID FK
slot_index INT NOT NULL
agent_build_id UUID FK
UNIQUE(gauntlet_id, slot_index)
```

### `games`

```text
id UUID PK
gauntlet_id UUID FK NULL
ruleset_id TEXT NOT NULL
game_seed TEXT NOT NULL
status TEXT NOT NULL
terminal_result TEXT NULL
terminal_reason TEXT NULL
started_at TIMESTAMPTZ NULL
completed_at TIMESTAMPTZ NULL
current_phase TEXT NULL
event_hash_head TEXT NULL
```

### `game_seats`

```text
id UUID PK
game_id UUID FK
public_player_id TEXT NOT NULL
seat_index INT NOT NULL
agent_build_id UUID FK
role TEXT NOT NULL
faction TEXT NOT NULL
alive BOOLEAN NOT NULL
death_phase TEXT NULL
UNIQUE(game_id, public_player_id)
UNIQUE(game_id, seat_index)
```

### `game_events`

As specified in section 11.

### `llm_calls`

```text
id UUID PK
game_id UUID FK
event_id UUID FK NULL
agent_build_id UUID FK
public_player_id TEXT NOT NULL
phase TEXT NOT NULL
request_json JSONB NOT NULL
request_prompt_hash TEXT NOT NULL
raw_response TEXT NULL
parsed_response JSONB NULL
status TEXT NOT NULL
error TEXT NULL
latency_ms INT NULL
input_tokens INT NULL
output_tokens INT NULL
cost_usd NUMERIC NULL
provider_response_id TEXT NULL
created_at TIMESTAMPTZ NOT NULL
```

### `ratings`

```text
id UUID PK
league_id UUID FK
agent_build_id UUID FK
scope_type TEXT NOT NULL -- GLOBAL | FACTION | ROLE_FAMILY
scope_value TEXT NOT NULL
mu NUMERIC NOT NULL
sigma NUMERIC NOT NULL
conservative_score NUMERIC NOT NULL
games INT NOT NULL
updated_at TIMESTAMPTZ NOT NULL
UNIQUE(league_id, agent_build_id, scope_type, scope_value)
```

### `rating_events`

```text
id UUID PK
league_id UUID FK
game_id UUID FK
agent_build_id UUID FK
scope_type TEXT NOT NULL
scope_value TEXT NOT NULL
before_mu NUMERIC NOT NULL
before_sigma NUMERIC NOT NULL
after_mu NUMERIC NOT NULL
after_sigma NUMERIC NOT NULL
created_at TIMESTAMPTZ NOT NULL
```

---

# 13. Backend API

## 13.1 Health

### `GET /healthz`

Returns:

```json
{"status": "ok"}
```

### `GET /readyz`

Checks DB, Redis, and worker heartbeat.

## 13.2 Agent builds

### `POST /agent-builds`

Creates an agent build.

Request:

```json
{
  "display_name": "gpt-x-classic15-v1",
  "model_config_id": "uuid",
  "prompt_version_id": "uuid",
  "adapter_version": "llm_adapter_v1",
  "inference_params": {
    "temperature": 0.7,
    "top_p": 1.0,
    "max_output_tokens": 700
  }
}
```

### `GET /agent-builds/{id}`

Returns configuration excluding secrets.

## 13.3 Leagues

### `POST /leagues`

```json
{
  "name": "Classic15 Ranked Season 1",
  "ruleset_id": "classic15_v1",
  "ranked": true
}
```

## 13.4 Gauntlets

### `POST /gauntlets`

```json
{
  "league_id": "uuid",
  "ruleset_id": "classic15_v1",
  "prompt_version_id": "uuid",
  "clone_count": 15,
  "ranked": true,
  "gauntlet_seed": "optional-string",
  "roster": [
    "agent_build_id_1",
    "agent_build_id_2"
  ]
}
```

Validation:

1. `roster.length == 15`.
2. All agent builds active.
3. All agent builds use compatible prompt/ruleset.
4. `clone_count >= 1 && clone_count <= 100`.

Response:

```json
{
  "gauntlet_id": "uuid",
  "status": "QUEUED",
  "game_ids": ["uuid"]
}
```

### `GET /gauntlets/{id}`

Returns status, child games, aggregate diagnostics.

## 13.5 Games

### `GET /games/{id}`

Returns public game summary.

### `GET /games/{id}/events?visibility=public`

Returns event log filtered by visibility and requester authorization.

### `GET /games/{id}/transcript`

Returns post-game transcript with public chat, private mafia chat, roles, actions, and outcome.

### `POST /games/{id}/replay`

Runs deterministic replay and returns:

```json
{
  "game_id": "uuid",
  "replay_status": "PASS",
  "final_event_hash": "hex"
}
```

## 13.6 Leaderboards

### `GET /leagues/{id}/leaderboard`

Returns leaderboard entries as specified in section 10.

---

# 14. Core services and modules

## 14.1 Repository layout

```text
syndicate_forge/
  app/
    api/
      routes_health.py
      routes_agents.py
      routes_leagues.py
      routes_gauntlets.py
      routes_games.py
      routes_leaderboards.py
    main.py
  core/
    rulesets/
      classic15_v1.py
    engine/
      state.py
      events.py
      reducer.py
      observations.py
      resolver.py
      replay.py
      rng.py
      hashing.py
    agents/
      contract.py
      llm_adapter.py
      scripted_agents.py
      sanitizer.py
    ratings/
      openskill_service.py
    gauntlets/
      scheduler.py
      role_assignment.py
  db/
    models.py
    migrations/
    repositories.py
  workers/
    celery_app.py
    run_game.py
    run_gauntlet.py
  tests/
```

## 14.2 Pure core rule

Everything under `core/engine` and `core/rulesets` must be pure and deterministic:

1. No database access.
2. No network calls.
3. No wall-clock reads.
4. No random module.
5. No provider SDKs.
6. No logging side effects required for correctness.

This is what makes the project agent-developable and testable.

---

# 15. Testing strategy

## 15.1 Test-first implementation order

Coding agents should implement in this order:

1. Deterministic RNG and hashing tests.
2. `classic15_v1` state model tests.
3. Vote/night action resolver tests.
4. Observation privacy tests.
5. Agent response schema validation tests.
6. Event sourcing/replay tests.
7. Scripted-agent game integration tests.
8. Gauntlet generation tests.
9. Rating update tests.
10. API route tests.
11. LLM adapter tests with mocked providers.
12. Celery worker tests with eager mode.

## 15.2 Required test files

### `tests/core/test_rng.py`

Required cases:

1. Same seed produces same role assignment.
2. Different seeds produce different assignments.
3. Bounded integer generation never exceeds range.
4. RNG does not import or call Python `random`.

### `tests/core/test_classic15_setup.py`

Required cases:

1. Exactly 15 players required.
2. Role counts are 4 mafia, 1 detective, 1 doctor, 9 villagers.
3. Public IDs are `P01` through `P15`.
4. Mafia receive teammate info.
5. Town do not receive mafia identities.
6. Game starts in `NIGHT_0_MAFIA_INTRO`.

### `tests/core/test_day_vote.py`

Required cases:

1. Unique plurality eliminates target.
2. Tie eliminates nobody.
3. All abstain eliminates nobody.
4. Dead players cannot vote.
5. Self-vote becomes abstain.
6. Invalid target becomes abstain.
7. Vote chat does not affect vote target.

### `tests/core/test_night_actions.py`

Required cases:

1. Mafia unique plurality kills target.
2. Mafia tie causes no kill.
3. Doctor protection blocks kill.
4. Doctor invalid repeat protection becomes noop.
5. Detective receives correct result if alive.
6. Detective receives no next-day result if killed.
7. Night action chat does not affect mechanics.

### `tests/core/test_win_conditions.py`

Required cases:

1. Town wins when last mafia eliminated.
2. Mafia wins when alive mafia count equals alive town count.
3. Draw occurs after Day 7 if no prior winner.
4. No events occur after terminal state.

### `tests/core/test_observation_privacy.py`

Required cases:

1. Ranked observations never include model names.
2. Ranked observations never include ratings.
3. Ranked observations never include gauntlet clone index.
4. Private mafia chat visible only to living mafia.
5. Detective result visible only to Detective.
6. Dead players receive no future prompts.

### `tests/agents/test_contract_validation.py`

Required cases:

1. Valid JSON response parses.
2. Invalid JSON becomes noop/abstain.
3. Invalid action type becomes noop/abstain.
4. Invalid target becomes noop/abstain.
5. Over-limit public message is truncated and logged.
6. Unicode zero-width characters removed.
7. URLs replaced with `[URL]`.
8. Long encoded strings replaced with `[ENCODED]`.

### `tests/core/test_event_sourcing.py`

Required cases:

1. Event sequence must be contiguous.
2. Hash chain validates.
3. Tampered event payload fails validation.
4. Replaying event log reconstructs identical final state.
5. Frozen-response replay reproduces event hashes.

### `tests/integration/test_scripted_games.py`

Required cases:

1. Scripted town victory completes.
2. Scripted mafia victory completes.
3. Scripted draw completes.
4. Game runner records every LLM/scripted call.
5. No phase resolves before all eligible agents submit or timeout.

### `tests/ratings/test_openskill_service.py`

Required cases:

1. Winning team mu increases.
2. Losing team mu decreases.
3. Sigma decreases after rated game.
4. Draw produces equal-rank update.
5. Role-family ratings update independently.
6. Conservative score equals `mu - 3*sigma`.

### `tests/gauntlets/test_gauntlet_generation.py`

Required cases:

1. Same gauntlet seed creates same game seeds.
2. Same gauntlet seed creates same role assignments.
3. Different seed changes assignments.
4. `clone_count` games are created.
5. Roster length other than 15 rejected.
6. Ranked gauntlet rejects incompatible ruleset/prompt versions.

### `tests/api/test_routes.py`

Required cases:

1. Create league.
2. Create agent build.
3. Create gauntlet.
4. Fetch game.
5. Fetch transcript after completion.
6. Fetch leaderboard.
7. Unauthorized access to private events rejected.

## 15.3 Property-based stateful tests

Use Hypothesis state machines for random legal/illegal action sequences.

Invariants:

1. Alive count never increases.
2. A player dies at most once.
3. Dead players never generate visible messages.
4. Role counts never change.
5. Terminal game has exactly one terminal result.
6. Mechanical state is independent of chat text.
7. Replay of generated event sequence equals live reducer state.

---

# 16. Observability and diagnostics

Each game should emit:

1. `game.started`
2. `phase.started`
3. `llm.call.started`
4. `llm.call.completed`
5. `llm.call.timeout`
6. `phase.resolved`
7. `game.completed`
8. `rating.updated`

Metrics:

1. Games completed.
2. Game duration wall-clock.
3. Phase duration.
4. LLM latency by provider/model.
5. Timeout rate.
6. Invalid JSON rate.
7. Invalid action rate.
8. Average public message length.
9. Token usage by provider/model.
10. Cost estimate by provider/model.
11. Rating update count.

Traces should connect:

`gauntlet_id -> game_id -> phase_id -> llm_call_id -> event_id`

---

# 17. Acceptance criteria for v1

v1 is complete when:

1. A developer can register at least two LLM-backed agent builds.
2. A developer can create a ranked `classic15_v1` league.
3. A developer can create a 15-game clone gauntlet with a 15-agent roster.
4. The backend runs all games to terminal states without manual intervention.
5. Every game has a complete append-only event log.
6. Every game passes deterministic replay.
7. Every LLM call is archived with request, raw response, parsed response, status, latency, and token/cost metadata where available.
8. Ranked observations exclude model identities and historical performance.
9. Invalid outputs and timeouts are handled deterministically.
10. Leaderboard endpoint returns OpenSkill global, faction, and role-family ratings.
11. The test suite passes in CI without real LLM API keys using scripted and mocked agents.
12. Optional provider integration tests pass when API keys are present.
13. Core engine test coverage is at least 90%.
14. No frontend is required.

---

# 18. Implementation milestones

## Milestone 1: Deterministic core

Deliver:

1. `classic15_v1` state model.
2. Deterministic RNG.
3. Event types.
4. Reducer.
5. Phase resolver.
6. Replay.
7. Unit/property tests.

Exit condition: scripted action sequences can produce deterministic terminal games.

## Milestone 2: Agent contract and observations

Deliver:

1. Observation builder.
2. Privacy filters.
3. Response schema.
4. Message sanitizer.
5. Scripted agents.
6. Mock LLM adapter.

Exit condition: integration tests can run full games with scripted agents.

## Milestone 3: Persistence and API

Deliver:

1. PostgreSQL schema.
2. Repositories.
3. FastAPI routes.
4. Event persistence.
5. Transcript endpoint.
6. Replay endpoint.

Exit condition: games can be created, run, stored, fetched, and replayed through API calls.

## Milestone 4: LLM execution

Deliver:

1. LiteLLM adapter.
2. Concurrent tick execution.
3. Timeout handling.
4. LLM call archive.
5. Provider metadata capture.
6. Mock-provider CI tests.

Exit condition: a full game can run with mocked LLMs and with real LLMs when keys are configured.

## Milestone 5: Gauntlets and ratings

Deliver:

1. Gauntlet scheduler.
2. Clone game creation.
3. OpenSkill rating service.
4. Leaderboard endpoint.
5. Rating event audit log.

Exit condition: a gauntlet completes and leaderboard updates deterministically from game outcomes.

## Milestone 6: Hardening

Deliver:

1. OpenTelemetry traces/metrics.
2. Admin API auth.
3. CI coverage gate.
4. Docker Compose for local Postgres/Redis/API/worker.
5. Seeded demo gauntlet.

Exit condition: a fresh developer or coding agent can run the system locally from README instructions and reproduce the demo gauntlet.

---

# 19. Recommended v1 prompt policy

Use one shared system prompt for all agents, with role-specific observation data. The prompt should emphasize:

1. You are playing a social deduction game.
2. Your goal is to make your faction win.
3. You may deceive in public chat if strategically useful.
4. You must return valid JSON only.
5. Mechanical actions are expressed only through the JSON `action` field.
6. Public messages are visible to all living players.
7. Private messages are visible only when the observation says the phase has a private channel.
8. Your `memory_update` is private and will be shown only to you later in this game.
9. Do not rely on prior games; you have no persistent identity.

Do not tell agents that they are in a benchmark leaderboard during ranked games. It invites meta-behavior.

---

# 20. Main risks and mitigations

## Risk: leaderboard overfits to one ruleset

Mitigation: v1 labels all ratings with `ruleset_id`. Future variants get separate leaderboards.

## Risk: model/provider drift

Mitigation: store provider response IDs, model names, observed model versions where available, prompts, parameters, and all raw responses. Treat re-calling a provider as a new run.

## Risk: hidden collusion

Mitigation: ranked v1 removes cross-game memory and model identity, normalizes surface text, logs raw/sanitized output, and avoids claiming full prevention.

## Risk: role imbalance

Mitigation: clone gauntlets, role-family ratings, faction ratings, and provisional ranking thresholds.

## Risk: invalid JSON dominates games

Mitigation: schema-constrained calls where available, strict validation, invalid-rate diagnostics, and no ranked repair calls.

## Risk: agents exploit rating metrics

Mitigation: v1 rating uses only team outcome. Do not use behavioral weights until proven robust.

## Risk: context window differences

Mitigation: deterministic transcript window and bounded private memory instead of unbounded full history.

---

# 21. One decision to confirm

I would lock v1 to the `classic15_v1` ruleset above unless you strongly prefer a different Mafia variant. The whole backend can be built around ruleset versioning, but the first credible leaderboard needs exactly one canonical ruleset.

[1]: https://arxiv.org/abs/2407.13943 "[2407.13943] Werewolf Arena: A Case Study in LLM Evaluation via Social Deduction"
[2]: https://arxiv.org/abs/2310.05036 "[2310.05036] AvalonBench: Evaluating LLMs Playing the Game of Avalon"
[3]: https://ai.meta.com/research/cicero/diplomacy/ "Diplomacy and CICERO"
[4]: https://arxiv.org/abs/2410.03768 "[2410.03768] Hidden in Plain Text: Emergence & Mitigation of Steganographic Collusion in LLMs"
[5]: https://fastapi.tiangolo.com/?utm_source=chatgpt.com "FastAPI"
[6]: https://pydantic.dev/docs/validation/latest/get-started/?utm_source=chatgpt.com "Welcome to Pydantic | Pydantic Docs"
[7]: https://github.com/BerriAI/litellm "GitHub - BerriAI/litellm: Python SDK, Proxy Server (AI Gateway) to call 100+ LLM APIs in OpenAI (or native) format, with cost tracking, guardrails, loadbalancing and logging. [Bedrock, Azure, OpenAI, VertexAI, Cohere, Anthropic, Sagemaker, HuggingFace, VLLM, NVIDIA NIM] · GitHub"
[8]: https://learn.microsoft.com/en-us/azure/architecture/patterns/event-sourcing "Event Sourcing Pattern - Azure Architecture Center | Microsoft Learn"
[9]: https://docs.celeryq.dev/en/main/getting-started/introduction.html "Introduction to Celery — Celery 5.6.2 documentation"
[10]: https://papers.neurips.cc/paper/3079-trueskilltm-a-bayesian-skill-rating-system.pdf "TrueSkill™: A Bayesian Skill Rating System"
[11]: https://openskill.me/en/stable/manual.html "User Manual - OpenSkill: Multiplayer Rating System. No Friction."
[12]: https://hypothesis.readthedocs.io/en/latest/stateful.html "Stateful tests - Hypothesis 6.152.7 documentation"
[13]: https://opentelemetry.io/docs/ "Documentation | OpenTelemetry"
[14]: https://developers.openai.com/api/docs/guides/structured-outputs "Structured model outputs | OpenAI API"
