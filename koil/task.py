import logging
import threading
from typing import AsyncIterator, Awaitable, Callable, Generic, TypeVar
from koil.errors import CancelledError
from koil.vars import current_cancel_event, current_loop
import inspect
from .utils import run_threaded_with_context
from typing_extensions import ParamSpec
import concurrent.futures

logger = logging.getLogger(__name__)

T = TypeVar("T")
P = ParamSpec("P")


class KoilFuture:
    def __init__(
        self, future: concurrent.futures.Future, cancel_event: threading.Event
    ):
        """Private Initializer

        You should really never call this function yourself. Instead, use either a runner
        to create a task or create_task to create a future.

        Args:
            future (concurrent.futures.Future): The concurrent future
            cancel_event (threading.Event): The cancel event for this future
        """
        self.future = future
        self.iscancelled = False
        self.cancel_event = cancel_event

    def done(self):
        return self.future.done()

    def cancel(self, wait=False):
        assert (
            not self.future.done()
        ), "Task finished already! You cannot cancel anymore"
        self.cancel_event.set()

        if wait:
            try:
                self.future.result()
                raise RuntimeError("Task was cancelled but returned a result")
            except CancelledError:
                return True

        return True

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

    def result(self, swallow_cancel=False) -> T:
        assert self.future, "Task was never run! Please run task before"

        try:
            res, context = self.future.result()
        except CancelledError as e:
            if not swallow_cancel:
                raise e
            return True

        for ctx, value in context.items():
            ctx.set(value)

        return res


class KoilYieldFuture(KoilFuture):
    def __init__(
        self, future: concurrent.futures.Future, cancel_event: threading.Event
    ):
        super().__init__(future, cancel_event)

    def buffered(self) -> T:
        assert self.future, "Task was never run! Please run task before"

        res, context = self.future.result()

        for ctx, value in context.items():
            ctx.set(value)

        return res


class KoilRunner(Generic[T, P]):
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

        loop = current_loop.get()
        assert loop is not None, "No loop found"
        assert loop.is_running(), "Loop is not running"
        assert not loop.is_closed(), "Loop is closed"
        cancel_event = threading.Event()
        future = run_threaded_with_context(
            self.coro, loop, cancel_event, *args, **kwargs
        )
        return KoilFuture(future, cancel_event, self)


class KoilGeneratorRunner(Generic[P, T]):
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
        assert loop is not None, "No koiled loop found"
        assert loop.is_running(), "Loop is not running"
        assert not loop.is_closed(), "Loop is closed"
        self.iterator(*self.args, **self.kwargs).__aiter__()
        current_cancel_event.get()
        raise NotImplementedError("No design decision was taken")
