//! Decision history + live alert stream API for the SOC portal.
//!
//! Serves recent decisions from Dragonfly over REST, computes explanations
//! via the XAI service (with a 24 h cache), and streams alerts from the
//! `upi.alerts` topic over WebSocket (consumer group `stream-api`).

mod routes;
mod ws;

use std::sync::Arc;
use std::time::Duration;

use actix_cors::Cors;
use actix_web::{middleware, web, App, HttpServer};
use anyhow::Result;
use futures::StreamExt;
use redis::aio::ConnectionManager;
use rskafka::client::consumer::{StartOffset, StreamConsumerBuilder};
use rskafka::client::error::{Error as KafkaError, ProtocolError};
use rskafka::client::partition::{PartitionClient, UnknownTopicHandling};
use rskafka::client::{Client, ClientBuilder};
use serde_json::json;
use tokio::sync::broadcast;
use tracing::{error, info, warn};
use tracing_subscriber::EnvFilter;
use upifraud_core::topics::GROUP_STREAM_API;
use upifraud_core::{TOPIC_ALERTS, TOPIC_DECISIONS};

use routes::AppState;

const MAX_BACKOFF_MS: u64 = 10_000;
const ALERT_CHANNEL_CAPACITY: usize = 256;
const XAI_TIMEOUT_SECS: u64 = 2;

#[actix_web::main]
async fn main() -> Result<()> {
    dotenvy::dotenv().ok();
    init_tracing();

    let brokers = env_brokers();
    let topic_decisions = env_var("KAFKA_TOPIC_DECISIONS", TOPIC_DECISIONS);
    let topic_alerts = env_var("KAFKA_TOPIC_ALERTS", TOPIC_ALERTS);
    let redis_host = env_var("DRAGONFLY_HOST", "localhost");
    let redis_port = env_port("DRAGONFLY_PORT", 6_379);
    let xai_url = env_var("XAI_URL", "http://localhost:8002");
    let bind = env_var("STREAM_API_BIND", "0.0.0.0:8081");

    info!(
        brokers = %brokers.join(","),
        %topic_alerts,
        %xai_url,
        %bind,
        group = %GROUP_STREAM_API,
        "stream-api starting"
    );

    // --- startup: wait for dependencies instead of crashing ---------------
    let redis = connect_redis(&redis_host, redis_port).await;
    let client = connect_kafka(&brokers).await;
    ensure_topics(&client, &[&topic_decisions, &topic_alerts]).await;
    let alerts_partition = connect_partition(&client, &topic_alerts).await;

    let xai = reqwest::Client::builder()
        .timeout(Duration::from_secs(XAI_TIMEOUT_SECS))
        .connect_timeout(Duration::from_secs(XAI_TIMEOUT_SECS))
        .build()?;

    let (alert_tx, _initial_rx) = broadcast::channel::<String>(ALERT_CHANNEL_CAPACITY);
    let forwarder = spawn_alert_forwarder(alerts_partition, alert_tx.clone());

    let state = web::Data::new(AppState {
        redis,
        xai,
        xai_url,
        alerts: alert_tx,
    });

    info!(%bind, "stream-api listening");
    let server = HttpServer::new(move || {
        App::new()
            .wrap(middleware::Logger::default())
            .wrap(Cors::permissive())
            .app_data(state.clone())
            .service(routes::healthz)
            .service(routes::list_decisions)
            .service(routes::get_decision)
            .service(routes::get_graph)
            .service(routes::get_explanation)
            .service(routes::get_stats)
            .route("/ws/alerts", web::get().to(ws::ws_alerts))
    })
    .bind(&bind)?
    .run();

    let result = server.await;
    forwarder.abort();
    result?;
    Ok(())
}

// ============================================================================
// Alert fan-out
// ============================================================================

/// Consume `upi.alerts` (from the last offset — only *live* alerts matter)
/// and broadcast `{"type":"alert","data":…}` frames to WebSocket sessions.
fn spawn_alert_forwarder(
    partition: Arc<PartitionClient>,
    tx: broadcast::Sender<String>,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        let mut next_offset: Option<i64> = None;
        let mut backoff_ms: u64 = 500;

        loop {
            let start = match next_offset {
                Some(o) => StartOffset::At(o),
                None => StartOffset::Latest,
            };
            let mut stream = StreamConsumerBuilder::new(Arc::clone(&partition), start)
                .with_max_wait_ms(250)
                .build();

            loop {
                match stream.next().await {
                    None => {
                        warn!("alert stream ended — rebuilding");
                        break;
                    }
                    Some(Err(e)) => {
                        error!(error = %e, "alert fetch error — rebuilding");
                        break;
                    }
                    Some(Ok((record, _watermark))) => {
                        next_offset = Some(record.offset + 1);
                        backoff_ms = 500;
                        match record
                            .record
                            .value
                            .as_deref()
                            .map(serde_json::from_slice::<serde_json::Value>)
                            .transpose()
                        {
                            Ok(Some(alert)) => {
                                let frame = json!({ "type": "alert", "data": alert }).to_string();
                                // Err = nobody listening right now; that's fine.
                                let _ = tx.send(frame);
                            }
                            Ok(None) => warn!("alert record without payload — skipped"),
                            Err(e) => warn!(error = %e, "unparseable alert — skipped"),
                        }
                    }
                }
            }

            tokio::time::sleep(Duration::from_millis(backoff_ms)).await;
            backoff_ms = (backoff_ms * 2).min(MAX_BACKOFF_MS);
        }
    })
}

// ============================================================================
// Dependency connections (retry with exponential backoff — never crash)
// ============================================================================

async fn try_redis(host: &str, port: u16) -> Result<ConnectionManager> {
    let client = redis::Client::open((host, port))?;
    let mut con = client.get_connection_manager().await?;
    let pong: String = redis::cmd("PING").query_async(&mut con).await?;
    debug_assert_eq!(pong, "PONG");
    Ok(con)
}

async fn connect_redis(host: &str, port: u16) -> ConnectionManager {
    let mut backoff_ms = 500;
    loop {
        match try_redis(host, port).await {
            Ok(con) => {
                info!(%host, port, "connected to Dragonfly");
                return con;
            }
            Err(e) => {
                warn!(error = %format!("{e:#}"), backoff_ms, "waiting for Dragonfly");
                tokio::time::sleep(Duration::from_millis(backoff_ms)).await;
                backoff_ms = (backoff_ms * 2).min(MAX_BACKOFF_MS);
            }
        }
    }
}

async fn connect_kafka(brokers: &[String]) -> Arc<Client> {
    let mut backoff_ms = 500;
    loop {
        match ClientBuilder::new(brokers.to_vec()).build().await {
            Ok(client) => {
                info!("connected to Redpanda");
                return Arc::new(client);
            }
            Err(e) => {
                warn!(error = %e, backoff_ms, "waiting for Redpanda");
                tokio::time::sleep(Duration::from_millis(backoff_ms)).await;
                backoff_ms = (backoff_ms * 2).min(MAX_BACKOFF_MS);
            }
        }
    }
}

async fn ensure_topics(client: &Client, topics: &[&str]) {
    let controller = match client.controller_client() {
        Ok(c) => c,
        Err(e) => {
            error!(error = %e, "controller client unavailable");
            return;
        }
    };
    for topic in topics {
        let mut backoff_ms = 500;
        loop {
            match controller.create_topic(*topic, 1, 1, 5_000).await {
                Ok(()) => {
                    info!(%topic, "created topic");
                    break;
                }
                Err(KafkaError::ServerError {
                    protocol_error: ProtocolError::TopicAlreadyExists,
                    ..
                }) => break,
                Err(e) => {
                    warn!(%topic, error = %e, backoff_ms, "waiting to create topic");
                    tokio::time::sleep(Duration::from_millis(backoff_ms)).await;
                    backoff_ms = (backoff_ms * 2).min(MAX_BACKOFF_MS);
                }
            }
        }
    }
}

async fn connect_partition(client: &Client, topic: &str) -> Arc<PartitionClient> {
    let mut backoff_ms = 500;
    loop {
        match client
            .partition_client(topic, 0, UnknownTopicHandling::Retry)
            .await
        {
            Ok(p) => return Arc::new(p),
            Err(e) => {
                warn!(%topic, error = %e, backoff_ms, "waiting for partition client");
                tokio::time::sleep(Duration::from_millis(backoff_ms)).await;
                backoff_ms = (backoff_ms * 2).min(MAX_BACKOFF_MS);
            }
        }
    }
}

// ============================================================================
// Misc helpers
// ============================================================================

fn init_tracing() {
    let filter = std::env::var("RUST_LOG").unwrap_or_else(|_| "info".to_string());
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::new(filter))
        .init();
}

fn env_var(name: &str, default: &str) -> String {
    std::env::var(name).unwrap_or_else(|_| default.to_string())
}

fn env_port(name: &str, default: u16) -> u16 {
    std::env::var(name)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
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
