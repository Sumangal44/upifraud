"""Inference service tests: heuristic path via FastAPI TestClient."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ml.common.scoring import blend, heuristic_scores, top_factors
from ml.inference.app.main import create_app
from ml.inference.app.model_loader import LoadedModels
from ml.tests.builders import make_features, make_velocity, mule_subgraph


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    for var in ("GNN_SCORE_WEIGHT", "TABULAR_SCORE_WEIGHT"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def client() -> TestClient:
    """Heuristic-only app (never depends on local artifacts)."""
    return TestClient(create_app(LoadedModels.heuristic()))


def _score_payload() -> dict:
    return {
        "tx_id": "score-tx-42",
        "features": make_features(
            velocity=make_velocity(src_count_1h=6, dst_distinct_senders_1h=4),
            subgraph=mule_subgraph(),
            ip_risk_score=0.9,
        ).model_dump(mode="json"),
    }


def test_healthz(client: TestClient):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_v1_models_reports_heuristic(client: TestClient):
    resp = client.get("/v1/models")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["model_version"] == "heuristic-v0"
    assert body["scorers"]["gnn"]["source"] == "heuristic"
    assert body["scorers"]["tabular"]["source"] == "heuristic"


def test_score_heuristic_path(client: TestClient):
    resp = client.post("/v1/score", json=_score_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert body["tx_id"] == "score-tx-42"
    assert body["model_version"] == "heuristic-v0"
    # blended == 0.6 * gnn + 0.4 * tabular (default policy weights)
    expected = 0.6 * body["gnn_score"] + 0.4 * body["tabular_score"]
    assert abs(body["blended_score"] - expected) < 1e-9
    # components come straight from the shared heuristic scorer
    fv = make_features(
        velocity=make_velocity(src_count_1h=6, dst_distinct_senders_1h=4),
        subgraph=mule_subgraph(),
        ip_risk_score=0.9,
    )
    gnn, tabular, factors = heuristic_scores(fv)
    assert abs(body["gnn_score"] - gnn) < 1e-9
    assert abs(body["tabular_score"] - tabular) < 1e-9
    assert body["top_factors"] == [
        f.model_dump(mode="json") for f in top_factors(factors, 8)
    ]


def test_score_is_deterministic(client: TestClient):
    payload = _score_payload()
    first = client.post("/v1/score", json=payload).json()
    second = client.post("/v1/score", json=payload).json()
    assert first == second


def test_top_factors_sorted_by_abs_weight(client: TestClient):
    body = client.post("/v1/score", json=_score_payload()).json()
    weights = [f["weight"] for f in body["top_factors"]]
    assert len(weights) <= 8
    abs_weights = [abs(w) for w in weights]
    assert abs_weights == sorted(abs_weights, reverse=True)


def test_mule_path_scores_above_honest(client: TestClient):
    mule = client.post("/v1/score", json=_score_payload()).json()
    honest_payload = {"tx_id": "honest", "features": make_features().model_dump(mode="json")}
    honest = client.post("/v1/score", json=honest_payload).json()
    assert mule["blended_score"] > honest["blended_score"]


def test_invalid_payload_rejected(client: TestClient):
    resp = client.post("/v1/score", json={"tx_id": "x"})  # missing features
    assert resp.status_code == 422


def test_default_module_app_is_importable():
    from ml.inference.app import main as main_module

    assert main_module.app is not None
    assert callable(main_module.create_app)
    # module-level app exposes the same contract regardless of artifacts
    with TestClient(main_module.app) as c:
        assert c.get("/healthz").json() == {"status": "ok"}
        body = c.get("/v1/models").json()
        assert body["scorers"]["gnn"]["version"] in ("heuristic-v0", "rgcn-v1")
        assert blend(1.0, 0.0) == pytest.approx(0.6)
