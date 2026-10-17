//! WebSocket endpoint streaming live alerts to the SOC portal.
//!
//! On connect the client receives `{"type":"hello"}`; every alert consumed
//! from `upi.alerts` is broadcast to **all** connected sessions as
//! `{"type":"alert","data":{…}}`. A 30 s ping heartbeat detects dead sessions
//! (they are evicted when the send fails).

use std::time::Duration;

use actix_web::{web, HttpRequest, HttpResponse};
use futures::StreamExt;
use tracing::{debug, warn};

use crate::routes::AppState;

/// `WS /ws/alerts`
pub async fn ws_alerts(
    req: HttpRequest,
    body: web::Payload,
    state: web::Data<AppState>,
) -> Result<HttpResponse, actix_web::Error> {
    let (response, mut session, mut messages) = actix_ws::handle(&req, body)?;

    // Per-connection subscription to the shared broadcast channel.
    let mut alerts_rx = state.alerts.subscribe();

    if session.text(r#"{"type":"hello"}"#).await.is_err() {
        return Ok(response);
    }

    actix_web::rt::spawn(async move {
        let mut heartbeat = tokio::time::interval(Duration::from_secs(30));
        heartbeat.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        heartbeat.tick().await; // consume the immediate first tick

        loop {
            tokio::select! {
                msg = messages.next() => match msg {
                    None => break, // client stream ended
                    Some(Ok(actix_ws::Message::Ping(bytes))) => {
                        if session.pong(&bytes).await.is_err() {
                            break;
                        }
                    }
                    Some(Ok(actix_ws::Message::Close(_))) => break,
                    Some(Ok(_)) => {}
                    Some(Err(e)) => {
                        debug!(error = %e, "websocket protocol error");
                        break;
                    }
                },
                frame = alerts_rx.recv() => match frame {
                    Ok(json) => {
                        if session.text(json).await.is_err() {
                            break; // session closed → evict
                        }
                    }
                    Err(tokio::sync::broadcast::error::RecvError::Lagged(n)) => {
                        warn!(skipped = n, "alert subscriber lagged — frames dropped");
                    }
                    Err(tokio::sync::broadcast::error::RecvError::Closed) => break,
                },
                _ = heartbeat.tick() => {
                    if session.ping(b"").await.is_err() {
                        break;
                    }
                }
            }
        }

        let _ = session.close(None).await;
        debug!("websocket session closed");
    });

    Ok(response)
}
