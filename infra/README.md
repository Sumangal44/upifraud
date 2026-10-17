# Infrastructure assets

## `memgraph/`
- `schema.cypher` — node/relationship constraints and indexes.
- `queries.cypher` — reference queries (mule paths, fan-in/out, shared devices,
  2-hop neighbourhood extraction used by `feature-worker`).

Schema is applied idempotently by `tools/seed/seed.py` (`make seed`).

## `flink/`
`velocity_job.sql` is the **production-path** streaming job: continuous
1h/24h windowed velocity aggregation feeding Dragonfly. The Rust
`feature-worker` implements the same counters inline (INCR + TTL) so the MVP
has no Flink dependency; the SQL job documents the intended Flink CEP/Table
API implementation for Phase 3 scale-out (25k TPS, per-key state in RocksDB,
checkpointed to S3/object storage).

## Ports (docker-compose defaults)
| Component | Port | Protocol |
| :--- | :--- | :--- |
| Redpanda (internal) | 9092 | Kafka |
| Redpanda (host) | 19092 | Kafka |
| Redpanda Schema Registry (host) | 18081 | HTTP |
| Redpanda Admin/metrics | 9644 | HTTP |
| Redpanda Console | 8086 | HTTP |
| Dragonfly | 6379 | Redis |
| Memgraph | 7687 | Bolt |
| Triton (GPU profile) | 8000 | HTTP |
| vLLM (GPU profile) | 8003 | OpenAI-compatible HTTP |
