"""Bridges between synchronous call sites and the koil event loop.

This module contains the core functions that synchronous code uses to call into
the background asyncio event loop managed by :class:`~koil.loop.Koil`:

* :func:`unkoil` — run a single coroutine and block until it completes.
* :func:`unkoil_gen` — drive an async generator as a synchronous generator.
* :func:`unkoil_task` — submit a coroutine without blocking; returns a
  :class:`~koil.utils.KoilFuture`.
* :func:`run_threaded` — run synchronous code on a thread pool from inside
  async code, with copy-in koil context propagation and cooperative
  cancellation. :func:`run_threaded_bridged` is the bidirectional variant.
* :func:`iterate_threaded` — drive a synchronous generator from async code
  one step at a time via :func:`run_threaded`.
  :func:`iterate_threaded_bridged` is the bidirectional variant.
* :func:`sleep` — a koil-aware replacement for :func:`time.sleep` that
  respects cancellation.

Cancellation surfaces in sync code as one of two exceptions:
:class:`~koil.errors.ThreadCancelledError` when the worker thread running the
code was cancelled, and :class:`~koil.errors.CancelledError` when a
:class:`~koil.utils.KoilFuture` was cancelled or the loop shut down under it.
"""

import asyncio
import contextvars
import inspect
import logging
import os
import threading
import time
import warnings
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine, Generator
from typing import Any, ParamSpec, TypeVar

from koil.context import (
    INTERNAL_VARS,
    THREAD_STYLE_HINT,
    koil_running_elsewhere,
    KoilThreadSafeEvent,
    blocked_calls,
    current_cancel_event,
    global_koil,
    global_koil_loop,
    loop_state,
)
from koil.errors import (
    CancelledError,
    KoilError,
    KoilStopIteration,
    ThreadCancelledError,
)
from koil.tracebacks import prune_traceback, should_rewrite
from koil.utils import (
    AsyncGenDriver,
    KoilFuture,
    _context_delta,
    run_async_blocking,
    run_async_sharing_context,
    timeout_backstop,
)

logger = logging.getLogger(__name__)

P = ParamSpec("P")
T = TypeVar("T")
R = TypeVar("R")

SendType = TypeVar("SendType")

KOIL_CANCEL_TIMEOUT = 10.0


def _reject_awaitable_object(value: Any, func_name: str) -> None:
    """Raise a pointed TypeError when *value* is an already-created coroutine
    or async-generator object instead of the async function itself.

    ``unkoil(fetch("url"))`` is a natural mistake for anyone used to
    ``asyncio.run``; without this guard it fails much later with an opaque
    "'coroutine' object is not callable".
    """
    if inspect.iscoroutine(value):
        value.close()  # suppress the "coroutine was never awaited" warning
        raise TypeError(
            f"{func_name}() expects an async function, not a coroutine object. "
            f"Pass the function and its arguments separately: "
            f"{func_name}(fn, *args) — not {func_name}(fn(*args))."
        )
    if inspect.isasyncgen(value):
        raise TypeError(
            f"{func_name}() expects an async generator function, not an async "
            f"generator object. Pass the function and its arguments separately: "
            f"{func_name}(fn, *args) — not {func_name}(fn(*args))."
        )


def _await_it_instead(fn: Callable[..., Any] | None) -> str:
    """The fix for calling a sync koil wrapper from async code, spelled out for *fn*."""
    if fn is None:
        return (
            "Use the async variant directly instead, usually the `a`-prefixed "
            "method, e.g. `await theholder.amethod(...)` rather than `theholder.method(...)`."
        )
    name = getattr(fn, "__qualname__", None) or getattr(fn, "__name__", repr(fn))
    name = name.rpartition("<locals>.")[
        2
    ]  # `Task.aprogress`, not `make.<locals>.Task...`
    if inspect.isasyncgenfunction(fn):
        return f"Iterate the async generator directly instead: `async for ... in {name}(...)`."
    return f"Await the async function directly instead: `await {name}(...)`."


def get_koiled_loop_or_raise(
    *, fn: Callable[..., Any] | None = None
) -> asyncio.AbstractEventLoop:
    """Return the ambient koil event loop or raise if none is active.

    Validates that:

    1. A koil context has been entered (i.e. :data:`~koil.context.global_koil_loop`
       is set).
    2. The caller is *not* running inside that same loop (which would deadlock).
       The only exception is when :attr:`~koil.loop.Koil.sync_in_async` is
       ``True`` on the active koil instance, which explicitly permits sync-in-
       async usage.
    3. The loop has not been closed.

    Args:
        fn: The async function the caller wants to run on the loop. Only used to
            name the fix in the error message when the caller is async code that
            reached for a sync wrapper.

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

    try:
        running_loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None

    if koil_loop is not None:
        owner_pid = getattr(global_koil.get(), "pid", None)
        if owner_pid is not None and owner_pid != os.getpid():
            # The loop thread did not survive the fork; submitting to the loop
            # would block this process forever.
            raise KoilError(
                "This koil context was entered in another process "
                f"(pid {owner_pid}) and its event loop does not run in this one "
                f"(pid {os.getpid()}): a forked child does not inherit the loop "
                "thread. Enter a koil context in the child process itself "
                "(`with Koil():`, or enter the client again there)."
            )

    if not koil_loop:
        if running_loop is not None:
            # The common mistake: a sync wrapper (task.progress, x.call, ...) called
            # from an `async def`. There is a loop -- this one -- but blocking on it
            # from inside itself would deadlock, and there is no koil loop to hop to.
            raise KoilError(
                "No koil context found: a sync koil wrapper (unkoil) was called from "
                "inside a running event loop, i.e. from async code, where it cannot "
                "block. " + _await_it_instead(fn)
            )
        if koil_running_elsewhere():
            # A koil loop runs in this process, just not for this thread.
            raise KoilError(
                "No koil context found in thread "
                f"{threading.current_thread().name!r}, although a koil loop is "
                f"running in this process. {THREAD_STYLE_HINT}"
            )
        raise KoilError(
            "No koil context found: unkoil needs a koil loop to run on. Enter "
            "`with Koil():` first, or call from a thread koil started "
            "(e.g. via run_threaded)."
        )

    if running_loop is not None:
        koil = global_koil.get()
        if koil_loop == running_loop:
            raise KoilError(
                "Calling unkoil() from within the koil loop itself, which would "
                "deadlock. " + _await_it_instead(fn)
            )
        if not koil or not koil.sync_in_async:
            raise KoilError(
                "Calling unkoil() from within a running event loop while koil doesn't "
                "allow sync-in-async (Koil(sync_in_async=True)). "
                + _await_it_instead(fn)
            )

    if koil_loop.is_closed():
        raise RuntimeError("Loop is not running")

    return koil_loop


def sleep(seconds: float, event_wait_time: float = 0.1) -> None:
    """Sleep for *seconds* in a way that cooperates with koil cancellation.

    Inside a koil worker thread the sleep ends the moment the task's cancel
    event fires. Outside one (no cancel event, or inside
    :func:`~koil.context.shielded`) this is :func:`time.sleep`.

    Args:
        seconds: Duration to sleep.
        event_wait_time: How often (in seconds) to poll a cancel event that is
            not koil's own and therefore cannot be blocked on. Koil's events
            wake the sleep directly, without polling.

    Raises:
        ThreadCancelledError: If the task is cancelled while sleeping.
    """
    cancel_event = current_cancel_event.get()
    if cancel_event is None:
        koil_loop = global_koil_loop.get()
        if koil_loop is not None:
            try:
                on_koil_loop = asyncio.get_running_loop() is koil_loop
            except RuntimeError:
                on_koil_loop = False
            if on_koil_loop:
                warnings.warn(
                    "koil.sleep() was called on the koil event loop itself and "
                    "blocks it for the whole duration. Use `await asyncio.sleep()` "
                    "in async code.",
                    RuntimeWarning,
                    stacklevel=2,
                )
        return time.sleep(seconds)

    wait_blocking = getattr(cancel_event, "wait_blocking", None)
    if wait_blocking is not None:
        if wait_blocking(seconds):
            raise ThreadCancelledError("Sleep was cancelled")
        return None

    deadline = time.monotonic() + seconds
    while True:
        if cancel_event.is_set():
            raise ThreadCancelledError("Sleep was cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(event_wait_time, remaining))


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
    _reject_awaitable_object(iterator, "unkoil_gen")
    koil_loop = get_koiled_loop_or_raise(fn=iterator)

    # One task on the loop owns the generator for its whole life; each step of
    # this sync generator is handed to it (see AsyncGenDriver).
    driver = AsyncGenDriver(
        iterator(*args, **kwargs).__aiter__(), koil_loop, None
    )
    try:
        send_val: Any = None
        while True:
            # result() is fetched outside the yield so that exceptions thrown
            # *into* the generator at the yield (GeneratorExit from close(), a
            # throw() from the consumer) never pass through the pruning handler.
            try:
                value = driver.step(send_val).result()
            except StopAsyncIteration:
                return
            except BaseException as e:
                if should_rewrite():
                    prune_traceback(e, keep_boundary=True)
                raise
            send_val = yield value
    finally:
        # Close the underlying async generator on the loop so its finally
        # blocks run now, not whenever it is garbage-collected or the loop is
        # torn down.
        driver.close()


def unkoil_gen_with_timeout(
    iterator: Callable[P, AsyncGenerator[R, SendType]],
    timeout: float,
    /,
    *args: P.args,
    **kwargs: P.kwargs,
) -> Generator[R, SendType, None]:
    """Like :func:`unkoil_gen`, but each iteration step is bounded by *timeout*.

    The timeout is a **per-step inactivity bound**, not a total bound: each
    ``__anext__`` submitted to the loop must produce its value (or finish)
    within *timeout* seconds, but a generator may run arbitrarily long overall
    as long as it keeps yielding. On expiry the in-flight step is cancelled on
    the loop and :class:`~koil.errors.KoilTimeoutError` is raised out of the
    consuming ``for`` loop; the underlying async generator's ``finally`` blocks
    still run via the normal close path.

    Args:
        iterator: An async generator function.
        timeout: Maximum seconds each iteration step may take.
        *args: Positional arguments forwarded to *iterator*.
        **kwargs: Keyword arguments forwarded to *iterator*.

    Yields:
        Values produced by the underlying async generator.

    Raises:
        KoilTimeoutError: If a step does not complete within *timeout* seconds.
        ValueError: If *timeout* is negative.
        KoilError: If no koil context is active.
    """
    _reject_awaitable_object(iterator, "unkoil_gen_with_timeout")
    koil_loop = get_koiled_loop_or_raise(fn=iterator)

    # One task on the loop owns the generator for its whole life; each step of
    # this sync generator is handed to it (see AsyncGenDriver).
    driver = AsyncGenDriver(
        iterator(*args, **kwargs).__aiter__(), koil_loop, timeout
    )
    try:
        send_val: Any = None
        while True:
            # result() is fetched outside the yield so that exceptions thrown
            # *into* the generator at the yield (GeneratorExit from close(), a
            # throw() from the consumer) never pass through the pruning handler.
            try:
                value = driver.step(send_val).result(
                    timeout=timeout_backstop(timeout)
                )
            except StopAsyncIteration:
                return
            except BaseException as e:
                if should_rewrite():
                    prune_traceback(e, keep_boundary=True)
                raise
            send_val = yield value
    finally:
        # Close the underlying async generator on the loop so its finally
        # blocks run now, not whenever it is garbage-collected or the loop is
        # torn down.
        driver.close()


def unkoil(
    coro: Callable[P, Coroutine[Any, Any, R]] | Callable[P, Awaitable[R]],
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
    _reject_awaitable_object(coro, "unkoil")
    koil_loop = get_koiled_loop_or_raise(fn=coro)

    context_aware_future = run_async_blocking(coro, koil_loop, None, args, kwargs)

    try:
        return context_aware_future.result()  # <- crossed the koil bridge
    except BaseException as e:
        if should_rewrite():
            prune_traceback(e, keep_boundary=True)
        raise


def unkoil_with_timeout(
    coro: Callable[P, Coroutine[Any, Any, R]] | Callable[P, Awaitable[R]],
    timeout: float,
    /,
    *args: P.args,
    **kwargs: P.kwargs,
) -> R:
    """Like :func:`unkoil`, but bounded by *timeout* seconds.

    The deadline is enforced on the koil loop: when it expires the coroutine is
    cancelled (cooperatively, with the ambient
    :attr:`~koil.loop.Koil.cancel_timeout` as acknowledgement grace) and
    :class:`~koil.errors.KoilTimeoutError` is raised to the caller.

    This is a separate function rather than a ``timeout`` keyword on
    :func:`unkoil` because ``unkoil`` forwards all keyword arguments to *coro*
    — a reserved keyword would collide with user functions that take a
    ``timeout`` of their own.

    Args:
        coro: An async function (coroutine function or awaitable-returning
            callable).
        timeout: Maximum seconds to wait for *coro* to complete.
        *args: Positional arguments forwarded to *coro*.
        **kwargs: Keyword arguments forwarded to *coro*.

    Returns:
        Whatever *coro* returns.

    Raises:
        KoilTimeoutError: If *coro* does not complete within *timeout* seconds.
        ValueError: If *timeout* is negative.
        KoilError: If no koil context is active.
        Any exception raised by *coro*.
    """
    _reject_awaitable_object(coro, "unkoil_with_timeout")
    koil_loop = get_koiled_loop_or_raise(fn=coro)

    context_aware_future = run_async_blocking(coro, koil_loop, timeout, args, kwargs)

    try:
        # The loop enforces the deadline; the wait here is bounded as well so
        # that a loop too wedged to run its own timers cannot hold us forever.
        return context_aware_future.result(  # <- crossed the koil bridge
            timeout=timeout_backstop(timeout)
        )
    except BaseException as e:
        if should_rewrite():
            prune_traceback(e, keep_boundary=True)
        raise


TaskReturn = TypeVar("TaskReturn")
TaskArgs = TypeVar("TaskArgs")
TaskNext = TypeVar("TaskNext")


def unkoil_task(
    coro: Callable[P, Coroutine[Any, Any, R]] | Callable[P, Awaitable[R]],
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
    _reject_awaitable_object(coro, "unkoil_task")
    koil_loop = get_koiled_loop_or_raise(fn=coro)

    return run_async_sharing_context(
        coro,
        koil_loop,
        None,
        None,
        *args,
        **kwargs,
    )


def unkoil_task_with_timeout(
    coro: Callable[P, Coroutine[Any, Any, R]] | Callable[P, Awaitable[R]],
    timeout: float,
    /,
    *args: P.args,
    **kwargs: P.kwargs,
) -> KoilFuture[R]:
    """Like :func:`unkoil_task`, but the submitted coroutine is bounded by
    *timeout* seconds.

    The deadline is enforced on the koil loop at submission time, so it applies
    even if :meth:`~koil.utils.KoilFuture.result` is never called: a
    fire-and-forget task is still cancelled when the deadline expires, and the
    future resolves with :class:`~koil.errors.KoilTimeoutError` (surfaced on
    ``result()`` or via Qt signals).

    For a wait bound applied only at the *call site* of ``result()``, use
    ``unkoil_task(...).result(timeout=...)`` instead.

    Args:
        coro: An async function (coroutine function or awaitable-returning
            callable).
        timeout: Maximum seconds the coroutine may run on the loop.
        *args: Positional arguments forwarded to *coro*.
        **kwargs: Keyword arguments forwarded to *coro*.

    Returns:
        A :class:`~koil.utils.KoilFuture` representing the pending coroutine.

    Raises:
        ValueError: If *timeout* is negative.
        KoilError: If no koil context is active.
    """
    _reject_awaitable_object(coro, "unkoil_task_with_timeout")
    koil_loop = get_koiled_loop_or_raise(fn=coro)

    return run_async_sharing_context(
        coro,
        koil_loop,
        None,
        timeout,
        *args,
        **kwargs,
    )


S = TypeVar("S")

# ContextVars that run_threaded sets on the worker thread for koil's own
# bookkeeping; never bridged back into the caller's context.
_BRIDGE_INTERNAL_VARS = INTERNAL_VARS


def _propagate_context_out(
    worker_context: contextvars.Context,
    baseline: contextvars.Context | None = None,
) -> None:
    """Re-apply ContextVars mutated inside *worker_context* into the caller's context.

    This is what makes the bridging variants bidirectional: any ContextVar the
    threaded function set (or changed) relative to *baseline* — the context it
    started from — is re-applied in the caller's context, so the async side
    observes it after the call returns. Koil-internal vars are skipped so
    per-call worker state never leaks outward.

    Must be called from the same context the caller awaited :func:`run_threaded` in
    (i.e. not from inside a freshly copied context), so the ``.set()`` calls land on
    the caller's context.
    """
    for var, value in _context_delta(worker_context, baseline):
        var.set(value)


async def _run_threaded(
    sync_func: Callable[P, R],
    sync_args: tuple[Any, ...],
    sync_kwargs: dict[str, Any],
    *,
    bridge: bool,
) -> R:
    """Shared implementation behind :func:`run_threaded` / :func:`run_threaded_bridged`.

    Submits *sync_func* to the koil worker pool with koil context, cancel-event
    setup, and the caller's :class:`~contextvars.Context` copied in. When *bridge* is
    true, ContextVars the worker mutated are copied back out into the caller's context
    on successful completion (skipping koil-internal vars).
    """
    __tracebackhide__ = True
    loop = asyncio.get_running_loop()

    koil = global_koil.get()

    def wrapper(
        cancel_event: KoilThreadSafeEvent,
        worker_context: contextvars.Context,
    ) -> R:
        __tracebackhide__ = True

        # Run the body *inside* the copied context via Context.run instead of
        # mutating the executor thread's own context with bare .set() calls.
        # Pooled threads are reused, so leftover contextvars would otherwise
        # leak into the next, unrelated task scheduled on that thread.
        def body() -> R:
            __tracebackhide__ = True
            global_koil.set(koil)
            global_koil_loop.set(loop)
            current_cancel_event.set(cancel_event)
            if drop_blocked_chain:
                blocked_calls.set(())

            # Cancelled while still queued behind other workers: don't start.
            if cancel_event.is_set():
                raise ThreadCancelledError("Cancelled before the function started")

            try:
                return sync_func(*sync_args, **sync_kwargs)  # type: ignore
            except StopIteration as e:
                # Transform so asyncio doesn't swallow it or crash.
                raise RuntimeError("Threaded function raised StopIteration") from e

        return worker_context.run(body)

    # The chain of threads blocked on this task only carries over to the worker
    # if this *is* the task they wait for; a task spawned from it inherited the
    # value, but nobody is blocked on that one.
    chain = blocked_calls.get()
    drop_blocked_chain = bool(chain) and chain[-1].task is not asyncio.current_task()

    # The worker mutates this copy (not the executor thread's own context); in
    # bridge mode we read its post-run values back out into the caller below.
    worker_context = contextvars.copy_context()
    baseline = contextvars.copy_context() if bridge else None
    # Set from the loop on cancellation; the worker reads it via is_set(),
    # blocks on it in sleep(), and a nested unkoil() in the worker reacts to it.
    cancel_event = KoilThreadSafeEvent(loop)

    # Koil's own pool when this loop is one koil started; the loop's default
    # executor on a foreign loop (plain asyncio code using run_threaded).
    state = loop_state(loop)
    executor = state.executor if state is not None else None
    pool_future = None
    if executor is not None:
        pool_future = executor.submit(wrapper, cancel_event, worker_context)
        future: "asyncio.Future[R]" = asyncio.wrap_future(pool_future, loop=loop)
    else:
        future = loop.run_in_executor(None, wrapper, cancel_event, worker_context)

    try:
        shielded_f = await asyncio.shield(future)
    except asyncio.CancelledError as e:
        cancel_event.set()

        if pool_future is not None and pool_future.cancel():
            # Never left the queue: nothing to wait for.
            raise

        try:
            await asyncio.wait_for(future, timeout=koil.cancel_timeout if koil else 10)
        except (ThreadCancelledError, CancelledError):
            logger.debug("Future in another thread was successfully cancelled")
        except TimeoutError as te:
            raise KoilError(
                f"We could not successfully cancel the future {future} in another thread. Make sure you are not blocking the thread with a long running task and check if you call check_cancelled periodically."
            ) from te

        raise e

    # Bidirectional bridging: re-apply any ContextVars the worker set back into
    # the caller's context. Done only on success, and only when bridging is on.
    if bridge:
        _propagate_context_out(worker_context, baseline)

    return shielded_f


async def run_threaded(
    sync_func: Callable[P, R],
    *sync_args: P.args,
    **sync_kwargs: P.kwargs,
) -> R:
    """Run a synchronous function on a worker thread with koil context.

    Submits *sync_func* to the worker pool of the :class:`~koil.loop.Koil`
    running the loop (or, on a loop koil did not start, to the loop's default
    executor) and awaits the result. Before calling the function, the worker
    thread is set up with:

    * The caller's :class:`contextvars.Context` (copied via
      :func:`contextvars.copy_context`).
    * :data:`~koil.context.global_koil` and
      :data:`~koil.context.global_koil_loop` so the worker can call
      :func:`unkoil` or :func:`check_cancelled`.
    * A per-call :class:`~koil.context.KoilThreadSafeEvent` as the cancel
      signal.

    Context propagation is **copy-in only**: the caller's
    :class:`~contextvars.ContextVar` values are copied *into* the worker thread, but
    any ContextVar the worker sets or changes stays isolated to that thread and is
    **not** propagated back into the caller's context. Use :func:`run_threaded_bridged`
    for bidirectional propagation, where worker mutations are also bridged back out.

    If the awaiting coroutine is cancelled, the cancel event is set and the
    function waits for the worker to finish (up to
    :attr:`~koil.loop.Koil.cancel_timeout` seconds) before re-raising
    :class:`asyncio.CancelledError`. From then on every
    :func:`~koil.context.check_cancelled`, :func:`sleep` and :func:`unkoil` in
    the worker raises :class:`~koil.errors.ThreadCancelledError`; cleanup that
    still has to call async code goes inside
    :func:`~koil.context.shielded`. A call cancelled while still queued for a
    free worker never starts.

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
    try:
        return await _run_threaded(sync_func, sync_args, sync_kwargs, bridge=False)
    except BaseException as e:
        if should_rewrite():
            prune_traceback(e, keep_boundary=True)
        raise


async def run_threaded_bridged(
    sync_func: Callable[P, R],
    *sync_args: P.args,
    **sync_kwargs: P.kwargs,
) -> R:
    """Like :func:`run_threaded`, but with **bidirectional** context propagation.

    The caller's :class:`~contextvars.ContextVar` values are copied *into* the worker
    thread, and any ContextVar the worker sets or changes is copied back *out* into
    the caller's context once the call returns. Koil-internal vars are never bridged
    out, and bridging happens only on successful completion — not on cancellation or
    error. Use this when the threaded function should publish contextvar changes back
    to the async caller. Everything else (koil setup, cancellation) matches
    :func:`run_threaded`.

    Args:
        sync_func: A synchronous callable to run on the thread pool.
        *sync_args: Positional arguments forwarded to *sync_func*.
        **sync_kwargs: Keyword arguments forwarded to *sync_func*.

    Returns:
        The return value of *sync_func*.
    """
    try:
        return await _run_threaded(sync_func, sync_args, sync_kwargs, bridge=True)
    except BaseException as e:
        if should_rewrite():
            prune_traceback(e, keep_boundary=True)
        raise


async def _iterate_threaded(
    sync_gen: Callable[P, Generator[R, S, None]],
    sync_args: tuple[Any, ...],
    sync_kwargs: dict[str, Any],
    *,
    bridge: bool,
) -> AsyncGenerator[R, S]:
    """Shared implementation behind :func:`iterate_threaded` / :func:`iterate_threaded_bridged`.

    Each step is driven via :func:`_run_threaded` with the given *bridge* mode, so the
    context-propagation behaviour matches the chosen public wrapper.

    This async-gen frame is also the traceback boundary marker for exceptions
    raised by the underlying generator: the public wrappers return the asyncgen
    immediately, so their frames never appear in a traceback.
    """
    generator: Generator[R, S, None] | None = None

    def step(send_value: Any) -> R:
        __tracebackhide__ = True
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

    async def close(report: bool) -> None:
        # The generator is suspended at a yield (or was never resumed after a
        # cancelled step): close it in a worker thread so its finally blocks
        # run there, now — not on whatever thread garbage-collects it later.
        if generator is None or getattr(generator, "gi_frame", None) is None:
            return  # never started, or already finished
        if getattr(generator, "gi_running", False):
            return  # a worker that ignored its cancellation is still inside it
        try:
            await _run_threaded(generator.close, (), {}, bridge=bridge)
        except BaseException as e:
            if not report:
                logger.warning(
                    "Closing the generator of %r failed", sync_gen, exc_info=True
                )
                return
            # Errors raised by the generator's finally blocks during an early
            # close get the same pruning treatment.
            if should_rewrite():
                prune_traceback(e, keep_boundary=True)
            raise

    send_value: Any = None
    try:
        while True:
            try:
                value = await _run_threaded(step, (send_value,), {}, bridge=bridge)
            except KoilStopIteration:
                return
            except BaseException as e:
                if should_rewrite():
                    prune_traceback(e, keep_boundary=True)
                raise

            send_value = yield value
    except GeneratorExit:
        # Consumer stopped early; an error in the generator's cleanup is the
        # consumer's to see.
        await close(report=True)
        raise
    except BaseException:
        # Cancelled or failed: the original exception is the one to surface.
        await close(report=False)
        raise


def iterate_threaded(
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
       thread-affine resource (e.g. an ``RLock``) across a ``yield`` will not see
       single-thread behaviour.

       Context propagation is **copy-in only**: each step copies the caller's context
       *into* its worker thread but does not bridge the generator's contextvar
       mutations back out. A ContextVar set in one step is therefore **not** visible
       to the next step or to the caller — every step starts from the caller's value.
       Use :func:`iterate_threaded_bridged` for bidirectional propagation, where
       mutations persist across yields.

    Args:
        sync_gen: A synchronous generator function.
        *sync_args: Positional arguments forwarded to *sync_gen*.
        **sync_kwargs: Keyword arguments forwarded to *sync_gen*.

    Yields:
        Values produced by the underlying synchronous generator.
    """
    return _iterate_threaded(sync_gen, sync_args, sync_kwargs, bridge=False)


def iterate_threaded_bridged(
    sync_gen: Callable[P, Generator[R, S, None]],
    *sync_args: P.args,
    **sync_kwargs: P.kwargs,
) -> AsyncGenerator[R, S]:
    """Like :func:`iterate_threaded`, but with **bidirectional** context propagation.

    Each step bridges the generator's contextvar mutations back into the caller,
    which re-seeds them into the next step. As a result, :class:`~contextvars.ContextVar`
    mutations **do** persist across yields: a value set in one step is visible to the
    next step and to the caller. Everything else matches :func:`iterate_threaded`.

    Args:
        sync_gen: A synchronous generator function.
        *sync_args: Positional arguments forwarded to *sync_gen*.
        **sync_kwargs: Keyword arguments forwarded to *sync_gen*.

    Yields:
        Values produced by the underlying synchronous generator.
    """
    return _iterate_threaded(sync_gen, sync_args, sync_kwargs, bridge=True)
