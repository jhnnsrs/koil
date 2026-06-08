import asyncio
import inspect
import pytest

from koil.decorators import koilable
from koil.bridge import unkoil
from koil.loop import Koil
from koil.context import global_koil


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@koilable(fieldname="_custom_koil")
class CustomFieldModel:
    def __init__(self):
        self._custom_koil = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def _value(self):
        await asyncio.sleep(0)
        return 42

    def get_value(self):
        return unkoil(self._value)


@koilable(add_connectors=True)
class ConnectedModel:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


@koilable()
class TrackingModel:
    entered = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *args):
        self.entered = False


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_koilable_custom_fieldname_works():
    """A custom fieldname stores the Koil instance on the named attribute."""
    m = CustomFieldModel()
    with m:
        assert m.get_value() == 42
    # Koil was stored on _custom_koil, not the default __koil
    assert hasattr(m, "_custom_koil")
    assert m._custom_koil is not None


def test_koilable_add_connectors_attaches_methods():
    """add_connectors=True adds enter/exit/aenter/aexit helpers to the class."""
    m = ConnectedModel()
    assert callable(getattr(m, "enter", None))
    assert callable(getattr(m, "exit", None))
    assert callable(getattr(m, "aenter", None))
    assert callable(getattr(m, "aexit", None))


async def test_koilable_add_connectors_aenter_aexit_work():
    """The aenter/aexit connectors work as async context-manager equivalents."""
    m = ConnectedModel()
    await m.aenter()
    await m.aexit()


def test_koilable_requires_coroutine_aenter():
    """Decorating a class whose __aenter__ is a plain function raises AssertionError."""
    with pytest.raises(AssertionError):

        @koilable()
        class BadAenter:
            def __aenter__(self):  # not async
                return self

            async def __aexit__(self, *args):
                pass


def test_koilable_requires_coroutine_aexit():
    """Decorating a class whose __aexit__ is a plain function raises AssertionError."""
    with pytest.raises(AssertionError):

        @koilable()
        class BadAexit:
            async def __aenter__(self):
                return self

            def __aexit__(self, *args):  # not async
                pass


def test_koilable_reuses_existing_koil():
    """Inside an existing Koil context, @koilable does not create a new inner Koil."""
    m = TrackingModel()
    with Koil() as outer:
        with m:
            assert m.entered
            # No inner Koil was created — the outer loop was reused
            assert getattr(m, "__koil", None) is None
    assert not m.entered


def test_koilable_standalone_creates_koil():
    """Outside any Koil context, @koilable transparently creates its own Koil."""
    m = TrackingModel()
    with m:
        assert m.entered
        # The decorator created a Koil and stored it under __koil
        assert getattr(m, "__koil", None) is not None
    assert not m.entered
