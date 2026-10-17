"""Small 2-layer RGCN for topological fraud risk (OPTIONAL).

Requires `torch` + `torch-geometric` — import this module only inside the
guarded training block (it imports both at module level).

Model contract (shared with `ml.inference.app.scorers.TorchScorer`)::

    logits = model(node_features, edge_index, edge_type)   # (N, 1)

Pipeline: the dataset's graph dicts are materialised as a PyG `HeteroData`
(per node type `x`/`y`, typed `edge_index`s), then converted to a homogeneous
tensor view whose relation ids are exactly the shared `EDGE_RELS` encoding
(forward `0..4`, reverse `+5`) so training and inference agree bit-for-bit.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch_geometric.data import HeteroData
from torch_geometric.nn import RGCNConv

from ml.common.graph_features import EDGE_RELS, NODE_FEATURE_DIM, NUM_RELATIONS

_HIDDEN = 64


class RGCNClassifier(nn.Module):
    """2-layer RGCN producing one logit per node."""

    def __init__(
        self,
        in_dim: int = NODE_FEATURE_DIM,
        hidden: int = _HIDDEN,
        num_relations: int = 2 * NUM_RELATIONS,  # forward + reverse
    ):
        super().__init__()
        self.conv1 = RGCNConv(in_dim, hidden, num_relations)
        self.conv2 = RGCNConv(hidden, 1, num_relations)

    def forward(
        self, x: torch.Tensor, edge_index: torch.Tensor, edge_type: torch.Tensor
    ) -> torch.Tensor:
        h = F.relu(self.conv1(x, edge_index, edge_type))
        return self.conv2(h, edge_index, edge_type)  # (N, 1) logits


def build_heterodata(graph: dict[str, Any]) -> HeteroData:
    """Build a PyG `HeteroData` from the dataset's graph dicts.

    Node stores carry `x` (10-dim features) and `y` (fraud-participant
    label); edge stores are typed `(src_type, rel, dst_type)` with node-local
    indices (forward direction only — reverses are added at conversion).
    """
    nodes = graph["nodes"]
    edges = graph["edges"]
    label_of = {n["id"]: n["label"] for n in nodes}
    labels = sorted(set(label_of.values()))

    data = HeteroData()
    node_ids_by_type: dict[str, list[str]] = {}
    for ntype in labels:
        members = [n["id"] for n in nodes if n["label"] == ntype]
        node_ids_by_type[ntype] = members
        feats = np.asarray(
            [n["features"] for n in nodes if n["label"] == ntype], dtype=np.float32
        )
        data[ntype].x = torch.from_numpy(feats)
        data[ntype].y = torch.tensor(
            [int(graph["node_labels"][i]) for i, n in enumerate(nodes) if n["label"] == ntype],
            dtype=torch.float32,
        )

    buckets: dict[tuple[str, str, str], list[tuple[int, int]]] = {}
    # node-local index per type (HeteroData edge stores use local ids)
    local_index: dict[str, int] = {}
    for members in node_ids_by_type.values():
        for i, nid in enumerate(members):
            local_index[nid] = i

    for e in edges:
        if e["src"] not in local_index or e["dst"] not in local_index:
            continue
        key = (label_of[e["src"]], e["rel"], label_of[e["dst"]])
        buckets.setdefault(key, []).append((local_index[e["src"]], local_index[e["dst"]]))
    for key in sorted(buckets):  # deterministic insertion order
        ei = torch.tensor(buckets[key], dtype=torch.long).t().contiguous()
        data[key[0], key[1], key[2]].edge_index = ei
    return data


def hetero_to_homogeneous(
    data: HeteroData,
) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
    """`(x, edge_index, edge_type, y)` with the shared relation encoding.

    Node rows are concatenated in `data.node_types` order (sorted labels);
    every forward edge is mirrored with `rel_id + NUM_RELATIONS`.
    """
    offsets: dict[str, int] = {}
    xs: list[torch.Tensor] = []
    ys: list[torch.Tensor] = []
    cursor = 0
    for ntype in data.node_types:
        offsets[ntype] = cursor
        xs.append(data[ntype].x)
        ys.append(data[ntype].y)
        cursor += int(data[ntype].x.shape[0])
    x = torch.cat(xs, dim=0)
    y = torch.cat(ys, dim=0)

    srcs: list[int] = []
    dsts: list[int] = []
    types: list[int] = []
    for src_type, rel, dst_type in data.edge_types():
        edge_index = data[src_type, rel, dst_type].edge_index
        base_src = offsets[src_type]
        base_dst = offsets[dst_type]
        rel_id = EDGE_RELS.index(rel) if rel in EDGE_RELS else EDGE_RELS.index("OTHER")
        s = (edge_index[0] + base_src).tolist()
        d = (edge_index[1] + base_dst).tolist()
        srcs.extend(s)
        dsts.extend(d)
        types.extend([rel_id] * len(s))
        # reverse direction, mirrored relation id
        srcs.extend(d)
        dsts.extend(s)
        types.extend([rel_id + NUM_RELATIONS] * len(s))

    edge_index = torch.tensor([srcs, dsts], dtype=torch.long)
    edge_type = torch.tensor(types, dtype=torch.long)
    return x, edge_index, edge_type, y


def train_rgcn(
    graph: dict[str, Any],
    out_path: str,
    seed: int = 42,
    epochs: int = 120,
    lr: float = 0.01,
    val_frac: float = 0.2,
    patience: int = 20,
) -> dict[str, Any]:
    """Train the RGCN on node labels, save `out_path`, return metrics.

    Tries `torch.jit.script` first (cleanest artefact), then falls back to
    `torch.save` of the full module — the inference loader handles both
    (`torch.jit.load` then `torch.load(map_location="cpu",
    weights_only=False)`).
    """
    from sklearn.metrics import precision_score, recall_score, roc_auc_score

    torch.manual_seed(seed)
    rng = np.random.default_rng(seed)

    data = build_heterodata(graph)  # PyG HeteroData (architecture pipeline)
    x, edge_index, edge_type, y = hetero_to_homogeneous(data)

    n = int(x.shape[0])
    y_np = y.numpy()
    pos = np.nonzero(y_np > 0)[0]
    neg = np.nonzero(y_np <= 0)[0]
    rng.shuffle(pos)
    rng.shuffle(neg)
    n_val_pos = max(1, int(len(pos) * val_frac))
    n_val_neg = max(1, int(len(neg) * val_frac))
    val_idx = np.concatenate([pos[:n_val_pos], neg[:n_val_neg]])
    train_mask = np.ones(n, dtype=bool)
    train_mask[val_idx] = False
    train_idx = np.nonzero(train_mask)[0]

    model = RGCNClassifier(in_dim=int(x.shape[1]))
    optim = torch.optim.Adam(model.parameters(), lr=lr, weight_decay=1e-4)
    n_pos_train = max(1.0, float(y_np[train_idx].sum()))
    n_neg_train = float(len(train_idx)) - float(y_np[train_idx].sum())
    criterion = nn.BCEWithLogitsLoss(
        pos_weight=torch.tensor(n_neg_train / n_pos_train)
    )

    y_train = y[train_idx].unsqueeze(1)
    best_auc = -1.0
    best_state: dict[str, Any] | None = None
    bad_epochs = 0
    for _ in range(epochs):
        model.train()
        optim.zero_grad()
        logits = model(x, edge_index, edge_type)
        loss = criterion(logits[train_idx], y_train)
        loss.backward()
        optim.step()

        model.eval()
        with torch.no_grad():
            val_logits = model(x, edge_index, edge_type)[val_idx].reshape(-1).numpy()
        auc = float(roc_auc_score(y_np[val_idx], val_logits))
        if auc > best_auc + 1e-4:
            best_auc = auc
            best_state = {k: v.detach().clone() for k, v in model.state_dict().items()}
            bad_epochs = 0
        else:
            bad_epochs += 1
            if bad_epochs >= patience:
                break

    if best_state is not None:
        model.load_state_dict(best_state)
    model.eval()

    with torch.no_grad():
        val_pred = (
            torch.sigmoid(model(x, edge_index, edge_type)[val_idx].reshape(-1)).numpy()
        )
    metrics: dict[str, Any] = {
        "auc": float(roc_auc_score(y_np[val_idx], val_pred)),
        "precision": float(precision_score(y_np[val_idx], val_pred >= 0.5, zero_division=0)),
        "recall": float(recall_score(y_np[val_idx], val_pred >= 0.5, zero_division=0)),
        "val_nodes": int(len(val_idx)),
        "train_nodes": int(len(train_idx)),
        "fraud_nodes": int(y_np.sum()),
        "num_nodes": n,
        "num_edge_type_ids": int(edge_type.max().item()) + 1 if n else 0,
    }

    save_format = "torch.save"
    try:
        scripted = torch.jit.script(model)
        scripted.save(out_path)
        save_format = "torch.jit.script"
    except Exception as exc:  # RGCNConv internals may resist scripting
        torch.save(model, out_path)
        metrics["script_error"] = str(exc)
    metrics["save_format"] = save_format
    return metrics
