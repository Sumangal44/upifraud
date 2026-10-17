"""Attribution engines for `/v1/explain`.

1. **Heuristic** (always available, deterministic): signed contributions of
   every risk signal, rescaled so the weights sum to the decision score —
   i.e. the "score delta" from a 0 baseline.
2. **SHAP** (optional): when `xgboost` + `shap` are importable *and* a trained
   `ml/artifacts/xgb.json` exists, TreeExplainer values are preferred — they
   natively sum to `prediction - expected_value` (the score delta).
"""

from __future__ import annotations

import importlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

import numpy as np

from ml.common.features import TABULAR_FEATURES, tabular_row
from ml.common.schemas import FeatureVector, ScoreFactor
from ml.common.scoring import heuristic_scores

log = logging.getLogger(__name__)


def _rescale_to(factors: list[ScoreFactor], target: Optional[float]) -> list[ScoreFactor]:
    """Scale factor weights so they sum to `target` (keeps relative shape)."""
    if target is None:
        return factors
    total = sum(f.weight for f in factors)
    if abs(total) <= 1e-12:
        return factors
    scale = target / total
    return [ScoreFactor(name=f.name, weight=f.weight * scale) for f in factors]


def heuristic_attributions(
    features: FeatureVector, target_score: Optional[float] = None
) -> list[ScoreFactor]:
    """Deterministic heuristic attributions sorted by `abs(weight)` desc."""
    _, _, factors = heuristic_scores(features)
    factors = _rescale_to(factors, target_score)
    factors.sort(key=lambda sf: (-abs(sf.weight), sf.name))
    return factors


def _optional_import(name: str) -> Optional[Any]:
    try:
        return importlib.import_module(name)
    except Exception as exc:
        log.info("optional dependency %s unavailable: %s", name, exc)
        return None


class ShapExplainer:
    """TreeExplainer over the trained XGBoost booster (optional path)."""

    source = "shap"

    def __init__(self, booster: Any, xgb_module: Any, explainer: Any, feature_names: list[str]):
        self.booster = booster
        self.xgb = xgb_module
        self.explainer = explainer
        self.feature_names = feature_names

    def explain(
        self, features: FeatureVector, target_score: Optional[float] = None
    ) -> Optional[list[ScoreFactor]]:
        try:
            row = np.asarray(tabular_row(features, self.feature_names), dtype=np.float32)
            values = np.asarray(self.explainer.shap_values(row.reshape(1, -1)), dtype=np.float64)
            values = values.reshape(-1)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("shap explanation failed: %s", exc)
            return None
        factors = [
            ScoreFactor(name=f"feature.{name}", weight=float(v))
            for name, v in zip(self.feature_names, values)
            if abs(float(v)) > 1e-9
        ]
        # SHAP values already sum to prediction - base value (the score delta),
        # so no rescaling: only sort.
        factors.sort(key=lambda sf: (-abs(sf.weight), sf.name))
        return factors


def load_shap_explainer(artifacts_dir: Optional[str] = None) -> Optional[ShapExplainer]:
    """Best-effort SHAP setup; returns None unless xgboost+shap+artifact exist."""
    art = Path(artifacts_dir or os.environ.get("ARTIFACTS_DIR") or "ml/artifacts")
    xgb_path = art / "xgb.json"
    if not xgb_path.exists():
        return None
    xgb = _optional_import("xgboost")
    shap = _optional_import("shap")
    if xgb is None or shap is None:
        return None
    try:
        booster = xgb.Booster()
        booster.load_model(str(xgb_path))
        feature_names = list(TABULAR_FEATURES)
        fjson = art / "features.json"
        if fjson.exists():
            raw = json.loads(fjson.read_text())
            if isinstance(raw, dict):
                raw = raw.get("features", [])
            if isinstance(raw, list) and all(isinstance(x, str) for x in raw):
                feature_names = raw
        explainer = shap.TreeExplainer(booster)
    except Exception as exc:
        log.warning("SHAP explainer unavailable: %s", exc)
        return None
    return ShapExplainer(booster, xgb, explainer, feature_names)
