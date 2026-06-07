import asyncio
import os
import sys
import threading
from types import TracebackType
from typing import Protocol, Self
from koil.vars import global_koil, global_koil_loop
from koil.errors import ContextError
import logging

try:
    import uvloop  # type: ignore[import]
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


def _new_event_loop(uvify: bool = True) -> asyncio.AbstractEventLoop:
    """Create a fresh event loop without touching the process-global policy.

    The previous implementation temporarily swapped ``asyncio``'s global event
    loop policy via ``set_event_loop_policy`` and restored it afterwards. That
    is process-global mutable state: two ``Koil`` instances entering
    concurrently from different threads could observe each other's temporary
    policy and restore the wrong one (or build a loop under the wrong policy).
    Constructing the desired loop directly is race-free.
    """
    if uvify:
        if uvloop:
            return uvloop.new_event_loop()  # type: ignore[no-any-return]
        logger.info("uvloop not installed, using default policy")
        return asyncio.new_event_loop()
    elif (
        sys.version_info >= (3, 8)
        and os.name == "nt"
        and hasattr(asyncio, "SelectorEventLoop")
    ):
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()


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


def get_threaded_loop(
    name: str = "KoilLoop", uvify: bool = True
) -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    """Creates a new event loop and runs it in a new thread.

    Returns both the loop and the thread so the thread can be joined on
    shutdown (see Koil.__exit__).
    """
    newloop = _new_event_loop(uvify=uvify)

    th = threading.Thread(target=run_threaded_event_loop, args=(newloop,), name=name)

    th.pydev_do_not_trace = os.getenv("KOIL_DO_TRACE", "0") == "0"
    th.is_pydev_daemon_thread = os.getenv("KOIL_DO_TRACE", "0") == "0"
    th.daemon = True
    th.start()

    return newloop, th


class Koil:
    def __init__(
        self,
        sync_in_async: bool = True,
        uvify: bool = True,
    ):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
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
        exc_tb: TracebackType | None,
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
            self._loop, self._loop_thread = get_threaded_loop(
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
        if self._loop:
            # If we started the loop, we need to stop it
            self._loop.call_soon_threadsafe(self._loop.stop)

            if self._loop_thread is not None:
                # Join the loop thread instead of busy-polling `is_running()`.
                # `is_running()` flips False/True/False during the loop's
                # shutdown sequence (cancel tasks -> gather -> close), so polling
                # it can return before the loop is actually closed. Joining the
                # thread guarantees we only return once `loop.close()` has run.
                self._loop_thread.join(timeout=self.cancel_timeout)
                if self._loop_thread.is_alive():
                    logger.warning(
                        "Shutting Down takes longer than expected. Probably we are "
                        "having loose Threads or a blocked task? Keyboard interrupt?"
                    )
                    # The thread is a daemon, so we wait for a clean close rather
                    # than busy-spinning, but never block the interpreter forever.
                    self._loop_thread.join()
                self._loop_thread = None

            # Drop the reference so a second __exit__ doesn't call
            # call_soon_threadsafe(stop) on an already-closed loop.
            self._loop = None
            global_koil.set(None)
            global_koil_loop.set(None)

        self.running = False
