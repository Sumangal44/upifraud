# ADR-0001: Pure-Rust Kafka client (rskafka) instead of rdkafka/librdkafka

**Status**: Accepted · **Date**: 2026-10

## Context
The gateway, feature-worker and stream-api all need Kafka-protocol access to
Redpanda. The ecosystem default is `rdkafka`, which compiles/bundles
librdkafka (C): requires cmake, a C toolchain and libsasl2 on every developer
machine and in every builder image, and ties TLS/SASL configuration to system
libraries.

## Decision
Use [`rskafka`](https://docs.rs/rskafka) 0.6 (InfluxData, pure Rust).

## Consequences
- ✅ `cargo build` works out-of-the-box on macOS/Linux with no native deps;
  builder images stay `rust:*-slim` with no build-essential/cmake.
- ✅ Memory-safe, async-native, single-digit-ms produce latency — fits the
  <100 ms end-to-end budget.
- ⚠️ **No consumer-group coordination / offset commits.** Each consumer tracks
  its own resume offset (rebuild on rebalance or restart from Earliest/Latest
  by service). For production multi-instance deployments this must be revisited
  (either rskafka group support or an external coordinator).
- ⚠️ Fewer throughput tuning knobs than librdkafka; acceptable at the MVP's
  volume and re-evaluate at the 25 k TPS load-test gate (Phase 3).

## Alternatives considered
`rdkafka` (rejected: native toolchain cost), HTTP produce via Redpanda
Pandaproxy (rejected: consume path still needs Kafka API), `neo4rs`-style
hand-rolled protocol (rejected: maintenance burden).
