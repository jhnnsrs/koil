from typing import Dict, Type
from koil.checker.base import BaseChecker
from koil.state import KoilState
import logging

logger = logging.getLogger(__name__)


class CheckerRegistry:
    def __init__(self) -> None:
        self.checkerClasses: Dict[str, Type[BaseChecker]] = {}
        self.checkers: Dict[str, BaseChecker] = {}

    def register_checker(self, checker_class):
        self.checkerClasses[checker_class.__name__] = checker_class

    def get_checkers(self, koil):
        if not self.checkers:
            self.checkers = {
                key: checker_class(koil)
                for key, checker_class in self.checkerClasses.items()
            }
        return self.checkers

    def get_desired_state(self, koil):
        self.desired_states = {
            key: checker.force_state()
            for key, checker in self.get_checkers(koil).items()
        }
        self.desired_none_states = [
            state for key, state in self.desired_states.items() if state != None
        ]
        if len(self.desired_none_states) == 0:
            return KoilState(
                threaded=False
            )  # We are not in an event loop so lets stay sync motherfucker
        else:
            if not len(self.desired_none_states) == 1:
                logger.info(
                    f"More than one state returned from our checkers. This is not going to work {self.desired_none_states}"
                )
            return self.desired_none_states[0]


def register_checker(
    overwrite: bool = False,
    registry: CheckerRegistry = None,
):
    """Registers a Checker

    Registers a Structure with the a Package Registry. Once registered Arkitekt will use this Structure to expand and shrink Node requests.

    Args:
        identifier (str, optional): A unique identifier to be known be. Defaults to None.
        overwrite (bool, optional): If overwrite is set this Structure will replace exisiting Structures, otherwise we return an error on Reigstration
        registry (PackerRegistry, optional): Which registry to use in order to register this Strucute. Will use the default Registry if not set
    """

    def real_decorator(cls):
        (registry or get_checker_registry()).register_checker(cls)
        return cls

    return real_decorator


CHECKER_REGISTRY = None


def get_checker_registry(register_defaults=True):
    global CHECKER_REGISTRY
    if not CHECKER_REGISTRY:
        CHECKER_REGISTRY = CheckerRegistry()
        if register_defaults:
            import koil.checker.defaults

    return CHECKER_REGISTRY
