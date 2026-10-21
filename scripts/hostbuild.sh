#!/usr/bin/env bash
# ============================================================================
# hostbuild.sh — low-memory path for machines where in-container cargo builds
# exhaust the Docker VM (e.g. Docker Desktop on an 8 GB host).
#
# Builds the Rust release binaries on the HOST, stages them into .hostbuild/,
# and builds runtime-only images via docker-compose.hostbuild.yml.
#
#   ./scripts/hostbuild.sh                    # build binaries + images
#   COMPOSE_FILE=docker-compose.yml:docker-compose.hostbuild.yml docker compose up -d
#   COMPOSE_FILE=... bash scripts/e2e.sh --skip-build
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

SERVICES=(gateway feature-worker stream-api)

echo "==> cargo build --release --workspace (host)"
cargo build --release --workspace

echo "==> staging binaries into .hostbuild/"
mkdir -p .hostbuild
for s in "${SERVICES[@]}"; do
  cp "target/release/$s" ".hostbuild/$s"
  echo "    .hostbuild/$s ($(du -h ".hostbuild/$s" | cut -f1))"
done

echo "==> docker compose build (runtime-only images)"
COMPOSE_PARALLEL_LIMIT=1 docker compose \
  -f docker-compose.yml -f docker-compose.hostbuild.yml \
  build "${SERVICES[@]}"

echo "==> done. Start with:"
echo "    COMPOSE_FILE=docker-compose.yml:docker-compose.hostbuild.yml docker compose up -d"
