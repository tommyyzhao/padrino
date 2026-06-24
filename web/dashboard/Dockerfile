# Multi-stage build for the SvelteKit dashboard.
# Stage 1 builds the static SPA; stage 2 serves it via `vite preview`.

FROM node:22-alpine AS builder
WORKDIR /app

RUN corepack enable && corepack prepare pnpm@11.1.3 --activate

COPY package.json pnpm-lock.yaml* pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile=false

COPY . .

ARG VITE_PADRINO_API_BASE_URL=http://api:8000
ENV VITE_PADRINO_API_BASE_URL=${VITE_PADRINO_API_BASE_URL}

RUN pnpm build

FROM node:22-alpine AS runtime
WORKDIR /app

RUN corepack enable && corepack prepare pnpm@11.1.3 --activate

COPY --from=builder /app/package.json ./package.json
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/build ./build
COPY --from=builder /app/svelte.config.js ./svelte.config.js
COPY --from=builder /app/vite.config.ts ./vite.config.ts

EXPOSE 5173

CMD ["pnpm", "preview", "--host", "0.0.0.0", "--port", "5173"]
