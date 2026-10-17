//! Inference client + local fallback heuristic.
//!
//! The remote inference service (`POST {INFERENCE_URL}/v1/score`) may be down
//! or slow — **any** failure (connect, timeout, non-2xx, malformed body) falls
//! back to a deterministic in-process heuristic so the pipeline keeps making
//! decisions. The blended score is always recomputed here with
//! `PolicyConfig` (the policy lives in core, not in the inference service).

use std::time::Duration;

use tracing::warn;
use upifraud_core::policy::PolicyConfig;
use upifraud_core::{FeatureVector, ScoreRequest, ScoreResponse, VelocityFeatures};

/// Model version reported when the fallback heuristic is used.
pub const FALLBACK_MODEL_VERSION: &str = "local-fallback-v0";

/// Deterministic tabular fallback (documented stand-in for XGBoost):
/// `0.15·min(count,10)/10 + 0.2·ip_risk + 0.25·new_dst + 0.2·shared_device
///  + 0.2·large_amount`, clamped to [0, 1].
pub fn fallback_tabular(
    velocity: &VelocityFeatures,
    ip_risk_score: f64,
    dst_account_age_hours: f64,
    amount_inr: f64,
) -> f64 {
    let score = 0.15 * (velocity.src_count_1h.min(10) as f64) / 10.0
        + 0.2 * ip_risk_score
        + 0.25
            * if dst_account_age_hours < 24.0 {
                1.0
            } else {
                0.0
            }
        + 0.2
            * if velocity.device_count_1h > 3 {
                1.0
            } else {
                0.0
            }
        + 0.25 * if amount_inr > 50_000.0 { 1.0 } else { 0.0 };
    score.clamp(0.0, 1.0)
}

/// Build the fallback `ScoreResponse` (GNN contributes ~0 without a model).
pub fn fallback_score(tx_id: &str, features: &FeatureVector, amount_inr: f64) -> ScoreResponse {
    ScoreResponse {
        tx_id: tx_id.to_string(),
        gnn_score: 0.0,
        tabular_score: fallback_tabular(
            &features.velocity,
            features.ip_risk_score,
            features.dst_account_age_hours,
            amount_inr,
        ),
        blended_score: 0.0, // filled by `PolicyConfig::finalize_score`
        model_version: FALLBACK_MODEL_VERSION.to_string(),
        top_factors: Vec::new(),
    }
}

/// Score one transaction: try the inference service, fall back on any failure,
/// and always finalise the blend with the local policy.
pub async fn score(
    http: &reqwest::Client,
    base_url: &str,
    tx_id: &str,
    features: &FeatureVector,
    amount_inr: f64,
    policy: &PolicyConfig,
) -> ScoreResponse {
    let url = format!("{}/v1/score", base_url.trim_end_matches('/'));
    let request = ScoreRequest {
        tx_id: tx_id.to_string(),
        features: features.clone(),
    };

    let remote = match http.post(&url).json(&request).send().await {
        Ok(resp) if resp.status().is_success() => match resp.json::<ScoreResponse>().await {
            Ok(mut body) => {
                body.tx_id = tx_id.to_string(); // never trust the remote echo
                Some(body)
            }
            Err(e) => {
                warn!(error = %e, %url, "inference returned malformed JSON");
                None
            }
        },
        Ok(resp) => {
            warn!(%url, status = %resp.status(), "inference returned error status");
            None
        }
        Err(e) => {
            warn!(error = %e, %url, "inference unreachable");
            None
        }
    };

    let response = remote.unwrap_or_else(|| fallback_score(tx_id, features, amount_inr));
    policy.finalize_score(response)
}

/// Convenience wrapper used by the pipeline (timeout comes from the client
/// configuration built with `INFER_TIMEOUT_MS`).
pub fn client_with_timeout(timeout_ms: u64) -> anyhow::Result<reqwest::Client> {
    let d = Duration::from_millis(timeout_ms);
    Ok(reqwest::Client::builder()
        .timeout(d)
        .connect_timeout(d)
        .build()?)
}

#[cfg(test)]
mod tests {
    use super::*;
    use upifraud_core::policy::{decide, PolicyConfig};

    fn velocity(count: u32, devices: u32) -> VelocityFeatures {
        VelocityFeatures {
            src_count_1h: count,
            device_count_1h: devices,
            ..VelocityFeatures::default()
        }
    }

    #[test]
    fn fallback_is_zero_when_nothing_is_suspicious() {
        let v = velocity(0, 0);
        assert_eq!(fallback_tabular(&v, 0.0, 500.0, 100.0), 0.0);
    }

    #[test]
    fn fallback_accumulates_signal_terms() {
        // only NEW_DST_ACCOUNT fires → exactly 0.25
        assert_eq!(fallback_tabular(&velocity(0, 0), 0.0, 1.0, 100.0), 0.25);
        // only SHARED_DEVICE fires → 0.2
        assert_eq!(fallback_tabular(&velocity(0, 4), 0.0, 500.0, 100.0), 0.2);
        // only LARGE_AMOUNT fires → 0.25
        assert_eq!(
            fallback_tabular(&velocity(0, 0), 0.0, 500.0, 60_000.0),
            0.25
        );
    }

    #[test]
    fn fallback_caps_at_velocity_term() {
        // count ≥ 10 → full 0.15 velocity term.
        let v = velocity(25, 0);
        assert!((fallback_tabular(&v, 0.0, 500.0, 10.0) - 0.15).abs() < 1e-12);
        // half of the 10-count ramp → 0.075
        assert!((fallback_tabular(&velocity(5, 0), 0.0, 500.0, 10.0) - 0.075).abs() < 1e-12);
    }

    #[test]
    fn fallback_is_clamped_to_unit_interval() {
        // 0.15 + 0.2·0.99 + 0.25 + 0.2 + 0.25 = 1.098 → 1.0
        let v = velocity(10, 4);
        assert_eq!(fallback_tabular(&v, 0.99, 1.0, 60_000.0), 1.0);
    }

    #[test]
    fn fallback_response_is_finalised_by_policy() {
        let features = FeatureVector {
            dst_account_age_hours: 1.0,
            ..FeatureVector::default()
        };
        let policy = PolicyConfig::default();
        let resp = fallback_score("tx-1", &features, 100.0);
        assert_eq!(resp.model_version, FALLBACK_MODEL_VERSION);
        let resp = policy.finalize_score(resp);
        // tabular 0.25 → blended 0.4 · 0.25 = 0.10 → ALLOW
        assert!((resp.blended_score - 0.10).abs() < 1e-9);
        assert_eq!(
            decide(resp.blended_score),
            upifraud_core::DecisionAction::Allow
        );
    }
}
