# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The release workflow (`.github/workflows/release.yml`) refuses to publish a
versioned tag whose `## [Unreleased]` section is empty — every release must
move its bullets out of `## [Unreleased]` into a dated `## [x.y.z]` heading
before pushing the `v*` tag.

## [Unreleased]

### Added
- Release CI workflow (`.github/workflows/release.yml`) that triggers on `v*`
  tags, runs the full test matrix on Ubuntu and macOS for Python 3.12 and
  3.13, builds the Dockerfile, publishes the image to
  `ghcr.io/<owner>/padrino:<version>` plus `:latest`, and signs the image
  with cosign keyless OIDC.
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
- CI matrix extended to `macos-latest` alongside `ubuntu-latest` and Python
  3.13 alongside 3.12 (3.11 dropped from CI now that the release surface is
  pinned to 3.12 / 3.13).

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
