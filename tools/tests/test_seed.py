"""Seeder tests — run `seed_graph` / `seed_redis` against in-memory fakes
(no infrastructure required) and assert the exact key/edge scheme."""

from __future__ import annotations

from typing import Any, Optional

from tools.demo import (
    ATTACK_AMOUNTS,
    BAD_IP,
    CASHOUT_IP,
    DEVICE_RING_A,
    DEVICE_RING_B,
    FANIN_AMOUNTS,
    FANIN_VPAS,
    FRESH_USERS,
    MERCHANTS,
    MULES,
    USERS,
    user_device,
    user_ip,
)
from tools.seed.seed import (
    SCHEMA_STATEMENTS,
    attack_edges,
    baseline_history,
    seed_graph,
    seed_redis,
)

NOW = 1_760_000_000_000  # fixed clock for determinism


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeResult:
    def consume(self) -> None:
        return None


class FakeSession:
    def __init__(self, fail_schema: Optional[Exception] = None):
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_schema = fail_schema

    def __enter__(self) -> "FakeSession":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def run(self, cypher: str, **params: Any) -> FakeResult:
        if self.fail_schema is not None and cypher.strip().upper().startswith(
            ("CREATE CONSTRAINT", "CREATE INDEX")
        ):
            raise self.fail_schema
        self.calls.append((" ".join(cypher.split()), params))
        return FakeResult()


class FakeDriver:
    def __init__(self, fail_schema: Optional[Exception] = None):
        self.session_obj = FakeSession(fail_schema=fail_schema)

    def session(self) -> FakeSession:
        return self.session_obj


class FakeRedis:
    """Tiny redis stand-in implementing the exact ops the seeder uses."""

    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}
        self.lists: dict[str, list[str]] = {}
        self.hashes: dict[str, dict[str, Any]] = {}
        self.ttls: dict[str, int] = {}
        self.ops: list[tuple[str, str]] = []

    def get(self, key: str) -> Optional[str]:
        return self.strings.get(key)

    def incrby(self, key: str, amount: int) -> int:
        value = int(self.strings.get(key, "0")) + int(amount)
        self.strings[key] = str(value)
        self.ops.append(("incrby", key))
        return value

    def incrbyfloat(self, key: str, amount: float) -> float:
        value = float(self.strings.get(key, "0")) + float(amount)
        self.strings[key] = repr(value)
        self.ops.append(("incrbyfloat", key))
        return value

    def expire(self, key: str, ttl: int) -> bool:
        self.ttls[key] = ttl
        return True

    def sadd(self, key: str, *members: str) -> int:
        bucket = self.sets.setdefault(key, set())
        before = len(bucket)
        bucket.update(str(m) for m in members)
        self.ops.append(("sadd", key))
        return len(bucket) - before

    def set(self, key: str, value: Any, nx: bool = False) -> Optional[str]:
        if nx and (key in self.strings or key in self.sets):
            return None
        self.strings[key] = str(value)
        self.ops.append(("set", key))
        return "OK"

    def exists(self, key: str) -> int:
        return int(key in self.strings or key in self.sets or key in self.lists)

    def lpush(self, key: str, *values: Any) -> int:
        bucket = self.lists.setdefault(key, [])
        for v in values:
            bucket.insert(0, str(v))  # LPUSH: each value becomes the new head
        self.ops.append(("lpush", key))
        return len(bucket)

    def ltrim(self, key: str, start: int, end: int) -> bool:
        if key in self.lists:
            self.lists[key] = self.lists[key][start : end + 1]
        return True

    def hsetnx(self, key: str, field: str, value: Any) -> int:
        bucket = self.hashes.setdefault(key, {})
        if field in bucket:
            return 0
        bucket[field] = value
        self.ops.append(("hsetnx", key))
        return 1


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------


def test_schema_statements_match_infra_semantics():
    joined = " ".join(SCHEMA_STATEMENTS)
    for constraint in ("vpa_id", "device_hash", "ip_address", "merchant_id"):
        assert constraint in joined
    assert "CREATE INDEX ON :TRANSFERRED(timestamp_ms);" in SCHEMA_STATEMENTS


def test_seed_graph_tolerates_already_exists():
    driver = FakeDriver(fail_schema=Exception("Constraint already exists"))
    stats = seed_graph(driver, now=NOW)
    assert stats["schema"] == len(SCHEMA_STATEMENTS)  # tolerated, not fatal


def test_seed_graph_propagates_real_errors():
    import pytest

    driver = FakeDriver(fail_schema=RuntimeError("connection reset"))
    with pytest.raises(RuntimeError):
        seed_graph(driver, now=NOW)


def test_seed_graph_writes_all_demo_entities_idempotently():
    driver = FakeDriver()
    stats = seed_graph(driver, now=NOW)
    calls = driver.session_obj.calls

    account_params = [p for c, p in calls if "MERGE (a:Account" in c]
    seeded_vpas = {p["vpa"] for p in account_params}
    assert seeded_vpas == set(USERS) | set(MULES) | {
        f"cashout01@cashdemo", f"cashout02@cashdemo", f"cashout03@cashdemo"
    }
    assert {p["vpa"] for p in account_params} >= set(MULES)

    # mule / cashout / fresh-account properties
    by_vpa = {p["vpa"]: p for p in account_params}
    for mule in MULES:
        assert by_vpa[mule]["risk_flags"] == ["mule_suspect"]
        assert NOW - by_vpa[mule]["created_at_ms"] < 48 * 3_600_000
    for cash in ("cashout01@cashdemo", "cashout02@cashdemo", "cashout03@cashdemo"):
        assert by_vpa[cash]["risk_flags"] == ["cashout"]
    for fresh in FRESH_USERS:
        assert NOW - by_vpa[fresh]["created_at_ms"] == 2 * 3_600_000
    # non-fresh users spread over the past 90 days
    for vpa in USERS:
        if vpa in FRESH_USERS:
            continue
        age = NOW - by_vpa[vpa]["created_at_ms"]
        assert 2 * 86_400_000 <= age <= 90 * 86_400_000

    # devices + IPs
    device_hashes = {p["hash"] for c, p in calls if "MERGE (d:Device" in c}
    assert device_hashes == {user_device(u) for u in USERS} | {DEVICE_RING_A, DEVICE_RING_B}
    ips = {p["ip"] for c, p in calls if "MERGE (i:IPAddress" in c}
    assert ips == {user_ip(u) for u in USERS} | {BAD_IP, CASHOUT_IP}
    merchants = {p["mid"] for c, p in calls if "MERGE (m:Merchant" in c}
    assert merchants == set(MERCHANTS)

    # every edge statement is a MERGE (idempotent) — never a bare CREATE
    edge_calls = [(c, p) for c, p in calls if c.startswith("MATCH")]
    assert edge_calls
    assert all("MERGE (a)-[" in c for c, _ in edge_calls)

    # attack edges: exact amounts + fast-pass timing
    transfers = [p for c, p in edge_calls if "MERGE (a)-[t:TRANSFERRED]" in c]
    t0 = NOW - 20 * 60_000
    fast = [p for p in transfers if p["ts"] in (t0, t0 + 90_000, t0 + 180_000)]
    assert {(p["src"], p["dst"], p["amount"]) for p in fast} == {
        (USERS[4], MULES[0], ATTACK_AMOUNTS[0]),
        (MULES[0], MULES[1], ATTACK_AMOUNTS[1]),
        (MULES[1], "cashout01@cashdemo", ATTACK_AMOUNTS[2]),
    }
    fanin = [p for p in transfers if p["dst"] == MULES[3] and p["amount"] in FANIN_AMOUNTS]
    assert len(fanin) == 6
    assert all(0 < NOW - p["ts"] <= 30 * 60_000 for p in fanin)

    # deterministic + re-runnable: identical plan for an identical clock
    again = FakeDriver()
    seed_graph(again, now=NOW)
    assert again.session_obj.calls == calls
    assert stats == {"schema": len(SCHEMA_STATEMENTS), "nodes": stats["nodes"],
                     "edges": stats["edges"]}


def test_attack_edges_fast_pass_hops_within_window():
    fast_pass, fan_in = attack_edges(NOW)
    assert len(fast_pass) == 3
    deltas = [fast_pass[i + 1]["ts"] - fast_pass[i]["ts"] for i in range(2)]
    assert deltas == [90_000, 90_000]
    assert all(d <= 300_000 for d in deltas)
    assert len(fan_in) == 7  # 6 victims + mule04 -> cashout02
    assert all(0 < NOW - e["ts"] <= 30 * 60_000 for e in fan_in)


def test_baseline_history_is_past_7d_and_deterministic():
    a1, b1 = baseline_history(NOW)
    a2, b2 = baseline_history(NOW)
    assert (a1, b1) == (a2, b2)
    for e in a1 + b1:
        assert 0 < NOW - e["ts"] <= 7 * 86_400_000
        assert e["src"] not in FRESH_USERS
    # no self loops
    assert all(e["src"] != e["dst"] for e in a1)


# ---------------------------------------------------------------------------
# Redis / Dragonfly
# ---------------------------------------------------------------------------


def test_redis_key_scheme_matches_feature_worker():
    client = FakeRedis()
    seed_redis(client, now=NOW)

    # exact keys from the REDIS KEY SCHEME
    assert client.strings["vel:cnt:1h:user05@upidemo"] == "3"
    assert client.ttls["vel:cnt:1h:user05@upidemo"] == 3600
    assert float(client.strings["vel:amt:1h:user05@upidemo"]) == 49_500.0
    assert client.ttls["vel:amt:1h:user05@upidemo"] == 3600
    assert client.ttls["vel:cnt:24h:user05@upidemo"] == 86_400
    assert client.ttls["vel:amt:24h:user05@upidemo"] == 86_400
    assert client.ttls["vel:cnt:1h:mule04@muledemo"] == 3600

    # SET-typed keys
    assert client.ttls["vel:dst:1h:mule04@muledemo"] == 3600
    assert client.sets["vel:dstsnd:1h:mule04@muledemo"] == set(FANIN_VPAS)
    assert client.ttls["vel:dstsnd:1h:mule04@muledemo"] == 3600
    assert "user05@upidemo" in client.sets["vel:dstsnd:1h:mule01@muledemo"]

    assert client.strings["vel:dev:1h:dev-ring-a"] == "9"
    assert client.ttls["vel:dev:1h:dev-ring-a"] == 3600
    assert client.strings["vel:ip:1h:10.66.0.1"] == "12"
    assert client.ttls["vel:ip:1h:10.66.0.1"] == 3600

    # dev:first SET-NX (unix seconds)
    assert client.strings["dev:first:dev-ring-a"].isdigit()
    assert int(client.strings["dev:first:dev-ring-a"]) == NOW // 1000

    # geo + history lists (head-first: lat, lon, timestamp_ms)
    geo = client.lists["geo:last:user05@upidemo"]
    assert len(geo) == 3
    assert float(geo[2]) == float(NOW)  # timestamp_ms last
    hist = client.lists["hist:amt:user05@upidemo"]
    assert len(hist) <= 100 and "48000.0" in hist

    # dashboard counters
    assert set(client.hashes["stats:actions"]) == {"ALLOW", "CHALLENGE", "BLOCK"}


def test_redis_seeding_is_idempotent():
    client = FakeRedis()
    seed_redis(client, now=NOW)
    snapshot = (dict(client.strings), {k: set(v) for k, v in client.sets.items()},
                dict(client.lists), {k: dict(v) for k, v in client.hashes.items()})
    seed_redis(client, now=NOW)
    assert client.strings == snapshot[0]  # delta-INCR converges to the same values
    assert {k: set(v) for k, v in client.sets.items()} == snapshot[1]
    assert client.hashes == snapshot[3]  # HSETNX never overwrites
    # lists are only initialised when absent
    assert client.lists == snapshot[2]


def test_no_per_transaction_keys_are_seeded():
    """decision:/features:/subgraph:/expl: are runtime-owned (feature-worker)."""
    client = FakeRedis()
    seed_redis(client, now=NOW)
    seeded = set(client.strings) | set(client.sets) | set(client.lists) | set(client.hashes)
    assert not any(k.startswith(("decision:", "features:", "subgraph:", "expl:",
                                 "decisions:")) for k in seeded)
