//! Kafka/Redpanda topic names and consumer group ids.

/// Raw UPI transaction events, produced by the gateway.
pub const TOPIC_TX_RAW: &str = "upi.tx.raw";
/// Risk decisions, produced by the feature-worker.
pub const TOPIC_DECISIONS: &str = "upi.decisions";
/// High-risk alerts (BLOCK / CHALLENGE), produced by the feature-worker.
pub const TOPIC_ALERTS: &str = "upi.alerts";

/// Consumer group for the feature-worker consuming raw transactions.
pub const GROUP_FEATURE_WORKER: &str = "feature-worker";
/// Consumer group for stream-api consuming alerts.
pub const GROUP_STREAM_API: &str = "stream-api";
