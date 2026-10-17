//! Shared domain contract for the UPI fraud detection platform.
//!
//! This crate is the single source of truth for the wire formats exchanged
//! between services:
//!
//! * `TransactionEvent`  — gateway → Redpanda (`upi.tx.raw`)
//! * `ScoreRequest` / `ScoreResponse` — feature-worker ↔ inference service
//! * `Decision`          — feature-worker → Redpanda (`upi.decisions`) + Dragonfly
//! * `Alert`             — feature-worker → Redpanda (`upi.alerts`) → stream-api → portal
//! * `ExplainRequest` / `ExplainResponse` — stream-api ↔ XAI service
//!
//! The Python services mirror these shapes in `ml/common/schemas.py` and the
//! portal mirrors them in `apps/portal/lib/types.ts`. Keep all three in sync.

pub mod policy;
pub mod topics;
pub mod types;

pub use policy::{decide, blend, RISK_BLOCK_THRESHOLD_DEFAULT, RISK_CHALLENGE_THRESHOLD_DEFAULT};
pub use topics::{TOPIC_ALERTS, TOPIC_DECISIONS, TOPIC_TX_RAW};
pub use types::*;
