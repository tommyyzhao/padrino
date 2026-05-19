# Padrino dashboard

A spectator dashboard for a running [Padrino](https://github.com/anthropics/padrino)
backend. SvelteKit 2 + TypeScript + Tailwind + shadcn-svelte-style components.
Pure read-only: the dashboard never POSTs.

## Routes

| Path             | What it shows                                                    |
| ---------------- | ---------------------------------------------------------------- |
| `/`              | KPIs — total games, active gauntlets, top 3 models.              |
| `/leaderboard`   | Per-model leaderboard with Global/Town/Mafia tabs (US-067).      |
| `/games`         | Paginated list of completed + in-flight games.                   |
| `/games/[id]`    | Replay scrubber — phase-by-phase event log, role-safe projection. |

The replay viewer only ever calls `/public/games/{id}/events` — never the
admin `/games/{id}/events` route, so private mafia chat / role payloads can
never reach the browser. Role assignments and the `GameTerminated` payload
are additionally redacted until the game is terminal.

## Dev setup

```bash
pnpm install
cp .env.example .env       # optional; defaults to http://localhost:8000
pnpm dev                   # http://localhost:5173
```

Quality gates (runs the same way under CI / `release` workflows):

```bash
pnpm lint
pnpm check
pnpm test
pnpm build
```

The unit tests cover the `PadrinoClient` API helper and the replay scrubber's
phase grouping / navigation / role-redaction logic.

## Pointing at a remote backend

Set `VITE_PADRINO_API_BASE_URL` at build or dev time:

```bash
VITE_PADRINO_API_BASE_URL=https://padrino.example.com pnpm dev
VITE_PADRINO_API_BASE_URL=https://padrino.example.com pnpm build
```

## API key handling

When `Settings.padrino_public_leaderboard_anonymous=True` the dashboard works
entirely against `/public/*` endpoints and no key is required. For a deployment
that gates the API behind spectator keys, the dashboard will prompt for a key
and stash it in `sessionStorage` for the duration of the tab — never in
`localStorage`, never in cookies. Clearing the tab clears the key.

## Build output

`pnpm build` produces a static SPA under `build/` via
`@sveltejs/adapter-static`. Any static file server (nginx, caddy, vite preview,
docker `dashboard` service) can serve it; the backend URL is baked into the
build via `VITE_PADRINO_API_BASE_URL`.

## Docker

The repo root `docker-compose.yml` exposes the dashboard as the `dashboard`
service on port 5173, gated on the API's healthcheck. See the comments in that
file for environment-variable overrides.
