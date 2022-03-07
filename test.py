import asyncio
from async_timeout import timeout

import pytest
from koil.errors import ContextError

from koil.helpers import unkoil
from koil import Koil
from PyQt5 import QtWidgets, QtCore
from koil.qt import QtCoro, QtFuture, QtGeneratorTask, QtKoil, QtTask
import contextvars
from rich.logging import RichHandler
import logging

x = contextvars.ContextVar("x")

logging.basicConfig(level="INFO", handlers=[RichHandler()])


async def sleep_and_resolve():
    await asyncio.sleep(0.1)
    return 1


async def sleep_and_raise():
    await asyncio.sleep(0.1)
    raise Exception("Task is done!")


async def sleep_and_use_context():
    await asyncio.sleep(0.1)
    return x.get() + 1


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
        self.call_raise_button = QtWidgets.QPushButton("Call Raise")
        self.call_context_button = QtWidgets.QPushButton("Call Context")

        self.sleep_and_resolve_task = QtTask(sleep_and_resolve)
        self.sleep_and_resolve_task.returned.connect(self.task_finished)

        self.sleep_and_use_context_task = QtTask(sleep_and_use_context)
        self.sleep_and_use_context_task.returned.connect(self.task_finished)

        self.sleep_and_yield_task = QtGeneratorTask(sleep_and_yield)
        self.sleep_and_yield_task.yielded.connect(self.task_finished)

        self.sleep_and_raise_task = QtTask(sleep_and_raise)
        self.sleep_and_resolve_task.returned.connect(self.task_finished)

        self.greet_label = QtWidgets.QLabel("")
        self.value = None
        layout = QtWidgets.QVBoxLayout()
        layout.addWidget(self.call_task_button)
        layout.addWidget(self.call_gen_button)
        layout.addWidget(self.call_context_button)
        layout.addWidget(self.greet_label)

        self.setLayout(layout)

        self.call_task_button.clicked.connect(self.call_task)
        self.call_gen_button.clicked.connect(self.call_gen)
        self.call_context_button.clicked.connect(self.call_context)
        self.call_raise_button.clicked.connect(self.call_raise)

    def call_task(self):
        self.sleep_and_resolve_task.run()

    def call_gen(self):
        self.sleep_and_yield_task.run()

    def call_context(self):
        x.set(1)
        self.sleep_and_use_context_task.run()

    def call_raise(self):
        self.sleep_and_raise_task.run()

    def task_finished(self, int):
        self.value = int
        self.greet_label.setText("Hello!")


def __main__():
    app = QtWidgets.QApplication([])
    widget = KoiledInterferingWidget()
    widget.show()
    app.exec_()


if __name__ == "__main__":
    __main__()
