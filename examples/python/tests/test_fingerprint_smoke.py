"""End-to-end smoke test for the Python fingerprint example.

Spawns the fingerprint-simulator-service (canonical location:
`examples/simulator/fingerprint/`) and runs
examples/python/fingerprint_client.py against it. The client falls back
to streaming 5 s of silence when examples/audio/ is empty (always the
case in CI), so this test exercises the full proto + gRPC wire path
without any audio fixtures.

Catches anything that breaks the fingerprint example: proto field
renames, message removals, RPC name changes, generated stub API shifts,
simulator impl bugs, drift in the client's print format.

The simulator lives at examples/simulator/fingerprint/ — no scenarios,
no YAML (embeddings are deterministic sha256-seeded synthetic vectors).
This test spawns it as a subprocess — no Docker required.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

EXAMPLES_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = EXAMPLES_DIR.parent.parent
SIMULATOR_SRC = REPO_ROOT / "examples" / "simulator" / "fingerprint" / "src"


def _free_port() -> int:
    """Ask the OS for an unused TCP port. See test_smoke.py for the
    tiny TOCTOU-window rationale — same shape, same trade-off."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("localhost", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


@pytest.fixture
def env() -> dict[str, str]:
    """Environment with PYTHONPATH covering the generated stubs + example
    dir + the fingerprint simulator package src/ (so `python -m
    fingerprint_simulator_service` resolves without a full `uv sync` in CI).
    """
    return {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [
                str(REPO_ROOT / "gen" / "py"),
                str(EXAMPLES_DIR),
                str(SIMULATOR_SRC),
                os.environ.get("PYTHONPATH", ""),
            ]
        ),
    }


@pytest.fixture
def server(env: dict[str, str]):
    """Spawn the fingerprint-simulator-service on a free port and tear it
    down at the end."""
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "fingerprint_simulator_service"],
        env={**env, "PORT": str(port)},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if not _wait_for_port(port):
        proc.terminate()
        out = proc.stdout.read() if proc.stdout else ""
        pytest.fail(f"Server didn't bind on :{port} within 15 s. Output:\n{out}")
    try:
        yield proc, port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _run_client(port: int, env: dict[str, str], audio_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable, str(EXAMPLES_DIR / "fingerprint_client.py"),
            "--target", f"localhost:{port}",
            "--audio-dir", str(audio_dir),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_client_silence_roundtrip(server, env: dict[str, str], tmp_path):
    """The stub client streams 5 s of silence and prints session /
    embedding / final lines. Points the client at an empty tmp dir so
    the silence-fallback path runs deterministically regardless of
    whatever WAVs the dev has dropped into examples/audio/ locally.
    """
    _, port = server
    result = _run_client(port, env, tmp_path)
    assert result.returncode == 0, f"client failed: stderr={result.stderr}"
    assert "Session:" in result.stdout, f"missing session line in:\n{result.stdout}"
    # Simulator issues per-session ids like 'sim-<8 hex>' — matches the
    # deepfake sim's convention so grep patterns work across both.
    assert "sim-" in result.stdout, f"missing simulator session id prefix in:\n{result.stdout}"
    # At least one Embedding line — 10 × 500 ms silence chunks fills one
    # 5000 ms window, so exactly one EmbeddingResult should fire.
    assert re.search(
        r"Embedding \| offset=0ms \| duration=5000ms \| code=[0-9a-f]{16} \| dim=768 \| head=[0-9a-f]{16}",
        result.stdout,
    ), f"missing / malformed Embedding line in:\n{result.stdout}"
    # FinalResult with the counters we expect from the silence path.
    assert re.search(
        r"FINAL\s+\| total=5000ms \| embeddings=1", result.stdout,
    ), f"missing / malformed FINAL line in:\n{result.stdout}"


def test_embedding_is_deterministic_across_runs(server, env: dict[str, str], tmp_path):
    """The fingerprint sim derives each embedding from
    sha256(payload[:64]) → seeded PRNG → L2-normalise, so identical
    input MUST produce an identical fingerprint_code across runs.

    This is the load-bearing invariant that makes the sim useful for
    smoke tests + golden-fixture diffs. If it breaks (e.g. a refactor
    swaps the seed source or the normaliser), this test catches it
    before the drift leaks into consumer test suites that rely on
    stable codes.

    Same silence payload → same code. Runs the client twice against a
    fresh session each time; both codes must match exactly.
    """
    _, port = server
    code_re = re.compile(r"Embedding \| .*? code=([0-9a-f]{16})")

    codes: list[str] = []
    for _ in range(2):
        result = _run_client(port, env, tmp_path)
        assert result.returncode == 0, f"client failed: stderr={result.stderr}"
        match = code_re.search(result.stdout)
        assert match, f"missing Embedding line in:\n{result.stdout}"
        codes.append(match.group(1))

    assert codes[0] == codes[1], (
        f"fingerprint_code drifted across runs: {codes[0]!r} != {codes[1]!r}. "
        "This breaks the sim's determinism invariant — check server.py's "
        "_synthetic_embedding() for non-deterministic changes (seed source, "
        "PRNG constants, normalisation order)."
    )
