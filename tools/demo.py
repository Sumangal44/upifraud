"""Fixed demo entity set — shared by `tools.seed` and `tools.loadgen`.

Both sides MUST agree on these identifiers: the seeder writes them into
Memgraph/Dragonfly and the load generator emits transactions that reference
the same VPAs, device hashes and IP keys (that is what wires the graph +
heuristic detection paths together).
"""

from __future__ import annotations

# --- parties ---------------------------------------------------------------
USERS: list[str] = [f"user{i:02d}@upidemo" for i in range(1, 21)]
MULES: list[str] = [f"mule{i:02d}@muledemo" for i in range(1, 6)]
CASHOUTS: list[str] = [f"cashout{i:02d}@cashdemo" for i in range(1, 4)]
MERCHANTS: list[str] = [f"merchant{i:02d}@paydemo" for i in range(1, 6)]

#: created only 2h ago (fresh-account anomaly demo)
FRESH_USERS: tuple[str, str] = ("user19@upidemo", "user20@upidemo")

#: victim fan-in set for the mule04 ring
FANIN_VPAS: list[str] = USERS[6:12]  # user07..user12
FANIN_AMOUNTS: list[float] = [5_000.0, 8_000.0, 12_000.0, 15_000.0, 18_000.0, 20_000.0]

#: the fast-pass attack: victim -> mule01 -> mule02 -> cashout01
ATTACK_AMOUNTS: tuple[float, float, float] = (48_000.0, 47_500.0, 47_000.0)

# --- devices / IPs ---------------------------------------------------------
RING_A_MULES: list[str] = MULES[0:3]  # mule01, mule02, mule03
RING_B_MULES: list[str] = MULES[3:5]  # mule04, mule05
DEVICE_RING_A = "dev-ring-a"
DEVICE_RING_B = "dev-ring-b"
BAD_IP = "10.66.0.1"  # risk_score 0.9, used by every mule
CASHOUT_IP = "ip-cash-01"  # risk_score 0.7
BAD_IP_RISK = 0.9
CASHOUT_IP_RISK = 0.7

# --- geo (approx city centres) --------------------------------------------
CITIES: dict[str, tuple[float, float]] = {
    "Mumbai": (19.0760, 72.8777),
    "Delhi": (28.6139, 77.2090),
    "Bengaluru": (12.9716, 77.5946),
    "Chennai": (13.0827, 80.2707),
    "Kolkata": (22.5726, 88.3639),
    "Hyderabad": (17.3850, 78.4867),
}
CITY_NAMES: list[str] = list(CITIES)
#: two cities ~1150 km apart — used by the geo-jump scenario
GEO_JUMP_PAIR: tuple[str, str] = ("Mumbai", "Delhi")


def user_local(vpa: str) -> str:
    """`user07@upidemo` -> `user07`."""
    return vpa.split("@", 1)[0]


def user_index(vpa: str) -> int:
    """`user07@upidemo` -> 7."""
    return int(user_local(vpa)[4:])


def user_device(vpa: str) -> str:
    """`user07@upidemo` -> `dev-user07`."""
    return f"dev-{user_local(vpa)}"


def user_ip(vpa: str) -> str:
    """`user07@upidemo` -> `ip-user07`."""
    return f"ip-{user_local(vpa)}"


def user_ip_risk(vpa: str) -> float:
    """Deterministic per-user IP reputation in 0.05..0.20."""
    return round(0.05 + ((user_index(vpa) - 1) % 4) * 0.05, 2)


def user_city(vpa: str) -> str:
    """Deterministic home city for a demo user."""
    return CITY_NAMES[(user_index(vpa) - 1) % len(CITY_NAMES)]
