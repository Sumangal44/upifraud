"""FastAPI app for hybrid inference (port `INFERENCE_PORT`, default 8001).

Deterministic by design: `/v1/score` for the same `ScoreRequest` always
returns the same `ScoreResponse` (heuristic path is pure; XGBoost CPU
prediction is deterministic; the RGCN has no dropout at eval time).

Run: `PYTHONPATH=. python -m ml.inference.app`
"""

from __future__ import annotations

from typing import Optional

from fastapi import FastAPI

from ml.common.schemas import ScoreRequest, ScoreResponse
from ml.common.scoring import blend, top_factors
from ml.inference.app.model_loader import LoadedModels, load_models


def create_app(loaded: Optional[LoadedModels] = None) -> FastAPI:
    """Build the app; `loaded` may be injected (tests) — otherwise artifacts
    are loaded once at import time (env `ARTIFACTS_DIR`, default `ml/artifacts`)."""
    models = loaded if loaded is not None else load_models()

    app = FastAPI(title="upifraud-inference", version="1.0.0")
    app.state.models = models

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/v1/models")
    def v1_models() -> dict:
        return app.state.models.describe()

    @app.post("/v1/score", response_model=ScoreResponse)
    def v1_score(req: ScoreRequest) -> ScoreResponse:
        m: LoadedModels = app.state.models
        gnn_score, gnn_factors = m.gnn.score(req.features)
        tabular_score, tabular_factors = m.tabular.score(req.features)
        blended = blend(gnn_score, tabular_score)
        factors = top_factors(list(gnn_factors) + list(tabular_factors), 8)
        return ScoreResponse(
            tx_id=req.tx_id,
            gnn_score=gnn_score,
            tabular_score=tabular_score,
            blended_score=blended,
            model_version=m.model_version,
            top_factors=factors,
        )

    return app


#: Module-level app for `python -m ml.inference.app` / uvicorn.
app = create_app()
