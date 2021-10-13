


import asyncio
from koil.task.base import KoilTask
from qtpy import QtCore

class QtTask(KoilTask, QtCore.QObject):
    except_signal = QtCore.Signal(Exception)
    cancelled_signal = QtCore.Signal()
    done_signal = QtCore.Signal(object)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)


    async def wrapped_future(self, future):
        try:
            value =  await super().wrapped_future(future)
            self.done_signal.emit(value)
        except Exception as e:
            self.except_signal.emit(e)
        except asyncio.CancelledError:
            self.cancelled_signal.emit()