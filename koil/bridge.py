"""Bridges between synchronous call sites and the koil event loop.

This module contains the core functions that synchronous code uses to call into
the background asyncio event loop managed by :class:`~koil.loop.Koil`:

* :func:`unkoil` — run a single coroutine and block until it completes.
* :func:`unkoil_gen` — drive an async generator as a synchronous generator.
* :func:`unkoil_task` — submit a coroutine without blocking; returns a
  :class:`~koil.utils.KoilFuture`.
* :func:`run_threaded` — run synchronous code on a thread pool from inside
  async code, with koil context propagation and cooperative cancellation.
* :func:`iterate_threaded` — drive a synchronous generator from async code
  one step at a time via :func:`run_threaded`.
* :func:`sleep` — a koil-aware replacement for :func:`time.sleep` that
  respects cancellation.
"""
import asyncio
import threading
import time

from koil.errors import (
    KoilError,
    KoilStopIteration,
    ThreadCancelledError,
)
from typing import AsyncGenerator
from koil.context import (
    current_cancel_event,
    global_koil,
    global_koil_loop,
    KoilThreadSafeEvent,
)
import contextvars
import logging
from typing import Callable, Dict, Tuple, TypeVar
from koil.utils import run_async_sharing_context, KoilFuture
from typing import ParamSpec


from typing import Coroutine, Any, Union, Awaitable, Generator

P = ParamSpec("P")
T = TypeVar("T")
R = TypeVar("R")

SendType = TypeVar("SendType")

KOIL_CANCEL_TIMEOUT = 10.0


def get_koiled_loop_or_raise() -> asyncio.AbstractEventLoop:
    """Return the ambient koil event loop or raise if none is active.

    Validates that:

    1. A koil context has been entered (i.e. :data:`~koil.context.global_koil_loop`
       is set).
    2. The caller is *not* running inside that same loop (which would deadlock).
       The only exception is when :attr:`~koil.loop.Koil.sync_in_async` is
       ``True`` on the active koil instance, which explicitly permits sync-in-
       async usage.
    3. The loop has not been closed.

    Raises:
        KoilError: When no koil context is active, when called from within the
            koil loop without ``sync_in_async``, or when ``sync_in_async`` is
            not enabled on the active instance.
        RuntimeError: When the koil loop has already been closed.

    Returns:
        The running :class:`asyncio.AbstractEventLoop` managed by the active
        :class:`~koil.loop.Koil` instance.
    """
    koil_loop = global_koil_loop.get()

    if not koil_loop:
        raise KoilError("No koil context found")

    try:
        loop0 = asyncio.get_running_loop()
        if koil_loop == loop0:
            raise KoilError(
                "Calling unkoil() from within a running loop. This is not supported"
            )
        else:
            koil = global_koil.get()
            if koil:
                if not koil.sync_in_async:
                    raise KoilError(
                        "Calling unkoil() from within a running loop while koil doesn't allow it. This is not supported"
                    )
            else:
                raise KoilError(
                    "Calling unkoil() from within a running loop while koil doesn't allow it. This is not supported"
                )
    except RuntimeError:
        pass

    if koil_loop.is_closed():
        raise RuntimeError("Loop is not running")

    return koil_loop


def sleep(seconds: float, event_wait_time: float = 0.1) -> None:
    """Sleep for *seconds* in a way that cooperates with koil cancellation.

    When called from inside a koil worker thread, the sleep is implemented via
    a loop callback so it does not block the event loop, and is interrupted
    immediately if the task's cancel event fires.  When called outside any koil
    context the function falls back to :func:`time.sleep`.

    Args:
        seconds: Duration to sleep.
        event_wait_time: How often (in seconds) to poll the cancel event while
            waiting.  Smaller values give faster cancellation response at the
            cost of slightly more CPU usage.

    Raises:
        ThreadCancelledError: If the task is cancelled while sleeping.
    """
    try:
        koil_loop = get_koiled_loop_or_raise()
    except KoilError:
        return time.sleep(seconds)

    event = threading.Event()

    def timer_callback() -> None:
        event.set()

    koil_loop.call_later(seconds, timer_callback)

    while not event.is_set():
        event.wait(timeout=event_wait_time)
        cancel_event = current_cancel_event.get()
        if cancel_event and cancel_event.is_set():
            raise ThreadCancelledError("Sleep was cancelled")


def unkoil_gen(
    iterator: Callable[P, AsyncGenerator[R, SendType]],
    *args: P.args,
    **kwargs: P.kwargs,
) -> Generator[R, SendType, None]:
    """Drive an async generator as a synchronous generator from a koil thread.

    Each iteration step is submitted to the koil event loop and the calling
    thread blocks until the loop delivers the next value.  Values sent into the
    generator via :meth:`~generator.send` are forwarded to the async generator.

    This function must be called from a thread that has an active
    :class:`~koil.loop.Koil` context (i.e. inside a ``with Koil():`` block or
    a :func:`run_threaded` worker).

    Args:
        iterator: An async generator function.
        *args: Positional arguments forwarded to *iterator*.
        **kwargs: Keyword arguments forwarded to *iterator*.

    Yields:
        Values produced by the underlying async generator.

    Raises:
        KoilError: If no koil context is active.
    """
    koil_loop = get_koiled_loop_or_raise()

    ait = iterator(*args, **kwargs).__aiter__()

    future = run_async_sharing_context(ait.__anext__, koil_loop, None)
    send_val = yield future.result()
    while True:
        if send_val is None:
            future = run_async_sharing_context(ait.__anext__, koil_loop, None)
        else:
            future = run_async_sharing_context(ait.__anext__, koil_loop, None, send_val)  # type: ignore

        try:
            send_val = yield future.result()
        except StopAsyncIteration:
            break


def unkoil(
    coro: Union[
        Callable[P, Coroutine[Any, Any, R]],
        Callable[P, Awaitable[R]],
    ],
    *args: P.args,
    **kwargs: P.kwargs,
) -> R:
    """Run a coroutine on the koil loop and block until it returns.

    The coroutine is submitted to the background event loop managed by the
    active :class:`~koil.loop.Koil` context, the calling thread blocks until
    the coroutine completes, and the result (or exception) is returned to the
    caller.

    This is the primary way to call async code from synchronous call sites
    inside a ``with Koil():`` block.

    Args:
        coro: An async function (coroutine function or awaitable-returning
            callable).
        *args: Positional arguments forwarded to *coro*.
        **kwargs: Keyword arguments forwarded to *coro*.

    Returns:
        Whatever *coro* returns.

    Raises:
        KoilError: If no koil context is active.
        Any exception raised by *coro*.
    """
    koil_loop = get_koiled_loop_or_raise()

    context_aware_future = run_async_sharing_context(
        coro, koil_loop, None, *args, **kwargs
    )

    return context_aware_future.result()


TaskReturn = TypeVar("TaskReturn")
TaskArgs = TypeVar("TaskArgs")
TaskNext = TypeVar("TaskNext")


def unkoil_task(
    coro: Union[
        Callable[P, Coroutine[Any, Any, R]],
        Callable[P, Awaitable[R]],
    ],
    *args: P.args,
    **kwargs: P.kwargs,
) -> KoilFuture[R]:
    """Submit a coroutine to the koil loop without blocking.

    Unlike :func:`unkoil`, this function returns immediately with a
    :class:`~koil.utils.KoilFuture` that the caller can wait on, poll, or
    cancel at a later time.

    Args:
        coro: An async function (coroutine function or awaitable-returning
            callable).
        *args: Positional arguments forwarded to *coro*.
        **kwargs: Keyword arguments forwarded to *coro*.

    Returns:
        A :class:`~koil.utils.KoilFuture` representing the pending coroutine.

    Raises:
        KoilError: If no koil context is active.
    """
    koil_loop = get_koiled_loop_or_raise()

    return run_async_sharing_context(
        coro,
        koil_loop,
        None,
        *args,
        **kwargs,
    )


async def run_threaded(
    sync_func: Callable[P, R],
    *sync_args: P.args,
    **sync_kwargs: P.kwargs,
) -> R:
    """Run a synchronous function on the default thread pool with koil context.

    Submits *sync_func* to ``loop.run_in_executor`` and awaits the result.
    Before calling the function, the worker thread is set up with:

    * The caller's :class:`contextvars.Context` (copied via
      :func:`contextvars.copy_context`).
    * :data:`~koil.context.global_koil` and
      :data:`~koil.context.global_koil_loop` so the worker can call
      :func:`unkoil` or :func:`check_cancelled`.
    * A per-call :class:`~koil.context.KoilThreadSafeEvent` as the cancel
      signal.

    If the awaiting coroutine is cancelled, the cancel event is set and the
    function waits for the worker to finish (up to
    :attr:`~koil.loop.Koil.cancel_timeout` seconds) before re-raising
    :class:`asyncio.CancelledError`.

    Args:
        sync_func: A synchronous callable to run on the thread pool.
        *sync_args: Positional arguments forwarded to *sync_func*.
        **sync_kwargs: Keyword arguments forwarded to *sync_func*.

    Returns:
        The return value of *sync_func*.

    Raises:
        asyncio.CancelledError: If the caller's coroutine is cancelled and the
            worker thread acknowledges the cancel within the timeout.
        KoilError: If the worker thread does not finish within the cancel
            timeout.
    """
    loop = asyncio.get_running_loop()

    koil = global_koil.get()

    def wrapper(
        cancel_event: KoilThreadSafeEvent,
        context: contextvars.Context,
        sync_args: Tuple[Any],
        sync_kwargs: Dict[str, Any],
    ) -> R:
        # Run the body *inside* the copied context via Context.run instead of
        # mutating the executor thread's own context with bare .set() calls.
        # run_in_executor reuses pooled threads, so leftover contextvars would
        # otherwise leak into the next, unrelated task scheduled on that thread.
        def body() -> R:
            global_koil.set(koil)
            global_koil_loop.set(loop)
            current_cancel_event.set(cancel_event)

            try:
                return sync_func(*sync_args, **sync_kwargs)  # type: ignore
            except StopIteration as e:
                # Transform so asyncio doesn't swallow it or crash.
                raise RuntimeError("Threaded function raised StopIteration") from e
            except Exception as e:
                raise e

        return context.run(body)

    context = contextvars.copy_context()
    # A thread-safe asyncio.Event so a nested unkoil() inside the worker can
    # await this same event without a bridging thread. The worker checks it via
    # is_set() (a plain bool read); we set() it from the loop on cancellation.
    cancel_event = KoilThreadSafeEvent(loop)

    future = loop.run_in_executor(
        None,
        wrapper,
        cancel_event,
        context,
        sync_args,
        sync_kwargs,
    )  # type: ignore
    try:
        shielded_f = await asyncio.shield(future)
        return shielded_f
    except asyncio.CancelledError as e:
        cancel_event.set()

        try:
            await asyncio.wait_for(future, timeout=koil.cancel_timeout if koil else 10)
        except ThreadCancelledError:
            logging.info("Future in another thread was successfully cancelled")
        except asyncio.TimeoutError as te:
            raise KoilError(
                f"We could not successfully cancel the future {future} in another thread. Make sure you are not blocking the thread with a long running task and check if you call check_cancelled periodically."
            ) from te

        raise e


S = TypeVar("S")


async def iterate_threaded(
    sync_gen: Callable[P, Generator[R, S, None]],
    *sync_args: P.args,
    **sync_kwargs: P.kwargs,
) -> AsyncGenerator[R, S]:
    """Drive a synchronous generator from async code one step at a time.

    Instead of running the whole generator on a dedicated worker thread and
    pumping every value across a sync↔async queue, the generator is resumed one
    ``send()`` per :func:`run_threaded` call. Each step is a short thread hop,
    so :func:`run_threaded` provides context propagation, koil setup, and
    cooperative cancellation for free — no queue needed.

    .. note::
       Because each step runs in a fresh :func:`run_threaded` call, consecutive
       steps may land on different pooled threads. Generators that hold a
       thread-affine resource (e.g. an ``RLock``) across a ``yield``, or that
       mutate a :class:`~contextvars.ContextVar` expecting it to persist into
       the next ``yield``, will not see single-thread behaviour.

    Args:
        sync_gen: A synchronous generator function.
        *sync_args: Positional arguments forwarded to *sync_gen*.
        **sync_kwargs: Keyword arguments forwarded to *sync_gen*.

    Yields:
        Values produced by the underlying synchronous generator.
    """

    generator: Generator[R, S, None] | None = None

    def step(send_value: Any) -> R:
        # Create the generator lazily on the first step so any work done while
        # building it happens in the thread, not on the loop.
        nonlocal generator
        if generator is None:
            generator = sync_gen(*sync_args, **sync_kwargs)
        try:
            return generator.send(send_value)
        except StopIteration:
            # StopIteration interacts badly with futures, so wrap it and let
            # the async side recognise the end of iteration.
            raise KoilStopIteration("Generator exhausted")

    send_value: Any = None
    while True:
        try:
            value = await run_threaded(step, send_value)
        except KoilStopIteration:
            return

        try:
            send_value = yield value
        except GeneratorExit:
            # Consumer stopped early: the generator is still suspended at a
            # yield. Close it in a worker thread so its finally blocks run,
            # then propagate GeneratorExit.
            if generator is not None:
                await run_threaded(generator.close)
            raise
