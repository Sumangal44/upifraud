"""Pytest bootstrap: make the repo root importable so tests can
`import ml.common...` / `import tools...` regardless of invocation dir."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
