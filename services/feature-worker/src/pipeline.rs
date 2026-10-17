//! Per-transaction pipeline: velocity → subgraph → inference → policy.
//!
//! Stage timings are recorded in the `LatencyBreakdown` of the decision:
//! `ingest_ms` is the record decode/ingest time inside the worker,
//! `feature_ms` covers the Redis velocity pipeline **and** the Memgraph
//! subgraph extraction (same stage budget), `inference_ms` the score call
//! (or fallback), and `policy_ms` reason derivation + action mapping.

use std::collections::BTreeMap;
use std::sync::Arc;
use std::time::Instant;

use neo4rs::Graph;
use redis::aio::ConnectionManager;
use rskafka::client::partition::{Compression, PartitionClient};
use rskafka::record::Record;
use upifraud_core::policy::PolicyConfig;
use upifraud_core::{
    Alert, Decision, DecisionAction, FeatureVector, LatencyBreakdown, TransactionEvent,
};

use crate::{reasons, scoring, store, subgraph, velocity};

/// Everything the pipeline needs to process one transaction.
pub struct Worker {
    pub redis: ConnectionManager,
    pub graph: Graph,
    pub http: reqwest::Client,
    pub inference_url: String,
    pub decisions: Arc<PartitionClient>,
    pub alerts: Arc<PartitionClient>,
    pub policy: PolicyConfig,
}

/// Outcome of [`Worker::process`].
pub struct Processed {
    pub decision: Decision,
    pub alert: Option<Alert>,
    pub features: FeatureVector,
    pub action_str: &'static str,
}

pub fn now_ms() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or_default()
}

impl Worker {
    /// Run all stages for a single event.
    ///
    /// Only a hard Redis failure aborts the transaction (`Err`) — subgraph and
    /// inference failures degrade gracefully (empty subgraph / fallback score).
    pub async fn process(
        &self,
        ev: &TransactionEvent,
        ingest_ms: u64,
    ) -> anyhow::Result<Processed> {
        let now_ms = now_ms();

        // --- stage 1: features (velocity + subgraph) ----------------------
        let t = Instant::now();
        let vel = velocity::compute(&self.redis, ev, now_ms).await?;
        let extracted = subgraph::extract(&self.graph, &ev.src_vpa, &ev.dst_vpa, now_ms).await;
        let feature_ms = t.elapsed().as_millis() as u64;

        let features = FeatureVector {
            tx_id: ev.tx_id.clone(),
            velocity: vel.velocity,
            dst_account_age_hours: extracted.dst_account_age_hours,
            device_age_hours: vel.device_age_hours,
            geo_jump_kmh: vel.geo_jump_kmh,
            amount_zscore: vel.amount_zscore,
            ip_risk_score: velocity::ip_risk_score(&ev.src_ip),
            subgraph: extracted.subgraph,
        };

        // --- stage 2: inference (fallback on any failure) -----------------
        let t = Instant::now();
        let score = scoring::score(
            &self.http,
            &self.inference_url,
            &ev.tx_id,
            &features,
            ev.amount_inr,
            &self.policy,
        )
        .await;
        let inference_ms = t.elapsed().as_millis() as u64;

        // --- stage 3: policy ---------------------------------------------
        let t = Instant::now();
        let reason_list = reasons::derive_reasons(ev, &features);
        let action = self.policy.decide(score.blended_score);
        let action_str = store::action_str(action);
        let policy_ms = t.elapsed().as_millis() as u64;

        let latency = LatencyBreakdown {
            ingest_ms,
            feature_ms,
            inference_ms,
            policy_ms,
            total_ms: ingest_ms + feature_ms + inference_ms + policy_ms,
        };

        let decision = Decision {
            tx_id: ev.tx_id.clone(),
            timestamp_ms: now_ms,
            score: score.blended_score,
            action,
            reasons: reason_list.clone(),
            top_factors: score.top_factors,
            model_version: score.model_version,
            latency,
        };

        let alert = if action == DecisionAction::Allow {
            None
        } else {
            Some(Alert {
                tx_id: ev.tx_id.clone(),
                timestamp_ms: now_ms,
                score: decision.score,
                action,
                reasons: reason_list,
                src_vpa: ev.src_vpa.clone(),
                dst_vpa: ev.dst_vpa.clone(),
                amount_inr: ev.amount_inr,
                model_version: decision.model_version.clone(),
            })
        };

        Ok(Processed {
            decision,
            alert,
            features,
            action_str,
        })
    }
}

/// Produce one JSON record to a partition, keyed by `key`.
pub async fn publish(partition: &PartitionClient, key: &str, payload: &[u8]) -> anyhow::Result<()> {
    let record = Record {
        key: Some(key.as_bytes().to_vec()),
        value: Some(payload.to_vec()),
        headers: BTreeMap::new(),
        timestamp: std::time::SystemTime::now().into(),
    };
    partition
        .produce(vec![record], Compression::NoCompression)
        .await?;
    Ok(())
}
