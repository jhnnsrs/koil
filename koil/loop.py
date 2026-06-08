"""Core event-loop management for koil.

This module provides :class:`Koil`, the primary context manager that starts a
dedicated asyncio event loop on a background thread so that async code can be
called from synchronous call sites via :mod:`koil.bridge`.
"""
import asyncio
import os
import sys
import threading
from types import TracebackType
from typing import Protocol, Self
from koil.context import global_koil, global_koil_loop
from koil.errors import ContextError
import logging

try:
    import uvloop  # type: ignore[import]
except ImportError:
    uvloop = None


logger = logging.getLogger(__name__)


class KoilProtocol(Protocol):
    """Minimal protocol satisfied by any synchronous context manager."""

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
    is process-global mutable state: two :class:`Koil` instances entering
    concurrently from different threads could observe each other's temporary
    policy and restore the wrong one (or build a loop under the wrong policy).
    Constructing the desired loop directly is race-free.

    Args:
        uvify: Use ``uvloop`` when available for better throughput. Falls back
            to the stdlib default loop if ``uvloop`` is not installed.

    Returns:
        A new, not-yet-running event loop.
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


def run_threaded_event_loop(loop: asyncio.BaseEventLoop) -> None:
    """Drive *loop* until :meth:`asyncio.AbstractEventLoop.stop` is called.

    Mirrors the cleanup performed by :func:`asyncio.run`: cancels every
    outstanding task, waits for them to finish, shuts down async generators,
    and finally closes the loop.  Runs on the dedicated background thread
    started by :func:`get_threaded_loop`.
    """
    try:
        loop.run_forever()
    finally:
        try:
            tasks = asyncio.all_tasks(loop)
            for task in tasks:
                task.cancel()

            async def gather() -> None:
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
            logger.debug("Loop successfully closed")
            loop.close()


def get_threaded_loop(
    name: str = "KoilLoop", uvify: bool = True
) -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    """Create an event loop and run it on a new daemon thread.

    Returns both the loop and the thread.  The thread must be joined on
    shutdown (see :meth:`Koil.__exit__`) to guarantee that ``loop.close()``
    has completed before the caller proceeds.

    Args:
        name: Name assigned to the background thread, visible in stack traces.
        uvify: Passed through to :func:`_new_event_loop`.

    Returns:
        ``(loop, thread)`` — the running loop and its hosting thread.
    """
    newloop = _new_event_loop(uvify=uvify)

    th = threading.Thread(target=run_threaded_event_loop, args=(newloop,), name=name)

    th.pydev_do_not_trace = os.getenv("KOIL_DO_TRACE", "0") == "0"
    th.is_pydev_daemon_thread = os.getenv("KOIL_DO_TRACE", "0") == "0"
    th.daemon = True
    th.start()

    return newloop, th


class Koil:
    """Context manager that provides a background asyncio event loop.

    Entering a ``Koil`` in a synchronous context starts a dedicated event loop
    on a daemon thread and registers it as the ambient koil loop via
    :data:`~koil.context.global_koil_loop`.  Functions in :mod:`koil.bridge`
    (``unkoil``, ``run_threaded``, etc.) use this loop to execute async code
    without blocking the calling thread.

    Entering a ``Koil`` inside an already-running asyncio loop is allowed when
    *sync_in_async* is ``True`` (the default); the existing loop is reused and
    no new thread is spawned.

    Example::

        with Koil():
            result = unkoil(my_async_function, arg1, arg2)

    ``Koil`` also supports the ``async with`` protocol for use inside existing
    async code, where it simply captures the running loop without starting a new
    thread.
    """

    def __init__(
        self,
        sync_in_async: bool = True,
        uvify: bool = True,
    ) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self.running = False
        self.sync_in_async = sync_in_async
        self.cancel_timeout = 2.0

    def exit(self) -> None:
        """Convenience alias for ``__exit__(None, None, None)``."""
        return self.__exit__(None, None, None)

    async def aenter(self) -> "Koil":
        """Convenience alias for ``await __aenter__()``."""
        return await self.__aenter__()

    def enter(self) -> "Koil":
        """Convenience alias for ``__enter__()``."""
        return self.__enter__()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        """The running event loop managed by this instance.

        Raises :class:`RuntimeError` if accessed before the context is entered.
        """
        if self._loop is None:
            raise RuntimeError("Loop is not running. This should not happen")
        return self._loop

    async def __aenter__(self) -> "Koil":
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

    def __enter__(self) -> "Koil":
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
    ) -> None:
        if self._loop:
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
                    self._loop_thread.join()
                self._loop_thread = None

            # Drop the reference so a second __exit__ doesn't call
            # call_soon_threadsafe(stop) on an already-closed loop.
            self._loop = None
            global_koil.set(None)
            global_koil_loop.set(None)

        self.running = False
