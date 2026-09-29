"""CLI entry — `orchestrator-simulator-service` runs `serve()` on $PORT (default 50053).

Kept as a thin wrapper so the entry-point shim from pyproject.toml has a
stable target and `python -m orchestrator_simulator_service` works the
same way in dev, Docker, and CI smoke tests.
"""

from __future__ import annotations

import asyncio
import os

from .server import serve


def main() -> None:
    port = int(os.environ.get("PORT", "50053"))
    asyncio.run(serve(port))


if __name__ == "__main__":
    main()
