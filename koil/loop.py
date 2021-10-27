
import asyncio
import threading
import logging


logger = logging.getLogger(__name__)


def unkoil(function):

    def koiled_function(*args, **kwargs):
        return koil(function(*args, **kwargs))

    return koiled_function


def koil(potential_future, as_task=False, block=False, koil= None):
    """Taks an Async Function and according
    to the current loop setting either returns over
    it synchronusly in the herre loop or returns
    a future

    Args:
        potential_future ([type]): [description]

    Returns:
        [type]: [description]
    """
    from koil.koil import get_current_koil

    koil = koil or get_current_koil()
    state = koil.state
    loop = koil.loop


    if loop.is_running():
        # Loop is running
        logger.debug("Loop is Running")
        if loop._thread_id == threading.current_thread().ident: 
            logger.debug(f"Same thread... Returning Future {potential_future}")
            # Loop is running in same thread, always returning future here (in an ansyc context you are the master)
            return potential_future
        # We are in a threaded context our state is importat
        if not block and as_task:
            return koil.state.get_task_class()(future=potential_future, koil=koil)

        return asyncio.run_coroutine_threadsafe(potential_future, loop).result()
    
    # loop is not running and we have no threaded loop, just returning the futures

    return loop.run_until_complete(potential_future)



def next_on_gen(potential_generator, loop):
    """Takes a Async Generator and iterates over it
    threadsafe in the provided loop and returns the result
    synchronously in another generator

    Args:
        potential_generator ([type]): [description]
        loop ([type]): [description]

    Returns:
        [type]: [description]

    Yields:
        [type]: [description]
    """
    ait = potential_generator.__aiter__()

    async def next_on_ait():
        try:
            obj = await ait.__anext__()
            return False, obj
        except StopAsyncIteration:
            return True, None
        
    while True:
        done, obj = asyncio.run_coroutine_threadsafe(next_on_ait(), loop).result()
        if done: 
            break
        yield obj

def next_on_gen_not_running(potential_generator, loop):
    """Takes a Async Generator and iterates over it
    threadsafe in the provided loop and returns the result
    synchronously in another generator

    Args:
        potential_generator ([type]): [description]
        loop ([type]): [description]

    Returns:
        [type]: [description]

    Yields:
        [type]: [description]
    """
    ait = potential_generator.__aiter__()

    async def next_on_ait():
        try:
            obj = await ait.__anext__()
            return False, obj
        except StopAsyncIteration:
            return True, None
        
    while True:
        done, obj = loop.run_until_complete(next_on_ait())
        if done: 
            break
        yield obj


def koil_gen(potential_generator, koil = None):
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
    
    koil = koil or get_current_koil()
    loop = koil.loop

    if loop.is_running():
        if loop._thread_id == threading.current_thread().ident: return potential_generator
        return next_on_gen(potential_generator, loop)


    return next_on_gen_not_running(potential_generator, loop)



