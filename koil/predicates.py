from .vars import current_loop, current_process
import asyncio


def running_in_thread():
    """
    Check if the current code is running in a thread
    """


def running_in_same_loop():
    """
    Check if the current code is running in the same loop as the main thread
    """
    loop = current_loop.get()

    try:
        loop0 = asyncio.events.get_running_loop()

        if not loop:
            # No loop found but we are running in a loop, so probably the main loop
            # without a koiled loop
            return True

        if loop0 == loop:
            # We are running in the same loop as the main koiled loop, we can safely
            return True

        return False
    except RuntimeError:
        return False


def running_in_process():
    """
    Check if the current code is running in a process
    """
    return current_process.get() is not None


def koiled_loop_is_healthy():
    loop = current_loop.get()
    return loop.is_closed() is False and loop.is_running() is True
