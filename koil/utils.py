import asyncio
import contextvars
import concurrent.futures
import threading
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
from koil.vars import current_cancel_event
import inspect
from koil.protocols import TaskSignalProtocol, IteratorSignalProtocol


def check_is_asyncgen(func: AnyCallable) -> bool:
    """Checks if a function is an async generator"""
    if inspect.isasyncgenfunction(func):
        return True

    return False


def check_is_asyncfunc(func: AnyCallable) -> bool:
    """Checks if a function is an async function"""
    if inspect.iscoroutinefunction(func):
        return True

    return False


def check_is_syncgen(func: AnyCallable) -> bool:
    """Checks if a function is an async generator"""
    if inspect.isgeneratorfunction(func):
        return True

    return False


def check_is_syncfunc(func: AnyCallable) -> bool:
    """Checks if a function is an async function"""
    if inspect.isfunction(func):
        return True

    return False


def wait_in_thread(event: threading.Event, fut: asyncio.Future[None]):
    event.wait()
    if not fut.cancelled():
        fut.get_loop().call_soon_threadsafe(fut.set_result, None)


async def await_thread_event(event: threading.Event):
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    threading.Thread(target=wait_in_thread, args=(event, fut), daemon=True).start()
    await fut


P = ParamSpec("P")
T = TypeVar("T")


class KoilFuture(Generic[T]):
    """ " A wrapper around a concurrent.futures.Future that allows
    cancelling the future and checking if it was cancelled.

    This is used to propagate the cancel event to the future
    and to check if the future was cancelled.
    """

    def __init__(
        self,
        future: concurrent.futures.Future[Tuple[T, contextvars.Context]],
        cancel_event: threading.Event,
    ):
        super().__init__()
        self.future = future
        self.cancel_event = cancel_event

    def cancel(self) -> bool:
        if not self.future.done():
            self.cancel_event.set()
            return True
        return False

    def done(self) -> bool:
        assert self.future, "Task was never run! Please run task before"
        return self.future.done()

    def cancelled(self) -> bool:
        assert self.future, "Task was never run! Please run task before"
        if self.cancel_event.is_set():
            return True
        else:
            return False

    def result(self) -> T:
        assert self.future, "Task was never run! Please run task before"
        try:
            res, context = self.future.result()
        except CancelledError as e:
            raise e

        for ctx, value in context.items():
            ctx.set(value)

        return res


class KoilIterator(Generic[T]):
    """ " A wrapper around a concurrent.futures.Future that allows
    cancelling the future and checking if it was cancelled.

    This is used to propagate the cancel event to the future
    and to check if the future was cancelled.
    """

    def __init__(
        self,
        future: concurrent.futures.Future[Tuple[T, contextvars.Context]],
        cancel_event: threading.Event,
    ):
        super().__init__()
        self.future = future
        self.cancel_event = cancel_event

    def cancel(self) -> bool:
        if not self.future.done():
            self.cancel_event.set()
            return True
        return False

    def done(self) -> bool:
        assert self.future, "Task was never run! Please run task before"
        return self.future.done()

    def cancelled(self) -> bool:
        assert self.future, "Task was never run! Please run task before"
        if self.cancel_event.is_set():
            return True
        else:
            return False

    def result(self) -> T:
        assert self.future, "Task was never run! Please run task before"
        try:
            res, context = self.future.result()
        except CancelledError as e:
            raise e

        for ctx, value in context.items():
            ctx.set(value)

        return res


def run_async_sharing_context(
    coro: Callable[P, Awaitable[T]],
    loop: asyncio.AbstractEventLoop,
    signals: TaskSignalProtocol[T] | None,
    *args: P.args,
    **kwargs: P.kwargs,
) -> KoilFuture[T]:
    """Runs a future in the supplied loop but copies the context
    of the current loop, also propagating the cancel event.


    Attention: This function will also share the current cancel event
    and will cancel the task if the event is set. This is not
    the default behaviour of asyncio.run_coroutine_threadsafe.

    Args:
        future (asyncio.Future): The asyncio FUture
        loop (asyncio.AbstractEventLoop): The loop in which we run?

    Returns:
        concurrent.futures.Future: The future
    """

    ctxs = contextvars.copy_context()

    cancel_event = current_cancel_event.get() or threading.Event()

    if cancel_event.is_set():
        raise ThreadCancelledError("Thread was cancelled")

    async def passed_with_context():
        async def context_future():
            for ctx, value in ctxs.items():
                ctx.set(value)

            result = await coro(*args, **kwargs)

            newcontext = contextvars.copy_context()
            return result, newcontext

        cancel_f = asyncio.create_task(await_thread_event(cancel_event))
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
                        pass  # we are not interested in this and it should always be fine

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
                        pass  # we are not interested in this and it should always be fine

                exception = task.exception()
                if exception:
                    if signals:
                        signals.errored.emit(exception)

                    raise exception

                typed_task = cast(
                    asyncio.Task[Tuple[T, contextvars.Context]], task
                )  # we cast here because we asserted that its the future task

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
    """Runs a async generator in the supplied loop until exhausted but copies the context
    of the current loop, also propagating the cancel event.


    Attention: This function will also share the current cancel event
    and will cancel the task if the event is set. This is not
    the default behaviour of asyncio.run_coroutine_threadsafe.

    Args:
        future (asyncio.Future): The asyncio FUture
        loop (asyncio.AbstractEventLoop): The loop in which we run?

    Returns:
        concurrent.futures.Future: The future
    """

    ctxs = contextvars.copy_context()

    cancel_event = current_cancel_event.get() or threading.Event()

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

        cancel_f = asyncio.create_task(await_thread_event(cancel_event))
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
                        pass  # we are not interested in this and it should always be fine

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
                        pass  # we are not interested in this and it should always be fine

                exception = task.exception()
                if exception:
                    if signals:
                        signals.errored.emit(exception)

                    raise exception

                typed_task = cast(
                    asyncio.Task[Tuple[None, contextvars.Context]], task
                )  # we cast here because we asserted that its the future task
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
