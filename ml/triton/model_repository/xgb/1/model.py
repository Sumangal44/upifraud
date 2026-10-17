"""Triton Python backend for XGBoost (`xgb`, CPU).

Serves `ml/artifacts/xgb.json` (+ `features.json`) with the same contract the
FastAPI inference service uses: one canonical feature row per transaction ->
`tabular_score` = P(fraud).

Importable outside Triton (degrades to a documented stub when the
`triton_python_backend_utils` module or xgboost/artefacts are missing).
"""

from __future__ import annotations

import json
import os
from typing import Any

try:  # pragma: no cover - only present inside a Triton container
    import triton_python_backend_utils as pb_utils
except ImportError:  # pragma: no cover
    pb_utils = None

DEFAULT_ARTIFACTS = os.path.join("ml", "artifacts")


class TritonPythonModel:
    def initialize(self, args: dict[str, Any]) -> None:
        self.model_config = json.loads(args.get("model_config", "{}"))
        artifacts = os.environ.get("ARTIFACTS_DIR", DEFAULT_ARTIFACTS)
        self._booster = None
        self._xgb = None
        self._features: list[str] = []
        try:
            import xgboost as xgb

            model_path = os.path.join(artifacts, "xgb.json")
            if not os.path.exists(model_path):
                self._load_error = f"no xgb.json under {artifacts}"
                return
            booster = xgb.Booster()
            booster.load_model(model_path)
            self._xgb = xgb
            self._booster = booster
            fjson = os.path.join(artifacts, "features.json")
            if os.path.exists(fjson):
                raw = json.loads(open(fjson).read())
                if isinstance(raw, dict):
                    raw = raw.get("features", [])
                if isinstance(raw, list) and all(isinstance(x, str) for x in raw):
                    self._features = raw
            if not self._features:
                self._load_error = "features.json missing/invalid; refusing blind rows"
                self._booster = None
        except Exception as exc:
            self._load_error = str(exc)

    def execute(self, requests: list[Any]) -> list[Any]:
        import numpy as np

        responses = []
        for request in requests:
            features = pb_utils.get_input_tensor_by_name(request, "features").as_numpy()
            if self._booster is None:
                scores = np.zeros((features.shape[0], 1), dtype=np.float32)
            else:
                dmat = self._xgb.DMatrix(features, feature_names=self._features)
                pred = self._booster.predict(dmat)
                scores = pred.reshape(-1, 1).astype(np.float32)
            responses.append(
                pb_utils.InferenceResponse(
                    output_tensors=[pb_utils.Tensor("tabular_score", scores)]
                )
            )
        return responses

    def finalize(self) -> None:
        self._booster = None
