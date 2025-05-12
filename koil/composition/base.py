from types import TracebackType
from pydantic import BaseModel, ConfigDict, PrivateAttr
from koil.decorators import koilable
from typing import Optional, Self, TypeVar
from koil.koil import Koil
import asyncio

T = TypeVar("T")


@koilable(fieldname="__koil", loop_field_name="__loop", add_connectors=True)
class KoiledModel(BaseModel):
    __koil: Optional[Koil] = PrivateAttr(None)
    __loop: Optional[asyncio.AbstractEventLoop] = PrivateAttr(None)
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    def __enter__(self: Self) -> Self: ...

    def enter(self: Self) -> Self: ...

    async def aenter(self: Self) -> Self: ...

    def exit(self: Self) -> None: ...

    async def aexit(self: Self) -> None: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None: ...

    async def __aenter__(self: Self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None: ...

    def __str__(self) -> str:
        return f"{self.__class__.__name__}"


class Composition(KoiledModel):
    """A composition of Koil models.

    A composition allows you to compose multiple Koil models into a single model.
    They will all be entered and exited together.

    This is useful for creating a single Koil model that can be used to
    manage multiple other Koil models, all models will share the same Koil instance.


    """

    async def __aenter__(self) -> Self:
        await super().__aenter__()
        for key, value in self:
            if isinstance(value, Koil):
                continue  # that was entered before
            if hasattr(value, "__aenter__"):
                await value.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await super().__aexit__(exc_type, exc_val, exc_tb)
        for _, value in self:
            if isinstance(value, Koil):
                continue  # that was entered before
            if hasattr(value, "__aexit__"):
                await value.__aexit__(exc_type, exc_val, exc_tb)
