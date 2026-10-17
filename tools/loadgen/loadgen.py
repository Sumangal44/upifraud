"""Async load generator: synthetic UPI traffic against the gateway.

    PYTHONPATH=. python -m tools.loadgen.loadgen --tps 20 --fraud-ratio 0.15
    make loadgen

Scenarios (weighted by `--fraud-ratio`; the demo VPAs/devices/IPs come from
`tools.demo`, matching what `tools.seed` wrote into the graph):

  1. normal          — user<->user / merchant transfers (the honest majority)
  2. mule_burst      — demo users paying mule01..03 using `dev-ring-a`
                       devices and the bad IP `10.66.0.1`
  3. geo_jump        — same user, cities ~1150 km apart within minutes
  4. rooted_device   — rooted device + high value (₹60k-90k)
  5. emulator        — emulator / screen-mirror environment
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import sys
import time
import uuid
from collections import deque
from typing import Any, Optional

import httpx

from tools.demo import (
    BAD_IP,
    CITIES,
    DEVICE_RING_A,
    GEO_JUMP_PAIR,
    MERCHANTS,
    MULES,
    USERS,
    user_city,
    user_device,
    user_ip,
)

DEFAULT_URL = os.environ.get("GATEWAY_URL", "http://localhost:8080")
DEFAULT_TPS = float(os.environ.get("LOADGEN_TPS", "20"))
DEFAULT_FRAUD_RATIO = float(os.environ.get("LOADGEN_FRAUD_RATIO", "0.15"))
ENDPOINT = "/v1/transactions"

#: (scenario, weight) — weights are normalised at pick time.
FRAUD_SCENARIOS: tuple[tuple[str, float], ...] = (
    ("mule_burst", 0.40),
    ("geo_jump", 0.25),
    ("rooted_device", 0.20),
    ("emulator", 0.15),
)

_NORMAL_NOTES = ["lunch split", "monthly rent", "cab fare", "gift for mom", None, None]
_APP_VERSION = "2.14.0"


# ---------------------------------------------------------------------------
# Event construction (pure given rng + state — unit-testable)
# ---------------------------------------------------------------------------


def weighted_choice(rng: random.Random, options: tuple[tuple[str, float], ...]) -> str:
    total = sum(w for _, w in options)
    pick = rng.random() * total
    acc = 0.0
    for name, weight in options:
        acc += weight
        if pick <= acc:
            return name
    return options[-1][0]


def pick_scenario(rng: random.Random, fraud_ratio: float) -> str:
    if rng.random() < fraud_ratio:
        return weighted_choice(rng, FRAUD_SCENARIOS)
    return "normal"


def _geo(city: str, rng: random.Random) -> dict[str, Any]:
    lat, lon = CITIES[city]
    return {
        "lat": round(lat + rng.uniform(-0.05, 0.05), 5),
        "lon": round(lon + rng.uniform(-0.05, 0.05), 5),
        "city": city,
    }


def _device(
    device_hash: str,
    rooted: bool = False,
    screen_mirror: bool = False,
    emulator: bool = False,
) -> dict[str, Any]:
    return {
        "device_hash": device_hash,
        "app_version": _APP_VERSION,
        "rooted": rooted,
        "screen_mirror": screen_mirror,
        "emulator": emulator,
    }


def _event(
    src_vpa: str,
    dst_vpa: str,
    amount: float,
    device: dict[str, Any],
    src_ip: str,
    geo: dict[str, Any],
    now_ms: int,
    merchant_id: Optional[str] = None,
    note: Optional[str] = None,
) -> dict[str, Any]:
    """A gateway `TransactionEvent` payload (snake_case wire contract)."""
    return {
        "tx_id": str(uuid.uuid4()),
        "timestamp_ms": now_ms,
        "src_vpa": src_vpa,
        "dst_vpa": dst_vpa,
        "amount_inr": float(amount),
        "device": device,
        "src_ip": src_ip,
        "geo": geo,
        "merchant_id": merchant_id,
        "note": note,
    }


def _other_user(rng: random.Random, src: str) -> str:
    dst = rng.choice(USERS)
    while dst == src:
        dst = rng.choice(USERS)
    return dst


def build_event(
    scenario: str,
    rng: random.Random,
    state: dict[str, Any],
    now_ms: Optional[int] = None,
) -> dict[str, Any]:
    """Build one transaction event for `scenario`.

    `state` carries cross-event memory (geo positions for the jump scenario).
    """
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    state.setdefault("geo", {})

    if scenario == "normal":
        src = rng.choice(USERS)
        if rng.random() < 0.25:  # merchant payment
            merchant = rng.choice(MERCHANTS)
            amount = round(rng.uniform(60.0, 2_500.0), 2)
            return _event(
                src, merchant, amount,
                _device(user_device(src)), user_ip(src),
                _geo(user_city(src), rng), now,
                merchant_id=merchant, note=rng.choice(_NORMAL_NOTES),
            )
        dst = _other_user(rng, src)
        amount = round(rng.uniform(100.0, 5_000.0), 2)
        if rng.random() < 0.1:
            amount = round(rng.uniform(5_000.0, 20_000.0), 2)
        return _event(
            src, dst, amount, _device(user_device(src)), user_ip(src),
            _geo(user_city(src), rng), now, note=rng.choice(_NORMAL_NOTES),
        )

    if scenario == "mule_burst":
        # demo user -> mule01..03, sent from the shared ring device + bad IP
        src = rng.choice(USERS)
        dst = rng.choice(MULES[0:3])
        amount = round(rng.uniform(5_000.0, 48_000.0), 2)
        return _event(
            src, dst, amount, _device(DEVICE_RING_A), BAD_IP,
            _geo(user_city(src), rng), now,
        )

    if scenario == "geo_jump":
        src = rng.choice(USERS)
        dst = _other_user(rng, src)
        # alternate between two cities ~1150 km apart, minutes apart
        last_city = state["geo"].get(src)
        home = user_city(src)
        if last_city is not None and last_city in GEO_JUMP_PAIR:
            city = next(c for c in GEO_JUMP_PAIR if c != last_city)
        else:
            city = next(c for c in GEO_JUMP_PAIR if c != home) if home in GEO_JUMP_PAIR else (
                GEO_JUMP_PAIR[1] if home == GEO_JUMP_PAIR[0] else GEO_JUMP_PAIR[0]
            )
        state["geo"][src] = city
        amount = round(rng.uniform(500.0, 15_000.0), 2)
        return _event(
            src, dst, amount, _device(user_device(src)), user_ip(src),
            _geo(city, rng), now,
        )

    if scenario == "rooted_device":
        src = rng.choice(USERS)
        dst = rng.choice([*USERS, *MULES])
        while dst == src:
            dst = rng.choice([*USERS, *MULES])
        amount = round(rng.uniform(60_000.0, 90_000.0), 2)
        idx = src.split("@")[0][-2:]
        return _event(
            src, dst, amount, _device(f"dev-rooted-{idx}", rooted=True),
            user_ip(src), _geo(user_city(src), rng), now,
        )

    if scenario == "emulator":
        src = rng.choice(USERS)
        dst = _other_user(rng, src)
        amount = round(rng.uniform(10_000.0, 30_000.0), 2)
        idx = src.split("@")[0][-2:]
        mirror = rng.random() < 0.5
        return _event(
            src, dst, amount,
            _device(f"dev-emu-{idx}", screen_mirror=mirror, emulator=not mirror),
            user_ip(src), _geo(user_city(src), rng), now,
        )

    raise ValueError(f"unknown scenario: {scenario!r}")


# ---------------------------------------------------------------------------
# Load loop
# ---------------------------------------------------------------------------


class Stats:
    def __init__(self) -> None:
        self.sent = 0
        self.ok = 0
        self.failures = 0
        self.latencies: deque[float] = deque(maxlen=20_000)
        self.errors: deque[str] = deque(maxlen=5)
        self.scenarios: dict[str, int] = {}
        self.started = time.monotonic()

    def record(self, scenario: str, latency_ms: float, error: Optional[str]) -> None:
        self.sent += 1
        self.scenarios[scenario] = self.scenarios.get(scenario, 0) + 1
        if error is None:
            self.ok += 1
            self.latencies.append(latency_ms)
        else:
            self.failures += 1
            self.errors.append(error)

    def percentiles(self) -> tuple[float, float]:
        values = sorted(self.latencies)
        if not values:
            return 0.0, 0.0
        p50 = values[int(0.50 * (len(values) - 1))]
        p95 = values[int(0.95 * (len(values) - 1))]
        return p50, p95

    def line(self) -> str:
        p50, p95 = self.percentiles()
        elapsed = time.monotonic() - self.started
        return (
            f"[loadgen] t={elapsed:6.1f}s sent={self.sent} ok={self.ok} "
            f"failures={self.failures} p50={p50:.1f}ms p95={p95:.1f}ms "
            f"scenarios={self.scenarios}"
        )


async def _print_stats(stats: Stats, every: float) -> None:
    try:
        while True:
            await asyncio.sleep(every)
            print(stats.line(), flush=True)
    except asyncio.CancelledError:
        pass


async def run_load(
    url: str,
    tps: float,
    fraud_ratio: float,
    duration: Optional[float],
    seed: Optional[int],
    path: str = ENDPOINT,
    print_every: float = 5.0,
    timeout: float = 5.0,
) -> Stats:
    """POST events at `tps` until Ctrl-C / `duration` seconds elapsed."""
    rng = random.Random(seed)
    state: dict[str, Any] = {}
    stats = Stats()
    interval = 1.0 / max(tps, 0.001)
    deadline = time.monotonic() + duration if duration else None

    async with httpx.AsyncClient(base_url=url, timeout=timeout) as client:
        printer = asyncio.create_task(_print_stats(stats, print_every))
        next_at = time.monotonic()
        try:
            while True:
                if deadline is not None and time.monotonic() >= deadline:
                    break
                scenario = pick_scenario(rng, fraud_ratio)
                event = build_event(scenario, rng, state)
                t0 = time.monotonic()
                error: Optional[str] = None
                try:
                    resp = await client.post(path, json=event)
                    if resp.status_code >= 400:
                        error = f"HTTP {resp.status_code}: {resp.text[:160]}"
                except httpx.HTTPError as exc:
                    error = f"{type(exc).__name__}: {exc}"
                stats.record(scenario, (time.monotonic() - t0) * 1000.0, error)

                next_at += interval
                delay = next_at - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                else:  # fell behind (slow endpoint) — reset pacing, don't burst
                    next_at = time.monotonic()
        finally:
            printer.cancel()
    return stats


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Synthetic UPI traffic generator")
    parser.add_argument("--url", default=DEFAULT_URL, help="gateway base URL")
    parser.add_argument("--path", default=ENDPOINT, help="ingestion path")
    parser.add_argument("--tps", type=float, default=DEFAULT_TPS, help="transactions per second")
    parser.add_argument("--fraud-ratio", type=float, default=DEFAULT_FRAUD_RATIO,
                        help="share of fraud-scenario events (0..1)")
    parser.add_argument("--duration", type=float, default=None,
                        help="seconds to run (default: until Ctrl-C)")
    parser.add_argument("--seed", type=int, default=None, help="rng seed for reproducible traffic")
    parser.add_argument("--print-every", type=float, default=5.0, help="stats interval (s)")
    args = parser.parse_args(argv)

    if not 0.0 <= args.fraud_ratio <= 1.0:
        parser.error("--fraud-ratio must be within [0, 1]")

    print(
        f"[loadgen] target={args.url}{args.path} tps={args.tps} "
        f"fraud_ratio={args.fraud_ratio} duration={args.duration or '∞'}"
    )
    stats: Optional[Stats] = None
    try:
        stats = asyncio.run(
            run_load(
                url=args.url,
                tps=args.tps,
                fraud_ratio=args.fraud_ratio,
                duration=args.duration,
                seed=args.seed,
                path=args.path,
                print_every=args.print_every,
            )
        )
    except KeyboardInterrupt:
        print("\n[loadgen] interrupted — final stats:")
    if stats is not None:
        print(stats.line())
        if stats.failures:
            print(f"[loadgen] last errors: {list(stats.errors)}")
    print("[loadgen] done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
