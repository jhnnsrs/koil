import asyncio
from typing import Tuple
import contextvars
import pytest

# Skip the entire module unless PyQt5 (and pytest-qt) are installed.
pytest.importorskip("PyQt5")
pytest.importorskip("pytestqt")

from PyQt5 import QtWidgets, QtCore
from koil.qt import QtFuture, QtGenerator, create_qt_koil
from koil.qt import (
    async_gen_to_qt,
    async_to_qt,
    qt_to_async,
    qt_gen_to_async_gen,
)
from pytestqt.qtbot import QtBot # type: ignore
from koil.contrib.pytest_qt import wait_for_qttask, wait_for_qtgenerator


x: contextvars.ContextVar["int"] = contextvars.ContextVar("x")


async def sleep_and_resolve() -> int:
    await asyncio.sleep(0.1)
    return 1


async def sleep_and_raise():
    await asyncio.sleep(0.1)
    raise Exception("Task is done!")


async def sleep_and_use_context():
    await asyncio.sleep(0.1)
    return x.get() + 1


async def sleep_and_yield(times: int = 5):
    for i in range(times):
        await asyncio.sleep(0.1)
        yield i



class KoiledWidget(QtWidgets.QWidget):
    def __init__(self, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent=parent, flags=QtCore.Qt.WindowFlags())
        self.koil = create_qt_koil(self)
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
    def __init__(self):
        super().__init__()
        self.koil = create_qt_koil(self)

        self.call_task_button = QtWidgets.QPushButton("Call Task")
        self.call_gen_button = QtWidgets.QPushButton("Call Generator")
        self.call_raise_button = QtWidgets.QPushButton("Call Raise")
        self.call_context_button = QtWidgets.QPushButton("Call Context")

        self.sleep_and_resolve_task = async_to_qt(sleep_and_resolve)
        #self.sleep_and_resolve_task.returned.connect(self.task_finished)

        self.sleep_and_use_context_task = async_to_qt(sleep_and_use_context)
        self.sleep_and_use_context_task.returned.connect(self.task_finished)

        self.sleep_and_yield_task = async_gen_to_qt(sleep_and_yield)
        self.sleep_and_yield_task.yielded.connect(self.task_finished)

        self.sleep_and_raise_task = async_to_qt(sleep_and_raise)
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
        self._task = self.sleep_and_resolve_task.run()

    def call_gen(self):
        self.sleep_and_yield_task.run()

    def call_context(self):
        self.sleep_and_use_context_task.run()

    def call_raise(self):
        self.sleep_and_raise_task.run()

    def task_finished(self, int: int):
        self.value = int
        self.greet_label.setText("Hello!")


class KoiledInterferingFutureWidget(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.koil = create_qt_koil(self)

        self.do_me = qt_to_async(self.in_qt_task)

        self.my_coro_task = async_to_qt(self.call_coro)
        self.my_coro_task.returned.connect(self.task_finished)

        self.task_was_run = False
        self.coroutine_was_run = False

        self.call_task_button = QtWidgets.QPushButton("Call Task")
        self.greet_label = QtWidgets.QLabel("")

        layout = QtWidgets.QVBoxLayout()
        layout.addWidget(self.call_task_button)
        layout.addWidget(self.greet_label)

        self.setLayout(layout)

        self.call_task_button.clicked.connect(self.call_task)

    def in_qt_task(self, future: QtFuture[str]):
        self.task_was_run = True
        future.resolve("called")

    def call_task(self):
        self.my_coro_task.run()

    def task_finished(self, both: Tuple[str, str]):
        str = both[0] + " " + both[1]
        self.greet_label.setText(str)

    async def call_coro(self) -> Tuple[str, str]:
        self.coroutine_was_run = True
        return await self.do_me.acall(), "called"


class KoiledGeneratorWidget(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.koil = create_qt_koil(self)
        self._task = None
        self.my_coro = async_to_qt(self.acall_coro)
        self.my_coro.returned.connect(self.task_finished)

        self.qt_generator = qt_gen_to_async_gen(self.iterator)

        self.task_was_run = False
        self.coroutine_was_run = False
        self.coroutine_finished = False

        self.call_task_button = QtWidgets.QPushButton("Call Task")
        self.greet_label = QtWidgets.QLabel("")

        layout = QtWidgets.QVBoxLayout()
        layout.addWidget(self.call_task_button)
        layout.addWidget(self.greet_label)

        self.setLayout(layout)

        self.call_task_button.clicked.connect(self.call_task)

    def in_qt_task(self, future: QtFuture[str]):
        self.task_was_run = True
        future.resolve("called")

    def iterator(self, iterator: QtGenerator[int], number: int = 5):
        for i in range(number):
            iterator.next(i)

    def call_task(self):
        self._task = self.my_coro.run()

    def task_finished(self, _: None):
        self.greet_label.setText("Hello!")

    async def acall_coro(self) -> None:
        async for x in self.qt_generator.acall(number=3):
            print(x)



@pytest.mark.qt
def test_koil_qt_no_interference(qtbot: QtBot):
    """Tests if just adding koil interferes with normal
    qtpy widgets.

    Args:
        qtbot (_type_): _description_
    """
    widget = KoiledWidget()
    qtbot.addWidget(widget) # type: ignore

    # click in the Greet button and make sure it updates the appropriate label
    qtbot.mouseClick(widget.button_greet, QtCore.Qt.LeftButton) # type: ignore

    assert widget.greet_label.text() == "Hello!"


@pytest.mark.qt
def test_koil_qt_call_task(qtbot: QtBot):
    """Tests if we can call a task from a koil widget."""
    widget = KoiledInterferingWidget()
    qtbot.addWidget(widget) # type: ignore

    
    wait_for_qttask(
        qtbot,
        widget.sleep_and_resolve_task,
        lambda qtbot: qtbot.mouseClick(widget.call_task_button, QtCore.Qt.LeftButton), # type: ignore
    )

@pytest.mark.qt
def test_call_gen(qtbot: QtBot ):
    """Tests if we can call a task from a koil widget."""
    widget = KoiledInterferingWidget()
    qtbot.addWidget(widget) # type: ignore

    # click in the Greet button and make sure it updates the appropriate label
    wait_for_qtgenerator(
        qtbot,
        widget.sleep_and_yield_task,
        lambda qtbot: qtbot.mouseClick(widget.call_gen_button, QtCore.Qt.LeftButton), # type: ignore
    )


@pytest.mark.qt
def test_call_future(qtbot: QtBot):
    """Tests if we can call a task from a koil widget."""
    widget = KoiledInterferingFutureWidget()
    qtbot.addWidget(widget) # type: ignore

    
    wait_for_qttask(
        qtbot,
        widget.my_coro_task,
        lambda qtbot: qtbot.mouseClick(widget.call_task_button, QtCore.Qt.LeftButton), # type: ignore
    )
    assert widget.task_was_run is True
    assert widget.coroutine_was_run is True


@pytest.mark.qt
def test_call_raise(qtbot: QtBot):
    """Tests if we can call a task from a koil widget."""
    widget = KoiledInterferingWidget()
    qtbot.addWidget(widget) # type: ignore

    # click in the Greet button and make sure it updates the appropriate label

    with qtbot.waitSignal(widget.sleep_and_raise_task.errored, timeout=1000) as b: # type: ignore
        qtbot.mouseClick(widget.call_raise_button, QtCore.Qt.LeftButton) # type: ignore



@pytest.mark.qt
def test_context(qtbot: QtBot):
    """Tests if we can call a task from a koil widget."""
    widget = KoiledInterferingWidget()
    qtbot.addWidget(widget) # type: ignore

    x.set(5)
    # click in the Greet button and make sure it updates the appropriate label

    with qtbot.waitSignal( # type: ignore
        widget.sleep_and_use_context_task.returned, timeout=1000
    ) as b:
        qtbot.mouseClick(widget.call_context_button, QtCore.Qt.LeftButton) # type: ignore

    assert b.args[0] == 6   # type: ignore


@pytest.mark.qt
def test_qt_koil_forwards_config(qtbot: QtBot):
    """QtKoil/create_qt_koil forward Koil constructor configuration."""
    widget = QtWidgets.QWidget()
    qtbot.addWidget(widget)  # type: ignore

    koil = create_qt_koil(
        parent=widget,
        auto_enter=False,
        cancel_timeout=5.0,
        uvify=False,
        rewrite_tracebacks=False,
        shutdown_join_timeout=1.0,
    )
    assert koil.cancel_timeout == 5.0
    assert koil.uvify is False
    assert koil.rewrite_tracebacks is False
    assert koil.shutdown_join_timeout == 1.0


@pytest.mark.qt
def test_async_to_qt_timeout_emits_errored(qtbot: QtBot):
    """A timed-out async task emits errored with KoilTimeoutError, not cancelled."""
    from koil.errors import KoilTimeoutError

    class TimeoutWidget(QtWidgets.QWidget):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.koil = create_qt_koil(parent=self)
            self.slow_task = async_to_qt(self.slow, timeout=0.05)

        async def slow(self):
            await asyncio.sleep(5)

    widget = TimeoutWidget()
    qtbot.addWidget(widget)  # type: ignore

    cancelled_calls = []
    widget.slow_task.cancelled.connect(lambda e: cancelled_calls.append(e))

    with qtbot.waitSignal(widget.slow_task.errored, timeout=5000) as blocker:  # type: ignore
        widget.slow_task.run()

    assert isinstance(blocker.args[0], KoilTimeoutError)  # type: ignore
    assert not cancelled_calls
