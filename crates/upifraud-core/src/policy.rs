//! Risk policy: score blending and action thresholds.
//!
//! ```text
//! score >= 0.80  → BLOCK     (settlement blocked + 24h cooldown)
//! 0.50 <= score  → CHALLENGE (step-up biometric / video OTP)
//! otherwise      → ALLOW     (authorize instantly)
//! ```

use crate::types::{DecisionAction, ScoreResponse};

/// Default score at/above which a transaction is blocked.
pub const RISK_BLOCK_THRESHOLD_DEFAULT: f64 = 0.80;
/// Default score at/above which a step-up challenge is issued.
pub const RISK_CHALLENGE_THRESHOLD_DEFAULT: f64 = 0.50;
/// Default ensemble weight for the GNN topological score.
pub const GNN_WEIGHT_DEFAULT: f64 = 0.6;
/// Default ensemble weight for the XGBoost behavioral score.
pub const TABULAR_WEIGHT_DEFAULT: f64 = 0.4;

/// Runtime policy configuration (loaded from environment by each service).
#[derive(Debug, Clone, Copy)]
pub struct PolicyConfig {
    pub block_threshold: f64,
    pub challenge_threshold: f64,
    pub gnn_weight: f64,
    pub tabular_weight: f64,
}

impl Default for PolicyConfig {
    fn default() -> Self {
        Self {
            block_threshold: RISK_BLOCK_THRESHOLD_DEFAULT,
            challenge_threshold: RISK_CHALLENGE_THRESHOLD_DEFAULT,
            gnn_weight: GNN_WEIGHT_DEFAULT,
            tabular_weight: TABULAR_WEIGHT_DEFAULT,
        }
    }
}

impl PolicyConfig {
    /// Load from env vars (`RISK_BLOCK_THRESHOLD`, `RISK_CHALLENGE_THRESHOLD`,
    /// `GNN_SCORE_WEIGHT`, `TABULAR_SCORE_WEIGHT`), falling back to defaults.
    /// Unparsable values are ignored (default kept).
    pub fn from_env() -> Self {
        let mut cfg = Self::default();
        if let Ok(v) = std::env::var("RISK_BLOCK_THRESHOLD") {
            if let Ok(f) = v.parse() {
                cfg.block_threshold = f;
            }
        }
        if let Ok(v) = std::env::var("RISK_CHALLENGE_THRESHOLD") {
            if let Ok(f) = v.parse() {
                cfg.challenge_threshold = f;
            }
        }
        if let Ok(v) = std::env::var("GNN_SCORE_WEIGHT") {
            if let Ok(f) = v.parse() {
                cfg.gnn_weight = f;
            }
        }
        if let Ok(v) = std::env::var("TABULAR_SCORE_WEIGHT") {
            if let Ok(f) = v.parse() {
                cfg.tabular_weight = f;
            }
        }
        cfg
    }

    /// Map a blended risk score to a decision action.
    pub fn decide(&self, score: f64) -> DecisionAction {
        if score >= self.block_threshold {
            DecisionAction::Block
        } else if score >= self.challenge_threshold {
            DecisionAction::Challenge
        } else {
            DecisionAction::Allow
        }
    }

    /// Blend GNN + tabular scores (weights normalized to sum to 1).
    pub fn blend(&self, gnn: f64, tabular: f64) -> f64 {
        blend_with(gnn, tabular, self.gnn_weight, self.tabular_weight)
    }

    /// Fill the blended score + model metadata in a `ScoreResponse`.
    pub fn finalize_score(&self, mut resp: ScoreResponse) -> ScoreResponse {
        resp.blended_score = self.blend(resp.gnn_score, resp.tabular_score);
        resp
    }
}

fn blend_with(gnn: f64, tabular: f64, gnn_w: f64, tabular_w: f64) -> f64 {
    let total = gnn_w + tabular_w;
    if total <= 0.0 {
        return 0.5 * gnn + 0.5 * tabular;
    }
    (gnn * gnn_w + tabular * tabular_w) / total
}

/// Convenience: default-policy action for a score.
pub fn decide(score: f64) -> DecisionAction {
    PolicyConfig::default().decide(score)
}

/// Convenience: default-weight blend.
pub fn blend(gnn: f64, tabular: f64) -> f64 {
    blend_with(gnn, tabular, GNN_WEIGHT_DEFAULT, TABULAR_WEIGHT_DEFAULT)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn thresholds_map_to_actions() {
        assert_eq!(decide(0.95), DecisionAction::Block);
        assert_eq!(decide(0.80), DecisionAction::Block);
        assert_eq!(decide(0.79), DecisionAction::Challenge);
        assert_eq!(decide(0.50), DecisionAction::Challenge);
        assert_eq!(decide(0.49), DecisionAction::Allow);
        assert_eq!(decide(0.0), DecisionAction::Allow);
    }

    #[test]
    fn blend_uses_default_weights() {
        // 0.6 * 1.0 + 0.4 * 0.0 = 0.6
        assert!((blend(1.0, 0.0) - 0.6).abs() < 1e-9);
        assert!((blend(0.5, 0.5) - 0.5).abs() < 1e-9);
    }

    #[test]
    fn custom_policy_overrides_thresholds() {
        let cfg = PolicyConfig {
            block_threshold: 0.9,
            challenge_threshold: 0.3,
            ..PolicyConfig::default()
        };
        assert_eq!(cfg.decide(0.91), DecisionAction::Block);
        assert_eq!(cfg.decide(0.35), DecisionAction::Challenge);
        assert_eq!(cfg.decide(0.1), DecisionAction::Allow);
    }
}
