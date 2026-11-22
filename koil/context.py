"""Context variables and thread-safe primitives used across koil.

This module owns every piece of mutable ambient state that koil threads and the
event loop need to share: the active :class:`~koil.loop.Koil` instance, the loop
it runs on, the per-task cancellation event injected by
:func:`~koil.bridge.run_threaded`, and the per-loop bookkeeping (worker pool,
calls still in flight) of the loops koil itself started.
"""

import asyncio
import concurrent.futures
import contextvars
import logging
import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, Protocol

from koil.errors import ThreadCancelledError

logger = logging.getLogger(__name__)


class KoiledLoop(Protocol):
    """Structural interface expected of a Koil-compatible context manager.

    Internal code depends only on this protocol rather than the concrete
    :class:`~koil.loop.Koil` class, avoiding circular imports and making the
    behaviour testable with lightweight stubs.
    """

    @property
    def loop(self) -> asyncio.AbstractEventLoop: ...

    @property
    def cancel_timeout(self) -> float: ...

    @property
    def sync_in_async(self) -> bool: ...


def _noop() -> None:
    return None


class KoilThreadSafeEvent:
    """A cancel signal that any thread can set, read, block on, or await.

    The cancel signal in koil originates from arbitrary threads — a sync caller
    invoking ``KoilFuture.cancel()``, or the koil loop cancelling a spawned task
    — and is consumed from arbitrary threads too: a worker reads it in
    :func:`check_cancelled`, blocks on it in :func:`~koil.bridge.sleep`, and the
    loop reacts to it to cancel the coroutine a worker is waiting for.

    * :meth:`set` takes effect immediately, on the calling thread: the next
      :meth:`is_set` anywhere returns ``True``, whether or not the loop is
      running.
    * :meth:`wait_blocking` blocks a thread until the event is set (or a
      timeout elapses) without polling.
    * :meth:`add_callback` registers a function that runs once when the event
      is set. Callbacks run on the *setting* thread, so they must be
      thread-safe and must not block (koil's own only schedule work on the
      loop).
    * :meth:`wait` is the awaitable form, for code that used this event as an
      :class:`asyncio.Event`.

    Args:
        koil_loop: The koil loop this event belongs to. Kept for
            backwards-compatibility; the event no longer needs it.
    """

    def __init__(self, koil_loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._loop = koil_loop
        self._flag = threading.Event()
        self._lock = threading.Lock()
        self._callbacks: dict[int, Callable[[], None]] = {}
        self._next_key = 0

    def is_set(self) -> bool:
        """Return ``True`` once :meth:`set` has been called."""
        return self._flag.is_set()

    def set(self) -> None:
        """Set the event and run the registered callbacks (thread-safe)."""
        with self._lock:
            if self._flag.is_set():
                return
            self._flag.set()
            callbacks = list(self._callbacks.values())
            self._callbacks.clear()
        for callback in callbacks:
            try:
                callback()
            except Exception:
                logger.debug("Cancel callback %r failed", callback, exc_info=True)

    def clear(self) -> None:
        """Reset the event (thread-safe)."""
        with self._lock:
            self._flag.clear()

    def wait_blocking(self, timeout: float | None = None) -> bool:
        """Block the calling thread until the event is set.

        Returns:
            ``True`` if the event is set, ``False`` if *timeout* elapsed first.
        """
        return self._flag.wait(timeout)

    def add_callback(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Run *callback* once when the event is set.

        If the event is already set the callback runs right away, on the
        calling thread.

        Returns:
            A function that unregisters the callback again. Calling it after
            the callback ran (or twice) is harmless.
        """
        with self._lock:
            if not self._flag.is_set():
                key = self._next_key
                self._next_key += 1
                self._callbacks[key] = callback

                def remove() -> None:
                    with self._lock:
                        self._callbacks.pop(key, None)

                return remove
        callback()
        return _noop

    async def wait(self) -> bool:
        """Wait until the event is set. Must be awaited inside a running loop."""
        if self._flag.is_set():
            return True
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[None] = loop.create_future()

        def wake() -> None:
            if not waiter.done():
                waiter.set_result(None)

        def wake_threadsafe() -> None:
            try:
                loop.call_soon_threadsafe(wake)
            except RuntimeError:
                pass  # the loop is gone, and so is whoever was waiting on it

        remove = self.add_callback(wake_threadsafe)
        try:
            await waiter
        finally:
            remove()
        return True


global_koil: contextvars.ContextVar[KoiledLoop | None] = contextvars.ContextVar(
    "GLOBAL_KOIL", default=None
)
"""The :class:`~koil.loop.Koil` (or compatible) instance active in the current context.

Set on ``__enter__`` / ``__aenter__`` and cleared on ``__exit__`` / ``__aexit__``.
"""

global_koil_loop: contextvars.ContextVar[asyncio.AbstractEventLoop | None] = (
    contextvars.ContextVar("GLOBAL_KOIL_LOOP", default=None)
)
"""The event loop managed by the active koil instance.

Stored separately from :data:`global_koil` so worker code can retrieve the loop
without importing the concrete ``Koil`` class.
"""

current_koil = contextvars.ContextVar("current_koil", default=None)

current_cancel_event: contextvars.ContextVar[KoilThreadSafeEvent | None] = (
    contextvars.ContextVar("current_cancel_event", default=None)
)
"""Per-task cancellation event injected by :func:`~koil.bridge.run_threaded`.

Worker threads read :meth:`~KoilThreadSafeEvent.is_set` via
:func:`check_cancelled`; the event is set from the loop when the enclosing
asyncio task is cancelled.
"""


class BlockedCall:
    """One thread blocked in :func:`~koil.bridge.unkoil`, and the task it waits for."""

    __slots__ = ("thread", "task", "active")

    def __init__(self, thread: int) -> None:
        self.thread = thread
        self.task: asyncio.Task[Any] | None = None
        self.active = True


blocked_calls: contextvars.ContextVar[tuple[BlockedCall, ...]] = contextvars.ContextVar(
    "KOIL_BLOCKED_CALLS", default=()
)
"""The chain of blocked sync callers the current task is running on behalf of.

Each entry is a thread that cannot run anything until the task it waits for
returns. The chain only describes the task of its *last* entry (a task spawned
from it inherits the value but nobody is blocked on that one), so readers check
``chain[-1].task is asyncio.current_task()`` first. It lets code on the loop
see that hopping to one of these threads would deadlock (see
:class:`~koil.qt.qt_to_async`).
"""

current_callback: contextvars.ContextVar[object | None] = contextvars.ContextVar(
    "KOIL_CURRENT_CALLBACK", default=None
)
"""The future/generator a foreign event loop (Qt) is currently being called for.

Set while the slot of a :class:`~koil.qt.qt_to_async` call runs, so a
``resolve()`` can tell whether it happens inside that call's own context or
somewhere unrelated — only the former has ContextVar changes worth handing
back to the awaiting task.
"""

INTERNAL_VARS: frozenset[contextvars.ContextVar[Any]] = frozenset(
    (
        global_koil,
        global_koil_loop,
        current_cancel_event,
        blocked_calls,
        current_callback,
    )
)
"""ContextVars holding koil's own bookkeeping.

They are never copied *back* across a bridge: the loop/koil references are
already correct on the calling side, and the cancel event and blocked-call
chain describe one call, so they are meaningless (and would leak) outside it.
"""


def ambient_koil() -> KoiledLoop | None:
    """The koil active in the current context, if it is usable from this process.

    A forked child inherits the parent's context, and with it a koil whose
    loop thread only exists in the parent. That one counts as absent: the
    child has to enter a koil of its own.
    """
    koil = global_koil.get()
    if koil is not None and getattr(koil, "pid", None) not in (None, os.getpid()):
        return None
    return koil


def check_cancelled() -> None:
    """Raise :class:`~koil.errors.ThreadCancelledError` if the current task is cancelled.

    Call this periodically inside long-running sync code executed via
    :func:`~koil.bridge.run_threaded` to cooperate with asyncio cancellation.
    It is a no-op when there is no active cancel event (i.e. outside a koil
    worker thread, or inside :func:`shielded`).
    """
    cancel_event = current_cancel_event.get()
    if cancel_event and cancel_event.is_set():
        raise ThreadCancelledError("Task was cancelled")


@contextmanager
def shielded() -> Iterator[None]:
    """Run a block of sync code shielded from the worker's cancellation.

    Cancellation of a :func:`~koil.bridge.run_threaded` worker is *level*
    triggered: once the worker is cancelled, every :func:`check_cancelled`,
    :func:`~koil.bridge.sleep` and :func:`~koil.bridge.unkoil` in it raises
    :class:`~koil.errors.ThreadCancelledError`. That is what makes a cancelled
    worker stop, but it would also stop the cleanup the worker runs on its way
    out. Inside ``with shielded():`` the cancel signal is hidden, so async
    teardown still works::

        try:
            work()
        finally:
            with shielded():
                unkoil(connection.aclose)

    The ``__exit__`` of a :func:`~koil.decorators.koilable` class and of
    :func:`~koil.decorators.koiled_cm` is shielded already. Keep shielded
    blocks short: a cancelled worker is only waited on for
    :attr:`~koil.loop.Koil.cancel_timeout` seconds.
    """
    token = current_cancel_event.set(None)
    try:
        yield
    finally:
        current_cancel_event.reset(token)


class LoopState:
    """What koil keeps per event loop it started: the worker pool and the calls in flight."""

    def __init__(self, executor: concurrent.futures.Executor | None) -> None:
        self.executor = executor
        self.pid = os.getpid()
        self._lock = threading.Lock()
        self._pending: set[concurrent.futures.Future[Any]] = set()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def track(self, future: "concurrent.futures.Future[Any]") -> None:
        """Remember *future* so it can be failed if the loop stops before resolving it."""
        with self._lock:
            if self._closed:
                raise RuntimeError("Loop is not running")
            self._pending.add(future)
        future.add_done_callback(self._forget)

    def _forget(self, future: "concurrent.futures.Future[Any]") -> None:
        with self._lock:
            self._pending.discard(future)

    def close(self, error: Callable[[], BaseException]) -> None:
        """Fail every call the loop left unresolved and refuse new ones."""
        with self._lock:
            self._closed = True
            pending = list(self._pending)
            self._pending.clear()
        for future in pending:
            try:
                future.set_exception(error())
            except concurrent.futures.InvalidStateError:
                pass  # resolved in the meantime


_loop_states: dict[asyncio.AbstractEventLoop, LoopState] = {}


def register_loop(
    loop: asyncio.AbstractEventLoop, executor: concurrent.futures.Executor | None
) -> LoopState:
    """Start keeping state for a loop koil runs. Paired with :func:`unregister_loop`."""
    state = LoopState(executor)
    _loop_states[loop] = state
    return state


def unregister_loop(loop: asyncio.AbstractEventLoop) -> LoopState | None:
    """Stop keeping state for *loop*; returns what was kept."""
    return _loop_states.pop(loop, None)


def loop_state(loop: asyncio.AbstractEventLoop) -> LoopState | None:
    """The state koil keeps for *loop*, or ``None`` for a loop koil did not start."""
    return _loop_states.get(loop)


THREAD_STYLE_HINT = (
    "A plain threading.Thread does not inherit context variables, and the koil "
    "context is one. Start the thread inside the context instead: "
    "`threading.Thread(target=contextvars.copy_context().run, args=(fn,))`; "
    "from async code, `await run_threaded(fn)` does that for you."
)
"""How to start a thread that can use the koil context it was started in."""


def koil_running_elsewhere() -> bool:
    """Whether a koil loop runs in this process that the current context does not see.

    Only meaningful when there is no ambient koil here: it then tells a thread
    that simply lost the context (a ``threading.Thread`` started inside
    ``with Koil():``) from code that never had one.
    """
    pid = os.getpid()
    return any(
        state.pid == pid and not state.closed for state in list(_loop_states.values())
    )
