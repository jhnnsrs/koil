"""Internal utilities: introspection helpers, future wrappers, and context runners.

The two central types here are :class:`KoilFuture` and :class:`KoilIterator`,
which wrap a :class:`concurrent.futures.Future` with a koil-aware cancel
mechanism.  :func:`run_async_sharing_context` and
:func:`iterate_async_sharing_context` are the low-level primitives that submit
coroutines/generators to the koil loop while propagating the caller's
:class:`~contextvars.Context` and cancellation event.
"""
import asyncio
import contextvars
import concurrent.futures
from typing import (
    AsyncIterator,
    Awaitable,
    Callable,
    Generic,
    ParamSpec,
    Tuple,
    TypeVar,
    cast,
)
from koil.errors import CancelledError, ThreadCancelledError
from koil.types import AnyCallable
from koil.context import current_cancel_event, KoilThreadSafeEvent
import inspect
from koil.protocols import TaskSignalProtocol, IteratorSignalProtocol


def check_is_asyncgen(func: AnyCallable) -> bool:
    """Return ``True`` if *func* is an async generator function."""
    if inspect.isasyncgenfunction(func):
        return True
    return False


def check_is_asyncfunc(func: AnyCallable) -> bool:
    """Return ``True`` if *func* is a coroutine function."""
    if inspect.iscoroutinefunction(func):
        return True
    return False


def check_is_syncgen(func: AnyCallable) -> bool:
    """Return ``True`` if *func* is a synchronous generator function."""
    if inspect.isgeneratorfunction(func):
        return True
    return False


def check_is_syncfunc(func: AnyCallable) -> bool:
    """Return ``True`` if *func* is a plain synchronous function."""
    if inspect.isfunction(func):
        return True
    return False


P = ParamSpec("P")
T = TypeVar("T")


def _safe_set_event(event: asyncio.Event) -> None:
    """Set *event*, tolerating a closed event loop.

    :class:`~koil.context.KoilThreadSafeEvent.set` routes through
    ``call_soon_threadsafe``, which raises :class:`RuntimeError` when the koil
    loop is already closed. If the loop is gone the work is already
    finished/cancelled, so swallowing that error is correct.
    """
    try:
        event.set()
    except RuntimeError:
        pass


class _KoilFutureBase(Generic[T]):
    """Shared implementation for :class:`KoilFuture` and :class:`KoilIterator`.

    Wraps a :class:`concurrent.futures.Future` with a koil-aware cancel
    mechanism: cancellation is signalled via a
    :class:`~koil.context.KoilThreadSafeEvent` rather than by attempting to
    interrupt the underlying thread directly.
    """

    def __init__(
        self,
        future: concurrent.futures.Future[Tuple[T, contextvars.Context]],
        cancel_event: asyncio.Event,
    ) -> None:
        super().__init__()
        self.future = future
        self.cancel_event = cancel_event
        self._cancel_requested = False

    def cancel(self) -> bool:
        """Request cancellation of the running task.

        Sets the associated cancel event so that koil worker code that checks
        :func:`~koil.context.check_cancelled` will raise
        :class:`~koil.errors.ThreadCancelledError` on its next poll.

        Returns:
            ``True`` if the task was still running at the time of the request;
            ``False`` if it had already completed.
        """
        if not self.future.done():
            self._cancel_requested = True
            _safe_set_event(self.cancel_event)
            return True
        return False

    def done(self) -> bool:
        """Return ``True`` if the underlying future has completed."""
        assert self.future, "Task was never run! Please run task before"
        return self.future.done()

    def cancelled(self) -> bool:
        """Return ``True`` if cancellation has been requested or confirmed."""
        assert self.future, "Task was never run! Please run task before"
        return self._cancel_requested or self.cancel_event.is_set()

    def result(self) -> T:
        """Block until the result is available and return it.

        Also propagates any updated :class:`~contextvars.ContextVar` values
        from the worker back into the calling context.

        Raises:
            CancelledError: If the task was cancelled.
            Any exception raised by the coroutine/function.
        """
        assert self.future, "Task was never run! Please run task before"
        try:
            res, context = self.future.result()
        except CancelledError as e:
            raise e

        for ctx, value in context.items():
            ctx.set(value)

        return res


class KoilFuture(_KoilFutureBase[T]):
    """A cancellable future representing a single coroutine submitted to the koil loop.

    Returned by :func:`~koil.bridge.unkoil_task` and used internally by
    :func:`~koil.utils.run_async_sharing_context`. Cancellation is cooperative:
    calling :meth:`cancel` sets a :class:`~koil.context.KoilThreadSafeEvent`
    that the running coroutine (or its worker wrapper) checks periodically.
    """


class KoilIterator(_KoilFutureBase[T]):
    """A cancellable future representing an async iterator submitted to the koil loop.

    Returned by :func:`~koil.utils.iterate_async_sharing_context`. Shares the
    same cancellation semantics as :class:`KoilFuture`.
    """


def run_async_sharing_context(
    coro: Callable[P, Awaitable[T]],
    loop: asyncio.AbstractEventLoop,
    signals: TaskSignalProtocol[T] | None,
    *args: P.args,
    **kwargs: P.kwargs,
) -> KoilFuture[T]:
    """Submit *coro* to *loop* while propagating context and the cancel event.

    Unlike :func:`asyncio.run_coroutine_threadsafe`, this function:

    * Copies the caller's :class:`~contextvars.Context` into the coroutine so
      that :class:`~contextvars.ContextVar` reads inside the coroutine see the
      values set by the calling thread.
    * Races the coroutine against the active cancel event; if the event fires
      first, the coroutine is cancelled and
      :class:`~asyncio.CancelledError` is raised.
    * Optionally emits lifecycle signals (*returned*, *errored*, *cancelled*)
      if a *signals* object is provided (used by the Qt integration).

    Args:
        coro: An async callable to invoke.
        loop: The koil event loop to run the coroutine on.
        signals: Optional signal holder for Qt integration callbacks.
        *args: Positional arguments forwarded to *coro*.
        **kwargs: Keyword arguments forwarded to *coro*.

    Returns:
        A :class:`KoilFuture` that resolves to the return value of *coro*.

    Raises:
        ThreadCancelledError: If the current cancel event is already set when
            this function is called.
    """

    ctxs = contextvars.copy_context()

    cancel_event = current_cancel_event.get() or KoilThreadSafeEvent(loop)

    if cancel_event.is_set():
        raise ThreadCancelledError("Thread was cancelled")

    async def passed_with_context():
        async def context_future():
            for ctx, value in ctxs.items():
                ctx.set(value)

            result = await coro(*args, **kwargs)

            newcontext = contextvars.copy_context()
            return result, newcontext

        cancel_f = asyncio.create_task(cancel_event.wait())
        future_t = asyncio.create_task(context_future())

        finished, unfinished = await asyncio.wait(
            [future_t, cancel_f], return_when=asyncio.FIRST_COMPLETED
        )

        for task in finished:
            if task == cancel_f:
                typed_task = cast(asyncio.Task[None], task)
                for untask in unfinished:
                    untask.cancel()
                    try:
                        await untask
                    except asyncio.CancelledError:
                        pass

                error = asyncio.CancelledError(f"Future {task} was cancelled")

                if signals:
                    signals.cancelled.emit(error)

                raise error

            elif task == future_t:
                for untask in unfinished:
                    untask.cancel()
                    try:
                        await untask
                    except asyncio.CancelledError:
                        pass

                exception = task.exception()
                if exception:
                    if signals:
                        signals.errored.emit(exception)

                    raise exception

                typed_task = cast(
                    asyncio.Task[Tuple[T, contextvars.Context]], task
                )

                result = typed_task.result()

                if signals:
                    try:
                        signals.returned.emit(result)
                    except Exception as e:
                        raise e

                return result

            else:
                raise Exception(
                    f"Task {task} was not cancelled and not the future task. This should never happen"
                )

        raise Exception("Should never happen")

    return KoilFuture(
        asyncio.run_coroutine_threadsafe(passed_with_context(), loop), cancel_event
    )


def iterate_async_sharing_context(
    coro: Callable[P, AsyncIterator[T]],
    loop: asyncio.AbstractEventLoop,
    signals: IteratorSignalProtocol[T] | None,
    *args: P.args,
    **kwargs: P.kwargs,
) -> KoilFuture[None]:
    """Submit an async generator to *loop* and drain it, propagating context.

    Like :func:`run_async_sharing_context` but for async generators: runs the
    generator to exhaustion on the loop, emitting each yielded value via
    *signals.next* before returning ``None``.  Cancellation and context
    propagation follow the same rules as :func:`run_async_sharing_context`.

    Args:
        coro: An async generator callable.
        loop: The koil event loop to run the generator on.
        signals: Optional signal holder for Qt integration callbacks.
        *args: Positional arguments forwarded to *coro*.
        **kwargs: Keyword arguments forwarded to *coro*.

    Returns:
        A :class:`KoilFuture` that resolves to ``None`` when the generator is
        exhausted.

    Raises:
        ThreadCancelledError: If the current cancel event is already set when
            this function is called.
    """

    ctxs = contextvars.copy_context()

    cancel_event = current_cancel_event.get() or KoilThreadSafeEvent(loop)

    if cancel_event.is_set():
        raise ThreadCancelledError("Thread was cancelled")

    async def passed_with_context():
        async def context_future():
            for ctx, value in ctxs.items():
                ctx.set(value)

            async for x in coro(*args, **kwargs):
                newcontext = contextvars.copy_context()
                if signals:
                    signals.next.emit((x, newcontext))

            newcontext = contextvars.copy_context()
            return None, newcontext

        cancel_f = asyncio.create_task(cancel_event.wait())
        future_t = asyncio.create_task(context_future())

        finished, unfinished = await asyncio.wait(
            [future_t, cancel_f], return_when=asyncio.FIRST_COMPLETED
        )

        for task in finished:
            if task == cancel_f:
                for untask in unfinished:
                    untask.cancel()
                    try:
                        await untask
                    except asyncio.CancelledError:
                        pass

                error = CancelledError(f"Future {task} was cancelled")

                if signals:
                    signals.cancelled.emit(error)

                raise error

            elif task == future_t:
                for untask in unfinished:
                    untask.cancel()
                    try:
                        await untask
                    except asyncio.CancelledError:
                        pass

                exception = task.exception()
                if exception:
                    if signals:
                        signals.errored.emit(exception)

                    raise exception

                typed_task = cast(
                    asyncio.Task[Tuple[None, contextvars.Context]], task
                )
                result = typed_task.result()

                if signals:
                    try:
                        signals.done.emit(None)
                    except Exception as e:
                        raise e

                return result

            else:
                raise Exception(
                    f"Task {task} was not cancelled and not the future task. This should never happen"
                )

        raise Exception("Should never happen")

    return KoilFuture(
        asyncio.run_coroutine_threadsafe(passed_with_context(), loop), cancel_event
    )
