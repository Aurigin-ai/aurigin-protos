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


def _free_port() -> int:
    """Ask the OS for an unused TCP port. There's a tiny TOCTOU window between
    here and when the server child binds, but it's vastly safer than a fixed
    port — Linux's default ephemeral range is 32768–60999, so any fixed port
    in there can be stolen by a transient outbound socket on a busy CI runner.
    """
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
    """Spawn the example server on a free port and tear it down at the end."""
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, str(EXAMPLES_DIR / "server.py")],
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


def test_client_silence_roundtrip(server, env: dict[str, str], tmp_path):
    """The stub client streams 3 s of silence and prints session/analysis/final.

    Points the client at an empty tmp dir so the silence-fallback path runs
    deterministically regardless of whatever WAVs the dev has dropped into
    examples/audio/ locally.
    """
    _, port = server
    result = subprocess.run(
        [
            sys.executable, str(EXAMPLES_DIR / "client.py"),
            "--target", f"localhost:{port}",
            "--audio-dir", str(tmp_path),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"client failed: stderr={result.stderr}"
    assert "Session:" in result.stdout, f"missing session line in:\n{result.stdout}"
    assert "FINAL" in result.stdout
    # Simulator issues per-session ids like 'sim-<8 hex>'. We don't pin the
    # specific value — just that the server emitted one.
    assert "sim-" in result.stdout, f"missing simulator session id prefix in:\n{result.stdout}"
    # NOTE: we deliberately don't assert AnalysisResult lines here. The
    # scenario-driven server emits curve samples at wallclock 1 s / 2 s / 3 s,
    # but client.py's silence fallback sends all 6 × 500 ms chunks back-to-back
    # without real-time pacing, so the client typically closes its write side
    # before any sample fires. Analysis emission is covered by the phone-call
    # test below, which paces audio in real time.


@pytest.mark.parametrize(
    ("fixture_name", "expected_header"),
    [
        # S16LE 8 kHz mono — the historical telephony fixture.
        ("test_call.wav", "8000Hz/1ch S16LE"),
        # F32LE 16 kHz mono — exercises the IEEE-float wire path so a
        # regression that breaks the RIFF reader's format dispatch fails CI.
        ("test_call_f32le.wav", "16000Hz/1ch F32LE"),
    ],
)
def test_phone_call_wav_roundtrip(server, env: dict[str, str], fixture_name: str, expected_header: str):
    """phone_call.py reads a real WAV fixture and streams it to the server."""
    _, port = server
    fixture = REPO_ROOT / "examples" / "audio" / "fixtures" / fixture_name
    assert fixture.is_file(), f"missing test fixture: {fixture}"

    result = subprocess.run(
        [
            sys.executable,
            str(EXAMPLES_DIR / "phone_call.py"),
            "--audio", str(fixture),
            "--duration", "1",
            "--chunk-ms", "100",
            "--target", f"localhost:{port}",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"phone_call failed: stderr={result.stderr}"
    # The header line confirms the WAV reader picked sr/channels/format correctly.
    assert expected_header in result.stdout, f"WAV reader didn't pick up {expected_header}:\n{result.stdout}"
    assert "📞 Session:" in result.stdout
    assert "Call ended" in result.stdout


# Backend-simulation scenarios — pin the dfs config they mirror via x-scenario-id
# metadata and assert the on-wire emission shape matches what the real backend
# would produce for the same audio length + config.
@pytest.mark.parametrize(
    ("scenario_id", "duration_s", "expected_analyses", "extra_substrings"),
    [
        # tail_strategy=drop → 2 main windows fire, 1ms tail silently skipped.
        ("tail_dropped_below_min", 11, 2, ()),
        # tail_strategy=extend → 2 emissions, second covers the 1ms tail
        # (duration_ms=5001). Check that duration in the analysis log line.
        ("tail_extended_full_coverage", 11, 2, ()),
        # tail_strategy=recompute → 2 emissions, second slides back so its
        # offset is at audio time 5001ms (rather than the 5000ms grid).
        # phone_call.py prints offsets as "Analysis @ 5.00s" — for recompute
        # the second emission's offset is 5.001s, which rounds to "5.00s"
        # in the 2-decimal print but is distinguishable in the FINAL line.
        ("tail_recomputed_full_coverage", 11, 2, ()),
        # silent_windows=[2] → one of the 5 emissions is the silence sentinel.
        # Loop the fixture (10s) to fill the 15s scenario timeline.
        ("silence_gated_window", 16, 5, ("label=silence",)),
    ],
)
def test_phone_call_backend_simulation(
    server,
    env: dict[str, str],
    scenario_id: str,
    duration_s: int,
    expected_analyses: int,
    extra_substrings: tuple[str, ...],
):
    """phone_call.py drives the simulator through each backend_simulation scenario."""
    _, port = server
    fixture = REPO_ROOT / "examples" / "audio" / "fixtures" / "test_call_10s_tail.wav"
    assert fixture.is_file(), f"missing test fixture: {fixture}"

    result = subprocess.run(
        [
            sys.executable,
            str(EXAMPLES_DIR / "phone_call.py"),
            "--audio", str(fixture),
            "--duration", str(duration_s),
            "--chunk-ms", "100",
            "--target", f"localhost:{port}",
            "--scenario-id", scenario_id,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=duration_s + 15,
    )
    assert result.returncode == 0, f"phone_call failed: stderr={result.stderr}"
    assert f"analyses={expected_analyses}" in result.stdout, (
        f"scenario {scenario_id}: expected analyses={expected_analyses}\n"
        f"stdout:\n{result.stdout}"
    )
    for needle in extra_substrings:
        assert needle in result.stdout, (
            f"scenario {scenario_id}: expected substring {needle!r}\n"
            f"stdout:\n{result.stdout}"
        )
