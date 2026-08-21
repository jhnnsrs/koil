"""Interpreter-exit safety: abandoned koil workers must not block process exit.

koil promises that uncancellable work abandoned by ``Koil.__exit__`` cannot
hang the process: the loop thread is a daemon, and (since the fix these tests
pin) workers run on dedicated daemon threads rather than the loop's default
``ThreadPoolExecutor`` — whose non-daemon workers the interpreter joins at
shutdown, which used to block ``python`` from exiting until the wedged worker
returned of its own accord.
"""

import subprocess
import sys
import textwrap

import pytest

_STUCK_WORKER_SCRIPT = textwrap.dedent(
    """
    import time
    from koil.loop import Koil
    from koil.bridge import run_threaded, unkoil_task

    def stubborn():
        # Deliberately never checks check_cancelled: uncancellable work.
        time.sleep(120)

    async def spawn():
        await run_threaded(stubborn)

    with Koil(cancel_timeout=0.2, shutdown_join_timeout=0.2):
        unkoil_task(spawn)
        time.sleep(0.3)  # let the worker actually start

    print("KOIL_EXITED", flush=True)
    """
)


@pytest.mark.timeout(30)
def test_stuck_worker_does_not_block_interpreter_exit():
    """A worker that ignores cancellation is abandoned on a daemon thread and
    the process exits promptly instead of hanging until the worker returns."""
    result = subprocess.run(
        [sys.executable, "-c", _STUCK_WORKER_SCRIPT],
        capture_output=True,
        text=True,
        timeout=20,  # far below the worker's 120s sleep; hang -> TimeoutExpired
    )
    assert "KOIL_EXITED" in result.stdout
    assert result.returncode == 0


@pytest.mark.timeout(30)
def test_worker_threads_are_daemon_threads():
    """run_threaded workers run on named daemon threads."""
    import asyncio
    import threading

    from koil.bridge import run_threaded

    async def main():
        def report():
            t = threading.current_thread()
            return t.daemon, t.name

        return await run_threaded(report)

    daemon, name = asyncio.run(main())
    assert daemon is True
    assert name.startswith("koil-worker-")
