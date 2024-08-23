import asyncio
import contextvars
import inspect
import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Generic, TypeVar, Awaitable

from qtpy import QtCore, QtWidgets
from typing_extensions import ParamSpec
from koil.koil import Koil, KoilMixin
from koil.task import KoilFuture, KoilGeneratorRunner, KoilRunner, KoilYieldFuture
from koil.utils import (
    iterate_threaded_with_context_and_signals,
    run_threaded_with_context_and_signals,
)
from koil.vars import current_loop
import uuid
from typing import Protocol
from .utils import check_is_asyncfunc, check_is_asyncgen, check_is_syncgen


logger = logging.getLogger(__name__)


Reference = str


class UnconnectedSignalError(Exception):
    pass


class QtFuture:
    def __init__(self):
        self.id = uuid.uuid4().hex
        self.loop = asyncio.get_event_loop()
        self.aiofuture = asyncio.Future()
        self.iscancelled = False

    def _set_cancelled(self):
        """WIll be called by the asyncio loop"""
        self.iscancelled = True

    def resolve(self, *args):
        if not args:
            args = (None,)
        ctx = contextvars.copy_context()

        if self.aiofuture.done():
            logger.warning(f"QtFuture {self} already done. Cannot resolve")
            return

        self.loop.call_soon_threadsafe(self.aiofuture.set_result, (ctx,) + args)

    def reject(self, exp: Exception):
        if self.aiofuture.done():
            logger.warning(f"QtFuture {self} already done. Could not reject")
            return

        self.loop.call_soon_threadsafe(self.aiofuture.set_exception, exp)


class KoilStopIteration(Exception):
    pass


class QtGenerator:
    def __init__(self):
        self.loop = asyncio.get_event_loop()
        self.aioqueue = asyncio.Queue()
        self.iscancelled = False

    def _set_cancelled(self):
        """WIll be called by the asyncio loop"""
        self.iscancelled = True

    def next(self, *args):
        self.loop.call_soon_threadsafe(self.aioqueue.put_nowait, *args)

    def throw(self, exception):
        self.loop.call_soon_threadsafe(self.aioqueue.put_nowait, exception)

    def stop(self):
        self.loop.call_soon_threadsafe(self.aioqueue.put_nowait, KoilStopIteration())

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            res = await self.aioqueue.get()
            if isinstance(res, KoilStopIteration):
                raise StopAsyncIteration
            if isinstance(res, Exception):
                raise res
        except asyncio.CancelledError:
            raise StopAsyncIteration
        return res


T = TypeVar("T")
P = ParamSpec("P")


class QtCoro(QtCore.QObject, Generic[T, P]):
    called = QtCore.Signal(QtFuture, tuple, dict, object)
    cancelled = QtCore.Signal(QtFuture)

    def __init__(
        self,
        coro: Callable[P, T],
        *args: P.args,
        autoresolve=False,
        use_context=True,
        **kwargs: P.kwargs,
    ):
        super().__init__(*args, **kwargs)
        assert not inspect.iscoroutinefunction(
            coro
        ), f"This should not be a coroutine, but a normal qt slot {'with the first parameter being a qtfuture' if autoresolve is False else ''}"
        self.coro = coro
        self.called.connect(self.on_called)
        self.autoresolve = autoresolve
        self.use_context = use_context

    def on_called(self, future, args, kwargs, ctx):
        try:
            if self.use_context:
                for ctx, value in ctx.items():
                    ctx.set(value)

            if self.autoresolve:
                x = self.coro(*args, **kwargs)
                future.resolve(x)
            else:
                x = self.coro(future, *args, **kwargs)

        except Exception as e:
            logger.error(f"Error in Qt Coro {self.coro}", exc_info=True)
            future.reject(e)

    async def acall(self, *args: P.args, timeout=None, **kwargs: P.kwargs):
        qtfuture = QtFuture()
        ctx = contextvars.copy_context()
        self.called.emit(qtfuture, args, kwargs, ctx)
        try:
            if timeout:
                context, x = await asyncio.wait_for(qtfuture.aiofuture, timeout=timeout)
            else:
                context, x = await qtfuture.aiofuture

            for ctx, value in context.items():
                ctx.set(value)

            return x

        except asyncio.CancelledError:
            qtfuture._set_cancelled()
            self.cancelled.emit(qtfuture)
            raise


class QtListener:
    def __init__(self, loop, queue) -> None:
        self.queue = queue
        self.loop = loop

    def __call__(self, *args):
        self.loop.call_soon_threadsafe(self.queue.put_nowait, args)


class QtSignal(QtCore.QObject, Generic[T, P]):
    def __init__(
        self,
        signal: QtCore.Signal,
        *args,
        use_context=True,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.signal = signal
        self.signal.connect(self.on_called)
        self.listeners = {}
        self.use_context = use_context
        self._attached = None

    def on_called(self, *returns):
        for listener in self.listeners.values():
            listener(*returns)

    async def aiterate(self, timeout=None):
        unique_id = uuid.uuid4().hex
        loop = asyncio.get_event_loop()
        queue = asyncio.Queue()
        listener = self.listeners[unique_id] = QtListener(loop, queue)

        try:
            while True:
                z = await asyncio.wait_for(listener.queue.get(), timeout=timeout)
                if len(z) == 1:
                    z = z[0]
                yield z

        except Exception as e:
            del self.listeners[unique_id]
            raise e

        except asyncio.CancelledError:
            del self.listeners[unique_id]
            raise

    async def aonce(self, timeout=None):
        async for i in self.aiterate(timeout=timeout):
            return i


class QtRunner(KoilRunner, QtCore.QObject):
    started = QtCore.Signal()
    errored = QtCore.Signal(Exception)
    cancelled = QtCore.Signal()
    returned = QtCore.Signal(object)
    _returnedwithoutcontext = QtCore.Signal(object, object)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._returnedwithoutcontext.connect(self.on_returnedwithoutcontext)

    def on_returnedwithoutcontext(self, res, ctxs):
        for ctx, value in ctxs.items():
            ctx.set(value)
        self.returned.emit(res)

    def run(self, *args: P.args, **kwargs: P.kwargs):
        args = self.args + args
        kwargs = {**self.kwargs, **kwargs}

        loop = current_loop.get()
        assert loop is not None, "No loop found"
        assert loop.is_running(), "Loop is not running"
        cancel_event = threading.Event()

        future = run_threaded_with_context_and_signals(
            self.coro,
            loop,
            cancel_event,
            self.started,
            self._returnedwithoutcontext,
            self.errored,
            self.cancelled,
            *args,
            **kwargs,
        )
        return KoilFuture(future, cancel_event)

    def __call__(self, *args: Any, **kwds: Any) -> Any:
        return self.run(*args, **kwds)


class QtGeneratorRunner(KoilGeneratorRunner, QtCore.QObject):
    errored = QtCore.Signal(Exception)
    cancelled = QtCore.Signal()
    yielded = QtCore.Signal(object)
    done = QtCore.Signal(object)
    _yieldedwithoutcontext = QtCore.Signal(object, object)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._yieldedwithoutcontext.connect(self.on_yieldedwithoutcontext)

    def on_yieldedwithoutcontext(self, x, context):
        for ctx, value in context.items():
            ctx.set(value)

        self.yielded.emit(x)

    async def wrapped_future(self, args, kwargs, ctxs):
        for ctx, value in ctxs.items():
            ctx.set(value)

        try:
            args = self.args + args
            kwargs = {**self.kwargs, **kwargs}
            async for i in self.iterator(*args, **kwargs):
                newcontext = contextvars.copy_context()
                self._yieldedwithoutcontext.emit(i, newcontext)
        except Exception as e:
            self.errored.emit(e)
        except asyncio.CancelledError:
            self.cancelled.emit()

    def run(self, *args, **kwargs):
        args = self.args + args
        kwargs = {**self.kwargs, **kwargs}
        cancel_event = threading.Event()
        loop = current_loop.get()
        assert loop is not None, "No loop found"
        assert loop.is_running(), "Loop is not running"

        future = iterate_threaded_with_context_and_signals(
            self.iterator,
            loop,
            cancel_event,
            self._yieldedwithoutcontext,
            self.errored,
            self.cancelled,
            self.done,
            *args,
            **kwargs,
        )
        return KoilYieldFuture(future, cancel_event)


class QtKoilMixin(KoilMixin):
    def __enter__(self):
        super().__enter__()
        assert self.parent, "Parent must be set before entering the loop"
        self._qobject = WrappedObject(parent=self.parent, koil=self)
        assert (
            self._qobject.parent() is not None
        ), "No parent found. Please provide a parent"
        ap_instance = QtWidgets.QApplication.instance()
        if ap_instance is None:
            raise NotImplementedError("Qt Application not found")
        return self


def async_generator_to_qt(func):
    return QtGeneratorRunner(func)


def async_to_qt(func):
    return QtRunner(func)


def qt_to_async(func, autoresolve=False, use_context=True):
    return QtCoro(func, autoresolve=autoresolve, use_context=use_context).acall


def qtgenerator_to_async(func):
    return QtGenerator(func)


class UnkoiledQt(Protocol):
    errored: QtCore.Signal
    cancelled: QtCore.Signal()
    yielded: QtCore.Signal
    done: QtCore.Signal
    returned: QtCore.Signal

    def run(self, *args, **kwargs) -> KoilFuture:
        """Runs the function in the governing loop and returns a KoilFuture

        This is useful if you want to cancel the function from the outside.
        The function will be run in the governing loop and the result will be
        send to the main thread via a QtSignal.

        Args:
            *args: The arguments for the function
            **kwargs: The keyword arguments for the function

        Returns:
            KoilFuture: The future that can be cancelled

        """
        ...


class KoilQt(Protocol):
    errored: QtCore.Signal
    cancelled: QtCore.Signal()
    yielded: QtCore.Signal
    done: QtCore.Signal
    returned: QtCore.Signal

    def run(self, *args, **kwargs) -> KoilFuture:
        """Runs the function in the governing loop and returns a KoilFuture

        This is useful if you want to cancel the function from the outside.
        The function will be run in the governing loop and the result will be
        send to the main thread via a QtSignal.

        Args:
            *args: The arguments for the function
            **kwargs: The keyword arguments for the function

        Returns:
            KoilFuture: The future that can be cancelled

        """
        ...


def unkoilqt(func, *args, **kwargs) -> UnkoiledQt:
    """Unkoils a function so that it can be run in the main thread

    Args:
        func (Callable): The function to run in the main thread



    """

    if not (check_is_asyncgen(func) or check_is_asyncfunc(func)):
        raise TypeError(f"{func} is not an async function")

    if check_is_asyncgen(func):
        return async_generator_to_qt(func, *args, **kwargs)

    else:
        return async_to_qt(func, *args, **kwargs)


def koilqt(func, *args, autoresolve=None, **kwargs) -> Callable[..., Awaitable[Any]]:
    """Converts a qt mainthread function to be run in the asyncio loop

    Args:
        func (Callable): The function to run in the main thread (can also
        be a generator)

    Returns:
        Callable[..., Awaitable[Any]]: The function that can be run in the
        asyncio loop

    """

    if check_is_asyncgen(func) or check_is_asyncfunc(func):
        raise TypeError(
            f"{func} should NOT be a coroutine function. This is a decorator to convert a function to be callable form the asyncio loop"
        )

    if check_is_syncgen(func):
        if autoresolve is not None or autoresolve is True:
            raise TypeError("Cannot autoresolve a generator")
        return qtgenerator_to_async(func, *args, **kwargs)

    else:
        if autoresolve is None:
            autoresolve = True
        return qt_to_async(func, *args, autoresolve=autoresolve, **kwargs)


class WrappedObject(QtCore.QObject):
    def __init__(self, *args, koil: Koil = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.koil = koil
        self._hooked_close_event = self.parent().closeEvent
        self.parent().closeEvent = self.on_close_event
        self.parent().destroyed.connect(self.destroyedEvent)

    def on_close_event(self, event):
        if self.koil.running:
            self.koil.__exit__(None, None, None)
        self._hooked_close_event(event)

    def destroyedEvent(self):
        if hasattr(self, "koil"):
            if self.koil.running:
                self.koil.__exit__(None, None, None)


@dataclass
class QtKoil(QtKoilMixin):
    auto_enter = True
    disconnect_on_close: bool = True
    parent: QtWidgets.QWidget = None

    def __post_init__(self):
        self.name = self.parent.__class__.__name__

    class Config:
        arbitrary_types_allowed = True


def create_qt_koil(parent, auto_enter: bool = True) -> QtKoil:
    koil = QtKoil(parent=parent)
    if auto_enter:
        koil.enter()
    return koil
