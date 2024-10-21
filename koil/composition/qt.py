from koil.composition.base import PedanticKoil
from typing import Optional

from koil.qt import QtKoilMixin
from qtpy import QtWidgets, QtCore
import logging


logger = logging.getLogger(__name__)


class QtPedanticKoil(PedanticKoil, QtKoilMixin):
    parent: Optional[QtWidgets.QWidget] = None
    _qobject: Optional[QtCore.QObject] = None
