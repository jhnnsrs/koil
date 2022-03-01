import asyncio
import threading
import logging

from pkg_resources import load_entry_point


logger = logging.getLogger(__name__)


def unkoil(function):
    def koiled_function(*args, **kwargs):
        return koil(function(*args, **kwargs))

    return koiled_function


def koil(func, *args, as_task=False, timeout=None, **kwargs):
    from koil.koil import get_current_koil

    koil = get_current_koil()

    if as_task:
        return koil.task(func, *args, **kwargs, timeout=timeout)
    else:
        return koil.run(func, *args, **kwargs, timeout=timeout)


def koil_gen(func, *args, timeout=None, **kwargs):
    """Takes a Async Generator and iterates over it
    threadsafe in the herre loop providing a syncrhonus
    generator or returns an async generator in the current
    loop

    Args:
        potential_generator ([type]): [description]

    Returns:
        [type]: [description]
    """
    from koil.koil import get_current_koil

    koil = get_current_koil()

    return koil.iterate(func, *args, **kwargs, timeout=timeout)
