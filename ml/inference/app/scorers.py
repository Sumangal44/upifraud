"""Scorer implementations behind `/v1/score`.

Every scorer exposes::

    score(features: FeatureVector) -> (score_0_to_1, list[ScoreFactor])

* `HeuristicGnnScorer`  — topological estimate from subgraph stats (fallback).
* `HeuristicTabularScorer` — behavioural estimate from scalars (fallback).
* `TorchScorer` — TorchScript / `torch.save`d RGCN (`rgcn-v1`).
* `XgbScorer` — XGBoost booster with gain-based factors (`xgb-v1`).

All scorers are deterministic: no randomness, no clocks, no I/O per request.
"""

from __future__ import annotations

from typing import Any, Sequence

import numpy as np

from ml.common.features import tabular_row
from ml.common.graph_features import build_graph_arrays, with_reverse_relations
from ml.common.schemas import FeatureVector, ScoreFactor
from ml.common.scoring import blend_weights, heuristic_scores

HEURISTIC_VERSION = "heuristic-v0"


def _rescale(factors: list[ScoreFactor], target_sum: float) -> list[ScoreFactor]:
    """Scale factors so their weights sum to `target_sum` (keeps signs)."""
    current = sum(f.weight for f in factors)
    if current <= 0.0 or target_sum <= 0.0:
        return []
    scale = target_sum / current
    return [ScoreFactor(name=f.name, weight=f.weight * scale) for f in factors]


class HeuristicGnnScorer:
    """Topological (graph) half of the heuristic fallback scorer."""

    kind = "gnn"
    version = HEURISTIC_VERSION
    source = "heuristic"

    def score(self, features: FeatureVector) -> tuple[float, list[ScoreFactor]]:
        gnn, _, factors = heuristic_scores(features)
        return gnn, [f for f in factors if f.name.startswith("graph.")]


class HeuristicTabularScorer:
    """Behavioural (tabular) half of the heuristic fallback scorer."""

    kind = "tabular"
    version = HEURISTIC_VERSION
    source = "heuristic"

    def score(self, features: FeatureVector) -> tuple[float, list[ScoreFactor]]:
        _, tab, factors = heuristic_scores(features)
        return tab, [f for f in factors if not f.name.startswith("graph.")]


class TorchScorer:
    """RGCN served from a TorchScript archive or a full `torch.save` module.

    Contract: `model(node_features, edge_index, edge_type) -> logits (N, 1)`
    over the featurised subgraph; the transaction score is the sigmoid of the
    **max pooled score over Account nodes** (the neighbourhood's riskiest
    participant dominates, which is what a mule-path transaction looks like).
    """

    kind = "gnn"
    version = "rgcn-v1"
    source = "torch"

    def __init__(self, model: Any, torch_module: Any) -> None:
        self._model = model
        self._torch = torch_module

    def score(self, features: FeatureVector) -> tuple[float, list[ScoreFactor]]:
        arrays = build_graph_arrays(features.subgraph)
        if arrays.num_nodes == 0:
            return HeuristicGnnScorer().score(features)

        t = self._torch
        node_features = t.from_numpy(arrays.node_features)
        ei, et = with_reverse_relations(arrays.edge_index, arrays.edge_type)
        edge_index = t.from_numpy(ei)
        edge_type = t.from_numpy(et)
        with t.no_grad():
            logits = self._model(node_features, edge_index, edge_type)
            probs = t.sigmoid(logits.reshape(-1))
        probs_np = probs.cpu().numpy()

        mask = arrays.account_mask if arrays.account_mask.any() else np.ones_like(arrays.account_mask)
        score = float(probs_np[mask].max())

        # Attribution proxy: heuristic graph factors rescaled to this score so
        # the factor sum still matches the model's blended contribution.
        _, _, factors = heuristic_scores(features)
        g_w, _ = blend_weights()
        graph_factors = [f for f in factors if f.name.startswith("graph.")]
        return score, _rescale(graph_factors, g_w * score)


class XgbScorer:
    """XGBoost behavioural model; factors come from per-feature gain."""

    kind = "tabular"
    version = "xgb-v1"
    source = "xgboost"

    def __init__(self, booster: Any, xgb_module: Any, feature_names: Sequence[str]) -> None:
        self._booster = booster
        self._xgb = xgb_module
        self.feature_names = list(feature_names)

    def score(self, features: FeatureVector) -> tuple[float, list[ScoreFactor]]:
        row = np.asarray(tabular_row(features, self.feature_names), dtype=np.float32)
        dmat = self._xgb.DMatrix(row.reshape(1, -1), feature_names=self.feature_names)
        pred = self._booster.predict(dmat)
        score = float(pred[0])
        score = 0.0 if score < 0.0 else (1.0 if score > 1.0 else score)

        _, tab_factors = HeuristicTabularScorer().score(features)
        _, t_w = blend_weights()

        gains: dict[str, float] = {}
        try:
            gains = {k: float(v) for k, v in self._booster.get_score(importance_type="gain").items()}
        except Exception:  # pragma: no cover - booster without gain stats
            gains = {}
        gain_total = sum(v for v in gains.values() if v > 0.0)
        if gain_total > 0.0:
            factors = [
                ScoreFactor(name=f"feature.{name}", weight=t_w * score * (gain / gain_total))
                for name, gain in sorted(gains.items())
                if gain > 0.0
            ]
            factors.sort(key=lambda sf: (-abs(sf.weight), sf.name))
            return score, factors
        # No gain stats: fall back to (rescaled) heuristic behavioural factors.
        return score, _rescale(tab_factors, t_w * score)


def default_models() -> tuple[HeuristicGnnScorer, HeuristicTabularScorer]:
    return HeuristicGnnScorer(), HeuristicTabularScorer()
