//! Wire formats shared across the platform.
//!
//! JSON is the on-the-wire encoding everywhere (Kafka payloads, HTTP bodies,
//! Redis values). Field names are `snake_case` to match the Python/TS mirrors.

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

// ============================================================================
// Ingestion
// ============================================================================

/// Geolocation attached to a payment event.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Geo {
    pub lat: f64,
    pub lon: f64,
    #[serde(default)]
    pub city: Option<String>,
}

/// Endpoint/device telemetry reported by the mobile SDK.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct DeviceInfo {
    /// Salted hash of the device fingerprint — never the raw identifiers.
    pub device_hash: String,
    #[serde(default)]
    pub app_version: Option<String>,
    /// Device is rooted / jailbroken.
    #[serde(default)]
    pub rooted: bool,
    /// Screen-mirroring / remote-control tooling detected (AnyDesk, TeamViewer…).
    #[serde(default)]
    pub screen_mirror: bool,
    /// Android emulator / virtualized environment detected.
    #[serde(default)]
    pub emulator: bool,
}

/// A UPI payment event as submitted to the gateway.
///
/// NOTE: UPI PINs, full MSISDNs and account numbers are NEVER part of this
/// payload (RBI/CERT-In data-minimisation rule) — only salted hashes.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TransactionEvent {
    /// Client-generated unique transaction id (UUID v4).
    pub tx_id: String,
    /// Event time, unix epoch milliseconds.
    pub timestamp_ms: i64,
    /// Sender VPA, e.g. `alice@okaxis`.
    pub src_vpa: String,
    /// Payee VPA, e.g. `merchant@paytm`.
    pub dst_vpa: String,
    /// Amount in INR.
    pub amount_inr: f64,
    pub device: DeviceInfo,
    /// Source IPv4/IPv6 address.
    pub src_ip: String,
    pub geo: Geo,
    /// Set for merchant payments (collect/request flows leave it null).
    #[serde(default)]
    pub merchant_id: Option<String>,
    /// Optional remittance note.
    #[serde(default)]
    pub note: Option<String>,
}

// ============================================================================
// Features
// ============================================================================

/// Rolling velocity counters computed from Dragonfly/Redis.
#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct VelocityFeatures {
    /// Transactions from `src_vpa` in the last hour.
    pub src_count_1h: u32,
    /// Total INR moved by `src_vpa` in the last hour.
    pub src_amount_1h: f64,
    /// Transactions from `src_vpa` in the last 24h.
    pub src_count_24h: u32,
    /// Total INR moved by `src_vpa` in the last 24h.
    pub src_amount_24h: f64,
    /// Transactions received by `dst_vpa` in the last hour.
    pub dst_count_1h: u32,
    /// Distinct senders that paid `dst_vpa` in the last hour.
    pub dst_distinct_senders_1h: u32,
    /// Transactions seen from the same device hash in the last hour.
    pub device_count_1h: u32,
    /// Transactions seen from the same source IP in the last hour.
    pub ip_count_1h: u32,
}

/// A node in the extracted neighbourhood subgraph.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct GraphNode {
    /// Stable synthetic id, e.g. `Account:alice@okaxis`, `Device:ab12…`.
    pub id: String,
    /// Node label: `Account` | `Device` | `IPAddress` | `Merchant`.
    pub label: String,
    /// Display-safe properties (e.g. `created_at_ms`, `risk_flags`).
    #[serde(default)]
    pub props: BTreeMap<String, serde_json::Value>,
}

/// An edge in the extracted neighbourhood subgraph.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct GraphEdge {
    pub src: String,
    pub dst: String,
    /// `TRANSFERRED` | `USED_DEVICE` | `CONNECTED_FROM` | `PAID_MERCHANT`.
    pub rel: String,
    #[serde(default)]
    pub amount: Option<f64>,
    #[serde(default)]
    pub timestamp_ms: Option<i64>,
}

/// 2-hop heterogeneous neighbourhood subgraph around the transaction parties.
#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct Subgraph {
    pub nodes: Vec<GraphNode>,
    pub edges: Vec<GraphEdge>,
}

/// Assembled feature bundle handed to the inference pipeline.
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct FeatureVector {
    pub tx_id: String,
    pub velocity: VelocityFeatures,
    /// Hours since the destination account first appeared in the graph.
    pub dst_account_age_hours: f64,
    /// Hours since the device hash was first seen.
    pub device_age_hours: f64,
    /// Implied speed between consecutive transaction geolocations (km/h).
    pub geo_jump_kmh: f64,
    /// Log-1p amount z-score against the sender's 24h history.
    pub amount_zscore: f64,
    /// Risk score of the source IP (0..1) from IP reputation feed.
    pub ip_risk_score: f64,
    pub subgraph: Subgraph,
}

// ============================================================================
// Inference
// ============================================================================

/// POST /v1/score request body (feature-worker → inference service).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ScoreRequest {
    pub tx_id: String,
    pub features: FeatureVector,
}

/// A single contributing factor returned with a score (for XAI / SOC brief).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ScoreFactor {
    /// Feature name, e.g. `velocity.src_count_1h`.
    pub name: String,
    /// Signed contribution to the blended score (positive → riskier).
    pub weight: f64,
}

/// POST /v1/score response body.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ScoreResponse {
    pub tx_id: String,
    /// Topological risk score from the GNN (RGCN/GraphSAGE) model, 0..1.
    pub gnn_score: f64,
    /// Behavioral anomaly score from the XGBoost model, 0..1.
    pub tabular_score: f64,
    /// Weighted blend of the two (see `policy::blend`), 0..1.
    pub blended_score: f64,
    /// e.g. `rgcn-v1+xgb-v1` or `heuristic-v0`.
    pub model_version: String,
    #[serde(default)]
    pub top_factors: Vec<ScoreFactor>,
}

// ============================================================================
// Decisions & alerts
// ============================================================================

/// Final disposition of a transaction.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "UPPERCASE")]
pub enum DecisionAction {
    /// Authorize instantly.
    Allow,
    /// Step-up challenge (biometric / video OTP).
    Challenge,
    /// Block settlement + account cooldown.
    Block,
}

/// Per-stage latency accounting (ms) for SLA dashboards.
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct LatencyBreakdown {
    pub ingest_ms: u64,
    pub feature_ms: u64,
    pub inference_ms: u64,
    pub policy_ms: u64,
    pub total_ms: u64,
}

/// The risk decision produced for every transaction.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Decision {
    pub tx_id: String,
    /// Decision time, unix epoch milliseconds.
    pub timestamp_ms: i64,
    /// Blended risk score, 0..1.
    pub score: f64,
    pub action: DecisionAction,
    /// Human-readable reason codes, e.g. `HIGH_VELOCITY_1H`, `MULE_PATH_2HOP`.
    pub reasons: Vec<String>,
    #[serde(default)]
    pub top_factors: Vec<ScoreFactor>,
    pub model_version: String,
    pub latency: LatencyBreakdown,
}

/// Alert pushed to the SOC dashboard (subset of `Decision` + context).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Alert {
    pub tx_id: String,
    pub timestamp_ms: i64,
    pub score: f64,
    pub action: DecisionAction,
    pub reasons: Vec<String>,
    pub src_vpa: String,
    pub dst_vpa: String,
    pub amount_inr: f64,
    pub model_version: String,
}

// ============================================================================
// Explainability (XAI service)
// ============================================================================

/// POST /v1/explain request (stream-api → XAI service).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExplainRequest {
    pub tx_id: String,
    pub decision: Decision,
    pub features: FeatureVector,
}

/// POST /v1/explain response.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExplainResponse {
    pub tx_id: String,
    /// Feature attributions (GNNExplainer masks / SHAP values), sorted desc.
    pub attributions: Vec<ScoreFactor>,
    /// Natural-language SOC brief generated by the local LLM (or template).
    pub brief: String,
    /// e.g. `gnnexplainer+qwen2.5-7b` or `heuristic+template`.
    pub engine: String,
}
