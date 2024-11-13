
from psygnal import Signal, SignalInstance, emit_queued
import logging
import asyncio
from koil.helpers import unkoil


class BaseCoroSignalWrapper:

    def __init__(self, start_signal: SignalInstance, done_signal: SignalInstance, cancel_signal: SignalInstance | None = None, end_signal_thread: str = "main"):
        
        self.start_signal = start_signal
        self.end_signal = done_signal
        self.cancel_signal = cancel_signal
        self.end_signal.connect(self.on_end, thread=end_signal_thread)
        self.future = None
        self.loop = None


    def on_end(self, *args, **kwargs):
        if self.future is None or self.loop is None:
            return
        

        self.end_signal.disconnect(self.on_end)
        if self.future.cancelled():
            logging.warning("Future was cancelled but signal was received. Ignoring")
            return
        self.loop.call_soon_threadsafe(self.future.set_result, *args)



    async def arun(self, *args, **kwargs):
        self.loop = asyncio.get_running_loop()
        self.future = asyncio.Future()
        self.start_signal.emit(*args, **kwargs)

        try:
            result = await self.future
            return result

        except asyncio.CancelledError:
            print("Future was cancelled")
            if self.cancel_signal is not None:
                self.cancel_signal.emit()
            raise







class SyncCoroSignalWrapper(BaseCoroSignalWrapper):


    def __call__(self, *args, **kwargs):
        return unkoil(self.arun, *args, **kwargs)
    

class AsyncCoroSignalWrapper(BaseCoroSignalWrapper):
    
    def __call__(self, *args, **kwargs):
        return self.arun(*args, **kwargs)
    



def signals_to_async(start_signal: SignalInstance, done_signal: SignalInstance, cancel_signal: SignalInstance | None = None, end_signal_thread: str = "main") -> BaseCoroSignalWrapper:
    return AsyncCoroSignalWrapper(start_signal, done_signal, cancel_signal, end_signal_thread)

def signals_to_sync(start_signal: SignalInstance, done_signal: SignalInstance, cancel_signal: SignalInstance | None = None, end_signal_thread: str = "main") -> BaseCoroSignalWrapper:
    return SyncCoroSignalWrapper(start_signal, done_signal, cancel_signal, end_signal_thread)






