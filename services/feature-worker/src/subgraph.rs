//! 2-hop heterogeneous neighbourhood extraction from Memgraph (Bolt, neo4rs).
//!
//! Two parameterised Cypher queries are seeded from **both** transaction
//! parties, then mapped onto the shared `Subgraph` wire type:
//! node id = `{primary_label}:{key}` (e.g. `Account:alice@okaxis`).
//!
//! Only display-safe properties (`created_at_ms`, `risk_flags`, `rooted`,
//! `risk_score`) are copied into the wire type — never notes/PII.

use std::collections::BTreeMap;

use neo4rs::{query, Graph, Node};
use tracing::warn;
use upifraud_core::{GraphEdge, GraphNode, Subgraph};

pub const NODE_QUERY: &str =
    "UNWIND $seeds AS v MATCH (a:Account {vpa_id:v})-[*1..2]-(n) RETURN DISTINCT n LIMIT 100";
pub const EDGE_QUERY: &str =
    "UNWIND $seeds AS v MATCH (a:Account {vpa_id:v})-[*1..2]-(n1)-[r]-(n2) \
     RETURN n1, type(r) AS rel, properties(r) AS props, n2 LIMIT 300";
pub const DST_AGE_QUERY: &str =
    "MATCH (a:Account {vpa_id: $dst}) RETURN a.created_at_ms AS created_at_ms LIMIT 1";

/// Property keys safe to expose to the SOC UI (no PII / free-text notes).
const SAFE_PROPS: [&str; 4] = ["created_at_ms", "risk_flags", "rooted", "risk_score"];

/// Label → key property, in id-priority order.
const LABEL_KEY_PROPS: [(&str, &str); 4] = [
    ("Account", "vpa_id"),
    ("Device", "device_hash"),
    ("IPAddress", "ip_address"),
    ("Merchant", "merchant_id"),
];

/// A graph node flattened into plain values (so the mapping logic is pure and
/// unit-testable without a live database).
#[derive(Debug, Clone, PartialEq)]
pub struct RawNode {
    pub labels: Vec<String>,
    /// Only the identity key properties (`vpa_id`, `device_hash`, …).
    pub id_props: BTreeMap<String, serde_json::Value>,
    /// Display-safe properties only (see [`SAFE_PROPS`]).
    pub props: BTreeMap<String, serde_json::Value>,
    /// Database-internal id, used only when no known key property exists.
    pub fallback_key: String,
}

/// Result of the extraction stage.
#[derive(Debug, Clone, PartialEq)]
pub struct Extracted {
    pub subgraph: Subgraph,
    /// Hours since the destination account appeared in the graph (0.0 if absent).
    pub dst_account_age_hours: f64,
}

fn primary_label(labels: &[String]) -> Option<&'static str> {
    LABEL_KEY_PROPS
        .iter()
        .map(|(label, _)| *label)
        .find(|label| labels.iter().any(|l| l == label))
}

/// Pick the primary label (priority: Account, Device, IPAddress, Merchant) and
/// the key for the synthetic id `{label}:{key}`.
fn label_and_key(raw: &RawNode) -> (String, String) {
    let label = primary_label(&raw.labels)
        .map(str::to_string)
        .or_else(|| raw.labels.first().cloned())
        .unwrap_or_else(|| "Unknown".to_string());

    let preferred = LABEL_KEY_PROPS
        .iter()
        .find(|(l, _)| *l == label)
        .map(|(_, kp)| *kp);
    let key = preferred
        .and_then(|kp| raw.id_props.get(kp))
        .or_else(|| {
            LABEL_KEY_PROPS
                .iter()
                .find_map(|(_, kp)| raw.id_props.get(*kp))
        })
        .and_then(|v| v.as_str())
        .map(str::to_string)
        .unwrap_or_else(|| raw.fallback_key.clone());
    (label, key)
}

/// Synthetic node id, e.g. `Account:user01@upidemo`.
pub fn node_id(raw: &RawNode) -> String {
    let (label, key) = label_and_key(raw);
    format!("{label}:{key}")
}

/// Map a [`RawNode`] onto the wire type.
pub fn to_graph_node(raw: &RawNode) -> GraphNode {
    let (label, key) = label_and_key(raw);
    GraphNode {
        id: format!("{label}:{key}"),
        label,
        props: raw.props.clone(),
    }
}

/// Drop everything that is not display-safe.
pub fn display_props(
    all: &BTreeMap<String, serde_json::Value>,
) -> BTreeMap<String, serde_json::Value> {
    all.iter()
        .filter(|(k, _)| SAFE_PROPS.contains(&k.as_str()))
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect()
}

/// Flatten a neo4rs `Node` into a [`RawNode`].
fn raw_from_node(n: &Node) -> RawNode {
    let labels: Vec<String> = n.labels().into_iter().map(str::to_string).collect();
    let mut all = BTreeMap::new();
    for key in n.keys() {
        if let Ok(v) = n.get::<serde_json::Value>(key) {
            if !v.is_null() {
                all.insert(key.to_string(), v);
            }
        }
    }
    let id_props = all
        .iter()
        .filter(|(k, _)| LABEL_KEY_PROPS.iter().any(|(_, kp)| kp == k))
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();
    RawNode {
        labels,
        id_props,
        props: display_props(&all),
        fallback_key: n.id().to_string(),
    }
}

/// Run both neighbourhood queries (best-effort) plus the destination-account
/// age lookup. Individual query failures degrade to an empty/partial subgraph
/// instead of failing the whole transaction.
pub async fn extract(graph: &Graph, src: &str, dst: &str, now_ms: i64) -> Extracted {
    let seeds = vec![src.to_owned(), dst.to_owned()];
    let mut nodes: BTreeMap<String, GraphNode> = BTreeMap::new();
    let mut edges: BTreeMap<(String, String, String, Option<u64>, Option<i64>), GraphEdge> =
        BTreeMap::new();

    // --- nodes -------------------------------------------------------------
    match graph
        .execute(query(NODE_QUERY).param("seeds", seeds.clone()))
        .await
    {
        Ok(mut stream) => loop {
            match stream.next().await {
                Ok(Some(row)) => match row.get::<Node>("n") {
                    Ok(node) => {
                        let raw = raw_from_node(&node);
                        let id = node_id(&raw);
                        nodes.insert(id, to_graph_node(&raw));
                    }
                    Err(e) => warn!(error = %e, "skipping unmappable node row"),
                },
                Ok(None) => break,
                Err(e) => {
                    warn!(error = %e, "node query failed mid-stream");
                    break;
                }
            }
        },
        Err(e) => warn!(error = %e, "node query failed — using empty subgraph"),
    }

    // --- edges (also unions their endpoint nodes into the node set) --------
    match graph
        .execute(query(EDGE_QUERY).param("seeds", seeds.clone()))
        .await
    {
        Ok(mut stream) => loop {
            let row = match stream.next().await {
                Ok(Some(row)) => row,
                Ok(None) => break,
                Err(e) => {
                    warn!(error = %e, "edge query failed mid-stream");
                    break;
                }
            };
            let n1 = row.get::<Node>("n1");
            let n2 = row.get::<Node>("n2");
            let rel = row.get::<String>("rel");
            let (Ok(n1), Ok(n2), Ok(rel)) = (n1, n2, rel) else {
                warn!("skipping unmappable edge row");
                continue;
            };
            let props: BTreeMap<String, serde_json::Value> = row
                .get::<BTreeMap<String, serde_json::Value>>("props")
                .unwrap_or_default();

            let raw1 = raw_from_node(&n1);
            let raw2 = raw_from_node(&n2);
            let src_id = node_id(&raw1);
            let dst_id = node_id(&raw2);
            nodes
                .entry(src_id.clone())
                .or_insert_with(|| to_graph_node(&raw1));
            nodes
                .entry(dst_id.clone())
                .or_insert_with(|| to_graph_node(&raw2));

            let amount = props.get("amount").and_then(|v| v.as_f64());
            let timestamp_ms = props
                .get("timestamp_ms")
                .and_then(|v| v.as_i64().or_else(|| v.as_f64().map(|f| f as i64)));
            // De-duplicate: the same row is returned once per seed.
            let key = (
                src_id.clone(),
                dst_id.clone(),
                rel.clone(),
                amount.map(f64::to_bits),
                timestamp_ms,
            );
            edges.entry(key).or_insert(GraphEdge {
                src: src_id,
                dst: dst_id,
                rel,
                amount,
                timestamp_ms,
            });
        },
        Err(e) => warn!(error = %e, "edge query failed — edges omitted"),
    }

    // --- destination account age ------------------------------------------
    let mut dst_account_age_hours = 0.0;
    match graph.execute(query(DST_AGE_QUERY).param("dst", dst)).await {
        Ok(mut stream) => match stream.next().await {
            Ok(Some(row)) => {
                if let Ok(v) = row.get::<serde_json::Value>("created_at_ms") {
                    let created = v.as_i64().or_else(|| v.as_f64().map(|f| f as i64));
                    if let Some(created_ms) = created {
                        dst_account_age_hours =
                            ((now_ms - created_ms) as f64 / 3_600_000.0).max(0.0);
                    }
                }
            }
            Ok(None) => {} // not in the graph → 0.0
            Err(e) => warn!(error = %e, "dst age query failed"),
        },
        Err(e) => warn!(error = %e, "dst age query failed"),
    }

    Extracted {
        subgraph: Subgraph {
            nodes: nodes.into_values().collect(),
            edges: edges.into_values().collect(),
        },
        dst_account_age_hours,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn value(v: &str) -> serde_json::Value {
        serde_json::Value::String(v.to_string())
    }

    fn account_raw() -> RawNode {
        let mut id_props = BTreeMap::new();
        id_props.insert("vpa_id".to_string(), value("alice@okaxis"));
        let mut props = BTreeMap::new();
        props.insert(
            "created_at_ms".to_string(),
            serde_json::json!(1_690_000_000_000u64),
        );
        props.insert(
            "risk_flags".to_string(),
            serde_json::json!(["mule", "new_device"]),
        );
        RawNode {
            labels: vec!["Account".to_string()],
            id_props,
            props,
            fallback_key: "42".to_string(),
        }
    }

    #[test]
    fn account_node_id_uses_vpa() {
        assert_eq!(node_id(&account_raw()), "Account:alice@okaxis");
        let node = to_graph_node(&account_raw());
        assert_eq!(node.id, "Account:alice@okaxis");
        assert_eq!(node.label, "Account");
        assert_eq!(node.props.len(), 2);
    }

    #[test]
    fn label_priority_picks_account_over_device() {
        let mut raw = account_raw();
        raw.labels = vec!["Device".to_string(), "Account".to_string()];
        assert_eq!(node_id(&raw), "Account:alice@okaxis");
    }

    #[test]
    fn device_node_id_uses_device_hash() {
        let mut id_props = BTreeMap::new();
        id_props.insert("device_hash".to_string(), value("dev-ring-a"));
        let raw = RawNode {
            labels: vec!["Device".to_string()],
            id_props,
            props: BTreeMap::new(),
            fallback_key: "7".to_string(),
        };
        assert_eq!(node_id(&raw), "Device:dev-ring-a");
    }

    #[test]
    fn unknown_label_falls_back_to_internal_id() {
        let raw = RawNode {
            labels: vec!["SomethingElse".to_string()],
            id_props: BTreeMap::new(),
            props: BTreeMap::new(),
            fallback_key: "99".to_string(),
        };
        assert_eq!(node_id(&raw), "SomethingElse:99");
    }

    #[test]
    fn display_props_filters_unsafe_keys() {
        let mut all = BTreeMap::new();
        all.insert("created_at_ms".to_string(), serde_json::json!(1));
        all.insert("risk_score".to_string(), serde_json::json!(0.4));
        all.insert("rooted".to_string(), serde_json::json!(true));
        all.insert("risk_flags".to_string(), serde_json::json!(["x"]));
        // Must be dropped:
        all.insert("note".to_string(), value("salary advance for friend"));
        all.insert("pan_hash".to_string(), value("abc"));

        let safe = display_props(&all);
        assert_eq!(safe.len(), 4);
        assert!(!safe.contains_key("note"));
        assert!(!safe.contains_key("pan_hash"));
        assert!(safe.contains_key("created_at_ms"));
        assert!(safe.contains_key("risk_flags"));
        assert!(safe.contains_key("rooted"));
        assert!(safe.contains_key("risk_score"));
    }

    #[test]
    fn extracted_subgraph_defaults_are_empty() {
        let sg = Subgraph::default();
        assert!(sg.nodes.is_empty());
        assert!(sg.edges.is_empty());
    }
}
