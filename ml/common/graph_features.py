"""Subgraph -> dense tensor arrays for the GNN (numpy only, no torch).

Shared by training and inference so the RGCN always sees identical label /
relation encodings and node features. Output shapes:

    node_features : (N, NODE_FEATURE_DIM) float32
    edge_index     : (2, E)             int64
    edge_type      : (E,)               int64
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ml.common.schemas import Subgraph

#: Stable node-label encodings (order = one-hot index).
NODE_LABELS: tuple[str, ...] = ("Account", "Device", "IPAddress", "Merchant")
#: Stable relation encodings; `OTHER` catches anything outside the schema.
EDGE_RELS: tuple[str, ...] = ("TRANSFERRED", "USED_DEVICE", "CONNECTED_FROM", "PAID_MERCHANT", "OTHER")

NODE_FEATURE_DIM = 10
NUM_RELATIONS = len(EDGE_RELS)


def with_reverse_relations(
    edge_index: np.ndarray, edge_type: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Append the reverse of every edge with relation id `+ NUM_RELATIONS`.

    Training and inference must both use this — RGCN relation ids are part of
    the model contract.
    """
    rev_index = edge_index[::-1, :]
    rev_type = edge_type + NUM_RELATIONS
    return (
        np.concatenate([edge_index, rev_index], axis=1),
        np.concatenate([edge_type, rev_type]),
    )

_WATCHLIST_FLAGS = ("mule_suspect", "cashout")
_DEGREE_CAP = 20.0
_VOLUME_CAP = 100_000.0
_RECENCY_WINDOW_MS = 7 * 24 * 3_600_000


@dataclass(frozen=True)
class GraphArrays:
    node_ids: list[str]
    node_features: np.ndarray  # (N, NODE_FEATURE_DIM) float32
    edge_index: np.ndarray  # (2, E) int64
    edge_type: np.ndarray  # (E,) int64
    account_mask: np.ndarray  # (N,) bool — node is an Account

    @property
    def num_nodes(self) -> int:
        return len(self.node_ids)


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def build_graph_arrays(sub: Subgraph) -> GraphArrays:
    """Deterministically featurise a neighbourhood subgraph."""
    node_ids: list[str] = []
    index: dict[str, int] = {}
    labels: list[str] = []
    props: list[dict] = []
    for n in sub.nodes:
        if n.id in index:
            continue
        index[n.id] = len(node_ids)
        node_ids.append(n.id)
        labels.append(n.label)
        props.append(n.props)

    n_nodes = len(node_ids)
    features = np.zeros((n_nodes, NODE_FEATURE_DIM), dtype=np.float32)

    in_deg = np.zeros(n_nodes, dtype=np.float64)
    out_deg = np.zeros(n_nodes, dtype=np.float64)
    volume = np.zeros(n_nodes, dtype=np.float64)
    latest_ts = np.zeros(n_nodes, dtype=np.float64)

    srcs: list[int] = []
    dsts: list[int] = []
    rels: list[int] = []
    max_ts = 0.0

    for e in sub.edges:
        si = index.get(e.src)
        di = index.get(e.dst)
        if si is None or di is None:
            continue  # subgraph must be closed; ignore dangling references
        rel = EDGE_RELS.index(e.rel) if e.rel in EDGE_RELS else EDGE_RELS.index("OTHER")
        srcs.append(si)
        dsts.append(di)
        rels.append(rel)
        out_deg[si] += 1
        in_deg[di] += 1
        if e.rel == "TRANSFERRED":
            amount = float(e.amount or 0.0)
            volume[si] += amount
            volume[di] += amount
        if e.timestamp_ms is not None:
            ts = float(e.timestamp_ms)
            max_ts = max(max_ts, ts)
            latest_ts[si] = max(latest_ts[si], ts)
            latest_ts[di] = max(latest_ts[di], ts)

    for i, label in enumerate(labels):
        if label in NODE_LABELS:
            features[i, NODE_LABELS.index(label)] = 1.0
        p = props[i]
        try:
            features[i, 4] = _clamp01(float(p.get("risk_score", 0.0) or 0.0))
        except (TypeError, ValueError):
            pass
        flags = p.get("risk_flags") or []
        if isinstance(flags, str):
            flags = [flags]
        features[i, 5] = 1.0 if any(str(x) in _WATCHLIST_FLAGS for x in flags) else 0.0
        features[i, 6] = float(min(1.0, in_deg[i] / _DEGREE_CAP))
        features[i, 7] = float(min(1.0, out_deg[i] / _DEGREE_CAP))
        features[i, 8] = float(min(1.0, volume[i] / _VOLUME_CAP))
        if max_ts > 0.0:
            age = max(0.0, max_ts - latest_ts[i])
            features[i, 9] = float(min(1.0, age / _RECENCY_WINDOW_MS))

    edge_index = np.array([srcs, dsts], dtype=np.int64).reshape(2, -1)
    edge_type = np.array(rels, dtype=np.int64)
    account_mask = np.array([lb == "Account" for lb in labels], dtype=bool)

    return GraphArrays(
        node_ids=node_ids,
        node_features=features,
        edge_index=edge_index,
        edge_type=edge_type,
        account_mask=account_mask,
    )
