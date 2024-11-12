from koil.helpers import unkoil, unkoil_gen
from koil.koil import Koil
import inspect
from typing import Callable, Type, TypeVar
from koil.vars import current_loop
from koil.errors import KoilError
import logging
from .utils import check_is_asyncfunc, check_is_asyncgen

T = TypeVar("T")


logger = logging.getLogger(__name__)


def koilable(
    fieldname: str = "__koil",
    add_connectors: bool = False,
    init_koil: bool = True,
    koil_class: Type[Koil] = Koil,
    **koilparams,
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
        cls.__koilable__ = True
        assert hasattr(cls, "__aenter__"), "Class must implement __aenter__"
        assert hasattr(cls, "__aenter__"), "Class must implement __aexit__"
        assert inspect.iscoroutinefunction(
            cls.__aenter__
        ), "__aenter__ must be a coroutine"
        assert inspect.iscoroutinefunction(
            cls.__aexit__
        ), "__aexit__ must be a coroutine (awaitable)"

        def koiled_enter(self, *args, **kwargs):
            potential_koiled_loop = current_loop.get()
            if potential_koiled_loop is not None:
                # We are in a koiled loop no need to koil again
                return unkoil(self.__aenter__, *args, **kwargs)
            else:
                if not hasattr(self, fieldname) or getattr(self, fieldname) is None:
                    if init_koil:
                        setattr(
                            self,
                            fieldname,
                            koil_class(name=f"{repr(self)}", **koilparams),
                        )
                    else:
                        raise KoilError(
                            f"Does not have a koil instance on {fieldname} and init_koil is False"
                        )

                getattr(self, fieldname).__enter__()
                return unkoil(self.__aenter__, *args, **kwargs)

        def koiled_exit(self, *args, **kwargs):
            unkoil(self.__aexit__, *args, **kwargs)
            koil = getattr(self, fieldname, None)
            if koil is not None:
                koil.__exit__(None, None, None)

        def koiled_param_exit(self):
            koiled_exit(self, None, None, None)

        async def aexit(self):
            return await self.__aexit__(None, None, None)

        async def aenter(self):
            return await self.__aenter__()

        cls.__enter__ = koiled_enter
        cls.__exit__ = koiled_exit
        if add_connectors:
            cls.aexit = aexit
            cls.aenter = aenter
            cls.exit = koiled_param_exit
            cls.enter = koiled_enter

        return cls

    return real_cls_decorator


def unkoilable(func):
    """Decorator to make a function unkoilable

    Args:
        func (Callable): The function to run in the koil loop

    Raises:
        TypeError: If the function is not a coroutine

    """

    if not check_is_asyncgen(func) and not check_is_asyncfunc(func):
        raise TypeError("Function must be a coroutine")

    if check_is_asyncgen(func):

        def wrapper(*args, **kwargs):
            return unkoil_gen(func, *args, **kwargs)

        return wrapper

    else:

        def wrapper(*args, **kwargs):
            return unkoil(func, *args, **kwargs)

        return wrapper
