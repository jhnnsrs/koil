import logging
import asyncio


logger = logging.getLogger(__name__)


class KoilTask:
    def __init__(
        self, future=None, loop=None, *args, log_errors=True, **kwargs
    ) -> None:
        super().__init__()
        self.future = future
        self.log_errors = log_errors
        self.loop = loop

        self.task = None

    async def wrapped_future(self, future):
        try:
            return await future
        except Exception as e:
            raise e

    def run(self):
        self.task = self.loop.create_task(self.wrapped_future(self.future))
        return self

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
        return asyncio.run_coroutine_threadsafe(self.acancel(), self.loop).result()

    def result(self):
        return asyncio.run_coroutine_threadsafe(self.acancel(), self.loop).result()
