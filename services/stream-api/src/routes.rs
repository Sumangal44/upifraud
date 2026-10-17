//! REST endpoints served to the SOC portal.

use std::collections::HashMap;

use actix_web::{get, web, HttpResponse};
use redis::AsyncCommands;
use serde_json::json;
use tracing::warn;
use upifraud_core::{
    Decision, DecisionAction, ExplainRequest, ExplainResponse, FeatureVector, Subgraph,
};

const DEFAULT_LIST_LIMIT: usize = 50;
const MAX_LIST_LIMIT: usize = 200;
const RECENT_SCAN_DEPTH: isize = 999; // LPUSH/LTRIM keeps 1000 entries
const DAY_SECS: i64 = 86_400;

/// Shared application state.
pub struct AppState {
    pub redis: redis::aio::ConnectionManager,
    pub xai: reqwest::Client,
    pub xai_url: String,
    pub alerts: tokio::sync::broadcast::Sender<String>,
}

fn bad_request(msg: &str) -> HttpResponse {
    HttpResponse::BadRequest().json(json!({ "error": msg }))
}

fn not_found() -> HttpResponse {
    HttpResponse::NotFound().json(json!({ "error": "not found" }))
}

fn storage_unavailable() -> HttpResponse {
    HttpResponse::InternalServerError().json(json!({ "error": "storage unavailable" }))
}

fn xai_unavailable() -> HttpResponse {
    HttpResponse::BadGateway().json(json!({ "error": "xai unavailable" }))
}

fn action_str(action: DecisionAction) -> &'static str {
    match action {
        DecisionAction::Allow => "ALLOW",
        DecisionAction::Challenge => "CHALLENGE",
        DecisionAction::Block => "BLOCK",
    }
}

/// Read a `{json: …}` hash written by the feature-worker.
async fn hget_json(
    con: &mut redis::aio::ConnectionManager,
    key: &str,
) -> Result<Option<String>, redis::RedisError> {
    con.hget(key, "json").await
}

/// `GET /healthz`
#[get("/healthz")]
pub async fn healthz() -> HttpResponse {
    HttpResponse::Ok().json(json!({ "status": "ok" }))
}

/// `GET /v1/decisions?limit=50&action=BLOCK`
#[get("/v1/decisions")]
pub async fn list_decisions(
    state: web::Data<AppState>,
    query: web::Query<HashMap<String, String>>,
) -> HttpResponse {
    let limit = match query.get("limit") {
        None => DEFAULT_LIST_LIMIT,
        Some(raw) => match raw.parse::<usize>() {
            Ok(0) | Err(_) => {
                return bad_request("limit must be an integer between 1 and 200");
            }
            Ok(n) => n.min(MAX_LIST_LIMIT),
        },
    };
    let action_filter = match query.get("action") {
        None => None,
        Some(raw) => {
            let upper = raw.to_ascii_uppercase();
            match upper.as_str() {
                "ALLOW" | "CHALLENGE" | "BLOCK" => Some(upper),
                _ => return bad_request("action must be ALLOW, CHALLENGE or BLOCK"),
            }
        }
    };

    let mut con = state.redis.clone();
    let rows: Vec<String> = match con.lrange("decisions:recent", 0, RECENT_SCAN_DEPTH).await {
        Ok(v) => v,
        Err(e) => {
            warn!(error = %e, "failed to read decisions:recent");
            return storage_unavailable();
        }
    };

    let mut decisions: Vec<Decision> = Vec::new();
    for row in rows {
        if decisions.len() >= limit {
            break;
        }
        let decision: Decision = match serde_json::from_str(&row) {
            Ok(d) => d,
            Err(e) => {
                warn!(error = %e, "skipping malformed entry in decisions:recent");
                continue;
            }
        };
        if let Some(want) = &action_filter {
            if action_str(decision.action) != want {
                continue;
            }
        }
        decisions.push(decision);
    }

    HttpResponse::Ok().json(json!({ "decisions": decisions }))
}

/// `GET /v1/decisions/{tx_id}`
#[get("/v1/decisions/{tx_id}")]
pub async fn get_decision(state: web::Data<AppState>, path: web::Path<String>) -> HttpResponse {
    let tx_id = path.into_inner();
    let key = format!("decision:{tx_id}");
    let mut con = state.redis.clone();
    let raw = match hget_json(&mut con, &key).await {
        Ok(v) => v,
        Err(e) => {
            warn!(error = %e, %key, "failed to read decision");
            return storage_unavailable();
        }
    };
    match raw {
        None => not_found(),
        Some(body) => match serde_json::from_str::<Decision>(&body) {
            Ok(decision) => HttpResponse::Ok().json(decision),
            Err(e) => {
                warn!(error = %e, %key, "corrupt decision record — treating as not found");
                not_found()
            }
        },
    }
}

/// `GET /v1/decisions/{tx_id}/graph` — empty subgraph on miss (not 404).
#[get("/v1/decisions/{tx_id}/graph")]
pub async fn get_graph(state: web::Data<AppState>, path: web::Path<String>) -> HttpResponse {
    let tx_id = path.into_inner();
    let key = format!("subgraph:{tx_id}");
    let mut con = state.redis.clone();
    match hget_json(&mut con, &key).await {
        Ok(Some(raw)) => match serde_json::from_str::<Subgraph>(&raw) {
            Ok(subgraph) => HttpResponse::Ok().json(subgraph),
            Err(e) => {
                warn!(error = %e, %key, "corrupt subgraph record — returning empty");
                HttpResponse::Ok().json(Subgraph::default())
            }
        },
        Ok(None) => HttpResponse::Ok().json(Subgraph::default()),
        Err(e) => {
            warn!(error = %e, %key, "failed to read subgraph");
            // Storage trouble must not break the graph panel: degrade to empty.
            HttpResponse::Ok().json(Subgraph::default())
        }
    }
}

/// `GET /v1/decisions/{tx_id}/explanation` — cached XAI result, computed on miss.
#[get("/v1/decisions/{tx_id}/explanation")]
pub async fn get_explanation(state: web::Data<AppState>, path: web::Path<String>) -> HttpResponse {
    let tx_id = path.into_inner();
    let mut con = state.redis.clone();

    // 1. cache hit?
    let expl_key = format!("expl:{tx_id}");
    match hget_json(&mut con, &expl_key).await {
        Ok(Some(raw)) => {
            if let Ok(cached) = serde_json::from_str::<ExplainResponse>(&raw) {
                return HttpResponse::Ok().json(cached);
            }
            warn!(%expl_key, "corrupt explanation cache — recomputing");
        }
        Ok(None) => {}
        Err(e) => {
            warn!(error = %e, %expl_key, "failed to read explanation cache");
            return storage_unavailable();
        }
    }

    // 2. the decision must exist (404 otherwise)
    let decision_key = format!("decision:{tx_id}");
    let decision_raw = match hget_json(&mut con, &decision_key).await {
        Ok(v) => v,
        Err(e) => {
            warn!(error = %e, %decision_key, "failed to read decision");
            return storage_unavailable();
        }
    };
    let decision: Decision = match decision_raw.and_then(|s| serde_json::from_str(&s).ok()) {
        Some(d) => d,
        None => return not_found(),
    };

    // 3. features (absent → defaults, per contract)
    let features_key = format!("features:{tx_id}");
    let features: FeatureVector = match hget_json(&mut con, &features_key).await {
        Ok(Some(raw)) => serde_json::from_str(&raw).unwrap_or_default(),
        Ok(None) => FeatureVector::default(),
        Err(e) => {
            warn!(error = %e, %features_key, "failed to read features");
            FeatureVector::default()
        }
    };

    // 4. call the XAI service (2 s timeout)
    let url = format!("{}/v1/explain", state.xai_url.trim_end_matches('/'));
    let request = ExplainRequest {
        tx_id: tx_id.clone(),
        decision,
        features,
    };
    let explanation = match state.xai.post(&url).json(&request).send().await {
        Ok(resp) if resp.status().is_success() => resp.json::<ExplainResponse>().await.ok(),
        Ok(resp) => {
            warn!(%url, status = %resp.status(), "xai returned error status");
            None
        }
        Err(e) => {
            warn!(error = %e, %url, "xai request failed");
            None
        }
    };
    let explanation = match explanation {
        Some(e) => e,
        None => return xai_unavailable(),
    };

    // 5. cache for 24 h (best-effort)
    match serde_json::to_string(&explanation) {
        Ok(json_body) => {
            let mut pipe = redis::pipe();
            pipe.cmd("HSET")
                .arg(&expl_key)
                .arg("json")
                .arg(&json_body)
                .ignore();
            pipe.cmd("EXPIRE").arg(&expl_key).arg(DAY_SECS).ignore();
            if let Err(e) = pipe.exec_async(&mut con).await {
                warn!(error = %e, %expl_key, "failed to cache explanation");
            }
        }
        Err(e) => warn!(error = %e, "explanation serialisation failed"),
    }

    HttpResponse::Ok().json(explanation)
}

/// `GET /v1/stats` — decision action counters.
#[get("/v1/stats")]
pub async fn get_stats(state: web::Data<AppState>) -> HttpResponse {
    let mut con = state.redis.clone();
    let counts: HashMap<String, i64> = match con.hgetall("stats:actions").await {
        Ok(v) => v,
        Err(e) => {
            warn!(error = %e, "failed to read stats:actions");
            return storage_unavailable();
        }
    };
    HttpResponse::Ok().json(json!({
        "ALLOW": counts.get("ALLOW").copied().unwrap_or(0),
        "CHALLENGE": counts.get("CHALLENGE").copied().unwrap_or(0),
        "BLOCK": counts.get("BLOCK").copied().unwrap_or(0),
    }))
}
