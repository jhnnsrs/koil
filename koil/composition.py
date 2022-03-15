from pydantic import BaseModel

from koil.decorators import koilable
from typing import Optional, TypeVar

from koil.koil import Koil


T = TypeVar("T")


@koilable(fieldname="koil", add_connectors=True)
class Composition(BaseModel):
    koil: Optional[Koil] = None

    async def __aenter__(self: T) -> T:
        for key, value in self:
            if isinstance(value, Koil):
                continue  # that was entered before
            if hasattr(value, "__aenter__"):
                await value.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        for key, value in self:
            if isinstance(value, Koil):
                continue  # that was entered before
            if hasattr(value, "__aexit__"):
                await value.__aexit__(exc_type, exc_val, exc_tb)

    def __enter__(self: T) -> T:
        ...

    class Config:
        arbitrary_types_allowed = True
