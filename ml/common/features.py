"""Canonical tabular feature set shared by training, XGBoost inference and
the heuristic scorer.

Every feature is derivable from a `FeatureVector` alone (no I/O), so the row
built at inference time matches the row built during training byte-for-byte.
Deterministic by construction.
"""

from __future__ import annotations

from ml.common.schemas import FeatureVector, GraphEdge, Subgraph

#: Ordered feature names — `ml/artifacts/features.json` mirrors this list.
TABULAR_FEATURES: list[str] = [
    # Velocity-like aggregates (Dragonfly/Redis counters)
    "src_count_1h",
    "src_amount_1h",
    "src_count_24h",
    "src_amount_24h",
    "dst_count_1h",
    "dst_distinct_senders_1h",
    "device_count_1h",
    "ip_count_1h",
    # Behavioural scalars
    "dst_account_age_hours",
    "device_age_hours",
    "geo_jump_kmh",
    "amount_zscore",
    "ip_risk_score",
    # Graph aggregates over the 2-hop subgraph
    "graph_transferred_edges",
    "graph_distinct_accounts",
    "graph_max_fan_in",
    "graph_fast_pass_count",
    "graph_shared_device_accounts",
    "graph_max_ip_risk",
    "graph_watchlist_flags",
]

#: Fast-pass window: two chained TRANSFERRED edges <= 5 minutes apart.
FAST_PASS_WINDOW_MS = 300_000

_WATCHLIST_FLAGS = ("mule_suspect", "cashout")


def fast_pass_count(edges: list[GraphEdge]) -> int:
    """Chained `a->b` + `b->c` TRANSFERRED pairs <= 300 s apart."""
    transferred = [e for e in edges if e.rel == "TRANSFERRED" and e.timestamp_ms is not None]
    out_edges: dict[str, list[GraphEdge]] = {}
    for e in transferred:
        out_edges.setdefault(e.src, []).append(e)
    count = 0
    for e1 in transferred:
        for e2 in out_edges.get(e1.dst, ()):
            if abs(int(e2.timestamp_ms) - int(e1.timestamp_ms)) <= FAST_PASS_WINDOW_MS:
                count += 1
    return count


def graph_features(sub: Subgraph) -> dict[str, float]:
    """Raw topological aggregates over a neighbourhood subgraph."""
    account_ids = {n.id for n in sub.nodes if n.label == "Account"}
    transferred = [e for e in sub.edges if e.rel == "TRANSFERRED"]

    # Fan-in: most-paid account by distinct sources.
    in_degree: dict[str, set[str]] = {}
    for e in transferred:
        in_degree.setdefault(e.dst, set()).add(e.src)
    max_fan_in = max((len(s) for s in in_degree.values()), default=0)

    # Shared devices: most accounts on a single device node.
    device_accounts: dict[str, set[str]] = {}
    for e in sub.edges:
        if e.rel != "USED_DEVICE":
            continue
        if e.src.startswith("Account:") and e.dst.startswith("Device:"):
            device_accounts.setdefault(e.dst, set()).add(e.src)
        elif e.dst.startswith("Account:") and e.src.startswith("Device:"):
            device_accounts.setdefault(e.src, set()).add(e.dst)
    shared = max((len(s) for s in device_accounts.values()), default=0)

    # IP reputation of any IPAddress node in the neighbourhood.
    max_ip_risk = 0.0
    flagged = 0
    for n in sub.nodes:
        if n.label == "IPAddress":
            try:
                max_ip_risk = max(max_ip_risk, float(n.props.get("risk_score", 0.0) or 0.0))
            except (TypeError, ValueError):
                pass
        flags = n.props.get("risk_flags") or []
        if isinstance(flags, str):
            flags = [flags]
        if any(str(x) in _WATCHLIST_FLAGS for x in flags):
            flagged += 1

    return {
        "graph_transferred_edges": float(len(transferred)),
        "graph_distinct_accounts": float(len(account_ids)),
        "graph_max_fan_in": float(max_fan_in),
        "graph_fast_pass_count": float(fast_pass_count(sub.edges)),
        "graph_shared_device_accounts": float(shared),
        "graph_max_ip_risk": float(max_ip_risk),
        "graph_watchlist_flags": float(flagged),
    }


def feature_map(features: FeatureVector) -> dict[str, float]:
    """All canonical features for one feature vector, keyed by name."""
    v = features.velocity
    out: dict[str, float] = {
        "src_count_1h": float(v.src_count_1h),
        "src_amount_1h": float(v.src_amount_1h),
        "src_count_24h": float(v.src_count_24h),
        "src_amount_24h": float(v.src_amount_24h),
        "dst_count_1h": float(v.dst_count_1h),
        "dst_distinct_senders_1h": float(v.dst_distinct_senders_1h),
        "device_count_1h": float(v.device_count_1h),
        "ip_count_1h": float(v.ip_count_1h),
        "dst_account_age_hours": float(features.dst_account_age_hours),
        "device_age_hours": float(features.device_age_hours),
        "geo_jump_kmh": float(features.geo_jump_kmh),
        "amount_zscore": float(features.amount_zscore),
        "ip_risk_score": float(features.ip_risk_score),
    }
    out.update(graph_features(features.subgraph))
    return out


def tabular_row(features: FeatureVector, names: list[str] | None = None) -> list[float]:
    """Dense feature row; `names` defaults to the canonical ordering."""
    fmap = feature_map(features)
    return [float(fmap.get(name, 0.0)) for name in (names or TABULAR_FEATURES)]
