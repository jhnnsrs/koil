import logging
import asyncio




logger = logging.getLogger(__name__)


class KoilTask:

    def __init__(self, future=None, koil = None, *args, log_errors=True, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        from koil.koil import get_current_koil
        self.future = future
        self.task = None
        self.log_errors = log_errors
        self.koil = koil or get_current_koil()
        self.loop = self.koil.loop
        self.loop.call_soon_threadsafe(self.run)

    async def wrapped_future(self, future):
        try:
            return await future
        except Exception as e:
            if self.log_errors: logger.exception(e)
            raise e


    def run(self):
        self.task = self.loop.create_task(self.wrapped_future(self.future))

    async def acancel(self):
        try:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError as e:
                print("Cancelled KoilTask")
        except Exception as e:
            print(e)

    def cancel(self):
        return asyncio.run_coroutine_threadsafe(self.acancel(), self.loop).result()
