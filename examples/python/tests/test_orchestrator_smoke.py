"""End-to-end smoke test for the Python orchestrator example — focuses
on the 0.3.8 ClientMetadata path.

Spawns the orchestrator-simulator-service (canonical location:
`examples/simulator/orchestrator/`) and runs
examples/python/orchestrator_client.py against it with the
`--demo-teams-meeting` flag, which schedules two canned ClientMetadata
emissions modelling an aurigin-guard-win session where a Chrome Teams
meeting starts partway through the stream and ends before close.

Catches:
- Client-side serialisation of ClientMetadata (int64 emitted_at_ms +
  map<string, string> attributes).
- Simulator-side drain + logging of the client_metadata oneof variant.
- The two converging on matching attribute dicts + in the right order.
- Counter arithmetic on the session-close log line.

No audio fixtures required — the client's silence fallback drives the
timeline and the metadata schedule rides alongside.
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
SIMULATOR_SRC = REPO_ROOT / "examples" / "simulator" / "orchestrator" / "src"


def _free_port() -> int:
    """Ask the OS for an unused TCP port — same TOCTOU-tolerant shape
    as the sibling smoke tests."""
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
    """PYTHONPATH covering the generated stubs + example dir + the
    orchestrator simulator package src/ (so `python -m
    orchestrator_simulator_service` resolves without a full `uv sync`
    in CI)."""
    return {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [
                str(REPO_ROOT / "gen" / "py"),
                str(EXAMPLES_DIR),
                str(SIMULATOR_SRC),
                os.environ.get("PYTHONPATH", ""),
            ],
        ),
    }


@pytest.fixture
def server(env: dict[str, str]):
    """Spawn the orchestrator-simulator-service on a free port, collect
    its stderr in-memory, tear it down at the end."""
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "orchestrator_simulator_service"],
        env={**env, "PORT": str(port)},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
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


def _run_client(
    port: int,
    env: dict[str, str],
    extra: list[str],
) -> subprocess.CompletedProcess[str]:
    """Invoke the orchestrator client against the local sim. `extra`
    carries test-specific flags (--demo-teams-meeting, --metadata-at,
    etc.). Short duration + no TLS keeps each test well under the
    simulator's 5-s verdict interval so the sim's canned verdict
    emissions don't race the metadata schedule."""
    return subprocess.run(
        [
            sys.executable,
            str(EXAMPLES_DIR / "orchestrator_client.py"),
            "--target", f"localhost:{port}",
            "--tls", "never",
            "--duration", "12",
            *extra,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _drain_sim_log(proc: subprocess.Popen, settle_s: float = 0.5) -> str:
    """Give the sim a brief moment to flush the session-close log line
    after the client half-closes, then drain whatever's buffered. The
    sim writes to stderr merged into stdout; we read non-blocking."""
    time.sleep(settle_s)
    import fcntl
    fd = proc.stdout.fileno()
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    out = ""
    try:
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            out += chunk
    except (BlockingIOError, OSError):
        pass
    return out


def test_demo_teams_meeting_roundtrip(server, env: dict[str, str]):
    """`--demo-teams-meeting` schedules two ClientMetadata emissions.
    The sim MUST log both with the expected attribute keys, in the
    right order, and surface a `client_metadata=2` counter on the
    session-close line."""
    proc, port = server
    result = _run_client(port, env, ["--demo-teams-meeting"])
    assert result.returncode == 0, f"client failed: stderr={result.stderr}"

    sim_log = _drain_sim_log(proc)

    # Pull all client_metadata lines in order and check the structure.
    md_lines = re.findall(r"client_metadata: .+", sim_log)
    assert len(md_lines) == 2, (
        f"expected 2 client_metadata log lines, got {len(md_lines)} in:\n{sim_log}"
    )
    first, second = md_lines
    # First emission — Teams meeting started. All three attrs must be
    # present; we don't pin attribute ordering since the sim renders
    # dict(attributes) which depends on protobuf map iteration order,
    # but the values are stable.
    assert "'meeting_state': 'in_meeting'" in first, f"missing in_meeting marker in {first!r}"
    assert "'active_app': 'chrome.exe'" in first, f"missing active_app in {first!r}"
    assert "'active_window_title':" in first, f"missing active_window_title in {first!r}"
    assert "Microsoft Teams" in first, f"missing Teams marker in {first!r}"
    # Second emission — meeting ended.
    assert "'meeting_state': 'idle'" in second, f"missing idle marker in {second!r}"
    assert "chrome.exe" not in second, (
        f"second emission should carry ONLY meeting_state, got {second!r}"
    )

    # Session-close line must reflect the count.
    close_line = next(
        (line for line in sim_log.splitlines() if "stream closed" in line),
        None,
    )
    assert close_line is not None, f"no 'stream closed' line in sim log:\n{sim_log}"
    assert "client_metadata=2" in close_line, (
        f"close line missing client_metadata=2 counter: {close_line!r}"
    )


def test_no_flags_emits_zero_metadata(server, env: dict[str, str]):
    """No `--metadata-at` / `--demo-teams-meeting` flags → zero
    ClientMetadata on the wire. The sim's counter stays at 0."""
    proc, port = server
    result = _run_client(port, env, [])
    assert result.returncode == 0, f"client failed: stderr={result.stderr}"

    sim_log = _drain_sim_log(proc)
    assert "client_metadata:" not in sim_log, (
        f"unexpected client_metadata log lines in no-flag run:\n{sim_log}"
    )
    close_line = next(
        (line for line in sim_log.splitlines() if "stream closed" in line),
        None,
    )
    assert close_line is not None, f"no 'stream closed' line in sim log:\n{sim_log}"
    assert "client_metadata=0" in close_line, (
        f"close line missing client_metadata=0 counter: {close_line!r}"
    )


def test_explicit_metadata_at(server, env: dict[str, str]):
    """`--metadata-at` is repeatable and should land each emission on
    the sim. Uses a value that includes spaces (an active_window_title)
    to exercise the parsing path."""
    proc, port = server
    result = _run_client(
        port,
        env,
        [
            "--metadata-at", "1", "active_app=chrome.exe,foreground=true",
            "--metadata-at", "4", "meeting_state=in_meeting,active_window_title=Daily Standup | Microsoft Teams",
        ],
    )
    assert result.returncode == 0, f"client failed: stderr={result.stderr}"

    sim_log = _drain_sim_log(proc)
    md_lines = re.findall(r"client_metadata: .+", sim_log)
    assert len(md_lines) == 2, (
        f"expected 2 client_metadata lines, got {len(md_lines)} in:\n{sim_log}"
    )
    assert "'foreground': 'true'" in md_lines[0], f"first emission missing: {md_lines[0]!r}"
    assert "Daily Standup | Microsoft Teams" in md_lines[1], (
        f"second emission missing window title: {md_lines[1]!r}"
    )
