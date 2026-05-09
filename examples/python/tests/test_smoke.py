"""End-to-end smoke test for the Python example.

Spawns examples/python/server.py and runs examples/python/client.py
against it. The client falls back to streaming 3 s of silence when
examples/audio/ is empty (always the case in CI), so this test exercises
the full proto + gRPC wire path without needing any audio fixtures.

Catches anything that breaks the example: proto field renames, message
removals, RPC name changes, generated stub API shifts, server impl bugs.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

EXAMPLES_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = EXAMPLES_DIR.parent.parent
PORT = 50061  # avoid clashing with a dev backend-app on 50051


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
    """Environment with PYTHONPATH covering the generated stubs + example dir."""
    return {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [
                str(REPO_ROOT / "gen" / "py"),
                str(EXAMPLES_DIR),
                os.environ.get("PYTHONPATH", ""),
            ]
        ),
    }


@pytest.fixture
def server(env: dict[str, str]):
    """Spawn the example server on PORT and tear it down at the end."""
    proc = subprocess.Popen(
        [sys.executable, str(EXAMPLES_DIR / "server.py")],
        env={**env, "PORT": str(PORT)},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if not _wait_for_port(PORT):
        proc.terminate()
        out = proc.stdout.read() if proc.stdout else ""
        pytest.fail(f"Server didn't bind on :{PORT} within 15 s. Output:\n{out}")
    try:
        yield proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_client_silence_roundtrip(server, env: dict[str, str]):
    """The stub client streams 3 s of silence and prints session/analysis/final."""
    result = subprocess.run(
        [sys.executable, str(EXAMPLES_DIR / "client.py"), f"localhost:{PORT}"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"client failed: stderr={result.stderr}"
    assert "Session:" in result.stdout, f"missing session line in:\n{result.stdout}"
    assert "Analysis | offset=" in result.stdout
    assert "FINAL" in result.stdout
    assert "demo-session-0001" in result.stdout, "stub server should return its hardcoded session id"


def test_phone_call_wav_roundtrip(server, env: dict[str, str]):
    """phone_call.py reads a real WAV fixture and streams it to the server."""
    fixture = REPO_ROOT / "examples" / "audio" / "fixtures" / "test_call.wav"
    assert fixture.is_file(), f"missing test fixture: {fixture}"

    result = subprocess.run(
        [
            sys.executable,
            str(EXAMPLES_DIR / "phone_call.py"),
            "--audio", str(fixture),
            "--duration", "1",
            "--chunk-ms", "100",
            "--target", f"localhost:{PORT}",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"phone_call failed: stderr={result.stderr}"
    # The header line confirms the WAV reader parsed sr/channels correctly.
    assert "8000Hz/1ch" in result.stdout, f"WAV reader didn't pick up 8 kHz mono:\n{result.stdout}"
    assert "📞 Session:" in result.stdout
    assert "Call ended" in result.stdout
