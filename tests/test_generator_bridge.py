"""``unkoil_gen``: an async generator stepped through from sync code.

The generator lives in one task on the koil loop for its whole life; every
``next()``/``send()`` of the sync consumer is one step handed to that task.
"""

import asyncio
import contextvars
import threading
from collections.abc import AsyncGenerator

import pytest

from koil import Koil, KoilTimeoutError, unkoil_gen, unkoil_gen_with_timeout


def test_send_reaches_the_async_generator() -> None:
    async def echo() -> AsyncGenerator[str, str]:
        received = yield "ready"
        while received != "stop":
            received = yield f"got {received}"

    with Koil():
        gen = unkoil_gen(echo)
        assert next(gen) == "ready"
        assert gen.send("a") == "got a"
        assert gen.send("b") == "got b"
        with pytest.raises(StopIteration):
            gen.send("stop")


def test_send_with_a_timeout_per_step() -> None:
    async def doubler() -> AsyncGenerator[int, int]:
        value = yield 0
        while True:
            value = yield value * 2

    with Koil():
        gen = unkoil_gen_with_timeout(doubler, 5)
        next(gen)
        assert gen.send(2) == 4
        assert gen.send(21) == 42
        gen.close()


@pytest.mark.parametrize("timeout", [None, 5])
def test_an_empty_generator_just_ends(timeout: float | None) -> None:
    async def empty() -> AsyncGenerator[None, None]:
        return
        yield  # pragma: no cover

    with Koil():
        if timeout is None:
            assert list(unkoil_gen(empty)) == []
        else:
            assert list(unkoil_gen_with_timeout(empty, timeout)) == []


def test_every_step_runs_in_the_same_task() -> None:
    """What a generator holds across a yield belongs to the task that entered it."""

    async def agen() -> AsyncGenerator[int | bool, None]:
        first = asyncio.current_task()
        yield 1
        yield asyncio.current_task() is first
        yield asyncio.current_task() is first

    with Koil():
        assert list(unkoil_gen(agen)) == [1, True, True]


def test_a_task_group_survives_the_yields() -> None:
    """A TaskGroup entered before a yield is exited by the same task after it."""

    async def agen() -> AsyncGenerator[str | list[int], None]:
        results: list[int] = []

        async def work(i: int) -> None:
            await asyncio.sleep(0.01)
            results.append(i)

        async with asyncio.TaskGroup() as group:
            group.create_task(work(1))
            yield "started"
            group.create_task(work(2))
            yield "more"
        yield sorted(results)

    with Koil():
        assert list(unkoil_gen(agen)) == ["started", "more", [1, 2]]


def test_breaking_out_runs_the_cleanup_before_the_loop_continues() -> None:
    closed = threading.Event()

    async def agen() -> AsyncGenerator[int, None]:
        try:
            while True:
                yield 1
        finally:
            await asyncio.sleep(0.01)  # async cleanup, not just a flag
            closed.set()

    with Koil():
        for _ in unkoil_gen(agen):
            break
        assert closed.is_set()


def test_a_cleanup_error_on_early_close_reaches_the_consumer() -> None:
    async def agen() -> AsyncGenerator[int, None]:
        try:
            yield 1
            yield 2
        finally:
            raise ValueError("cleanup failed")

    with Koil():
        gen = unkoil_gen(agen)
        next(gen)
        with pytest.raises(ValueError, match="cleanup failed"):
            gen.close()


def test_throwing_into_the_consumer_closes_the_async_generator() -> None:
    closed = threading.Event()

    async def agen() -> AsyncGenerator[int, None]:
        try:
            yield 1
            yield 2
        finally:
            closed.set()

    with Koil():
        gen = unkoil_gen(agen)
        next(gen)
        with pytest.raises(KeyError):
            gen.throw(KeyError("from the consumer"))
        assert closed.is_set()


def test_an_exception_ends_the_generator() -> None:
    async def agen() -> AsyncGenerator[int, None]:
        yield 1
        raise ValueError("boom")

    with Koil():
        gen = unkoil_gen(agen)
        assert next(gen) == 1
        with pytest.raises(ValueError, match="boom"):
            next(gen)
        with pytest.raises(StopIteration):
            next(gen)


def test_each_step_sees_the_consumers_context_and_hands_changes_back() -> None:
    var: contextvars.ContextVar[str] = contextvars.ContextVar("step_var")

    async def agen() -> AsyncGenerator[str, None]:
        seen = var.get()
        var.set("gen-1")
        yield seen
        seen = var.get()
        var.set("gen-2")
        yield seen

    def consume() -> None:
        var.set("a")
        gen = unkoil_gen(agen)
        assert next(gen) == "a"
        assert var.get() == "gen-1"
        var.set("b")
        assert next(gen) == "b"
        assert var.get() == "gen-2"

    with Koil():
        contextvars.copy_context().run(consume)


def test_a_step_timeout_cancels_the_generator_and_runs_its_finally() -> None:
    cleaned = threading.Event()

    async def slow() -> AsyncGenerator[int, None]:
        try:
            yield 1
            await asyncio.sleep(30)
            yield 2
        finally:
            cleaned.set()

    with Koil():
        gen = unkoil_gen_with_timeout(slow, 0.1)
        assert next(gen) == 1
        with pytest.raises(KoilTimeoutError):
            next(gen)
        assert cleaned.is_set()


def test_the_timeout_is_per_step_not_for_the_whole_generator() -> None:
    async def steady() -> AsyncGenerator[int, None]:
        for i in range(5):
            await asyncio.sleep(0.05)
            yield i

    with Koil():
        # 0.25s in total, but never more than 0.05s between two values.
        assert list(unkoil_gen_with_timeout(steady, 0.2)) == [0, 1, 2, 3, 4]


def test_a_plain_async_iterator_works_too() -> None:
    """Not every async iterable is a generator: no ``asend``, no ``aclose``."""

    class Countdown:
        def __init__(self, start: int) -> None:
            self.left = start

        def __aiter__(self) -> "Countdown":
            return self

        async def __anext__(self) -> int:
            if self.left == 0:
                raise StopAsyncIteration
            self.left -= 1
            return self.left

    with Koil():
        assert list(unkoil_gen(Countdown, 3)) == [2, 1, 0]  # type: ignore[arg-type]


def test_many_items_come_through_in_order() -> None:
    async def count(n: int) -> AsyncGenerator[int, None]:
        for i in range(n):
            yield i

    with Koil():
        assert list(unkoil_gen(count, 2000)) == list(range(2000))


def test_two_generators_interleave_independently() -> None:
    async def count(start: int) -> AsyncGenerator[int, None]:
        for i in range(start, start + 3):
            await asyncio.sleep(0)
            yield i

    with Koil():
        a, b = unkoil_gen(count, 0), unkoil_gen(count, 100)
        assert [next(a), next(b), next(a), next(b), next(a), next(b)] == [
            0,
            100,
            1,
            101,
            2,
            102,
        ]
