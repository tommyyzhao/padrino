# PREP — Wave 9 (Human Multiplayer v1) before running `./ralph.sh`

Companion to `prd-v3.md` (vision) and `ralph/prd-v9.json` (the 37-story plan, US-121–157).
This is the human-gated checklist. The loop itself must reach loop-complete **without any new credential** (tests use mock adapters); everything requiring a real key/secret/art/legal-text is a deploy-time human step listed here, **not** a story — mirroring the Wave 8 launch-checklist pattern.

---

## 0. Sequencing (do NOT start v9 early)

- [ ] **Wave 8 reaches loop-complete and is merged to `main`.** Wave 9 builds on Wave 8's public/private surface split (US-110/111), retention executor (US-116), and alerting (US-113). The `ralph/padrino-v9` branch forks from `main` after that merge.
- [ ] Confirm `git status` clean on `main`, Wave 8 merged.

---

## 1. Refresh the loop's source-of-truth files (both are STALE)

The loop pipes `ralph/prompt.md` to `claude` every iteration and treats `AGENTS.md` as law. Both currently describe **Wave 4 / backend-only** and will actively mislead a Wave 9 run.

- [ ] **`ralph/prompt.md`** — currently titled "Padrino Wave 4", references US-079–083, `prd.md`, "backend-only", `mini7_v1` only. Rewrite the header + the "What's already locked in" + "Hard rules" sections for Wave 9:
  - Vision doc is now **`prd-v3.md`** (not `prd.md`); plan is `ralph/prd.json` (= the swapped-in v9).
  - Branch `ralph/padrino-v9` from `main`.
  - Add the **two non-negotiable invariants** (anonymity fail-closed; segregation = zero scientific rating writes) and tell the loop they are CI-gated by US-146.
  - Add the v9 locked-ins: SSE-out+POST-in transport (not WebSocket); separate human worker lane; out-of-band human chat (off the hash chain); symmetric fixed release delay (hcad jitter/multi-message DEFERRED); static sprite library (no runtime image-gen); block-before-release moderation hardened; one OAuth provider, no friends-graph/merge.
  - Frontend stories (US-153–156) additionally run the **pnpm gates** (`pnpm -C web/dashboard lint|check|test:e2e`) — already encoded in those stories' acceptance criteria.
  - Keep the still-valid process sections verbatim: boot sequence, four backend gates, `feat: US-XXX - <title>` commit format, set `passes:true`, append `progress.txt`, and the **stop condition** (`jq '[.userStories[]|select(.passes==false)]|length'` → `<promise>COMPLETE</promise>` only at 0).
- [ ] **`AGENTS.md`** — Hard rule 6 ("Backend-only — no browser verification") is false since Wave 7 and very false for v9. Update it to: frontend is first-class; frontend stories run the pnpm + Playwright gates. Add new hard rules: (a) human-vs-AI/model identity never leaks pre-reveal, guards fail closed to ANONYMOUS; (b) human games write zero scientific rating rows; (c) human chat is stored out-of-band of the hash chain; (d) human games run on the separate worker lane.

> I can generate the rewritten `prompt.md` and the `AGENTS.md` diff on request — held back so they don't perturb a possible in-flight Wave 8 run.

---

## 2. Human-gated provisioning & sign-off (NOT loop stories)

- [ ] **Architecture ratification** for the two heavy designs before the loop builds them:
  - **US-131 durable state** = Postgres-backed snapshots (`human_game_runtime` + replay from the event log). **No Redis** (stack rule). Confirm this is the chosen approach.
  - **US-132 worker lane** = a dedicated process/compose service for human games with its own concurrency cap. Confirm the topology.
- [ ] **OAuth app (one provider, e.g. Google):** create the app, obtain client id/secret, plan to set them via settings/env at deploy. *Not needed for the loop* (US-129 tests use a stubbed provider) — needed for real sign-in.
- [ ] **Static sprite library (US-152):** an OFFLINE, human-run art step. Use `gemini-3.1-flash-image` (needs the image API key) to generate the 3–5 theme packs' **role-agnostic** archetypes, moderate + curate them, and commit the static assets + manifest so US-152 has files to serve. The loop builds the serving/manifest plumbing, **not** the art.
- [ ] **Guard model for live moderation (US-140):** ensure the Llama-Guard guard model endpoint (e.g. DeepInfra) is reachable at deploy. Loop tests use a stub; real play needs the key.
- [ ] **Legal copy:** Terms & Privacy text for the one-tap consent gate (US-130). The loop builds the gate + version-stamping; legal authors the copy.
- [ ] **Deploy surface reconciliation:** the human-play API (lobbies, human auth, action/chat POST, seat streams) is a **new authenticated, internet-facing surface** that is neither the Wave 8 spectator `/public/*` nor the private admin backend. Decide where it lives in the public/private split and how the human worker lane is exposed.

---

## 3. Numbers to sign off (settings; placeholders OK for the loop, confirm before public launch)

- [ ] Per-user/day game + join caps; per-user/day inference-$ cap; per-lobby USD cap (sized to the **Moderate $500–$2k/mo** envelope, decision 19).
- [ ] `padrino_human_phase_deadline_seconds`, `padrino_human_release_delay_seconds`, `padrino_human_lane_max_concurrent`, moderation hold-window latency budget (ms).

---

## 4. Loop runtime config

- [ ] **Model pin.** `ralph.sh` defaults to `RALPH_CLAUDE_MODEL=claude-sonnet-4-6`. Several v9 stories are architecturally heavy (US-131 durable state, US-132 worker lane, US-133 live-tail, US-137–139 adapter/tick/replay, US-140 moderation). Recommend running the **foundational/architectural stories on a stronger model** (Opus) and the mechanical ones on Sonnet — e.g. run P0–P3 with `RALPH_CLAUDE_MODEL=claude-opus-4-8`, then P4–P5 on Sonnet. (Cost tradeoff is real; the script's header documents the 3× pin.)
- [ ] **Iteration budget.** 37 stories need headroom: run `./ralph.sh 60` (the loop exits early on `<promise>COMPLETE</promise>` / 0 remaining, so over-allocating is free).
- [ ] **Tooling preflight:** `claude` CLI installed + authed (the loop's runner); `uv`, `pnpm`, `jq` on PATH; `uv sync`; `pnpm -C web/dashboard install`; Playwright browsers installed (`pnpm -C web/dashboard exec playwright install`) for the frontend e2e gates.
- [ ] Baseline green on the fresh branch before the first iteration: `uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run pytest -m "not integration"`.

---

## 5. Swap-in procedure (avoids the `ralph.sh` archive footgun)

`ralph.sh` auto-archives when `branchName` changes vs `.last-branch` — but if you simply `cp prd-v9.json prd.json` first, the script copies the **new v9** file into the **v8** archive folder and resets `progress.txt` (the exact v3→v4 incident its own comments warn about). Do this instead:

```bash
cd /Users/admin/Projects/opensource/padrino
DATE=$(date +%Y-%m-%d)

# 1. Preserve Wave 8 final state manually
mkdir -p ralph/archive/$DATE-padrino-v8
cp ralph/prd.json      ralph/archive/$DATE-padrino-v8/prd.json
cp ralph/progress.txt  ralph/archive/$DATE-padrino-v8/progress.txt

# 2. Swap the v9 plan into the active slot
cp ralph/prd-v9.json ralph/prd.json

# 3. Fresh progress log for the new wave, KEEPING the cross-wave "## Codebase Patterns" section
#    (the loop reads that section first every iteration). Hand-carry it forward, then truncate the rest.

# 4. Neutralize the auto-archive: make .last-branch already match v9 so the first run does nothing destructive
echo "ralph/padrino-v9" > ralph/.last-branch

# 5. Create the branch from main and confirm
git checkout main && git pull
git checkout -b ralph/padrino-v9
```

---

## 6. Scope/risk notes & recommended split

- **37 stories is a large wave** (Wave 8 was 11). Recommended: run it as **two checkpointed sub-waves** rather than one 60-iteration marathon:
  - **v9a — foundations + loop (US-121–140):** branch `ralph/padrino-v9a`. Stop, run the full suite + a manual anonymity/segregation check, and eyeball the heavy stories (131/132/133/140) before continuing.
  - **v9b — disclosure + lobbies + frontend (US-141–157):** branch `ralph/padrino-v9b` off v9a.
  - (If splitting, give each its own `prd.json` slice + branchName and repeat §5.)
- **Watch stories** likely to need a human design spike or AC correction mid-loop (the loop is permitted to edit `prd.json` AC + note it in `progress.txt`): US-131, US-132, US-133, US-140. Review their diffs first.
- **Hard checkpoint after P0 (US-121–126):** the schema + invariants are load-bearing for everything after; run the suite and the anonymity/segregation gates before letting the loop proceed to P1.

---

## 7. Go

```bash
# after §0–§5 are checked off:
RALPH_CLAUDE_MODEL=claude-opus-4-8 ./ralph.sh 60      # or split per §6
# watch ralph/progress.txt; the loop self-terminates on <promise>COMPLETE</promise>
```
