"""Shared builders for ML tests (deterministic, no I/O)."""

from __future__ import annotations

from typing import Any, Optional

from ml.common.schemas import (
    Decision,
    DecisionAction,
    FeatureVector,
    GraphEdge,
    GraphNode,
    LatencyBreakdown,
    Subgraph,
    VelocityFeatures,
)


def make_velocity(**overrides: Any) -> VelocityFeatures:
    base: dict[str, Any] = {
        "src_count_1h": 1,
        "src_amount_1h": 500.0,
        "src_count_24h": 3,
        "src_amount_24h": 1500.0,
        "dst_count_1h": 1,
        "dst_distinct_senders_1h": 1,
        "device_count_1h": 1,
        "ip_count_1h": 1,
    }
    base.update(overrides)
    return VelocityFeatures(**base)


def honest_subgraph() -> Subgraph:
    """A quiet 2-party neighbourhood: sender, receiver, their device/IP."""
    return Subgraph(
        nodes=[
            GraphNode(id="Account:a@okaxis", label="Account",
                      props={"vpa_id": "a@okaxis", "created_at_ms": 1_700_000_000_000}),
            GraphNode(id="Account:b@okaxis", label="Account",
                      props={"vpa_id": "b@okaxis", "created_at_ms": 1_700_000_000_000}),
            GraphNode(id="Device:dev-a", label="Device",
                      props={"created_at_ms": 1_700_000_000_000}),
            GraphNode(id="IPAddress:10.0.0.1", label="IPAddress", props={"risk_score": 0.1}),
        ],
        edges=[
            GraphEdge(src="Account:a@okaxis", dst="Account:b@okaxis", rel="TRANSFERRED",
                      amount=250.0, timestamp_ms=1_735_000_000_000),
            GraphEdge(src="Account:a@okaxis", dst="Device:dev-a", rel="USED_DEVICE",
                      timestamp_ms=1_735_000_000_000),
            GraphEdge(src="Account:a@okaxis", dst="IPAddress:10.0.0.1", rel="CONNECTED_FROM",
                      timestamp_ms=1_735_000_000_000),
        ],
    )


def mule_subgraph() -> Subgraph:
    """Fan-in to a mule + 2-hop fast pass-through + shared ring device."""
    t0 = 1_735_700_000_000
    return Subgraph(
        nodes=[
            GraphNode(id="Account:victim@upi", label="Account",
                      props={"vpa_id": "victim@upi", "created_at_ms": 1_700_000_000_000}),
            GraphNode(id="Account:mule1@mule", label="Account",
                      props={"vpa_id": "mule1@mule", "created_at_ms": t0 - 40 * 3_600_000,
                             "risk_flags": ["mule_suspect"]}),
            GraphNode(id="Account:mule2@mule", label="Account",
                      props={"vpa_id": "mule2@mule", "created_at_ms": t0 - 40 * 3_600_000,
                             "risk_flags": ["mule_suspect"]}),
            GraphNode(id="Account:cash@cash", label="Account",
                      props={"vpa_id": "cash@cash", "created_at_ms": t0 - 40 * 3_600_000,
                             "risk_flags": ["cashout"]}),
            GraphNode(id="Device:dev-ring-a", label="Device", props={"created_at_ms": t0}),
            GraphNode(id="IPAddress:10.66.0.1", label="IPAddress", props={"risk_score": 0.9}),
        ],
        edges=[
            GraphEdge(src="Account:victim@upi", dst="Account:mule1@mule", rel="TRANSFERRED",
                      amount=48_000.0, timestamp_ms=t0),
            GraphEdge(src="Account:mule1@mule", dst="Account:mule2@mule", rel="TRANSFERRED",
                      amount=47_500.0, timestamp_ms=t0 + 90_000),
            GraphEdge(src="Account:mule2@mule", dst="Account:cash@cash", rel="TRANSFERRED",
                      amount=47_000.0, timestamp_ms=t0 + 180_000),
            GraphEdge(src="Account:mule1@mule", dst="Device:dev-ring-a", rel="USED_DEVICE",
                      timestamp_ms=t0),
            GraphEdge(src="Account:mule2@mule", dst="Device:dev-ring-a", rel="USED_DEVICE",
                      timestamp_ms=t0),
            GraphEdge(src="Account:mule1@mule", dst="IPAddress:10.66.0.1",
                      rel="CONNECTED_FROM", timestamp_ms=t0),
            GraphEdge(src="Account:mule2@mule", dst="IPAddress:10.66.0.1",
                      rel="CONNECTED_FROM", timestamp_ms=t0),
        ],
    )


def make_features(
    velocity: Optional[VelocityFeatures] = None,
    subgraph: Optional[Subgraph] = None,
    **overrides: Any,
) -> FeatureVector:
    base: dict[str, Any] = {
        "tx_id": "test-tx-001",
        "velocity": velocity or make_velocity(),
        "dst_account_age_hours": 720.0,
        "device_age_hours": 720.0,
        "geo_jump_kmh": 0.0,
        "amount_zscore": 0.2,
        "ip_risk_score": 0.1,
        "subgraph": subgraph or honest_subgraph(),
    }
    base.update(overrides)
    return FeatureVector(**base)


def make_decision(
    score: float = 0.85,
    action: DecisionAction = DecisionAction.BLOCK,
    reasons: Optional[list[str]] = None,
) -> Decision:
    return Decision(
        tx_id="test-tx-001",
        timestamp_ms=1_735_700_000_000,
        score=score,
        action=action,
        reasons=reasons if reasons is not None else ["MULE_PATH_2HOP", "HIGH_VELOCITY_1H"],
        model_version="heuristic-v0",
        latency=LatencyBreakdown(
            ingest_ms=2, feature_ms=5, inference_ms=3, policy_ms=1, total_ms=11
        ),
    )
