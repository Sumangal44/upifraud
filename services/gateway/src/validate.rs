//! Request normalisation + validation for the ingestion gateway.
//!
//! The shared wire type `TransactionEvent` lives in `upifraud-core`; the only
//! normalisation performed here is generating a `tx_id` when the client did not
//! supply one (the wire type requires the field, so it is injected on the raw
//! JSON before deserialisation).

use serde_json::Value;
use upifraud_core::TransactionEvent;
use uuid::Uuid;

/// Maximum accepted clock skew into the past (replay window): 24 hours.
pub const MAX_PAST_SKEW_MS: i64 = 24 * 60 * 60 * 1_000;
/// Maximum accepted clock skew into the future: 5 minutes.
pub const MAX_FUTURE_SKEW_MS: i64 = 5 * 60 * 1_000;

/// Ensure a `tx_id` is present (UUID v4 is generated when missing/empty) and
/// deserialise into the shared wire contract.
///
/// Returns a client-safe error message on malformed payloads.
pub fn normalize_event(mut value: Value) -> Result<TransactionEvent, String> {
    if !value.is_object() {
        return Err("body must be a JSON object".to_string());
    }
    let needs_id = match value.get("tx_id") {
        None | Some(Value::Null) => true,
        Some(Value::String(s)) => s.trim().is_empty(),
        // Wrong type — leave it in place so serde reports a precise error.
        Some(_) => false,
    };
    if needs_id {
        value["tx_id"] = Value::String(Uuid::new_v4().to_string());
    }
    serde_json::from_value::<TransactionEvent>(value).map_err(|e| e.to_string())
}

/// Validate a decoded event against the gateway contract.
///
/// Checks: positive finite amount, non-empty/different VPAs, non-empty device
/// hash and source IP, and event timestamp within `[-24h, +5min]` of `now_ms`.
/// Returns `Err` with a client-safe reason on the first violation.
pub fn validate_event(ev: &TransactionEvent, now_ms: i64) -> Result<(), String> {
    if !ev.amount_inr.is_finite() || ev.amount_inr <= 0.0 {
        return Err("amount_inr must be a finite number greater than 0".to_string());
    }
    if ev.src_vpa.trim().is_empty() {
        return Err("src_vpa must not be empty".to_string());
    }
    if ev.dst_vpa.trim().is_empty() {
        return Err("dst_vpa must not be empty".to_string());
    }
    if ev.src_vpa == ev.dst_vpa {
        return Err("src_vpa and dst_vpa must be different".to_string());
    }
    if ev.device.device_hash.trim().is_empty() {
        return Err("device.device_hash must not be empty".to_string());
    }
    if ev.src_ip.trim().is_empty() {
        return Err("src_ip must not be empty".to_string());
    }
    let min = now_ms - MAX_PAST_SKEW_MS;
    let max = now_ms + MAX_FUTURE_SKEW_MS;
    if ev.timestamp_ms < min || ev.timestamp_ms > max {
        return Err(format!(
            "timestamp_ms must be between {min} and {max} (unix ms)"
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    const NOW: i64 = 1_700_000_000_000; // fixed reference clock

    /// A fully valid event as JSON (no `tx_id` — normalisation must add one).
    fn base_event() -> Value {
        serde_json::json!({
            "timestamp_ms": NOW,
            "src_vpa": "alice@okaxis",
            "dst_vpa": "merchant@paytm",
            "amount_inr": 150.5,
            "device": { "device_hash": "dev-abc123", "app_version": "2.4.1" },
            "src_ip": "10.0.0.9",
            "geo": { "lat": 12.97, "lon": 77.59, "city": "Bengaluru" }
        })
    }

    fn parsed(mut patch: impl FnMut(&mut Value)) -> TransactionEvent {
        let mut v = base_event();
        patch(&mut v);
        normalize_event(v).expect("event should parse")
    }

    #[test]
    fn valid_event_passes() {
        let ev = normalize_event(base_event()).unwrap();
        assert!(validate_event(&ev, NOW).is_ok());
    }

    #[test]
    fn missing_tx_id_is_generated() {
        let ev = normalize_event(base_event()).unwrap();
        Uuid::parse_str(&ev.tx_id).expect("generated tx_id must be a UUID");
    }

    #[test]
    fn empty_tx_id_is_regenerated() {
        let mut v = base_event();
        v["tx_id"] = Value::String("   ".to_string());
        let ev = normalize_event(v).unwrap();
        Uuid::parse_str(&ev.tx_id).unwrap();
    }

    #[test]
    fn existing_tx_id_is_preserved() {
        let mut v = base_event();
        v["tx_id"] = Value::String("tx-fixed-1".to_string());
        let ev = normalize_event(v).unwrap();
        assert_eq!(ev.tx_id, "tx-fixed-1");
    }

    #[test]
    fn non_object_body_is_rejected() {
        assert!(normalize_event(serde_json::json!([1, 2, 3])).is_err());
        assert!(normalize_event(serde_json::json!("nope")).is_err());
    }

    #[test]
    fn missing_required_field_is_rejected() {
        let mut v = base_event();
        v.as_object_mut().unwrap().remove("amount_inr");
        assert!(normalize_event(v).is_err());
    }

    #[test]
    fn zero_and_negative_amounts_are_rejected() {
        for amount in [0.0, -42.0] {
            let ev = parsed(|v| v["amount_inr"] = serde_json::json!(amount));
            let err = validate_event(&ev, NOW).unwrap_err();
            assert!(err.contains("amount_inr"), "unexpected error: {err}");
        }
    }

    #[test]
    fn non_finite_amount_is_rejected() {
        let mut ev = parsed(|_| {});
        ev.amount_inr = f64::NAN;
        assert!(validate_event(&ev, NOW).is_err());
        ev.amount_inr = f64::INFINITY;
        assert!(validate_event(&ev, NOW).is_err());
    }

    #[test]
    fn same_src_and_dst_vpa_is_rejected() {
        let ev = parsed(|v| v["dst_vpa"] = serde_json::json!("alice@okaxis"));
        let err = validate_event(&ev, NOW).unwrap_err();
        assert!(err.contains("different"), "unexpected error: {err}");
    }

    #[test]
    fn empty_identity_fields_are_rejected() {
        let ev = parsed(|v| v["src_vpa"] = serde_json::json!(""));
        assert!(validate_event(&ev, NOW).unwrap_err().contains("src_vpa"));

        let ev = parsed(|v| v["dst_vpa"] = serde_json::json!("   "));
        assert!(validate_event(&ev, NOW).unwrap_err().contains("dst_vpa"));

        let ev = parsed(|v| {
            v["device"] = serde_json::json!({ "device_hash": "" });
        });
        assert!(validate_event(&ev, NOW)
            .unwrap_err()
            .contains("device.device_hash"));

        let ev = parsed(|v| v["src_ip"] = serde_json::json!(""));
        assert!(validate_event(&ev, NOW).unwrap_err().contains("src_ip"));
    }

    #[test]
    fn stale_timestamp_is_rejected() {
        let ev = parsed(|v| v["timestamp_ms"] = serde_json::json!(NOW - MAX_PAST_SKEW_MS - 1));
        assert!(validate_event(&ev, NOW)
            .unwrap_err()
            .contains("timestamp_ms"));
        // …but just inside the window is fine.
        let ev = parsed(|v| v["timestamp_ms"] = serde_json::json!(NOW - MAX_PAST_SKEW_MS));
        assert!(validate_event(&ev, NOW).is_ok());
    }

    #[test]
    fn future_timestamp_is_rejected() {
        let ev = parsed(|v| v["timestamp_ms"] = serde_json::json!(NOW + MAX_FUTURE_SKEW_MS + 1));
        assert!(validate_event(&ev, NOW)
            .unwrap_err()
            .contains("timestamp_ms"));
        let ev = parsed(|v| v["timestamp_ms"] = serde_json::json!(NOW + MAX_FUTURE_SKEW_MS));
        assert!(validate_event(&ev, NOW).is_ok());
    }
}
