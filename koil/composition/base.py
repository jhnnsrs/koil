from pydantic import BaseModel, Field, ConfigDict, PrivateAttr
from koil.decorators import koilable
from typing import Optional, TypeVar, Any
from koil.koil import KoilMixin

T = TypeVar("T")


class PedanticKoil(BaseModel, KoilMixin):
    model_config: ConfigDict = ConfigDict(arbitrary_types_allowed=True, extra="forbid", )
    creating_instance: Optional[Any] = Field(default=None, exclude=True)
    running: bool = False
    name: str = "KoilLoop"
    uvify: bool = True
    grace_period: Optional[float] = None
    grant_sync: bool = True
    sync_in_async: bool = False

    _token = PrivateAttr(None)
    _loop = PrivateAttr(None)

    def _repr_html_inline_(self):
        return f"<table><tr><td>allow sync in async</td><td>{self.sync_in_async}</td></tr><tr><td>uvified</td><td>{self.uvify}</td></tr></table>"
    



@koilable(fieldname="koil", add_connectors=True, koil_class=PedanticKoil)
class KoiledModel(BaseModel):
    koil: PedanticKoil = Field(default_factory=PedanticKoil, exclude=True)
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    def __enter__(self: T) -> T: ...

    def enter(self: T) -> T: ...

    async def aenter(self: T) -> T: ...

    def exit(self: T): ...

    async def aexit(self: T): ...

    def __exit__(self, exc_type, exc_val, exc_tb) -> None: ...

    async def __aenter__(self: T) -> T:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


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
