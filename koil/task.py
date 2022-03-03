import logging
import asyncio
import time
from koil.vars import current_cancel_event, current_loop
import inspect

logger = logging.getLogger(__name__)


class KoilTask:
    def __init__(
        self,
        coro,
        *args,
        log_errors=True,
        loop=None,
        bypass_test=False,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.coro = coro
        self.loop = loop or current_loop.get()
        assert self.loop, "No koiled Loop found"
        self.task = None
        self.future = None
        if not bypass_test:
            assert self.loop.is_running(), "Loop is not running"
            assert not self.loop.is_closed(), "Loop is closed"
            assert inspect.iscoroutine(coro), "Task is not a coroutine"

    def run(self):
        self.future = asyncio.run_coroutine_threadsafe(self.coro, self.loop)
        return self

    def done(self):
        return self.future.done()

    async def acancel(self):
        try:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError as e:
                logger.debug("Cancelled KoilTask")
        except Exception as e:
            logger.debug("Koil Task Cancellation failed")

    def cancel(self):
        self.future.cancel()

    def result(self):
        assert self.future, "Task was never run! Please run task before"
        return self.future.result()


class KoilGeneratorTask:
    def __init__(self, iterator, *args, loop=None, **kwargs) -> None:
        print("initializitng generator task")
        super().__init__(*args, **kwargs)
        self.iterator = iterator
        self.loop = loop or current_loop.get()
        self.task = None

    def run(self):
        ait = self.iterator.__aiter__()
        res = [False, False]
        cancel_event = current_cancel_event.get()

        async def next_on_ait_with_context():
            try:
                try:
                    obj = await ait.__anext__()
                    return [False, obj]
                except StopAsyncIteration:
                    return [True, None]
            except asyncio.CancelledError as e:
                return [False, e]

        while True:
            res = asyncio.run_coroutine_threadsafe(
                next_on_ait_with_context(), loop=self.loop
            )
            while not res.done():
                if cancel_event and cancel_event.is_set():
                    raise Exception("Task was cancelled")

                time.sleep(0.01)
            done, obj = res.result()
            if done:
                if obj:
                    raise obj
                break
            yield obj
