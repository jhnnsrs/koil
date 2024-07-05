import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
import os
import sys
import threading

from koil.errors import ContextError
from koil.vars import current_loop
import time
import logging


logger = logging.getLogger(__name__)

try:
    import uvloop
except ImportError:
    uvloop = None


@contextmanager
def _selector_policy(uvify=True):
    original_policy = asyncio.get_event_loop_policy()

    try:
        if uvify:
            if uvloop:
                asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
            else:
                logger.info("uvloop not installed, using default policy")
        elif (
            sys.version_info >= (3, 8)
            and os.name == "nt"
            and hasattr(asyncio, "WindowsSelectorEventLoopPolicy")
        ):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        yield
    finally:
        asyncio.set_event_loop_policy(original_policy)


def run_threaded_event_loop(loop):
    try:
        loop.run_forever()
    finally:
        try:
            # mimic asyncio.run() behavior
            # cancel unexhausted async generators
            tasks = asyncio.all_tasks(loop)
            for task in tasks:
                task.cancel()

            async def gather():
                logger.debug(f"Cancelling {tasks}")
                await asyncio.gather(*tasks, return_exceptions=True)

            loop.run_until_complete(gather())
            for task in tasks:
                if task.cancelled():
                    continue
                if task.exception() is not None:
                    loop.call_exception_handler(
                        {
                            "message": "unhandled exception during loop shutdown",
                            "exception": task.exception(),
                            "task": task,
                        }
                    )
            if hasattr(loop, "shutdown_asyncgens"):
                loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            logger.debug("Loop Sucessfully Closed")
            loop.close()


def get_threaded_loop(name="KoilLoop", uvify=True):
    """Creates a new event loop and run it in a new threads"""
    with _selector_policy(uvify=uvify):
        newloop = asyncio.new_event_loop()

    th = threading.Thread(target=run_threaded_event_loop, args=(newloop,), name=name)
    th.daemon = True
    th.start()

    newloop.name = name

    return newloop


class KoilMixin:
    def exit(self):
        return self.__exit__(None, None, None)

    async def aenter(self):
        return await self.__aenter__()

    def enter(self):
        return self.__enter__()

    async def aexit(self):
        return await self.__aexit__(None, None, None)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args, **kwargs):
        pass

    def __enter__(self):
        try:
            asyncio.get_running_loop()
            if not hasattr(self, "sync_in_async") or self.sync_in_async is False:
                raise ContextError(
                    f"""You are running in asyncio event loop already. 
                    Using koil makes no sense here, use asyncio instead. You can use koil in a sync context by setting `sync_in_async=True` currently it is
                    set to {getattr(self, "sync_in_async", None)}.
                    If this happens in a context manager, you probably forgot to use the `async with` syntax."""
                )
        except RuntimeError:
            pass

        self._loop = None
        _loop = current_loop.get()
        if _loop is None:
            # We are now creating a koiled loop for this context
            self._loop = get_threaded_loop(
                getattr(
                    self,
                    "name",
                    f"KoiledLoop {'governed by' + self.__class__.__name__ if getattr(self, 'creating_instance', None) else ''}",
                ),
                uvify=getattr(self, "uvify", True),
            )
            current_loop.set(self._loop)
        self.running = True
        return self

    def __exit__(self, *args, **kwargs):
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

            iterations = 0

            while self._loop.is_running():
                time.sleep(0.001)
                iterations += 1
                if iterations == 100:
                    logger.warning(
                        "Shutting Down takes longer than expected. Probably we are having loose Threads? Keyboard interrupt?"
                    )

            current_loop.set(None)
        self.running = False


@dataclass
class Koil(KoilMixin):
    "The instance that created this class through entering"

    uvify: bool = False
    """Shoul we run the loop with uvloop?"""

    name: str = "KoilLoop"
    """How would you like to name this loop"""

    force_lonely: bool = False
