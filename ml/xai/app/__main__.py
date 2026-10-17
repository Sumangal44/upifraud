"""Entry point: `python -m ml.xai.app` (port XAI_PORT, default 8002)."""

from __future__ import annotations

import os

import uvicorn

from ml.xai.app.main import app


def main() -> None:
    port = int(os.environ.get("XAI_PORT", "8002"))
    host = os.environ.get("XAI_HOST", "0.0.0.0")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
