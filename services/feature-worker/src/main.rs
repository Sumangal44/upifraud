//! Feature assembly + scoring worker.
//!
//! Consumes `upi.tx.raw` (consumer group `feature-worker`), computes velocity
//! features (Dragonfly) and a 2-hop subgraph (Memgraph), calls the inference
//! service, applies the risk policy, and emits decisions (`upi.decisions`) and
//! alerts (`upi.alerts`) plus the Redis projections the portal reads.
//!
//! All startup connections retry with exponential backoff instead of crashing;
//! the consumer loop shuts down gracefully on ctrl-c.

mod pipeline;
mod reasons;
mod scoring;
mod store;
mod subgraph;
mod velocity;

use std::sync::Arc;
use std::time::Duration;

use anyhow::Result;
use futures::StreamExt;
use neo4rs::{query, Graph};
use redis::aio::ConnectionManager;
use rskafka::client::consumer::{StartOffset, StreamConsumerBuilder};
use rskafka::client::error::{Error as KafkaError, ProtocolError};
use rskafka::client::partition::{PartitionClient, UnknownTopicHandling};
use rskafka::client::{Client, ClientBuilder};
use tracing::{error, info, warn};
use tracing_subscriber::EnvFilter;
use upifraud_core::policy::PolicyConfig;
use upifraud_core::topics::GROUP_FEATURE_WORKER;
use upifraud_core::{TransactionEvent, TOPIC_ALERTS, TOPIC_DECISIONS, TOPIC_TX_RAW};

use pipeline::Worker;

const MAX_BACKOFF_MS: u64 = 10_000;

#[tokio::main]
async fn main() -> Result<()> {
    dotenvy::dotenv().ok();
    init_tracing();

    let brokers = env_brokers();
    let topic_tx = env_var("KAFKA_TOPIC_TX_RAW", TOPIC_TX_RAW);
    let topic_dec = env_var("KAFKA_TOPIC_DECISIONS", TOPIC_DECISIONS);
    let topic_alerts = env_var("KAFKA_TOPIC_ALERTS", TOPIC_ALERTS);
    let redis_host = env_var("DRAGONFLY_HOST", "localhost");
    let redis_port = env_port("DRAGONFLY_PORT", 6_379);
    let memgraph_host = env_var("MEMGRAPH_HOST", "localhost");
    let memgraph_port = env_port("MEMGRAPH_PORT", 7_687);
    let inference_url = env_var("INFERENCE_URL", "http://localhost:8001");
    let infer_timeout_ms: u64 = env_var("INFER_TIMEOUT_MS", "250").parse().unwrap_or(250);
    let policy = PolicyConfig::from_env();

    info!(
        brokers = %brokers.join(","),
        %topic_tx,
        %topic_dec,
        %topic_alerts,
        %inference_url,
        infer_timeout_ms,
        group = %GROUP_FEATURE_WORKER,
        "feature-worker starting"
    );

    // --- startup: wait for dependencies instead of crashing ---------------
    let client = connect_kafka(&brokers).await;
    ensure_topics(&client, &[&topic_tx, &topic_dec, &topic_alerts]).await;
    let tx_partition = connect_partition(&client, &topic_tx).await;
    let decisions_partition = connect_partition(&client, &topic_dec).await;
    let alerts_partition = connect_partition(&client, &topic_alerts).await;
    let redis = connect_redis(&redis_host, redis_port).await;
    let graph = connect_memgraph(&memgraph_host, memgraph_port).await;
    let http = scoring::client_with_timeout(infer_timeout_ms)?;

    let worker = Arc::new(Worker {
        redis,
        graph,
        http,
        inference_url,
        decisions: decisions_partition,
        alerts: alerts_partition,
        policy,
    });

    info!(group = %GROUP_FEATURE_WORKER, start = "earliest", "consuming raw transactions (ctrl-c to stop)");
    consume_loop(worker, tx_partition).await
}

// ============================================================================
// Consumer loop
// ============================================================================

async fn consume_loop(worker: Arc<Worker>, partition: Arc<PartitionClient>) -> Result<()> {
    // Resume point after a stream failure (rskafka has no consumer groups, so
    // we track the next offset ourselves — at-least-once across rebuilds).
    let mut next_offset: Option<i64> = None;
    let mut backoff_ms: u64 = 500;

    loop {
        let start = match next_offset {
            Some(o) => StartOffset::At(o),
            None => StartOffset::Earliest,
        };
        let mut stream = StreamConsumerBuilder::new(Arc::clone(&partition), start)
            .with_max_wait_ms(250)
            .build();

        'stream: loop {
            tokio::select! {
                _ = tokio::signal::ctrl_c() => {
                    info!("shutdown signal received — consumer stopped (all publishes are awaited, nothing buffered)");
                    return Ok(());
                }
                item = stream.next() => {
                    match item {
                        None => {
                            warn!("kafka stream ended — rebuilding consumer");
                            break 'stream;
                        }
                        Some(Err(e)) => {
                            error!(error = %e, "kafka fetch error — rebuilding consumer");
                            break 'stream;
                        }
                        Some(Ok((record, _watermark))) => {
                            next_offset = Some(record.offset + 1);
                            backoff_ms = 500;
                            let decode_start = std::time::Instant::now();
                            let parsed = record
                                .record
                                .value
                                .as_deref()
                                .map(serde_json::from_slice::<TransactionEvent>)
                                .transpose();
                            let ingest_ms = decode_start.elapsed().as_millis() as u64;
                            match parsed {
                                Ok(Some(ev)) => handle_tx(&worker, ev, ingest_ms).await,
                                Ok(None) => warn!(offset = record.offset, "record without payload — skipped"),
                                Err(e) => warn!(offset = record.offset, error = %e, "unparseable record — skipped"),
                            }
                        }
                    }
                }
            }
        }

        tokio::time::sleep(Duration::from_millis(backoff_ms)).await;
        backoff_ms = (backoff_ms * 2).min(MAX_BACKOFF_MS);
    }
}

/// Process, publish and persist one transaction; log one structured line.
async fn handle_tx(worker: &Worker, ev: TransactionEvent, ingest_ms: u64) {
    let tx_id = ev.tx_id.clone();
    let processed = match worker.process(&ev, ingest_ms).await {
        Ok(p) => p,
        Err(e) => {
            error!(tx_id = %tx_id, error = %format!("{e:#}"), "pipeline failed — transaction skipped");
            return;
        }
    };

    let decision_json = match serde_json::to_string(&processed.decision) {
        Ok(j) => j,
        Err(e) => {
            error!(tx_id = %tx_id, error = %e, "decision serialisation failed");
            return;
        }
    };

    if let Err(e) =
        pipeline::publish(worker.decisions.as_ref(), &tx_id, decision_json.as_bytes()).await
    {
        error!(tx_id = %tx_id, error = %format!("{e:#}"), "failed to publish decision");
    }
    if let Some(alert) = &processed.alert {
        match serde_json::to_string(alert) {
            Ok(alert_json) => {
                if let Err(e) =
                    pipeline::publish(worker.alerts.as_ref(), &tx_id, alert_json.as_bytes()).await
                {
                    error!(tx_id = %tx_id, error = %format!("{e:#}"), "failed to publish alert");
                }
            }
            Err(e) => error!(tx_id = %tx_id, error = %e, "alert serialisation failed"),
        }
    }

    if let Err(e) = store::persist(&worker.redis, &processed.decision, &processed.features).await {
        warn!(tx_id = %tx_id, error = %format!("{e:#}"), "failed to persist decision to redis");
    }

    info!(
        tx_id = %processed.decision.tx_id,
        score = processed.decision.score,
        action = processed.action_str,
        reasons = ?processed.decision.reasons,
        total_ms = processed.decision.latency.total_ms,
        feature_ms = processed.decision.latency.feature_ms,
        inference_ms = processed.decision.latency.inference_ms,
        "decision"
    );
}

// ============================================================================
// Dependency connections (retry with exponential backoff — never crash)
// ============================================================================

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
            // controller_client is effectively infallible, but retry anyway.
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

async fn try_memgraph(host: &str, port: u16) -> Result<Graph> {
    let graph = Graph::new(format!("{host}:{port}"), "any", "pass").await?;
    let mut stream = graph.execute(query("RETURN 1 AS ok")).await?;
    let _row = stream.next().await?;
    Ok(graph)
}

async fn connect_memgraph(host: &str, port: u16) -> Graph {
    let mut backoff_ms = 500;
    loop {
        match try_memgraph(host, port).await {
            Ok(g) => {
                info!(%host, port, "connected to Memgraph");
                return g;
            }
            Err(e) => {
                warn!(error = %format!("{e:#}"), backoff_ms, "waiting for Memgraph");
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
