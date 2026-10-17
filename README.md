# UPI Fraud Detection Platform

Real-time UPI fraud detection: sub-100 ms risk decisioning over a hybrid
**GNN (topological) + XGBoost (behavioral)** ensemble, with in-memory graph
features, streaming velocity computation, and an air-gapped XAI/SOC agent.

Full architecture and design rationale:
[`next_generation_real_time_upi_fraud_detection_solution_architecture.md`](./next_generation_real_time_upi_fraud_detection_solution_architecture.md)

---

## Quickstart

```bash
cp .env.example .env
make up          # build images + start Redpanda, Dragonfly, Memgraph, services, portal
make seed        # load demo entities + a money-mule ring into the graph
make loadgen     # synthetic traffic incl. fraud patterns (Ctrl-C to stop)
open http://localhost:3000   # SOC dashboard — live alerts, decisions, graph view
```

One-shot verification of the whole pipeline (build → up → seed → fraud
scenario incl. WebSocket alert → assertions):

```bash
make e2e         # or: bash scripts/e2e.sh --down
```

Without Docker, the same stack runs natively (see [Local development](#local-development)).

## How a transaction flows

```
POST /v1/transactions ─► gateway ─► Redpanda (upi.tx.raw)
                                        │
                                 feature-worker
                     ┌──────────────────┼──────────────────┐
               Dragonfly           Memgraph           inference:8001
          (velocity counters)   (2-hop subgraph)   (RGCN + XGBoost blend)
                     └──────────────────┼──────────────────┘
                                        ▼
                            policy (0.80 / 0.50)
                    ┌───────────────────┼───────────────────┐
              ALLOW (0–0.5)      CHALLENGE (0.5–0.8)    BLOCK (0.8–1.0)
                    │            step-up biometric        block + cooldown
                    └──────────► Redpanda (upi.decisions, upi.alerts)
                                        │
                          stream-api:8081 ─► portal:3000 (WS live feed)
                                        │
                              xai:8002 (GNNExplainer + local LLM brief)
```

## Repository layout

| Path | What it is |
| :--- | :--- |
| `crates/upifraud-core` | **Wire contract**: event/feature/decision types, topics, risk policy (single source of truth) |
| `services/gateway` | Actix-web ingestion API → Redpanda |
| `services/feature-worker` | Consumes events, computes velocity + subgraph features, scores, applies policy |
| `services/stream-api` | Decision history REST + WebSocket alert stream for the portal |
| `ml/inference` | FastAPI scoring service (PyG RGCN + XGBoost when trained, deterministic heuristic fallback) |
| `ml/xai` | GNNExplainer/SHAP attributions + LLM SOC briefs (templated when no vLLM) |
| `ml/training` | Synthetic fraud-graph dataset generator + RGCN/GraphSAGE/XGBoost training |
| `ml/triton` | Triton model repository layout for GPU serving |
| `apps/portal` | Next.js 15 / React 19 SOC dashboard (Cytoscape.js graph view, live WS alerts) |
| `tools/seed` | Idempotent demo-data seeder (graph + feature store) |
| `tools/loadgen` | Synthetic traffic generator (normal + fraud patterns) |
| `infra/` | Memgraph schema, reference Cypher queries, Flink velocity job (production path) |
| `docs/` | Architecture decisions (ADR), API notes, roadmap |

## Services & ports

| Service | Port | Endpoint(s) |
| :--- | :--- | :--- |
| gateway | `8080` | `POST /v1/transactions`, `GET /healthz` |
| stream-api | `8081` | `GET /v1/decisions`, `GET /v1/decisions/{id}`, `GET /v1/decisions/{id}/graph`, `GET /v1/decisions/{id}/explanation`, `WS /ws/alerts`, `GET /healthz` |
| inference | `8001` | `POST /v1/score`, `GET /v1/models`, `GET /healthz` |
| xai | `8002` | `POST /v1/explain`, `GET /healthz` |
| portal | `3000` | SOC dashboard |
| redpanda-console | `8086` | topic browser |
| redpanda | `19092` | Kafka API (host) |
| dragonfly | `6379` | Redis protocol |
| memgraph | `7687` | Bolt |
| triton / vllm | `8000` / `8003` | GPU profile only (`--profile gpu`) |

## Risk policy

| Blended score | Action |
| :--- | :--- |
| `≥ 0.80` | **BLOCK** settlement + 24 h account cooldown |
| `0.50 – 0.80` | **CHALLENGE** (step-up biometric / video OTP) |
| `< 0.50` | **ALLOW** instantly |

Blend: `0.6 · GNN_topological + 0.4 · XGBoost_behavioral` — configurable via
`RISK_*` / `*_WEIGHT` env vars (see `.env.example`).

The `upifraud-core` crate is the canonical policy implementation
(`crates/upifraud-core/src/policy.rs`); Python mirrors it in
`ml/common/scoring.py`.

## Local development

No Docker required — each piece runs natively:

```bash
# 1. Infrastructure (pick what you have; Docker is easiest)
docker compose up -d redpanda dragonfly memgraph

# 2. Rust services (pure-Rust clients — no cmake/librdkafka needed)
cargo run -p gateway          # :8080
cargo run -p feature-worker   # background worker
cargo run -p stream-api       # :8081

# 3. ML services
python3 -m venv .venv && . .venv/bin/activate
pip install -r ml/inference/requirements.txt -r tools/requirements.txt
PYTHONPATH=. python3 -m ml.inference.app     # :8001 (heuristic model, no GPU needed)
PYTHONPATH=. python3 -m ml.xai.app           # :8002

# 4. Portal
npm --prefix apps/portal install
npm --prefix apps/portal run dev             # :3000

# 5. Demo data + traffic
PYTHONPATH=. python3 -m tools.seed.seed
PYTHONPATH=. python3 -m tools.loadgen.loadgen --tps 20
```

**Model training** (optional, CPU works): `make train` → artifacts in
`ml/artifacts/`; the inference service auto-loads them on next start. Without
artifacts it serves a deterministic heuristic scorer so the pipeline is always
runnable.

## Testing

```bash
make test        # Rust unit tests + Python pytest + portal lint
make test-rust   # cargo test --workspace
make test-py     # pytest ml tools
make test-ts     # portal lint/type-check
```

## Security & compliance notes

- **PII minimisation**: payloads carry salted device hashes and VPAs only —
  never UPI PINs, MSISDNs or account numbers (RBI/CERT-In).
- **PIN handling** belongs exclusively in the NPCI CL enclave/HSM and is out
  of scope for this pipeline by design.
- **Data sovereignty**: deploy only in Indian regions (`ap-south-1`,
  Azure Central India, on-prem NM1) — enforced at deployment time, not in code.
- **XAI agent is air-gapped**: the LLM runs locally (vLLM); `VLLM_BASE_URL`
  empty ⇒ deterministic templated briefs (still no PII egress).

## Roadmap

Maps to the phases in the architecture doc:

1. ✅ Ingestion + feature store pipeline (gateway, Dragonfly velocities, Memgraph subgraphs)
2. ✅ Graph engine + GNN training scaffold (RGCN/GraphSAGE training on synthetic fraud graphs)
3. ⬜ Triton GPU serving + sub-100 ms SLA load-tuning (`--profile gpu`; benchmark harness pending)
4. ✅ XAI engine + local LLM agent + SOC dashboard (template briefs; vLLM wired via env)

See `docs/roadmap.md` for the detailed backlog.
