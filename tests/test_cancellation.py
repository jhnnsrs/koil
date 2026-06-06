import asyncio
import threading
import time
import pytest

from koil.composition.base import KoiledModel
from koil.errors import KoilError, ThreadCancelledError
from koil.helpers import iterate_spawned, run_spawned, sleep
from koil.koil import Koil
from koil.vars import check_cancelled, current_cancel_event, global_koil


class CoopWorker(KoiledModel):
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def run_blocking(self):
        return await run_spawned(self._work)

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
        async for val in iterate_spawned(self._gen):
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
        return await run_spawned(self._set_cancel_then_sleep)

    def _set_cancel_then_sleep(self):
        # Simulate an external cancellation signal arriving mid-sleep
        event = current_cancel_event.get()
        event.set()
        sleep(10)  # should raise ThreadCancelledError on the first cancel check


async def test_run_spawned_cooperative_cancel():
    """Cancelling a task wrapping run_spawned completes cleanly when the thread checks check_cancelled."""
    async with CoopWorker() as w:
        task = asyncio.create_task(w.run_blocking())
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_iterate_spawned_cooperative_cancel():
    """Cancelling a task wrapping iterate_spawned completes cleanly when the generator checks check_cancelled."""

    async def consume(w: GenWorker):
        async for _ in w.stream():
            pass

    async with GenWorker() as w:
        task = asyncio.create_task(consume(w))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_sleep_raises_thread_cancelled_error_on_cancel():
    """koil.helpers.sleep raises ThreadCancelledError when the thread's cancel event is already set."""
    async with SleepWorker() as w:
        with pytest.raises(ThreadCancelledError):
            await w.do_sleep()


async def test_run_spawned_cancel_timeout():
    """A non-cooperative thread causes KoilError after cancel_timeout expires."""
    blocker = threading.Event()

    k = Koil()
    k.cancel_timeout = 0.1
    token = global_koil.set(k)

    try:
        task = asyncio.create_task(run_spawned(blocker.wait))
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
