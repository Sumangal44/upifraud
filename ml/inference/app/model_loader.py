"""Optional model loaders.

Lifetied at service start: try `torch` for `ml/artifacts/gnn_rgcn.pt`, try
`xgboost` for `ml/artifacts/xgb.json` (+ `features.json`), and always fall
back to the deterministic heuristic scorers — the service must be runnable
with zero artifacts.

Heavy deps (torch) are imported lazily and guarded: missing packages or
missing artifacts downgrade to the heuristic path instead of failing.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ml.common.features import TABULAR_FEATURES
from ml.inference.app import scorers

log = logging.getLogger(__name__)

DEFAULT_ARTIFACTS_DIR = "ml/artifacts"


@dataclass
class LoadedModels:
    gnn: Any
    tabular: Any
    artifacts_dir: str
    notes: list[str] = field(default_factory=list)

    @classmethod
    def heuristic(cls, artifacts_dir: str = "") -> "LoadedModels":
        return cls(
            gnn=scorers.HeuristicGnnScorer(),
            tabular=scorers.HeuristicTabularScorer(),
            artifacts_dir=artifacts_dir,
            notes=["heuristic fallback (no models loaded)"],
        )

    @property
    def model_version(self) -> str:
        g, t = self.gnn.version, self.tabular.version
        return g if g == t else f"{g}+{t}"

    @property
    def loaded(self) -> dict[str, bool]:
        return {
            "gnn": getattr(self.gnn, "source", "heuristic") != "heuristic",
            "tabular": getattr(self.tabular, "source", "heuristic") != "heuristic",
        }

    def describe(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "model_version": self.model_version,
            "scorers": {
                "gnn": {"version": self.gnn.version, "source": getattr(self.gnn, "source", "?")},
                "tabular": {
                    "version": self.tabular.version,
                    "source": getattr(self.tabular, "source", "?"),
                },
            },
            "artifacts_dir": self.artifacts_dir,
            "notes": self.notes,
        }


def _optional_import(name: str) -> Optional[Any]:
    """Import a heavy optional dependency, or None if unavailable."""
    try:
        return importlib.import_module(name)
    except Exception as exc:  # ImportError, but also binary-mismatch RuntimeErrors
        log.info("optional dependency %s unavailable: %s", name, exc)
        return None


def _read_feature_list(path: Path) -> list[str]:
    raw = json.loads(path.read_text())
    if isinstance(raw, dict):
        raw = raw.get("features", [])
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise ValueError(f"{path} does not contain a list of feature names")
    return raw


def load_models(artifacts_dir: Optional[str] = None) -> LoadedModels:
    """Load whatever artifacts exist under `artifacts_dir` (env `ARTIFACTS_DIR`)."""
    art = Path(artifacts_dir or os.environ.get("ARTIFACTS_DIR") or DEFAULT_ARTIFACTS_DIR)
    loaded = LoadedModels(
        gnn=scorers.HeuristicGnnScorer(),
        tabular=scorers.HeuristicTabularScorer(),
        artifacts_dir=str(art),
        notes=[],
    )

    # --- GNN (torch) -------------------------------------------------------
    torch = _optional_import("torch")
    gnn_path = art / "gnn_rgcn.pt"
    if torch is None:
        loaded.notes.append("torch not installed — GNN scorer: heuristic")
    elif not gnn_path.exists():
        loaded.notes.append(f"no artifact {gnn_path} — GNN scorer: heuristic")
    else:
        try:
            try:
                model = torch.jit.load(str(gnn_path), map_location="cpu")
            except Exception:
                model = torch.load(str(gnn_path), map_location="cpu", weights_only=False)
            if hasattr(model, "eval"):
                model.eval()
            loaded.gnn = scorers.TorchScorer(model, torch)
            loaded.notes.append(f"GNN loaded from {gnn_path}")
        except Exception as exc:
            loaded.notes.append(f"failed to load {gnn_path}: {exc} — GNN scorer: heuristic")

    # --- Tabular (xgboost) -------------------------------------------------
    xgb = _optional_import("xgboost")
    xgb_path = art / "xgb.json"
    if xgb is None:
        loaded.notes.append("xgboost not installed — tabular scorer: heuristic")
    elif not xgb_path.exists():
        loaded.notes.append(f"no artifact {xgb_path} — tabular scorer: heuristic")
    else:
        try:
            booster = xgb.Booster()
            booster.load_model(str(xgb_path))
            feature_names = TABULAR_FEATURES
            fjson = art / "features.json"
            if fjson.exists():
                feature_names = _read_feature_list(fjson)
            loaded.tabular = scorers.XgbScorer(booster, xgb, feature_names)
            loaded.notes.append(f"XGBoost loaded from {xgb_path} ({len(feature_names)} features)")
        except Exception as exc:
            loaded.notes.append(f"failed to load {xgb_path}: {exc} — tabular scorer: heuristic")

    if not loaded.notes:
        loaded.notes.append("no artifacts found — heuristic fallback")
    log.info("model loading: %s", "; ".join(loaded.notes))
    return loaded
