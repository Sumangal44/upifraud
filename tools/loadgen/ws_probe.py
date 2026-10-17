"""WebSocket alert probe (used by scripts/e2e.sh).

Connects to stream-api's alert socket and waits for the first alert frame.
Run inside the loadgen container:

    docker compose --profile tools run -d --no-deps \
        --entrypoint python loadgen -m tools.loadgen.ws_probe

Prints `WS_ALERT_OK <tx_id> <action>` and exits 0 on success,
`WS_ALERT_TIMEOUT` / `WS_ALERT_FAIL` and exits non-zero otherwise.
"""

import asyncio
import json
import os
import sys

try:
    import websockets
except ImportError:  # pragma: no cover - dependency is in tools/requirements.txt
    print("WS_ALERT_FAIL missing 'websockets' package", flush=True)
    sys.exit(2)

WS_URL = os.environ.get("STREAM_API_WS_URL", "ws://stream-api:8081/ws/alerts")
TIMEOUT_S = float(os.environ.get("WS_PROBE_TIMEOUT_S", "90"))


async def main() -> int:
    try:
        async with websockets.connect(WS_URL, open_timeout=15) as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), 15))
            if hello.get("type") != "hello":
                print(f"WS_ALERT_FAIL unexpected hello frame: {hello}", flush=True)
                return 1
            print("WS_CONNECTED", flush=True)
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), TIMEOUT_S))
                if msg.get("type") == "alert":
                    data = msg.get("data", {})
                    print(
                        "WS_ALERT_OK",
                        data.get("tx_id"),
                        data.get("action"),
                        data.get("score"),
                        flush=True,
                    )
                    return 0
    except asyncio.TimeoutError:
        print("WS_ALERT_TIMEOUT no alert frame received", flush=True)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"WS_ALERT_FAIL {type(exc).__name__}: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
