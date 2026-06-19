// orchestrate.workflow.mjs
// ---------------------------------------------------------------------------
// A *dynamic Workflow-tool script* (NOT standalone node — it uses the Workflow
// runtime globals agent()/phase()/log()/args). Wave-agnostic: it drives ANY
// ralph wave (reads ralph/prd.json, works whatever stories are passes:false).
//
//   Workflow({ scriptPath: "ralph/orchestrate.workflow.mjs" })            // run all remaining
//   Workflow({ scriptPath: "ralph/orchestrate.workflow.mjs",
//              args: { stopAtPriority: 126 } })                            // checkpoint
//
// Per story, in strict ascending-priority order (a valid topological order for
// these prds — every dependency points to a lower priority):
//   implement (test-first, gates, commit, flip passes:true, progress.txt)
//   -> adversarial verify (re-run gates, check every AC, audit invariants)
//   -> bounded retry (verify feedback threaded into the next attempt)
//   -> on unrecoverable block: HALT (dependents would inherit a broken base).
//
// PRECONDITION (operator-owned, verified by hand — see PREP-v9 §5):
//   * git is on the branch named in ralph/prd.json (.branchName)
//   * baseline is green (ruff/format/mypy/pytest)
// State of record is the repo (passes:true + commits) -> re-launching resumes.
// ---------------------------------------------------------------------------

export const meta = {
  name: 'orchestrate-ralph-wave',
  description:
    'Implement every passes:false story in ralph/prd.json in dependency order: implement test-first -> adversarially verify -> bounded retry, halting on an unrecoverable block. Wave-agnostic (v8, v9, ...).',
  whenToUse:
    'After a wave is set up: ralph/prd.json present, git on its .branchName, baseline green. Optional args.stopAtPriority to checkpoint; args.maxRetries (default 2).',
  phases: [
    { title: 'Scan', detail: 'read prd.json + git branch; assert precondition; compute remaining work' },
    { title: 'Build', detail: 'per story: implement -> adversarial verify -> retry' },
    { title: 'Report', detail: 'final scan + structured report' },
  ],
}

const A = args || {}
const REPO = '/Users/admin/Projects/opensource/padrino'
const STOP_AT = typeof A.stopAtPriority === 'number' ? A.stopAtPriority : Infinity
const MAX_RETRIES = typeof A.maxRetries === 'number' ? A.maxRetries : 2

// --------------------------------------------------------------------------- schemas
const SCAN_SCHEMA = {
  type: 'object',
  required: ['branchName', 'currentBranch', 'stories'],
  properties: {
    branchName: { type: 'string' },
    currentBranch: { type: 'string' },
    stories: {
      type: 'array',
      items: {
        type: 'object',
        required: ['id', 'priority', 'passes'],
        properties: {
          id: { type: 'string' },
          priority: { type: 'number' },
          passes: { type: 'boolean' },
        },
      },
    },
  },
}

const IMPLEMENT_SCHEMA = {
  type: 'object',
  required: ['storyId', 'status'],
  properties: {
    storyId: { type: 'string' },
    status: { type: 'string', enum: ['implemented', 'blocked'] },
    summary: { type: 'string' },
    filesChanged: { type: 'array', items: { type: 'string' } },
    gates: {
      type: 'object',
      properties: {
        ruff: { type: 'boolean' },
        format: { type: 'boolean' },
        mypy: { type: 'boolean' },
        pytest: { type: 'boolean' },
        pnpm: { type: 'string' },
      },
    },
    commitSha: { type: 'string' },
    passesSet: { type: 'boolean' },
    blockers: { type: 'string' },
    notes: { type: 'string' },
  },
}

const VERIFY_SCHEMA = {
  type: 'object',
  required: ['storyId', 'verdict', 'gatesGreen', 'acMet'],
  properties: {
    storyId: { type: 'string' },
    verdict: { type: 'string', enum: ['pass', 'fail'] },
    gatesGreen: { type: 'boolean' },
    gateOutput: { type: 'string' },
    acMet: { type: 'boolean' },
    invariantViolations: { type: 'array', items: { type: 'string' } },
    commitPresent: { type: 'boolean' },
    passesTrueSet: { type: 'boolean' },
    reasons: { type: 'string' },
  },
}

const FINAL_SCHEMA = {
  type: 'object',
  required: ['remainingCount'],
  properties: {
    remainingCount: { type: 'number' },
    remainingIds: { type: 'array', items: { type: 'string' } },
    recentCommits: { type: 'array', items: { type: 'string' } },
  },
}

// --------------------------------------------------------------------------- prompts
function implementPrompt(storyId, attempt, feedback) {
  return `You are an autonomous coding agent implementing ONE user story in the Padrino repo (${REPO}). Work test-first, keep the diff surgical, keep CI green. You have full tool access (Read/Edit/Write/Bash). The text you return is consumed by an orchestrator, not a human.

## Boot — read these FIRST every time; they are LAW, never re-derive
- AGENTS.md / CLAUDE.md — the project's hard rules and quality gates. This is the single source of truth for what is and isn't allowed. Re-read it; it may have grown since you last saw it.
- The wave's vision doc(s) that AGENTS.md points to (prd.md / prd-v2.md / prd-v3.md as applicable to the current wave).
- ralph/progress.txt — read the '## Codebase Patterns' section at the TOP first, then skim the most recent entries.
- ralph/prd.json — the active plan. Read the FULL spec (title, description, EVERY acceptanceCriteria, notes) for ${storyId}.

## Your story: ${storyId}
Implement EXACTLY ${storyId} and nothing else. Satisfy EVERY one of its acceptanceCriteria.

## Steps (the Ralph loop, one story)
1. Confirm git branch == the prd.json .branchName. If not on it, check it out (create from main only if it does not exist). NEVER commit on main.
2. Test-first: write the failing test(s) covering the AC -> implement -> green -> refactor.
3. Run ALL backend quality gates; every one must be green:
   uv run ruff check .
   uv run ruff format --check .
   uv run mypy src tests
   uv run pytest -m "not integration"
   If ${storyId} is a FRONTEND story (its AC name the web/dashboard or pnpm gates) ALSO run:
   pnpm -C web/dashboard lint && pnpm -C web/dashboard check && pnpm -C web/dashboard test:e2e
   Integration tests (@pytest.mark.integration) are skipped by default; use mock adapters/stubs — never a real credential.
   Once every gate passes ONE time, move STRAIGHT to the commit. Do NOT loop re-running suites to "confirm" non-flakiness — a single green run satisfies the gate. If a test is inherently flaky, make it DETERMINISTIC (pin the clock/seed/order) rather than re-running it. Budget your effort: implement + verify gates + commit; do not open-endedly grind.
4. Fix ROOT CAUSES only. FORBIDDEN: '# type: ignore' to force green, --no-verify, git commit --amend, deleting/skipping a failing test, or weakening an assertion just to pass.
5. Commit with EXACTLY this format (title verbatim from prd.json): feat: ${storyId} - <title>. One focused implementation commit.
6. Set ONLY this story's "passes": true in ralph/prd.json. Leave every other story untouched.
7. Append a dated entry to ralph/progress.txt (match the existing format). Add any REUSABLE pattern to the '## Codebase Patterns' section at the top.
8. Commit the prd.json/progress.txt update (with, or right after, the implementation commit).

## Universal hard rules (always; AGENTS.md may add more — obey those too)
- Pure core: NOTHING impure (random / secrets / datetime.utcnow() / time.time()) under src/padrino/core/. Use SeededRng; clocks/jitter live only in the impure shell (runner/llm/db/api).
- Chat is never parsed for mechanics; only the structured action drives state.
- Migrations: if this story needs a schema change, add exactly ONE new Alembic revision under src/padrino/db/migrations/versions/ whose down_revision is the CURRENT head (the next free number after the latest existing revision). Do NOT reuse or duplicate a revision number; keep the chain linear.
- Honor every additional hard rule defined in AGENTS.md (e.g. for the human-multiplayer wave: anonymity fails CLOSED to ANONYMOUS and covers DB columns; human games write ZERO rows to the scientific Rating/RatingEvent tables; human chat is stored out-of-band of the hash chain; human games run on a separate worker lane).
- Do not jump dependency order; do not create *.md docs unless this story explicitly requires one; do NOT git push, open PRs, or tag.
${attempt > 0 ? `\n## THIS IS RETRY ATTEMPT ${attempt + 1} — the previous attempt FAILED adversarial verification. Fix EVERY point below, then re-run ALL gates before returning:\n${feedback}\n` : ''}
Return the StructuredOutput tool: status='implemented' ONLY if all gates are green AND the commit + passes:true are done; otherwise status='blocked' with a precise 'blockers' string. Populate filesChanged (real paths), gates (which passed), commitSha (short sha of the impl commit), passesSet.`
}

function verifyPrompt(storyId) {
  return `You are an ADVERSARIAL verifier in the Padrino repo (${REPO}). A coding agent claims to have implemented story ${storyId}. Assume it is WRONG until proven right. Do NOT fix anything — only inspect and report. You have Read + Bash + Grep.

## Checks
1. Read ${storyId}'s FULL acceptanceCriteria from ralph/prd.json. For each criterion, confirm the committed code actually meets it; cite file:line. Set acMet=false if any criterion is unmet/partial.
2. Re-run the quality gates YOURSELF and record pass/fail + the tail of any failure into gateOutput:
   uv run ruff check .
   uv run ruff format --check .
   uv run mypy src tests
   uv run pytest -m "not integration"
   If ${storyId} is a frontend story (AC name web/dashboard) also run the three pnpm gates. Set gatesGreen accordingly.
3. Invariant audit — list EACH violation in invariantViolations (inspect the story's diff via git show / git diff):
   - No random/secrets/datetime.utcnow()/time.time() introduced under src/padrino/core/.
   - Migrations: at most ONE new Alembic revision, down_revision == the prior head, no duplicate/reused number, chain stays linear.
   - No '# type: ignore' added to force green; no test deleted/skipped/weakened to pass.
   - Any additional hard rule in AGENTS.md that this story touches. In particular, IF the story concerns observation/spectator/anonymity, ratings/results, human chat, or the worker lane, verify: no human-vs-AI or model/provider identity leaks before the endgame reveal and the guard fails CLOSED to ANONYMOUS (and covers DB columns); ZERO writes to the scientific Rating/RatingEvent from a human game; human chat is out-of-band of the hash chain and deterministic replay still holds.
4. Confirm a commit 'feat: ${storyId} - ...' exists (git log --oneline) -> commitPresent; and ralph/prd.json has "passes": true for ${storyId} and ONLY the intended story flipped -> passesTrueSet.

verdict='pass' ONLY if: all gates green AND acMet AND invariantViolations is empty AND commitPresent AND passesTrueSet. Otherwise verdict='fail' with a specific, ACTIONABLE 'reasons' string the next implementation attempt can act on. Return the StructuredOutput tool.`
}

// --------------------------------------------------------------------------- Scan
phase('Scan')
const scan = await agent(
  `In the Padrino repo (${REPO}): read ralph/prd.json and run \`git branch --show-current\`. Return branchName (from prd.json), currentBranch, and for EVERY userStory an object {id, priority, passes}. Modify nothing.`,
  { label: 'scan', phase: 'Scan', schema: SCAN_SCHEMA, effort: 'low' },
)
if (!scan) {
  log('ABORT: scan agent failed.')
  return { aborted: true, reason: 'scan-failed' }
}
log(`plan branchName=${scan.branchName}; current git branch=${scan.currentBranch}; ${scan.stories.length} stories.`)

if (!scan.stories.length) {
  return { aborted: true, reason: 'no-stories', planBranch: scan.branchName }
}
if (scan.currentBranch !== scan.branchName) {
  log(`WARNING: git is on ${scan.currentBranch}, not ${scan.branchName}; the first implement agent will check it out.`)
}

const remaining = scan.stories
  .filter((s) => !s.passes && s.priority <= STOP_AT)
  .sort((a, b) => a.priority - b.priority)
const alreadyDone = scan.stories.filter((s) => s.passes).length
log(`${alreadyDone}/${scan.stories.length} already pass; building ${remaining.length}${STOP_AT < Infinity ? ` (stopAtPriority=${STOP_AT})` : ''}.`)

// --------------------------------------------------------------------------- build loop
const completed = []
const blocked = []
let stoppedReason = 'all-remaining-complete'
let currentGroup = null

for (const s of remaining) {
  const grp = `P${Math.floor(s.priority / 10)}` // e.g. 121 -> P12 ; cosmetic grouping
  if (grp !== currentGroup) {
    currentGroup = grp
    phase(grp)
  }

  let attempt = 0
  let passed = false
  let lastVerify = null
  let lastImpl = null
  while (attempt <= MAX_RETRIES) {
    const feedback = lastVerify
      ? `gatesGreen=${lastVerify.gatesGreen}; acMet=${lastVerify.acMet}; ` +
        `invariantViolations=[${(lastVerify.invariantViolations || []).join(' | ') || 'none'}]; ` +
        `reasons=${lastVerify.reasons || ''}; gateOutputTail=${(lastVerify.gateOutput || '').slice(-1200)}`
      : ''
    lastImpl = await agent(implementPrompt(s.id, attempt, feedback), {
      label: `impl:${s.id}${attempt ? `#${attempt + 1}` : ''}`,
      phase: grp,
      schema: IMPLEMENT_SCHEMA,
      effort: 'high',
    })
    lastVerify = await agent(verifyPrompt(s.id), {
      label: `verify:${s.id}${attempt ? `#${attempt + 1}` : ''}`,
      phase: grp,
      schema: VERIFY_SCHEMA,
      effort: 'high',
    })
    if (lastVerify && lastVerify.verdict === 'pass') {
      passed = true
      break
    }
    log(`x ${s.id} attempt ${attempt + 1} failed verify: ${lastVerify ? lastVerify.reasons : 'verify agent died'}`)
    attempt++
  }

  if (passed) {
    completed.push({ id: s.id, commitSha: lastImpl && lastImpl.commitSha })
    log(`ok ${s.id} done${lastImpl && lastImpl.commitSha ? ' @ ' + lastImpl.commitSha : ''}`)
  } else {
    blocked.push({ id: s.id, reasons: lastVerify ? lastVerify.reasons : 'verify agent died' })
    log(`BLOCKED ${s.id} after ${MAX_RETRIES + 1} attempts — halting (dependents would inherit a broken base).`)
    stoppedReason = `blocked:${s.id}`
    break
  }
}

// --------------------------------------------------------------------------- Report
phase('Report')
const final = await agent(
  `In the Padrino repo (${REPO}) run, and report only: ` +
    `(1) jq '[.userStories[]|select(.passes==false)]|length' ralph/prd.json -> remainingCount; ` +
    `(2) jq -r '[.userStories[]|select(.passes==false)|.id]' ralph/prd.json -> remainingIds; ` +
    `(3) git log --oneline -25 -> recentCommits (array). Modify nothing.`,
  { label: 'final-scan', phase: 'Report', schema: FINAL_SCHEMA, effort: 'low' },
)

return {
  stoppedReason,
  completed: completed.map((c) => c.id),
  blocked: blocked.map((b) => ({ id: b.id, reasons: b.reasons })),
  remainingCount: final ? final.remainingCount : null,
  remainingIds: final ? final.remainingIds : null,
  recentCommits: final ? final.recentCommits : null,
}
