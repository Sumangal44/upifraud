# ADR-0003: Deterministic heuristic fallback scoring in the inference path

**Status**: Accepted · **Date**: 2026-10

## Context
The target architecture serves a GNN (PyG RGCN) via Triton on NVIDIA GPUs plus
CUDA XGBoost. Two problems for the MVP and for CI: GPUs are not available in
every environment, and a scoring path that *depends* on model artifacts makes
the pipeline un-runnable until training completes.

## Decision
Three-tier scoring, tried in order, all behind the same `POST /v1/score`
contract:

1. **Loaded models** — TorchScript GNN (`ml/artifacts/gnn_rgcn.pt`) and/or
   XGBoost booster (`ml/artifacts/xgb.json`), auto-detected at service start.
2. **Heuristic scorer** (`ml/common/scoring.py`) — deterministic, pure-function
   risk estimate from velocity/graph statistics. Same code is used by the
   feature-worker's in-service fallback when the inference HTTP call fails
   (`model_version: local-fallback-v0`).
3. Never fails open: worst case returns a conservative mid-range score with an
   honest `model_version` string, so SOC dashboards can see which engine
   produced every score.

## Consequences
- ✅ `docker compose up` and CI work with no GPU and no artifacts; `make train`
  upgrades the service in place (restart picks up artifacts).
- ✅ Determinism makes unit tests and incident replay reliable.
- ⚠️ The heuristic cannot see topology the graph model would; it is an MVP
  bridge, not the production decision engine. Triton serving is the Phase-3
  gate (see `docs/roadmap.md`).

## Alternatives
Fail hard without models (rejected: pipeline unusable pre-training), random
scores in dev (rejected: poisons dashboards and tests).
