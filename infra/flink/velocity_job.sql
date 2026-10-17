-- ============================================================================
-- UPI velocity features — Apache Flink Table API (production path)
--
-- The Rust feature-worker computes the same counters inline against
-- Dragonfly (INCR + TTL). This job is the Phase-3 scale-out path: keyed
-- state, RocksDB backend, checkpointed to object storage, sinking straight
-- into Dragonfly via the Redis dialect connector.
--
-- Source topic: upi.tx.raw (JSON — see crates/upifraud-core TransactionEvent)
-- ============================================================================

CREATE TABLE upi_events (
  tx_id        STRING,
  timestamp_ms BIGINT,
  src_vpa      STRING,
  dst_vpa      STRING,
  amount_inr   DOUBLE,
  device_hash  STRING,
  src_ip       STRING,
  event_time   AS TO_TIMESTAMP_LTZ(timestamp_ms, 3),
  WATERMARK FOR event_time AS event_time - INTERVAL '5' SECOND
) WITH (
  'connector' = 'kafka',
  'topic'     = 'upi.tx.raw',
  'properties.bootstrap.servers' = 'redpanda:9092',
  'format'    = 'json'
);

-- 1-hour sliding velocity per sender (count + amount) --------------------------
CREATE TABLE velocity_1h AS
SELECT
  src_vpa,
  COUNT(*)                AS src_count_1h,
  SUM(amount_inr)         AS src_amount_1h,
  MAX(event_time)         AS last_event
FROM TABLE(
  HOP(TABLE upi_events, DESCRIPTOR(event_time), INTERVAL '5' MINUTE, INTERVAL '1' HOUR)
)
GROUP BY src_vpa;

-- 24-hour tumbling aggregates + distinct receiver/fan-in ------------------------
CREATE TABLE velocity_24h AS
SELECT
  src_vpa,
  COUNT(*)                             AS src_count_24h,
  SUM(amount_inr)                      AS src_amount_24h,
  COUNT(DISTINCT dst_vpa)              AS dst_distinct_24h
FROM TABLE(
  TUMBLE(TABLE upi_events, DESCRIPTOR(event_time), INTERVAL '24' HOUR)
)
GROUP BY src_vpa;

-- Sink example: Redis/Dragonfly upsert ------------------------------------------
-- CREATE TABLE dragonfly_sink (
--   key   STRING,
--   value STRING,
--   PRIMARY KEY (key) NOT ENFORCED
-- ) WITH (
--   'connector'       = 'redis',
--   'mode'            = 'PIPELINED',
--   'host'            = 'dragonfly',
--   'port'            = '6379'
-- );
