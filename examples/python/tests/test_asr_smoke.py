"""End-to-end smoke test for the Python ASR example.

Spawns the asr-simulator-service (canonical location:
`examples/simulator/asr/`) and runs examples/python/asr_client.py
against it. The client falls back to streaming 4 s of silence when
examples/audio/ is empty (always the case in CI), so this test
exercises the full proto + gRPC wire path without any audio fixtures.

Catches anything that breaks the ASR example: proto field renames,
message removals, RPC name changes, generated stub API shifts,
simulator impl bugs, drift in the client's print format.

The simulator lives at examples/simulator/asr/ — no scenarios, no YAML
(transcripts are drawn deterministically from a fixed vocabulary cycled
by turn index). This test spawns it as a subprocess — no Docker
required.
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
SIMULATOR_SRC = REPO_ROOT / "examples" / "simulator" / "asr" / "src"


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
    dir + the ASR simulator package src/ (so `python -m
    asr_simulator_service` resolves without a full `uv sync` in CI).
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
    """Spawn the asr-simulator-service on a free port and tear it down
    at the end."""
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "asr_simulator_service"],
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
            sys.executable, str(EXAMPLES_DIR / "asr_client.py"),
            "--target", f"localhost:{port}",
            "--audio-dir", str(audio_dir),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_client_silence_roundtrip(server, env: dict[str, str], tmp_path):
    """The stub client streams 4 s of silence and prints session / turn /
    final lines. Points the client at an empty tmp dir so the silence-
    fallback path runs deterministically regardless of whatever WAVs the
    dev has dropped into examples/audio/ locally.

    The simulator's default TURN_MS=2000 means 4 s of audio produces
    exactly 2 TranscriptTurns + 1 FinalResult.
    """
    _, port = server
    result = _run_client(port, env, tmp_path)
    assert result.returncode == 0, f"client failed: stderr={result.stderr}"
    assert "Session:" in result.stdout, f"missing session line in:\n{result.stdout}"
    # Simulator issues per-session ids like 'sim-<8 hex>' — matches the
    # sibling sims' convention so grep patterns work across all four.
    assert "sim-" in result.stdout, f"missing simulator session id prefix in:\n{result.stdout}"
    # Exactly 2 Turn lines expected (4 s audio @ 2 s per turn). Match
    # the client's print format: "Turn   0 |      0-2000  ms | ..." —
    # t1_ms is left-padded to width 6, so there can be trailing spaces
    # between the number and the literal `ms`.
    turn_lines = re.findall(r"Turn\s+\d+\s+\|\s+\d+-\d+\s*ms\s+\|", result.stdout)
    assert len(turn_lines) == 2, (
        f"expected 2 Turn lines, got {len(turn_lines)} in:\n{result.stdout}"
    )
    # FINAL with the counters we expect from the silence path. Note
    # total_audio_ms rounds via the simulator's int-division of
    # per-frame ms, so we accept the near-but-exact 4000 figure.
    assert re.search(
        r"FINAL\s+\| total=4000ms \| turns=2", result.stdout,
    ), f"missing / malformed FINAL line in:\n{result.stdout}"


def test_transcripts_are_deterministic_across_runs(server, env: dict[str, str], tmp_path):
    """The ASR sim walks a fixed vocabulary cycled by turn index, so
    identical input MUST produce identical transcripts across runs.

    This is the load-bearing invariant that makes the sim useful for
    smoke tests + golden-fixture diffs. If it breaks (e.g. a refactor
    swaps the vocab source or introduces a random draw), this test
    catches it before the drift leaks into consumer test suites that
    rely on stable transcripts.

    Same silence payload → same transcript lines. Runs the client twice
    against a fresh session each time; both transcript sequences must
    match exactly.
    """
    _, port = server
    text_re = re.compile(r"Turn\s+\d+\s+\|.*?\|\s+'([^']+)'")

    transcript_runs: list[list[str]] = []
    for _ in range(2):
        result = _run_client(port, env, tmp_path)
        assert result.returncode == 0, f"client failed: stderr={result.stderr}"
        matches = text_re.findall(result.stdout)
        assert matches, f"missing Turn lines in:\n{result.stdout}"
        transcript_runs.append(matches)

    assert transcript_runs[0] == transcript_runs[1], (
        f"transcripts drifted across runs: {transcript_runs[0]!r} != "
        f"{transcript_runs[1]!r}. This breaks the sim's determinism "
        "invariant — check server.py's _text_for() for non-deterministic "
        "changes (random draws, mutable state, PRNG injection)."
    )
