import logging
from types import TracebackType
from pydantic import BaseModel, ConfigDict, PrivateAttr
from koil.decorators import koilable
from typing import Any, Self, TypeVar
from koil.loop import Koil

T = TypeVar("T")

logger = logging.getLogger(__name__)

# Where @koilable keeps an instance's Koil and its enter bookkeeping. They
# describe one object's running state and must not travel to a copy of it.
_KOIL_STATE = ("__koil", "__koil_owned")


@koilable(fieldname="__koil", add_connectors=True)
class KoiledModel(BaseModel):
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

    def __copy__(self: Self) -> Self:
        # A copy is a new, not-yet-entered object: it starts its own Koil
        # rather than sharing (and later stopping) the original's loop.
        copied = super().__copy__()
        for key in _KOIL_STATE:
            copied.__dict__.pop(key, None)
        return copied

    def __deepcopy__(self: Self, memo: dict[int, Any] | None = None) -> Self:
        # The Koil holds a thread and a loop, neither of which can be deep
        # copied; step around it and leave the copy without one.
        state = {key: self.__dict__.pop(key) for key in _KOIL_STATE if key in self.__dict__}
        try:
            return super().__deepcopy__(memo)
        finally:
            self.__dict__.update(state)

    def __str__(self) -> str:
        return f"{self.__class__.__name__}"


class Composition(KoiledModel):
    """A composition of Koil models.

    A composition allows you to compose multiple Koil models into a single model.
    They will all be entered and exited together.

    This is useful for creating a single Koil model that can be used to
    manage multiple other Koil models, all models will share the same Koil instance.

    Members are entered in field order and exited in reverse. If a member
    fails to enter, the ones entered before it are exited again; if one fails
    to exit, the remaining ones are still exited before the error is raised.
    """

    _entered: list[Any] | None = PrivateAttr(default=None)

    def _members(self) -> list[Any]:
        return [
            value
            for _, value in self
            if not isinstance(value, Koil)  # that was entered before
        ]

    async def __aenter__(self) -> Self:
        await super().__aenter__()
        entered: list[Any] = []
        try:
            for value in self._members():
                if hasattr(value, "__aenter__"):
                    await value.__aenter__()
                    entered.append(value)
        except BaseException as e:
            # __aexit__ will not run for a block that was never entered, so
            # the members that did enter are ours to unwind.
            try:
                await self._exit_members(entered, type(e), e, e.__traceback__)
            except Exception:
                logger.warning(
                    "Exiting %s after a failed enter raised", self, exc_info=True
                )
            raise
        self._entered = entered
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await super().__aexit__(exc_type, exc_val, exc_tb)
        entered, self._entered = self._entered, None
        if entered is None:
            # Entered by a subclass that does not go through our __aenter__.
            entered = [v for v in self._members() if hasattr(v, "__aexit__")]
        await self._exit_members(entered, exc_type, exc_val, exc_tb)

    async def _exit_members(
        self,
        members: list[Any],
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit *members* last-entered first; every one gets its turn."""
        first_error: BaseException | None = None
        for value in reversed(members):
            if not hasattr(value, "__aexit__"):
                continue
            try:
                await value.__aexit__(exc_type, exc_val, exc_tb)
            except BaseException as e:
                if first_error is None:
                    first_error = e
                else:
                    logger.warning("Exiting %r raised as well", value, exc_info=True)
        if first_error is not None:
            raise first_error
