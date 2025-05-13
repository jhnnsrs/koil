from typing import Callable, Literal, ParamSpec, TypeVar
from koil.qt import async_to_qt, async_gen_to_qt
from pytestqt.qtbot import QtBot # type: ignore

P = ParamSpec("P")
T = TypeVar("T")

def wait_for_qttask(qtbot: QtBot, task: async_to_qt[T, P], cause: Callable[[QtBot], None], timeout: int = 1000) -> T:
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
    error_out: BaseException | None = None
    out: T | Literal["KARLKALRKARLAKRLAKRLAKRLKAR"] = "KARLKALRKARLAKRLAKRLAKRLKAR"

    def callback(value:T) -> None:
        nonlocal out
        out = value
        
    def on_error(value: BaseException) -> None:
        nonlocal error_out
        error_out = value

    task.returned.connect(callback)
    task.errored.connect(on_error)

    with qtbot.waitSignal(task.returned, timeout=timeout) as blocker: # type: ignore
        blocker.connect(task.errored) # type: ignore
        cause(qtbot)

    if error_out:
        raise error_out
    
    if out == "KARLKALRKARLAKRLAKRLAKRLKAR":
        raise RuntimeError("Task did not return anything")
    
    return out


def wait_for_qtgenerator(qtbot: QtBot, task: async_gen_to_qt[T, P], cause: Callable[[QtBot], None], timeout: int = 1000) -> T:
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
    error_out: BaseException | None = None
    out: T | Literal["KARLKALRKARLAKRLAKRLAKRLKAR"] = "KARLKALRKARLAKRLAKRLAKRLKAR"
    done: bool = False

    def callback(value:T) -> None:
        nonlocal out
        out = value
        
    def on_error(value: BaseException) -> None:
        nonlocal error_out
        error_out = value
        
    def on_done() -> None:
        nonlocal done
        done = True

    task.yielded.connect(callback)
    task.errored.connect(on_error)
    task.done.connect(on_done) # type: ignore

    with qtbot.waitSignal(task.done, timeout=timeout) as blocker: # type: ignore
        blocker.connect(task.errored) # type: ignore
        cause(qtbot)

    if error_out:
        raise error_out
    
    if out == "KARLKALRKARLAKRLAKRLAKRLKAR":
        raise RuntimeError("Task did not return anything")
    
    return out
