from koil.helpers import unkoil
from koil.koil import Koil
import inspect
from typing import Callable, Type, TypeVar

T = TypeVar("T")


def koilable(
    fieldname: str = "__koil", add_connectors: bool = False, **koilparams
) -> Callable[[Type[T]], Type[T]]:
    """
    Decorator to make an async generator koilable.

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
        ), "__aexit__ must be a coroutine"

        def koiled_enter(self, *args, **kwargs):
            setattr(self, fieldname, Koil(**koilparams))
            getattr(self, fieldname).__enter__()
            return unkoil(self.__aenter__, *args, **kwargs)

        def koiled_exit(self, *args, **kwargs):
            unkoil(self.__aexit__, *args, **kwargs)
            getattr(self, fieldname).__exit__(*args, **kwargs)
            setattr(self, fieldname, None)

        def disconnect(self):
            unkoil(self.__aexit__, None, None, None)
            getattr(self, fieldname).__exit__(None, None, None)
            setattr(self, fieldname, None)

        cls.__enter__ = koiled_enter
        cls.__exit__ = koiled_exit
        if add_connectors:
            cls.disconnect = disconnect
            cls.connect = koiled_enter

        return cls

    return real_cls_decorator


def unkoilable(func):
    def wrapper(*args, **kwargs):
        return unkoil(func, *args, **kwargs)

    return wrapper
