"""Tests for nextorm.session — SessionCache, db_session, async_db_session."""

from __future__ import annotations

import asyncio

import pytest

from nextorm.entity import Entity
from nextorm.exceptions import TransactionError
from nextorm.fields import Req
from nextorm.session import (
    DBSessionManager,
    SessionCache,
    async_db_session,
    db_session,
    get_current_session,
)

# ---------------------------------------------------------------------------
# Minimal entity stubs
# ---------------------------------------------------------------------------


class Person(Entity):
    name: Req[str]


class Order(Entity):
    ref: Req[str]


# ---------------------------------------------------------------------------
# SessionCache — identity map
# ---------------------------------------------------------------------------


def test_session_cache_put_get() -> None:
    cache = SessionCache()
    p = Person.__new__(Person)
    cache.put(p, 1)
    assert cache.get((Person, 1)) is p


def test_session_cache_get_missing_returns_none() -> None:
    cache = SessionCache()
    assert cache.get((Person, 999)) is None


def test_session_cache_remove() -> None:
    cache = SessionCache()
    p = Person.__new__(Person)
    cache.put(p, 1)
    cache.remove(p, 1)
    assert cache.get((Person, 1)) is None


def test_session_cache_remove_nonexistent_is_silent() -> None:
    cache = SessionCache()
    p = Person.__new__(Person)
    cache.remove(p, 999)  # must not raise


def test_session_cache_separate_types_do_not_collide() -> None:
    cache = SessionCache()
    p = Person.__new__(Person)
    o = Order.__new__(Order)
    cache.put(p, 1)
    cache.put(o, 1)
    assert cache.get((Person, 1)) is p
    assert cache.get((Order, 1)) is o


# ---------------------------------------------------------------------------
# SessionCache — dirty tracking
# ---------------------------------------------------------------------------


def test_mark_and_dirty_objects() -> None:
    cache = SessionCache()
    p = Person.__new__(Person)
    cache.mark_dirty(p)
    assert p in cache.dirty_objects


def test_unmark_dirty() -> None:
    cache = SessionCache()
    p = Person.__new__(Person)
    cache.mark_dirty(p)
    cache.unmark_dirty(p)
    assert p not in cache.dirty_objects


def test_remove_also_unmarks_dirty() -> None:
    cache = SessionCache()
    p = Person.__new__(Person)
    cache.put(p, 1)
    cache.mark_dirty(p)
    cache.remove(p, 1)
    assert p not in cache.dirty_objects


def test_dirty_objects_is_frozen_snapshot() -> None:
    cache = SessionCache()
    p = Person.__new__(Person)
    snap = cache.dirty_objects  # take snapshot before marking
    cache.mark_dirty(p)
    assert p not in snap  # original snapshot not mutated


# ---------------------------------------------------------------------------
# SessionCache — objects_to_save
# ---------------------------------------------------------------------------


def test_schedule_save_queues_entity() -> None:
    cache = SessionCache()
    p = Person.__new__(Person)
    cache.schedule_save(p)
    assert p in cache.objects_to_save


def test_schedule_save_deduplicates() -> None:
    cache = SessionCache()
    p = Person.__new__(Person)
    cache.schedule_save(p)
    cache.schedule_save(p)
    assert cache.objects_to_save.count(p) == 1


def test_objects_to_save_returns_copy() -> None:
    cache = SessionCache()
    p = Person.__new__(Person)
    cache.schedule_save(p)
    lst = cache.objects_to_save
    lst.clear()
    assert p in cache.objects_to_save


# ---------------------------------------------------------------------------
# SessionCache — M2M collection tracking
# ---------------------------------------------------------------------------


def test_track_collection_add() -> None:
    cache = SessionCache()
    p = Person.__new__(Person)
    o = Order.__new__(Order)
    cache.track_collection_change(p, "orders", "add", o)
    colls = cache.modified_collections
    assert (p, "orders") in colls
    assert o in colls[(p, "orders")]["add"]


def test_track_collection_same_key_twice() -> None:
    """Second change to the same (owner, attr) key must not reset existing list."""
    cache = SessionCache()
    p = Person.__new__(Person)
    o1 = Order.__new__(Order)
    o2 = Order.__new__(Order)
    cache.track_collection_change(p, "orders", "add", o1)
    cache.track_collection_change(p, "orders", "add", o2)  # key already exists
    colls = cache.modified_collections
    assert o1 in colls[(p, "orders")]["add"]
    assert o2 in colls[(p, "orders")]["add"]


def test_track_collection_remove() -> None:
    cache = SessionCache()
    p = Person.__new__(Person)
    o = Order.__new__(Order)
    cache.track_collection_change(p, "orders", "remove", o)
    assert o in cache.modified_collections[(p, "orders")]["remove"]


def test_modified_collections_returns_copy() -> None:
    cache = SessionCache()
    p = Person.__new__(Person)
    o = Order.__new__(Order)
    cache.track_collection_change(p, "orders", "add", o)
    snap = cache.modified_collections
    snap.clear()
    assert (p, "orders") in cache.modified_collections


# ---------------------------------------------------------------------------
# SessionCache — clear
# ---------------------------------------------------------------------------


def test_clear_wipes_all_state() -> None:
    cache = SessionCache()
    p = Person.__new__(Person)
    o = Order.__new__(Order)
    cache.put(p, 1)
    cache.mark_dirty(p)
    cache.schedule_save(o)
    cache.track_collection_change(p, "orders", "add", o)

    cache.clear()

    assert cache.get((Person, 1)) is None
    assert len(cache.dirty_objects) == 0
    assert cache.objects_to_save == []
    assert cache.modified_collections == {}


# ---------------------------------------------------------------------------
# db_session — sync context manager
# ---------------------------------------------------------------------------


def test_db_session_context_manager() -> None:
    with db_session as cache:
        assert isinstance(cache, SessionCache)
        assert get_current_session() is cache


def test_db_session_cleared_after_exit() -> None:
    with db_session as cache:
        p = Person.__new__(Person)
        cache.put(p, 1)
    # After exit, cache is cleared
    assert cache.get((Person, 1)) is None


def test_db_session_call_syntax() -> None:
    with db_session() as cache:
        assert isinstance(cache, SessionCache)


def test_get_current_session_raises_outside_session() -> None:
    with pytest.raises(RuntimeError, match="No active db_session"):
        get_current_session()


def test_db_session_nesting() -> None:
    with db_session as outer:
        with db_session as inner:
            assert db_session.depth == 2
            assert get_current_session() is inner
        # Inner exited, outer still active
        assert db_session.depth == 1
        assert get_current_session() is outer


def test_db_session_depth_zero_outside() -> None:
    assert db_session.depth == 0


def test_db_session_current_none_outside() -> None:
    assert DBSessionManager.current() is None


def test_db_session_current_inside() -> None:
    with db_session as cache:
        assert DBSessionManager.current() is cache


# ---------------------------------------------------------------------------
# db_session — sync decorator
# ---------------------------------------------------------------------------


def test_db_session_sync_decorator() -> None:
    captured: list[SessionCache] = []

    @db_session
    def worker() -> None:
        captured.append(get_current_session())

    worker()
    assert len(captured) == 1
    assert isinstance(captured[0], SessionCache)


def test_db_session_decorator_wraps_name() -> None:
    @db_session
    def my_func() -> None:
        pass  # pragma: no cover

    assert my_func.__name__ == "my_func"


# ---------------------------------------------------------------------------
# db_session — async decorator
# ---------------------------------------------------------------------------


def test_db_session_async_decorator() -> None:
    captured: list[SessionCache] = []

    @db_session
    async def async_worker() -> None:
        captured.append(get_current_session())

    asyncio.run(async_worker())
    assert len(captured) == 1
    assert isinstance(captured[0], SessionCache)


def test_db_session_async_decorator_wraps_name() -> None:
    @db_session
    async def my_async_func() -> None:
        pass  # pragma: no cover

    assert my_async_func.__name__ == "my_async_func"


# ---------------------------------------------------------------------------
# db_session — async context manager
# ---------------------------------------------------------------------------


def test_async_db_session_context_manager() -> None:
    async def _run() -> SessionCache:
        async with async_db_session as cache:
            assert isinstance(cache, SessionCache)
            return cache

    cache = asyncio.run(_run())
    # After exit, cache is cleared
    assert cache.get((Person, 1)) is None


def test_async_db_session_is_same_object() -> None:
    assert async_db_session is db_session


# ---------------------------------------------------------------------------
# db_session — as_context / as_async_context helpers
# ---------------------------------------------------------------------------


def test_as_context_helper() -> None:
    with db_session.as_context() as cache:
        assert isinstance(cache, SessionCache)


def test_as_async_context_helper() -> None:
    async def _run() -> None:
        async with db_session.as_async_context() as cache:
            assert isinstance(cache, SessionCache)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Exception safety — cache must be cleared even after exception
# ---------------------------------------------------------------------------


def test_db_session_clears_on_exception() -> None:
    cache_ref: list[SessionCache] = []
    with pytest.raises(ValueError, match="boom"), db_session as cache:
        cache_ref.append(cache)
        raise ValueError("boom")

    # Cache was cleared
    assert cache_ref[0].objects_to_save == []
    # Depth back to 0
    assert db_session.depth == 0


def test_async_db_session_clears_on_exception() -> None:
    cache_ref: list[SessionCache] = []

    async def _run() -> None:
        with pytest.raises(ValueError, match="async boom"):
            async with async_db_session as cache:
                cache_ref.append(cache)
                raise ValueError("async boom")

    asyncio.run(_run())
    assert db_session.depth == 0


# ---------------------------------------------------------------------------
# db_session parametrised form
# ---------------------------------------------------------------------------


def test_db_session_retry_no_error() -> None:
    """retry=N does not affect behaviour when no error is raised."""
    calls: list[int] = []

    @db_session(retry=3)
    def work() -> None:
        calls.append(1)

    work()
    assert calls == [1]


def test_db_session_retry_on_transaction_error() -> None:
    """retry=2 means up to 3 total attempts."""
    attempts: list[int] = []

    @db_session(retry=2)
    def work() -> None:
        attempts.append(len(attempts) + 1)
        if len(attempts) < 3:
            raise TransactionError("deadlock")

    work()
    assert len(attempts) == 3


def test_db_session_retry_exhausted_raises() -> None:
    """If all retries are exhausted, the TransactionError propagates."""
    count: list[int] = []

    @db_session(retry=1)
    def work() -> None:
        count.append(1)
        raise TransactionError("always fails")

    with pytest.raises(TransactionError):
        work()
    assert len(count) == 2  # 1 original + 1 retry


def test_db_session_retry_async() -> None:
    """Async decorator retries on TransactionError."""
    attempts: list[int] = []

    @db_session(retry=1)
    async def async_work() -> None:
        attempts.append(1)
        if len(attempts) < 2:
            raise TransactionError("serialization failure")

    asyncio.run(async_work())
    assert len(attempts) == 2


def test_db_session_retry_async_exhausted() -> None:
    count: list[int] = []

    @db_session(retry=0)
    async def async_work() -> None:
        count.append(1)
        raise TransactionError("always fails")

    with pytest.raises(TransactionError):
        asyncio.run(async_work())
    assert len(count) == 1


def test_db_session_sql_debug_enables_and_restores(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from nextorm.debug import set_sql_debug  # noqa: PLC0415

    set_sql_debug(False)  # ensure off before test
    with db_session(sql_debug=True):
        from nextorm.debug import _debug_enabled  # noqa: PLC0415

        assert _debug_enabled is True
    from nextorm.debug import _debug_enabled  # noqa: PLC0415

    assert _debug_enabled is False


def test_db_session_sql_debug_async(capsys: pytest.CaptureFixture[str]) -> None:
    from nextorm.debug import set_sql_debug  # noqa: PLC0415

    set_sql_debug(False)

    async def _run() -> bool:
        async with db_session(sql_debug=True):
            from nextorm.debug import _debug_enabled  # noqa: PLC0415

            return _debug_enabled

    assert asyncio.run(_run()) is True
    from nextorm.debug import _debug_enabled  # noqa: PLC0415

    assert _debug_enabled is False
    set_sql_debug(False)


def test_db_session_serializable_flag() -> None:
    with db_session(serializable=True) as cache:
        assert cache.serializable is True


def test_db_session_immediate_flag() -> None:
    with db_session(immediate=True) as cache:
        assert cache.immediate is True


def test_db_session_default_flags() -> None:
    with db_session as cache:
        assert cache.serializable is False
        assert cache.immediate is False


def test_db_session_parametrised_context_returns_new_manager() -> None:
    mgr = db_session(retry=5)
    assert isinstance(mgr, DBSessionManager)
    assert mgr._retry == 5


def test_db_session_parametrised_context_works() -> None:
    with db_session(serializable=True, immediate=True) as cache:
        assert cache.serializable is True
        assert cache.immediate is True


def test_db_session_strict_flag() -> None:
    with db_session(strict=True) as cache:
        assert cache.strict is True


def test_db_session_strict_default() -> None:
    with db_session as cache:
        assert cache.strict is False


def test_db_session_show_values_stored() -> None:
    mgr = db_session(show_values=True)
    assert isinstance(mgr, DBSessionManager)
    assert mgr._show_values is True


def test_db_session_allowed_exceptions_suppresses() -> None:
    """allowed_exceptions: exception is suppressed and cache is cleared."""

    class MyError(Exception): ...

    caught: list[bool] = []
    with db_session(allowed_exceptions=[MyError]):
        raise MyError("ok")
    caught.append(True)
    assert caught == [True]
    # session stack must be empty after suppression
    assert db_session.depth == 0


async def test_db_session_allowed_exceptions_async() -> None:
    """async path: allowed_exceptions suppresses exception."""

    class MyError(Exception): ...

    async with db_session(allowed_exceptions=[MyError]):
        raise MyError("ok")
    # execution continues here if exception was suppressed
    assert db_session.depth == 0


def test_db_session_non_allowed_exception_propagates() -> None:
    """Exceptions not in allowed_exceptions still propagate normally."""

    class OtherError(Exception): ...

    import pytest as _pytest

    with _pytest.raises(OtherError), db_session(allowed_exceptions=[ValueError]):
        raise OtherError("boom")
    assert db_session.depth == 0


def test_db_session_retry_exceptions_custom() -> None:
    """retry_exceptions replaces the default TransactionError list."""

    class MyRetriable(Exception): ...

    calls: list[int] = []

    @db_session(retry=2, retry_exceptions=[MyRetriable])
    def _fn() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise MyRetriable
        return "ok"

    result = _fn()
    assert result == "ok"
    assert len(calls) == 3


def test_db_session_call_inherits_params() -> None:
    """__call__ propagates all new params to child manager."""
    parent = DBSessionManager(
        show_values=True,
        strict=True,
        allowed_exceptions=[ValueError],
        retry_exceptions=[RuntimeError],
    )
    child = parent()
    assert isinstance(child, DBSessionManager)
    assert child._show_values is True
    assert child._strict is True
    assert child._allowed_exceptions == (ValueError,)
    assert child._retry_exceptions == (RuntimeError,)


# ---------------------------------------------------------------------------
# Auto flush+commit on session exit
# ---------------------------------------------------------------------------


def test_session_exit_auto_flushes_and_commits() -> None:
    """Entities created inside db_session are auto-INSERTed on clean exit."""
    from nextorm.database import Database  # noqa: PLC0415

    class _AutoPerson(Entity):
        name: Req[str]

    _db = Database(entities=[_AutoPerson])
    _db.bind("sqlite", ":memory:")
    _db.generate_mapping(create_tables=True)

    with db_session:
        _AutoPerson(name="alice")  # no db.save() call

    # After session exits, alice should be in the DB
    result = _db.select(_AutoPerson).fetch_all()
    assert len(result) == 1
    assert result[0].name == "alice"
    _db.close()


def test_session_exit_rollback_on_exception() -> None:
    """An exception inside db_session triggers rollback — entity is NOT persisted."""
    from nextorm.database import Database  # noqa: PLC0415

    class _RollPerson(Entity):
        name: Req[str]

    _db = Database(entities=[_RollPerson])
    _db.bind("sqlite", ":memory:")
    _db.generate_mapping(create_tables=True)

    with pytest.raises(RuntimeError), db_session:
        _RollPerson(name="bob")  # scheduled for insert
        raise RuntimeError("abort")

    # bob should NOT be in the DB — rollback was called
    result = _db.select(_RollPerson).fetch_all()
    assert result == []
    _db.close()


def test_session_nested_only_outermost_commits() -> None:
    """Nested db_session blocks: only the outermost triggers flush+commit."""
    from nextorm.database import Database  # noqa: PLC0415

    class _NestPerson(Entity):
        name: Req[str]

    _db = Database(entities=[_NestPerson])
    _db.bind("sqlite", ":memory:")
    _db.generate_mapping(create_tables=True)

    with db_session, db_session:
        _NestPerson(name="carol")
        # inner exit — NOT outermost, so no flush yet

    result = _db.select(_NestPerson).fetch_all()
    assert len(result) == 1
    assert result[0].name == "carol"
    _db.close()


def test_session_exit_allowed_exception_commits() -> None:
    """allowed_exceptions path still flushes+commits before suppressing."""
    from nextorm.database import Database  # noqa: PLC0415

    class _AllowedPerson(Entity):
        name: Req[str]

    class _Redirect(Exception): ...

    _db = Database(entities=[_AllowedPerson])
    _db.bind("sqlite", ":memory:")
    _db.generate_mapping(create_tables=True)

    with db_session(allowed_exceptions=[_Redirect]):
        _AllowedPerson(name="dave")
        raise _Redirect("redirect")

    result = _db.select(_AllowedPerson).fetch_all()
    assert len(result) == 1
    assert result[0].name == "dave"
    _db.close()


@pytest.mark.asyncio
async def test_async_session_exit_auto_flushes_and_commits() -> None:
    """Async entities created inside db_session are auto-INSERTed on clean exit."""
    from nextorm.async_database import AsyncDatabase  # noqa: PLC0415

    class _AsyncAutoPerson(Entity):
        name: Req[str]

    _db = AsyncDatabase(entities=[_AsyncAutoPerson])
    await _db.bind("sqlite", ":memory:")
    await _db.generate_mapping(create_tables=True)

    async with db_session:
        p = _AsyncAutoPerson(name="eve")
        await _db.asave(p)  # explicit save to get PK and _dbvals_

    result = await _db.aselect(_AsyncAutoPerson).fetch_all()
    assert len(result) == 1
    assert result[0].name == "eve"
    await _db.close()


@pytest.mark.asyncio
async def test_async_session_exit_rollback_on_exception() -> None:
    """Async: exception inside db_session prevents entity from being persisted."""
    from nextorm.async_database import AsyncDatabase  # noqa: PLC0415

    class _AsyncRollPerson(Entity):
        name: Req[str]

    _db = AsyncDatabase(entities=[_AsyncRollPerson])
    await _db.bind("sqlite", ":memory:")
    await _db.generate_mapping(create_tables=True)

    with pytest.raises(RuntimeError):
        async with db_session:
            _AsyncRollPerson(name="frank")  # auto-scheduled, NOT yet flushed to DB
            raise RuntimeError("abort")

    # frank was never flushed (exception before flush), session cleaned up
    result = await _db.aselect(_AsyncRollPerson).fetch_all()
    assert result == []
    await _db.close()


# ---------------------------------------------------------------------------
# Coverage gap tests
# ---------------------------------------------------------------------------


def test_unschedule_save_idempotent() -> None:
    """unschedule_save on an entity not queued must not raise (ValueError path)."""
    cache = SessionCache()
    p = Person.__new__(Person)
    # Not scheduled — calling unschedule is safe (hits except ValueError: pass)
    cache.unschedule_save(p)
    assert cache.objects_to_save == []


def test_session_nested_exception_in_inner_propagates() -> None:
    """Exception in an inner nested session propagates; outer sees it and rolls back.

    This covers the ``else: if is_outermost:`` rollback branch with
    ``is_outermost=False`` (the branch where we skip rollback for inner exits).
    """
    from nextorm.database import Database  # noqa: PLC0415

    class _NestRollPerson(Entity):
        name: Req[str]

    _db = Database(entities=[_NestRollPerson])
    _db.bind("sqlite", ":memory:")
    _db.generate_mapping(create_tables=True)

    with pytest.raises(RuntimeError), db_session, db_session:
        # inner session — is_outermost=False when exiting; no rollback here
        raise RuntimeError("inner abort")

    result = _db.select(_NestRollPerson).fetch_all()
    assert result == []
    _db.close()


@pytest.mark.asyncio
async def test_async_session_nested_only_outermost_commits() -> None:
    """Async nested db_session: only outermost triggers flush+commit."""
    from nextorm.async_database import AsyncDatabase  # noqa: PLC0415

    class _AsyncNestPerson(Entity):
        name: Req[str]

    _db = AsyncDatabase(entities=[_AsyncNestPerson])
    await _db.bind("sqlite", ":memory:")
    await _db.generate_mapping(create_tables=True)

    async with db_session, db_session:
        p = _AsyncNestPerson(name="grace")
        await _db.asave(p)
        # inner exit — is_outermost=False, no flush yet

    result = await _db.aselect(_AsyncNestPerson).fetch_all()
    assert len(result) == 1
    assert result[0].name == "grace"
    await _db.close()


@pytest.mark.asyncio
async def test_async_session_nested_exception_propagates() -> None:
    """Exception in inner async session propagates; outer rolls back."""
    from nextorm.async_database import AsyncDatabase  # noqa: PLC0415

    class _AsyncNestRoll(Entity):
        name: Req[str]

    _db = AsyncDatabase(entities=[_AsyncNestRoll])
    await _db.bind("sqlite", ":memory:")
    await _db.generate_mapping(create_tables=True)

    with pytest.raises(RuntimeError):
        async with db_session:
            async with db_session:
                raise RuntimeError("inner async abort")

    result = await _db.aselect(_AsyncNestRoll).fetch_all()
    assert result == []
    await _db.close()


@pytest.mark.asyncio
async def test_async_session_with_sync_db_flushes_and_commits() -> None:
    """A sync Database used inside async with db_session uses the sync code paths."""
    from nextorm.database import Database  # noqa: PLC0415

    class _SyncInAsync(Entity):
        name: Req[str]

    _db = Database(entities=[_SyncInAsync])
    _db.bind("sqlite", ":memory:")
    _db.generate_mapping(create_tables=True)

    async with db_session:
        _SyncInAsync(name="henry")  # auto-scheduled; _db_ is sync Database

    result = _db.select(_SyncInAsync).fetch_all()
    assert len(result) == 1
    assert result[0].name == "henry"
    _db.close()


@pytest.mark.asyncio
async def test_async_session_with_sync_db_rollback_on_exception() -> None:
    """A sync Database inside async with db_session rolls back via sync path."""
    from nextorm.database import Database  # noqa: PLC0415

    class _SyncInAsyncRoll(Entity):
        name: Req[str]

    _db = Database(entities=[_SyncInAsyncRoll])
    _db.bind("sqlite", ":memory:")
    _db.generate_mapping(create_tables=True)

    with pytest.raises(RuntimeError):
        async with db_session:
            _SyncInAsyncRoll(name="ivan")  # scheduled but not flushed
            raise RuntimeError("async abort with sync db")

    result = _db.select(_SyncInAsyncRoll).fetch_all()
    assert result == []
    _db.close()


def test_session_exit_commit_failure_cleans_up() -> None:
    """When commit raises in __exit__, the session is still cleaned up
    and the exception re-raised.
    """
    from nextorm.database import Database  # noqa: PLC0415

    class _CommitFailPerson(Entity):
        name: Req[str]

    _db = Database(entities=[_CommitFailPerson])
    _db.bind("sqlite", ":memory:")
    _db.generate_mapping(create_tables=True)

    commit_called: list[int] = []

    def _bad_commit() -> None:
        commit_called.append(1)
        raise RuntimeError("Forced commit failure")

    with pytest.raises(RuntimeError, match="Forced commit failure"), db_session:
        _CommitFailPerson(name="oops")
        # Patch _commit_transaction (called by __exit__ after flush).
        _db._commit_transaction = _bad_commit  # type: ignore[method-assign]

    # Session must be fully cleaned up despite the commit failure.
    assert db_session.depth == 0
    assert DBSessionManager.current() is None
    assert commit_called
    _db.close()


def test_session_exit_commit_and_rollback_both_fail() -> None:
    """When both commit and rollback fail, the rollback exception is swallowed
    and the commit error is raised.
    """
    from nextorm.database import Database  # noqa: PLC0415

    class _BothFailPerson(Entity):
        name: Req[str]

    _db = Database(entities=[_BothFailPerson])
    _db.bind("sqlite", ":memory:")
    _db.generate_mapping(create_tables=True)

    def _bad_commit() -> None:
        raise RuntimeError("commit failed")

    def _bad_rollback() -> None:
        raise RuntimeError("rollback also failed")

    with pytest.raises(RuntimeError, match="commit failed"), db_session:
        _BothFailPerson(name="x")
        _db._commit_transaction = _bad_commit  # type: ignore[method-assign]
        _db._rollback_transaction = _bad_rollback  # type: ignore[method-assign]

    assert db_session.depth == 0
    _db.close()


@pytest.mark.asyncio
async def test_async_session_exit_commit_failure_cleans_up() -> None:
    """When commit raises in __aexit__, the session is still cleaned up
    and the exception re-raised.
    """
    from nextorm.async_database import AsyncDatabase  # noqa: PLC0415

    class _AsyncCommitFailPerson(Entity):
        name: Req[str]

    _db = AsyncDatabase(entities=[_AsyncCommitFailPerson])
    await _db.bind("sqlite", ":memory:")
    await _db.generate_mapping(create_tables=True)

    commit_called: list[int] = []

    async def _bad_acommit() -> None:
        commit_called.append(1)
        raise RuntimeError("Forced async commit failure")

    with pytest.raises(RuntimeError, match="Forced async commit failure"):
        async with db_session:
            _AsyncCommitFailPerson(name="oops")
            _db._acommit_transaction = _bad_acommit  # type: ignore[method-assign]

    assert db_session.depth == 0
    assert DBSessionManager.current() is None
    assert commit_called
    await _db.close()


@pytest.mark.asyncio
async def test_async_session_exit_commit_and_rollback_both_fail() -> None:
    """Async: when both acommit and arollback fail, the rollback exception is swallowed."""
    from nextorm.async_database import AsyncDatabase  # noqa: PLC0415

    class _AsyncBothFail(Entity):
        name: Req[str]

    _db = AsyncDatabase(entities=[_AsyncBothFail])
    await _db.bind("sqlite", ":memory:")
    await _db.generate_mapping(create_tables=True)

    async def _bad_acommit() -> None:
        raise RuntimeError("async commit failed")

    async def _bad_arollback() -> None:
        raise RuntimeError("async rollback also failed")

    with pytest.raises(RuntimeError, match="async commit failed"):
        async with db_session:
            _AsyncBothFail(name="y")
            _db._acommit_transaction = _bad_acommit  # type: ignore[method-assign]
            _db._arollback_transaction = _bad_arollback  # type: ignore[method-assign]

    assert db_session.depth == 0
    await _db.close()


@pytest.mark.asyncio
async def test_async_session_with_sync_db_commit_failure_cleans_up() -> None:
    """Sync Database inside async db_session: when commit fails, sync rollback path is taken."""
    from nextorm.database import Database  # noqa: PLC0415

    class _AsyncSyncCommitFail(Entity):
        name: Req[str]

    _db = Database(entities=[_AsyncSyncCommitFail])
    _db.bind("sqlite", ":memory:")
    _db.generate_mapping(create_tables=True)

    def _bad_commit() -> None:
        raise RuntimeError("sync commit failed inside async session")

    with pytest.raises(RuntimeError, match="sync commit failed inside async session"):
        async with db_session:
            _AsyncSyncCommitFail(name="z")
            _db._commit_transaction = _bad_commit  # type: ignore[method-assign]

    assert db_session.depth == 0
    _db.close()


def test_session_exit_flush_failure_rolls_back() -> None:
    """When flush raises inside __exit__, all databases are rolled back
    and the original exception re-raised.
    """
    from nextorm.database import Database  # noqa: PLC0415

    class _FlushFailEntity(Entity):
        name: Req[str]

    _db = Database(entities=[_FlushFailEntity])
    _db.bind("sqlite", ":memory:")
    _db.generate_mapping(create_tables=True)

    with pytest.raises(RuntimeError, match="flush boom"), db_session:
        _FlushFailEntity(name="x")
        # Patch _do_insert so flush() raises during __exit__
        original = _db._do_insert

        def _bad_insert(*args: object, **kw: object) -> None:
            raise RuntimeError("flush boom")

        _db._do_insert = _bad_insert  # type: ignore[method-assign]

    # Session should be fully cleaned up
    assert db_session.depth == 0
    _db._do_insert = original  # type: ignore[method-assign]
    _db.close()


def test_session_exit_flush_failure_rollback_also_fails() -> None:
    """Flush fails AND rollback also fails — rollback error is silently swallowed."""
    from nextorm.database import Database  # noqa: PLC0415

    class _FlushAndRollbackFail(Entity):
        name: Req[str]

    _db = Database(entities=[_FlushAndRollbackFail])
    _db.bind("sqlite", ":memory:")
    _db.generate_mapping(create_tables=True)

    with pytest.raises(RuntimeError, match="flush boom2"), db_session:
        _FlushAndRollbackFail(name="y")

        def _bad_insert2(*args: object, **kw: object) -> None:
            raise RuntimeError("flush boom2")

        def _bad_rollback2() -> None:
            raise RuntimeError("rollback also failed")

        _db._do_insert = _bad_insert2  # type: ignore[method-assign]
        _db._rollback_transaction = _bad_rollback2  # type: ignore[method-assign]

    assert db_session.depth == 0
    _db.close()


def test_session_exit_primary_commit_failure_rolls_back_secondaries() -> None:
    """When the primary commit fails, all secondary databases are rolled back."""
    from nextorm.database import Database  # noqa: PLC0415

    class _PriCommitFail(Entity):
        val: Req[str]

    class _SecRollbackTarget(Entity):
        ref: Req[str]

    db1 = Database(entities=[_PriCommitFail])
    db1.bind("sqlite", ":memory:")
    db1.generate_mapping(create_tables=True)

    db2 = Database(entities=[_SecRollbackTarget])
    db2.bind("sqlite", ":memory:")
    db2.generate_mapping(create_tables=True)

    def _bad_primary_commit() -> None:
        raise RuntimeError("primary commit failed")

    with pytest.raises(RuntimeError, match="primary commit failed"), db_session:
        _PriCommitFail(val="a")
        _SecRollbackTarget(ref="b")
        db1._commit_transaction = _bad_primary_commit  # type: ignore[method-assign]

    assert db_session.depth == 0
    db1.close()
    db2.close()


def test_session_exit_secondary_commit_failure() -> None:
    """When a secondary commit fails after the primary succeeds,
    the exception is re-raised after all secondaries are processed.
    """
    from nextorm.database import Database  # noqa: PLC0415

    class _SecPrimary(Entity):
        label: Req[str]

    class _SecSecondary(Entity):
        tag: Req[str]

    db1 = Database(entities=[_SecPrimary])
    db1.bind("sqlite", ":memory:")
    db1.generate_mapping(create_tables=True)

    db2 = Database(entities=[_SecSecondary])
    db2.bind("sqlite", ":memory:")
    db2.generate_mapping(create_tables=True)

    def _bad_commit2() -> None:
        raise RuntimeError("secondary commit failed")

    with pytest.raises(RuntimeError, match="secondary commit failed"), db_session:
        _SecPrimary(label="p")
        _SecSecondary(tag="s")
        db2._commit_transaction = _bad_commit2  # type: ignore[method-assign]

    assert db_session.depth == 0
    db1.close()
    db2.close()


def test_session_exit_error_path_rollback_exception_swallowed() -> None:
    """When an exception exits the session and rollback also raises,
    the rollback error is silently swallowed.
    """
    from nextorm.database import Database  # noqa: PLC0415

    class _ErrorRollbackFail(Entity):
        item: Req[str]

    _db = Database(entities=[_ErrorRollbackFail])
    _db.bind("sqlite", ":memory:")
    _db.generate_mapping(create_tables=True)

    def _bad_rollback_err() -> None:
        raise RuntimeError("rollback in error path failed")

    with pytest.raises(RuntimeError, match="user error"), db_session:
        _ErrorRollbackFail(item="x")
        _db._rollback_transaction = _bad_rollback_err  # type: ignore[method-assign]
        raise RuntimeError("user error")

    assert db_session.depth == 0
    _db.close()


@pytest.mark.asyncio
async def test_async_session_exit_flush_failure_rolls_back() -> None:
    """Async: when flush raises in __aexit__, all databases are rolled back
    and the original exception re-raised.
    """
    from nextorm.database import Database  # noqa: PLC0415

    class _AsyncFlushFail(Entity):
        name: Req[str]

    _db = Database(entities=[_AsyncFlushFail])
    _db.bind("sqlite", ":memory:")
    _db.generate_mapping(create_tables=True)

    def _bad_insert_a(*args: object, **kw: object) -> None:
        raise RuntimeError("async flush boom")

    with pytest.raises(RuntimeError, match="async flush boom"):
        async with db_session:
            _AsyncFlushFail(name="a")
            _db._do_insert = _bad_insert_a  # type: ignore[method-assign]

    assert db_session.depth == 0
    _db.close()


@pytest.mark.asyncio
async def test_async_session_exit_primary_commit_failure_rolls_back_secondaries() -> None:
    """Async: when the primary commit fails, all secondary databases are rolled back."""
    from nextorm.database import Database  # noqa: PLC0415

    class _AsyncPriCommitFail(Entity):
        val: Req[str]

    class _AsyncSecRollback(Entity):
        ref: Req[str]

    db1 = Database(entities=[_AsyncPriCommitFail])
    db1.bind("sqlite", ":memory:")
    db1.generate_mapping(create_tables=True)

    db2 = Database(entities=[_AsyncSecRollback])
    db2.bind("sqlite", ":memory:")
    db2.generate_mapping(create_tables=True)

    def _bad_primary() -> None:
        raise RuntimeError("async primary commit failed")

    with pytest.raises(RuntimeError, match="async primary commit failed"):
        async with db_session:
            _AsyncPriCommitFail(val="a")
            _AsyncSecRollback(ref="b")
            db1._commit_transaction = _bad_primary  # type: ignore[method-assign]

    assert db_session.depth == 0
    db1.close()
    db2.close()


@pytest.mark.asyncio
async def test_async_session_exit_secondary_commit_failure() -> None:
    """Async: when a secondary commit fails after the primary succeeds,
    the exception is re-raised.
    """
    from nextorm.database import Database  # noqa: PLC0415

    class _AsyncSecPrimary(Entity):
        label: Req[str]

    class _AsyncSecSecondary(Entity):
        tag: Req[str]

    db1 = Database(entities=[_AsyncSecPrimary])
    db1.bind("sqlite", ":memory:")
    db1.generate_mapping(create_tables=True)

    db2 = Database(entities=[_AsyncSecSecondary])
    db2.bind("sqlite", ":memory:")
    db2.generate_mapping(create_tables=True)

    def _bad_secondary_async() -> None:
        raise RuntimeError("async secondary commit failed")

    with pytest.raises(RuntimeError, match="async secondary commit failed"):
        async with db_session:
            _AsyncSecPrimary(label="p")
            _AsyncSecSecondary(tag="s")
            db2._commit_transaction = _bad_secondary_async  # type: ignore[method-assign]

    assert db_session.depth == 0
    db1.close()
    db2.close()


@pytest.mark.asyncio
async def test_async_session_exit_error_path_rollback_exception_swallowed() -> None:
    """Async: when an exception exits the session and rollback also raises,
    the rollback error is silently swallowed.
    """
    from nextorm.database import Database  # noqa: PLC0415

    class _AsyncErrorRollbackFail(Entity):
        item: Req[str]

    _db = Database(entities=[_AsyncErrorRollbackFail])
    _db.bind("sqlite", ":memory:")
    _db.generate_mapping(create_tables=True)

    def _bad_async_rollback() -> None:
        raise RuntimeError("async rollback in error path failed")

    with pytest.raises(RuntimeError, match="async user error"):
        async with db_session:
            _AsyncErrorRollbackFail(item="y")
            _db._rollback_transaction = _bad_async_rollback  # type: ignore[method-assign]
            raise RuntimeError("async user error")

    assert db_session.depth == 0
    _db.close()


def test_session_exit_primary_commit_failure_secondary_rollback_also_fails() -> None:
    """When the primary commit fails and secondary rollback also raises,
    the rollback error is silently swallowed.
    """
    from nextorm.database import Database  # noqa: PLC0415

    class _PriFailSecRollFail1(Entity):
        val: Req[str]

    class _PriFailSecRollFail2(Entity):
        ref: Req[str]

    db1 = Database(entities=[_PriFailSecRollFail1])
    db1.bind("sqlite", ":memory:")
    db1.generate_mapping(create_tables=True)

    db2 = Database(entities=[_PriFailSecRollFail2])
    db2.bind("sqlite", ":memory:")
    db2.generate_mapping(create_tables=True)

    def _bad_primary2() -> None:
        raise RuntimeError("primary commit failed2")

    def _bad_secondary_rollback() -> None:
        raise RuntimeError("secondary rollback failed")

    with pytest.raises(RuntimeError, match="primary commit failed2"), db_session:
        _PriFailSecRollFail1(val="a")
        _PriFailSecRollFail2(ref="b")
        db1._commit_transaction = _bad_primary2  # type: ignore[method-assign]
        db2._rollback_transaction = _bad_secondary_rollback  # type: ignore[method-assign]

    assert db_session.depth == 0
    db1.close()
    db2.close()


def test_session_exit_multiple_secondary_commit_failures() -> None:
    """When multiple secondary commits fail, only the first exception propagates;
    subsequent ones are swallowed.
    """
    from nextorm.database import Database  # noqa: PLC0415

    class _MultiSec1(Entity):
        a: Req[str]

    class _MultiSec2(Entity):
        b: Req[str]

    class _MultiSec3(Entity):
        c: Req[str]

    db1 = Database(entities=[_MultiSec1])
    db1.bind("sqlite", ":memory:")
    db1.generate_mapping(create_tables=True)

    db2 = Database(entities=[_MultiSec2])
    db2.bind("sqlite", ":memory:")
    db2.generate_mapping(create_tables=True)

    db3 = Database(entities=[_MultiSec3])
    db3.bind("sqlite", ":memory:")
    db3.generate_mapping(create_tables=True)

    def _bad_sec_a() -> None:
        raise RuntimeError("secondary a commit failed")

    def _bad_sec_b() -> None:
        raise RuntimeError("secondary b commit failed")

    with pytest.raises(RuntimeError, match="secondary a commit failed"), db_session:
        _MultiSec1(a="1")
        _MultiSec2(b="2")
        _MultiSec3(c="3")
        db2._commit_transaction = _bad_sec_a  # type: ignore[method-assign]
        db3._commit_transaction = _bad_sec_b  # type: ignore[method-assign]

    assert db_session.depth == 0
    db1.close()
    db2.close()
    db3.close()


@pytest.mark.asyncio
async def test_async_session_exit_flush_failure_with_async_db_rolls_back() -> None:
    """Async: when flush fails using an actual AsyncDatabase, the async rollback path is taken."""
    from nextorm.async_database import AsyncDatabase  # noqa: PLC0415

    class _AsyncFlushFailAsync(Entity):
        name: Req[str]

    _db = AsyncDatabase(entities=[_AsyncFlushFailAsync])
    await _db.bind("sqlite", ":memory:")
    await _db.generate_mapping(create_tables=True)

    def _bad_insert_async(*args: object, **kw: object) -> object:
        raise RuntimeError("async db flush boom")

    with pytest.raises(RuntimeError, match="async db flush boom"):
        async with db_session:
            _AsyncFlushFailAsync(name="x")
            _db._do_insert = _bad_insert_async  # type: ignore[method-assign, assignment]

    assert db_session.depth == 0
    await _db.close()


@pytest.mark.asyncio
async def test_async_session_exit_flush_failure_async_rollback_also_fails() -> None:
    """Async: when flush fails and async rollback also raises,
    the rollback error is silently swallowed.
    """
    from nextorm.async_database import AsyncDatabase  # noqa: PLC0415

    class _AsyncFlushAndRollbackFail(Entity):
        name: Req[str]

    _db = AsyncDatabase(entities=[_AsyncFlushAndRollbackFail])
    await _db.bind("sqlite", ":memory:")
    await _db.generate_mapping(create_tables=True)

    def _bad_insert_roll(*args: object, **kw: object) -> object:
        raise RuntimeError("async flush and rollback fail")

    async def _bad_arollback() -> None:
        raise RuntimeError("async rollback also failed")

    with pytest.raises(RuntimeError, match="async flush and rollback fail"):
        async with db_session:
            _AsyncFlushAndRollbackFail(name="y")
            _db._do_insert = _bad_insert_roll  # type: ignore[method-assign, assignment]
            _db._arollback_transaction = _bad_arollback  # type: ignore[method-assign]

    assert db_session.depth == 0
    await _db.close()


@pytest.mark.asyncio
async def test_async_session_exit_primary_async_commit_failure_with_async_secondary() -> None:
    """Async: when an AsyncDatabase primary commit fails,
    async secondary databases are rolled back.
    """
    from nextorm.async_database import AsyncDatabase  # noqa: PLC0415

    class _AsyncPriAsync(Entity):
        v: Req[str]

    class _AsyncSecAsync(Entity):
        w: Req[str]

    _db1 = AsyncDatabase(entities=[_AsyncPriAsync])
    await _db1.bind("sqlite", ":memory:")
    await _db1.generate_mapping(create_tables=True)

    _db2 = AsyncDatabase(entities=[_AsyncSecAsync])
    await _db2.bind("sqlite", ":memory:")
    await _db2.generate_mapping(create_tables=True)

    async def _bad_acommit_primary() -> None:
        raise RuntimeError("async primary acommit failed")

    with pytest.raises(RuntimeError, match="async primary acommit failed"):
        async with db_session:
            _AsyncPriAsync(v="a")
            _AsyncSecAsync(w="b")
            _db1._acommit_transaction = _bad_acommit_primary  # type: ignore[method-assign]

    assert db_session.depth == 0
    await _db1.close()
    await _db2.close()


@pytest.mark.asyncio
async def test_async_session_exit_primary_async_commit_failure_secondary_rollback_fails() -> None:
    """Async: when the AsyncDatabase primary commit fails and secondary async rollback
    also raises, the rollback error is silently swallowed.
    """
    from nextorm.async_database import AsyncDatabase  # noqa: PLC0415

    class _AsyncPriFailSecRollFail1(Entity):
        v: Req[str]

    class _AsyncPriFailSecRollFail2(Entity):
        w: Req[str]

    _db1 = AsyncDatabase(entities=[_AsyncPriFailSecRollFail1])
    await _db1.bind("sqlite", ":memory:")
    await _db1.generate_mapping(create_tables=True)

    _db2 = AsyncDatabase(entities=[_AsyncPriFailSecRollFail2])
    await _db2.bind("sqlite", ":memory:")
    await _db2.generate_mapping(create_tables=True)

    async def _bad_primary_roll() -> None:
        raise RuntimeError("async primary fail secondary roll")

    async def _bad_sec_rollback() -> None:
        raise RuntimeError("async secondary rollback fail")

    with pytest.raises(RuntimeError, match="async primary fail secondary roll"):
        async with db_session:
            _AsyncPriFailSecRollFail1(v="a")
            _AsyncPriFailSecRollFail2(w="b")
            _db1._acommit_transaction = _bad_primary_roll  # type: ignore[method-assign]
            _db2._arollback_transaction = _bad_sec_rollback  # type: ignore[method-assign]

    assert db_session.depth == 0
    await _db1.close()
    await _db2.close()


@pytest.mark.asyncio
async def test_async_session_exit_async_secondary_commit_failure() -> None:
    """Async: when an AsyncDatabase secondary commit fails after the primary succeeds,
    the exception is re-raised.
    """
    from nextorm.async_database import AsyncDatabase  # noqa: PLC0415

    class _AsyncSecPrimaryA(Entity):
        p: Req[str]

    class _AsyncSecSecondaryA(Entity):
        q: Req[str]

    _db1 = AsyncDatabase(entities=[_AsyncSecPrimaryA])
    await _db1.bind("sqlite", ":memory:")
    await _db1.generate_mapping(create_tables=True)

    _db2 = AsyncDatabase(entities=[_AsyncSecSecondaryA])
    await _db2.bind("sqlite", ":memory:")
    await _db2.generate_mapping(create_tables=True)

    async def _bad_acommit_secondary() -> None:
        raise RuntimeError("async secondary acommit failed")

    with pytest.raises(RuntimeError, match="async secondary acommit failed"):
        async with db_session:
            _AsyncSecPrimaryA(p="x")
            _AsyncSecSecondaryA(q="y")
            _db2._acommit_transaction = _bad_acommit_secondary  # type: ignore[method-assign]

    assert db_session.depth == 0
    await _db1.close()
    await _db2.close()


@pytest.mark.asyncio
async def test_async_session_exit_multiple_async_secondary_commit_failures() -> None:
    """Async: when multiple async secondary commits fail, only the first exception
    propagates; subsequent ones are swallowed.
    """
    from nextorm.async_database import AsyncDatabase  # noqa: PLC0415

    class _ThreeAsyncA(Entity):
        a: Req[str]

    class _ThreeAsyncB(Entity):
        b: Req[str]

    class _ThreeAsyncC(Entity):
        c: Req[str]

    _db1 = AsyncDatabase(entities=[_ThreeAsyncA])
    await _db1.bind("sqlite", ":memory:")
    await _db1.generate_mapping(create_tables=True)

    _db2 = AsyncDatabase(entities=[_ThreeAsyncB])
    await _db2.bind("sqlite", ":memory:")
    await _db2.generate_mapping(create_tables=True)

    _db3 = AsyncDatabase(entities=[_ThreeAsyncC])
    await _db3.bind("sqlite", ":memory:")
    await _db3.generate_mapping(create_tables=True)

    async def _bad_sec_async_a() -> None:
        raise RuntimeError("async sec A commit failed")

    async def _bad_sec_async_b() -> None:
        raise RuntimeError("async sec B commit failed")

    with pytest.raises(RuntimeError, match="async sec A commit failed"):
        async with db_session:
            _ThreeAsyncA(a="1")
            _ThreeAsyncB(b="2")
            _ThreeAsyncC(c="3")
            _db2._acommit_transaction = _bad_sec_async_a  # type: ignore[method-assign]
            _db3._acommit_transaction = _bad_sec_async_b  # type: ignore[method-assign]

    assert db_session.depth == 0
    await _db1.close()
    await _db2.close()
    await _db3.close()
