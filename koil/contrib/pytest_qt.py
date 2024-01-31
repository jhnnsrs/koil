from typing import Callable
from koil.qt import QtRunner
from pytestqt.qtbot import QtBot


def wait_for_qttask(qtbot: QtBot, task: QtRunner, cause: Callable[[QtBot], None]):
    """A helper function that waits for a QtRunner task to finish.

    This function will utilize the pytest-qt qtbot fixture to wait for a QtRunner
      task to finish.
    It will return the value of the task, or raise an exception if the task errored.

    Parameters
    ----------
    qtbot : QtBot
        The qtbot fixture from pytest-qt
    task : QtRunner
        The task to wait for
    cause : Callable[[QtBot], None]
        A function that will cause the task to run

    Raises
    ------
    Exception
        The exception that the task raised

    Returns
    -------
    Any
        The value that the task returned



    """
    out = ("MAGIC_NOTHINGNESS_WORD",)

    def callback(*value):
        nonlocal out
        out = value

    task.returned.connect(callback)
    task.errored.connect(callback)

    with qtbot.waitSignal(task.returned) as blocker:
        blocker.connect(task.errored)
        cause(qtbot)

    if len(out) == 0:
        raise RuntimeError("Task did not return a value.")

    if isinstance(out[0], Exception):
        raise out[0]
    elif out[0] == "MAGIC_NOTHINGNESS_WORD":
        raise RuntimeError("Task did not return a value.")
    else:
        if len(out) == 1:
            return out[0]
        return out
