from pydantic import Field
from koil.composition.base import PedanticKoil
from typing import Optional

from koil.qt import QtFuture, QtKoilMixin, QtRunner
from qtpy import QtWidgets, QtCore
import logging


logger = logging.getLogger(__name__)


class QtPedanticKoil(PedanticKoil, QtKoilMixin):
    parent: Optional[QtWidgets.QWidget] = Field(exclude=True)

    _qobject: QtCore.QObject = None

    def create_task(self, coro, *args, **kwargs) -> QtFuture:
        logger.warning(
            """Creating a task within the qt loop is not recommended. As this might lead to deathlocks and other bad things. (Especially when calling qt back from the koil). Try to use a `create_runner`
            and connect your signals to and then call the `run` method instead."""
        )
        return coro(*args, **kwargs, as_task=True).run()

    def create_runner(self, coro, *args, **kwargs) -> QtRunner:
        return coro(*args, **kwargs, as_task=True)

    class Config:
        underscore_attrs_are_private = True
        arbitrary_types_allowed = True
