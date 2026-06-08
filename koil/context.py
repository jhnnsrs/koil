"""Context variables and thread-safe primitives used across koil.

This module owns every piece of mutable ambient state that koil threads and the
event loop need to share: the active :class:`~koil.loop.Koil` instance, the loop
it runs on, and the per-task cancellation event injected by
:func:`~koil.bridge.run_threaded`.
"""
import contextvars
from koil.errors import ThreadCancelledError
from typing import Optional, Protocol
import asyncio


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


class KoilThreadSafeEvent(asyncio.Event):
    """An ``asyncio.Event`` that can be set/cleared safely from any thread.

    The cancel signal in koil originates from arbitrary threads — a sync caller
    invoking ``KoilFuture.cancel()``, or the koil loop cancelling a spawned task
    — but must be awaited *inside* the koil loop. ``asyncio.Event`` is not
    thread-safe to mutate from outside its loop, so :meth:`set` and :meth:`clear`
    are routed through ``loop.call_soon_threadsafe``.

    :meth:`asyncio.Event.is_set` (a plain bool read) is left unchanged and is
    used by cooperative sync consumers such as :func:`check_cancelled`.

    The loop is bound at construction time so that :meth:`set`/:meth:`clear`
    always target the right koil loop, even when called from a thread that has
    no running loop of its own.
    """

    def __init__(self, koil_loop: asyncio.AbstractEventLoop) -> None:
        super().__init__()
        self._loop = koil_loop

    def set(self) -> None:
        """Schedule ``asyncio.Event.set`` on the koil loop (thread-safe)."""
        self._loop.call_soon_threadsafe(super().set)

    def clear(self) -> None:
        """Schedule ``asyncio.Event.clear`` on the koil loop (thread-safe)."""
        self._loop.call_soon_threadsafe(super().clear)


global_koil: contextvars.ContextVar[Optional[KoiledLoop]] = contextvars.ContextVar(
    "GLOBAL_KOIL", default=None
)
"""The :class:`~koil.loop.Koil` (or compatible) instance active in the current context.

Set on ``__enter__`` / ``__aenter__`` and cleared on ``__exit__`` / ``__aexit__``.
"""

global_koil_loop: contextvars.ContextVar[Optional[asyncio.AbstractEventLoop]] = (
    contextvars.ContextVar("GLOBAL_KOIL_LOOP", default=None)
)
"""The event loop managed by the active koil instance.

Stored separately from :data:`global_koil` so worker code can retrieve the loop
without importing the concrete ``Koil`` class.
"""

current_koil = contextvars.ContextVar("current_koil", default=None)

current_cancel_event: contextvars.ContextVar[Optional[asyncio.Event]] = (
    contextvars.ContextVar("current_cancel_event", default=None)
)
"""Per-task cancellation event injected by :func:`~koil.bridge.run_threaded`.

Worker threads read :meth:`~asyncio.Event.is_set` via :func:`check_cancelled`;
the event is set from the loop when the enclosing asyncio task is cancelled.
"""


def check_cancelled() -> None:
    """Raise :class:`~koil.errors.ThreadCancelledError` if the current task is cancelled.

    Call this periodically inside long-running sync code executed via
    :func:`~koil.bridge.run_threaded` to cooperate with asyncio cancellation.
    It is a no-op when there is no active cancel event (i.e. outside a koil
    worker thread).
    """
    cancel_event = current_cancel_event.get()
    if cancel_event and cancel_event.is_set():
        raise ThreadCancelledError("Task was cancelled")
