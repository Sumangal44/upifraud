"""Train the tabular XGBoost model (+ optional GNN) into `ml/artifacts/`.

    PYTHONPATH=. python -m ml.training.train
    make train                       # same thing

Steps:
  1. generate the synthetic fraud dataset (seeded, deterministic);
  2. train XGBoost with a stratified split, `scale_pos_weight` and early
     stopping  -> `xgb.json` + `features.json` + `metrics.json`;
  3. if `torch` + `torch-geometric` are importable, train the 2-layer RGCN
     -> `gnn_rgcn.pt`; otherwise log a clear message and exit 0.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

import numpy as np

from ml.common.features import TABULAR_FEATURES
from ml.training.dataset import FraudDataset, generate

DEFAULT_ARTIFACTS_DIR = "ml/artifacts"


def train_xgb(
    ds: FraudDataset,
    out_dir: Path,
    seed: int,
    rounds: int = 400,
    val_frac: float = 0.2,
) -> dict[str, Any]:
    """Train XGBoost on the tabular rows; persist model + feature list."""
    import pandas as pd
    import xgboost as xgb
    from sklearn.metrics import precision_score, recall_score, roc_auc_score
    from sklearn.model_selection import train_test_split

    df = pd.DataFrame(ds.x, columns=ds.feature_names)
    y = ds.y
    train_idx, val_idx = train_test_split(
        np.arange(len(y)), test_size=val_frac, random_state=seed, stratify=y
    )

    dtrain = xgb.DMatrix(df.iloc[train_idx], label=y[train_idx])
    dval = xgb.DMatrix(df.iloc[val_idx], label=y[val_idx])
    n_pos = int(y[train_idx].sum())
    n_neg = int(len(train_idx)) - n_pos

    params: dict[str, Any] = {
        "objective": "binary:logistic",
        "eval_metric": ["auc", "logloss"],
        "max_depth": 6,
        "eta": 0.08,
        "min_child_weight": 5,
        "subsample": 0.9,
        "colsample_bytree": 0.9,
        "scale_pos_weight": (n_neg / max(1, n_pos)),
        "tree_method": "hist",
        "seed": seed,
    }
    booster = xgb.train(
        params,
        dtrain,
        num_boost_round=rounds,
        evals=[(dtrain, "train"), (dval, "val")],
        early_stopping_rounds=30,
        verbose_eval=False,
    )
    best_iteration = getattr(booster, "best_iteration", None)
    predict_kwargs: dict[str, Any] = {}
    if best_iteration is not None:
        predict_kwargs["iteration_range"] = (0, int(best_iteration) + 1)
    pred = booster.predict(dval, **predict_kwargs)
    y_val = y[val_idx]

    auc = float(roc_auc_score(y_val, pred))
    at50 = pred >= 0.5
    at80 = pred >= 0.8
    metrics = {
        "auc": auc,
        "precision": float(precision_score(y_val, at50, zero_division=0)),
        "recall": float(recall_score(y_val, at50, zero_division=0)),
        "precision_at_0.8": float(precision_score(y_val, at80, zero_division=0)),
        "recall_at_0.8": float(recall_score(y_val, at80, zero_division=0)),
        "best_iteration": int(best_iteration) + 1 if best_iteration is not None else rounds,
        "scale_pos_weight": params["scale_pos_weight"],
        "train_samples": int(len(train_idx)),
        "val_samples": int(len(val_idx)),
        "val_fraud_rate": round(float(y_val.mean()), 4),
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    booster.save_model(str(out_dir / "xgb.json"))
    (out_dir / "features.json").write_text(json.dumps(ds.feature_names, indent=2))
    return metrics


def train_gnn(
    ds: FraudDataset, out_dir: Path, seed: int, epochs: int
) -> dict[str, Any]:
    """Train the RGCN when torch+torch_geometric are available, else skip."""
    try:
        import torch  # noqa: F401
        import torch_geometric  # noqa: F401
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        print("[train] GNN skipped — torch/torch-geometric not available.")
        print(f"[train]   reason: {reason}")
        print(
            "[train]   install with: pip install torch --index-url "
            "https://download.pytorch.org/whl/cpu && pip install torch-geometric"
        )
        return {"skipped": True, "reason": reason}

    from ml.training.gnn import train_rgcn

    out_dir.mkdir(parents=True, exist_ok=True)
    print("[train] training 2-layer RGCN on the full heterogeneous graph...")
    metrics = train_rgcn(ds.graph, str(out_dir / "gnn_rgcn.pt"), seed=seed, epochs=epochs)
    print(
        "[train] GNN val: auc={auc:.4f} precision={precision:.4f} "
        "recall={recall:.4f} ({save_format})".format(**metrics)
    )
    return metrics


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Train GNN + XGBoost into ml/artifacts/")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--users", type=int, default=2000, help="honest users to synthesise")
    parser.add_argument("--out", type=str, default=None, help="artifacts dir (env ARTIFACTS_DIR)")
    parser.add_argument("--xgb-rounds", type=int, default=400)
    parser.add_argument("--gnn-epochs", type=int, default=120)
    parser.add_argument("--val-frac", type=float, default=0.2)
    args = parser.parse_args(argv)

    out_dir = Path(
        args.out
        or os.environ.get("ARTIFACTS_DIR")
        or (Path(__file__).resolve().parents[1] / "artifacts")
    )

    print(f"[train] generating synthetic dataset (seed={args.seed}, users={args.users})...")
    ds = generate(seed=args.seed, n_users=args.users)
    summary = ds.summary()
    print(f"[train] dataset: {summary}")
    if not 0.05 <= ds.fraud_rate <= 0.15:
        print(
            f"[train] WARNING: fraud rate {ds.fraud_rate:.3f} outside the 5-15% target band"
        )

    print("[train] fitting XGBoost (early stopping, scale_pos_weight)...")
    xgb_metrics = train_xgb(
        ds, out_dir, seed=args.seed, rounds=args.xgb_rounds, val_frac=args.val_frac
    )
    print(
        "[train] XGB val: auc={auc:.4f} precision={precision:.4f} "
        "recall={recall:.4f} (best_iteration={best_iteration})".format(**xgb_metrics)
    )

    gnn_metrics = train_gnn(ds, out_dir, seed=args.seed, epochs=args.gnn_epochs)

    metrics = {
        "dataset": summary,
        "seed": args.seed,
        "xgb": xgb_metrics,
        "gnn": gnn_metrics,
        "artifacts_dir": str(out_dir),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "metrics.json").write_text(json.dumps(metrics, indent=2, sort_keys=True))
    print(f"[train] wrote {out_dir / 'xgb.json'}, features.json, metrics.json")
    print(f"[train] val AUC: {xgb_metrics['auc']:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
