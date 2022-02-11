from abc import abstractmethod
import abc
from typing import Optional, Type, Union
from koil.exceptions import KoilException
from koil.state import KoilState
from koil.task import KoilTask


class CheckerExpection(KoilException):
    pass


class BaseChecker(abc.ABC):
    pass

    def __init__(self, koil) -> None:
        self.koil = koil
        super().__init__()

    @abstractmethod
    def force_state(self) -> Optional[KoilState]:
        """Force State

        Please overwrite this method in your Checker. A Checkers
        purpose is to identify environment in which we choose
        different runstates for the koil.

        Your checker should either return None if it does identify
        a certain set of conditions for a certain desired state or
        should return the desired state

        Raises:
            CheckerExpection: An exception if there is a mismatch between a nesecarry state and the current environment we are in (e.g no desired library)

        Returns:
            Union[None, KoilState]: The derised State or None if no state is desired
        """

        raise CheckerExpection("Checker did not overwrite the proper function")
