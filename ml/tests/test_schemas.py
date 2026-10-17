"""Wire-contract tests: Python schemas must mirror `types.rs` exactly."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from ml.common.schemas import (
    Alert,
    Decision,
    DecisionAction,
    DeviceInfo,
    ExplainRequest,
    ExplainResponse,
    FeatureVector,
    Geo,
    GraphEdge,
    GraphNode,
    LatencyBreakdown,
    ScoreFactor,
    ScoreRequest,
    ScoreResponse,
    Subgraph,
    TransactionEvent,
    VelocityFeatures,
)
from ml.tests.builders import make_decision, make_features


def sample_event() -> dict:
    return {
        "tx_id": "3f6b2f2e-1f3a-4c7d-9a1b-0f2e4d6c8a90",
        "timestamp_ms": 1_735_700_000_000,
        "src_vpa": "user05@upidemo",
        "dst_vpa": "mule01@muledemo",
        "amount_inr": 48_000.0,
        "device": {
            "device_hash": "dev-user05",
            "app_version": "2.14.0",
            "rooted": False,
            "screen_mirror": False,
            "emulator": False,
        },
        "src_ip": "10.1.0.5",
        "geo": {"lat": 19.076, "lon": 72.8777, "city": "Mumbai"},
        "merchant_id": None,
        "note": "rent",
    }


def test_transaction_event_round_trip_snake_case():
    payload = sample_event()
    ev = TransactionEvent.model_validate(payload)
    dumped = json.loads(ev.model_dump_json())
    assert dumped == payload  # every field name + value survives intact
    assert TransactionEvent.model_validate(dumped) == ev


def test_transaction_event_field_names_match_rust():
    assert set(TransactionEvent.model_fields) == {
        "tx_id", "timestamp_ms", "src_vpa", "dst_vpa", "amount_inr",
        "device", "src_ip", "geo", "merchant_id", "note",
    }
    assert set(DeviceInfo.model_fields) == {
        "device_hash", "app_version", "rooted", "screen_mirror", "emulator",
    }
    assert set(Geo.model_fields) == {"lat", "lon", "city"}


def test_extra_fields_are_rejected():
    payload = sample_event()
    payload["upi_pin"] = "1234"  # must never be accepted (RBI/CERT-In)
    with pytest.raises(ValidationError):
        TransactionEvent.model_validate(payload)


def test_rust_optionals_default_when_omitted():
    payload = sample_event()
    del payload["note"]
    del payload["merchant_id"]
    del payload["geo"]["city"]
    payload["device"].pop("rooted")
    ev = TransactionEvent.model_validate(payload)
    assert ev.note is None
    assert ev.merchant_id is None
    assert ev.geo.city is None
    assert ev.device.rooted is False


def test_feature_vector_round_trip():
    fv = make_features()
    dumped = json.loads(fv.model_dump_json())
    assert set(dumped) == {
        "tx_id", "velocity", "dst_account_age_hours", "device_age_hours",
        "geo_jump_kmh", "amount_zscore", "ip_risk_score", "subgraph",
    }
    assert FeatureVector.model_validate(dumped) == fv
    assert set(dumped["velocity"]) == {
        "src_count_1h", "src_amount_1h", "src_count_24h", "src_amount_24h",
        "dst_count_1h", "dst_distinct_senders_1h", "device_count_1h", "ip_count_1h",
    }
    assert set(dumped["subgraph"]["nodes"][0]) == {"id", "label", "props"}
    assert set(dumped["subgraph"]["edges"][0]) == {
        "src", "dst", "rel", "amount", "timestamp_ms",
    }


def test_subgraph_and_velocity_defaults():
    sub = Subgraph.model_validate({})
    assert sub.nodes == [] and sub.edges == []
    node = GraphNode.model_validate({"id": "Account:x", "label": "Account"})
    assert node.props == {}
    edge = GraphEdge.model_validate({"src": "a", "dst": "b", "rel": "TRANSFERRED"})
    assert edge.amount is None and edge.timestamp_ms is None
    assert VelocityFeatures.model_validate(
        {
            "src_count_1h": 0, "src_amount_1h": 0.0, "src_count_24h": 0,
            "src_amount_24h": 0.0, "dst_count_1h": 0, "dst_distinct_senders_1h": 0,
            "device_count_1h": 0, "ip_count_1h": 0,
        }
    ).src_count_1h == 0


def test_decision_round_trip_uppercase_action():
    decision = make_decision(score=0.85)
    dumped = json.loads(decision.model_dump_json())
    assert dumped["action"] == "BLOCK"  # serde UPPERCASE enum
    assert set(dumped) == {
        "tx_id", "timestamp_ms", "score", "action", "reasons", "top_factors",
        "model_version", "latency",
    }
    assert Decision.model_validate(dumped) == decision
    assert set(dumped["latency"]) == {
        "ingest_ms", "feature_ms", "inference_ms", "policy_ms", "total_ms",
    }
    assert DecisionAction("CHALLENGE") is DecisionAction.CHALLENGE
    with pytest.raises(ValueError):
        DecisionAction("DROP")


def test_score_and_explain_contracts():
    fv = make_features()
    req = ScoreRequest(tx_id="t1", features=fv)
    dumped = json.loads(req.model_dump_json())
    assert set(dumped) == {"tx_id", "features"}
    resp = ScoreResponse(
        tx_id="t1", gnn_score=0.5, tabular_score=0.5, blended_score=0.5,
        model_version="heuristic-v0",
        top_factors=[ScoreFactor(name="ip.risk_score", weight=0.1)],
    )
    assert json.loads(resp.model_dump_json())["top_factors"][0]["name"] == "ip.risk_score"

    er = ExplainRequest(tx_id="t1", decision=make_decision(), features=fv)
    assert set(json.loads(er.model_dump_json())) == {"tx_id", "decision", "features"}
    ex = json.loads(
        ExplainResponse(
            tx_id="t1", attributions=[], brief="b", engine="heuristic+template"
        ).model_dump_json()
    )
    assert set(ex) == {"tx_id", "attributions", "brief", "engine"}


def test_alert_field_names_match_rust():
    alert = Alert(
        tx_id="t1", timestamp_ms=1, score=0.9, action=DecisionAction.BLOCK,
        reasons=["MULE_PATH_2HOP"], src_vpa="a@u", dst_vpa="b@u",
        amount_inr=100.0, model_version="heuristic-v0",
    )
    assert set(json.loads(alert.model_dump_json())) == {
        "tx_id", "timestamp_ms", "score", "action", "reasons", "src_vpa",
        "dst_vpa", "amount_inr", "model_version",
    }
