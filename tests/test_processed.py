import asyncio

import pytest
from koil.errors import ContextError

from koil.helpers import unkoil, unkoil_gen, run_processed, iterate_processed
from .context import AsyncContextManager
from koil import Koil


async def sleep(ins):
    await asyncio.sleep(0.001)
    return ins

async def sleep_and_raise(ins):
    await asyncio.sleep(0.001)
    raise Exception("This is a sleep and raise exception")


async def iterating():
    yield 1
    await asyncio.sleep(0.001)
    yield 2
    await asyncio.sleep(0.001)
    yield 3

async def iterate_and_raise():
    yield 1
    raise Exception("This is an iterate and raise exception")

def test_sync_context():

    with AsyncContextManager() as c:
        print(c.aprint())


async def test_async_context():
    async with AsyncContextManager() as c:
        print(await c.aprint())


def test_sync():

    with Koil():
        assert unkoil(sleep, 1) == 1, "Koil realized its async and was okay with that"



def process_func(arg: int, number: int):
    return arg + number


def raising_process_func(arg: int, number: int):
    raise Exception("This is a test exception")
    

def back_calling_func(arg: int, number: int):
    return unkoil(sleep, arg + number)


def back_calling_raising_func(arg: int, number: int):
    return unkoil(sleep_and_raise, arg + number)






async def test_spawn_process_func():
    async with Koil():
        assert await run_processed(process_func, 1, number=2) == 3, "Process should run and return 3"


async def test_spawn_process_exception_func():
    async with Koil():
        with pytest.raises(Exception, match="This is a test exception"):
            assert await run_processed(raising_process_func, 1, number=2) == 3, "Process should run and return 3"


async def test_spawn_process_back_calling_func():
    async with Koil():
        assert await run_processed(back_calling_func, 1, number=2) == 3, "Process should run and return 3"

async def test_spawn_process_back_raise_calling_func():
    async with Koil():
        with pytest.raises(Exception, match="This is a sleep and raise exception"):
            assert await run_processed(back_calling_raising_func, 1, number=2) == 3, "Process should run and return 3"




def process_gen(arg: int, number: int):
    yield arg + number
    yield arg + number


def raising_process_gen(arg: int, number: int):
    raise Exception("This is a test exception")
    

def back_calling_gen(arg: int, number: int):
    for i in unkoil_gen(iterating):
        yield arg + number


def back_calling_raising_gen(arg: int, number: int):
    for i in unkoil_gen(iterate_and_raise):
        yield arg + number

async def test_spawn_process_gen():
    async with Koil():
        async for i in iterate_processed(process_gen, 1, number=2):
            assert i == 3, "Process should run and yield 3"


async def test_spawn_process_exception_gen():
    async with Koil():
        with pytest.raises(Exception, match="This is a test exception"):
            async for i in iterate_processed(raising_process_gen, 1, number=2):
                assert i == 3, "Process should run and yield 3"


async def test_spawn_process_back_calling_gen():
    async with Koil():
        async for i in iterate_processed(back_calling_gen, 1, number=2):
                assert i == 3, "Process should run and yield 3"

async def test_spawn_process_back_raise_calling_gen():
    async with Koil():
        with pytest.raises(Exception, match="This is an iterate and raise exception"):
            async for i in iterate_processed(back_calling_raising_gen, 1, number=2):
                assert i == 3, "Process should run and yield 3"