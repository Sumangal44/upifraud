//! Velocity / device / geo features backed by Dragonfly (Redis).
//!
//! All counters for one transaction are updated in a **single pipeline**
//! (one network round-trip); the INCR results are read post-increment, i.e.
//! they already include the current transaction.
//!
//! Key scheme (shared contract with the Python tools — do not rename):
//! - `vel:cnt:1h:{vpa}` / `vel:amt:1h:{vpa}`      TTL 3600
//! - `vel:cnt:24h:{vpa}` / `vel:amt:24h:{vpa}`    TTL 86400
//! - `vel:dst:1h:{vpa}` / `vel:dstsnd:1h:{vpa}`   TTL 3600
//! - `vel:dev:1h:{device_hash}` / `vel:ip:1h:{ip}` TTL 3600
//! - `dev:first:{device_hash}`                     SET-NX, no TTL
//! - `geo:last:{vpa}`                              LIST [lat, lon, ts_ms]
//! - `hist:amt:{vpa}`                              LIST last 100 amounts

use redis::aio::ConnectionManager;
use tracing::warn;
use upifraud_core::{Geo, TransactionEvent, VelocityFeatures};

const HOUR_SECS: i64 = 3_600;
const DAY_SECS: i64 = 86_400;
const HIST_MAX: isize = 100;

/// Per-transaction outcome of the velocity stage.
#[derive(Debug, Clone, PartialEq)]
pub struct VelocityOutcome {
    pub velocity: VelocityFeatures,
    pub amount_zscore: f64,
    pub geo_jump_kmh: f64,
    pub device_age_hours: f64,
}

/// The non-ignored replies of the velocity pipeline, in insertion order.
type PipeOut = (
    i64,         // 0. INCR vel:cnt:1h:{src}       → src_count_1h (post-incr)
    i64,         // 1. INCR vel:cnt:24h:{src}      → src_count_24h
    f64,         // 2. INCRBYFLOAT vel:amt:1h      → src_amount_1h
    f64,         // 3. INCRBYFLOAT vel:amt:24h     → src_amount_24h
    i64,         // 4. INCR vel:dst:1h:{dst}       → dst_count_1h
    i64,         // 5. SCARD vel:dstsnd:1h:{dst}   → dst_distinct_senders_1h
    i64,         // 6. INCR vel:dev:1h:{hash}      → device_count_1h
    i64,         // 7. INCR vel:ip:1h:{ip}         → ip_count_1h
    Option<i64>, // 8. GET dev:first:{hash}        → device first-seen (unix s)
    Vec<f64>,    // 9. LRANGE hist:amt:{src}       → amount history
    Vec<f64>,    // 10. LRANGE geo:last:{src}      → previous position
);

/// Run the velocity pipeline (with short retries) and derive the scalar features.
pub async fn compute(
    con: &ConnectionManager,
    ev: &TransactionEvent,
    now_ms: i64,
) -> anyhow::Result<VelocityOutcome> {
    let src = &ev.src_vpa;
    let dst = &ev.dst_vpa;
    let dev = &ev.device.device_hash;
    let ip = &ev.src_ip;
    let amount = ev.amount_inr;
    let now_secs = now_ms / 1_000;

    let cnt_1h = format!("vel:cnt:1h:{src}");
    let cnt_24h = format!("vel:cnt:24h:{src}");
    let amt_1h = format!("vel:amt:1h:{src}");
    let amt_24h = format!("vel:amt:24h:{src}");
    let dst_cnt = format!("vel:dst:1h:{dst}");
    let dst_snd = format!("vel:dstsnd:1h:{dst}");
    let dev_cnt = format!("vel:dev:1h:{dev}");
    let ip_cnt = format!("vel:ip:1h:{ip}");
    let dev_first = format!("dev:first:{dev}");
    let hist = format!("hist:amt:{src}");
    let geo_last = format!("geo:last:{src}");

    let mut pipe = redis::pipe();
    pipe.cmd("INCR").arg(&cnt_1h);
    pipe.cmd("INCR").arg(&cnt_24h);
    pipe.cmd("INCRBYFLOAT").arg(&amt_1h).arg(amount);
    pipe.cmd("INCRBYFLOAT").arg(&amt_24h).arg(amount);
    pipe.cmd("EXPIRE").arg(&cnt_1h).arg(HOUR_SECS).ignore();
    pipe.cmd("EXPIRE").arg(&amt_1h).arg(HOUR_SECS).ignore();
    pipe.cmd("EXPIRE").arg(&cnt_24h).arg(DAY_SECS).ignore();
    pipe.cmd("EXPIRE").arg(&amt_24h).arg(DAY_SECS).ignore();
    pipe.cmd("INCR").arg(&dst_cnt);
    pipe.cmd("SADD").arg(&dst_snd).arg(src).ignore();
    pipe.cmd("SCARD").arg(&dst_snd);
    pipe.cmd("EXPIRE").arg(&dst_snd).arg(HOUR_SECS).ignore();
    pipe.cmd("INCR").arg(&dev_cnt);
    pipe.cmd("EXPIRE").arg(&dev_cnt).arg(HOUR_SECS).ignore();
    pipe.cmd("INCR").arg(&ip_cnt);
    pipe.cmd("EXPIRE").arg(&ip_cnt).arg(HOUR_SECS).ignore();
    pipe.cmd("SETNX").arg(&dev_first).arg(now_secs).ignore();
    pipe.cmd("GET").arg(&dev_first);
    pipe.cmd("RPUSH").arg(&hist).arg(amount).ignore();
    pipe.cmd("LTRIM").arg(&hist).arg(-HIST_MAX).arg(-1).ignore();
    pipe.cmd("LRANGE").arg(&hist).arg(0).arg(-1);
    // Read the previous geo position *before* overwriting it below.
    pipe.cmd("LRANGE").arg(&geo_last).arg(0).arg(-1);
    pipe.cmd("RPUSH")
        .arg(&geo_last)
        .arg(ev.geo.lat)
        .arg(ev.geo.lon)
        .arg(now_ms)
        .ignore();
    pipe.cmd("LTRIM").arg(&geo_last).arg(-3).arg(-1).ignore();

    // Short retry loop — ConnectionManager reconnects on its own, this covers
    // transient blips while Dragonfly restarts.
    let mut attempt = 0u32;
    let out: PipeOut = loop {
        attempt += 1;
        let mut con = con.clone();
        match pipe.query_async::<PipeOut>(&mut con).await {
            Ok(v) => break v,
            Err(e) if attempt < 3 => {
                warn!(error = %e, attempt, "velocity pipeline failed — retrying");
                tokio::time::sleep(std::time::Duration::from_millis(150)).await;
            }
            Err(e) => return Err(e.into()),
        }
    };

    let (
        cnt_1h,
        cnt_24h,
        amt_1h,
        amt_24h,
        dst_cnt,
        dst_snd,
        dev_cnt,
        ip_cnt,
        dev_first,
        hist,
        geo_prev,
    ) = out;

    Ok(VelocityOutcome {
        velocity: VelocityFeatures {
            src_count_1h: cnt_1h.max(0) as u32,
            src_amount_1h: amt_1h,
            src_count_24h: cnt_24h.max(0) as u32,
            src_amount_24h: amt_24h,
            dst_count_1h: dst_cnt.max(0) as u32,
            dst_distinct_senders_1h: dst_snd.max(0) as u32,
            device_count_1h: dev_cnt.max(0) as u32,
            ip_count_1h: ip_cnt.max(0) as u32,
        },
        amount_zscore: amount_zscore(amount, &hist),
        geo_jump_kmh: geo_jump_kmh(&ev.geo, &geo_prev, now_ms),
        device_age_hours: device_age_hours(dev_first, now_secs),
    })
}

/// z-score of `amount` against the sender's recent history.
/// `z = (amount - mean(hist)) / max(stdev(hist), 1.0)`, clamped to [-10, 10].
/// Empty history → 0.0.
pub fn amount_zscore(amount: f64, hist: &[f64]) -> f64 {
    if hist.is_empty() {
        return 0.0;
    }
    let n = hist.len() as f64;
    let mean = hist.iter().sum::<f64>() / n;
    if n < 2.0 {
        return (amount - mean).clamp(-10.0, 10.0);
    }
    // Sample standard deviation (n-1).
    let var = hist.iter().map(|v| (v - mean).powi(2)).sum::<f64>() / (n - 1.0);
    let stdev = var.sqrt();
    ((amount - mean) / stdev.max(1.0)).clamp(-10.0, 10.0)
}

/// Great-circle distance in kilometres.
pub fn haversine_km(lat1: f64, lon1: f64, lat2: f64, lon2: f64) -> f64 {
    const EARTH_R_KM: f64 = 6_371.0;
    let rad = std::f64::consts::PI / 180.0;
    let dlat = (lat2 - lat1) * rad;
    let dlon = (lon2 - lon1) * rad;
    let a = (dlat / 2.0).sin().powi(2)
        + (lat1 * rad).cos() * (lat2 * rad).cos() * (dlon / 2.0).sin().powi(2);
    let a = a.clamp(0.0, 1.0);
    let c = 2.0 * a.sqrt().atan2((1.0 - a).sqrt());
    EARTH_R_KM * c
}

/// Implied speed between the previous stored position (`geo:last`) and the
/// current event position, in km/h. No previous position (or no meaningful
/// time delta) → 0.0.
pub fn geo_jump_kmh(geo: &Geo, prev: &[f64], now_ms: i64) -> f64 {
    if prev.len() < 3 {
        return 0.0;
    }
    let (plat, plon, pts) = (prev[0], prev[1], prev[2] as i64);
    let dt_ms = now_ms - pts;
    if dt_ms <= 0 {
        return 0.0;
    }
    let km = haversine_km(plat, plon, geo.lat, geo.lon);
    km / (dt_ms as f64 / 3_600_000.0)
}

/// Hours since the device hash was first seen (0.0 if unknown).
pub fn device_age_hours(first_seen_secs: Option<i64>, now_secs: i64) -> f64 {
    match first_seen_secs {
        Some(t) if t > 0 && now_secs >= t => (now_secs - t) as f64 / 3_600.0,
        _ => 0.0,
    }
}

/// Deterministic placeholder for an IP-reputation feed: FNV-1a hash of the
/// address scaled to 0..1. Stands in until a real reputation lookup is wired in.
pub fn ip_risk_score(ip: &str) -> f64 {
    let mut hash: u64 = 0xcbf2_9ce4_8422_2325; // FNV-1a offset basis
    for b in ip.as_bytes() {
        hash ^= u64::from(*b);
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3); // FNV prime
    }
    (hash % 10_000) as f64 / 10_000.0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn zscore_empty_history_is_zero() {
        assert_eq!(amount_zscore(5_000.0, &[]), 0.0);
    }

    #[test]
    fn zscore_of_typical_value() {
        // mean 20, sample stdev 10 → (30 - 20) / 10 = 1.0
        assert!((amount_zscore(30.0, &[10.0, 20.0, 30.0]) - 1.0).abs() < 1e-9);
    }

    #[test]
    fn zscore_is_clamped_and_floor_denominator_applies() {
        // stdev 0 → denominator floored to 1.0 → 4900 → clamped to 10.
        assert_eq!(amount_zscore(5_000.0, &[100.0; 4]), 10.0);
        assert_eq!(amount_zscore(-100.0, &[0.0; 4]), -10.0);
    }

    #[test]
    fn zscore_of_mean_is_zero() {
        assert_eq!(amount_zscore(20.0, &[10.0, 20.0, 30.0]), 0.0);
    }

    #[test]
    fn haversine_zero_for_identical_points() {
        assert_eq!(haversine_km(12.97, 77.59, 12.97, 77.59), 0.0);
    }

    #[test]
    fn haversine_one_degree_longitude_on_equator() {
        // 1° of longitude at the equator ≈ 111.19 km (R = 6371 km).
        let d = haversine_km(0.0, 0.0, 0.0, 1.0);
        assert!((d - 111.19).abs() < 0.1, "got {d} km");
    }

    #[test]
    fn geo_jump_without_previous_position_is_zero() {
        let geo = Geo {
            lat: 12.97,
            lon: 77.59,
            city: None,
        };
        assert_eq!(geo_jump_kmh(&geo, &[], 1_700_000_000_000), 0.0);
        assert_eq!(geo_jump_kmh(&geo, &[12.97, 77.59], 1_700_000_000_000), 0.0);
    }

    #[test]
    fn geo_jump_computes_speed_over_one_hour() {
        let now = 1_700_000_000_000i64;
        let prev = vec![0.0, 0.0, (now - 3_600_000) as f64];
        let geo = Geo {
            lat: 0.0,
            lon: 1.0,
            city: None,
        };
        let speed = geo_jump_kmh(&geo, &prev, now);
        assert!((speed - 111.19).abs() < 1.0, "got {speed} km/h");
    }

    #[test]
    fn geo_jump_zero_when_time_does_not_advance() {
        let now = 1_700_000_000_000i64;
        let prev = vec![0.0, 0.0, now as f64];
        let geo = Geo {
            lat: 10.0,
            lon: 10.0,
            city: None,
        };
        assert_eq!(geo_jump_kmh(&geo, &prev, now), 0.0);
        // Timestamp in the future (clock skew) → no jump either.
        assert_eq!(geo_jump_kmh(&geo, &prev, now - 1_000), 0.0);
    }

    #[test]
    fn device_age_handles_missing_and_present() {
        assert_eq!(device_age_hours(None, 1_000_000), 0.0);
        assert_eq!(device_age_hours(Some(0), 1_000_000), 0.0);
        // 2 hours ago → 2.0 h
        assert_eq!(device_age_hours(Some(992_800), 1_000_000), 2.0);
        // first-seen in the future → 0.0
        assert_eq!(device_age_hours(Some(2_000_000), 1_000_000), 0.0);
    }

    #[test]
    fn ip_risk_is_deterministic_and_bounded() {
        for ip in ["10.0.0.1", "203.0.113.7", "2001:db8::1"] {
            let a = ip_risk_score(ip);
            assert!((0.0..1.0).contains(&a), "out of range for {ip}: {a}");
            assert_eq!(a, ip_risk_score(ip), "must be deterministic");
        }
        assert_ne!(ip_risk_score("10.0.0.1"), ip_risk_score("10.0.0.2"));
    }
}
