"""Tests for thread-safety of the session stack.

Each thread should have an independent :class:`_SessionStack` so that
concurrent requests running in different threads cannot accidentally see
each other's session caches.
"""

from __future__ import annotations

import threading

from nextorm.session import _get_session_stack, db_session


def test_fresh_thread_starts_with_empty_stack() -> None:
    """A newly-spawned thread must start with depth == 0."""
    depth_in_thread: list[int] = []

    def worker() -> None:
        depth_in_thread.append(_get_session_stack().depth)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert depth_in_thread == [0]


def test_threads_have_independent_stacks() -> None:
    """Sessions opened in one thread must not be visible in another."""
    barriers = [threading.Barrier(2) for _ in range(2)]
    depths: dict[str, list[int]] = {"A": [], "B": []}

    def worker_a() -> None:
        with db_session:
            depths["A"].append(_get_session_stack().depth)  # should be 1
            barriers[0].wait()  # sync with B
            depths["A"].append(_get_session_stack().depth)  # still 1, even while B has 2
            barriers[1].wait()

    def worker_b() -> None:
        barriers[0].wait()  # wait for A to open its session
        with db_session:  # noqa: SIM117
            with db_session:
                depths["B"].append(_get_session_stack().depth)  # should be 2 (B's own)
        barriers[1].wait()

    t_a = threading.Thread(target=worker_a)
    t_b = threading.Thread(target=worker_b)
    t_a.start()
    t_b.start()
    t_a.join()
    t_b.join()

    assert depths["A"] == [1, 1], f"A: {depths['A']}"
    assert depths["B"] == [2], f"B: {depths['B']}"


def test_main_thread_stack_empty_after_session() -> None:
    """After the session context exits, depth must return to 0."""
    assert _get_session_stack().depth == 0
    with db_session:
        assert _get_session_stack().depth == 1
    assert _get_session_stack().depth == 0


def test_session_stack_lazy_init_per_thread() -> None:
    """Calling _get_session_stack() twice in the same thread returns the same object."""
    stack1 = _get_session_stack()
    stack2 = _get_session_stack()
    assert stack1 is stack2
