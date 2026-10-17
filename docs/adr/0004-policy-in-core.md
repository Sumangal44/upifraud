# ADR-0004: Risk policy lives in upifraud-core, not in the model service

**Status**: Accepted · **Date**: 2026-10

## Context
The inference service returns model scores; the *decision* (ALLOW / CHALLENGE /
BLOCK) is a business-policy concern with regulatory weight (RBI dispute
liability flips with the action taken). If the model service owned thresholds,
tuning a model could silently change the block rate.

## Decision
`crates/upifraud-core::policy` is the single implementation of:

- **Blend**: `score = (0.6·gnn + 0.4·tabular) / weights` (env-tunable)
- **Thresholds**: `≥0.80 BLOCK`, `≥0.50 CHALLENGE`, else `ALLOW` (env-tunable)

The feature-worker *always* recomputes `blended_score` and the action locally
via `PolicyConfig::finalize_score` + `decide`, regardless of what any model
service returned. The Python mirror (`ml/common/scoring.py`) is covered by
tests that pin the same boundaries (0.80 / 0.50).

## Consequences
- ✅ One place to audit/change policy; every `Decision` records the thresholds'
  effect plus `model_version`, so scores remain interpretable.
- ✅ Inference stays a pure scoring function — safe to swap engines (Triton,
  heuristic, shadow models) without touching decisioning.
- ⚠️ Two implementations exist (Rust canonical, Python mirrored); the contract
  test in `ml/tests/test_scoring.py` guards the boundary values, but a
  drift in weights semantics is still possible — keep both in the same PR.
