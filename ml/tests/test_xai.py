"""XAI service tests: deterministic attributions, PII-free template briefs."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ml.common.schemas import GraphEdge, GraphNode, Subgraph
from ml.tests.builders import make_decision, make_features, make_velocity, mule_subgraph
from ml.xai.app.briefs import pii_guard, template_brief
from ml.xai.app.main import create_app


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    """No LLM in tests: force the template-brief path."""
    monkeypatch.delenv("VLLM_BASE_URL", raising=False)
    monkeypatch.delenv("VLLM_MODEL", raising=False)


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app(explainer=None))  # heuristic attributions


def _subgraph_with_secrets() -> Subgraph:
    """Subgraph carrying canary PII that must never reach the brief."""
    sub = mule_subgraph()
    sub.nodes.append(
        GraphNode(
            id="Account:secret@upi",
            label="Account",
            props={
                "vpa_id": "secret@upi",
                "city": "Aurangabad-Confidential",
                "note": "CALL ME AT 9999911111",
            },
        )
    )
    sub.edges.append(
        GraphEdge(
            src="Account:secret@upi",
            dst="Account:mule1@mule",
            rel="TRANSFERRED",
            amount=9_999.0,
            timestamp_ms=1_735_700_090_000,
        )
    )
    return sub


def _explain_payload(subgraph: Subgraph | None = None) -> dict:
    return {
        "tx_id": "explain-tx-7",
        "decision": make_decision(score=0.85).model_dump(mode="json"),
        "features": make_features(subgraph=subgraph or mule_subgraph()).model_dump(
            mode="json"
        ),
    }


def test_healthz(client: TestClient):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_explain_template_brief_has_no_note_text(client: TestClient):
    resp = client.post("/v1/explain", json=_explain_payload(_subgraph_with_secrets()))
    assert resp.status_code == 200
    body = resp.json()
    assert body["engine"] == "heuristic+template"
    brief = body["brief"].lower()
    assert "9999911111" not in brief  # note canary
    assert "confidential" not in brief  # geo city canary
    assert "call me" not in brief
    # still a useful, multi-sentence brief
    assert body["brief"].count(".") >= 3
    assert "explain-tx-7" in body["brief"]
    assert "blocked" in brief


def test_explain_is_deterministic(client: TestClient):
    payload = _explain_payload()
    first = client.post("/v1/explain", json=payload).json()
    second = client.post("/v1/explain", json=payload).json()
    assert first == second


def test_attributions_sorted_and_sum_to_score(client: TestClient):
    payload = _explain_payload()
    body = client.post("/v1/explain", json=payload).json()
    weights = [a["weight"] for a in body["attributions"]]
    abs_weights = [abs(w) for w in weights]
    assert abs_weights == sorted(abs_weights, reverse=True)
    # signed attributions sum to the decision's score (score delta from 0)
    assert sum(weights) == pytest.approx(payload["decision"]["score"], abs=1e-6)
    assert {a["name"] for a in body["attributions"]} >= {
        "ip.risk_score",
        "graph.fast_pass_2hop",
    }


def test_template_brief_builder_direct():
    from ml.common.schemas import DecisionAction, ExplainRequest
    from ml.xai.app.attributions import heuristic_attributions

    decision = make_decision(score=0.62, action=DecisionAction.CHALLENGE)
    features = make_features(subgraph=mule_subgraph())
    req = ExplainRequest(tx_id="t-9", decision=decision, features=features)
    attrs = heuristic_attributions(features, decision.score)
    brief = template_brief(req, attrs)
    assert "t-9" in brief
    assert "mule2@mule" in brief  # src VPA derived from latest transfer
    assert "cash@cash" in brief
    assert "47,000.00" in brief
    assert "challenged" in brief.lower()
    assert template_brief(req, attrs) == brief  # deterministic


def test_pii_guard_redacts_note_and_city():
    text = "user paid via note 'lunch with Ravi' in city Pune"
    guarded = pii_guard(text, note="lunch with Ravi", city="Pune")
    assert "lunch with Ravi" not in guarded
    assert "Pune" not in guarded
    assert "[redacted]" in guarded
    # None-safe: no secrets -> unchanged
    assert pii_guard(text) == text


def test_llm_disabled_falls_back_to_template(client: TestClient):
    # VLLM_BASE_URL is unset by the fixture -> engine must be template-based
    body = client.post("/v1/explain", json=_explain_payload()).json()
    assert body["engine"].endswith("+template")


def test_invalid_explain_payload_rejected(client: TestClient):
    resp = client.post("/v1/explain", json={"tx_id": "x"})
    assert resp.status_code == 422
