import asyncio

import pytest
from koil.errors import ContextError

from koil.helpers import unkoil
from .context import AsyncContextManager
from koil import Koil
from PyQt5 import QtWidgets, QtCore
from koil.qt import QtCoro, QtFuture, QtGeneratorTask, QtKoil, QtTask


async def sleep_and_resolve():
    await asyncio.sleep(0.1)
    return 1


async def sleep_and_yield(times=5):
    for i in range(times):
        await asyncio.sleep(0.1)
        print(i)
        yield i


class KoiledWidget(QtWidgets.QWidget):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.koil = QtKoil()

        self.button_greet = QtWidgets.QPushButton("Greet")
        self.greet_label = QtWidgets.QLabel("")

        layout = QtWidgets.QVBoxLayout()
        layout.addWidget(self.button_greet)
        layout.addWidget(self.greet_label)

        self.setLayout(layout)

        self.button_greet.clicked.connect(self.greet)

    def greet(self):
        self.greet_label.setText("Hello!")


class KoiledInterferingWidget(QtWidgets.QWidget):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.koil = QtKoil()

        self.call_task_button = QtWidgets.QPushButton("Call Task")
        self.call_gen_button = QtWidgets.QPushButton("Call Generator")
        self.greet_label = QtWidgets.QLabel("")
        self.value = None
        layout = QtWidgets.QVBoxLayout()
        layout.addWidget(self.call_task_button)
        layout.addWidget(self.call_gen_button)
        layout.addWidget(self.greet_label)

        self.setLayout(layout)

        self.call_task_button.clicked.connect(self.call_task)
        self.call_gen_button.clicked.connect(self.call_gen)

    def call_task(self):
        self.task = QtTask(sleep_and_resolve())
        self.task.returned.connect(self.task_finished)
        self.task.run()

    def call_gen(self):
        self.task = QtGeneratorTask(sleep_and_yield())
        self.task.yielded.connect(self.task_finished)
        self.task.run()

    def task_finished(self, int):
        self.value = int
        self.greet_label.setText("Hello!")


class KoiledInterferingFutureWidget(QtWidgets.QWidget):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.koil = QtKoil()

        self.do_me = QtCoro(self.in_qt_task)

        self.call_task_button = QtWidgets.QPushButton("Call Task")
        self.greet_label = QtWidgets.QLabel("")

        layout = QtWidgets.QVBoxLayout()
        layout.addWidget(self.call_task_button)
        layout.addWidget(self.greet_label)

        self.setLayout(layout)

        self.call_task_button.clicked.connect(self.call_task)

    def in_qt_task(self, future: QtFuture):
        self.task_was_run = True
        future.resolve("called")
        print("here")

    def call_task(self):
        self.task = QtTask(self.call_coro())
        self.task.returned.connect(self.task_finished)
        self.task.run()

    def task_finished(self):
        self.greet_label.setText("Hello!")

    async def call_coro(self):
        print(self)
        x = await self.do_me.acall()
        self.coroutine_was_run = True
        print("nana")


def test_no_interference(qtbot):
    """Tests if just adding koil interferes with normal
    qtpy widgets.

    Args:
        qtbot (_type_): _description_
    """
    widget = KoiledWidget()
    qtbot.addWidget(widget)

    # click in the Greet button and make sure it updates the appropriate label
    qtbot.mouseClick(widget.button_greet, QtCore.Qt.LeftButton)

    assert widget.greet_label.text() == "Hello!"


def test_call_task(qtbot):
    """Tests if we can call a task from a koil widget."""
    widget = KoiledInterferingWidget()
    qtbot.addWidget(widget)

    # click in the Greet button and make sure it updates the appropriate label
    qtbot.mouseClick(widget.call_task_button, QtCore.Qt.LeftButton)
    with qtbot.waitSignal(widget.task.returned) as b:
        print(b)


def test_call_gen(qtbot):
    """Tests if we can call a task from a koil widget."""
    widget = KoiledInterferingWidget()
    qtbot.addWidget(widget)

    # click in the Greet button and make sure it updates the appropriate label
    qtbot.mouseClick(widget.call_gen_button, QtCore.Qt.LeftButton)
    with qtbot.waitSignal(widget.task.yielded) as b:
        print(b)


def test_call_future(qtbot):
    """Tests if we can call a task from a koil widget."""
    widget = KoiledInterferingFutureWidget()
    qtbot.addWidget(widget)

    # click in the Greet button and make sure it updates the appropriate label
    qtbot.mouseClick(widget.call_task_button, QtCore.Qt.LeftButton)
    with qtbot.waitSignal(widget.task.returned, raising=False, timeout=1):
        pass

    assert widget.task_was_run == True
    assert widget.coroutine_was_run == True
