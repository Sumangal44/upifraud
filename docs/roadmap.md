# Roadmap & backlog

Status: ✅ done · 🚧 in progress · ⬜ not started. Maps to the phases in
`next_generation_real_time_upi_fraud_detection_solution_architecture.md`.

## Phase 1 — Ingestion & feature store pipeline ✅
- ✅ Redpanda streaming backbone + Console (`docker-compose.yml`)
- ✅ Rust ingestion gateway: `POST /v1/transactions[/batch]`, validation,
  keyed produce (`services/gateway`)
- ✅ Real-time velocity counters in Dragonfly (1h/24h count+amount, dst
  fan-in, distinct senders, device/IP counters) (`services/feature-worker/src/velocity.rs`)
- ✅ Demo seeder + synthetic traffic generator (`tools/seed`, `tools/loadgen`)
- ⬜ ISO 20022 / NPCI payload adapters in the gateway (currently JSON)
- 🚧 Flink SQL velocity job for scale-out (`infra/flink/velocity_job.sql` —
  written, not yet deployed; inline Rust counters cover the MVP)

## Phase 2 — Graph engine & GNN training ✅
- ✅ Memgraph schema + reference Cypher (mule paths, fan-in/out, shared devices)
  (`infra/memgraph`)
- ✅ 2-hop heterogeneous subgraph extraction in the hot path (`subgraph.rs`)
- ✅ Synthetic fraud-graph dataset generator + XGBoost training
  (`make train` → val AUC reported in `ml/artifacts/metrics.json`)
- ⬜ Train on historical/real UPI fraud datasets (procurement + labeling plan)
- 🚧 RGCN/GraphSAGE training (`ml/training/gnn.py` — implemented, requires
  `pip install torch torch-geometric`; skipped automatically when absent)

## Phase 3 — Triton serving & sub-100 ms SLA tuning ⬜
- ⬜ GPU load-testing harness targeting 25 k TPS with p99 < 100 ms; per-stage
  latency already recorded in every `Decision.latency`
- ⬜ Wire Triton (`--profile gpu`, model repo scaffolded under
  `ml/triton/model_repository`) and switch inference service from direct
  torch/xgboost loading to Triton client
- ⬜ Consumer-group story for horizontally scaled feature-workers
  (see ADR-0001 consequences)
- ⬜ Observability stack: Prometheus exporters for gateway/worker, RED
  dashboards, alerting on decision-latency SLO burn

## Phase 4 — XAI, local LLM & SOC dashboard ✅
- ✅ Attribution engine (deterministic heuristic; SHAP path when
  `shap`+booster present) (`ml/xai`)
- ✅ LLM SOC briefs: vLLM-compatible client (`VLLM_BASE_URL`) with
  air-gapped template fallback, PII guard (`ml/xai/app/briefs.py`)
- ✅ Next.js 15 / React 19 portal: live WS alert feed, decision table,
  2-hop graph view (Cytoscape), explanation panel (`apps/portal`)
- ⬜ GNNExplainer proper (needs torch; today attributions come from the
  heuristic/SHAP paths)
- ⬜ Case-management workflow (assign/annotate/close) + URCS chargeback
  integration (Phase 4 in the design doc)

## Cross-cutting (any phase)
- ⬜ AuthN/Z on gateway + portal (currently open — internal network assumed)
- ⬜ AES-256-GCM field-level encryption service for MSISDN/account numbers
- ⬜ Chaos tests: Redpanda failover, Dragonfly restart mid-flight
- ⬜ e2e smoke in CI: compose up → seed → loadgen → assert BLOCK alert arrives
  on the portal WebSocket
