# syntax=docker/dockerfile:1
# ============================================================================
# gateway — UPI transaction ingestion (Actix-web → Redpanda)
# Build context = repo root (cargo workspace).
#
# BuildKit cache mounts keep the cargo registry + target dir across builds and
# are SHARED with the feature-worker/stream-api images, so workspace deps are
# compiled once instead of three times (big win on memory-constrained VMs).
# ============================================================================
FROM rust:1.99-slim-bookworm AS builder

WORKDIR /build

COPY Cargo.toml Cargo.lock ./
COPY crates/ crates/
COPY services/ services/

RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/build/target \
    cargo build --release --bin gateway \
    && cp target/release/gateway /usr/local/bin/gateway

# ----------------------------------------------------------------------------
FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/bin/gateway /usr/local/bin/gateway

EXPOSE 8080
ENV RUST_LOG=info
CMD ["gateway"]
