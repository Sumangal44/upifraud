"""Idempotent demo-data seeder: Memgraph (graph) + Dragonfly (velocities).

    PYTHONPATH=. python -m tools.seed.seed
    make seed                    # same, inside docker compose

Behaviour:

* waits for Memgraph (`bolt://MEMGRAPH_HOST:MEMGRAPH_PORT`) and Dragonfly
  (`DRAGONFLY_HOST:DRAGONFLY_PORT`) with a retry loop before doing anything;
* applies `infra/memgraph/schema.cypher` semantics (constraints/indexes),
  tolerating "already exists" errors — safe to re-run;
* MERGEs the fixed demo entity set (shared with the load generator via
  `tools.demo`) plus a real fast-pass mule path and a 6-way fan-in;
* seeds Dragonfly velocity keys with the exact feature-worker key scheme
  (delta-INCR so re-runs converge to the same values) and prints a summary
  of the attack ring + how to trigger it with the load generator.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
from typing import Any, Optional

from tools.demo import (
    BAD_IP,
    BAD_IP_RISK,
    CASHOUTS,
    CASHOUT_IP,
    CASHOUT_IP_RISK,
    CITIES,
    DEVICE_RING_A,
    DEVICE_RING_B,
    FANIN_AMOUNTS,
    FANIN_VPAS,
    FRESH_USERS,
    MERCHANTS,
    MULES,
    RING_A_MULES,
    RING_B_MULES,
    USERS,
    ATTACK_AMOUNTS,
    user_city,
    user_device,
    user_ip,
    user_ip_risk,
)

DAY_MS = 86_400_000
MIN_MS = 60_000
BASELINE_SEED = 1701  # deterministic baseline history

# ---------------------------------------------------------------------------
# Schema (mirrors infra/memgraph/schema.cypher)
# ---------------------------------------------------------------------------

SCHEMA_STATEMENTS: list[str] = [
    "CREATE CONSTRAINT ON (a:Account) ASSERT a.vpa_id IS UNIQUE;",
    "CREATE CONSTRAINT ON (d:Device) ASSERT d.device_hash IS UNIQUE;",
    "CREATE CONSTRAINT ON (i:IPAddress) ASSERT i.ip_address IS UNIQUE;",
    "CREATE CONSTRAINT ON (m:Merchant) ASSERT m.merchant_id IS UNIQUE;",
    "CREATE INDEX ON :Account(created_at_ms);",
    "CREATE INDEX ON :TRANSFERRED(timestamp_ms);",
    "CREATE INDEX ON :Device(created_at_ms);",
]

# --- node upserts ----------------------------------------------------------
_MERGE_ACCOUNT = """
MERGE (a:Account {vpa_id: $vpa})
SET a.created_at_ms = $created_at_ms, a.risk_flags = $risk_flags
"""
_MERGE_MERCHANT = """
MERGE (m:Merchant {merchant_id: $mid})
SET m.created_at_ms = $created_at_ms
"""
_MERGE_DEVICE = """
MERGE (d:Device {device_hash: $hash})
SET d.created_at_ms = $created_at_ms, d.rooted = $rooted
"""
_MERGE_IP = """
MERGE (i:IPAddress {ip_address: $ip})
SET i.risk_score = $risk_score
"""

# --- edge upserts ----------------------------------------------------------
_LINK_TRANSFERRED = """
MATCH (a:Account {vpa_id: $src}), (b:Account {vpa_id: $dst})
MERGE (a)-[t:TRANSFERRED]->(b)
SET t.amount = $amount, t.timestamp_ms = $ts, t.latency_ms = $latency
"""
_LINK_PAID_MERCHANT = """
MATCH (a:Account {vpa_id: $src}), (m:Merchant {merchant_id: $mid})
MERGE (a)-[p:PAID_MERCHANT]->(m)
SET p.amount = $amount, p.timestamp_ms = $ts
"""
_LINK_USED_DEVICE = """
MATCH (a:Account {vpa_id: $vpa}), (d:Device {device_hash: $hash})
MERGE (a)-[u:USED_DEVICE]->(d)
SET u.last_active = $ts, u.timestamp_ms = $ts
"""
_LINK_CONNECTED_FROM = """
MATCH (a:Account {vpa_id: $vpa}), (i:IPAddress {ip_address: $ip})
MERGE (a)-[c:CONNECTED_FROM]->(i)
SET c.timestamp_ms = $ts
"""


def now_ms() -> int:
    return int(time.time() * 1000)


def _tolerate_exists(exc: Exception, stmt: str) -> None:
    """Re-raise unless the error is the benign 'already exists' kind."""
    msg = str(exc).lower()
    if "already" in msg or "exist" in msg:
        return
    raise exc from None


# ---------------------------------------------------------------------------
# Graph seeding
# ---------------------------------------------------------------------------


def baseline_history(now: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Deterministic past-7d activity: p2p transfers + merchant payments."""
    rng = random.Random(BASELINE_SEED)
    ordinary = [u for u in USERS if u not in FRESH_USERS]
    p2p: list[dict[str, Any]] = []
    paid: list[dict[str, Any]] = []
    for u in ordinary:
        for _ in range(rng.randint(2, 3)):
            dst = rng.choice(ordinary)
            while dst == u:
                dst = rng.choice(ordinary)
            p2p.append(
                {
                    "src": u,
                    "dst": dst,
                    "amount": round(rng.uniform(120.0, 4_800.0), 2),
                    "ts": now - rng.randint(10 * MIN_MS, 7 * DAY_MS),
                    "latency": rng.randint(25, 180),
                }
            )
        for _ in range(rng.randint(1, 2)):
            paid.append(
                {
                    "src": u,
                    "mid": rng.choice(MERCHANTS),
                    "amount": round(rng.uniform(60.0, 1_500.0), 2),
                    "ts": now - rng.randint(10 * MIN_MS, 7 * DAY_MS),
                }
            )
    return p2p, paid


def attack_edges(now: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Today's attack: fast-pass ring + mule04 fan-in (times relative to now)."""
    t0 = now - 20 * MIN_MS  # fast-pass path, 90 s hops
    fast_pass = [
        {"src": USERS[4], "dst": MULES[0], "amount": ATTACK_AMOUNTS[0], "ts": t0, "latency": 42},
        {"src": MULES[0], "dst": MULES[1], "amount": ATTACK_AMOUNTS[1],
         "ts": t0 + 90_000, "latency": 35},
        {"src": MULES[1], "dst": CASHOUTS[0], "amount": ATTACK_AMOUNTS[2],
         "ts": t0 + 180_000, "latency": 28},
    ]
    fan_in: list[dict[str, Any]] = []
    for i, (vpa, amount) in enumerate(zip(FANIN_VPAS, FANIN_AMOUNTS)):
        fan_in.append(
            {
                "src": vpa,
                "dst": MULES[3],  # mule04
                "amount": amount,
                "ts": now - (30 - i * 4) * MIN_MS,  # within the last 30 min
                "latency": 30 + (i * 7) % 50,
            }
        )
    fan_in.append(
        {
            "src": MULES[3],  # mule04 -> cashout02 pass-through
            "dst": CASHOUTS[1],
            "amount": round(sum(FANIN_AMOUNTS) * 0.98, 2),
            "ts": now - 8 * MIN_MS,
            "latency": 31,
        }
    )
    return fast_pass, fan_in


def seed_graph(driver: Any, now: Optional[int] = None) -> dict[str, int]:
    """Apply schema + demo entities/edges (idempotent MERGEs)."""
    now = now if now is not None else now_ms()
    stats = {"schema": 0, "nodes": 0, "edges": 0}

    with driver.session() as s:
        # --- schema (idempotent) ---
        for stmt in SCHEMA_STATEMENTS:
            try:
                s.run(stmt)
            except Exception as exc:  # noqa: BLE001 - driver-specific error types
                _tolerate_exists(exc, stmt)  # re-raises unless benign
            stats["schema"] += 1  # applied or already present — both fine

        # --- accounts ---
        for i, vpa in enumerate(USERS, start=1):
            if vpa in FRESH_USERS:
                created = now - 2 * 3_600_000  # 2h ago
            else:
                created = now - ((i * 13) % 88 + 2) * DAY_MS  # 2..89 days ago
            s.run(_MERGE_ACCOUNT, vpa=vpa, created_at_ms=created, risk_flags=[])
            stats["nodes"] += 1
        for vpa in MULES:
            s.run(
                _MERGE_ACCOUNT,
                vpa=vpa,
                created_at_ms=now - 30 * 3_600_000,  # < 48h
                risk_flags=["mule_suspect"],
            )
            stats["nodes"] += 1
        for vpa in CASHOUTS:
            s.run(
                _MERGE_ACCOUNT,
                vpa=vpa,
                created_at_ms=now - 36 * 3_600_000,  # < 48h
                risk_flags=["cashout"],
            )
            stats["nodes"] += 1

        # --- merchants / devices / IPs ---
        for mid in MERCHANTS:
            s.run(_MERGE_MERCHANT, mid=mid, created_at_ms=now - 120 * DAY_MS)
            stats["nodes"] += 1
        for vpa in USERS:
            s.run(
                _MERGE_DEVICE,
                hash=user_device(vpa),
                created_at_ms=now - 60 * DAY_MS if vpa not in FRESH_USERS else now - 2 * 3_600_000,
                rooted=False,
            )
            s.run(_MERGE_IP, ip=user_ip(vpa), risk_score=user_ip_risk(vpa))
            stats["nodes"] += 2
        for ring_device in (DEVICE_RING_A, DEVICE_RING_B):
            s.run(_MERGE_DEVICE, hash=ring_device, created_at_ms=now - 40 * 3_600_000, rooted=False)
            stats["nodes"] += 1
        s.run(_MERGE_IP, ip=BAD_IP, risk_score=BAD_IP_RISK)
        s.run(_MERGE_IP, ip=CASHOUT_IP, risk_score=CASHOUT_IP_RISK)
        stats["nodes"] += 2

        # --- structural edges ---
        for vpa in USERS:
            s.run(_LINK_USED_DEVICE, vpa=vpa, hash=user_device(vpa), ts=now - 60 * MIN_MS)
            s.run(_LINK_CONNECTED_FROM, vpa=vpa, ip=user_ip(vpa), ts=now - 60 * MIN_MS)
            stats["edges"] += 2
        for vpa in RING_A_MULES:
            s.run(_LINK_USED_DEVICE, vpa=vpa, hash=DEVICE_RING_A, ts=now - 5 * MIN_MS)
            s.run(_LINK_CONNECTED_FROM, vpa=vpa, ip=BAD_IP, ts=now - 5 * MIN_MS)
            stats["edges"] += 2
        for vpa in RING_B_MULES:
            s.run(_LINK_USED_DEVICE, vpa=vpa, hash=DEVICE_RING_B, ts=now - 7 * MIN_MS)
            s.run(_LINK_CONNECTED_FROM, vpa=vpa, ip=BAD_IP, ts=now - 7 * MIN_MS)
            stats["edges"] += 2
        for vpa in CASHOUTS:
            s.run(_LINK_CONNECTED_FROM, vpa=vpa, ip=CASHOUT_IP, ts=now - 6 * MIN_MS)
            stats["edges"] += 1

        # --- baseline history (past 7d) ---
        p2p, paid = baseline_history(now)
        for e in p2p:
            s.run(_LINK_TRANSFERRED, src=e["src"], dst=e["dst"], amount=e["amount"],
                  ts=e["ts"], latency=e["latency"])
            stats["edges"] += 1
        for e in paid:
            s.run(_LINK_PAID_MERCHANT, src=e["src"], mid=e["mid"], amount=e["amount"], ts=e["ts"])
            stats["edges"] += 1
        # a few deterministic merchant payments for the demo users
        for vpa, mid, amount in (
            (USERS[0], MERCHANTS[0], 499.0),
            (USERS[4], MERCHANTS[2], 1_299.0),
            (USERS[11], MERCHANTS[1], 849.0),
        ):
            s.run(_LINK_PAID_MERCHANT, src=vpa, mid=mid, amount=amount, ts=now - 3 * DAY_MS)
            stats["edges"] += 1

        # --- today's attack ---
        fast_pass, fan_in = attack_edges(now)
        for e in fast_pass + fan_in:
            s.run(_LINK_TRANSFERRED, src=e["src"], dst=e["dst"], amount=e["amount"],
                  ts=e["ts"], latency=e["latency"])
            stats["edges"] += 1

    return stats


# ---------------------------------------------------------------------------
# Dragonfly / Redis seeding (exact feature-worker key scheme)
# ---------------------------------------------------------------------------

TTL_1H = 3600
TTL_24H = 86400


def seed_redis(client: Any, now: Optional[int] = None) -> dict[str, int]:
    """Seed velocity keys for the demo users/ring (delta-INCR = idempotent)."""
    now = now if now is not None else now_ms()
    counts = {"counters": 0, "sets": 0, "lists": 0, "hashes": 0, "firsts": 0}

    def set_int(key: str, target: int, ttl: int) -> None:
        cur = int(client.get(key) or 0)
        delta = int(target) - cur
        if delta:
            client.incrby(key, delta)
        client.expire(key, ttl)
        counts["counters"] += 1

    def set_float(key: str, target: float, ttl: int) -> None:
        cur = float(client.get(key) or 0.0)
        delta = float(target) - cur
        if abs(delta) > 1e-9:
            client.incrbyfloat(key, delta)
        client.expire(key, ttl)
        counts["counters"] += 1

    def sadd(key: str, members: list[str], ttl: int) -> None:
        client.sadd(key, *members)
        client.expire(key, ttl)
        counts["sets"] += 1

    # --- user05: the fast-pass victim ---
    set_int(f"vel:cnt:1h:{USERS[4]}", 3, TTL_1H)
    set_float(f"vel:amt:1h:{USERS[4]}", 49_500.0, TTL_1H)
    set_int(f"vel:cnt:24h:{USERS[4]}", 6, TTL_24H)
    set_float(f"vel:amt:24h:{USERS[4]}", 62_000.0, TTL_24H)
    set_int(f"vel:dev:1h:{user_device(USERS[4])}", 3, TTL_1H)
    set_int(f"vel:ip:1h:{user_ip(USERS[4])}", 3, TTL_1H)

    # --- fan-in victims (user07..user12 -> mule04) ---
    for vpa, amount in zip(FANIN_VPAS, FANIN_AMOUNTS):
        set_int(f"vel:cnt:1h:{vpa}", 2, TTL_1H)
        set_float(f"vel:amt:1h:{vpa}", amount + 700.0, TTL_1H)
        set_int(f"vel:cnt:24h:{vpa}", 4, TTL_24H)
        set_float(f"vel:amt:24h:{vpa}", amount + 3_100.0, TTL_24H)

    # --- mules: destination velocity + shared device/IP ---
    set_int(f"vel:cnt:1h:{MULES[3]}", 7, TTL_1H)  # mule04 receives 6 + forwards
    set_float(f"vel:amt:1h:{MULES[3]}", 78_000.0, TTL_1H)
    set_int(f"vel:dst:1h:{MULES[3]}", 7, TTL_1H)
    sadd(f"vel:dstsnd:1h:{MULES[3]}", list(FANIN_VPAS), TTL_1H)
    set_int(f"vel:cnt:1h:{MULES[0]}", 4, TTL_1H)  # mule01: receives + passes on
    set_int(f"vel:dst:1h:{MULES[0]}", 2, TTL_1H)
    sadd(f"vel:dstsnd:1h:{MULES[0]}", [USERS[4], MULES[1]], TTL_1H)
    set_int(f"vel:dev:1h:{DEVICE_RING_A}", 9, TTL_1H)
    set_int(f"vel:ip:1h:{BAD_IP}", 12, TTL_1H)

    # --- device first-seen (SET-NX, unix seconds) ---
    for dev in (DEVICE_RING_A, DEVICE_RING_B, user_device(USERS[4])):
        if client.set(f"dev:first:{dev}", int(now / 1000), nx=True) is not None:
            counts["firsts"] += 1

    # --- geo + amount history for a few users (only if absent) ---
    for vpa in (USERS[4], *FANIN_VPAS):
        geo_key = f"geo:last:{vpa}"
        if not client.exists(geo_key):
            lat, lon = CITIES[user_city(vpa)]
            client.lpush(geo_key, now, lon, lat)  # list reads head-first: lat, lon, ts
            counts["lists"] += 1
        hist_key = f"hist:amt:{vpa}"
        if not client.exists(hist_key):
            client.lpush(hist_key, *[float(v) for v in reversed([500, 990, 1_250, 48_000, 750])])
            client.ltrim(hist_key, 0, 99)
            counts["lists"] += 1

    # --- dashboard counters (HSETNX: only initialise when absent) ---
    for field, value in (("ALLOW", 1_240), ("CHALLENGE", 96), ("BLOCK", 14)):
        if client.hsetnx("stats:actions", field, value):
            counts["hashes"] += 1

    return counts


# ---------------------------------------------------------------------------
# Service wait-loops
# ---------------------------------------------------------------------------


def wait_for_graph(host: str, port: int, wait_s: float) -> Any:
    from neo4j import GraphDatabase

    deadline = time.time() + wait_s
    delay = 1.0
    last: Optional[Exception] = None
    while time.time() < deadline:
        driver = GraphDatabase.driver(f"bolt://{host}:{port}", auth=("", ""))
        try:
            with driver.session() as s:
                s.run("RETURN 1").consume()
            return driver
        except Exception as exc:  # connection refused while containers boot
            last = exc
            try:
                driver.close()
            except Exception:  # pragma: no cover
                pass
            print(f"[seed] waiting for memgraph at {host}:{port} ({exc.__class__.__name__})")
            time.sleep(delay)
            delay = min(delay * 1.5, 5.0)
    raise RuntimeError(f"memgraph unreachable at {host}:{port} after {wait_s:.0f}s: {last}")


def wait_for_redis(host: str, port: int, wait_s: float) -> Any:
    import redis as redis_lib

    deadline = time.time() + wait_s
    delay = 1.0
    last: Optional[Exception] = None
    while time.time() < deadline:
        client = redis_lib.Redis(host=host, port=port, decode_responses=True)
        try:
            client.ping()
            return client
        except Exception as exc:
            last = exc
            print(f"[seed] waiting for dragonfly at {host}:{port} ({exc.__class__.__name__})")
            time.sleep(delay)
            delay = min(delay * 1.5, 5.0)
    raise RuntimeError(f"dragonfly unreachable at {host}:{port} after {wait_s:.0f}s: {last}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _print_summary(graph: Optional[dict[str, int]], redis_stats: Optional[dict[str, int]]) -> None:
    print()
    print("=== upifraud seed summary ===")
    if graph is not None:
        print(
            f"memgraph : schema stmts={graph['schema']} nodes~={graph['nodes']} "
            f"edges~={graph['edges']} (MERGE-idempotent)"
        )
    if redis_stats is not None:
        print(f"dragonfly: {redis_stats}")
    print()
    print("Seeded attack ring (trigger it with the load generator):")
    print(
        "  FAST-PASS : {u} --{a0:,.0f}--> {m1} --{a1:,.0f}--> {m2} --{a2:,.0f}--> {c}".format(
            u=USERS[4], a0=ATTACK_AMOUNTS[0], m1=MULES[0], a1=ATTACK_AMOUNTS[1],
            m2=MULES[1], a2=ATTACK_AMOUNTS[2], c=CASHOUTS[0],
        )
    )
    print("             (t0 = now-20min, 90s hops — graph fast-pass window is 300s)")
    print(
        "  FAN-IN    : {v0}..{v1} --> {m} (₹5k-20k each, last 30 min) --> {c}".format(
            v0=FANIN_VPAS[0], v1=FANIN_VPAS[-1], m=MULES[3], c=CASHOUTS[1],
        )
    )
    print(f"  DEVICES   : {DEVICE_RING_A} (mule01-03), {DEVICE_RING_B} (mule04-05)")
    print(f"  BAD IP    : {BAD_IP} (risk {BAD_IP_RISK}), {CASHOUT_IP} (risk {CASHOUT_IP_RISK})")
    print()
    print("Trigger traffic:  PYTHONPATH=. python -m tools.loadgen.loadgen --tps 20")
    print("              or: make loadgen")
    print(f"Fresh accounts  : {FRESH_USERS[0]}, {FRESH_USERS[1]} (created 2h ago)")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Seed Memgraph + Dragonfly demo data")
    parser.add_argument("--memgraph-host", default=os.environ.get("MEMGRAPH_HOST", "localhost"))
    parser.add_argument("--memgraph-port", type=int, default=int(os.environ.get("MEMGRAPH_PORT", "7687")))
    parser.add_argument("--dragonfly-host", default=os.environ.get("DRAGONFLY_HOST", "localhost"))
    parser.add_argument("--dragonfly-port", type=int, default=int(os.environ.get("DRAGONFLY_PORT", "6379")))
    parser.add_argument("--wait", type=float, default=float(os.environ.get("SEED_WAIT_SECONDS", "60")),
                        help="seconds to wait for services before failing")
    parser.add_argument("--no-graph", action="store_true", help="skip Memgraph")
    parser.add_argument("--no-redis", action="store_true", help="skip Dragonfly")
    args = parser.parse_args(argv)

    if args.no_graph and args.no_redis:
        print("[seed] nothing to do (--no-graph and --no-redis both set)")
        return 0

    graph_stats: Optional[dict[str, int]] = None
    redis_stats: Optional[dict[str, int]] = None

    if not args.no_graph:
        print(f"[seed] connecting to memgraph bolt://{args.memgraph_host}:{args.memgraph_port}")
        driver = wait_for_graph(args.memgraph_host, args.memgraph_port, args.wait)
        try:
            graph_stats = seed_graph(driver)
        finally:
            driver.close()

    if not args.no_redis:
        print(f"[seed] connecting to dragonfly {args.dragonfly_host}:{args.dragonfly_port}")
        client = wait_for_redis(args.dragonfly_host, args.dragonfly_port, args.wait)
        try:
            redis_stats = seed_redis(client)
        finally:
            try:
                client.close()
            except Exception:  # pragma: no cover
                pass

    _print_summary(graph_stats, redis_stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
