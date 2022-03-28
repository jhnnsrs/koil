import asyncio
from pydantic import BaseModel, Field, root_validator
from pydantic.dataclasses import dataclass

from koil.decorators import koilable
from typing import Optional, Type, TypeVar
from koil.vars import *
from koil.errors import *
from koil.koil import *

T = TypeVar("T")


class PedanticKoil(BaseModel, KoilMixin):
    creating_instance: Optional[Any] = Field(default=None, exclude=True)
    running: bool = False
    name: str = "KoilLoop"
    uvify: bool = True
    grace_period: Optional[float] = None
    grant_sync = True

    _token = None
    _loop = None

    @root_validator()
    def check_not_running_in_koil(cls, values):
        if current_loop.get() is not None:
            raise ValueError(
                f"You are already running in a Koil Loop. You cannot run a Koil Loop inside another Koil Loop. {current_loop.get()}"
            )
        try:
            asyncio.get_running_loop()
            if not values["grant_sync"]:
                raise ValueError(
                    "Please use async instead. Or set Koil to grant_sync=True"
                )
        except RuntimeError:
            pass

        return values

    class Config:
        arbitrary_types_allowed = True
        underscore_attrs_are_private = True


@koilable(fieldname="koil", add_connectors=True, koil_class=PedanticKoil)
class KoiledModel(BaseModel):
    koil: Optional[PedanticKoil]

    def __enter__(self: T) -> T:
        ...

    def connect(self: T) -> T:
        ...

    async def aconnect(self: T) -> T:
        ...

    def disconnect(self: T):
        ...

    async def adisconnect(self: T):
        ...

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        ...

    async def __aenter__(self: T) -> T:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    class Config:
        arbitrary_types_allowed = True


class Composition(KoiledModel):
    async def __aenter__(self: T) -> T:
        for key, value in self:
            if isinstance(value, PedanticKoil):
                continue  # that was entered before
            if hasattr(value, "__aenter__"):
                await value.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        for key, value in self:
            if isinstance(value, PedanticKoil):
                continue  # that was entered before
            if hasattr(value, "__aexit__"):
                await value.__aexit__(exc_type, exc_val, exc_tb)

    def _repr_html_(self):
        return (
            "<div><p>App</p><table>"
            + "\n".join(["<tr><td>{}</td></tr>".format(key) for key, value in self])
            + "</table></div>"
        )
