"""Tests for nextorm.providers.mariadb.

Requires a live MySQL server. Connection is configured via the
``NEXTORM_MYSQL_*`` environment variables (see :data:`MARIADB_CONNECT_KWARGS`).
All tests are skipped automatically when the driver or server is unavailable.
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Coroutine

from nextorm.providers.base import (
    get_async_provider,
    get_sync_provider,
    registered_providers,
)
from nextorm.providers.mariadb import (
    MariaDBAsyncConnection,
    MariaDBAsyncProvider,
    MariaDBSyncConnection,
    MariaDBSyncProvider,
)
from nextorm.schema.core import Column, ForeignKey, Index, Table
from nextorm.schema.ddl import MariaDBRenderer
from nextorm.schema.introspect import introspect_mariadb

MARIADB_CONNECT_KWARGS: dict[str, Any] = {
    "host": os.environ.get("NEXTORM_MARIADB_HOST", "127.0.0.1"),
    "user": os.environ.get("NEXTORM_MARIADB_USER", "nextorm"),
    "password": os.environ.get("NEXTORM_MARIADB_PASSWORD", "nextorm"),
    "database": os.environ.get("NEXTORM_MARIADB_DATABASE", "nextorm_test"),
}


def _can_connect() -> bool:
    try:
        import pymysql  # noqa: PLC0415

        conn = pymysql.connect(**MARIADB_CONNECT_KWARGS)
        conn.close()
        return True
    except Exception:
        return False


# Skip all tests in this module if no live DB is available
pytestmark = pytest.mark.skipif(
    not _can_connect(),
    reason="No MySQL server reachable (set NEXTORM_MYSQL_* env vars)",
)


def run(coro: Coroutine[Any, Any, None]) -> None:
    """Run a coroutine synchronously."""
    asyncio.run(coro)


# ---------------------------------------------------------------------------
# Helper — clean up test tables after each test
# ---------------------------------------------------------------------------


def _drop_tables(conn: MariaDBSyncConnection, *names: str) -> None:
    cur = conn._conn.cursor()
    for name in names:
        cur.execute(f"DROP TABLE IF EXISTS {name}")  # noqa: S608
    conn._conn.commit()
    cur.close()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registered_providers_includes_mysql() -> None:
    assert "mariadb" in registered_providers()


def test_get_sync_provider_mysql() -> None:
    cls = get_sync_provider("mariadb")
    assert cls is MariaDBSyncProvider


def test_get_async_provider_mysql() -> None:
    cls = get_async_provider("mariadb")
    assert cls is MariaDBAsyncProvider


# ---------------------------------------------------------------------------
# MariaDBSyncProvider — connect / placeholder
# ---------------------------------------------------------------------------


def test_sync_provider_placeholder() -> None:
    p = MariaDBSyncProvider()
    assert p.placeholder() == "%s"
    assert p.placeholder("x") == "%s"


def test_sync_provider_name() -> None:
    assert MariaDBSyncProvider.name == "mariadb"
    assert MariaDBSyncProvider.param_style == "format"


def test_sync_provider_connect_returns_connection() -> None:
    p = MariaDBSyncProvider()
    conn = p.connect(**MARIADB_CONNECT_KWARGS)
    assert isinstance(conn, MariaDBSyncConnection)
    conn.close()


# ---------------------------------------------------------------------------
# MariaDBSyncCursor
# ---------------------------------------------------------------------------


def test_sync_cursor_execute_select() -> None:
    p = MariaDBSyncProvider()
    conn = p.connect(**MARIADB_CONNECT_KWARGS)
    cur = conn.cursor()
    cur.execute("SELECT 1 AS x")
    row = cur.fetchone()
    assert row == (1,)
    cur.close()
    conn.close()


def test_sync_cursor_description_after_select() -> None:
    p = MariaDBSyncProvider()
    conn = p.connect(**MARIADB_CONNECT_KWARGS)
    cur = conn.cursor()
    cur.execute("SELECT 1 AS y")
    assert cur.description is not None
    assert cur.description[0][0] == "y"
    cur.close()
    conn.close()


def test_sync_cursor_description_before_execute() -> None:
    p = MariaDBSyncProvider()
    conn = p.connect(**MARIADB_CONNECT_KWARGS)
    cur = conn.cursor()
    assert cur.description is None
    cur.close()
    conn.close()


def test_sync_cursor_lastrowid_after_insert() -> None:
    p = MariaDBSyncProvider()
    conn = p.connect(**MARIADB_CONNECT_KWARGS)
    _drop_tables(conn, "my_test_lastrowid")
    try:
        cur = conn.cursor()
        cur.execute("CREATE TABLE my_test_lastrowid (id INT AUTO_INCREMENT PRIMARY KEY, v INT)")
        cur.execute("INSERT INTO my_test_lastrowid (v) VALUES (42)")
        assert cur.lastrowid == 1
        cur.close()
        conn.commit()
    finally:
        _drop_tables(conn, "my_test_lastrowid")
        conn.close()


def test_sync_cursor_rowcount_after_insert() -> None:
    p = MariaDBSyncProvider()
    conn = p.connect(**MARIADB_CONNECT_KWARGS)
    _drop_tables(conn, "my_test_rowcount")
    try:
        cur = conn.cursor()
        cur.execute("CREATE TABLE my_test_rowcount (v INT)")
        cur.execute("INSERT INTO my_test_rowcount VALUES (1)")
        assert cur.rowcount == 1
        cur.close()
        conn.commit()
    finally:
        _drop_tables(conn, "my_test_rowcount")
        conn.close()


def test_sync_cursor_fetchmany() -> None:
    p = MariaDBSyncProvider()
    conn = p.connect(**MARIADB_CONNECT_KWARGS)
    _drop_tables(conn, "my_fetchmany")
    try:
        cur = conn.cursor()
        cur.execute("CREATE TABLE my_fetchmany (v INT)")
        cur.executemany("INSERT INTO my_fetchmany VALUES (%s)", [[i] for i in range(5)])
        conn.commit()
        cur.execute("SELECT v FROM my_fetchmany ORDER BY v")
        rows = cur.fetchmany(3)
        assert len(rows) == 3
        cur.close()
    finally:
        _drop_tables(conn, "my_fetchmany")
        conn.close()


def test_sync_cursor_fetchall() -> None:
    p = MariaDBSyncProvider()
    conn = p.connect(**MARIADB_CONNECT_KWARGS)
    _drop_tables(conn, "my_fetchall")
    try:
        cur = conn.cursor()
        cur.execute("CREATE TABLE my_fetchall (v INT)")
        cur.executemany("INSERT INTO my_fetchall VALUES (%s)", [[i] for i in range(3)])
        conn.commit()
        cur.execute("SELECT v FROM my_fetchall ORDER BY v")
        rows = cur.fetchall()
        assert len(rows) == 3
        cur.close()
    finally:
        _drop_tables(conn, "my_fetchall")
        conn.close()


def test_sync_cursor_executemany() -> None:
    p = MariaDBSyncProvider()
    conn = p.connect(**MARIADB_CONNECT_KWARGS)
    _drop_tables(conn, "my_executemany")
    try:
        cur = conn.cursor()
        cur.execute("CREATE TABLE my_executemany (v INT)")
        cur.executemany("INSERT INTO my_executemany VALUES (%s)", [[1], [2], [3]])
        cur.execute("SELECT COUNT(*) FROM my_executemany")
        row = cur.fetchone()
        assert row == (3,)
        cur.close()
        conn.commit()
    finally:
        _drop_tables(conn, "my_executemany")
        conn.close()


# ---------------------------------------------------------------------------
# MariaDBSyncConnection — commit / rollback / executescript / raw
# ---------------------------------------------------------------------------


def test_sync_connection_commit() -> None:
    p = MariaDBSyncProvider()
    conn = p.connect(**MARIADB_CONNECT_KWARGS)
    _drop_tables(conn, "my_commit")
    try:
        cur = conn.cursor()
        cur.execute("CREATE TABLE my_commit (v INT)")
        conn.commit()
        cur.close()
    finally:
        _drop_tables(conn, "my_commit")
        conn.close()


def test_sync_connection_rollback() -> None:
    p = MariaDBSyncProvider()
    conn = p.connect(**MARIADB_CONNECT_KWARGS)
    _drop_tables(conn, "my_rollback")
    try:
        cur = conn.cursor()
        cur.execute("CREATE TABLE my_rollback (v INT) ENGINE=InnoDB")
        conn.commit()
        cur.execute("INSERT INTO my_rollback VALUES (1)")
        conn.rollback()
        cur.execute("SELECT COUNT(*) FROM my_rollback")
        assert cur.fetchone() == (0,)
        cur.close()
    finally:
        _drop_tables(conn, "my_rollback")
        conn.close()


def test_sync_connection_executescript() -> None:
    p = MariaDBSyncProvider()
    conn = p.connect(**MARIADB_CONNECT_KWARGS)
    _drop_tables(conn, "my_script")
    try:
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS my_script (v INT); INSERT INTO my_script VALUES (42)"
        )
        cur = conn.cursor()
        cur.execute("SELECT v FROM my_script")
        row = cur.fetchone()
        assert row == (42,)
        cur.close()
        conn.commit()
    finally:
        _drop_tables(conn, "my_script")
        conn.close()


def test_sync_connection_raw() -> None:
    import pymysql  # noqa: PLC0415

    p = MariaDBSyncProvider()
    conn = p.connect(**MARIADB_CONNECT_KWARGS)
    assert isinstance(conn.raw, pymysql.connections.Connection)
    conn.close()


# ---------------------------------------------------------------------------
# MariaDBSyncProvider — execute_ddl
# ---------------------------------------------------------------------------


def test_sync_execute_ddl_empty_is_noop() -> None:
    p = MariaDBSyncProvider()
    conn = p.connect(**MARIADB_CONNECT_KWARGS)
    p.execute_ddl(conn, [])  # must not raise
    conn.close()


def test_sync_execute_ddl_creates_table() -> None:
    p = MariaDBSyncProvider()
    conn = p.connect(**MARIADB_CONNECT_KWARGS)
    _drop_tables(conn, "my_ddl")
    try:
        stmts = [
            "CREATE TABLE IF NOT EXISTS my_ddl "
            "(id INT AUTO_INCREMENT PRIMARY KEY, name TEXT NOT NULL)"
        ]
        p.execute_ddl(conn, stmts)
        conn.commit()
        cur = conn.cursor()
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = 'my_ddl'"
        )
        assert cur.fetchone() is not None
        cur.close()
    finally:
        _drop_tables(conn, "my_ddl")
        conn.close()


def test_sync_execute_ddl_multiple_statements() -> None:
    p = MariaDBSyncProvider()
    conn = p.connect(**MARIADB_CONNECT_KWARGS)
    _drop_tables(conn, "my_ddl_a", "my_ddl_b")
    try:
        stmts = [
            "CREATE TABLE IF NOT EXISTS my_ddl_a (id INT AUTO_INCREMENT PRIMARY KEY)",
            "CREATE TABLE IF NOT EXISTS my_ddl_b (id INT AUTO_INCREMENT PRIMARY KEY)",
        ]
        p.execute_ddl(conn, stmts)
        conn.commit()
        cur = conn.cursor()
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = DATABASE() "
            "AND table_name IN ('my_ddl_a', 'my_ddl_b') ORDER BY table_name"
        )
        names = [row[0] for row in cur.fetchall()]
        assert "my_ddl_a" in names
        assert "my_ddl_b" in names
        cur.close()
    finally:
        _drop_tables(conn, "my_ddl_a", "my_ddl_b")
        conn.close()


def test_sync_execute_ddl_trailing_semicolons() -> None:
    p = MariaDBSyncProvider()
    conn = p.connect(**MARIADB_CONNECT_KWARGS)
    _drop_tables(conn, "my_ddl_semi")
    try:
        stmts = ["CREATE TABLE IF NOT EXISTS my_ddl_semi (id INT AUTO_INCREMENT PRIMARY KEY);"]
        p.execute_ddl(conn, stmts)  # must not raise
        conn.commit()
    finally:
        _drop_tables(conn, "my_ddl_semi")
        conn.close()


# ---------------------------------------------------------------------------
# MariaDBSyncProvider — introspect
# ---------------------------------------------------------------------------


def test_sync_introspect_empty_schema() -> None:
    p = MariaDBSyncProvider()
    conn = p.connect(**MARIADB_CONNECT_KWARGS)
    schema = p.introspect(conn)
    assert isinstance(schema, dict)
    conn.close()


def test_sync_introspect_created_table() -> None:
    p = MariaDBSyncProvider()
    conn = p.connect(**MARIADB_CONNECT_KWARGS)
    _drop_tables(conn, "my_introspect")
    try:
        p.execute_ddl(
            conn,
            [
                "CREATE TABLE IF NOT EXISTS my_introspect "
                "(id INT AUTO_INCREMENT PRIMARY KEY, name TEXT NOT NULL)"
            ],
        )
        conn.commit()
        schema = p.introspect(conn)
        assert "my_introspect" in schema
        col_names = [c.name for c in schema["my_introspect"].columns]
        assert "id" in col_names
        assert "name" in col_names
    finally:
        _drop_tables(conn, "my_introspect")
        conn.close()


def test_sync_introspect_index() -> None:
    p = MariaDBSyncProvider()
    conn = p.connect(**MARIADB_CONNECT_KWARGS)
    _drop_tables(conn, "my_idx")
    try:
        p.execute_ddl(
            conn,
            [
                "CREATE TABLE IF NOT EXISTS my_idx "
                "(id INT AUTO_INCREMENT PRIMARY KEY, val INT NOT NULL)",
                "CREATE INDEX idx_my_idx__val ON my_idx (val)",
            ],
        )
        conn.commit()
        schema = p.introspect(conn)
        assert "my_idx" in schema
        idx_names = [i.name for i in schema["my_idx"].indexes]
        assert "idx_my_idx__val" in idx_names
    finally:
        _drop_tables(conn, "my_idx")
        conn.close()


# ---------------------------------------------------------------------------
# introspect_mariadb standalone
# ---------------------------------------------------------------------------


def test_introspect_mariadb_directly() -> None:
    p = MariaDBSyncProvider()
    conn = p.connect(**MARIADB_CONNECT_KWARGS)
    _drop_tables(conn, "my_direct")
    try:
        p.execute_ddl(
            conn,
            ["CREATE TABLE IF NOT EXISTS my_direct (x INT NOT NULL, y TEXT)"],
        )
        conn.commit()
        result = introspect_mariadb(conn)
        assert "my_direct" in result
        cols = {c.name for c in result["my_direct"].columns}
        assert cols == {"x", "y"}
    finally:
        _drop_tables(conn, "my_direct")
        conn.close()


def test_introspect_mariadb_nullable_columns() -> None:
    p = MariaDBSyncProvider()
    conn = p.connect(**MARIADB_CONNECT_KWARGS)
    _drop_tables(conn, "my_nullable")
    try:
        p.execute_ddl(
            conn,
            ["CREATE TABLE IF NOT EXISTS my_nullable (a INT NOT NULL, b TEXT)"],
        )
        conn.commit()
        result = introspect_mariadb(conn)
        tbl = result["my_nullable"]
        col_map = {c.name: c for c in tbl.columns}
        assert col_map["a"].nullable is False
        assert col_map["b"].nullable is True
    finally:
        _drop_tables(conn, "my_nullable")
        conn.close()


def test_introspect_mariadb_unique_index() -> None:
    p = MariaDBSyncProvider()
    conn = p.connect(**MARIADB_CONNECT_KWARGS)
    _drop_tables(conn, "my_uniq_idx")
    try:
        p.execute_ddl(
            conn,
            [
                "CREATE TABLE IF NOT EXISTS my_uniq_idx (id INT PRIMARY KEY, slug TEXT NOT NULL)",
                "CREATE UNIQUE INDEX uniq_my_uniq_idx__slug ON my_uniq_idx (slug(255))",
            ],
        )
        conn.commit()
        result = introspect_mariadb(conn)
        tbl = result["my_uniq_idx"]
        idx_map = {i.name: i for i in tbl.indexes}
        assert "uniq_my_uniq_idx__slug" in idx_map
        assert idx_map["uniq_my_uniq_idx__slug"].unique is True
    finally:
        _drop_tables(conn, "my_uniq_idx")
        conn.close()


# ---------------------------------------------------------------------------
# MariaDBAsyncProvider
# ---------------------------------------------------------------------------


def test_async_provider_placeholder() -> None:
    p = MariaDBAsyncProvider()
    assert p.placeholder() == "%s"


def test_async_provider_name() -> None:
    assert MariaDBAsyncProvider.name == "mariadb"


def test_async_provider_connect_returns_connection() -> None:
    async def _run() -> None:
        p = MariaDBAsyncProvider()
        conn = await p.connect(**MARIADB_CONNECT_KWARGS)
        assert isinstance(conn, MariaDBAsyncConnection)
        await conn.close()

    run(_run())


def test_async_cursor_execute_select() -> None:
    async def _run() -> None:
        p = MariaDBAsyncProvider()
        conn = await p.connect(**MARIADB_CONNECT_KWARGS)
        cur = await conn.cursor()
        await cur.execute("SELECT 1 AS x")
        row = await cur.fetchone()
        assert row == (1,)
        await cur.close()
        await conn.close()

    run(_run())


def test_async_cursor_fetchall() -> None:
    async def _run() -> None:
        p = MariaDBAsyncProvider()
        conn = await p.connect(**MARIADB_CONNECT_KWARGS)
        cur = await conn.cursor()
        await cur.execute("SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4")
        rows = await cur.fetchall()
        assert len(rows) == 4
        await cur.close()
        await conn.close()

    run(_run())


def test_async_cursor_fetchmany() -> None:
    async def _run() -> None:
        p = MariaDBAsyncProvider()
        conn = await p.connect(**MARIADB_CONNECT_KWARGS)
        cur = await conn.cursor()
        await cur.execute(
            "SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5 UNION SELECT 6"
        )
        rows = await cur.fetchmany(2)
        assert len(rows) == 2
        await cur.close()
        await conn.close()

    run(_run())


def test_async_cursor_description() -> None:
    async def _run() -> None:
        p = MariaDBAsyncProvider()
        conn = await p.connect(**MARIADB_CONNECT_KWARGS)
        cur = await conn.cursor()
        await cur.execute("SELECT 42 AS answer")
        assert cur.description is not None
        assert cur.description[0][0] == "answer"
        await cur.close()
        await conn.close()

    run(_run())


def test_async_cursor_description_before_execute() -> None:
    async def _run() -> None:
        p = MariaDBAsyncProvider()
        conn = await p.connect(**MARIADB_CONNECT_KWARGS)
        cur = await conn.cursor()
        assert cur.description is None
        await cur.close()
        await conn.close()

    run(_run())


def test_async_cursor_lastrowid_after_insert() -> None:
    async def _run() -> None:
        p_sync = MariaDBSyncProvider()
        conn_sync = p_sync.connect(**MARIADB_CONNECT_KWARGS)
        _drop_tables(conn_sync, "my_async_lastrowid")
        conn_sync.close()

        p = MariaDBAsyncProvider()
        conn = await p.connect(**MARIADB_CONNECT_KWARGS)
        try:
            cur = await conn.cursor()
            await cur.execute(
                "CREATE TABLE IF NOT EXISTS my_async_lastrowid "
                "(id INT AUTO_INCREMENT PRIMARY KEY, v INT)"
            )
            await cur.execute("INSERT INTO my_async_lastrowid (v) VALUES (99)")
            assert cur.lastrowid == 1
            await cur.close()
            await conn.commit()
        finally:
            cur2 = await conn.cursor()
            await cur2.execute("DROP TABLE IF EXISTS my_async_lastrowid")
            await cur2.close()
            await conn.close()

    run(_run())


def test_async_connection_raw() -> None:
    async def _run() -> None:
        import asyncmy.connection  # noqa: PLC0415

        p = MariaDBAsyncProvider()
        conn = await p.connect(**MARIADB_CONNECT_KWARGS)
        assert isinstance(conn.raw, asyncmy.connection.Connection)
        await conn.close()

    run(_run())


def test_async_execute_ddl_empty_is_noop() -> None:
    async def _run() -> None:
        p = MariaDBAsyncProvider()
        conn = await p.connect(**MARIADB_CONNECT_KWARGS)
        await p.execute_ddl(conn, [])  # must not raise
        await conn.close()

    run(_run())


def test_async_execute_ddl_creates_table() -> None:
    async def _run() -> None:
        p_sync = MariaDBSyncProvider()
        conn_sync = p_sync.connect(**MARIADB_CONNECT_KWARGS)
        _drop_tables(conn_sync, "my_async_ddl")
        conn_sync.close()

        p = MariaDBAsyncProvider()
        conn = await p.connect(**MARIADB_CONNECT_KWARGS)
        try:
            stmts = [
                "CREATE TABLE IF NOT EXISTS my_async_ddl "
                "(id INT AUTO_INCREMENT PRIMARY KEY, v TEXT)"
            ]
            await p.execute_ddl(conn, stmts)
            cur = await conn.cursor()
            await cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name = 'my_async_ddl'"
            )
            result = await cur.fetchone()
            assert result is not None
            await cur.close()
        finally:
            cur2 = await conn.cursor()
            await cur2.execute("DROP TABLE IF EXISTS my_async_ddl")
            await cur2.close()
            await conn.close()

    run(_run())


def test_async_execute_ddl_multiple_statements() -> None:
    async def _run() -> None:
        p_sync = MariaDBSyncProvider()
        conn_sync = p_sync.connect(**MARIADB_CONNECT_KWARGS)
        _drop_tables(conn_sync, "my_async_a", "my_async_b")
        conn_sync.close()

        p = MariaDBAsyncProvider()
        conn = await p.connect(**MARIADB_CONNECT_KWARGS)
        try:
            stmts = [
                "CREATE TABLE IF NOT EXISTS my_async_a (id INT AUTO_INCREMENT PRIMARY KEY)",
                "CREATE TABLE IF NOT EXISTS my_async_b (id INT AUTO_INCREMENT PRIMARY KEY)",
            ]
            await p.execute_ddl(conn, stmts)
            cur = await conn.cursor()
            await cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = DATABASE() "
                "AND table_name IN ('my_async_a', 'my_async_b') ORDER BY table_name"
            )
            names = [r[0] for r in await cur.fetchall()]
            assert "my_async_a" in names
            assert "my_async_b" in names
            await cur.close()
        finally:
            for name in ("my_async_a", "my_async_b"):
                cur2 = await conn.cursor()
                await cur2.execute(f"DROP TABLE IF EXISTS {name}")  # noqa: S608
                await cur2.close()
            await conn.close()

    run(_run())


def test_async_execute_ddl_trailing_semicolons() -> None:
    async def _run() -> None:
        p_sync = MariaDBSyncProvider()
        conn_sync = p_sync.connect(**MARIADB_CONNECT_KWARGS)
        _drop_tables(conn_sync, "my_async_semi")
        conn_sync.close()

        p = MariaDBAsyncProvider()
        conn = await p.connect(**MARIADB_CONNECT_KWARGS)
        try:
            await p.execute_ddl(
                conn,
                ["CREATE TABLE IF NOT EXISTS my_async_semi (id INT AUTO_INCREMENT PRIMARY KEY);"],
            )
        finally:
            cur = await conn.cursor()
            await cur.execute("DROP TABLE IF EXISTS my_async_semi")
            await cur.close()
            await conn.close()

    run(_run())


def test_async_cursor_executemany() -> None:
    async def _run() -> None:
        p_sync = MariaDBSyncProvider()
        conn_sync = p_sync.connect(**MARIADB_CONNECT_KWARGS)
        _drop_tables(conn_sync, "my_async_emany")
        conn_sync.close()

        p = MariaDBAsyncProvider()
        conn = await p.connect(**MARIADB_CONNECT_KWARGS)
        try:
            cur = await conn.cursor()
            await cur.execute("CREATE TABLE IF NOT EXISTS my_async_emany (v INT)")
            await cur.executemany("INSERT INTO my_async_emany VALUES (%s)", [[1], [2], [3]])
            await cur.execute("SELECT COUNT(*) FROM my_async_emany")
            row = await cur.fetchone()
            assert row is not None and row[0] == 3
            await cur.close()
            await conn.commit()
        finally:
            cur2 = await conn.cursor()
            await cur2.execute("DROP TABLE IF EXISTS my_async_emany")
            await cur2.close()
            await conn.close()

    run(_run())


def test_async_connection_commit() -> None:
    async def _run() -> None:
        p = MariaDBAsyncProvider()
        conn = await p.connect(**MARIADB_CONNECT_KWARGS)
        await conn.commit()  # must not raise
        await conn.close()

    run(_run())


def test_async_connection_rollback() -> None:
    async def _run() -> None:
        p = MariaDBAsyncProvider()
        conn = await p.connect(**MARIADB_CONNECT_KWARGS)
        await conn.rollback()  # must not raise
        await conn.close()

    run(_run())


def test_async_connection_executescript() -> None:
    async def _run() -> None:
        p_sync = MariaDBSyncProvider()
        conn_sync = p_sync.connect(**MARIADB_CONNECT_KWARGS)
        _drop_tables(conn_sync, "my_async_script")
        conn_sync.close()

        p = MariaDBAsyncProvider()
        conn = await p.connect(**MARIADB_CONNECT_KWARGS)
        try:
            await conn.executescript(
                "CREATE TABLE IF NOT EXISTS my_async_script (v INT);"
                "INSERT INTO my_async_script VALUES (7);"
            )
            cur = await conn.cursor()
            await cur.execute("SELECT v FROM my_async_script")
            row = await cur.fetchone()
            assert row is not None and row[0] == 7
            await cur.close()
            await conn.commit()
        finally:
            cur2 = await conn.cursor()
            await cur2.execute("DROP TABLE IF EXISTS my_async_script")
            await cur2.close()
            await conn.close()

    run(_run())


# ---------------------------------------------------------------------------
# MariaDBRenderer (DDL)
# ---------------------------------------------------------------------------


def test_mysql_renderer_sql_type_int() -> None:
    r = MariaDBRenderer()
    col = Column(name="x", py_type=int)
    assert r.sql_type(col) == "INT"


def test_mysql_renderer_sql_type_bool() -> None:
    r = MariaDBRenderer()
    col = Column(name="active", py_type=bool)
    assert r.sql_type(col) == "TINYINT(1)"


def test_mysql_renderer_sql_type_float() -> None:
    r = MariaDBRenderer()
    col = Column(name="price", py_type=float)
    assert r.sql_type(col) == "DOUBLE"


def test_mysql_renderer_sql_type_varchar() -> None:
    r = MariaDBRenderer()
    col = Column(name="slug", py_type=str, max_len=50)
    assert r.sql_type(col) == "VARCHAR(50)"


def test_mysql_renderer_create_table_basic() -> None:
    r = MariaDBRenderer()
    table = Table(
        name="post",
        columns=[
            Column(name="id", py_type=int, primary_key=True, auto_increment=True),
            Column(name="title", py_type=str, nullable=False),
        ],
    )
    sql = r.create_table(table)
    assert "CREATE TABLE IF NOT EXISTS post" in sql
    assert "id INT AUTO_INCREMENT PRIMARY KEY" in sql
    assert "title TEXT NOT NULL" in sql


def test_mysql_renderer_create_table_with_fk() -> None:
    r = MariaDBRenderer()
    table = Table(
        name="comment",
        columns=[
            Column(name="id", py_type=int, primary_key=True, auto_increment=True),
            Column(name="post_id", py_type=int, nullable=False),
        ],
        foreign_keys=[ForeignKey(name="fk_comment__post_id", column="post_id", ref_table="post")],
    )
    sql = r.create_table(table)
    assert "CONSTRAINT fk_comment__post_id FOREIGN KEY (post_id) REFERENCES post (id)" in sql


def test_mysql_renderer_drop_table() -> None:
    r = MariaDBRenderer()
    assert r.drop_table("post") == "DROP TABLE IF EXISTS post"


def test_mysql_renderer_add_column() -> None:
    r = MariaDBRenderer()
    col = Column(name="body", py_type=str, nullable=True)
    sql = r.add_column("post", col)
    assert sql == "ALTER TABLE post ADD COLUMN body TEXT"


def test_mysql_renderer_drop_column() -> None:
    r = MariaDBRenderer()
    assert r.drop_column("post", "body") == "ALTER TABLE post DROP COLUMN body"


def test_mysql_renderer_create_index() -> None:
    r = MariaDBRenderer()
    idx = Index(name="idx_post__title", columns=["title"])
    sql = r.create_index("post", idx)
    assert sql == "CREATE INDEX idx_post__title ON post (title)"


def test_mysql_renderer_create_unique_index() -> None:
    r = MariaDBRenderer()
    idx = Index(name="unq_post__slug", columns=["slug"], unique=True)
    sql = r.create_index("post", idx)
    assert sql == "CREATE UNIQUE INDEX unq_post__slug ON post (slug)"


def test_mysql_renderer_drop_index_via_render() -> None:
    from nextorm.schema.diff import DropIndex  # noqa: PLC0415

    r = MariaDBRenderer()
    op = DropIndex(table_name="post", index_name="idx_post__title")
    assert r.render(op) == "DROP INDEX idx_post__title ON post"


def test_mysql_renderer_nullable_column() -> None:
    r = MariaDBRenderer()
    col = Column(name="notes", py_type=str, nullable=True)
    sql = r.add_column("post", col)
    assert "NOT NULL" not in sql


def test_mysql_renderer_unique_column() -> None:
    r = MariaDBRenderer()
    table = Table(
        name="t",
        columns=[Column(name="email", py_type=str, unique=True, nullable=False)],
    )
    sql = r.create_table(table)
    assert "UNIQUE" in sql


def test_mysql_renderer_default_value() -> None:
    r = MariaDBRenderer()
    col = Column(name="created_at", py_type=str, sql_default="CURRENT_TIMESTAMP")
    sql = r.add_column("t", col)
    assert "DEFAULT CURRENT_TIMESTAMP" in sql
