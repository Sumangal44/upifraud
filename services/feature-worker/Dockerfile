# syntax=docker/dockerfile:1
# ============================================================================
# feature-worker — feature assembly + scoring (Redpanda → Dragonfly/Memgraph)
# Build context = repo root (cargo workspace).
# Cargo registry/target are BuildKit cache mounts shared across the three
# Rust service images — workspace deps compile once (see gateway Dockerfile).
# ============================================================================
FROM rust:1.99-slim-bookworm AS builder

WORKDIR /build

COPY Cargo.toml Cargo.lock ./
COPY crates/ crates/
COPY services/ services/

RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/build/target \
    cargo build --release --bin feature-worker \
    && cp target/release/feature-worker /usr/local/bin/feature-worker

# ----------------------------------------------------------------------------
FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/bin/feature-worker /usr/local/bin/feature-worker

ENV RUST_LOG=info
CMD ["feature-worker"]
