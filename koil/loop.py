"""Core event-loop management for koil.

This module provides :class:`Koil`, the primary context manager that starts a
dedicated asyncio event loop on a background thread so that async code can be
called from synchronous call sites via :mod:`koil.bridge`.
"""
import asyncio
import contextvars
import os
import sys
import threading
from types import TracebackType
from typing import Any, Dict, List, Optional, Protocol, Self, Tuple
from koil.context import global_koil, global_koil_loop
from koil.errors import ContextError
import logging

try:
    import uvloop  # type: ignore[import]
except ImportError:
    uvloop = None


logger = logging.getLogger(__name__)

#: Default time, in seconds, that :meth:`Koil.__exit__` waits for the loop
#: thread to stop *after* the initial graceful :attr:`Koil.cancel_timeout` wait
#: has already elapsed, before giving up and abandoning the thread.
#:
#: The loop thread is a daemon, so abandoning it does not block interpreter
#: exit; this bound exists so that uncancellable work on the loop (a blocking
#: call with no ``await``, or a ``run_threaded`` worker that never checks
#: :func:`~koil.context.check_cancelled`) cannot hang ``__exit__`` forever.
#:
#: This is the process-wide default. Override it globally by reassigning this
#: module attribute, or per instance via the ``shutdown_join_timeout`` argument
#: to :class:`Koil`. It is intentionally *separate* from
#: :attr:`Koil.cancel_timeout`, which bounds the initial graceful wait.
SHUTDOWN_JOIN_TIMEOUT: float = 5.0


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

    By default the loop thread is left visible to step debuggers, so a
    breakpoint inside a coroutine running on it (the async "glue" between
    :func:`~koil.bridge.run_threaded` workers) is hit. Set the ``KOIL_DO_TRACE``
    environment variable to a falsy value (``0``/``false``/``no``/``off``) to
    hide the loop thread from the debugger instead (the pre-3.4 behavior). The
    ``pydev_do_not_trace`` / ``is_pydev_daemon_thread`` markers set here are
    pydevd/debugpy conventions and are inert unless a debugger is attached, so
    tracing costs nothing outside a debug session.

    Args:
        name: Name assigned to the background thread, visible in stack traces.
        uvify: Passed through to :func:`_new_event_loop`.

    Returns:
        ``(loop, thread)`` — the running loop and its hosting thread.
    """
    newloop = _new_event_loop(uvify=uvify)

    th = threading.Thread(target=run_threaded_event_loop, args=(newloop,), name=name)

    # Hide the loop thread from the debugger only when KOIL_DO_TRACE is set to a
    # falsy value; otherwise trace it so coroutine breakpoints work by default.
    hide_from_debugger = os.environ.get("KOIL_DO_TRACE", "").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    )
    th.pydev_do_not_trace = hide_from_debugger
    th.is_pydev_daemon_thread = hide_from_debugger
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

    Entering a ``Koil`` (with the sync ``with`` syntax) inside an
    already-running asyncio loop is allowed when *sync_in_async* is ``True``
    (the default): a background koil loop is still started, and sync bridge
    calls will block the calling (async) thread while work runs on it. Set
    ``sync_in_async=False`` to make that a :class:`~koil.errors.ContextError`
    instead.

    Re-entering the same instance (nested ``with`` blocks, or from several
    threads) is safe: one loop is started, every enter gets ambient access to
    it, and only the enter that started the loop stops it on exit.

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
        shutdown_join_timeout: float | None = None,
        rewrite_tracebacks: bool = True,
        cancel_timeout: float = 2.0,
    ) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        #: True only when this instance *created* self._loop (sync __enter__).
        #: An async-captured loop (``async with``) is never owned and must
        #: never be stopped by this instance.
        self._owns_loop = False
        #: Guards loop creation when the same instance is entered from
        #: several threads at once: exactly one thread starts the loop, the
        #: others reuse it.
        self._creation_lock = threading.Lock()
        #: Per-thread stack of enter records, so nested ``with`` blocks on the
        #: *same* instance pair up: only the enter that started the loop tears
        #: it down, and each exit resets exactly the ContextVar tokens its
        #: matching enter set.
        self._sync_entries = threading.local()
        #: Tokens set by __aenter__, reset LIFO by __aexit__.
        self._async_tokens: List[
            Tuple[contextvars.Token[Any], contextvars.Token[Any]]
        ] = []
        self.running = False
        self.sync_in_async = sync_in_async
        self.uvify = uvify
        #: Seconds to wait for a cancelled worker to acknowledge cancellation:
        #: both when a run_threaded task is cancelled and as the initial
        #: graceful wait in __exit__. A plain attribute, so it can also be
        #: adjusted after construction.
        self.cancel_timeout = cancel_timeout
        #: Drop koil-internal frames from tracebacks of exceptions that cross
        #: the bridge (see :mod:`koil.tracebacks`). The KOIL_FULL_TRACEBACK=1
        #: environment variable forces full tracebacks regardless.
        self.rewrite_tracebacks = rewrite_tracebacks
        #: Extra grace (seconds) to wait for the loop thread to stop on exit
        #: before abandoning it. ``None`` uses :data:`SHUTDOWN_JOIN_TIMEOUT`.
        self.shutdown_join_timeout = shutdown_join_timeout

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

    def _sync_entry_stack(self) -> List[Dict[str, Any]]:
        stack = getattr(self._sync_entries, "stack", None)
        if stack is None:
            stack = []
            self._sync_entries.stack = stack
        return stack

    @staticmethod
    def _reset_token(
        var: contextvars.ContextVar[Any], token: Optional[contextvars.Token[Any]]
    ) -> None:
        """Reset *var* via *token*, falling back to clearing it when the token
        belongs to a different context (an enter/exit pair split across
        contexts, e.g. via ``Context.run``)."""
        if token is None:
            return
        try:
            var.reset(token)
        except ValueError:
            var.set(None)

    async def __aenter__(self) -> "Koil":
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return self

        self._loop = loop
        # Set BOTH ambient vars (the old implementation set only the loop,
        # yet cleared only the koil on exit): worker threads spawned from this
        # loop resolve sync_in_async / cancel_timeout through global_koil, and
        # the tokens let __aexit__ restore whatever was ambient before.
        self._async_tokens.append(
            (global_koil.set(self), global_koil_loop.set(loop))
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._async_tokens:
            koil_token, loop_token = self._async_tokens.pop()
            self._reset_token(global_koil, koil_token)
            self._reset_token(global_koil_loop, loop_token)
        if not self._async_tokens and not self._owns_loop:
            # Drop the captured (never owned) loop reference so a later sync
            # __exit__ cannot mistake the user's loop for one to stop.
            self._loop = None
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

        entry: Dict[str, Any] = {
            "started_loop": False,
            "koil_token": None,
            "loop_token": None,
        }

        if global_koil.get() is None:
            with self._creation_lock:
                if self._loop is None:
                    self._loop, self._loop_thread = get_threaded_loop(
                        getattr(
                            self,
                            "name",
                            f"KoiledLoop {'governed by' + self.__class__.__name__ if getattr(self, 'creating_instance', None) else ''}",
                        ),
                        uvify=getattr(self, "uvify", True),
                    )
                    self._owns_loop = True
                    entry["started_loop"] = True
            entry["koil_token"] = global_koil.set(self)
            entry["loop_token"] = global_koil_loop.set(self._loop)

        self._sync_entry_stack().append(entry)
        self.running = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        stack = self._sync_entry_stack()
        if stack:
            entry = stack.pop()
            self._reset_token(global_koil, entry["koil_token"])
            self._reset_token(global_koil_loop, entry["loop_token"])
        else:
            # Unmatched exit — e.g. called directly from a thread that never
            # entered (a Qt close handler on another thread). Fall back to
            # tearing down the loop if this instance owns one, clearing the
            # ambient vars in this context like the pre-token implementation.
            entry = {"started_loop": self._owns_loop and self._loop is not None}
            if entry["started_loop"]:
                global_koil.set(None)
                global_koil_loop.set(None)

        if entry["started_loop"] and self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)

            if self._loop_thread is not None:
                # Join the loop thread instead of busy-polling `is_running()`.
                # `is_running()` flips False/True/False during the loop's
                # shutdown sequence (cancel tasks -> gather -> close), so polling
                # it can return before the loop is actually closed. Joining the
                # thread guarantees we only return once `loop.close()` has run.
                self._loop_thread.join(timeout=self.cancel_timeout)
                if self._loop_thread.is_alive():
                    # Graceful stop didn't land within cancel_timeout. Wait a
                    # bounded bit longer, then abandon the thread rather than
                    # block the caller forever. This happens when the loop is
                    # wedged on uncancellable work (a blocking call with no
                    # await, or a run_threaded worker that never checks
                    # check_cancelled), so loop.stop() never gets to run.
                    join_timeout = (
                        SHUTDOWN_JOIN_TIMEOUT
                        if self.shutdown_join_timeout is None
                        else self.shutdown_join_timeout
                    )
                    logger.warning(
                        "Koil loop thread %r did not stop within %.1fs; waiting "
                        "up to %.1fs more before abandoning it. A coroutine is "
                        "likely blocking the event loop. Keyboard interrupt?",
                        self._loop_thread.name,
                        self.cancel_timeout,
                        join_timeout,
                    )
                    self._loop_thread.join(timeout=join_timeout)
                    if self._loop_thread.is_alive():
                        logger.warning(
                            "Koil loop thread %r is STILL running after %.1fs and "
                            "is being abandoned. This means uncancellable work is "
                            "blocking the event loop (a blocking call with no "
                            "await, or a run_threaded worker that never checks "
                            "check_cancelled). The thread is a daemon and will be "
                            "killed on interpreter exit, but resources it holds "
                            "will not be released cleanly.",
                            self._loop_thread.name,
                            self.cancel_timeout + join_timeout,
                        )
                self._loop_thread = None

            # Drop the reference so a second __exit__ doesn't call
            # call_soon_threadsafe(stop) on an already-closed loop.
            self._loop = None
            self._owns_loop = False

        self.running = bool(stack)
