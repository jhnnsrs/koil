import asyncio
import threading
import time
import pytest

from koil.composition.base import KoiledModel
from koil.errors import KoilError, ThreadCancelledError
from koil.bridge import iterate_threaded, run_threaded, sleep
from koil.loop import Koil
from koil.context import check_cancelled, current_cancel_event, global_koil


class CoopWorker(KoiledModel):
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def run_blocking(self):
        return await run_threaded(self._work)

    def _work(self):
        for _ in range(100):
            time.sleep(0.005)
            check_cancelled()
        return "done"


class GenWorker(KoiledModel):
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def stream(self):
        async for val in iterate_threaded(self._gen):
            yield val

    def _gen(self):
        for i in range(100):
            time.sleep(0.005)
            check_cancelled()
            yield i


class SleepWorker(KoiledModel):
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def do_sleep(self):
        return await run_threaded(self._set_cancel_then_sleep)

    def _set_cancel_then_sleep(self):
        # Simulate an external cancellation signal arriving mid-sleep
        event = current_cancel_event.get()
        event.set()
        sleep(10)  # should raise ThreadCancelledError on the first cancel check


@pytest.mark.timeout(10)
async def test_run_spawned_cooperative_cancel():
    """Cancelling a task wrapping run_threaded completes cleanly when the thread checks check_cancelled."""
    async with CoopWorker() as w:
        task = asyncio.create_task(w.run_blocking())
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.timeout(10)
async def test_iterate_spawned_cooperative_cancel():
    """Cancelling a task wrapping iterate_threaded completes cleanly when the generator checks check_cancelled."""

    async def consume(w: GenWorker):
        async for _ in w.stream():
            pass

    async with GenWorker() as w:
        task = asyncio.create_task(consume(w))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.timeout(10)
async def test_sleep_raises_thread_cancelled_error_on_cancel():
    """koil.bridge.sleep raises ThreadCancelledError when the thread's cancel event is already set."""
    async with SleepWorker() as w:
        with pytest.raises(ThreadCancelledError):
            await w.do_sleep()


@pytest.mark.timeout(10)
async def test_run_spawned_cancel_timeout():
    """A non-cooperative thread causes KoilError after cancel_timeout expires."""
    blocker = threading.Event()

    k = Koil()
    k.cancel_timeout = 0.1
    token = global_koil.set(k)

    try:
        task = asyncio.create_task(run_threaded(blocker.wait))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises((KoilError, asyncio.CancelledError)):
            # KoilError when thread doesn't cooperate within cancel_timeout;
            # CancelledError if the executor future is cancelled before the timeout fires.
            await task
    finally:
        global_koil.reset(token)
        blocker.set()  # unblock the background thread so it can finish
        await asyncio.sleep(0)  # yield so the executor can clean up


@pytest.mark.timeout(10)
async def test_cancel_timeout_constructor_kwarg():
    """cancel_timeout can be set via the Koil constructor, not only as an attribute."""
    blocker = threading.Event()

    k = Koil(cancel_timeout=0.1)
    assert k.cancel_timeout == 0.1
    token = global_koil.set(k)

    try:
        task = asyncio.create_task(run_threaded(blocker.wait))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises((KoilError, asyncio.CancelledError)):
            await task
    finally:
        global_koil.reset(token)
        blocker.set()
        await asyncio.sleep(0)


class IdleLoopSleeper(KoiledModel):
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def nap(self) -> float:
        return await run_threaded(self._nap)

    def _nap(self) -> float:
        started = time.monotonic()
        for _ in range(5):
            sleep(0.02)
        return time.monotonic() - started


@pytest.mark.timeout(10)
async def test_sleep_returns_on_time_while_the_loop_is_idle():
    """A worker's sleep is timed by the loop, and the loop has to be woken for it.

    Nothing else happens on the loop here: it only awaits the worker. A timer added
    from the worker's thread without waking the loop was not noticed until something
    else did, so each sleep lasted until the next unrelated event -- or for ever.
    """
    async with IdleLoopSleeper() as sleeper:
        took = await asyncio.wait_for(sleeper.nap(), timeout=5)

    assert 0.1 <= took < 1.0, f"five sleeps of 20 ms took {took:.2f} s"
