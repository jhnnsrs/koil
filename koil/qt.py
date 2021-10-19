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
    call = Signal(Reference, tuple, dict)
    resolve = Signal(Reference, object)
    reject = Signal(Reference, Exception)
    cancel = Signal(Reference, tuple, dict)
    cancelled = Signal(Reference)

    
    def __init__(self, *args, koil: Koil = None, cancel_timeout = 4, pass_through=False, **kwargs) -> None:
        super().__init__( *args, **kwargs)
        self.resolve.connect(self.on_resolve)
        self.reject.connect(self.on_reject)
        self.cancelled.connect(self.on_cancelled)
        self.futureMap = {}
        self.cancelMap = {}
        self.koil = koil or get_current_koil()
        self.loop = self.koil.loop
        self.pass_through = pass_through
        self.cancel_timeout = 4


    def on_cancelled(self, ref):
        assert ref in self.cancelMap, "Received cancellation for non invoked cancel future"
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

        if get_receiver_length(self, self.call, "call(str, tuple, dict)") == 0:
           if self.pass_through: return None
           else: raise UnconnectedSignalError("This future has no connected receivers")


        
        reference = str(uuid.uuid4())
        future = self.loop.create_future()
        self.futureMap[reference] = future
        self.call.emit(reference, args, kwargs)
        try:
            return await future
        except asyncio.CancelledError as e:
            if self.receivers(self.cancel) > 0:
                cancel_future = self.loop.create_future()
                self.cancelMap[reference] = future
                self.cancel.emit(reference, args, kwargs)
                try:
                    await asyncio.wait_for(cancel_future, timeout=self.cancel_timeout)
                    raise e
                except asyncio.TimeoutError as e:
                    logger.error("Cancellation recevied timeout, Cancelling anyways")
                    raise e
            else: 
                raise e






class TimeoutFutureWrapper(FutureWrapper):

    def __init__(self, *args, timeout=None, **kwargs) -> None:
        super().__init__( *args, **kwargs)
        self.timeout = timeout

    async def acall(self, *args, **kwargs):
        return await asyncio.wait_for(super().acall(*args, **kwargs), timeout=self.timeout)

