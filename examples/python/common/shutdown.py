"""Graceful SIGINT/SIGTERM handler for asyncio task lists.

Lets the example clients tear down in-flight bidi streams cleanly on
Ctrl-C instead of unwinding through grpc.aio with a CancelledError
traceback. Pulled into common/ so phone_call.py + phone_call_burst.py
don't each re-implement the boilerplate.

Usage:
    tasks = [asyncio.create_task(...), ...]
    shutdown = install_signal_shutdown(tasks)
    results = await asyncio.gather(*tasks, return_exceptions=True)
    if shutdown.seen:
        ... # signal-triggered, not natural completion

Behaviour:
  - Handles SIGINT + SIGTERM identically.
  - First signal: prints "Received <signame>, cancelling streams..." to
    stderr and calls .cancel() on every still-running task.
  - Subsequent signals: no-op (so a frantic Ctrl-C-Ctrl-C doesn't double-
    cancel or re-print the banner).
  - Captures the task list by reference at install time — tasks added
    later won't be cancelled. The example clients create all their tasks
    up front, so this is fine.
"""
from __future__ import annotations

import asyncio
import signal
import sys
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass
class Shutdown:
    """Mutable flag the signal handler flips. Poll `.seen` after gather()."""
    seen: bool = False


def install_signal_shutdown(tasks: Iterable[asyncio.Task]) -> Shutdown:
    """Install SIGINT + SIGTERM handlers that cancel every task in `tasks`.

    Returns a `Shutdown` whose `.seen` flag flips the first time a signal
    fires. Callers use it to distinguish 'cancelled by user' from
    'completed normally' when shaping the final summary.
    """
    state = Shutdown()
    task_list = list(tasks)

    def _request_shutdown(signame: str) -> None:
        if state.seen:
            return
        state.seen = True
        print(f"\nReceived {signame}, cancelling streams...", file=sys.stderr, flush=True)
        for t in task_list:
            if not t.done():
                t.cancel()

    loop = asyncio.get_running_loop()
    for sig, name in ((signal.SIGINT, "SIGINT"), (signal.SIGTERM, "SIGTERM")):
        loop.add_signal_handler(sig, _request_shutdown, name)
    return state
