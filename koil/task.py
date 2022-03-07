import contextvars
import logging
import asyncio
import time
from typing import AsyncIterator, Awaitable, Callable, Coroutine, Generic, TypeVar
from koil.vars import current_cancel_event, current_loop
import inspect
from .utils import run_threaded_with_context
from typing_extensions import ParamSpec, final

logger = logging.getLogger(__name__)

T = TypeVar("T")
P = ParamSpec("P")


class KoilTask(Generic[T, P]):
    """Will run the giving coroutine in the thread of the loop
    and return the result

    This is a thin wrapper around concurrent.futures api, but with
    additional context support.

    Args:
        Generic (_type_): _description_
    """

    def __init__(
        self,
        coro: Callable[P, Awaitable[T]],
        args=(),
        kwargs={},
        log_errors=True,
        loop=None,
        bypass_test=False,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.coro = coro
        self.args = args
        self.kwargs = kwargs
        self.loop = loop or current_loop.get()
        assert self.loop, "No koiled Loop found"
        self.task = None
        self.future = None
        if not bypass_test:
            assert self.loop.is_running(), "Loop is not running"
            assert not self.loop.is_closed(), "Loop is closed"
            assert inspect.iscoroutinefunction(coro), "Task is not a coroutine"

    def run(self, *args: P.args, **kwargs: P.kwargs):
        args = self.args + args
        kwargs = {**self.kwargs, **kwargs}
        self.future = run_threaded_with_context(self.coro(*args, **kwargs), self.loop)

        return self

    def done(self):
        return self.future.done()

    def cancel(self):
        self.future.cancel()

    def result(self) -> T:
        assert self.future, "Task was never run! Please run task before"

        res, context = self.future.result()

        for ctx, value in context.items():
            ctx.set(value)

        return res


class KoilGeneratorTask(Generic[P, T]):
    def __init__(
        self,
        iterator: Callable[P, AsyncIterator[T]],
        args=(),
        kwargs={},
        loop=None,
        bypass_test=False,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.iterator = iterator
        self.args = args
        self.kwargs = kwargs
        self.loop = loop or current_loop.get()
        self.task = None
        if not bypass_test:
            assert self.loop.is_running(), "Loop is not running"
            assert not self.loop.is_closed(), "Loop is closed"
            assert inspect.isasyncgenfunction(iterator), "Task is not a async iterator"

    def run(self, *args: P.args, **kwargs: P.kwargs):
        ait = self.iterator(*self.args, **self.kwargs).__aiter__()
        res = [False, False]
        cancel_event = current_cancel_event.get()

        async def next_on_ait():
            try:
                try:
                    obj = await ait.__anext__()
                    return [False, obj]
                except StopAsyncIteration:
                    return [True, None]
            except asyncio.CancelledError as e:
                return [False, e]

        while True:
            res, context = run_threaded_with_context(next_on_ait(), loop=self.loop)
            while not res.done():
                if cancel_event and cancel_event.is_set():
                    raise Exception("Task was cancelled")

                time.sleep(0.01)
            done, obj = res.result()
            if done:
                if obj:
                    raise obj
                break

            for ctx, value in context.items():
                ctx.set(value)

            yield obj

    def result(self):
        raise NotImplementedError("This is a generator that yields results")
