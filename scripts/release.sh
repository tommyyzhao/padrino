#!/usr/bin/env bash
# Release-checklist helper for Padrino.
#
# Runs locally before the operator pushes a versioned tag. The CI release
# workflow (.github/workflows/release.yml) re-runs the test matrix and gates
# the changelog on its own, but this script catches a stale lockfile, a
# broken build, or a forgotten CHANGELOG entry before the tag pushes and
# triggers the publish pipeline.
#
# Usage:
#   ./scripts/release.sh                 # prompts before pushing
#   ./scripts/release.sh --yes           # non-interactive (CI / autopilot)
#
# Reads the version from `pyproject.toml`. The created tag is `v<version>`.

set -euo pipefail

ASSUME_YES=0
for arg in "$@"; do
  case "${arg}" in
    --yes|-y)
      ASSUME_YES=1
      ;;
    --help|-h)
      sed -n '2,18p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown argument: ${arg}" >&2
      exit 2
      ;;
  esac
done

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required (https://docs.astral.sh/uv/)" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${REPO_ROOT}"

VERSION="$(uv run --quiet python -c 'import tomllib, pathlib; print(tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]["version"])')"
TAG="v${VERSION}"

echo "==> Cutting ${TAG} from $(git rev-parse --abbrev-ref HEAD)"

echo "==> Verifying CHANGELOG.md has Unreleased entries"
uv run --quiet python <<'PY'
import pathlib
import re
import sys

text = pathlib.Path("CHANGELOG.md").read_text(encoding="utf-8")
pattern = re.compile(
    r"^## \[Unreleased\]\s*\n(?P<body>.*?)(?=^## \[)",
    re.DOTALL | re.MULTILINE,
)
match = pattern.search(text)
if not match:
    sys.stderr.write("CHANGELOG.md is missing `## [Unreleased]`.\n")
    sys.exit(1)
body = match.group("body").strip()
stripped = "\n".join(
    line for line in body.splitlines() if line.strip() and not line.lstrip().startswith("###")
).strip()
if not stripped:
    sys.stderr.write("CHANGELOG.md `## [Unreleased]` is empty; nothing to release.\n")
    sys.exit(1)
PY

echo "==> Verifying lockfile is in sync"
uv lock --frozen

echo "==> Running test suite (no integration / live_llm / docker / postgres)"
uv run pytest -m "not integration and not live_llm and not docker and not postgres"

echo "==> Building sdist + wheel"
rm -rf dist
uv build

if git rev-parse "${TAG}" >/dev/null 2>&1; then
  echo "Tag ${TAG} already exists locally; refusing to overwrite." >&2
  exit 1
fi

if [ "${ASSUME_YES}" -ne 1 ]; then
  printf '\nReady to push %s. Continue? [y/N] ' "${TAG}"
  read -r answer
  case "${answer}" in
    y|Y|yes|YES) ;;
    *)
      echo "Aborted; no tag created."
      exit 0
      ;;
  esac
fi

git tag -a "${TAG}" -m "Release ${TAG}"
git push origin "${TAG}"
echo "==> Pushed ${TAG}. The release workflow will now publish the image."
