"""Triton Python backend for the RGCN (`gnn_rgcn`, GPU profile).

Serves the artefact written by `python -m ml.training.train`
(`ml/artifacts/gnn_rgcn.pt`) with the exact model contract used by the
FastAPI inference service:

    logits = model(node_features, edge_index, edge_type)   # (N, 1)

Inputs  : `node_features` [N,10] FP32, `edge_index` [2,E] INT64,
          `edge_type` [E] INT64 (forward relations + reverse offset).
Output  : `gnn_score` [N] FP32 (sigmoid per node).

This module is only imported by Triton (which provides
`triton_python_backend_utils`); outside Triton it degrades to a documented
stub so the repository stays importable for lint/tests.
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
        self._torch = None
        self._model = None
        try:
            import torch

            self._torch = torch
            path = os.path.join(artifacts, "gnn_rgcn.pt")
            if os.path.exists(path):
                try:
                    self._model = torch.jit.load(path, map_location="cpu")
                except Exception:
                    self._model = torch.load(path, map_location="cpu", weights_only=False)
                if hasattr(self._model, "eval"):
                    self._model.eval()
        except Exception as exc:  # torch missing / artefact absent
            self._load_error = str(exc)
            return
        if self._model is None:
            self._load_error = f"no gnn_rgcn.pt under {artifacts}"

    def execute(self, requests: list[Any]) -> list[Any]:
        """Run the RGCN per request; without torch/artifact return zeros."""
        import numpy as np

        responses = []
        for request in requests:
            node_features = pb_utils.get_input_tensor_by_name(
                request, "node_features"
            ).as_numpy()
            edge_index = pb_utils.get_input_tensor_by_name(request, "edge_index").as_numpy()
            edge_type = pb_utils.get_input_tensor_by_name(request, "edge_type").as_numpy()
            n_nodes = int(node_features.shape[0])

            if self._model is None or self._torch is None:
                # Documented fallback: neutral scores (heuristic lives in the
                # FastAPI service — Triton is the GPU-profile path).
                scores = np.zeros((n_nodes,), dtype=np.float32)
            else:
                t = self._torch
                with t.no_grad():
                    logits = self._model(
                        t.from_numpy(np.ascontiguousarray(node_features)),
                        t.from_numpy(np.ascontiguousarray(edge_index, dtype=np.int64)),
                        t.from_numpy(np.ascontiguousarray(edge_type, dtype=np.int64)),
                    )
                    scores = t.sigmoid(logits.reshape(-1)).cpu().numpy().astype(np.float32)

            out = pb_utils.InferenceResponse(
                output_tensors=[pb_utils.Tensor("gnn_score", scores)]
            )
            responses.append(out)
        return responses

    def finalize(self) -> None:
        self._model = None
        self._torch = None
