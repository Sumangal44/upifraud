"""Policy mirror tests: thresholds, blend normalisation, heuristic scorer."""

from __future__ import annotations

import pytest

from ml.common import scoring
from ml.common.schemas import DecisionAction, ScoreFactor
from ml.tests.builders import honest_subgraph, make_features, make_velocity, mule_subgraph


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    """Pin default policy weights regardless of the surrounding shell."""
    for var in (
        "GNN_SCORE_WEIGHT",
        "TABULAR_SCORE_WEIGHT",
        "RISK_BLOCK_THRESHOLD",
        "RISK_CHALLENGE_THRESHOLD",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------- thresholds


def test_decide_boundaries_match_rust_policy():
    # mirrors `thresholds_map_to_actions` in policy.rs
    assert scoring.decide(0.95) == DecisionAction.BLOCK
    assert scoring.decide(0.80) == DecisionAction.BLOCK
    assert scoring.decide(0.79) == DecisionAction.CHALLENGE
    assert scoring.decide(0.50) == DecisionAction.CHALLENGE
    assert scoring.decide(0.49) == DecisionAction.ALLOW
    assert scoring.decide(0.0) == DecisionAction.ALLOW


def test_decide_env_overrides_and_bad_values(monkeypatch):
    monkeypatch.setenv("RISK_BLOCK_THRESHOLD", "0.9")
    monkeypatch.setenv("RISK_CHALLENGE_THRESHOLD", "0.3")
    assert scoring.decide(0.91) == DecisionAction.BLOCK
    assert scoring.decide(0.35) == DecisionAction.CHALLENGE
    assert scoring.decide(0.1) == DecisionAction.ALLOW
    # unparsable values keep the default (Rust from_env behaviour)
    monkeypatch.setenv("RISK_BLOCK_THRESHOLD", "not-a-number")
    assert scoring.decide(0.80) == DecisionAction.BLOCK


# --------------------------------------------------------------------- blend


def test_blend_default_weights():
    # mirrors `blend_uses_default_weights` in policy.rs
    assert abs(scoring.blend(1.0, 0.0) - 0.6) < 1e-9
    assert abs(scoring.blend(0.5, 0.5) - 0.5) < 1e-9


def test_blend_env_weights_are_normalised(monkeypatch):
    monkeypatch.setenv("GNN_SCORE_WEIGHT", "1.5")
    monkeypatch.setenv("TABULAR_SCORE_WEIGHT", "0.5")
    # 1.5/0.5 normalises to 0.75/0.25
    assert abs(scoring.blend(1.0, 0.0) - 0.75) < 1e-9
    g, t = scoring.blend_weights()
    assert abs(g + t - 1.0) < 1e-12


def test_blend_zero_weights_fall_back_to_half_half(monkeypatch):
    monkeypatch.setenv("GNN_SCORE_WEIGHT", "0")
    monkeypatch.setenv("TABULAR_SCORE_WEIGHT", "0")
    assert abs(scoring.blend(1.0, 0.0) - 0.5) < 1e-9
    # explicit weights behave the same way (mirror blend_with)
    assert abs(scoring.blend(1.0, 0.0, gnn_weight=0.0, tabular_weight=0.0) - 0.5) < 1e-9


def test_policy_config_from_env(monkeypatch):
    monkeypatch.setenv("GNN_SCORE_WEIGHT", "0.7")
    monkeypatch.setenv("TABULAR_SCORE_WEIGHT", "0.3")
    cfg = scoring.PolicyConfig.from_env()
    assert abs(cfg.blend(1.0, 0.0) - 0.7) < 1e-9
    assert cfg.decide(0.80) == DecisionAction.BLOCK


# --------------------------------------------------------------- heuristic


def test_heuristic_is_deterministic():
    fv = make_features(
        velocity=make_velocity(src_count_1h=7, dst_distinct_senders_1h=5),
        subgraph=mule_subgraph(),
        geo_jump_kmh=1200.0,
        amount_zscore=3.1,
        ip_risk_score=0.9,
    )
    first = scoring.heuristic_scores(fv)
    second = scoring.heuristic_scores(fv)
    assert first[0] == second[0]
    assert first[1] == second[1]
    assert first[2] == second[2]
    gnn, tab, factors = first
    assert 0.0 <= gnn <= 1.0
    assert 0.0 <= tab <= 1.0


def test_more_velocity_scores_higher():
    low = make_features(velocity=make_velocity(src_count_1h=2, dst_count_1h=2))
    high = make_features(velocity=make_velocity(src_count_1h=8, dst_count_1h=8))
    gnn_low, tab_low, _ = scoring.heuristic_scores(low)
    gnn_high, tab_high, _ = scoring.heuristic_scores(high)
    assert tab_high > tab_low
    assert gnn_low == gnn_high  # graph part untouched by velocity
    blended_low = scoring.blend(gnn_low, tab_low)
    blended_high = scoring.blend(gnn_high, tab_high)
    assert blended_high > blended_low


def test_more_geo_jump_and_ip_risk_score_higher():
    calm = make_features(geo_jump_kmh=0.0, ip_risk_score=0.05)
    wild = make_features(geo_jump_kmh=3000.0, ip_risk_score=0.95)
    _, tab_calm, _ = scoring.heuristic_scores(calm)
    _, tab_wild, _ = scoring.heuristic_scores(wild)
    assert tab_wild > tab_calm


def test_fresh_destination_scores_higher_than_established():
    fresh = make_features(dst_account_age_hours=1.0, device_age_hours=1.0)
    old = make_features(dst_account_age_hours=24 * 60.0, device_age_hours=24 * 60.0)
    _, tab_fresh, _ = scoring.heuristic_scores(fresh)
    _, tab_old, _ = scoring.heuristic_scores(old)
    assert tab_fresh > tab_old


def test_mule_subgraph_riskier_than_honest():
    honest = make_features(subgraph=honest_subgraph())
    mule = make_features(subgraph=mule_subgraph())
    gnn_honest, tab_honest, _ = scoring.heuristic_scores(honest)
    gnn_mule, tab_mule, _ = scoring.heuristic_scores(mule)
    assert gnn_mule > gnn_honest
    assert scoring.blend(gnn_mule, tab_mule) > scoring.blend(gnn_honest, tab_honest)


def test_factors_sum_to_blended_score():
    fv = make_features(
        velocity=make_velocity(src_count_1h=9, dst_distinct_senders_1h=6),
        subgraph=mule_subgraph(),
        ip_risk_score=0.9,
    )
    gnn, tab, factors = scoring.heuristic_scores(fv)
    expected = scoring.blend(gnn, tab)
    assert abs(sum(f.weight for f in factors) - expected) < 1e-9


def test_graph_factors_carry_the_right_signal_names():
    _, _, factors = scoring.heuristic_scores(make_features(subgraph=mule_subgraph()))
    graph_names = {f.name for f in factors if f.name.startswith("graph.")}
    assert graph_names == {
        "graph.transferred_edges",
        "graph.distinct_accounts",
        "graph.fan_in_concentration",
        "graph.fast_pass_2hop",
        "graph.shared_device",
        "graph.ip_risk",
        "graph.watchlist_flags",
    }


def test_fast_pass_window_is_300s():
    """Two chained edges within 5 min -> fast-pass signal; outside -> none."""
    from ml.common.schemas import GraphEdge, GraphNode, Subgraph

    def chain(delta_ms: int) -> float:
        sub = Subgraph(
            nodes=[
                GraphNode(id="Account:a", label="Account", props={}),
                GraphNode(id="Account:b", label="Account", props={}),
                GraphNode(id="Account:c", label="Account", props={}),
            ],
            edges=[
                GraphEdge(src="Account:a", dst="Account:b", rel="TRANSFERRED",
                          amount=100.0, timestamp_ms=1_700_000_000_000),
                GraphEdge(src="Account:b", dst="Account:c", rel="TRANSFERRED",
                          amount=100.0, timestamp_ms=1_700_000_000_000 + delta_ms),
            ],
        )
        fv = make_features(subgraph=sub)
        gnn, _, _ = scoring.heuristic_scores(fv)
        return gnn

    inside = chain(299_000)
    outside = chain(301_000)
    assert inside > outside


def test_top_factors_sorted_and_limited():
    factors = [ScoreFactor(name=f"f{i}", weight=(-1.0) ** i * (i / 10)) for i in range(20)]
    top = scoring.top_factors(factors, limit=8)
    assert len(top) == 8
    weights = [abs(f.weight) for f in top]
    assert weights == sorted(weights, reverse=True)
    # ties broken deterministically by name
    tied = [ScoreFactor(name="b", weight=0.1), ScoreFactor(name="a", weight=0.1)]
    assert [f.name for f in scoring.top_factors(tied)] == ["a", "b"]
