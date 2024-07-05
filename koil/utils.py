import asyncio
import contextvars
import concurrent.futures
import threading
from typing import Any, Callable
from koil.errors import CancelledError

import inspect


def check_is_asyncgen(func) -> bool:
    """Checks if a function is an async generator"""
    if inspect.isasyncgenfunction(func):
        return True

    return False


def check_is_asyncfunc(func) -> bool:
    """Checks if a function is an async function"""
    if inspect.iscoroutinefunction(func):
        return True

    return False


def check_is_syncgen(func) -> bool:
    """Checks if a function is an async generator"""
    if inspect.isgeneratorfunction(func):
        return True

    return False


def check_is_syncfunc(func) -> bool:
    """Checks if a function is an async function"""
    if inspect.isfunction(func):
        return True

    return False


async def check_event(event: threading.Event):
    """
    Waits for an event to be set, or for a timeout to occur.
    """
    try:
        while not event.is_set():
            await asyncio.sleep(0.01)
    except asyncio.CancelledError:
        pass


def run_threaded_with_context(
    coro: Callable,
    loop: asyncio.AbstractEventLoop,
    cancel_event: threading.Event,
    *args: Any,
    **kwargs: Any,
) -> concurrent.futures.Future:
    """Runs a future in the supplied loop but copiesthe context
    of the current loop,

    Args:
        future (asyncio.Future): The asyncio FUture
        loop (asyncio.AbstractEventLoop): The loop in which we run?

    Returns:
        concurrent.futures.Future: The future + newcontext
    """

    ctxs = contextvars.copy_context()

    async def passed_with_context(coro):
        async def context_future():
            for ctx, value in ctxs.items():
                ctx.set(value)

            result = await coro(*args, **kwargs)

            newcontext = contextvars.copy_context()
            return result, newcontext

        cancel_f = asyncio.create_task(check_event(cancel_event))
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

                raise CancelledError(f"Future {task} was cancelled")

            for untask in unfinished:
                untask.cancel()
                try:
                    await untask
                except asyncio.CancelledError:
                    pass  # we are not interested in this and it should always be fine

            if task.exception():
                raise task.exception()

            return task.result()

    return asyncio.run_coroutine_threadsafe(passed_with_context(coro), loop)


def run_threaded_with_context_and_signals(
    coro: Callable,
    loop: asyncio.AbstractEventLoop,
    cancel_event: threading.Event,
    started_signal: Any,
    returned_signal: Any,
    errored_signal: Any,
    cancelled_signal: Any,
    *args,
    **kwargs,
) -> concurrent.futures.Future:
    """Runs a future in the supplied loop but copiesthe context
    of the current loop,

    Args:
        future (asyncio.Future): The asyncio FUture
        loop (asyncio.AbstractEventLoop): The loop in which we run?

    Returns:
        concurrent.futures.Future: The future + newcontext
    """

    ctxs = contextvars.copy_context()

    async def passed_with_context(coro):
        async def context_future():
            for ctx, value in ctxs.items():
                ctx.set(value)

            started_signal.emit()
            result = await coro(*args, **kwargs)
            newcontext = contextvars.copy_context()
            return result, newcontext

        cancel_f = asyncio.create_task(check_event(cancel_event))
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

                cancelled_signal.emit()
                raise CancelledError(f"Future {task} was cancelled")

            for untask in unfinished:
                untask.cancel()
                try:
                    await untask
                except asyncio.CancelledError:
                    pass  # we are not interested in this and it should always be fine

            if task.exception():
                errored_signal.emit(task.exception())
                raise task.exception()

            returned_signal.emit(*task.result())
            return task.result()

    return asyncio.run_coroutine_threadsafe(passed_with_context(coro), loop)


def iterate_threaded_with_context_and_signals(
    iterator: Callable,
    loop: asyncio.AbstractEventLoop,
    cancel_event: threading.Event,
    yielded_signal: Any,
    errored_signal: Any,
    cancelled_signal: Any,
    done_signal: Any,
    *args,
    **kwargs,
) -> concurrent.futures.Future:
    """Runs a future in the supplied loop but copiesthe context
    of the current loop,

    Args:
        future (asyncio.Future): The asyncio FUture
        loop (asyncio.AbstractEventLoop): The loop in which we run?

    Returns:
        concurrent.futures.Future: The future + newcontext
    """

    ctxs = contextvars.copy_context()

    async def passed_with_context(coro):
        async def context_future():

            for ctx, value in ctxs.items():
                ctx.set(value)

            async for x in coro(*args, **kwargs):
                newcontext = contextvars.copy_context()
                yielded_signal.emit(x, newcontext)

            return newcontext

        cancel_f = asyncio.create_task(check_event(cancel_event))
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

                cancelled_signal.emit()
                raise CancelledError(f"Future {task} was cancelled")

            for untask in unfinished:
                untask.cancel()
                try:
                    await untask
                except asyncio.CancelledError:
                    pass  # we are not interested in this and it should always be fine

            if task.exception():
                errored_signal.emit(task.exception())
                raise task.exception()

            done_signal.emit(task.result())
            return task.result()

    return asyncio.run_coroutine_threadsafe(passed_with_context(iterator), loop)
