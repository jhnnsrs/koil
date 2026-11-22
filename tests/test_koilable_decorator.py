import asyncio
import inspect

import pytest

from koil.bridge import unkoil
from koil.context import global_koil
from koil.decorators import koilable
from koil.loop import Koil

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@koilable(fieldname="_custom_koil")
class CustomFieldModel:
    def __init__(self) -> None:
        self._custom_koil = None

    async def __aenter__(self) -> "CustomFieldModel":
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    async def _value(self) -> int:
        await asyncio.sleep(0)
        return 42

    def get_value(self) -> int:
        return unkoil(self._value)


@koilable(add_connectors=True)
class ConnectedModel:
    async def __aenter__(self) -> "ConnectedModel":
        return self

    async def __aexit__(self, *args: object) -> None:
        pass


@koilable()
class TrackingModel:
    entered = False

    async def __aenter__(self) -> "TrackingModel":
        self.entered = True
        return self

    async def __aexit__(self, *args: object) -> None:
        self.entered = False


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_koilable_custom_fieldname_works() -> None:
    """A custom fieldname stores the Koil instance on the named attribute."""
    m = CustomFieldModel()
    with m:
        assert m.get_value() == 42
    # Koil was stored on _custom_koil, not the default __koil
    assert hasattr(m, "_custom_koil")
    assert m._custom_koil is not None


def test_koilable_add_connectors_attaches_methods() -> None:
    """add_connectors=True adds enter/exit/aenter/aexit helpers to the class."""
    m = ConnectedModel()
    assert callable(getattr(m, "enter", None))
    assert callable(getattr(m, "exit", None))
    assert callable(getattr(m, "aenter", None))
    assert callable(getattr(m, "aexit", None))


async def test_koilable_add_connectors_aenter_aexit_work() -> None:
    """The aenter/aexit connectors work as async context-manager equivalents."""
    m = ConnectedModel()
    await m.aenter()
    await m.aexit()


def test_koilable_requires_coroutine_aenter() -> None:
    """Decorating a class whose __aenter__ is a plain function raises TypeError."""
    with pytest.raises(TypeError, match="__aenter__ must be a coroutine"):

        @koilable()
        class BadAenter:
            def __aenter__(self) -> "BadAenter":  # not async
                return self

            async def __aexit__(self, *args: object) -> None:
                pass


def test_koilable_requires_coroutine_aexit() -> None:
    """Decorating a class whose __aexit__ is a plain function raises TypeError."""
    with pytest.raises(TypeError, match="__aexit__ must be a coroutine"):

        @koilable()
        class BadAexit:
            async def __aenter__(self) -> "BadAexit":
                return self

            def __aexit__(self, *args: object) -> None:  # not async
                pass


def test_koilable_requires_aexit_present() -> None:
    """Decorating a class with no __aexit__ at all raises a pointed TypeError."""
    with pytest.raises(TypeError, match="must implement __aexit__"):

        @koilable()
        class MissingAexit:
            async def __aenter__(self) -> "MissingAexit":
                return self


def test_koilable_reuses_existing_koil() -> None:
    """Inside an existing Koil context, @koilable does not create a new inner Koil."""
    m = TrackingModel()
    with Koil():
        with m:
            assert m.entered
            # No inner Koil was created — the outer loop was reused
            assert getattr(m, "__koil", None) is None
    assert not m.entered


def test_koilable_standalone_creates_koil() -> None:
    """Outside any Koil context, @koilable transparently creates its own Koil."""
    m = TrackingModel()
    with m:
        assert m.entered
        # The decorator created a Koil and stored it under __koil
        assert getattr(m, "__koil", None) is not None
    assert not m.entered


def test_koilable_bare_form() -> None:
    """@koilable without parentheses decorates the class directly."""

    @koilable
    class BareModel:
        entered = False

        async def __aenter__(self) -> "BareModel":
            self.entered = True
            return self

        async def __aexit__(self, *args: object) -> None:
            self.entered = False

    assert inspect.isclass(BareModel)
    m = BareModel()
    with m:
        assert m.entered
        assert getattr(m, "__koil", None) is not None
    assert not m.entered


def test_koilable_legacy_positional_fieldname() -> None:
    """koilable("_field") keeps the legacy positional-fieldname contract."""

    @koilable("_legacy_koil")
    class LegacyModel:
        async def __aenter__(self) -> "LegacyModel":
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

    m = LegacyModel()
    with m:
        assert getattr(m, "_legacy_koil", None) is not None


def test_koilable_enter_failure_tears_down_koil() -> None:
    """A failing __aenter__ must not leak the auto-started Koil's loop thread."""

    @koilable()
    class FailsOnEnter:
        async def __aenter__(self) -> "FailsOnEnter":
            raise ValueError("boom")

        async def __aexit__(self, *args: object) -> None:
            pass

    m = FailsOnEnter()
    with pytest.raises(ValueError, match="boom"):
        with m:
            pass

    assert global_koil.get() is None
    auto_koil = getattr(m, "__koil", None)
    assert auto_koil is not None
    assert auto_koil.running is False
