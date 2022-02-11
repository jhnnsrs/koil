from typing import Optional, Type, Union
from koil.checker.base import BaseChecker
from koil.checker.registry import register_checker
from koil.state import KoilState
from koil.task import KoilTask
import asyncio


try:
    from qtpy import QtWidgets
    from qtpy import QtCore

    class QtTask(KoilTask, QtCore.QObject):
        except_signal = QtCore.Signal(Exception)
        cancelled_signal = QtCore.Signal()
        done_signal = QtCore.Signal(object)

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)

        async def wrapped_future(self, future):
            try:
                value = await super().wrapped_future(future)
                self.done_signal.emit(value)
            except Exception as e:
                self.except_signal.emit(e)
            except asyncio.CancelledError:
                self.cancelled_signal.emit()

    HAS_QT = True

except Exception as e:
    QtTask = None
    QtWidgets = None
    HAS_QT = False


class QtKoilState(KoilState):
    def __init__(self, qt_app=None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.qt_app = qt_app

    def get_task_class(self) -> Optional[Type[KoilTask]]:
        return QtTask


@register_checker()
class QtChecker(BaseChecker):
    def force_state(self) -> Optional[KoilState]:
        """Checks if a running Qt Instance is there, if so we would like
        to run in a seperate thread

        Returns:
            Union[None, KoilState]: [description]
        """
        if HAS_QT:
            ap_instance = QtWidgets.QApplication.instance()
            if ap_instance is None:
                return None
            ap_instance.lastWindowClosed.connect(self.koil.close)

            return QtKoilState(qt_app=ap_instance, threaded=True, prefer_task=True)

        return None
