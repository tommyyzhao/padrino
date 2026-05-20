# Credential rotation — operational checklist (US-076)

This document is the one-shot runbook for rotating every credential Padrino
uses against a live provider, plus the recurring posture that keeps a leaked
key from ever surviving more than a few minutes.

The recurring secret-history audit
([`scripts/audit_git_history_for_secrets.sh`](../../scripts/audit_git_history_for_secrets.sh))
is the *passive* defence — it scans every commit reachable from any ref for
credential-shaped strings and fails CI on a match. The rotation flow below
is the *active* defence — what a maintainer does when a key is suspected
leaked, when a contributor rotates out of the project, or as part of the
pre-launch hygiene pass.

The audit and the rotation flow are deliberately decoupled: the audit
treats the *content* of `.env` as out of scope (it is gitignored) and
relies on the rotation flow to keep the file fresh. The rotation flow in
turn relies on the audit to catch the case where a key escaped `.env` and
landed somewhere reachable from a ref.

## Credentials in scope

| Env var                 | Provider   | Where used                                                     | Format prefix |
| ----------------------- | ---------- | -------------------------------------------------------------- | ------------- |
| `CEREBRAS_API_KEY`      | Cerebras   | Primary LLM provider (real-LLM integration tests, live games)  | `csk-`        |
| `DEEPINFRA_API_KEY`     | DeepInfra  | Fallback LLM provider (cassette re-records, real-LLM gauntlet) | `lw`          |
| `OPENAI_API_KEY`        | OpenAI     | Optional — only if `padrino bootstrap` provisions OpenAI       | `sk-`         |
| `ANTHROPIC_API_KEY`     | Anthropic  | Optional — only if `padrino bootstrap` provisions Anthropic    | `sk-ant-`     |
| `OLLAMA_HOST` + headers | Ollama     | Self-hosted; no key by default                                 | n/a           |
| `POSTGRES_PASSWORD`     | docker-compose Postgres | bundled `postgres` service password                | n/a           |

All values live in the **project root `.env`** file, which is gitignored
(`.gitignore` lines under `# Environment`). The shipped
[`.env.example`](../../.env.example) lists every key with an empty value
and is the contract source-of-truth for what a fresh checkout needs to
populate.

## Rotation flow — per credential

1. **Revoke the current key at the provider console.**
   - **Cerebras:** dashboard → API Keys → revoke. The `csk-...` key is
     destroyed; existing in-flight requests 401 within seconds.
   - **DeepInfra:** dashboard → API Keys → revoke. `lw...` keys may take up
     to a minute to fully invalidate cached sessions; wait one minute
     before assuming revocation took.
   - **OpenAI / Anthropic:** standard dashboard revoke. Both surface a
     "last used at" timestamp — sanity-check against the audit log if
     you suspect compromise.
   - **GitHub PATs:** if any rotation involves a `ghp_*` / `gho_*` token
     used by CI, revoke under github.com → Settings → Developer settings
     → Personal access tokens.

2. **Generate a fresh key at the provider console.** Copy it once; the
   provider will never re-display it.

3. **Update the local `.env`.** Edit the matching line in place — do **not**
   commit. The file is gitignored; `git status` should show no change
   after the edit.

4. **Verify the new key works:**

   ```bash
   uv run pytest -m integration -k real_providers --maxfail=1
   ```

   The gated integration suite (`tests/integration/test_real_providers*.py`)
   exercises the primary + fallback adapters end-to-end. A 401 here means
   the rotation didn't take.

5. **Confirm the new key never appears in git history:**

   ```bash
   bash scripts/audit_git_history_for_secrets.sh
   ```

   Exits 0 on a clean repo. If the audit fires with the new key's SHA, the
   key has already leaked — revoke immediately at the provider, generate
   another, and start the rewrite-history conversation below.

6. **Update any deployed environments** (docker-compose, container
   registries, GitHub Actions secrets) that consumed the old key.
   `docker compose up -d --force-recreate` is sufficient for the bundled
   stack; per-host secret managers are out of scope here.

7. **Sign off:** initial + date the line at the bottom of this file under
   "Rotation log".

## When the audit finds a match in history

The audit prints only `<commit-sha>\t<file-path>` to stdout; the full
matched line is written to `.padrino_audit/audit.log` (gitignored). The
log is also produced as a CI artifact-less side effect — operators read it
locally to confirm what was matched.

Two paths forward, in order of preference:

### Path A — Rotate and document the historical exposure

If the leaked key has already been revoked, the historical reference is no
longer a live credential. In that case:

1. Confirm the provider console shows the leaked key as revoked.
2. Add an allowlist note to this file (next section) recording the SHA,
   the file path, the rotation date, and the operator's initials.
3. Re-run `bash scripts/audit_git_history_for_secrets.sh` to confirm the
   audit still fails — that is the expected state, and the CI job is
   allowed to stay red on exactly the SHA(s) listed in the allowlist.
   Update the allowlist matcher in the script *only* if the maintainer
   has reviewed and approved the addition (this is a manual edit, not an
   automated step).

This path is fast and reversible. It is the right answer for a key that
was committed to a non-default branch, observed and revoked within
minutes, and never actually used by a third party.

### Path B — Rewrite history with `git filter-repo` / BFG

If the leak persisted long enough that the key was likely scraped, or the
maintainer wants a clean public history before going open-source, rewrite:

1. Coordinate with every contributor — history rewrites invalidate every
   local clone.
2. Plan the operation:

   ```bash
   # Dry-run first; never push --force without a sign-off.
   git filter-repo --replace-text <(echo 'csk-EXAMPLE==>REDACTED')
   ```

   The `--replace-text` rule file MUST live in a private location — do not
   commit the rule file to the repo since it itself would contain the
   leaked value.
3. Force-push to every remote that mirrors the repo.
4. Ask GitHub support to purge cached views of the affected commits.

This path is **never** taken by the autonomous Ralph agent — it rewrites
shared history and requires the maintainer's explicit go-ahead. The audit
script's exit code 1 is the trigger for a human decision, not an
automated rewrite.

## Rotation log

Append a row whenever a key is rotated, in the format
`YYYY-MM-DD — <env var> — <initials> — <reason>`. Keep the line concise;
the audit log under `.padrino_audit/` is the forensic record, this is the
operator-facing summary.

- 2026-05-20 — `CEREBRAS_API_KEY` — TZ — pre-launch baseline rotation (US-076)
- 2026-05-20 — `DEEPINFRA_API_KEY` — TZ — pre-launch baseline rotation (US-076)

## Allowlisted historical SHAs

This section is empty on a clean repo. If the secret-history audit ever
finds a match that the maintainer has accepted (Path A above), record the
SHA and the rotation date here so future maintainers can verify the audit
is failing for a known, documented reason rather than a fresh leak.

| Commit SHA | File path | Rotated on | Operator | Notes |
| ---------- | --------- | ---------- | -------- | ----- |
| _(none)_   |           |            |          |       |

## CI integration

The `secret-audit` job
([`.github/workflows/ci.yml`](../../.github/workflows/ci.yml)) runs the
audit on every push to `main` and on every pull request that touches
`.env*`, `scripts/`, the audit script itself, or this workflow file. The
job fails on any match; the failing job is the maintainer's signal to
follow Path A or Path B above.

The audit does a full-history clone (`fetch-depth: 0`); a shallow clone
would silently miss historical commits on side branches and degrade the
audit into a HEAD-only scan.
