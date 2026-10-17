//! UPI transaction ingestion gateway.
//!
//! Receives payment events over HTTP, validates them, and publishes them to
//! the `upi.tx.raw` Redpanda topic (keyed by `src_vpa` so a sender's events
//! stay ordered on the single partition).

mod validate;

use std::collections::BTreeMap;
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use actix_cors::Cors;
use actix_web::{get, middleware, post, web, App, HttpResponse, HttpServer};
use anyhow::{Context, Result};
use rskafka::client::error::{Error as KafkaError, ProtocolError};
use rskafka::client::partition::{Compression, PartitionClient, UnknownTopicHandling};
use rskafka::client::{Client, ClientBuilder};
use rskafka::record::Record;
use serde_json::json;
use tracing::{error, info, warn};
use tracing_subscriber::EnvFilter;
use upifraud_core::TOPIC_TX_RAW;

/// Application state: a connected Kafka client + the topic partition we publish to.
struct AppState {
    client: Arc<Client>,
    partition: Arc<PartitionClient>,
}

impl AppState {
    /// Publish one event as a JSON record keyed by `src_vpa`.
    async fn publish(&self, key: &str, payload: &[u8]) -> anyhow::Result<()> {
        let record = Record {
            key: Some(key.as_bytes().to_vec()),
            value: Some(payload.to_vec()),
            headers: BTreeMap::new(),
            timestamp: std::time::SystemTime::now().into(),
        };
        self.partition
            .produce(vec![record], Compression::NoCompression)
            .await?;
        Ok(())
    }
}

// ============================================================================
// HTTP handlers
// ============================================================================

fn bad_request(msg: impl Into<String>) -> HttpResponse {
    HttpResponse::BadRequest().json(json!({ "error": msg.into() }))
}

fn stream_unavailable() -> HttpResponse {
    HttpResponse::ServiceUnavailable().json(json!({ "error": "stream unavailable" }))
}

fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or_default()
}

/// `POST /v1/transactions` — single event ingestion.
#[post("/v1/transactions")]
async fn create_transaction(state: web::Data<AppState>, body: web::Bytes) -> HttpResponse {
    let now = now_ms();
    let value: serde_json::Value = match serde_json::from_slice(&body) {
        Ok(v) => v,
        Err(e) => return bad_request(format!("invalid JSON body: {e}")),
    };
    let event = match validate::normalize_event(value) {
        Ok(ev) => ev,
        Err(e) => return bad_request(e),
    };
    if let Err(e) = validate::validate_event(&event, now) {
        return bad_request(e);
    }
    let payload = serde_json::to_vec(&event).expect("TransactionEvent serialises");
    match state.publish(&event.src_vpa, &payload).await {
        Ok(()) => {
            info!(tx_id = %event.tx_id, src_vpa = %event.src_vpa, amount = event.amount_inr, "event accepted");
            HttpResponse::Accepted().json(json!({ "tx_id": event.tx_id, "status": "accepted" }))
        }
        Err(e) => {
            error!(tx_id = %event.tx_id, error = %format!("{e:#}"), "kafka publish failed");
            stream_unavailable()
        }
    }
}

/// `POST /v1/transactions/batch` — batch ingestion with per-item validation.
///
/// Partial success is allowed: valid items are published, invalid/failed items
/// are listed in `rejected` (with their index), and the response is still 202.
#[post("/v1/transactions/batch")]
async fn create_batch(state: web::Data<AppState>, body: web::Bytes) -> HttpResponse {
    let now = now_ms();
    let items: Vec<serde_json::Value> = match serde_json::from_slice(&body) {
        Ok(v) => v,
        Err(_) => return bad_request("invalid JSON body: expected an array of transactions"),
    };
    let mut accepted = 0usize;
    let mut rejected: Vec<serde_json::Value> = Vec::new();

    for (index, raw) in items.into_iter().enumerate() {
        // Best-effort id for diagnostics when normalisation itself fails.
        let raw_tx_id = raw
            .get("tx_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let event = match validate::normalize_event(raw) {
            Ok(ev) => ev,
            Err(e) => {
                rejected.push(json!({ "tx_id": raw_tx_id, "index": index, "error": e }));
                continue;
            }
        };
        if let Err(e) = validate::validate_event(&event, now) {
            rejected.push(json!({ "tx_id": event.tx_id, "index": index, "error": e }));
            continue;
        }
        let payload = serde_json::to_vec(&event).expect("TransactionEvent serialises");
        match state.publish(&event.src_vpa, &payload).await {
            Ok(()) => accepted += 1,
            Err(e) => {
                error!(tx_id = %event.tx_id, index, error = %format!("{e:#}"), "kafka publish failed");
                rejected.push(
                    json!({ "tx_id": event.tx_id, "index": index, "error": "stream unavailable" }),
                );
            }
        }
    }

    HttpResponse::Accepted().json(json!({ "accepted": accepted, "rejected": rejected }))
}

/// `GET /healthz` — liveness + broker reachability.
#[get("/healthz")]
async fn healthz(state: web::Data<AppState>) -> HttpResponse {
    let kafka = match tokio::time::timeout(Duration::from_secs(2), state.client.list_topics()).await
    {
        Ok(Ok(_)) => "ok",
        _ => "degraded",
    };
    HttpResponse::Ok().json(json!({ "status": "ok", "kafka": kafka }))
}

// ============================================================================
// Startup
// ============================================================================

fn init_tracing() {
    let filter = std::env::var("RUST_LOG").unwrap_or_else(|_| "info".to_string());
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::new(filter))
        .init();
}

fn env_brokers() -> Vec<String> {
    let raw = std::env::var("REDPANDA_BROKERS").unwrap_or_else(|_| "localhost:19092".to_string());
    let brokers: Vec<String> = raw
        .split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(String::from)
        .collect();
    if brokers.is_empty() {
        vec!["localhost:19092".to_string()]
    } else {
        brokers
    }
}

/// Build a Kafka client, ensure the topic exists, and grab partition 0.
async fn try_connect(brokers: &[String], topic: &str) -> Result<(Client, PartitionClient)> {
    let client = ClientBuilder::new(brokers.to_vec())
        .build()
        .await
        .context("building kafka client")?;
    let controller = client.controller_client().context("controller client")?;
    match controller.create_topic(topic, 1, 1, 5_000).await {
        Ok(()) => info!(%topic, "created topic"),
        Err(KafkaError::ServerError {
            protocol_error: ProtocolError::TopicAlreadyExists,
            ..
        }) => {}
        Err(e) => return Err(e).context("creating topic"),
    }
    let partition = client
        .partition_client(topic, 0, UnknownTopicHandling::Retry)
        .await
        .context("partition client")?;
    Ok((client, partition))
}

/// Retry with exponential backoff until Redpanda is reachable (never crash).
async fn connect_kafka(
    brokers: &[String],
    topic: &str,
) -> Result<(Arc<Client>, Arc<PartitionClient>)> {
    let mut backoff_ms: u64 = 500;
    loop {
        match try_connect(brokers, topic).await {
            Ok((client, partition)) => {
                info!(%topic, "connected to Redpanda");
                return Ok((Arc::new(client), Arc::new(partition)));
            }
            Err(e) => {
                warn!(error = %format!("{e:#}"), backoff_ms, "waiting for Redpanda");
                tokio::time::sleep(Duration::from_millis(backoff_ms)).await;
                backoff_ms = (backoff_ms * 2).min(10_000);
            }
        }
    }
}

#[actix_web::main]
async fn main() -> Result<()> {
    dotenvy::dotenv().ok();
    init_tracing();

    let brokers = env_brokers();
    let topic = std::env::var("KAFKA_TOPIC_TX_RAW").unwrap_or_else(|_| TOPIC_TX_RAW.to_string());
    let bind = std::env::var("GATEWAY_BIND").unwrap_or_else(|_| "0.0.0.0:8080".to_string());

    info!(
        brokers = %brokers.join(","),
        %topic,
        %bind,
        "gateway starting"
    );

    let (client, partition) = connect_kafka(&brokers, &topic).await?;
    let state = web::Data::new(AppState { client, partition });

    HttpServer::new(move || {
        App::new()
            .wrap(middleware::Logger::default())
            .wrap(Cors::permissive())
            .app_data(state.clone())
            .service(create_transaction)
            .service(create_batch)
            .service(healthz)
    })
    .bind(&bind)?
    .run()
    .await?;

    Ok(())
}
