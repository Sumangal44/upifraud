"""FastAPI app for explainability (port `XAI_PORT`, default 8002).

`POST /v1/explain`:

* attributions — SHAP when `xgboost`+`shap`+artifacts are available,
  otherwise the deterministic heuristic attribution (signed weights sum to
  the decision score);
* brief — local vLLM when `VLLM_BASE_URL` is set (strict air-gapped system
  prompt, <=120 words, PII-guarded), else a deterministic template brief;
* engine — `gnnexplainer+qwen2.5-7b` | `heuristic+llm` | `heuristic+template`.

Run: `PYTHONPATH=. python -m ml.xai.app`
"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import FastAPI

from ml.common.schemas import ExplainRequest, ExplainResponse
from ml.xai.app.attributions import heuristic_attributions, load_shap_explainer
from ml.xai.app.briefs import build_engine, llm_brief, template_brief


def create_app(explainer=None) -> FastAPI:
    """Build the app; `explainer` may be injected (tests) — otherwise the
    optional SHAP explainer is loaded once at import (graceful if unavailable)."""
    app = FastAPI(title="upifraud-xai", version="1.0.0")
    app.state.explainer = explainer

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    @app.post("/v1/explain", response_model=ExplainResponse)
    async def v1_explain(req: ExplainRequest) -> ExplainResponse:
        explainer = app.state.explainer
        attributions_from_model = False
        attributions = None
        if explainer is not None:
            attributions = explainer.explain(req.features, req.decision.score)
            attributions_from_model = attributions is not None
        if attributions is None:
            attributions = heuristic_attributions(req.features, req.decision.score)

        brief = None
        if _llm_enabled():
            model = _llm_model()
            brief = await llm_brief(
                req, attributions, base_url=_llm_base_url(), model=model
            )
            engine = build_engine(attributions_from_model, brief is not None, model)
            if brief is None:
                brief = template_brief(req, attributions)
        else:
            engine = build_engine(attributions_from_model, False)
            brief = template_brief(req, attributions)

        return ExplainResponse(
            tx_id=req.tx_id,
            attributions=attributions,
            brief=brief,
            engine=engine,
        )

    return app


def _llm_base_url() -> str:
    return os.environ.get("VLLM_BASE_URL", "") or ""


def _llm_model() -> str:
    return os.environ.get("VLLM_MODEL", "Qwen/Qwen2.5-7B-Instruct")


def _llm_enabled() -> bool:
    return bool(_llm_base_url().strip())


#: Module-level app for `python -m ml.xai.app` / uvicorn.
app = create_app(explainer=load_shap_explainer())
