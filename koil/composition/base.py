from types import TracebackType
from pydantic import BaseModel, ConfigDict, PrivateAttr
from koil.decorators import koilable
from typing import Any, Optional, Self, TypeVar
from koil.loop import Koil

T = TypeVar("T")


@koilable(fieldname="__koil", add_connectors=True)
class KoiledModel(BaseModel):
    __koil: Optional[Koil] = PrivateAttr(None)
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

    Children are entered in declaration order and exited in **reverse**
    order (later children may depend on earlier ones). If entering a child
    fails, the children already entered are exited (in reverse) before the
    error propagates; if a child's exit fails, the remaining children are
    still exited and the first exit error is re-raised afterwards.
    """

    async def __aenter__(self) -> Self:
        await super().__aenter__()
        entered: list[Any] = []
        for _, value in self:
            if isinstance(value, Koil):
                continue  # that was entered before
            if hasattr(value, "__aenter__"):
                try:
                    await value.__aenter__()
                except BaseException:
                    await self._aexit_children(entered, None, None, None)
                    raise
                entered.append(value)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await super().__aexit__(exc_type, exc_val, exc_tb)
        entered = [
            value
            for _, value in self
            if not isinstance(value, Koil) and hasattr(value, "__aexit__")
        ]
        await self._aexit_children(entered, exc_type, exc_val, exc_tb)

    @staticmethod
    async def _aexit_children(
        entered: "list[Any]",
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit *entered* children in reverse order, exiting every child even
        when one of them raises; the first error is re-raised at the end."""
        first_error: BaseException | None = None
        for value in reversed(entered):
            try:
                await value.__aexit__(exc_type, exc_val, exc_tb)
            except BaseException as e:
                if first_error is None:
                    first_error = e
        if first_error is not None:
            raise first_error
