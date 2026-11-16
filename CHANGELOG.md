# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The release workflow (`.github/workflows/release.yml`) refuses to publish a
versioned tag whose `## [Unreleased]

### Added
- Seven more rulesets beside `mini7_v1`, each with its own leaderboard:
  `bench10_v1`, `roleblock10_v1`, `visit12_v1`, `ninja13_v1`,
  `deception13_v1` (team-scored), `sk12_v1` (placement-scored Serial Killer)
  and `jester8_v1` (solo-rate-scored Jester). New roles include Roleblocker,
  Tracker, Watcher, Ninja, Godfather, Janitor, Serial Killer and Jester.
- Separate rating contexts (canonical team, placement, solo-rate) with a
  purity check that keeps non-canonical modes out of the canonical ladder.
- Seat-rotated heterogeneous tournaments (`padrino gauntlet run`), campaigns
  with hard global and per-campaign budget ceilings, resumable in-progress
  games, and a continuous matchmaker.
- Per-model leaderboards, provisional and established ratings with decay,
  Wilson confidence intervals, and deterministic plus sampled LLM-judge
  game analytics.
- SvelteKit dashboard under `web/dashboard/`: leaderboard, ladder, model
  explorer, game list and detail, gauntlet reports, live watch and recap,
  with Playwright end-to-end tests.
- Spoiler-safe public surface: a public-surface-only API mode, live and
  recent game indexes, a fail-closed moderation gate, and a Server-Sent
  Events live endpoint with a live-tail mode and cadence pacing.
- Narrated live watch and record mode, with optional local text-to-speech
  (`PADRINO_TTS_PROVIDER`) served to loopback clients only.
- `padrino showcase`: one anonymous, unranked game through any
  OpenAI-compatible gateway, with a per-slug preflight, a replay `--seed`,
  and a chat `--language` rule.
- Casual human multiplayer: private friend lobbies, guest and OAuth sign-in,
  one-tap consent with an age gate, anonymous-by-default identity mode, human
  chat stored outside the hash chain, a separate human-game worker lane with
  durable state, AI takeover of disconnected seats, an endgame reveal and a
  spot-the-AI guess.
- Casual custom games from hash-addressed `GameRulesSnapshot` values that
  replay from the archived snapshot.
- Human ratings in separate tables behind default-off kill switches, with
  ranked eligibility, bans, an audit trail, rollback and `padrino
  ranked-audit`. Human games write no scientific rating rows.
- OpenRouter integration: immutable model catalog snapshots, qualification
  tiers, bring-your-own-key routing with envelope encryption, optional
  per-account subkeys, and an append-only integer-micro-USD cost ledger.
- Postgres as a first-class database target, a single-host backup runbook,
  and `padrino game verify-restore` to check a restored game's hash chain.
- Operational alerting (webhook notifier and Prometheus alert rules), budget
  and cost-drift gauges, a retention executor, and a git-history secret audit.
- Same-model multi-host fallback in the LiteLLM adapter.
- `docs/architecture.md` and `docs/design-decisions.md`; README rewritten
  for the current feature set.
- Release CI workflow (`.github/workflows/release.yml`) that triggers on `v*`
  tags, runs the test matrix on Ubuntu and macOS, builds the Dockerfile,
  publishes the image to `ghcr.io/<owner>/padrino:<version>` plus `:latest`,
  and signs the image with cosign keyless OIDC.
- `scripts/release.sh` release-checklist helper that runs `uv lock --frozen`,
  `uv run pytest -m "not integration"`, builds the sdist + wheel via
  `uv build`, and prompts the operator before pushing the tag.
- `tests/release/test_metadata.py` package-metadata invariants: `padrino
  version` matches `pyproject.toml`, the `padrino` console-script entry
  point is stable, and no test module imports a private
  (underscore-prefixed) name from another package boundary.

### Changed
- `padrino.__version__` is now read from installed package metadata via
  `importlib.metadata.version("padrino")` rather than a hardcoded literal
  in `src/padrino/__init__.py`, so the source of truth is `pyproject.toml`.
- CI runs on `ubuntu-latest` and `macos-latest` with Python 3.12. Python 3.13
  is not currently exercised in CI (an async test-harness issue), although the
  package metadata still allows it.
- Gauntlet game seeds derive from the gauntlet seed alone, so a re-run with
  the same seed reproduces the same per-game seeds.
- Duplicated `utcnow`, `as_utc` and canonical-JSON helpers are consolidated into
  `padrino.clock`.
- Ingestion docs now state the actual ruleset whitelist (`mini7_v1` and
  `bench10_v1`); `docs/production-readiness.md` is marked as a historical
  snapshot with a current-status table.

### Fixed
- Mid-game role and faction leak: public event reads for a non-terminal game
  now go through the spectator projection, which drops private and system
  events and strips role and faction keys.
- Rating writes are idempotent per game, agent build and scope (unique
  constraint `uq_rating_event_scope`).
- Agent JSON extraction tolerates `<think>` blocks, Markdown code fences and
  surrounding prose instead of failing the turn.
- Narration phase cues and captions now match the server's speech templates.

## [0.1.0] - 2026-05-18

### Added
- Initial wave-2 release: deterministic event-sourced engine, async game
  runner, async gauntlet scheduler, public ingestion + federated leaderboard
  read API, signed game-export bundles, bundled Dockerfile +
  docker-compose stack, `padrino bootstrap` one-command deployment, and
  Prometheus `/metrics` endpoint. See `docs/deployment/` for operator
  guides.

[Unreleased]: https://github.com/tommyyzhao/padrino/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/tommyyzhao/padrino/releases/tag/v0.1.0
