# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# SOC portal (Next.js 15 standalone).
#
# NOTE: docker-compose.yml builds this with the REPO ROOT as context
#   build: { context: ., dockerfile: apps/portal/Dockerfile }
# so all COPY paths are prefixed with apps/portal/ and the app is copied to
# /app directly — that keeps the standalone output at /app/.next/standalone.
# ---------------------------------------------------------------------------

FROM node:22-alpine AS builder

# NEXT_PUBLIC_* values are inlined into the client bundle at build time.
# Override via `--build-arg NEXT_PUBLIC_STREAM_API_URL=…` when needed.
ARG NEXT_PUBLIC_STREAM_API_URL=http://localhost:8081
ENV NEXT_PUBLIC_STREAM_API_URL=${NEXT_PUBLIC_STREAM_API_URL}
ENV NEXT_TELEMETRY_DISABLED=1

WORKDIR /app
COPY apps/portal/package*.json ./
RUN npm ci
COPY apps/portal/ .
RUN npm run build

FROM node:22-alpine AS runner

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

WORKDIR /app

COPY --from=builder --chown=node:node /app/.next/standalone ./
COPY --from=builder --chown=node:node /app/.next/static ./.next/static
COPY --from=builder --chown=node:node /app/public ./public

USER node
EXPOSE 3000

CMD ["node", "server.js"]
