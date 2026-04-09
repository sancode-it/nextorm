"""Tests for nextorm.providers.base and nextorm.providers.sqlite."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Coroutine

import pytest

from nextorm.providers.base import (
    _PROVIDER_REGISTRY,
    AsyncConnection,
    AsyncCursor,
    AsyncProvider,
    ProviderBase,
    SyncConnection,
    SyncCursor,
    SyncProvider,
    get_async_provider,
    get_sync_provider,
    register_provider,
    registered_providers,
)
from nextorm.providers.sqlite import (
    SQLiteAsyncConnection,
    SQLiteAsyncCursor,
    SQLiteAsyncProvider,
    SQLiteSyncConnection,
    SQLiteSyncCursor,
    SQLiteSyncProvider,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run(coro: Coroutine[Any, Any, None]) -> None:
    """Run a coroutine synchronously in a fresh event loop."""
    asyncio.run(coro)


# ---------------------------------------------------------------------------
# Registry (base.py)
# ---------------------------------------------------------------------------


def test_registered_providers_includes_sqlite() -> None:
    assert "sqlite" in registered_providers()


def test_registered_providers_returns_sorted_list() -> None:
    providers = registered_providers()
    assert providers == sorted(providers)


def test_get_sync_provider_sqlite() -> None:
    cls = get_sync_provider("sqlite")
    assert cls is SQLiteSyncProvider


def test_get_async_provider_sqlite() -> None:
    cls = get_async_provider("sqlite")
    assert cls is SQLiteAsyncProvider


def test_get_sync_provider_unknown_raises() -> None:
    with pytest.raises(ValueError, match="No sync provider registered"):
        get_sync_provider("__nonexistent__")


def test_get_async_provider_unknown_raises() -> None:
    with pytest.raises(ValueError, match="No async provider registered"):
        get_async_provider("__nonexistent__")


def test_get_sync_provider_no_sync_raises() -> None:
    """A provider registered with only async_ raises for get_sync_provider."""

    class _DummyAsync(AsyncProvider):
        name = "_dummy_async_only"
        param_style = "qmark"

        def placeholder(self, param_name: str | None = None) -> str:
            return "?"

        async def connect(self, *args: Any, **kwargs: Any) -> AsyncConnection:
            raise NotImplementedError

        async def execute_ddl(self, connection: AsyncConnection, statements: list[str]) -> None:
            raise NotImplementedError

    # Register with only async
    register_provider("_dummy_async_only", async_=_DummyAsync)
    try:
        with pytest.raises(ValueError, match="No sync provider"):
            get_sync_provider("_dummy_async_only")
    finally:
        del _PROVIDER_REGISTRY["_dummy_async_only"]


def test_get_async_provider_no_async_raises() -> None:
    """A provider registered with only sync raises for get_async_provider."""

    class _DummySync(SyncProvider):
        name = "_dummy_sync_only"
        param_style = "qmark"

        def placeholder(self, param_name: str | None = None) -> str:
            return "?"

        def connect(self, *args: Any, **kwargs: Any) -> SyncConnection:
            raise NotImplementedError

        def execute_ddl(self, connection: SyncConnection, statements: list[str]) -> None:
            pass

    register_provider("_dummy_sync_only", sync=_DummySync)
    try:
        with pytest.raises(ValueError, match="No async provider"):
            get_async_provider("_dummy_sync_only")
    finally:
        del _PROVIDER_REGISTRY["_dummy_sync_only"]


def test_register_provider_requires_at_least_one() -> None:
    with pytest.raises(ValueError, match="must supply at least one"):
        register_provider("_bad_register")


# ---------------------------------------------------------------------------
# SQLiteSyncProvider — metadata
# ---------------------------------------------------------------------------


def test_sync_provider_name() -> None:
    assert SQLiteSyncProvider.name == "sqlite"


def test_sync_provider_param_style() -> None:
    assert SQLiteSyncProvider.param_style == "qmark"


def test_sync_provider_placeholder() -> None:
    p = SQLiteSyncProvider()
    assert p.placeholder() == "?"
    assert p.placeholder("user_id") == "?"


# ---------------------------------------------------------------------------
# SQLiteSyncProvider — connect & cursor
# ---------------------------------------------------------------------------


def test_sync_connect_returns_connection() -> None:
    p = SQLiteSyncProvider()
    conn = p.connect(":memory:")
    assert isinstance(conn, SQLiteSyncConnection)
    conn.close()


def test_sync_connection_raw_property() -> None:
    import sqlite3

    p = SQLiteSyncProvider()
    conn = p.connect(":memory:")
    assert isinstance(conn.raw, sqlite3.Connection)
    conn.close()


def test_sync_connection_cursor_type() -> None:
    p = SQLiteSyncProvider()
    conn = p.connect(":memory:")
    cur = conn.cursor()
    assert isinstance(cur, SQLiteSyncCursor)
    cur.close()
    conn.close()


# ---------------------------------------------------------------------------
# SQLiteSyncCursor — properties and operations
# ---------------------------------------------------------------------------


def test_sync_cursor_execute_and_fetchone() -> None:
    p = SQLiteSyncProvider()
    conn = p.connect(":memory:")
    cur = conn.cursor()
    cur.execute("SELECT 42")
    row = cur.fetchone()
    assert row == (42,)
    cur.close()
    conn.close()


def test_sync_cursor_fetchall() -> None:
    p = SQLiteSyncProvider()
    conn = p.connect(":memory:")
    cur = conn.cursor()
    cur.execute("CREATE TABLE t (v INTEGER)")
    cur.execute("INSERT INTO t VALUES (1)")
    cur.execute("INSERT INTO t VALUES (2)")
    cur.execute("SELECT v FROM t ORDER BY v")
    rows = cur.fetchall()
    assert list(rows) == [(1,), (2,)]
    cur.close()
    conn.close()


def test_sync_cursor_fetchmany() -> None:
    p = SQLiteSyncProvider()
    conn = p.connect(":memory:")
    cur = conn.cursor()
    cur.execute("CREATE TABLE t (v INTEGER)")
    for i in range(5):
        cur.execute("INSERT INTO t VALUES (?)", (i,))
    cur.execute("SELECT v FROM t ORDER BY v")
    rows = cur.fetchmany(3)
    assert len(list(rows)) == 3
    cur.close()
    conn.close()


def test_sync_cursor_executemany() -> None:
    p = SQLiteSyncProvider()
    conn = p.connect(":memory:")
    cur = conn.cursor()
    cur.execute("CREATE TABLE t (v INTEGER)")
    cur.executemany("INSERT INTO t VALUES (?)", [(1,), (2,), (3,)])
    cur.execute("SELECT COUNT(*) FROM t")
    row = cur.fetchone()
    assert row == (3,)
    cur.close()
    conn.close()


def test_sync_cursor_description_after_select() -> None:
    p = SQLiteSyncProvider()
    conn = p.connect(":memory:")
    cur = conn.cursor()
    cur.execute("SELECT 1 AS x")
    assert cur.description is not None
    assert cur.description[0][0] == "x"
    cur.close()
    conn.close()


def test_sync_cursor_description_before_execute() -> None:
    p = SQLiteSyncProvider()
    conn = p.connect(":memory:")
    cur = conn.cursor()
    assert cur.description is None
    cur.close()
    conn.close()


def test_sync_cursor_rowcount_after_insert() -> None:
    p = SQLiteSyncProvider()
    conn = p.connect(":memory:")
    cur = conn.cursor()
    cur.execute("CREATE TABLE t (v INTEGER)")
    cur.execute("INSERT INTO t VALUES (1)")
    assert cur.rowcount == 1
    cur.close()
    conn.close()


def test_sync_cursor_lastrowid_after_insert() -> None:
    p = SQLiteSyncProvider()
    conn = p.connect(":memory:")
    cur = conn.cursor()
    cur.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, v INTEGER)")
    cur.execute("INSERT INTO t (v) VALUES (99)")
    assert cur.lastrowid == 1
    cur.close()
    conn.close()


# ---------------------------------------------------------------------------
# SQLiteSyncConnection — commit / rollback / executescript
# ---------------------------------------------------------------------------


def test_sync_connection_commit() -> None:
    p = SQLiteSyncProvider()
    conn = p.connect(":memory:")
    cur = conn.cursor()
    cur.execute("CREATE TABLE t (v INTEGER)")
    conn.commit()
    cur.close()
    conn.close()


def test_sync_connection_rollback() -> None:
    p = SQLiteSyncProvider()
    conn = p.connect(":memory:")
    cur = conn.cursor()
    cur.execute("CREATE TABLE t (v INTEGER)")
    cur.execute("INSERT INTO t VALUES (1)")
    conn.rollback()
    cur.close()
    conn.close()


def test_sync_connection_executescript() -> None:
    p = SQLiteSyncProvider()
    conn = p.connect(":memory:")
    conn.executescript("CREATE TABLE t (v INTEGER); INSERT INTO t VALUES (7);")
    cur = conn.cursor()
    cur.execute("SELECT v FROM t")
    assert cur.fetchone() == (7,)
    cur.close()
    conn.close()


# ---------------------------------------------------------------------------
# SQLiteSyncProvider — execute_ddl
# ---------------------------------------------------------------------------


def test_sync_execute_ddl_empty_is_noop() -> None:
    """execute_ddl with no statements must not raise."""
    p = SQLiteSyncProvider()
    conn = p.connect(":memory:")
    p.execute_ddl(conn, [])  # must not raise
    conn.close()


def test_sync_execute_ddl_creates_table() -> None:
    p = SQLiteSyncProvider()
    conn = p.connect(":memory:")
    stmts = ["CREATE TABLE IF NOT EXISTS user (id INTEGER PRIMARY KEY, name TEXT NOT NULL)"]
    p.execute_ddl(conn, stmts)
    # Verify the table exists
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user'")
    assert cur.fetchone() == ("user",)
    cur.close()
    conn.close()


def test_sync_execute_ddl_multiple_statements() -> None:
    p = SQLiteSyncProvider()
    conn = p.connect(":memory:")
    stmts = [
        "CREATE TABLE IF NOT EXISTS a (id INTEGER PRIMARY KEY)",
        "CREATE TABLE IF NOT EXISTS b (id INTEGER PRIMARY KEY)",
    ]
    p.execute_ddl(conn, stmts)
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    names = [row[0] for row in cur.fetchall()]
    assert "a" in names and "b" in names
    cur.close()
    conn.close()


def test_sync_execute_ddl_statements_with_trailing_semicolons() -> None:
    """execute_ddl strips trailing semicolons before joining to avoid double-;;."""
    p = SQLiteSyncProvider()
    conn = p.connect(":memory:")
    stmts = ["CREATE TABLE IF NOT EXISTS c (id INTEGER PRIMARY KEY);"]  # has semicolon
    p.execute_ddl(conn, stmts)  # must not raise
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='c'")
    assert cur.fetchone() is not None
    cur.close()
    conn.close()


# ---------------------------------------------------------------------------
# SQLiteAsyncProvider — metadata
# ---------------------------------------------------------------------------


def test_async_provider_name() -> None:
    assert SQLiteAsyncProvider.name == "sqlite"


def test_async_provider_param_style() -> None:
    assert SQLiteAsyncProvider.param_style == "qmark"


def test_async_provider_placeholder() -> None:
    p = SQLiteAsyncProvider()
    assert p.placeholder() == "?"
    assert p.placeholder("col") == "?"


# ---------------------------------------------------------------------------
# SQLiteAsyncProvider — connect & cursor
# ---------------------------------------------------------------------------


def test_async_connect_returns_connection() -> None:
    async def _run() -> None:
        p = SQLiteAsyncProvider()
        conn = await p.connect(":memory:")
        assert isinstance(conn, SQLiteAsyncConnection)
        await conn.close()

    run(_run())


def test_async_connection_raw_property() -> None:
    import aiosqlite

    async def _run() -> None:
        p = SQLiteAsyncProvider()
        conn = await p.connect(":memory:")
        assert isinstance(conn.raw, aiosqlite.Connection)
        await conn.close()

    run(_run())


def test_async_connection_cursor_type() -> None:
    async def _run() -> None:
        p = SQLiteAsyncProvider()
        conn = await p.connect(":memory:")
        cur = await conn.cursor()
        assert isinstance(cur, SQLiteAsyncCursor)
        await cur.close()
        await conn.close()

    run(_run())


# ---------------------------------------------------------------------------
# SQLiteAsyncCursor — properties and operations
# ---------------------------------------------------------------------------


def test_async_cursor_execute_and_fetchone() -> None:
    async def _run() -> None:
        p = SQLiteAsyncProvider()
        conn = await p.connect(":memory:")
        cur = await conn.cursor()
        await cur.execute("SELECT 42")
        row = await cur.fetchone()
        assert row == (42,)
        await cur.close()
        await conn.close()

    run(_run())


def test_async_cursor_fetchall() -> None:
    async def _run() -> None:
        p = SQLiteAsyncProvider()
        conn = await p.connect(":memory:")
        cur = await conn.cursor()
        await cur.execute("CREATE TABLE t (v INTEGER)")
        await cur.execute("INSERT INTO t VALUES (1)")
        await cur.execute("INSERT INTO t VALUES (2)")
        await cur.execute("SELECT v FROM t ORDER BY v")
        rows = await cur.fetchall()
        assert list(rows) == [(1,), (2,)]
        await cur.close()
        await conn.close()

    run(_run())


def test_async_cursor_fetchmany() -> None:
    async def _run() -> None:
        p = SQLiteAsyncProvider()
        conn = await p.connect(":memory:")
        cur = await conn.cursor()
        await cur.execute("CREATE TABLE t (v INTEGER)")
        for i in range(5):
            await cur.execute("INSERT INTO t VALUES (?)", (i,))
        await cur.execute("SELECT v FROM t ORDER BY v")
        rows = await cur.fetchmany(3)
        assert len(list(rows)) == 3
        await cur.close()
        await conn.close()

    run(_run())


def test_async_cursor_executemany() -> None:
    async def _run() -> None:
        p = SQLiteAsyncProvider()
        conn = await p.connect(":memory:")
        cur = await conn.cursor()
        await cur.execute("CREATE TABLE t (v INTEGER)")
        await cur.executemany("INSERT INTO t VALUES (?)", [(1,), (2,), (3,)])
        await cur.execute("SELECT COUNT(*) FROM t")
        row = await cur.fetchone()
        assert row == (3,)
        await cur.close()
        await conn.close()

    run(_run())


def test_async_cursor_description_after_select() -> None:
    async def _run() -> None:
        p = SQLiteAsyncProvider()
        conn = await p.connect(":memory:")
        cur = await conn.cursor()
        await cur.execute("SELECT 1 AS y")
        assert cur.description is not None
        assert cur.description[0][0] == "y"
        await cur.close()
        await conn.close()

    run(_run())


def test_async_cursor_description_before_execute() -> None:
    async def _run() -> None:
        p = SQLiteAsyncProvider()
        conn = await p.connect(":memory:")
        cur = await conn.cursor()
        assert cur.description is None
        await cur.close()
        await conn.close()

    run(_run())


def test_async_cursor_rowcount_after_insert() -> None:
    async def _run() -> None:
        p = SQLiteAsyncProvider()
        conn = await p.connect(":memory:")
        cur = await conn.cursor()
        await cur.execute("CREATE TABLE t (v INTEGER)")
        await cur.execute("INSERT INTO t VALUES (1)")
        assert cur.rowcount == 1
        await cur.close()
        await conn.close()

    run(_run())


def test_async_cursor_lastrowid_after_insert() -> None:
    async def _run() -> None:
        p = SQLiteAsyncProvider()
        conn = await p.connect(":memory:")
        cur = await conn.cursor()
        await cur.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, v INTEGER)")
        await cur.execute("INSERT INTO t (v) VALUES (99)")
        assert cur.lastrowid == 1
        await cur.close()
        await conn.close()

    run(_run())


# ---------------------------------------------------------------------------
# SQLiteAsyncConnection — commit / rollback / executescript
# ---------------------------------------------------------------------------


def test_async_connection_commit() -> None:
    async def _run() -> None:
        p = SQLiteAsyncProvider()
        conn = await p.connect(":memory:")
        cur = await conn.cursor()
        await cur.execute("CREATE TABLE t (v INTEGER)")
        await conn.commit()
        await cur.close()
        await conn.close()

    run(_run())


def test_async_connection_rollback() -> None:
    async def _run() -> None:
        p = SQLiteAsyncProvider()
        conn = await p.connect(":memory:")
        cur = await conn.cursor()
        await cur.execute("CREATE TABLE t (v INTEGER)")
        await cur.execute("INSERT INTO t VALUES (1)")
        await conn.rollback()
        await cur.close()
        await conn.close()

    run(_run())


def test_async_connection_executescript() -> None:
    async def _run() -> None:
        p = SQLiteAsyncProvider()
        conn = await p.connect(":memory:")
        await conn.executescript("CREATE TABLE t (v INTEGER); INSERT INTO t VALUES (7);")
        cur = await conn.cursor()
        await cur.execute("SELECT v FROM t")
        row = await cur.fetchone()
        assert row == (7,)
        await cur.close()
        await conn.close()

    run(_run())


# ---------------------------------------------------------------------------
# SQLiteAsyncProvider — execute_ddl
# ---------------------------------------------------------------------------


def test_async_execute_ddl_empty_is_noop() -> None:
    async def _run() -> None:
        p = SQLiteAsyncProvider()
        conn = await p.connect(":memory:")
        await p.execute_ddl(conn, [])  # must not raise
        await conn.close()

    run(_run())


def test_async_execute_ddl_creates_table() -> None:
    async def _run() -> None:
        p = SQLiteAsyncProvider()
        conn = await p.connect(":memory:")
        stmts = ["CREATE TABLE IF NOT EXISTS user (id INTEGER PRIMARY KEY, name TEXT NOT NULL)"]
        await p.execute_ddl(conn, stmts)
        cur = await conn.cursor()
        await cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user'")
        row = await cur.fetchone()
        assert row == ("user",)
        await cur.close()
        await conn.close()

    run(_run())


def test_async_execute_ddl_multiple_statements() -> None:
    async def _run() -> None:
        p = SQLiteAsyncProvider()
        conn = await p.connect(":memory:")
        stmts = [
            "CREATE TABLE IF NOT EXISTS a (id INTEGER PRIMARY KEY)",
            "CREATE TABLE IF NOT EXISTS b (id INTEGER PRIMARY KEY)",
        ]
        await p.execute_ddl(conn, stmts)
        cur = await conn.cursor()
        await cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        names = [row[0] for row in await cur.fetchall()]
        assert "a" in names and "b" in names
        await cur.close()
        await conn.close()

    run(_run())


def test_async_execute_ddl_statements_with_trailing_semicolons() -> None:
    async def _run() -> None:
        p = SQLiteAsyncProvider()
        conn = await p.connect(":memory:")
        stmts = ["CREATE TABLE IF NOT EXISTS c (id INTEGER PRIMARY KEY);"]
        await p.execute_ddl(conn, stmts)  # must not raise
        cur = await conn.cursor()
        await cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='c'")
        assert await cur.fetchone() is not None
        await cur.close()
        await conn.close()

    run(_run())


# ---------------------------------------------------------------------------
# ProviderBase helpers (abstract, covered via concrete subclasses above)
# ---------------------------------------------------------------------------


def test_provider_base_is_abstract() -> None:
    """Direct instantiation of ProviderBase must raise TypeError."""
    with pytest.raises(TypeError):
        ProviderBase()  # type: ignore[abstract]


def test_sync_provider_is_abstract() -> None:
    with pytest.raises(TypeError):
        SyncProvider()  # type: ignore[abstract]


def test_async_provider_is_abstract() -> None:
    with pytest.raises(TypeError):
        AsyncProvider()  # type: ignore[abstract]


def test_sync_cursor_is_abstract() -> None:
    with pytest.raises(TypeError):
        SyncCursor()  # type: ignore[abstract]


def test_async_cursor_is_abstract() -> None:
    with pytest.raises(TypeError):
        AsyncCursor()  # type: ignore[abstract]


def test_sync_connection_is_abstract() -> None:
    with pytest.raises(TypeError):
        SyncConnection()  # type: ignore[abstract]


def test_async_connection_is_abstract() -> None:
    with pytest.raises(TypeError):
        AsyncConnection()  # type: ignore[abstract]
