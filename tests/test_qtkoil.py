import asyncio
from PyQt5 import QtWidgets, QtCore
from koil.qt import QtCoro, QtFuture, QtGenerator, QtGeneratorRunner, QtKoil, QtRunner
import contextvars

x = contextvars.ContextVar("x")


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
        yield i


class KoiledWidget(QtWidgets.QWidget):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.koil = QtKoil(parent=self)
        self.koil.enter()

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
        self.koil = QtKoil(parent=self)
        self.koil.enter()

        self.call_task_button = QtWidgets.QPushButton("Call Task")
        self.call_gen_button = QtWidgets.QPushButton("Call Generator")
        self.call_raise_button = QtWidgets.QPushButton("Call Raise")
        self.call_context_button = QtWidgets.QPushButton("Call Context")

        self.sleep_and_resolve_task = QtRunner(sleep_and_resolve)
        self.sleep_and_resolve_task.returned.connect(self.task_finished)

        self.sleep_and_use_context_task = QtRunner(sleep_and_use_context)
        self.sleep_and_use_context_task.returned.connect(self.task_finished)

        self.sleep_and_yield_task = QtGeneratorRunner(sleep_and_yield)
        self.sleep_and_yield_task.yielded.connect(self.task_finished)

        self.sleep_and_raise_task = QtRunner(sleep_and_raise)
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
        self.sleep_and_use_context_task.run()

    def call_raise(self):
        self.sleep_and_raise_task.run()

    def task_finished(self, int):
        self.value = int
        self.greet_label.setText("Hello!")


class KoiledInterferingFutureWidget(QtWidgets.QWidget):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.koil = QtKoil(parent=self)
        self.koil.enter()

        self.do_me = QtCoro(self.in_qt_task)

        self.my_coro_task = QtRunner(self.call_coro)
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

    def in_qt_task(self, future: QtFuture):
        self.task_was_run = True
        future.resolve("called")

    def call_task(self):
        self.my_coro_task.run()

    def task_finished(self):
        self.greet_label.setText("Hello!")

    async def call_coro(self):
        x = await self.do_me.acall()
        self.coroutine_was_run = True


class KoiledGeneratorWidget(QtWidgets.QWidget):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.koil = QtKoil(parent=self)
        self.koil.enter()

        self.my_coro_task = QtRunner(self.call_coro)
        self.my_coro_task.returned.connect(self.task_finished)

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

    def in_qt_task(self, future: QtFuture):
        self.task_was_run = True
        future.resolve("called")

    def call_task(self):
        self.my_coro_task.run()

    def task_finished(self):
        self.greet_label.setText("Hello!")

    async def call_coro(self):
        self.qt_generator = QtGenerator()

        async for x in self.qt_generator:
            self.coroutine_was_run = True

        self.coroutine_finished = True

        x = await self.do_me.acall()
        self.coroutine_was_run = True
        print("nana")


def test_koil_qt_no_interference(qtbot):
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


def test_koil_qt_call_task(qtbot):
    """Tests if we can call a task from a koil widget."""
    widget = KoiledInterferingWidget()
    qtbot.addWidget(widget)

    # click in the Greet button and make sure it updates the appropriate label
    with qtbot.waitSignal(widget.sleep_and_resolve_task.returned) as b:
        qtbot.mouseClick(widget.call_task_button, QtCore.Qt.LeftButton)


def test_call_gen(qtbot):
    """Tests if we can call a task from a koil widget."""
    widget = KoiledInterferingWidget()
    qtbot.addWidget(widget)

    # click in the Greet button and make sure it updates the appropriate label
    with qtbot.waitSignal(widget.sleep_and_yield_task.yielded, timeout=1000) as b:

        qtbot.mouseClick(widget.call_gen_button, QtCore.Qt.LeftButton)


def test_call_future(qtbot):
    """Tests if we can call a task from a koil widget."""
    widget = KoiledInterferingFutureWidget()
    qtbot.addWidget(widget)

    # click in the Greet button and make sure it updates the appropriate label
    with qtbot.waitSignal(widget.my_coro_task.returned, timeout=1000):

        qtbot.mouseClick(widget.call_task_button, QtCore.Qt.LeftButton)

    assert widget.task_was_run == True
    assert widget.coroutine_was_run == True


def test_call_raise(qtbot):
    """Tests if we can call a task from a koil widget."""
    widget = KoiledInterferingWidget()
    qtbot.addWidget(widget)

    # click in the Greet button and make sure it updates the appropriate label

    with qtbot.waitSignal(widget.sleep_and_raise_task.errored, timeout=1000) as b:
        qtbot.mouseClick(widget.call_raise_button, QtCore.Qt.LeftButton)

    assert isinstance(b.args[0], Exception)


def test_context(qtbot):
    """Tests if we can call a task from a koil widget."""
    widget = KoiledInterferingWidget()
    qtbot.addWidget(widget)

    x.set(5)
    # click in the Greet button and make sure it updates the appropriate label

    with qtbot.waitSignal(
        widget.sleep_and_use_context_task.returned, timeout=1000
    ) as b:
        qtbot.mouseClick(widget.call_context_button, QtCore.Qt.LeftButton)

    assert b.args[0] == 6
