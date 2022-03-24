import asyncio
from pydantic import BaseModel, Field, root_validator

from koil.decorators import koilable
from typing import Optional, Type, TypeVar
from koil.task import KoilGeneratorTask, KoilTask
from koil.vars import *
from koil.errors import *
from koil.koil import *

T = TypeVar("T")


class PedanticKoil(BaseModel):
    name: str = "KoilLoop"
    uvify: bool = True
    grace_period: Optional[float] = None
    task_class: Optional[Type[KoilTask]] = Field(default=KoilTask, exclude=True)
    gen_class: Optional[Type[KoilGeneratorTask]] = Field(
        default=KoilGeneratorTask, exclude=True
    )
    grant_sync = True

    _entered_loop: asyncio.BaseEventLoop = None
    _old_loop: asyncio.BaseEventLoop = None
    _old_taskclass: Type[KoilTask] = None
    _old_genclass: Type[KoilGeneratorTask] = None

    @root_validator()
    def check_not_running_in_loop(cls, values):
        if current_loop.get() is not None:
            raise ValueError(
                "You are already running in a Koil Loop. You cannot run a Koil Loop inside another Koil Loop."
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

    def connect(self):
        return self.__enter__()

    async def aconnect(self):
        return self.__aenter__()

    def disconnect(self):
        return self.__exit__(None, None, None)

    async def adisconnect(self):
        return self.__aexit__(None, None, None)

    async def __aenter__(self):
        self._old_loop = current_loop.get()
        loop = asyncio.get_event_loop()
        current_loop.set(loop)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        current_loop.set(self._old_loop)

    def __enter__(self):
        try:
            asyncio.get_running_loop()
            if not self.grant_sync:
                raise ContextError(
                    "You are running in an event loop already. Using koil makes no sense here, use asyncio instead. If this happens in a context manager, you probably forgot to use the `async with` syntax."
                )
        except RuntimeError:
            pass

        self._old_loop = current_loop.get()
        self._old_taskclass = current_taskclass.get()
        self._old_genclass = current_genclass.get()

        current_taskclass.set(
            self.task_class or self._old_taskclass or KoilTask
        )  # task classes can be overwriten, as they only apply to the context
        current_genclass.set(
            self.gen_class or self._old_genclass or KoilGeneratorTask
        )  # task classes can be overwriten, as they only apply to the context
        if self._old_loop is not None:
            # already runnning with a koiled loop, we will just attach to it
            return self

        self._entered_loop = get_threaded_loop(self.name, uvify=self.uvify)
        current_loop.set(self._entered_loop)
        return self

    async def aclose(self):
        loop = asyncio.get_event_loop()
        logger.debug("Causing loop to stop")
        loop.stop()

    def __exit__(self, exc_type, exc_val, exc_tb):

        if self._entered_loop is not None:
            if self._entered_loop.is_running():
                asyncio.run_coroutine_threadsafe(self.aclose(), self._entered_loop)

                iterations = 0

                while self._entered_loop.is_running():
                    time.sleep(0.001)
                    iterations += 1
                    if iterations == 100:
                        logger.warning(
                            "Shutting Down takes longer than expected. Probably we are having loose Threads? Keyboard interrupt?"
                        )

                current_loop.set(self._old_loop)  # Reset the loop

        current_taskclass.set(self._old_taskclass)
        current_taskclass.set(self._old_genclass)

    class Config:
        arbitraty_types_allowed = True
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
