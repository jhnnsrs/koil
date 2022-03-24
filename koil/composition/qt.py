from pydantic import Field
from koil.composition.base import PedanticKoil

from typing import Optional, Type, TypeVar

from koil.qt import QtFuture, QtGeneratorTask, QtTask
from qtpy import QtWidgets, QtCore
import logging

logger = logging.getLogger(__name__)


class WrappedObject(QtCore.QObject):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def set_koil(self, koil: PedanticKoil):
        self.koil = koil

    def close(self):
        self.koil.__exit__(None, None, None)


class QtPedanticKoil(PedanticKoil):
    disconnect_on_close = True
    task_class: Optional[Type[QtTask]] = Field(default=QtTask, exclude=True)
    gen_class: Optional[Type[QtGeneratorTask]] = Field(
        default=QtGeneratorTask, exclude=True
    )
    qobject: WrappedObject = Field(default_factory=WrappedObject)

    def __enter__(self):
        self.qobject.set_koil(self)
        ap_instance = QtWidgets.QApplication.instance()
        if ap_instance is None:
            raise NotImplementedError("Qt Application not found")
        if self.disconnect_on_close:
            ap_instance.lastWindowClosed.connect(self.qobject.close)
        return super().__enter__()

    class Config:
        arbitrary_types_allowed = True

    def create_task(self, coro, *args, **kwargs) -> QtFuture:
        logger.warning(
            """Creating a task within the qt loop is not recommended. As this might lead to deathlocks and other bad things. (Especially when calling qt back from the koil). Try to use a `create_runner`
            and connect your signals to and then call the `run` method instead."""
        )
        return coro(*args, **kwargs, as_task=True).run()

    def create_runner(self, coro, *args, **kwargs) -> QtTask:
        return coro(*args, **kwargs, as_task=True)
