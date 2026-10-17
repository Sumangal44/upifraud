//! Deterministic risk-reason derivation.
//!
//! Pure functions over the event + feature vector; reasons are emitted in a
//! fixed canonical order so identical inputs always produce identical output.

use upifraud_core::{FeatureVector, TransactionEvent};

/// Edge pairs that constitute a fast-pass must be this close together (ms).
const FAST_PASS_WINDOW_MS: i64 = 300_000; // 5 minutes
/// Minimum number of timestamped TRANSFERRED edges for a mule-path signal.
const MULE_MIN_TRANSFERRED: usize = 3;

/// Derive the human-readable reason codes attached to the decision.
///
/// Order is fixed (canonical), which keeps the output deterministic:
/// `HIGH_VELOCITY_1H`, `HIGH_AMOUNT_24H`, `NEW_DST_ACCOUNT`, `SHARED_DEVICE`,
/// `IP_RISK`, `GEO_JUMP`, `ROOTED_DEVICE`, `SCREEN_MIRROR`, `EMULATOR`,
/// `LARGE_AMOUNT`, `MULE_PATH_2HOP`.
pub fn derive_reasons(event: &TransactionEvent, features: &FeatureVector) -> Vec<String> {
    let v = &features.velocity;
    let mut reasons = Vec::new();

    if v.src_count_1h > 10 {
        reasons.push("HIGH_VELOCITY_1H".to_string());
    }
    if v.src_amount_24h > 100_000.0 {
        reasons.push("HIGH_AMOUNT_24H".to_string());
    }
    if features.dst_account_age_hours < 24.0 {
        reasons.push("NEW_DST_ACCOUNT".to_string());
    }
    if v.device_count_1h > 3 {
        reasons.push("SHARED_DEVICE".to_string());
    }
    if features.ip_risk_score > 0.7 {
        reasons.push("IP_RISK".to_string());
    }
    if features.geo_jump_kmh > 500.0 {
        reasons.push("GEO_JUMP".to_string());
    }
    if event.device.rooted {
        reasons.push("ROOTED_DEVICE".to_string());
    }
    if event.device.screen_mirror {
        reasons.push("SCREEN_MIRROR".to_string());
    }
    if event.device.emulator {
        reasons.push("EMULATOR".to_string());
    }
    if event.amount_inr > 50_000.0 {
        reasons.push("LARGE_AMOUNT".to_string());
    }
    if mule_path_2hop(features) {
        reasons.push("MULE_PATH_2HOP".to_string());
    }
    reasons
}

/// ≥3 timestamped `TRANSFERRED` edges in the subgraph, at least two of which
/// are within 5 minutes of each other (fast pass-through).
fn mule_path_2hop(features: &FeatureVector) -> bool {
    let mut timestamps: Vec<i64> = features
        .subgraph
        .edges
        .iter()
        .filter(|e| e.rel == "TRANSFERRED")
        .filter_map(|e| e.timestamp_ms)
        .collect();
    if timestamps.len() < MULE_MIN_TRANSFERRED {
        return false;
    }
    timestamps.sort_unstable();
    timestamps
        .windows(2)
        .any(|pair| (pair[1] - pair[0]).abs() <= FAST_PASS_WINDOW_MS)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use upifraud_core::{DeviceInfo, Geo, GraphEdge, Subgraph};

    fn event(json_patch: serde_json::Value) -> TransactionEvent {
        let mut v = json!({
            "tx_id": "tx-1",
            "timestamp_ms": 1_700_000_000_000i64,
            "src_vpa": "alice@okaxis",
            "dst_vpa": "bob@okicici",
            "amount_inr": 1000.0,
            "device": { "device_hash": "dev-1" },
            "src_ip": "10.0.0.1",
            "geo": { "lat": 12.97, "lon": 77.59 }
        });
        if let Some(obj) = v.as_object_mut() {
            if let Some(patch) = json_patch.as_object() {
                for (k, val) in patch {
                    obj.insert(k.clone(), val.clone());
                }
            }
        }
        serde_json::from_value(v).expect("valid event")
    }

    fn features() -> FeatureVector {
        FeatureVector {
            tx_id: "tx-1".to_string(),
            dst_account_age_hours: 100.0, // old account → no NEW_DST_ACCOUNT
            ip_risk_score: 0.1,           // clean IP
            geo_jump_kmh: 0.0,            // stationary
            ..FeatureVector::default()
        }
    }

    #[test]
    fn clean_transaction_has_no_reasons() {
        let ev = event(json!({}));
        let fv = features();
        assert!(derive_reasons(&ev, &fv).is_empty());
    }

    #[test]
    fn velocity_amount_and_device_thresholds() {
        let mut fv = features();
        fv.velocity.src_count_1h = 11;
        fv.velocity.src_amount_24h = 100_000.01;
        fv.velocity.device_count_1h = 4;
        let ev = event(json!({}));
        assert_eq!(
            derive_reasons(&ev, &fv),
            vec![
                "HIGH_VELOCITY_1H".to_string(),
                "HIGH_AMOUNT_24H".to_string(),
                "SHARED_DEVICE".to_string(),
            ]
        );
        // Exactly at threshold → still not flagged.
        let mut fv2 = features();
        fv2.velocity.src_count_1h = 10;
        fv2.velocity.src_amount_24h = 100_000.0;
        fv2.velocity.device_count_1h = 3;
        assert!(derive_reasons(&event(json!({})), &fv2).is_empty());
    }

    #[test]
    fn new_dst_ip_risk_and_geo_jump() {
        let mut fv = features();
        fv.dst_account_age_hours = 2.0;
        fv.ip_risk_score = 0.71;
        fv.geo_jump_kmh = 900.0;
        let ev = event(json!({}));
        assert_eq!(
            derive_reasons(&ev, &fv),
            vec![
                "NEW_DST_ACCOUNT".to_string(),
                "IP_RISK".to_string(),
                "GEO_JUMP".to_string(),
            ]
        );
    }

    #[test]
    fn device_flags_and_large_amount() {
        let mut ev = event(json!({ "amount_inr": 50_000.01 }));
        ev.device = DeviceInfo {
            device_hash: "dev-1".to_string(),
            app_version: None,
            rooted: true,
            screen_mirror: true,
            emulator: true,
        };
        let fv = features();
        assert_eq!(
            derive_reasons(&ev, &fv),
            vec![
                "ROOTED_DEVICE".to_string(),
                "SCREEN_MIRROR".to_string(),
                "EMULATOR".to_string(),
                "LARGE_AMOUNT".to_string(),
            ]
        );
        // Exactly 50000 is not "large".
        let mut ev2 = event(json!({ "amount_inr": 50_000.0 }));
        ev2.device.rooted = false;
        assert!(derive_reasons(&ev2, &features()).is_empty());
    }

    fn transferred(ts: i64) -> GraphEdge {
        GraphEdge {
            src: format!("Account:a{ts}"),
            dst: format!("Account:b{ts}"),
            rel: "TRANSFERRED".to_string(),
            amount: Some(100.0),
            timestamp_ms: Some(ts),
        }
    }

    #[test]
    fn mule_path_requires_three_edges_with_a_fast_pair() {
        let mut fv = features();

        // Two edges only → never a mule path.
        fv.subgraph = Subgraph {
            nodes: vec![],
            edges: vec![transferred(0), transferred(10_000)],
        };
        assert_eq!(derive_reasons(&event(json!({})), &fv), Vec::<String>::new());

        // Three edges but all far apart → no.
        fv.subgraph.edges = vec![transferred(0), transferred(400_000), transferred(800_000)];
        assert_eq!(derive_reasons(&event(json!({})), &fv), Vec::<String>::new());

        // Three edges with a pair inside 300 s → yes.
        fv.subgraph.edges = vec![transferred(0), transferred(400_000), transferred(120_000)];
        assert_eq!(
            derive_reasons(&event(json!({})), &fv),
            vec!["MULE_PATH_2HOP".to_string()]
        );
    }

    #[test]
    fn non_transfer_edges_do_not_count_towards_mule_path() {
        let mut fv = features();
        fv.subgraph = Subgraph {
            nodes: vec![],
            edges: vec![GraphEdge {
                src: "Account:a".to_string(),
                dst: "Device:d".to_string(),
                rel: "USED_DEVICE".to_string(),
                amount: None,
                timestamp_ms: Some(0),
            }],
        };
        let mut unused = transferred(0);
        unused.timestamp_ms = Some(1_000);
        fv.subgraph.edges.push(unused);
        fv.subgraph.edges.push(transferred(2_000));
        // Only 2 timestamped TRANSFERRED edges → no.
        assert_eq!(derive_reasons(&event(json!({})), &fv), Vec::<String>::new());
    }

    #[test]
    fn reason_order_is_deterministic() {
        let mut ev = event(json!({ "amount_inr": 60_000.0 }));
        ev.device.rooted = true;
        let mut fv = features();
        fv.velocity.src_count_1h = 50;
        fv.dst_account_age_hours = 1.0;
        fv.ip_risk_score = 0.99;
        let a = derive_reasons(&ev, &fv);
        let b = derive_reasons(&ev, &fv);
        assert_eq!(a, b);
        assert_eq!(
            a,
            vec![
                "HIGH_VELOCITY_1H".to_string(),
                "NEW_DST_ACCOUNT".to_string(),
                "IP_RISK".to_string(),
                "ROOTED_DEVICE".to_string(),
                "LARGE_AMOUNT".to_string(),
            ]
        );
    }

    #[test]
    fn geo_defaults_do_not_spurious_flag() {
        // Sanity: the default Geo/DeviceInfo used above really is benign.
        let ev = event(json!({}));
        assert_eq!(
            ev.geo,
            Geo {
                lat: 12.97,
                lon: 77.59,
                city: None
            }
        );
    }
}
