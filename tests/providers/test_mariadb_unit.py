"""Unit tests for nextorm.providers.mariadb — no live DB required.

These tests cover the wrapper classes (cursor, connection, provider) using
:mod:`unittest.mock` to simulate the underlying PyMySQL / aiomysql objects.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from nextorm.providers.base import SyncCursor
from nextorm.providers.mariadb import (
    MariaDBAsyncConnection,
    MariaDBAsyncCursor,
    MariaDBAsyncProvider,
    MariaDBSyncConnection,
    MariaDBSyncCursor,
    MariaDBSyncProvider,
)
from nextorm.schema.introspect import introspect_mariadb

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run(coro: Any) -> None:
    asyncio.run(coro)


def _mock_sync_cursor() -> MagicMock:
    cur = MagicMock()
    cur.description = None
    cur.rowcount = 0
    cur.lastrowid = None
    cur.fetchone.return_value = (1,)
    cur.fetchmany.return_value = [(1,), (2,)]
    cur.fetchall.return_value = [(1,), (2,), (3,)]
    return cur


def _mock_sync_conn(cursor: MagicMock | None = None) -> MagicMock:
    conn = MagicMock()
    conn.cursor.return_value = cursor or _mock_sync_cursor()
    return conn


# ---------------------------------------------------------------------------
# MariaDBSyncCursor
# ---------------------------------------------------------------------------


def test_sync_cursor_description() -> None:
    mock_cur = _mock_sync_cursor()
    mock_cur.description = (("x", None, None, None, None, None, None),)
    cursor = MariaDBSyncCursor(mock_cur)
    assert cursor.description is not None
    assert cursor.description[0][0] == "x"


def test_sync_cursor_description_none() -> None:
    mock_cur = _mock_sync_cursor()
    mock_cur.description = None
    cursor = MariaDBSyncCursor(mock_cur)
    assert cursor.description is None


def test_sync_cursor_rowcount() -> None:
    mock_cur = _mock_sync_cursor()
    mock_cur.rowcount = 5
    cursor = MariaDBSyncCursor(mock_cur)
    assert cursor.rowcount == 5


def test_sync_cursor_lastrowid() -> None:
    mock_cur = _mock_sync_cursor()
    mock_cur.lastrowid = 42
    cursor = MariaDBSyncCursor(mock_cur)
    assert cursor.lastrowid == 42


def test_sync_cursor_execute() -> None:
    mock_cur = _mock_sync_cursor()
    cursor = MariaDBSyncCursor(mock_cur)
    cursor.execute("SELECT 1")
    mock_cur.execute.assert_called_once_with("SELECT 1", ())


def test_sync_cursor_execute_with_params() -> None:
    mock_cur = _mock_sync_cursor()
    cursor = MariaDBSyncCursor(mock_cur)
    cursor.execute("SELECT %s", (1,))
    mock_cur.execute.assert_called_once_with("SELECT %s", (1,))


def test_sync_cursor_executemany() -> None:
    mock_cur = _mock_sync_cursor()
    cursor = MariaDBSyncCursor(mock_cur)
    cursor.executemany("INSERT INTO t VALUES (%s)", [[1], [2]])
    mock_cur.executemany.assert_called_once()


def test_sync_cursor_fetchone() -> None:
    mock_cur = _mock_sync_cursor()
    cursor = MariaDBSyncCursor(mock_cur)
    assert cursor.fetchone() == (1,)


def test_sync_cursor_fetchmany() -> None:
    mock_cur = _mock_sync_cursor()
    cursor = MariaDBSyncCursor(mock_cur)
    rows = cursor.fetchmany(2)
    assert len(rows) == 2


def test_sync_cursor_fetchall() -> None:
    mock_cur = _mock_sync_cursor()
    cursor = MariaDBSyncCursor(mock_cur)
    rows = cursor.fetchall()
    assert len(rows) == 3


def test_sync_cursor_close() -> None:
    mock_cur = _mock_sync_cursor()
    cursor = MariaDBSyncCursor(mock_cur)
    cursor.close()
    mock_cur.close.assert_called_once()


# ---------------------------------------------------------------------------
# MariaDBSyncConnection
# ---------------------------------------------------------------------------


def test_sync_connection_cursor() -> None:
    mock_cur = _mock_sync_cursor()
    conn = MariaDBSyncConnection(_mock_sync_conn(mock_cur))
    result = conn.cursor()
    assert isinstance(result, MariaDBSyncCursor)


def test_sync_connection_commit() -> None:
    mock_conn = _mock_sync_conn()
    conn = MariaDBSyncConnection(mock_conn)
    conn.commit()
    mock_conn.commit.assert_called_once()


def test_sync_connection_rollback() -> None:
    mock_conn = _mock_sync_conn()
    conn = MariaDBSyncConnection(mock_conn)
    conn.rollback()
    mock_conn.rollback.assert_called_once()


def test_sync_connection_close() -> None:
    mock_conn = _mock_sync_conn()
    conn = MariaDBSyncConnection(mock_conn)
    conn.close()
    mock_conn.close.assert_called_once()


def test_sync_connection_executescript() -> None:
    mock_cur = _mock_sync_cursor()
    mock_conn = _mock_sync_conn(mock_cur)
    conn = MariaDBSyncConnection(mock_conn)
    conn.executescript("CREATE TABLE t (v INT); INSERT INTO t VALUES (1)")
    assert mock_cur.execute.call_count == 2


def test_sync_connection_executescript_skips_empty() -> None:
    mock_cur = _mock_sync_cursor()
    mock_conn = _mock_sync_conn(mock_cur)
    conn = MariaDBSyncConnection(mock_conn)
    conn.executescript("SELECT 1;  ;  SELECT 2")
    assert mock_cur.execute.call_count == 2


def test_sync_connection_raw() -> None:
    mock_conn = _mock_sync_conn()
    conn = MariaDBSyncConnection(mock_conn)
    assert conn.raw is mock_conn


# ---------------------------------------------------------------------------
# MariaDBSyncProvider
# ---------------------------------------------------------------------------


def test_sync_provider_placeholder_unit() -> None:
    p = MariaDBSyncProvider()
    assert p.placeholder() == "%s"
    assert p.placeholder("x") == "%s"


def test_sync_provider_name_unit() -> None:
    assert MariaDBSyncProvider.name == "mariadb"
    assert MariaDBSyncProvider.param_style == "format"


def test_sync_provider_connect_unit() -> None:
    mock_raw = _mock_sync_conn()
    with patch("pymysql.connect", return_value=mock_raw):
        p = MariaDBSyncProvider()
        conn = p.connect(host="localhost", user="root", password="", database="test")
        assert isinstance(conn, MariaDBSyncConnection)


def test_sync_execute_ddl_empty_unit() -> None:
    mock_conn = MariaDBSyncConnection(_mock_sync_conn())
    p = MariaDBSyncProvider()
    p.execute_ddl(mock_conn, [])  # must not raise


def test_sync_execute_ddl_statements_unit() -> None:
    mock_cur = _mock_sync_cursor()
    mock_raw = _mock_sync_conn(mock_cur)
    conn = MariaDBSyncConnection(mock_raw)
    p = MariaDBSyncProvider()
    p.execute_ddl(conn, ["CREATE TABLE t (v INT)", "CREATE TABLE u (v INT)"])
    assert mock_cur.execute.call_count == 2


def test_sync_execute_ddl_strips_semicolons_unit() -> None:
    mock_cur = _mock_sync_cursor()
    mock_raw = _mock_sync_conn(mock_cur)
    conn = MariaDBSyncConnection(mock_raw)
    p = MariaDBSyncProvider()
    p.execute_ddl(conn, ["CREATE TABLE t (v INT);"])
    mock_cur.execute.assert_called_once_with("CREATE TABLE t (v INT)")


# ---------------------------------------------------------------------------
# introspect_mariadb unit (mock)
# ---------------------------------------------------------------------------


def test_introspect_mariadb_unit() -> None:
    """Unit test for introspect_mariadb using a mock connection."""

    class _FakeCursor(SyncCursor):
        def __init__(self) -> None:
            self._calls: int = 0

        @property
        def description(self) -> Any:
            return None

        @property
        def rowcount(self) -> int:
            return 0

        @property
        def lastrowid(self) -> int | None:
            return None

        def execute(self, sql: str, parameters: Any = ()) -> None:
            pass

        def executemany(self, sql: str, seq_of_parameters: Any) -> None:
            pass

        def fetchone(self) -> Any:
            return ("test_db",)  # for SELECT DATABASE()

        def fetchmany(self, size: int = 1) -> Any:
            return []

        def fetchall(self) -> list[Any]:
            self._calls += 1
            if self._calls == 1:
                return [("post",)]  # table names
            if self._calls == 2:
                return [("id", "NO"), ("title", "YES")]  # columns
            if self._calls == 3:
                return [("idx_post__title", 0, "title")]  # indexes
            return []

        def close(self) -> None:
            pass

    class _FakeConn(MariaDBSyncConnection):
        def __init__(self) -> None:
            self._fake_cur = _FakeCursor()
            self._conn = sqlite3.connect(":memory:")  # type: ignore[assignment]

        def cursor(self) -> _FakeCursor:  # type: ignore[override]
            return self._fake_cur

    result = introspect_mariadb(_FakeConn())
    assert "post" in result
    col_names = [c.name for c in result["post"].columns]
    assert "id" in col_names
    assert "title" in col_names
    assert result["post"].columns[0].nullable is False
    assert result["post"].columns[1].nullable is True
    assert len(result["post"].indexes) == 1
    assert result["post"].indexes[0].name == "idx_post__title"
    assert result["post"].indexes[0].unique is False


# ---------------------------------------------------------------------------
# MariaDBAsyncCursor
# ---------------------------------------------------------------------------


def _mock_async_cursor() -> MagicMock:
    cur = AsyncMock()
    cur.description = None
    cur.rowcount = 0
    cur.lastrowid = None
    cur.fetchone.return_value = (1,)
    cur.fetchmany.return_value = [(1,), (2,)]
    cur.fetchall.return_value = [(1,), (2,), (3,)]
    return cur


def _mock_async_conn(cursor: MagicMock | None = None) -> MagicMock:
    conn = MagicMock()
    conn.cursor.return_value = cursor or _mock_async_cursor()
    conn.commit = AsyncMock()
    conn.rollback = AsyncMock()
    return conn


def test_async_cursor_description_unit() -> None:
    mock_cur = _mock_async_cursor()
    mock_cur.description = (("y", None, None, None, None, None, None),)
    cursor = MariaDBAsyncCursor(mock_cur)
    assert cursor.description is not None
    assert cursor.description[0][0] == "y"


def test_async_cursor_description_none_unit() -> None:
    mock_cur = _mock_async_cursor()
    mock_cur.description = None
    cursor = MariaDBAsyncCursor(mock_cur)
    assert cursor.description is None


def test_async_cursor_rowcount_unit() -> None:
    mock_cur = _mock_async_cursor()
    mock_cur.rowcount = 7
    cursor = MariaDBAsyncCursor(mock_cur)
    assert cursor.rowcount == 7


def test_async_cursor_lastrowid_unit() -> None:
    mock_cur = _mock_async_cursor()
    mock_cur.lastrowid = 99
    cursor = MariaDBAsyncCursor(mock_cur)
    assert cursor.lastrowid == 99


def test_async_cursor_execute_unit() -> None:
    async def _run() -> None:
        mock_cur = _mock_async_cursor()
        cursor = MariaDBAsyncCursor(mock_cur)
        await cursor.execute("SELECT 1")
        mock_cur.execute.assert_called_once_with("SELECT 1", ())

    run(_run())


def test_async_cursor_executemany_unit() -> None:
    async def _run() -> None:
        mock_cur = _mock_async_cursor()
        cursor = MariaDBAsyncCursor(mock_cur)
        await cursor.executemany("INSERT INTO t VALUES (%s)", [[1], [2]])
        mock_cur.executemany.assert_called_once()

    run(_run())


def test_async_cursor_fetchone_unit() -> None:
    async def _run() -> None:
        mock_cur = _mock_async_cursor()
        cursor = MariaDBAsyncCursor(mock_cur)
        row = await cursor.fetchone()
        assert row == (1,)

    run(_run())


def test_async_cursor_fetchmany_unit() -> None:
    async def _run() -> None:
        mock_cur = _mock_async_cursor()
        cursor = MariaDBAsyncCursor(mock_cur)
        rows = await cursor.fetchmany(2)
        assert len(rows) == 2

    run(_run())


def test_async_cursor_fetchall_unit() -> None:
    async def _run() -> None:
        mock_cur = _mock_async_cursor()
        cursor = MariaDBAsyncCursor(mock_cur)
        rows = await cursor.fetchall()
        assert len(rows) == 3

    run(_run())


def test_async_cursor_close_unit() -> None:
    async def _run() -> None:
        mock_cur = _mock_async_cursor()
        cursor = MariaDBAsyncCursor(mock_cur)
        await cursor.close()
        mock_cur.close.assert_called_once()

    run(_run())


# ---------------------------------------------------------------------------
# MariaDBAsyncConnection
# ---------------------------------------------------------------------------


def test_async_connection_cursor_unit() -> None:
    async def _run() -> None:
        mock_cur = _mock_async_cursor()
        mock_raw = _mock_async_conn(mock_cur)
        conn = MariaDBAsyncConnection(mock_raw)
        result = await conn.cursor()
        assert isinstance(result, MariaDBAsyncCursor)

    run(_run())


def test_async_connection_commit_unit() -> None:
    async def _run() -> None:
        mock_raw = _mock_async_conn()
        conn = MariaDBAsyncConnection(mock_raw)
        await conn.commit()
        mock_raw.commit.assert_called_once()

    run(_run())


def test_async_connection_rollback_unit() -> None:
    async def _run() -> None:
        mock_raw = _mock_async_conn()
        conn = MariaDBAsyncConnection(mock_raw)
        await conn.rollback()
        mock_raw.rollback.assert_called_once()

    run(_run())


def test_async_connection_close_unit() -> None:
    async def _run() -> None:
        mock_raw = MagicMock()  # close() is sync in asyncmy
        conn = MariaDBAsyncConnection(mock_raw)
        await conn.close()
        mock_raw.close.assert_called_once()

    run(_run())


def test_async_connection_executescript_unit() -> None:
    async def _run() -> None:
        mock_cur = _mock_async_cursor()
        mock_raw = _mock_async_conn(mock_cur)
        conn = MariaDBAsyncConnection(mock_raw)
        await conn.executescript("CREATE TABLE t (v INT); INSERT INTO t VALUES (1)")
        assert mock_cur.execute.call_count == 2

    run(_run())


def test_async_connection_executescript_skips_empty_unit() -> None:
    async def _run() -> None:
        mock_cur = _mock_async_cursor()
        mock_raw = _mock_async_conn(mock_cur)
        conn = MariaDBAsyncConnection(mock_raw)
        await conn.executescript("SELECT 1;  ;  SELECT 2")
        assert mock_cur.execute.call_count == 2

    run(_run())


def test_async_connection_raw_unit() -> None:
    mock_raw = _mock_async_conn()
    conn = MariaDBAsyncConnection(mock_raw)
    assert conn.raw is mock_raw


# ---------------------------------------------------------------------------
# MariaDBAsyncProvider
# ---------------------------------------------------------------------------


def test_async_provider_placeholder_unit() -> None:
    p = MariaDBAsyncProvider()
    assert p.placeholder() == "%s"
    assert p.placeholder("x") == "%s"


def test_async_provider_name_unit() -> None:
    assert MariaDBAsyncProvider.name == "mariadb"
    assert MariaDBAsyncProvider.param_style == "format"


def test_async_provider_connect_unit() -> None:
    async def _run() -> None:
        mock_raw = MagicMock()
        with patch("asyncmy.connect", new_callable=AsyncMock, return_value=mock_raw):
            p = MariaDBAsyncProvider()
            conn = await p.connect(host="localhost", user="root", password="", database="test")
            assert isinstance(conn, MariaDBAsyncConnection)

    run(_run())


def test_async_execute_ddl_empty_unit() -> None:
    async def _run() -> None:
        mock_raw = _mock_async_conn()
        conn = MariaDBAsyncConnection(mock_raw)
        p = MariaDBAsyncProvider()
        await p.execute_ddl(conn, [])  # must not raise

    run(_run())


def test_async_execute_ddl_statements_unit() -> None:
    async def _run() -> None:
        mock_cur = _mock_async_cursor()
        mock_raw = _mock_async_conn(mock_cur)
        conn = MariaDBAsyncConnection(mock_raw)
        p = MariaDBAsyncProvider()
        await p.execute_ddl(conn, ["CREATE TABLE t (v INT)", "CREATE TABLE u (v INT)"])
        assert mock_cur.execute.call_count == 2

    run(_run())


def test_async_execute_ddl_strips_semicolons_unit() -> None:
    async def _run() -> None:
        mock_cur = _mock_async_cursor()
        mock_raw = _mock_async_conn(mock_cur)
        conn = MariaDBAsyncConnection(mock_raw)
        p = MariaDBAsyncProvider()
        await p.execute_ddl(conn, ["CREATE TABLE t (v INT);"])
        mock_cur.execute.assert_called_once_with("CREATE TABLE t (v INT)")

    run(_run())


def test_sync_provider_introspect_unit() -> None:
    """Cover MariaDBSyncProvider.introspect() — delegates to introspect_mariadb."""

    class _FakeCursor(SyncCursor):
        def __init__(self) -> None:
            pass

        @property
        def description(self) -> Any:
            return None

        @property
        def rowcount(self) -> int:
            return 0

        @property
        def lastrowid(self) -> int | None:
            return None

        def execute(self, sql: str, parameters: Any = ()) -> None:
            pass

        def executemany(self, sql: str, seq_of_parameters: Any) -> None:
            pass

        def fetchone(self) -> Any:
            return ("test_db",)

        def fetchmany(self, size: int = 1) -> Any:
            return []

        def fetchall(self) -> list[Any]:
            return []

        def close(self) -> None:
            pass

    class _FakeConn(MariaDBSyncConnection):
        def __init__(self) -> None:
            self._conn = sqlite3.connect(":memory:")  # type: ignore[assignment]

        def cursor(self) -> _FakeCursor:  # type: ignore[override]
            return _FakeCursor()

    p = MariaDBSyncProvider()
    result = p.introspect(_FakeConn())
    assert isinstance(result, dict)
