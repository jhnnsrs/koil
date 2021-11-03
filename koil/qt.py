import asyncio
from asyncio.futures import Future
from qtpy import QtGui
from qtpy.QtCore import QObject, Signal
import uuid
import logging
from koil.koil import Koil, get_current_koil

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
        self.loop = asyncio.get_event_loop()
        if not self.call_connected:
            logger.info(f"Future {self} was never connected")
            if self.pass_through:
                return None

        reference = str(uuid.uuid4())
        future = self.loop.create_future()
        self.futureMap[reference] = future
        self.callSignal.emit(reference, args, kwargs)

        try:
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
