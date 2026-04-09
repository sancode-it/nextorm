"""Tests for Batch I — Connection pooling (ConnectionPool / AsyncConnectionPool)."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

import pytest

from nextorm.database import Database
from nextorm.entity import Entity
from nextorm.fields import PK, Req
from nextorm.pool import AsyncConnectionPool, ConnectionPool, PoolTimeoutError

if TYPE_CHECKING:
    from collections.abc import Generator


# ---------------------------------------------------------------------------
# Entity definitions (for Database-level pool integration tests)
# ---------------------------------------------------------------------------


class PoolArticle(Entity):
    _table_ = "pool_article"
    id: PK[int]
    title: Req[str]
    score: Req[int]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db() -> Generator[Database, None, None]:
    _db = Database(entities=[PoolArticle])
    _db.bind("sqlite", ":memory:")
    _db.generate_mapping(create_tables=True)
    yield _db
    _db.close()


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def _make_sqlite_conn(check_same_thread: bool = True) -> Any:
    import sqlite3  # noqa: PLC0415

    return sqlite3.connect(":memory:", check_same_thread=check_same_thread)


async def _make_async_sqlite_conn() -> Any:
    try:
        import aiosqlite  # noqa: PLC0415

        return await aiosqlite.connect(":memory:")
    except ImportError:
        # Fall back to a minimal async-compatible wrapper around sqlite3
        import sqlite3  # noqa: PLC0415

        conn = sqlite3.connect(":memory:")

        class _FakeAsync:
            def __init__(self, c: Any) -> None:
                self._c = c

            async def close(self) -> None:
                self._c.close()

        return _FakeAsync(conn)


# ---------------------------------------------------------------------------
# ConnectionPool tests
# ---------------------------------------------------------------------------


def test_connection_pool_basic_acquire_release() -> None:
    pool: ConnectionPool = ConnectionPool(lambda: _make_sqlite_conn(), min_size=1, max_size=2)
    conn = pool.acquire()
    assert conn is not None
    assert pool.pool_size == 1
    pool.release(conn)
    assert pool.idle_count == 1
    pool.close_all()


def test_connection_pool_reuses_connections() -> None:
    calls: list[int] = []

    def factory() -> Any:
        calls.append(1)
        return _make_sqlite_conn()

    pool: ConnectionPool = ConnectionPool(factory, min_size=1, max_size=2)
    # Pre-created 1 connection in __init__
    assert len(calls) == 1
    conn = pool.acquire()  # gets the pre-created one
    pool.release(conn)
    conn2 = pool.acquire()  # gets it back from the queue
    assert len(calls) == 1  # no new connection created
    pool.release(conn2)
    pool.close_all()


def test_connection_pool_creates_up_to_max() -> None:
    pool: ConnectionPool = ConnectionPool(_make_sqlite_conn, min_size=0, max_size=2)
    c1 = pool.acquire()
    c2 = pool.acquire()
    assert pool.pool_size == 2
    pool.release(c1)
    pool.release(c2)
    pool.close_all()


def test_connection_pool_timeout_raises() -> None:
    pool: ConnectionPool = ConnectionPool(_make_sqlite_conn, min_size=1, max_size=1, timeout=0.05)
    conn = pool.acquire()
    with pytest.raises(PoolTimeoutError):
        pool.acquire()  # pool exhausted, timeout 50 ms
    pool.release(conn)
    pool.close_all()


def test_connection_pool_idle_count() -> None:
    pool: ConnectionPool = ConnectionPool(_make_sqlite_conn, min_size=2, max_size=3)
    assert pool.idle_count == 2
    c = pool.acquire()
    assert pool.idle_count == 1
    pool.release(c)
    assert pool.idle_count == 2
    pool.close_all()


def test_connection_pool_invalid_params() -> None:
    with pytest.raises(ValueError, match="min_size"):
        ConnectionPool(_make_sqlite_conn, min_size=-1, max_size=2)
    with pytest.raises(ValueError, match="max_size"):
        ConnectionPool(_make_sqlite_conn, min_size=0, max_size=0)
    with pytest.raises(ValueError, match="min_size must be <= max_size"):
        ConnectionPool(_make_sqlite_conn, min_size=3, max_size=1)


def test_connection_pool_thread_safety() -> None:
    """Multiple threads can acquire/release without error or deadlock."""
    # Use check_same_thread=False so connections can be closed from the main thread.
    pool: ConnectionPool = ConnectionPool(
        lambda: _make_sqlite_conn(check_same_thread=False), min_size=0, max_size=5
    )
    errors: list[Exception] = []

    def worker() -> None:
        try:
            conn = pool.acquire()
            pool.release(conn)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    pool.close_all()


def test_database_bind_with_pool(db: Database) -> None:
    """Database.bind() with pool_max=2 creates a ConnectionPool."""
    db2 = Database(entities=[PoolArticle])
    db2.bind("sqlite", ":memory:", pool_max=2)
    assert db2._pool is not None
    db2.generate_mapping(create_tables=True)
    art = PoolArticle(title="Pooled", score=42)
    db2.save(art)
    results = db2.select(PoolArticle).fetch_all()
    assert len(results) == 1
    assert results[0].title == "Pooled"
    db2.close()
    assert db2._pool is None or db2._pool.idle_count == 0


def test_database_pool_min_size_prewarms(db: Database) -> None:
    """pool_min=1 pre-creates one connection at bind time."""
    db2 = Database(entities=[PoolArticle])
    db2.bind("sqlite", ":memory:", pool_min=1, pool_max=3)
    assert db2._pool is not None
    assert db2._pool.pool_size >= 1
    db2.generate_mapping(create_tables=True)
    db2.close()


def test_pool_timeout_error_exported_from_init() -> None:
    """PoolTimeoutError is accessible from the top-level nextorm package."""
    from nextorm import PoolTimeoutError as _PTE  # noqa: PLC0415

    assert _PTE is PoolTimeoutError


def test_connection_pool_exported_from_init() -> None:
    """ConnectionPool and AsyncConnectionPool are accessible from nextorm."""
    from nextorm import AsyncConnectionPool as _ACP  # noqa: PLC0415
    from nextorm import ConnectionPool as _CP  # noqa: PLC0415

    assert _CP is ConnectionPool
    assert _ACP is AsyncConnectionPool


# ---------------------------------------------------------------------------
# AsyncConnectionPool tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_pool_start_acquire_release() -> None:
    pool: AsyncConnectionPool = AsyncConnectionPool(_make_async_sqlite_conn, min_size=1, max_size=2)
    await pool.start()
    assert pool.pool_size == 1
    conn = await pool.acquire()
    assert conn is not None
    await pool.release(conn)
    assert pool.idle_count == 1
    await pool.close_all()


@pytest.mark.asyncio
async def test_async_pool_creates_on_demand() -> None:
    pool: AsyncConnectionPool = AsyncConnectionPool(_make_async_sqlite_conn, min_size=0, max_size=2)
    await pool.start()
    c1 = await pool.acquire()
    c2 = await pool.acquire()
    assert pool.pool_size == 2
    await pool.release(c1)
    await pool.release(c2)
    await pool.close_all()


@pytest.mark.asyncio
async def test_async_pool_timeout_raises() -> None:
    pool: AsyncConnectionPool = AsyncConnectionPool(
        _make_async_sqlite_conn, min_size=1, max_size=1, timeout=0.05
    )
    await pool.start()
    conn = await pool.acquire()
    with pytest.raises(PoolTimeoutError):
        await pool.acquire()
    await pool.release(conn)
    await pool.close_all()


@pytest.mark.asyncio
async def test_async_pool_invalid_params() -> None:
    with pytest.raises(ValueError, match="min_size"):
        AsyncConnectionPool(_make_async_sqlite_conn, min_size=-1, max_size=2)
    with pytest.raises(ValueError, match="max_size"):
        AsyncConnectionPool(_make_async_sqlite_conn, min_size=0, max_size=0)
    with pytest.raises(ValueError, match="min_size must be <= max_size"):
        AsyncConnectionPool(_make_async_sqlite_conn, min_size=3, max_size=1)
