from typing import Type, Union
from koil.checker.base import BaseChecker
from koil.checker.registry import register_checker
from koil.state import KoilState
from koil.task.base import KoilTask
from koil.task.qt import QtTask


class QtKoilState(KoilState):
    
    def __init__(self, qt_app=None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.qt_app = qt_app

    def get_task_class(self) -> Type[KoilTask]:
        return QtTask


@register_checker()
class QtChecker(BaseChecker):

    def force_state(self) -> Union[None, KoilState]:
        """Checks if a running Qt Instance is there, if so we would like
        to run in a seperate thread

        Returns:
            Union[None, KoilState]: [description]
        """

        try:
            from qtpy import QtWidgets

            ap_instance = QtWidgets.QApplication.instance() 
            ap_instance.lastWindowClosed.connect(self.koil.close)

            if ap_instance is None:
                return None

            else:
                return QtKoilState(qt_app=ap_instance,threaded=True, prefer_task =True)
        except:
            return None 

