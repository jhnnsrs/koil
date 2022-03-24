import contextvars
import logging
import asyncio
import threading
import time
from typing import AsyncIterator, Awaitable, Callable, Coroutine, Generic, TypeVar
from koil.errors import CancelledError
from koil.vars import current_cancel_event, current_loop
import inspect
from .utils import run_threaded_with_context
from typing_extensions import ParamSpec, final
import concurrent.futures

logger = logging.getLogger(__name__)

T = TypeVar("T")
P = ParamSpec("P")


class KoilFuture:
    def __init__(
        self, future: concurrent.futures.Future, cancel_event: threading.Event, task
    ):
        self.future = future
        self.iscancelled = False
        self.cancel_event = cancel_event
        self.task = task

    def done(self):
        return self.future.done()

    def cancel(self):
        assert (
            not self.future.done()
        ), "Task finished already! You cannot cancel anymore"
        self.cancel_event.set()

    def cancelled(self):
        assert self.future, "Task was never run! Please run task before"
        if not self.cancel_event.is_set():
            return False
        if not self.future.done():
            return False
        try:
            return self.future.result()
        except CancelledError:
            return True

    def result(self) -> T:
        assert self.future, "Task was never run! Please run task before"

        res, context = self.future.result()

        for ctx, value in context.items():
            ctx.set(value)
            print(ctx, value.__class__)

        return res

    def __getattr__(self, __name: str):
        if __name == "errored":
            raise NotImplementedError(
                "errored is not a signal of a task but of a runner"
            )
        super().__getattr__(__name)


class KoilYieldFuture(KoilFuture):
    def __init__(
        self, future: concurrent.futures.Future, cancel_event: threading.Event
    ):
        super().__init__(future, cancel_event, None)

    def buffered(self) -> T:
        assert self.future, "Task was never run! Please run task before"

        res, context = self.future.result()

        for ctx, value in context.items():
            ctx.set(value)
            print(ctx, value.__class__)

        return res


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
        *args,
        preset_args=(),
        preset_kwargs={},
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.coro = coro
        self.args = preset_args
        self.kwargs = preset_kwargs
        self.task = None
        self.future = None
        assert inspect.iscoroutinefunction(coro), "Task is not a coroutine"

    def run(self, *args: P.args, **kwargs: P.kwargs):
        args = self.args + args
        kwargs = {**self.kwargs, **kwargs}
        cancel_event = threading.Event()
        loop = current_loop.get()
        assert loop.is_running(), "Loop is not running"
        assert not loop.is_closed(), "Loop is closed"
        future = run_threaded_with_context(
            self.coro, loop, cancel_event, *args, **kwargs
        )
        return KoilFuture(future, cancel_event, self)


class KoilGeneratorTask(Generic[P, T]):
    def __init__(
        self,
        iterator: Callable[P, AsyncIterator[T]],
        args=(),
        kwargs={},
    ) -> None:
        super().__init__(*args, **kwargs)
        self.iterator = iterator
        self.args = args
        self.kwargs = kwargs
        self.task = None
        assert inspect.isasyncgenfunction(iterator), "Task is not a async iterator"

    def run(self, *args: P.args, **kwargs: P.kwargs):
        loop = current_loop.get()
        assert loop.is_running(), "Loop is not running"
        assert not loop.is_closed(), "Loop is closed"
        ait = self.iterator(*self.args, **self.kwargs).__aiter__()
        res = [False, False]
        cancel_event = current_cancel_event.get()
        raise NotImplementedError("No design decision was taken")

    def done(self):
        return self._task_done

    def result(self):
        if not self._task_done:
            raise Exception("Task is not done yet")

        self._buffer

        raise NotImplementedError("This is a generator that yields results")
