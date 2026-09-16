"""CLI entry — `fingerprint-simulator-service` runs `serve()` on $PORT (default 50051).

Kept as a thin wrapper so the entry-point shim from pyproject.toml has a
stable target and the `python -m fingerprint_simulator_service` invocation
works the same way in dev, Docker, and CI smoke tests.
"""

from __future__ import annotations

import os

from .server import serve


def main() -> None:
    serve(int(os.environ.get("PORT", "50051")))


if __name__ == "__main__":
    main()
