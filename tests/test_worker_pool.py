"""The thread pool ``run_threaded`` runs on, and ``koil.sleep`` inside it.

A :class:`~koil.Koil` owns its workers: they are not the loop's default
executor (which the loop needs for itself), they are daemon threads, and a
call still queued behind them is cancelled without waiting.
"""

import asyncio
import contextvars
import os
import threading
import time

import pytest

import koil
from koil import Koil, ThreadCancelledError, run_threaded, unkoil
from koil.executor import KoilExecutor

#: Size of the event loop's default executor.
DEFAULT_POOL = min(32, (os.cpu_count() or 1) + 4)


# -- the pool itself ----------------------------------------------------------


def test_the_pool_returns_results_and_exceptions() -> None:
    pool = KoilExecutor(2)
    try:
        assert pool.submit(pow, 2, 5).result(5) == 32
        with pytest.raises(ZeroDivisionError):
            pool.submit(lambda: 1 / 0).result(5)
    finally:
        pool.shutdown()


def test_the_pool_reuses_an_idle_thread() -> None:
    pool = KoilExecutor(8)
    try:
        idents = {pool.submit(threading.get_ident).result(5) for _ in range(20)}
        assert len(idents) == 1
    finally:
        pool.shutdown()


def test_the_pool_never_runs_more_than_max_workers_at_once() -> None:
    pool = KoilExecutor(3)
    lock = threading.Lock()
    running = 0
    peak = 0

    def work() -> None:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.02)
        with lock:
            running -= 1

    try:
        for future in [pool.submit(work) for _ in range(12)]:
            future.result(5)
        assert peak == 3
    finally:
        pool.shutdown()


def test_the_pools_threads_are_daemons() -> None:
    pool = KoilExecutor(1)
    try:
        assert pool.submit(lambda: threading.current_thread().daemon).result(5)
    finally:
        pool.shutdown()


def test_shutdown_cancels_what_is_still_queued_and_refuses_new_work() -> None:
    pool = KoilExecutor(1)
    release = threading.Event()
    running = pool.submit(release.wait)
    queued = pool.submit(lambda: "never")
    pool.shutdown(wait=False, cancel_futures=True)
    assert queued.cancelled()
    with pytest.raises(RuntimeError):
        pool.submit(lambda: None)
    release.set()
    assert running.result(5) is True
    pool.shutdown(wait=True)  # idempotent, and the thread ends


def test_the_pool_rejects_a_size_of_zero() -> None:
    with pytest.raises(ValueError):
        KoilExecutor(0)


# -- run_threaded on a Koil -----------------------------------------------------


def test_run_threaded_runs_on_koils_own_daemon_workers() -> None:
    async def main() -> threading.Thread:
        return await run_threaded(threading.current_thread)

    with Koil():
        thread = unkoil(main)
    assert thread.name.startswith("KoilWorker")
    assert thread.daemon


def test_blocked_workers_leave_the_loops_default_executor_free() -> None:
    """Name resolution runs on the default executor; sync actors must not starve it."""
    release = threading.Event()
    count = DEFAULT_POOL + 4

    async def main() -> str:
        loop = asyncio.get_running_loop()
        blocked = [
            asyncio.ensure_future(run_threaded(release.wait)) for _ in range(count)
        ]
        try:
            await asyncio.sleep(0.1)
            return await asyncio.wait_for(
                loop.run_in_executor(None, lambda: "resolved"), timeout=3
            )
        finally:
            release.set()
            await asyncio.gather(*blocked)

    with Koil():
        assert unkoil(main) == "resolved"


def test_sync_code_waiting_on_async_code_waiting_on_sync_code() -> None:
    """More nested calls than the default executor has threads still finish."""
    count = DEFAULT_POOL + 4

    def inner() -> int:
        return 1

    async def middle(gate: asyncio.Event) -> int:
        await gate.wait()
        return await run_threaded(inner)

    def outer(gate: asyncio.Event) -> int:
        return unkoil(middle, gate)

    async def main() -> int:
        gate = asyncio.Event()
        tasks = [asyncio.ensure_future(run_threaded(outer, gate)) for _ in range(count)]
        await asyncio.sleep(0.2)  # every outer call now holds a thread
        gate.set()
        return sum(await asyncio.wait_for(asyncio.gather(*tasks), timeout=10))

    with Koil():
        assert unkoil(main) == count


def test_max_workers_bounds_the_pool() -> None:
    lock = threading.Lock()
    running = 0
    peak = 0

    def work() -> None:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.02)
        with lock:
            running -= 1

    async def main() -> None:
        await asyncio.gather(*(run_threaded(work) for _ in range(10)))

    with Koil(max_workers=2):
        unkoil(main)
    assert peak == 2


def test_cancelling_a_call_still_in_the_queue_is_immediate() -> None:
    release = threading.Event()
    ran: list[int] = []

    async def main() -> float:
        busy = asyncio.ensure_future(run_threaded(release.wait))
        await asyncio.sleep(0.05)
        queued = asyncio.ensure_future(run_threaded(ran.append, 1))
        await asyncio.sleep(0.05)
        started = time.monotonic()
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        elapsed = time.monotonic() - started
        release.set()
        await busy
        return elapsed

    with Koil(max_workers=1, cancel_timeout=5):
        assert unkoil(main) < 1
    assert ran == []


def test_a_worker_that_ignores_cancellation_does_not_hold_up_the_exit() -> None:
    stop = threading.Event()
    inside = threading.Event()

    def stubborn() -> None:
        inside.set()
        stop.wait()

    async def main() -> None:
        asyncio.ensure_future(run_threaded(stubborn))
        await asyncio.get_running_loop().run_in_executor(None, inside.wait)

    started = time.monotonic()
    with Koil(cancel_timeout=0.2, shutdown_join_timeout=0.2):
        unkoil(main)
    try:
        assert time.monotonic() - started < 3
        left = [
            t
            for t in threading.enumerate()
            if t.name.startswith("KoilWorker") and t.is_alive()
        ]
        # Still running, but as a daemon: interpreter exit will not wait for it.
        assert left and all(t.daemon for t in left)
    finally:
        stop.set()


def test_workers_do_not_leak_context_into_the_next_call() -> None:
    var: contextvars.ContextVar[str] = contextvars.ContextVar("leak", default="clean")

    def dirty() -> None:
        var.set("dirty")

    async def main() -> str:
        await run_threaded(dirty)
        return await run_threaded(var.get)

    with Koil(max_workers=1):  # the same thread serves both calls
        assert unkoil(main) == "clean"


async def test_run_threaded_still_works_on_a_loop_koil_did_not_start() -> None:
    thread = await run_threaded(threading.current_thread)
    assert not thread.name.startswith("KoilWorker")


# -- koil.sleep ---------------------------------------------------------------


async def test_a_sleeping_worker_wakes_the_moment_it_is_cancelled() -> None:
    asleep = threading.Event()
    caught: list[BaseException] = []

    def work() -> None:
        asleep.set()
        try:
            koil.sleep(30)
        except BaseException as e:
            caught.append(e)
            raise

    task = asyncio.ensure_future(run_threaded(work))
    await asyncio.get_running_loop().run_in_executor(None, asleep.wait)
    await asyncio.sleep(0.05)
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - started < 0.5
    assert isinstance(caught[0], ThreadCancelledError)


async def test_sleep_in_a_worker_lasts_as_long_as_asked() -> None:
    def work() -> float:
        started = time.monotonic()
        koil.sleep(0.1)
        return time.monotonic() - started

    # Lower bound with slack for coarse clocks (Windows ticks at ~16ms).
    assert 0.07 <= await run_threaded(work) < 0.5


def test_sleep_does_not_need_the_loop() -> None:
    """A worker's sleep ends on time even while the loop is busy."""

    async def main() -> float:
        def work() -> float:
            started = time.monotonic()
            koil.sleep(0.1)
            return time.monotonic() - started

        nap = asyncio.ensure_future(run_threaded(work))
        await asyncio.sleep(0.02)
        # The loop is stuck well past the end of the nap.
        time.sleep(0.5)  # noqa: ASYNC251
        return await nap

    with Koil():
        assert unkoil(main) < 0.4


def test_sleep_outside_a_worker_is_a_plain_sleep() -> None:
    started = time.monotonic()
    koil.sleep(0.05)
    assert time.monotonic() - started >= 0.03
    with Koil():
        started = time.monotonic()
        koil.sleep(0.05)
        assert time.monotonic() - started >= 0.03


def test_sleeping_on_the_loop_itself_warns() -> None:
    async def on_the_loop() -> None:
        koil.sleep(0.01)

    with Koil():
        with pytest.warns(RuntimeWarning, match="blocks it"):
            unkoil(on_the_loop)
