# CLAUDE.md

This file mirrors `AGENTS.md` for Claude-family agents (Claude Code, Anthropic SDK).

See [AGENTS.md](./AGENTS.md) for the canonical project conventions, quality gates, hard rules, and post-story checklist. Everything that applies to agents in general applies to Claude.

Claude-specific notes:

- Do NOT use Python's `random` module or `datetime.utcnow()` anywhere under `src/padrino/core/` — see AGENTS.md "Hard rule 1".
- When implementing a Ralph story, work the test-first loop: write the failing test → implement → green → refactor → commit.
- Prefer `Edit` over `Write` when modifying existing files.
- Never create documentation files (`*.md`) unless the story explicitly calls for it.
- When unsure whether a change belongs in `core/` (pure) or `runner/` / `llm/` / `db/` (impure), default to **pure core** and inject the impure dependency via constructor.
