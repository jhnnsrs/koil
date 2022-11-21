import asyncio
import contextvars
import inspect
import logging
import threading
from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

from qtpy import QtCore, QtWidgets
from typing_extensions import ParamSpec

from koil.koil import Koil, KoilMixin
from koil.task import KoilFuture, KoilGeneratorRunner, KoilRunner, KoilYieldFuture
from koil.utils import (
    iterate_threaded_with_context_and_signals,
    run_threaded_with_context_and_signals,
)
from koil.vars import current_loop

logger = logging.getLogger(__name__)


Reference = str


class UnconnectedSignalError(Exception):
    pass


def get_receiver_length(qobject, qsignal, callstring):
    try:
        return qobject.receivers(qsignal)
    except:
        return qobject.receivers(callstring)


class QtFuture:
    def __init__(self):
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
        self.loop.call_soon_threadsafe(self.aiofuture.set_result, (ctx,) + args)

    def reject(self, exp: Exception):
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
    cancelled = QtCore.Signal(str)

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
        ), "This should not be a coroutine, but a normal qt slot with the first parameter being a qtfuture"
        self.coro = coro
        self.called.connect(self.on_called)
        self.autoresolve = autoresolve
        self.use_context = use_context

    def on_called(self, future, args, kwargs, ctx):

        try:
            if self.use_context:
                for ctx, value in ctx.items():
                    ctx.set(value)

            x = self.coro(future, *args, **kwargs)
            if self.autoresolve:
                future.resolve(x)
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
            raise


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

    def create_task(self, coro, *args, **kwargs) -> QtFuture:
        logger.warning(
            """Creating a task within the qt loop is not recommended. As this might lead to deathlocks and other bad things. (Especially when calling qt back from the koil). Try to use a `create_runner`
            and connect your signals to and then call the `run` method instead."""
        )
        return coro(*args, **kwargs, as_task=True).run()

    def create_runner(self, coro, *args, **kwargs) -> QtRunner:
        return coro(*args, **kwargs, as_task=True)
