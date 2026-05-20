# Federated ingestion — trust model and threat model (US-075)

`POST /ingest/game` is the public ingress for a federated leaderboard. A
self-hoster runs games locally, exports a signed bundle, and pushes it to a
central node. This document lays out exactly what guarantees the route makes
and which abuse classes are explicitly out of scope.

## What a submitter API key authorizes

A key with the `submitter` scope (or any `admin` key) may:

- POST `/ingest/game` once per `game_id`. Re-posting the same `game_id`
  returns `200 {"already_ingested": true}` with no new write.
- Optionally bind an Ed25519 `submission_public_key` to the key. When set,
  signed bundles whose `sig` verifies against that key are stored
  `verification_status="verified"`; everything else is stored
  `"unverified"`.

A submitter key does NOT authorize:

- Any read endpoint outside the public surface — see `central-backend.md`
  for the scope matrix.
- Issuing further keys, mutating other submitters' rows, or deleting
  bundles.

## What gets verified on every POST

The route performs these checks, in order, before any row is written:

1. **Request size cap.** Both the `Content-Length` header (when present)
   and the actual streamed body length are bounded by
   `MAX_BUNDLE_BYTES = 10 MiB`. A request that lies about Content-Length
   (claims 100 bytes, streams 10 MB) is cut off mid-stream and returns
   `413 bundle_too_large` — the full body is never buffered.
2. **Bundle schema.** `GameBundle.model_validate_json` rejects unknown
   fields (`extra="forbid"`) and missing required fields with `422`.
3. **Idempotency.** If `bundle.game_id` already exists in `ingested_games`,
   the route short-circuits with `200 {already_ingested: true}` and skips
   every check below. This is the federated equivalent of "exactly-once
   delivery": a duplicate POST never produces a duplicate row, regardless
   of whether it is a benign retry or a replay attack.
4. **Payload safety guard.** `assert_bundle_payload_safe` rejects bundles
   whose event payloads contain any forbidden provenance key
   (`agent_build_id`, `model_name`, `provider`, `rating`, `api_key`, etc.).
5. **Ruleset whitelist.** `ruleset_id` must be one of the currently
   shipped rulesets (today: `mini7_v1`). Unknown ids return
   `422 {"error": "unknown_ruleset"}`.
6. **Hash-chain replay.** `verify_chain` replays every event envelope
   through the same hasher the runner uses (`event_type`, `sequence`,
   `phase`, `visibility`, `actor_player_id`, `payload`; `event_hash`,
   `prev_event_hash`, `created_at` excluded). The recomputed tip must
   equal `bundle.tip_hash` — any mutation to any event's payload triggers
   `422 {"error": "hash_chain_mismatch"}`.
7. **Terminal consistency.** When `bundle.terminal_result.winner` is set,
   the event log MUST contain a `GameTerminated` event whose
   `payload.winner` matches. A bundle that claims a winner the events
   don't record (or that claims TOWN while the events say MAFIA) returns
   `422 {"error": "inconsistent_terminal"}`.
8. **Signature verification.** When `bundle.sig` is present, the
   submitter key must have a registered `submission_public_key` and the
   signature must verify against `canonical_bundle_bytes(bundle)` —
   `cryptography`'s Ed25519 `verify` is constant-time. Mismatches return
   `422 {"error": "signature_mismatch"}`. Unsigned bundles persist as
   `verification_status="unverified"`.
9. **Per-key rate limit.** Every authenticated request passes through
   the US-074 rate limiter (`Settings.padrino_rate_limit_submitter_per_minute`,
   default 30/min). Bursts above the ceiling return `429` with a
   `Retry-After` header.

## What is NOT verified

The route deliberately does NOT attempt:

- **Cross-submitter consistency.** Two submitters can disagree about the
  outcome of "the same" game-shaped scenario. The route stores both
  bundles under their independent `game_id`s; reconciliation is a
  leaderboard concern, not an ingestion concern.
- **Real-time provenance.** The route does not call back to the
  submitter's host to confirm the bundle came from a real LLM run.
  Anyone with a `submitter` key can craft a bundle whose hash chain is
  internally consistent. The signature only proves the bundle came from
  the holder of the registered private key — not that the underlying
  game was actually played.
- **Model identity claims.** `agent_builds[*].model_name` is a free-text
  field. The route does not verify that the named model was actually
  invoked. Operators who care should run their own gauntlet on the
  central node; ingested bundles are advisory.
- **Submitter pseudonymity.** Bundles carry the submitter's
  `api_keys.id` as `submitter_key_id` in the row, but the public surface
  (`/public/games/*`) strips submitter PII via `_scrub_bundle`. A
  determined operator with admin scope can still link a bundle back to
  its submitter — anonymity is not a guarantee.

## Abuse classes addressed

| Class | Defense |
|---|---|
| Replayed bundle (same `game_id`) | Idempotent `200 already_ingested`; no duplicate row written. |
| Tampered event payload | Hash-chain replay fails; `422 hash_chain_mismatch`. |
| Tampered signature | Ed25519 verify fails; `422 signature_mismatch`. |
| Signed bundle from unregistered key | `422 signature_unverifiable`. |
| Oversized bundle (honest Content-Length) | `413 bundle_too_large` before any parsing. |
| Oversized bundle (lying Content-Length) | Streamed read cuts off at `MAX_BUNDLE_BYTES`; `413 bundle_too_large`. |
| Unknown ruleset id | `422 unknown_ruleset`. |
| Inconsistent terminal claim (winner without GameTerminated event) | `422 inconsistent_terminal`. |
| Forbidden provenance key smuggled into a payload | `422 unsafe_payload`. |
| Sybil clones from one key | Per-key rate limiter (US-074) caps `submitter` scope at 30/min by default; multi-worker deployments share state via `DatabaseRateLimitStore`. |
| Timing oracle on bearer tokens | Lookup is by sha256 digest equality — the digest is precomputed and indistinguishable between any two invalid keys. |
| Role/faction/private-message leak on non-terminal bundle | `/public/games/{id}/events` redacts forbidden payload keys and filters `visibility=PRIVATE` events when the bundle is non-terminal. |

## Non-goals

- **DDoS resistance** beyond the per-key rate limit. Production
  deployments should put a CDN or reverse proxy in front of the API.
- **Cryptographic identity beyond Ed25519.** Submitters who lose their
  private key must register a new one; key rotation is operator-driven.
- **Content-level fairness.** The route does not score, rank, or filter
  bundles by perceived quality.

## Operator checklist

When adding a new submitter:

1. Issue a new API key with scope `submitter` via `POST /admin/keys`.
2. (Recommended.) Register the submitter's Ed25519 public key on the
   row so future bundles are stored `verified` rather than `unverified`.
3. If the new submitter is expected to push more than 30 bundles/min,
   raise `padrino_rate_limit_submitter_per_minute` in the deployment
   `.env` — but only after confirming the upstream CDN can absorb the
   resulting traffic.

When ingesting a bundle fails:

1. `413` — re-export with the runner; bundles should typically be under
   1 MiB. A 10 MiB bundle indicates a misuse of the event log, not a
   legitimate game.
2. `422 hash_chain_mismatch` — re-run `padrino-export` on the same game;
   do not hand-edit the JSON.
3. `422 inconsistent_terminal` — the local game did not terminate;
   wait for `GameTerminated` before exporting.
4. `422 unknown_ruleset` — upgrade the central node to the same Padrino
   release as the submitter, or downgrade the submitter to match.
5. `429` — back off and retry per the `Retry-After` header; if persistent,
   request a higher ceiling from the operator.
