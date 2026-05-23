#!/usr/bin/env bash
# US-076: Scan every commit reachable from any ref for credential-shaped
# strings. Prints commit SHA + file path to stdout; the full matched line is
# only written to .padrino_audit/audit.log (which is gitignored) so the
# script itself can never re-leak a secret into a CI log.
#
# Exit codes:
#   0 — no secret-shaped strings found in any commit on any ref
#   1 — at least one match found (see .padrino_audit/audit.log for redacted detail)
#   2 — invocation / environment error
#
# Allowlist (path-based, evaluated against the diff's file path):
#   .env.example                     — empty key template, values absent by convention
#   docs/**                          — placeholders like `sk-...` and inline cassette stubs
#   tests/**                         — synthetic probes used by the cassette-scrub audit (US-072)
#   web/dashboard/pnpm-lock.yaml,
#   uv.lock, package-lock.json, ... — generated lock files contain base64 integrity
#                                      hashes whose substrings collide with the `lw...` prefix
#   scripts/audit_git_history_for_secrets.sh — the script defines the patterns it scans for
#   .padrino_audit/**                — the script's own output
#
# The pattern set is intentionally narrow: it favours specific provider
# prefixes (csk-, lw, sk-, sk-ant-, tp-, ghp_, gho_) plus the populated
# `*_API_KEY=<value>` shape over generic high-entropy heuristics, because
# false positives in CI are noisy and erode trust in the audit.

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/audit_git_history_for_secrets.sh [--help]

Scans `git log -p --all` for credential-shaped strings. Exits non-zero on
match. Redacted findings stream to .padrino_audit/audit.log; stdout only
ever shows `<commit-sha>\t<file-path>` pairs.
EOF
}

case "${1:-}" in
  -h|--help) usage; exit 0;;
esac

if ! command -v git >/dev/null 2>&1; then
  echo "git is required" >&2
  exit 2
fi

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [ -z "${REPO_ROOT}" ]; then
  echo "Not inside a git working tree." >&2
  exit 2
fi
cd "${REPO_ROOT}"

AUDIT_DIR=".padrino_audit"
AUDIT_LOG="${AUDIT_DIR}/audit.log"
mkdir -p "${AUDIT_DIR}"
: > "${AUDIT_LOG}"

# Pattern alternation. Anchored prefixes plus a minimum body length keep
# false positives down on common English words, base64 fragments, and
# placeholder text. The `_API_KEY=` pattern requires a value of >= 20
# alphanumeric / `-` / `_` characters, so `.env.example`-shaped empty
# templates and `...` placeholders never match.
PATTERN='(csk-[A-Za-z0-9_-]{20,}|lw[A-Za-z0-9_-]{30,}|sk-ant-[A-Za-z0-9_-]{32,}|sk-[A-Za-z0-9_-]{32,}|tp-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{36,}|gho_[A-Za-z0-9]{36,}|[A-Z][A-Z0-9_]*_API_KEY=[A-Za-z0-9_-]{20,})'

# Stream the full history through awk. The `__COMMIT__` sentinel is unlikely
# to collide with any natural diff content; if a future commit literally
# contains that string in a file it would be misclassified as a commit
# boundary — acceptable because the consequence is at worst attributing a
# finding to the wrong SHA, never silencing one.
git log --all --no-merges --pretty=format:'__COMMIT__ %H' -p \
  | awk \
      -v pat="${PATTERN}" \
      -v log_file="${AUDIT_LOG}" \
      '
function is_allowlisted(path,    _name) {
  if (path == ".env.example") return 1
  if (path == "scripts/audit_git_history_for_secrets.sh") return 1
  if (path == "docs/deployment/credential-rotation.md") return 1
  if (path ~ /^\.padrino_audit\//) return 1
  if (path ~ /^docs\//) return 1
  if (path ~ /^tests\//) return 1
  # Generated lock files — base64 integrity hashes can incidentally contain
  # provider-prefix substrings and we never expect real secrets in lock data.
  _name = path
  sub(/^.*\//, "", _name)
  if (_name == "pnpm-lock.yaml") return 1
  if (_name == "package-lock.json") return 1
  if (_name == "uv.lock") return 1
  if (_name == "Cargo.lock") return 1
  if (_name == "yarn.lock") return 1
  if (_name == "poetry.lock") return 1
  return 0
}
BEGIN {
  found = 0
  sha = ""
  file = ""
  delete seen
}
/^__COMMIT__ / {
  sha = $2
  file = ""
  next
}
/^diff --git / {
  raw = $0
  sub(/^diff --git a\//, "", raw)
  sub(/ b\/.*$/, "", raw)
  file = raw
  next
}
{
  if (file == "") next
  if (is_allowlisted(file)) next
  if (match($0, pat) > 0) {
    key = sha "\t" file
    if (!(key in seen)) {
      seen[key] = 1
      print key
    }
    # Redacted finding: full line goes to gitignored log, never to stdout.
    printf "%s\t%s\t%s\n", sha, file, $0 >> log_file
    found = 1
  }
}
END { exit (found ? 1 : 0) }
'
