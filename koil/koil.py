import asyncio
from contextlib import contextmanager
import os
import sys
import threading
from types import TracebackType
from typing import Protocol, Self
from koil.vars import global_koil, global_koil_loop
from koil.errors import ContextError
import time
import logging
try:
    import uvloop # type: ignore[import]
except ImportError:
    uvloop = None



logger = logging.getLogger(__name__)


class KoilProtocol(Protocol):
    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: type[BaseException] | None,
    ) -> None: ...


@contextmanager
def _selector_policy(uvify: bool = True):
    original_policy = asyncio.get_event_loop_policy()

    try:
        if uvify:
            if uvloop:
                asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())  # type: ignore
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


def run_threaded_event_loop(loop: asyncio.BaseEventLoop):
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


def get_threaded_loop(name: str = "KoilLoop", uvify: bool = True):
    """Creates a new event loop and run it in a new threads"""
    with _selector_policy(uvify=uvify):
        newloop = asyncio.new_event_loop()

    th = threading.Thread(target=run_threaded_event_loop, args=(newloop,), name=name)
    th.daemon = True
    th.start()

    return newloop


class Koil:
    def __init__(
        self,
        sync_in_async: bool = True,
        uvify: bool = True,
    ):
        self._loop: asyncio.AbstractEventLoop | None = None
        self.running = False
        self.sync_in_async = sync_in_async
        self.cancel_timeout = 2.0

    def exit(self):
        return self.__exit__(None, None, None)

    async def aenter(self):
        return await self.__aenter__()

    def enter(self):
        return self.__enter__()

    @property
    def loop(self):
        if self._loop is None:
            raise RuntimeError("Loop is not running. This should not happen")
        return self._loop

    async def __aenter__(self):
        try:
            self._loop = asyncio.get_running_loop()
            global_koil_loop.set(self._loop)
        except RuntimeError:
            pass
        return self

    async def __aexit__(
        self, 
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None
    ) -> None:
        
        
        global_koil.set(None)  # type: ignore[assignment]
        return None

    def __enter__(self):
        try:
            asyncio.get_running_loop()
            if not self.sync_in_async:
                raise ContextError(
                    f"""You are running in asyncio event loop already. 
                    Using koil makes no sense here, use asyncio instead. You can use koil in a sync context by setting `sync_in_async=True` currently it is
                    set to {self.sync_in_async}.
                    If this happens in a context manager, you probably forgot to use the `async with` syntax."""
                )
        except RuntimeError:
            pass

        koil = global_koil.get()  # type: ignore[assignment]

        if koil is None:
            # We are not in a koiled loop, so we need to create one
            self._loop = get_threaded_loop(
                getattr(
                    self,
                    "name",
                    f"KoiledLoop {'governed by' + self.__class__.__name__ if getattr(self, 'creating_instance', None) else ''}",
                ),
                uvify=getattr(self, "uvify", True),
            )
            global_koil.set(self)
            global_koil_loop.set(self._loop)
        self.running = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ):
        if self.loop:
            # If we started the loop, we need to stop it
            self.loop.call_soon_threadsafe(self.loop.stop)

            iterations = 0

            while self.loop.is_running():
                time.sleep(0.001)
                iterations += 1
                if iterations == 100:
                    logger.warning(
                        "Shutting Down takes longer than expected. Probably we are having loose Threads? Keyboard interrupt?"
                    )

            global_koil.set(None)
            global_koil_loop.set(None)

        self.running = False
