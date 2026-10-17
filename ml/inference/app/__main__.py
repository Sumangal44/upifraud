"""Entry point: `python -m ml.inference.app` (port INFERENCE_PORT, default 8001)."""

from __future__ import annotations

import os

import uvicorn

from ml.inference.app.main import app


def main() -> None:
    port = int(os.environ.get("INFERENCE_PORT", "8001"))
    host = os.environ.get("INFERENCE_HOST", "0.0.0.0")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
