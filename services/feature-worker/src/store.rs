//! Redis/Dragonfly persistence for decisions, features and stats.
//!
//! Key scheme (shared contract with the Python tools — do not rename):
//! - `decision:{tx_id}`  HASH {json: Decision}   TTL 86400
//! - `features:{tx_id}`  HASH {json: FeatureVector (incl. subgraph)} TTL 86400
//! - `subgraph:{tx_id}`  HASH {json: Subgraph}   TTL 86400
//! - `decisions:recent`  LIST, newest first (LPUSH + LTRIM 0 999)
//! - `stats:actions`     HASH {ALLOW/CHALLENGE/BLOCK: count}

use redis::aio::ConnectionManager;
use upifraud_core::{Decision, DecisionAction, FeatureVector};

const DAY_SECS: i64 = 86_400;
const RECENT_MAX: isize = 999;

/// Uppercase wire name of an action (`ALLOW` / `CHALLENGE` / `BLOCK`).
pub fn action_str(action: DecisionAction) -> &'static str {
    match action {
        DecisionAction::Allow => "ALLOW",
        DecisionAction::Challenge => "CHALLENGE",
        DecisionAction::Block => "BLOCK",
    }
}

/// Persist everything the portal/SOC needs, in one pipeline.
pub async fn persist(
    con: &ConnectionManager,
    decision: &Decision,
    features: &FeatureVector,
) -> anyhow::Result<()> {
    let decision_json = serde_json::to_string(decision)?;
    let features_json = serde_json::to_string(features)?;
    let subgraph_json = serde_json::to_string(&features.subgraph)?;

    let decision_key = format!("decision:{}", decision.tx_id);
    let features_key = format!("features:{}", decision.tx_id);
    let subgraph_key = format!("subgraph:{}", decision.tx_id);

    let mut pipe = redis::pipe();
    pipe.cmd("HSET")
        .arg(&decision_key)
        .arg("json")
        .arg(&decision_json)
        .ignore();
    pipe.cmd("EXPIRE").arg(&decision_key).arg(DAY_SECS).ignore();
    pipe.cmd("HSET")
        .arg(&features_key)
        .arg("json")
        .arg(&features_json)
        .ignore();
    pipe.cmd("EXPIRE").arg(&features_key).arg(DAY_SECS).ignore();
    pipe.cmd("HSET")
        .arg(&subgraph_key)
        .arg("json")
        .arg(&subgraph_json)
        .ignore();
    pipe.cmd("EXPIRE").arg(&subgraph_key).arg(DAY_SECS).ignore();
    pipe.cmd("LPUSH")
        .arg("decisions:recent")
        .arg(&decision_json)
        .ignore();
    pipe.cmd("LTRIM")
        .arg("decisions:recent")
        .arg(0)
        .arg(RECENT_MAX)
        .ignore();
    pipe.cmd("HINCRBY")
        .arg("stats:actions")
        .arg(action_str(decision.action))
        .arg(1)
        .ignore();

    let mut con = con.clone();
    pipe.exec_async(&mut con).await?;
    Ok(())
}
