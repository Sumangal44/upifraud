"""Risk policy — Python mirror of `crates/upifraud-core/src/policy.rs` —
plus a deterministic heuristic scorer used as the inference fallback and as
the training sanity check.

Policy (must match Rust exactly)::

    score >= 0.80  -> BLOCK
    0.50 <= score  -> CHALLENGE
    otherwise      -> ALLOW

Blend (weights from env, normalised to sum to 1)::

    blended = 0.6 * gnn + 0.4 * tabular

The heuristic scorer is pure and deterministic (no randomness, no I/O): the
same `FeatureVector` always yields the same `(gnn_part, tabular_part,
factors)`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from ml.common.features import feature_map
from ml.common.schemas import DecisionAction, FeatureVector, ScoreFactor

# Defaults — mirror the Rust constants in policy.rs.
RISK_BLOCK_THRESHOLD_DEFAULT = 0.80
RISK_CHALLENGE_THRESHOLD_DEFAULT = 0.50
GNN_WEIGHT_DEFAULT = 0.6
TABULAR_WEIGHT_DEFAULT = 0.4


# ============================================================================
# Env helpers (Rust `from_env`: unparsable values are ignored -> default kept)
# ============================================================================


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def blend_weights(
    gnn_weight: Optional[float] = None, tabular_weight: Optional[float] = None
) -> tuple[float, float]:
    """Return normalised `(gnn_w, tabular_w)`; env-overridable.

    Mirrors `blend_with` in policy.rs: if the total weight is <= 0 the blend
    falls back to an equal 0.5/0.5 split.
    """
    g = gnn_weight if gnn_weight is not None else _env_float("GNN_SCORE_WEIGHT", GNN_WEIGHT_DEFAULT)
    t = (
        tabular_weight
        if tabular_weight is not None
        else _env_float("TABULAR_SCORE_WEIGHT", TABULAR_WEIGHT_DEFAULT)
    )
    total = g + t
    if total <= 0.0:
        return 0.5, 0.5
    return g / total, t / total


def blend(
    gnn: float,
    tabular: float,
    gnn_weight: Optional[float] = None,
    tabular_weight: Optional[float] = None,
) -> float:
    """`0.6*gnn + 0.4*tabular` with env weights, normalised."""
    g, t = blend_weights(gnn_weight, tabular_weight)
    return g * gnn + t * tabular


def decide(score: float) -> DecisionAction:
    """Map a blended risk score to an action (0.80 / 0.50 boundaries)."""
    if score >= _env_float("RISK_BLOCK_THRESHOLD", RISK_BLOCK_THRESHOLD_DEFAULT):
        return DecisionAction.BLOCK
    if score >= _env_float("RISK_CHALLENGE_THRESHOLD", RISK_CHALLENGE_THRESHOLD_DEFAULT):
        return DecisionAction.CHALLENGE
    return DecisionAction.ALLOW


@dataclass(frozen=True)
class PolicyConfig:
    """Runtime policy configuration (env-loaded, mirrors the Rust struct)."""

    block_threshold: float = RISK_BLOCK_THRESHOLD_DEFAULT
    challenge_threshold: float = RISK_CHALLENGE_THRESHOLD_DEFAULT
    gnn_weight: float = GNN_WEIGHT_DEFAULT
    tabular_weight: float = TABULAR_WEIGHT_DEFAULT

    @classmethod
    def from_env(cls) -> "PolicyConfig":
        return cls(
            block_threshold=_env_float("RISK_BLOCK_THRESHOLD", RISK_BLOCK_THRESHOLD_DEFAULT),
            challenge_threshold=_env_float(
                "RISK_CHALLENGE_THRESHOLD", RISK_CHALLENGE_THRESHOLD_DEFAULT
            ),
            gnn_weight=_env_float("GNN_SCORE_WEIGHT", GNN_WEIGHT_DEFAULT),
            tabular_weight=_env_float("TABULAR_SCORE_WEIGHT", TABULAR_WEIGHT_DEFAULT),
        )

    def decide(self, score: float) -> DecisionAction:
        if score >= self.block_threshold:
            return DecisionAction.BLOCK
        if score >= self.challenge_threshold:
            return DecisionAction.CHALLENGE
        return DecisionAction.ALLOW

    def blend(self, gnn: float, tabular: float) -> float:
        return blend(gnn, tabular, self.gnn_weight, self.tabular_weight)


# ============================================================================
# Heuristic scorer
# ============================================================================


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def _normalise(weights: Sequence[float]) -> list[float]:
    total = sum(weights)
    if total <= 0.0:
        return [0.0 for _ in weights]
    return [w / total for w in weights]


# (name, signal in [0,1], raw weight) — behavioural scalars -> tabular part.
def _tabular_signals(fv: FeatureVector) -> list[tuple[str, float, float]]:
    f = feature_map(fv)
    return [
        ("velocity.src_count_1h", _clamp01(f["src_count_1h"] / 10.0), 0.12),
        ("velocity.src_amount_1h", _clamp01(f["src_amount_1h"] / 100_000.0), 0.06),
        ("velocity.dst_count_1h", _clamp01(f["dst_count_1h"] / 10.0), 0.10),
        (
            "velocity.dst_distinct_senders_1h",
            _clamp01(f["dst_distinct_senders_1h"] / 6.0),
            0.12,
        ),
        ("velocity.device_count_1h", _clamp01(f["device_count_1h"] / 8.0), 0.05),
        ("velocity.ip_count_1h", _clamp01(f["ip_count_1h"] / 8.0), 0.05),
        ("amount.zscore", _clamp01(f["amount_zscore"] / 4.0), 0.12),
        ("ip.risk_score", _clamp01(f["ip_risk_score"]), 0.13),
        ("geo.jump_kmh", _clamp01(f["geo_jump_kmh"] / 900.0), 0.10),
        # Fresh destinations / devices are far riskier than established ones.
        ("dst.account_age_hours", 1.0 / (1.0 + max(0.0, f["dst_account_age_hours"]) / 12.0), 0.10),
        ("device.age_hours", 1.0 / (1.0 + max(0.0, f["device_age_hours"]) / 12.0), 0.05),
    ]


# Topological signals over the 2-hop subgraph -> gnn (approximation) part.
def _graph_signals(fv: FeatureVector) -> list[tuple[str, float, float]]:
    f = feature_map(fv)
    fan_in = f["graph_max_fan_in"]
    shared = f["graph_shared_device_accounts"]
    return [
        ("graph.transferred_edges", _clamp01(f["graph_transferred_edges"] / 10.0), 0.05),
        ("graph.distinct_accounts", _clamp01((f["graph_distinct_accounts"] - 2.0) / 8.0), 0.03),
        (
            "graph.fan_in_concentration",
            _clamp01((fan_in - 1.0) / 6.0) if fan_in >= 2 else 0.0,
            0.25,
        ),
        ("graph.fast_pass_2hop", _clamp01(f["graph_fast_pass_count"] / 2.0), 0.30),
        (
            "graph.shared_device",
            _clamp01((shared - 1.0) / 3.0) if shared >= 2 else 0.0,
            0.12,
        ),
        ("graph.ip_risk", _clamp01(f["graph_max_ip_risk"]), 0.10),
        ("graph.watchlist_flags", _clamp01(f["graph_watchlist_flags"] / 2.0), 0.15),
    ]


def heuristic_scores(
    features: FeatureVector,
    gnn_weight: Optional[float] = None,
    tabular_weight: Optional[float] = None,
) -> tuple[float, float, list[ScoreFactor]]:
    """Deterministic heuristic risk estimate.

    Returns `(gnn_part, tabular_part, factors)` where:

    * `gnn_part` approximates topological risk from subgraph stats
      (TRANSFERRED density, distinct accounts, fan-in concentration,
      fast-pass paths, shared devices, IP/watchlist flags);
    * `tabular_part` is computed from behavioural scalars (velocity,
      new destination, amounts, IP risk, geo jumps, ages);
    * `factors` are signed contributions **to the blended score** — their sum
      equals `blend(gnn_part, tabular_part)` — sorted by `abs(weight)` desc.
    """
    g_w, t_w = blend_weights(gnn_weight, tabular_weight)

    tab_signals = _tabular_signals(features)
    gnn_signals = _graph_signals(features)

    tab_norm = _normalise([w for _, _, w in tab_signals])
    gnn_norm = _normalise([w for _, _, w in gnn_signals])

    tabular_part = 0.0
    gnn_part = 0.0
    factors: list[ScoreFactor] = []
    for (name, signal, _), w in zip(tab_signals, tab_norm):
        contribution = signal * w
        tabular_part += contribution
        factors.append(ScoreFactor(name=name, weight=t_w * contribution))
    for (name, signal, _), w in zip(gnn_signals, gnn_norm):
        contribution = signal * w
        gnn_part += contribution
        factors.append(ScoreFactor(name=name, weight=g_w * contribution))

    gnn_part = _clamp01(gnn_part)
    tabular_part = _clamp01(tabular_part)
    factors.sort(key=lambda sf: (-abs(sf.weight), sf.name))
    return gnn_part, tabular_part, factors


def heuristic_blend(
    features: FeatureVector,
    gnn_weight: Optional[float] = None,
    tabular_weight: Optional[float] = None,
) -> tuple[float, float, float, list[ScoreFactor]]:
    """Convenience: `(gnn, tabular, blended, factors)` for one feature vector."""
    gnn, tabular, factors = heuristic_scores(features, gnn_weight, tabular_weight)
    return gnn, tabular, blend(gnn, tabular, gnn_weight, tabular_weight), factors


def top_factors(factors: Iterable[ScoreFactor], limit: int = 8) -> list[ScoreFactor]:
    """Top-`limit` factors sorted by `abs(weight)` descending (stable)."""
    ordered = sorted(factors, key=lambda sf: (-abs(sf.weight), sf.name))
    return ordered[:limit]
