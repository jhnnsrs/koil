from types import TracebackType
from koil.helpers import unkoil
from koil.koil import Koil, KoilProtocol
from koil.vars import global_koil
import inspect
from typing import Callable, Self, Type, TypeVar

import logging
from typing import Callable, Type, Protocol


class Koilable(Protocol):
    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None: ...


class SyncKoilable(Protocol):
    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None: ...


T = TypeVar("T", bound=Koilable)
KoilType = TypeVar("KoilType", bound=KoilProtocol)


logger = logging.getLogger(__name__)


def koilable(
    fieldname: str = "__koil",
    add_connectors: bool = False,
) -> Callable[[Type[T]], Type[T]]:
    """
    Decorator to make an async generator koilable.

    Args:
        fieldname (str, optional): The name of the field to store the koil instance. Defaults to "__koil".
        add_connectors (bool, optional): If True, it will add the connectors to the class. Defaults to False.
        init_koil (bool, optional): If True, it will initialize the koil instance on the field if it is None. Defaults to True.
        koil_class (Type[Koil], optional): The class of the koil to use. Defaults to Koil.

    """

    def real_cls_decorator(cls: Type[T]) -> Type[T]:
        assert hasattr(cls, "__aenter__"), "Class must implement __aenter__"
        assert hasattr(cls, "__aenter__"), "Class must implement __aexit__"
        assert inspect.iscoroutinefunction(cls.__aenter__), (
            "__aenter__ must be a coroutine"
        )
        assert inspect.iscoroutinefunction(cls.__aexit__), (
            "__aexit__ must be a coroutine (awaitable)"
        )
        setattr(cls, "__koilable__", True)

        def koiled_enter(self: Koilable):
            potential_koiled_loop = global_koil.get()
            if potential_koiled_loop is not None:
                # We are in a koiled loop no need to koil again
                return unkoil(self.__aenter__)
            else:
                if not hasattr(self, fieldname) or getattr(self, fieldname) is None:
                    setattr(self, fieldname, Koil())

                getattr(self, fieldname).__enter__()
                return unkoil(self.__aenter__)

        def koiled_exit(
            self: Koilable,
            exc_type: type[BaseException] | None,
            exc_val: BaseException | None,
            exc_tb: TracebackType | None,
        ):
            unkoil(self.__aexit__, exc_type, exc_val, exc_tb)
            koil = getattr(self, fieldname, None)
            if koil is not None:
                koil.__exit__(None, None, None)

        def koiled_param_exit(self: Koilable):
            koiled_exit(self, None, None, None)

        async def aexit(self: Koilable):
            return await self.__aexit__(None, None, None)

        async def aenter(self: Koilable):
            return await self.__aenter__()

        setattr(cls, "__enter__", koiled_enter)
        setattr(cls, "__exit__", koiled_exit)
        if add_connectors:
            setattr(cls, "aexit", aexit)
            setattr(cls, "aenter", aenter)
            setattr(cls, "exit", koiled_param_exit)
            setattr(cls, "enter", koiled_enter)

        return cls

    return real_cls_decorator
