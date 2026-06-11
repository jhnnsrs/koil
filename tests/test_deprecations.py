"""The legacy shim modules warn on access and hand back the real objects."""

import pytest


def test_helpers_unkoil_warns_and_resolves():
    import koil.bridge
    import koil.helpers

    with pytest.warns(DeprecationWarning, match="koil.helpers.unkoil is deprecated"):
        assert koil.helpers.unkoil is koil.bridge.unkoil


def test_helpers_run_spawned_points_at_run_threaded():
    import koil.bridge
    import koil.helpers

    with pytest.warns(DeprecationWarning, match="renamed"):
        assert koil.helpers.run_spawned is koil.bridge.run_threaded


def test_helpers_iterate_spawned_points_at_iterate_threaded():
    import koil.bridge
    import koil.helpers

    with pytest.warns(DeprecationWarning, match="renamed"):
        assert koil.helpers.iterate_spawned is koil.bridge.iterate_threaded


def test_from_import_triggers_warning():
    with pytest.warns(DeprecationWarning):
        from koil.helpers import sleep  # noqa: F401


def test_vars_shim_warns_and_resolves():
    import koil.context
    import koil.vars

    with pytest.warns(DeprecationWarning, match="koil.vars.global_koil is deprecated"):
        assert koil.vars.global_koil is koil.context.global_koil


def test_koil_shim_warns_and_resolves():
    import koil.koil
    import koil.loop

    with pytest.warns(DeprecationWarning, match="koil.koil.Koil is deprecated"):
        assert koil.koil.Koil is koil.loop.Koil


def test_shim_unknown_attribute_raises_attribute_error():
    import koil.helpers

    with pytest.raises(AttributeError):
        koil.helpers.does_not_exist
