import asyncio
from asyncio.futures import Future
import contextvars
import inspect
from typing import Callable, Generic, TypeVar
from qtpy import QtGui
from qtpy.QtCore import QObject, Signal, QThread
import uuid
import logging
from koil.koil import Koil
from qtpy import QtWidgets
from qtpy import QtCore
from concurrent import futures
from typing_extensions import ParamSpec, final
from koil.task import KoilGeneratorTask, KoilTask


logger = logging.getLogger(__name__)


Reference = str


class UnconnectedSignalError(Exception):
    pass


def get_receiver_length(qobject, qsignal, callstring):
    try:
        return qobject.receivers(qsignal)
    except:
        return qobject.receivers(callstring)


class FutureWrapper(QObject):
    callSignal = Signal(Reference, tuple, dict)
    resolveSignal = Signal(Reference, object)
    rejectSignal = Signal(Reference, Exception)
    cancelSignal = Signal(Reference, tuple, dict)
    cancelledSignal = Signal(Reference)

    def wire(self, receiver, cancel_receiver=None):
        """Wires a Call signal to a QtPy Slot

        Args:
            receiver ([type]): [description]
            cancel_receiver ([type], optional): [description]. Defaults to None.
        """
        self.call_connected = True
        self.callSignal.connect(receiver)

        if cancel_receiver:
            self.cancel_connected = True
            self.cancelSignal.connect(cancel_receiver)

    def resolve(self, reference, value):
        self.resolveSignal.emit(reference, value)

    def reject(self, reference, value: Exception):
        self.rejectSignal.emit(reference, value)

    def cancelled(self, reference):
        self.cancelledSignal.emit(reference)

    def __init__(self, *args, cancel_timeout=4, pass_through=False, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.resolveSignal.connect(self.on_resolve)
        self.rejectSignal.connect(self.on_reject)
        self.cancelledSignal.connect(self.on_cancelled)
        self.futureMap = {}
        self.cancelMap = {}
        self.pass_through = pass_through
        self.cancel_timeout = 4

        self.call_connected = False
        self.cancel_connected = False

    def on_cancelled(self, ref):
        assert (
            ref in self.cancelMap
        ), "Received cancellation for non invoked cancel future"
        cancel_future = self.cancelMap[ref]
        if not cancel_future.done():
            self.loop.call_soon_threadsafe(cancel_future.set_result, None)

    def on_resolve(self, ref, obj):
        assert ref in self.futureMap, "Received resolvation for non invoked future"
        future = self.futureMap[ref]
        if not future.done():
            self.loop.call_soon_threadsafe(future.set_result, obj)

    def on_reject(self, ref, exp):
        assert ref in self.futureMap, "Received rejection for non invoked future"
        future = self.futureMap[ref]
        if not future.done():
            self.loop.call_soon_threadsafe(future.set_exception, exp)

    async def acall(self, *args, **kwargs):
        print("Called Here")
        self.loop = asyncio.get_event_loop()
        if not self.call_connected:
            print(f"Future {self} was never connected")
            if self.pass_through:
                return None

        reference = str(uuid.uuid4())
        future = self.loop.create_future()
        self.futureMap[reference] = future
        self.callSignal.emit(reference, args, kwargs)

        try:
            print("Calling Future?")
            return await future
        except asyncio.CancelledError as e:
            if not self.cancel_connected:
                logger.info(f"Future {self} has no cancellation partner. Just Raising")
                raise e

            cancel_future = self.loop.create_future()
            self.cancelMap[reference] = future
            self.cancelSignal.emit(reference, args, kwargs)
            try:
                await asyncio.wait_for(cancel_future, timeout=self.cancel_timeout)
                raise e
            except asyncio.TimeoutError as t:
                logger.error("Cancellation recevied timeout, Cancelling anyways")
                raise e


class TimeoutFutureWrapper(FutureWrapper):
    def __init__(self, *args, timeout=None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.timeout = timeout

    async def acall(self, *args, **kwargs):
        return await asyncio.wait_for(
            super().acall(*args, **kwargs), timeout=self.timeout
        )


class QtFuture:
    def __init__(self):
        self.loop = asyncio.get_event_loop()
        self.aiofuture = asyncio.Future()
        self.iscancelled = False

    def _set_cancelled(self):
        """WIll be called by the asyncio loop"""
        self.iscancelled = True

    def resolve(self, *args):
        ctx = contextvars.copy_context()
        self.loop.call_soon_threadsafe(self.aiofuture.set_result, (ctx,) + args)

    def reject(self, exp: Exception):
        self.loop.call_soon_threadsafe(self.aiofuture.set_exception, exp)


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


class QtTask(KoilTask, QtCore.QObject):
    errored = QtCore.Signal(Exception)
    cancelled = QtCore.Signal()
    returned = QtCore.Signal(object)
    _returnedwithoutcontext = QtCore.Signal(object, object)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._returnedwithoutcontext.connect(self.on_returnedwithoutcontext)

    def on_returnedwithoutcontext(self, obj, context):
        for ctx, value in context.items():
            ctx.set(value)

        self.returned.emit(obj)

    async def wrapped_future(self, args, kwargs, context):
        try:
            for ctx, value in context.items():
                ctx.set(value)

            args = self.args + args
            kwargs = {**self.kwargs, **kwargs}
            i = await self.coro(*args, **kwargs)
            newcontext = contextvars.copy_context()
            self._returnedwithoutcontext.emit(i, newcontext)
        except Exception as e:
            logger.error(
                f"Error in task of coroutine {self.coro.__name__}", exc_info=True
            )
            self.errored.emit(e)
        except asyncio.CancelledError:
            self.cancelled.emit()

    def run(self, *args, **kwargs):

        ctxs = contextvars.copy_context()
        future = asyncio.run_coroutine_threadsafe(
            self.wrapped_future(args, kwargs, ctxs), self.loop
        )
        return future


class QtGeneratorTask(KoilGeneratorTask, QtCore.QObject):
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
        print("osinosinsoi")
        for ctx, value in ctxs.items():
            ctx.set(value)

        try:
            args = self.args + args
            kwargs = {**self.kwargs, **kwargs}
            print("nanana")
            async for i in self.iterator(*args, **kwargs):
                print(i)

                newcontext = contextvars.copy_context()
                self._yieldedwithoutcontext.emit(i, newcontext)
        except Exception as e:
            self.errored.emit(e)
        except asyncio.CancelledError:
            self.cancelled.emit()

    def run(self, *args, **kwargs):
        ctxs = contextvars.copy_context()
        print("Hallo")

        future = asyncio.run_coroutine_threadsafe(
            self.wrapped_future(args, kwargs, ctxs), self.loop
        )
        return future


class QtKoil(Koil, QtCore.QObject):
    def __init__(
        self,
        *args,
        disconnect_on_close=True,
        autoconnect=True,
        parent=None,
        uvify=False,
        **kwargs,
    ) -> None:
        super().__init__(
            *args, **kwargs, uvify=False, task_class=QtTask, gen_class=QtGeneratorTask
        )
        self.disconnect_on_close = disconnect_on_close
        if autoconnect:
            self.connect()

    def close(self):
        self.__exit__(None, None, None)

    def connect(self):
        ap_instance = QtWidgets.QApplication.instance()
        if ap_instance is None:
            raise NotImplementedError("Qt Application not found")
        if self.disconnect_on_close:
            ap_instance.lastWindowClosed.connect(self.close)
        return self.__enter__()
