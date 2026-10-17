"""Python mirror of the Rust wire contract: `crates/upifraud-core/src/types.rs`.

Rules:
  * JSON field names are ``snake_case`` — byte-compatible with the serde types.
  * Unknown fields are rejected (``extra="forbid"``) so a payload that would
    not deserialise in Rust fails loudly here too.
  * Option/#[serde(default)] fields get Python defaults so omitted JSON keys
    round-trip the same way as in Rust.

NOTE: UPI PINs, MSISDNs and account numbers are NEVER part of these payloads
(RBI/CERT-In data-minimisation) — only salted hashes and VPAs.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class WireModel(BaseModel):
    """Base for every wire type: reject unknown keys, keep snake_case fields."""

    model_config = ConfigDict(extra="forbid")


# ============================================================================
# Ingestion
# ============================================================================


class Geo(WireModel):
    lat: float
    lon: float
    city: Optional[str] = None


class DeviceInfo(WireModel):
    """Endpoint/device telemetry from the mobile SDK (salted hash only)."""

    device_hash: str
    app_version: Optional[str] = None
    rooted: bool = False
    screen_mirror: bool = False
    emulator: bool = False


class TransactionEvent(WireModel):
    tx_id: str
    timestamp_ms: int
    src_vpa: str
    dst_vpa: str
    amount_inr: float
    device: DeviceInfo
    src_ip: str
    geo: Geo
    merchant_id: Optional[str] = None
    note: Optional[str] = None  # NEVER echoed into briefs (PII guard)


# ============================================================================
# Features
# ============================================================================


class VelocityFeatures(WireModel):
    """Rolling velocity counters computed from Dragonfly/Redis."""

    src_count_1h: int
    src_amount_1h: float
    src_count_24h: int
    src_amount_24h: float
    dst_count_1h: int
    dst_distinct_senders_1h: int
    device_count_1h: int
    ip_count_1h: int


class GraphNode(WireModel):
    id: str  # stable id, `{label}:{key}` e.g. `Account:alice@okaxis`
    label: str  # Account | Device | IPAddress | Merchant
    props: dict[str, Any] = {}


class GraphEdge(WireModel):
    src: str
    dst: str
    rel: str  # TRANSFERRED | USED_DEVICE | CONNECTED_FROM | PAID_MERCHANT
    amount: Optional[float] = None
    timestamp_ms: Optional[int] = None


class Subgraph(WireModel):
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []


class FeatureVector(WireModel):
    tx_id: str
    velocity: VelocityFeatures
    dst_account_age_hours: float
    device_age_hours: float
    geo_jump_kmh: float
    amount_zscore: float
    ip_risk_score: float
    subgraph: Subgraph


# ============================================================================
# Inference
# ============================================================================


class ScoreRequest(WireModel):
    tx_id: str
    features: FeatureVector


class ScoreFactor(WireModel):
    """A single contributing factor (positive weight -> riskier)."""

    name: str
    weight: float


class ScoreResponse(WireModel):
    tx_id: str
    gnn_score: float
    tabular_score: float
    blended_score: float
    model_version: str
    top_factors: list[ScoreFactor] = []


# ============================================================================
# Decisions & alerts
# ============================================================================


class DecisionAction(str, Enum):
    ALLOW = "ALLOW"
    CHALLENGE = "CHALLENGE"
    BLOCK = "BLOCK"


class LatencyBreakdown(WireModel):
    ingest_ms: int
    feature_ms: int
    inference_ms: int
    policy_ms: int
    total_ms: int


class Decision(WireModel):
    tx_id: str
    timestamp_ms: int
    score: float
    action: DecisionAction
    reasons: list[str]
    top_factors: list[ScoreFactor] = []
    model_version: str
    latency: LatencyBreakdown


class Alert(WireModel):
    tx_id: str
    timestamp_ms: int
    score: float
    action: DecisionAction
    reasons: list[str]
    src_vpa: str
    dst_vpa: str
    amount_inr: float
    model_version: str


# ============================================================================
# Explainability (XAI service)
# ============================================================================


class ExplainRequest(WireModel):
    tx_id: str
    decision: Decision
    features: FeatureVector


class ExplainResponse(WireModel):
    tx_id: str
    attributions: list[ScoreFactor]
    brief: str
    engine: str
