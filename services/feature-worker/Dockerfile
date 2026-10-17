# ============================================================================
# feature-worker — feature assembly + scoring (Redpanda → Dragonfly/Memgraph)
# Build context = repo root (cargo workspace).
# ============================================================================
FROM rust:1.99-slim-bookworm AS builder

WORKDIR /build

# Warm the dependency cache first (better layer caching).
COPY Cargo.toml Cargo.lock ./
COPY crates/ crates/
COPY services/ services/

RUN cargo build --release --bin feature-worker

# ----------------------------------------------------------------------------
FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /build/target/release/feature-worker /usr/local/bin/feature-worker

ENV RUST_LOG=info
CMD ["feature-worker"]
