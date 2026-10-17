# Next-Generation Real-Time UPI Fraud Detection Solution
## Enterprise System Architecture & Technical Blueprint

---

## 1. Executive Summary & Operational Targets

Building a production-grade, real-time UPI fraud detection solution requires balancing ultra-low latency transaction decisioning with deep multi-hop graph analytical capabilities. Modern fraud attacks leverage rapid money mule networks, automated script attacks, and social engineering that traditional tabular models fail to detect in real-time.

### Core Service Level Objectives (SLOs)
* **Total End-to-End Latency**: `< 100 ms` (Strict Budget: Ingestion 10ms | Graph Extraction 20ms | GNN Model Inference 35ms | Policy & XAI 15ms | Buffer 20ms).
* **Peak Throughput**: `25,000+ transactions per second (TPS)` at peak processing loads.
* **Precision**: `> 92%` at an operational decision threshold of `0.80` (minimizing SOC false positive alert fatigue).
* **Recall & Coverage**: `> 82%` fraud capture across active money mule rings and phishing vectors.
* **Availability**: `99.999%` (Five Nines) active-active deployment within sovereign cloud datacenters in India.

---

## 2. Technology Stack Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                      INVESTIGATOR PORTAL & SOC DASHBOARD                 │
│        Next.js 15 / React 19 • Tailwind CSS • Cytoscape.js / Deck.gl     │
└────────────────────────────────────┬─────────────────────────────────────┘
                                     │ (WebSockets / REST API)
┌────────────────────────────────────▼─────────────────────────────────────┐
│                       STREAM INGESTION & GATEWAY                         │
│             Redpanda / Apache Kafka • Rust Ingestion Workers             │
└────────────────────────────────────┬─────────────────────────────────────┘
                                     │
         ┌───────────────────────────┴───────────────────────────┐
         │                                                       │
┌────────▼────────────────────────────────┐  ┌───────────────────▼─────────┐
│        GRAPH & FEATURE STORE            │  │      STREAM PROCESSING        │
│   Memgraph DB / Neo4j Enterprise (Graph)│  │     Apache Flink / Rust       │
│      Dragonfly / Redis Enterprise       │  │ (Dynamic Velocity Computation)│
└────────┬────────────────────────────────┘  └───────────────────┬─────────┘
         │                                                       │
         └───────────────────────────┬───────────────────────────┘
                                     │
┌────────────────────────────────────▼─────────────────────────────────────┐
│                    HYBRID GNN + TABULAR INFERENCE                        │
│ PyTorch Geometric (PyG) + Triton Inference Server • XGBoost on NVIDIA CUDA│
└────────────────────────────────────┬─────────────────────────────────────┘
                                     │
┌────────────────────────────────────▼─────────────────────────────────────┐
│                   EXPLAINABILITY & REGULATORY AGENTS                     │
│    GNNExplainer Engine • Local vLLM (Qwen2.5-7B / Gemma 2 Air-Gapped)    │
└──────────────────────────────────────────────────────────────────────────┘
```

### Technical Stack Component Specification

| System Layer | Selected Technology | Alternative Choice | Architectural Justification |
| :--- | :--- | :--- | :--- |
| **Stream Ingestion** | **Redpanda** (C++) / Kafka | Apache Pulsar | Zero JVM garbage collection pauses. Handles over 100k TPS per broker with single-digit millisecond latency. |
| **Gateway Microservice** | **Rust (Actix-web)** | Go / FastAPI | Zero-cost abstractions, memory safety, and thread concurrency for parsing incoming UPI ISO20022 / JSON payloads. |
| **In-Memory Feature Store** | **Dragonfly** / **Redis Enterprise** | Feast / Hopsworks | In-memory key-value store providing sub-5ms device history, transaction velocity counters, and behavioral telemetry lookups. |
| **Graph Database** | **Memgraph DB** (In-Memory C++) | Neo4j Enterprise | C++ in-memory graph engine capable of executing 2-hop neighbor subgraph extractions in sub-10ms. |
| **Stream Analytics** | **Apache Flink** | RisingWave | Stateful continuous window aggregations (e.g., 5-minute transaction frequency, spatial location jumps). |
| **GNN Framework** | **PyTorch Geometric (PyG)** | Deep Graph Library (DGL) | Dynamic heterogeneous graph embedding with `RGCN` and `GraphSAGE` layers. |
| **Model Serving Engine** | **NVIDIA Triton Inference Server** | TensorRT / vLLM | Multi-model pipeline orchestration (GNN + XGBoost ensemble) running GPU micro-batches on Tensor Cores. |
| **Tabular Auxiliary Model** | **XGBoost** (CUDA accelerated) | LightGBM | High-speed tabular feature scoring combined with GNN topological risk score for ensemble stability. |
| **XAI & SOC Agent** | **vLLM** + **Local Qwen-2.5-7B / Gemma-2** | Ollama | On-premise air-gapped LLM generating natural language fraud briefs from SHAP & GNNExplainer masks without PII leakage. |
| **Investigator Portal** | **Next.js 15 (React 19)** | Deck.gl / Canvas API | Interactive 2D/3D graph visualization with real-time WebSocket alert streaming for security analysts. |

---

## 3. Core Operational Pipeline & Dataflow

```
1. Payment Event Trigger (VPA, Device Fingerprint, Amount, IP, Geolocation, Timestamp)
   │
   ├──► [Rust Gateway Worker] ──► Push to Redpanda Streaming Topic
   │
2. Parallel Feature & Subgraph Assembly (Target SLA: < 20ms)
   ├──► Query Dragonfly/Redis for 1h/24h velocity metrics & device profile
   └──► Extract 2-hop neighborhood subgraph from Memgraph (Accounts, Shared Devices, Shared IPs)
   │
3. Multi-Model Inference Pipeline (Target SLA: < 35ms)
   ├──► Construct PyG Subgraph Tensor Batch
   ├──► Triton Inference Server Execution
   │     ├── Model A: RGCN (Entity-Relational Topological Score)
   │     └── Model B: XGBoost (Individual Behavioral Anomaly Score)
   └──► Blended Fraud Risk Score Output (e.g., 0.88)
   │
4. Decision & Interception Layer (Target SLA: < 15ms)
   ├──► Risk Score > 0.80: Block Settlement + Trigger 24h Account Cooldown
   ├──► Risk Score 0.50 - 0.80: Challenge User (Step-up Biometric / Video OTP)
   └──► Risk Score < 0.50: Authorize Instantly
   │
5. Asynchronous Audit & XAI Generation (< 90ms Async)
   ├──► Run GNNExplainer on Flagged Subgraph
   └──► Local vLLM Agent synthesizes SOC Summary Brief -> Streamed to Investigator Portal
```

---

## 4. Graph Schema Design (Memgraph / Neo4j)

To model money laundering rings and account takeovers, the schema maintains heterogeneous nodes and relationships:

```cypher
// Node Definitions
CREATE CONSTRAINT ON (a:Account) ASSERT a.vpa_id IS UNIQUE;
CREATE CONSTRAINT ON (d:Device) ASSERT d.device_hash IS UNIQUE;
CREATE CONSTRAINT ON (i:IPAddress) ASSERT i.ip_address IS UNIQUE;
CREATE CONSTRAINT ON (m:Merchant) ASSERT m.merchant_id IS UNIQUE;

// Relationship Schema:
// (:Account)-[:TRANSFERRED {amount: FLOAT, timestamp: INT, latency_ms: INT}]->(:Account)
// (:Account)-[:USED_DEVICE {last_active: INT}]->(:Device)
// (:Account)-[:CONNECTED_FROM {timestamp: INT}]->(:IPAddress)
// (:Account)-[:PAID_MERCHANT {amount: FLOAT, timestamp: INT}]->(:Merchant)
```

### Real-Time Mule Detection Cypher Query
```cypher
// 2-Hop Layered Money Mule Path Extraction
MATCH (src:Account {vpa_id: $incoming_vpa})-[t1:TRANSFERRED]->(mule:Account)-[t2:TRANSFERRED]->(dest:Account)
WHERE t1.timestamp >= $window_start 
  AND t2.timestamp >= t1.timestamp 
  AND t2.timestamp - t1.timestamp <= 300 // Transferred within 5 minutes
RETURN src.vpa_id, mule.vpa_id, dest.vpa_id, t1.amount, t2.amount;
```

---

## 5. Security, Regulatory Compliance & Data Governance

### NPCI & RBI Regulatory Alignment
1. **Data Sovereignty & Localization**: Infrastructure deployed strictly within Indian Sovereign Cloud Datacenters (AWS `ap-south-1` Mumbai / Azure Central India / Yotta NM1) in compliance with CERT-In mandates.
2. **Cryptographic Protection**:
   * **AES-256 GCM** encryption for all sensitive customer fields (MSISDN, Account Numbers).
   * **NPCI Common Library (CL) Enclave**: UPI PIN processing occurs strictly within HSM / isolated memory enclaves. PINs are **never** logged or made accessible as raw model features.
3. **Hardware Anti-Emulation & Endpoint Protection**: Endpoint Mobile SDK enforcing real-time detection of root access, screen mirroring tools (AnyDesk, TeamViewer), and Android emulator runtimes.
4. **URCS Automated Integration**: Direct integration with NPCI Unified Rupee Clearing and Settlement (URCS) system for automated chargeback dispute filing under Circular OC-235 rules.

---

## 6. Deployment & Implementation Roadmap

```
┌────────────────────────────────────────────────────────────────────────────┐
│ Phase 1: Ingestion & Feature Store Pipeline (Weeks 1 - 6)                  │
│ • Deploy Redpanda, Dragonfly, and Apache Flink stream pipelines.            │
│ • Build real-time feature extraction for baseline velocity rules.          │
├────────────────────────────────────────────────────────────────────────────┤
│ Phase 2: In-Memory Graph Engine & GNN Training (Weeks 7 - 14)              │
│ • Deploy Memgraph cluster with real-time edge sync.                         │
│ • Train PyG RGCN and GraphSAGE models on historical UPI fraud datasets.   │
├────────────────────────────────────────────────────────────────────────────┤
│ Phase 3: Triton Model Serving & Sub-100ms SLA Tuning (Weeks 15 - 20)       │
│ • Deploy Triton Inference Server on NVIDIA A10G/L4 GPUs.                   │
│ • Perform end-to-end load testing to achieve sub-100ms SLAs at 25,000 TPS. │
├────────────────────────────────────────────────────────────────────────────┤
│ Phase 4: XAI Engine, Local LLM & SOC Dashboard Launch (Weeks 21 - 26)      │
│ • Integrate GNNExplainer and local vLLM for automated investigator briefs.  │
│ • Launch Next.js / Cytoscape SOC Dashboard for compliance teams.           │
└────────────────────────────────────────────────────────────────────────────┘
```