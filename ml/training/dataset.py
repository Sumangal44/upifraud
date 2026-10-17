"""Synthetic heterogeneous UPI fraud-graph generator (fully seeded/deterministic).

Produces:

* **graph dicts** — heterogeneous nodes (`Account`/`Device`/`IPAddress`/
  `Merchant`), `TRANSFERRED`/`USED_DEVICE`/`CONNECTED_FROM`/`PAID_MERCHANT`
  edges, per-node features and node labels (1 = fraud participant);
* **tabular rows** — one row per transaction with the canonical
  `TABULAR_FEATURES` semantics (velocity windows, account/device ages, geo
  jump, amount z-score, IP risk, 2-hop graph aggregates) and a 0/1 fraud
  label.

Patterns baked in (labelled fraud share ≈ 5-15%):

* 5 money-mule rings — fan-in from victims, 2-hop pass-through within
  minutes, fan-out to a cashout account (shared ring device + bad IP);
* shared-device sybil clusters;
* fresh (new-account) transfer targets;
* geo anomalies (>1000 km jump within minutes);
* ~2000 honest users with power-law amounts and sparse traffic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

from ml.common.features import TABULAR_FEATURES, graph_features
from ml.common.graph_features import build_graph_arrays
from ml.common.schemas import GraphEdge, GraphNode, Subgraph

# Fixed epoch base — keeps every run byte-identical for a given seed.
SIM_START = 1_735_689_600_000  # 2025-01-01T00:00:00Z
DAY_MS = 86_400_000
HOUR_MS = 3_600_000
MIN_MS = 60_000
SIM_DAYS = 7
SIM_END = SIM_START + SIM_DAYS * DAY_MS

# Rough (lat, lon) of Indian cities — used for geo-jump realism only.
_CITIES = [
    (19.0760, 72.8777),  # Mumbai
    (28.6139, 77.2090),  # Delhi
    (12.9716, 77.5946),  # Bengaluru
    (13.0827, 80.2707),  # Chennai
    (22.5726, 88.3639),  # Kolkata
    (17.3850, 78.4867),  # Hyderabad
]

MAX_SUBGRAPH_NODES = 80
MAX_SUBGRAPH_EDGES = 200


@dataclass
class FraudDataset:
    """Generated dataset: tabular rows + labelled heterogeneous graph."""

    feature_names: list[str]
    x: np.ndarray  # (n_samples, n_features) float32 — tabular rows
    y: np.ndarray  # (n_samples,) int64 — 0/1 fraud label
    graph: dict[str, Any]  # {nodes:[{id,label,props,features}], edges:[...], node_labels:[...]}
    node_ids: list[str]
    node_features: np.ndarray  # (n_nodes, node feature dim) float32
    node_labels: np.ndarray  # (n_nodes,) int64 — 1 = fraud participant
    tx_meta: list[dict[str, Any]] = field(default_factory=list)

    @property
    def fraud_rate(self) -> float:
        return float(self.y.mean()) if len(self.y) else 0.0

    def summary(self) -> dict[str, Any]:
        return {
            "samples": int(len(self.y)),
            "fraud_samples": int(self.y.sum()),
            "fraud_rate": round(self.fraud_rate, 4),
            "features": len(self.feature_names),
            "nodes": len(self.node_ids),
            "edges": len(self.graph["edges"]),
            "fraud_nodes": int(self.node_labels.sum()),
        }


@dataclass
class _Tx:
    seq: int
    ts: int
    src: str  # Account:<vpa>
    dst: str  # Account:<vpa> | Merchant:<id>
    amount: float
    device: str  # Device:<hash> of the sender
    ip: str  # IPAddress:<ip> of the sender
    city: tuple[float, float]
    label: int
    kind: str


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = a
    lat2, lon2 = b
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi = np.radians(lat2 - lat1)
    dlmb = np.radians(lon2 - lon1)
    h = np.sin(dphi / 2.0) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlmb / 2.0) ** 2
    return float(2.0 * 6371.0 * np.arcsin(np.sqrt(h)))


def _fast_subgraph(nodes: list[GraphNode], edges: list[GraphEdge]) -> Subgraph:
    """Subgraph without per-model validation (hot path in the generator)."""
    sub = Subgraph.model_construct(_fields_set={"nodes", "edges"})
    sub.nodes = nodes
    sub.edges = edges
    return sub


class _Builder:
    """Accumulates entities, edges and transactions, then extracts features."""

    def __init__(self, seed: int):
        self.rng = np.random.default_rng(seed)
        self.nodes: dict[str, dict[str, Any]] = {}
        self.node_order: list[str] = []
        self.edges: list[dict[str, Any]] = []
        self.tx: list[_Tx] = []
        self.acc_dev: dict[str, str] = {}
        self.acc_ip: dict[str, str] = {}
        self.acc_city: dict[str, tuple[float, float]] = {}
        self._seq = 0

    # -- entities ---------------------------------------------------------
    def add_node(self, node_id: str, label: str, props: dict[str, Any]) -> None:
        if node_id in self.nodes:
            return
        self.nodes[node_id] = {"id": node_id, "label": label, "props": props}
        self.node_order.append(node_id)

    def add_edge(
        self,
        src: str,
        dst: str,
        rel: str,
        amount: Optional[float] = None,
        timestamp_ms: Optional[int] = None,
    ) -> None:
        self.edges.append(
            {"src": src, "dst": dst, "rel": rel, "amount": amount, "timestamp_ms": timestamp_ms}
        )

    def add_tx(
        self,
        ts: int,
        src: str,
        dst: str,
        amount: float,
        device: str,
        ip: str,
        city: tuple[float, float],
        label: int,
        kind: str,
    ) -> None:
        self.tx.append(
            _Tx(
                seq=self._seq,
                ts=int(ts),
                src=src,
                dst=dst,
                amount=float(amount),
                device=device,
                ip=ip,
                city=city,
                label=int(label),
                kind=kind,
            )
        )
        # the transfer is also a graph edge (feeds subgraph aggregation + GNN)
        rel = "PAID_MERCHANT" if dst.startswith("Merchant:") else "TRANSFERRED"
        self.add_edge(src, dst, rel, amount=float(amount), timestamp_ms=int(ts))
        self._seq += 1

    def power_amount(self, cap: float = 150_000.0) -> float:
        """Power-law-ish transfer amount (heavy tail, rounded to ₹10)."""
        a = float(self.rng.pareto(1.3))
        raw = 60.0 + a * 3_000.0
        return float(np.round(min(cap, max(50.0, raw)), -1))


# ============================================================================
# Generation
# ============================================================================


def generate(
    seed: int = 42,
    n_users: int = 2000,
    n_merchants: int = 100,
    n_rings: int = 5,
    n_geo_fraud: int = 150,
    n_fresh: int = 40,
    n_sybil_clusters: int = 20,
) -> FraudDataset:
    """Generate the synthetic fraud dataset (deterministic for a given seed)."""
    b = _Builder(seed)
    rng = b.rng

    def register_account(vpa: str, created: int, flags: list[str]) -> str:
        """Account + dedicated device + dedicated IP + structural edges."""
        acc = f"Account:{vpa}"
        dev = f"Device:dev-{vpa}"
        ip = f"IPAddress:ip-{vpa}"
        b.add_node(acc, "Account", {"vpa_id": vpa, "created_at_ms": created, "risk_flags": flags})
        b.add_node(dev, "Device", {"created_at_ms": created, "rooted": False})
        b.add_node(ip, "IPAddress", {"risk_score": round(float(rng.uniform(0.05, 0.20)), 3)})
        b.add_edge(acc, dev, "USED_DEVICE", timestamp_ms=created)
        b.add_edge(acc, ip, "CONNECTED_FROM", timestamp_ms=created)
        b.acc_dev[acc] = dev
        b.acc_ip[acc] = ip
        return acc

    # --- ~2000 honest users ---------------------------------------------
    established: list[str] = []
    for i in range(n_users):
        vpa = f"user{i:04d}@synth"
        created = int(SIM_END - int(rng.uniform(2 * DAY_MS, 90 * DAY_MS)))
        acc = register_account(vpa, created, flags=[])
        established.append(acc)
        b.acc_city[acc] = _CITIES[int(rng.integers(0, len(_CITIES)))]

    # --- merchants -------------------------------------------------------
    merchants: list[str] = []
    for j in range(n_merchants):
        mid = f"mcht{j:03d}@synth"
        node = f"Merchant:{mid}"
        created = int(SIM_END - int(rng.uniform(30 * DAY_MS, 180 * DAY_MS)))
        b.add_node(node, "Merchant", {"merchant_id": mid, "created_at_ms": created})
        merchants.append(node)
    merchant_set = set(merchants)

    # --- fresh accounts (new-account targets) ----------------------------
    fresh: list[str] = []
    for k in range(n_fresh):
        vpa = f"fresh{k:03d}@synth"
        created = int(SIM_END - int(rng.uniform(2 * HOUR_MS, 46 * HOUR_MS)))
        acc = register_account(vpa, created, flags=[])
        fresh.append(acc)
        b.acc_city[acc] = _CITIES[0]

    # --- n_rings mule rings ----------------------------------------------
    for r in range(n_rings):
        mules = [f"Account:mule{r}{m}@synth" for m in range(3)]
        cashout = f"Account:cash{r}@synth"
        dev = f"Device:dev-ring-{chr(ord('a') + r)}"
        ip = f"IPAddress:10.66.0.{r + 1}"
        mule_created = int(SIM_END - int(rng.uniform(20 * HOUR_MS, 47 * HOUR_MS)))
        b.add_node(ip, "IPAddress", {"risk_score": 0.9})
        b.add_node(dev, "Device", {"created_at_ms": mule_created, "rooted": False})
        for node_id in (*mules, cashout):
            vpa = node_id.split(":", 1)[1]
            flags = ["cashout"] if node_id == cashout else ["mule_suspect"]
            b.add_node(
                node_id,
                "Account",
                {"vpa_id": vpa, "created_at_ms": mule_created, "risk_flags": flags},
            )
            b.add_edge(node_id, dev, "USED_DEVICE", timestamp_ms=mule_created)
            b.add_edge(node_id, ip, "CONNECTED_FROM", timestamp_ms=mule_created)
            b.acc_dev[node_id] = dev
            b.acc_ip[node_id] = ip
            b.acc_city[node_id] = _CITIES[r % len(_CITIES)]

        # fan-in: 15 victims -> mule0 within a 30-minute window
        base = SIM_END - (19 - 2 * r) * HOUR_MS
        picks = rng.choice(len(established), 15, replace=False)
        for i in picks:
            victim = established[int(i)]
            ts = base + int(rng.integers(0, 30 * MIN_MS))
            b.add_tx(
                ts,
                victim,
                mules[0],
                float(np.round(rng.uniform(5_000, 20_000), -2)),
                b.acc_dev[victim],
                b.acc_ip[victim],
                b.acc_city[victim],
                label=1,
                kind=f"ring{r}_fanin",
            )

        # fast pass-through: mule0 -> mule1 -> mule2 -> cashout (<= minutes)
        t0 = base + 30 * MIN_MS + 30_000
        chain_amount = float(np.round(rng.uniform(40_000, 60_000), -2))
        hop = t0
        path = [mules[0], mules[1], mules[2], cashout]
        for a, c in zip(path, path[1:]):
            b.add_tx(
                hop,
                a,
                c,
                chain_amount,
                dev,
                ip,
                b.acc_city[a],
                label=1,
                kind=f"ring{r}_pass",
            )
            chain_amount = float(np.round(chain_amount * 0.99, -2))
            hop += int(rng.integers(60, 120) * 1000)

    # --- sybil shared-device clusters ------------------------------------
    for c in range(n_sybil_clusters):
        size = 5
        dev = f"Device:dev-sybil-{c}"
        ip = f"IPAddress:10.77.{c}.1"
        created = int(SIM_END - int(rng.uniform(10 * DAY_MS, 60 * DAY_MS)))
        b.add_node(dev, "Device", {"created_at_ms": created, "rooted": False})
        b.add_node(ip, "IPAddress", {"risk_score": 0.6})
        members: list[str] = []
        for m in range(size):
            vpa = f"sybil{c:02d}_{m}@synth"
            acc = f"Account:{vpa}"
            b.add_node(
                acc, "Account", {"vpa_id": vpa, "created_at_ms": created, "risk_flags": []}
            )
            b.add_edge(acc, dev, "USED_DEVICE", timestamp_ms=created)
            b.add_edge(acc, ip, "CONNECTED_FROM", timestamp_ms=created)
            b.acc_dev[acc] = dev
            b.acc_ip[acc] = ip
            b.acc_city[acc] = _CITIES[c % len(_CITIES)]
            members.append(acc)
        for _ in range(5):
            src_i, dst_i = (int(v) for v in rng.choice(len(members), 2, replace=False))
            ts = int(SIM_START + rng.uniform(0, SIM_DAYS * DAY_MS))
            b.add_tx(
                ts,
                members[src_i],
                members[dst_i],
                float(np.round(rng.uniform(20_000, 60_000), -2)),
                dev,
                ip,
                b.acc_city[members[src_i]],
                label=1,
                kind=f"sybil{c}",
            )

    # --- geo anomalies: same user, two cities within minutes -------------
    for g in range(n_geo_fraud):
        src = established[int(rng.integers(0, len(established)))]
        dst = established[int(rng.integers(0, len(established)))]
        while dst == src:
            dst = established[int(rng.integers(0, len(established)))]
        home = b.acc_city[src]
        far = _CITIES[int(rng.integers(0, len(_CITIES)))]
        while far == home:
            far = _CITIES[int(rng.integers(0, len(_CITIES)))]
        t1 = int(SIM_START + rng.uniform(0, SIM_DAYS * DAY_MS - 10 * MIN_MS))
        dev, ip = b.acc_dev[src], b.acc_ip[src]
        # honest leg from home city...
        b.add_tx(t1, src, dst, b.power_amount(cap=5_000), dev, ip, home, 0, f"geo{g}_leg1")
        # ...then a second payment from >1000 km away minutes later
        t2 = t1 + int(rng.uniform(60, 240) * 1000)
        b.add_tx(t2, src, dst, b.power_amount(cap=5_000), dev, ip, far, 1, f"geo{g}_leg2")

    # --- fresh-account fan-in (new-account targets) ----------------------
    for acc in fresh:
        created = int(b.nodes[acc]["props"]["created_at_ms"])
        payers = [
            established[int(i)]
            for i in rng.choice(len(established), 4, replace=False)
        ]
        window_start = min(
            created + int(rng.uniform(5 * MIN_MS, 1 * HOUR_MS)), SIM_END - 2 * HOUR_MS
        )
        for p, payer in enumerate(payers):
            ts = window_start + p * int(rng.uniform(3, 10) * MIN_MS)
            b.add_tx(
                ts,
                payer,
                acc,
                float(np.round(rng.uniform(50_000, 90_000), -2)),
                b.acc_dev[payer],
                b.acc_ip[payer],
                b.acc_city[payer],
                label=1,
                kind="fresh_target",
            )

    # --- honest traffic: sparse, power-law amounts ------------------------
    for acc in established:
        n_send = 1 + int(rng.integers(0, 4))
        for _ in range(n_send):
            if rng.random() < 0.30:
                dst = merchants[int(rng.integers(0, len(merchants)))]
            else:
                dst = established[int(rng.integers(0, len(established)))]
                while dst == acc:
                    dst = established[int(rng.integers(0, len(established)))]
            ts = int(SIM_START + rng.uniform(0, SIM_DAYS * DAY_MS))
            home = b.acc_city[acc]
            jitter = (float(rng.uniform(-0.05, 0.05)), float(rng.uniform(-0.05, 0.05)))
            city = (home[0] + jitter[0], home[1] + jitter[1])
            b.add_tx(
                ts,
                acc,
                dst,
                b.power_amount(),
                b.acc_dev[acc],
                b.acc_ip[acc],
                city,
                label=0,
                kind="merchant" if dst in merchant_set else "p2p",
            )

    # --- structural edge timestamps from last activity --------------------
    struct_idx: dict[tuple[str, str], int] = {}
    for i, e in enumerate(b.edges):
        if e["rel"] in ("USED_DEVICE", "CONNECTED_FROM"):
            struct_idx[(e["src"], e["rel"])] = i
    last_active: dict[str, int] = {}
    for tx in b.tx:
        last_active[tx.src] = max(last_active.get(tx.src, 0), tx.ts)
    for (acc, _rel), i in struct_idx.items():
        if acc in last_active:
            b.edges[i]["timestamp_ms"] = last_active[acc]

    return _extract(b)


# ============================================================================
# Feature extraction (streaming, causal)
# ============================================================================


def _window(items: list[tuple[int, float]], ts: int, window_ms: int) -> list[tuple[int, float]]:
    """Entries in `(ts - window_ms, ts]` — lists are append-ordered by time."""
    lo = ts - window_ms
    out = []
    for it in reversed(items):
        if it[0] <= lo:
            break
        out.append(it)
    return out


def _extract(b: _Builder) -> FraudDataset:
    txs = sorted(b.tx, key=lambda t: (t.ts, t.seq))

    user_hist: dict[str, list[tuple[int, float]]] = {}
    dst_hist: dict[str, list[tuple[int, str]]] = {}
    dev_hist: dict[str, list[tuple[int, float]]] = {}
    ip_hist: dict[str, list[tuple[int, float]]] = {}
    log_hist: dict[str, list[float]] = {}
    prev_geo: dict[str, tuple[tuple[float, float], int]] = {}

    rows: list[list[float]] = []
    labels: list[int] = []
    meta: list[dict[str, Any]] = []

    # adjacency for 2-hop subgraphs (undirected, insertion-ordered)
    adjacency: dict[str, list[int]] = {}
    for idx, e in enumerate(b.edges):
        adjacency.setdefault(e["src"], []).append(idx)
        adjacency.setdefault(e["dst"], []).append(idx)

    for tx in txs:
        user_hist.setdefault(tx.src, []).append((tx.ts, tx.amount))
        dst_hist.setdefault(tx.dst, []).append((tx.ts, tx.src))
        dev_hist.setdefault(tx.device, []).append((tx.ts, tx.amount))
        ip_hist.setdefault(tx.ip, []).append((tx.ts, tx.amount))

        # velocity (current tx included — matches Redis INCR-then-read)
        w1 = _window(user_hist[tx.src], tx.ts, HOUR_MS)
        w24 = _window(user_hist[tx.src], tx.ts, DAY_MS)
        d1 = _window(dst_hist[tx.dst], tx.ts, HOUR_MS)
        dev1 = _window(dev_hist[tx.device], tx.ts, HOUR_MS)
        ip1 = _window(ip_hist[tx.ip], tx.ts, HOUR_MS)

        src_count_1h = float(len(w1))
        src_amount_1h = float(sum(a for _, a in w1))
        src_count_24h = float(len(w24))
        src_amount_24h = float(sum(a for _, a in w24))
        dst_count_1h = float(len(d1))
        dst_distinct = float(len({s for _, s in d1}))
        device_count_1h = float(len(dev1))
        ip_count_1h = float(len(ip1))

        # amount z-score against sender history (before this tx)
        hist = log_hist.setdefault(tx.src, [])
        if len(hist) >= 2:
            arr = np.asarray(hist, dtype=np.float64)
            std = float(arr.std())
            z = (float(np.log1p(tx.amount)) - float(arr.mean())) / std if std > 1e-9 else 0.0
        else:
            z = 0.0
        hist.append(float(np.log1p(tx.amount)))

        # geo jump vs the sender's previous position
        prev = prev_geo.get(tx.src)
        jump = 0.0
        if prev is not None:
            (plat, plon), pts = prev
            dt_h = (tx.ts - pts) / HOUR_MS
            if dt_h > 0:
                jump = _haversine_km((plat, plon), tx.city) / dt_h
        prev_geo[tx.src] = (tx.city, tx.ts)

        dst_props = b.nodes[tx.dst]["props"]
        dst_age = max(0.0, (tx.ts - int(dst_props["created_at_ms"])) / HOUR_MS)
        dev_props = b.nodes[tx.device]["props"]
        dev_age = max(0.0, (tx.ts - int(dev_props.get("created_at_ms", tx.ts))) / HOUR_MS)
        ip_risk = float(b.nodes[tx.ip]["props"].get("risk_score", 0.0))

        agg = graph_features(_neighbourhood(b, adjacency, tx))

        row = {
            "src_count_1h": src_count_1h,
            "src_amount_1h": src_amount_1h,
            "src_count_24h": src_count_24h,
            "src_amount_24h": src_amount_24h,
            "dst_count_1h": dst_count_1h,
            "dst_distinct_senders_1h": dst_distinct,
            "device_count_1h": device_count_1h,
            "ip_count_1h": ip_count_1h,
            "dst_account_age_hours": dst_age,
            "device_age_hours": dev_age,
            "geo_jump_kmh": float(jump),
            "amount_zscore": float(z),
            "ip_risk_score": ip_risk,
            **agg,
        }
        rows.append([float(row[name]) for name in TABULAR_FEATURES])
        labels.append(tx.label)
        meta.append(
            {
                "tx_id": f"tx{tx.seq:06d}",
                "ts": tx.ts,
                "src": tx.src,
                "dst": tx.dst,
                "amount": tx.amount,
                "label": tx.label,
                "kind": tx.kind,
            }
        )

    # --- full graph featurisation ----------------------------------------
    full_nodes = [
        GraphNode.model_construct(
            id=node_id,
            label=b.nodes[node_id]["label"],
            props=b.nodes[node_id]["props"],
            _fields_set={"id", "label", "props"},
        )
        for node_id in b.node_order
    ]
    full_edges = [
        GraphEdge.model_construct(
            src=e["src"],
            dst=e["dst"],
            rel=e["rel"],
            amount=e["amount"],
            timestamp_ms=e["timestamp_ms"],
            _fields_set={"src", "dst", "rel", "amount", "timestamp_ms"},
        )
        for e in b.edges
    ]
    arrays = build_graph_arrays(_fast_subgraph(full_nodes, full_edges))

    # --- node labels: fraud participants ----------------------------------
    fraud_ids: set[str] = set()
    for n in b.nodes.values():
        if n["label"] != "Account":
            continue
        flags = n["props"].get("risk_flags") or []
        if any(f in ("mule_suspect", "cashout") for f in flags):
            fraud_ids.add(n["id"])
        elif n["id"].startswith("Account:sybil") or n["id"].startswith("Account:fresh"):
            fraud_ids.add(n["id"])

    users_of: dict[str, set[str]] = {}
    for e in b.edges:
        if e["rel"] == "USED_DEVICE":
            users_of.setdefault(e["dst"], set()).add(e["src"])
        elif e["rel"] == "CONNECTED_FROM":
            users_of.setdefault(e["dst"], set()).add(e["src"])

    node_labels = np.zeros(len(arrays.node_ids), dtype=np.int64)
    for i, node_id in enumerate(arrays.node_ids):
        if node_id in fraud_ids:
            node_labels[i] = 1
        elif b.nodes[node_id]["label"] in ("Device", "IPAddress"):
            users = users_of.get(node_id, set())
            if users and users <= fraud_ids:
                node_labels[i] = 1

    graph = {
        "nodes": [
            {
                "id": node_id,
                "label": b.nodes[node_id]["label"],
                "props": b.nodes[node_id]["props"],
                "features": [float(v) for v in arrays.node_features[i]],
            }
            for i, node_id in enumerate(arrays.node_ids)
        ],
        "edges": [
            {
                "src": e["src"],
                "dst": e["dst"],
                "rel": e["rel"],
                "amount": e["amount"],
                "timestamp_ms": e["timestamp_ms"],
            }
            for e in b.edges
        ],
        "node_labels": [int(v) for v in node_labels],
    }

    return FraudDataset(
        feature_names=list(TABULAR_FEATURES),
        x=np.asarray(rows, dtype=np.float32),
        y=np.asarray(labels, dtype=np.int64),
        graph=graph,
        node_ids=arrays.node_ids,
        node_features=arrays.node_features,
        node_labels=node_labels,
        tx_meta=meta,
    )


def _neighbourhood(b: _Builder, adjacency: dict[str, list[int]], tx: _Tx) -> Subgraph:
    """Causal 2-hop subgraph around (src, dst), capped deterministically.

    `TRANSFERRED`/`PAID_MERCHANT` edges must predate the transaction;
    structural device/IP edges always exist.
    """

    def edge_ok(e: dict[str, Any]) -> bool:
        if e["rel"] in ("TRANSFERRED", "PAID_MERCHANT"):
            return e["timestamp_ms"] is None or e["timestamp_ms"] <= tx.ts
        return True

    seeds = [tx.src, tx.dst]
    order: list[str] = []
    seen: set[str] = set(seeds)
    frontier = list(seeds)
    for _ in range(2):
        nxt: list[str] = []
        for node_id in frontier:
            for ei in adjacency.get(node_id, ()):
                e = b.edges[ei]
                other = e["dst"] if e["src"] == node_id else e["src"]
                if other in seen:
                    continue
                seen.add(other)
                nxt.append(other)
                order.append(other)
                if len(seen) >= MAX_SUBGRAPH_NODES:
                    break
            if len(seen) >= MAX_SUBGRAPH_NODES:
                break
        frontier = nxt
        if not frontier or len(seen) >= MAX_SUBGRAPH_NODES:
            break

    node_ids = seeds + order
    node_set = set(node_ids)

    edge_idxs: set[int] = set()
    for node_id in node_ids:
        edge_idxs.update(adjacency.get(node_id, ()))

    sub_nodes = [
        GraphNode.model_construct(
            id=node_id,
            label=b.nodes[node_id]["label"],
            props=b.nodes[node_id]["props"],
            _fields_set={"id", "label", "props"},
        )
        for node_id in node_ids
    ]
    sub_edges: list[GraphEdge] = []
    for ei in sorted(edge_idxs):  # deterministic: global edge insertion order
        e = b.edges[ei]
        if e["src"] in node_set and e["dst"] in node_set and edge_ok(e):
            sub_edges.append(
                GraphEdge.model_construct(
                    src=e["src"],
                    dst=e["dst"],
                    rel=e["rel"],
                    amount=e["amount"],
                    timestamp_ms=e["timestamp_ms"],
                    _fields_set={"src", "dst", "rel", "amount", "timestamp_ms"},
                )
            )
            if len(sub_edges) >= MAX_SUBGRAPH_EDGES:
                break
    return _fast_subgraph(sub_nodes, sub_edges)


if __name__ == "__main__":  # pragma: no cover - manual smoke
    ds = generate(seed=42)
    print(ds.summary())
