from inflection import underscore
from pydantic import Field, dataclasses, root_validator
from pydantic.dataclasses import dataclass
from koil.composition.base import PedanticKoil
from typing import Optional, Type, TypeVar
from koil.koil import KoilMixin

from koil.qt import QtFuture, QtGeneratorRunner, QtKoilMixin, QtRunner, WrappedObject
from qtpy import QtWidgets, QtCore
import logging

from koil.task import KoilFuture

logger = logging.getLogger(__name__)


class QtPedanticKoil(PedanticKoil, QtKoilMixin):
    parent: QtWidgets.QWidget = Field(exclude=True)

    _qobject: QtCore.QObject = None

    @root_validator()
    def check_not_running_in_koil(cls, values):
        values["name"] = repr(values["parent"])
        return values

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
