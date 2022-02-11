from typing import Optional, Type, Union
from koil.checker.base import BaseChecker
from koil.checker.registry import register_checker
from koil.state import KoilState
from koil.task import KoilTask


class JupyterKoilState(KoilState):
    def __init__(self, is_terminal=False, **kwargs) -> None:
        super().__init__(**kwargs)
        self.is_terminal = is_terminal

    def get_task_class(self) -> Type[KoilTask]:
        return KoilTask


@register_checker()
class JupyterChecker(BaseChecker):
    def force_state(self) -> Optional[KoilState]:
        """Checks if a running Qt Instance is there, if so we would like
        to run in a seperate thread

        Returns:
            Union[None, KoilState]: [description]
        """

        try:
            from IPython import get_ipython

            shell = get_ipython().__class__.__name__
            if shell == "ZMQInteractiveShell":
                return JupyterKoilState(threaded=True)  # Jupyter notebook or qtconsole
            elif shell == "TerminalInteractiveShell":
                return JupyterKoilState(
                    threaded=True, is_terminal=True
                )  # Terminal running IPython
            else:
                return None  # Other type (?)
        except NameError:
            return None
        except ImportError:
            return None
