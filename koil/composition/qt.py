from pydantic import Field
from koil.composition.base import PedanticKoil
from typing import Optional

from koil.qt import QtFuture, QtKoilMixin, QtRunner
from qtpy import QtWidgets, QtCore
import logging


logger = logging.getLogger(__name__)


class QtPedanticKoil(PedanticKoil, QtKoilMixin):
    class Config:
        underscore_attrs_are_private = True
        arbitrary_types_allowed = True
