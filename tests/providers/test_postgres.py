"""Tests for nextorm.providers.postgres.

Requires a live PostgreSQL server. Connection is configured via the
``NEXTORM_POSTGRES_DSN`` environment variable (default: see
:data:`POSTGRES_DSN`).  Tests are skipped automatically when the driver
cannot connect to the configured server.
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Coroutine

import psycopg

from nextorm.providers.base import (
    get_async_provider,
    get_sync_provider,
    registered_providers,
)
from nextorm.providers.postgres import (
    PostgresAsyncConnection,
    PostgresAsyncProvider,
    PostgresSyncConnection,
    PostgresSyncProvider,
)
from nextorm.schema.core import Column, ForeignKey, Index, Table
from nextorm.schema.ddl import PostgresRenderer
from nextorm.schema.introspect import introspect_postgres

POSTGRES_DSN = os.environ.get(
    "NEXTORM_POSTGRES_DSN",
    "postgresql://wadera:nextorm_test@localhost/nextorm_test",
)


def _can_connect() -> bool:
    try:
        conn = psycopg.connect(POSTGRES_DSN)
        conn.close()
        return True
    except Exception:
        return False


# Skip all tests in this module if no live DB is available
pytestmark = pytest.mark.skipif(
    not _can_connect(),
    reason="No PostgreSQL server reachable at NEXTORM_POSTGRES_DSN",
)


def run(coro: Coroutine[Any, Any, None]) -> None:
    """Run a coroutine synchronously in a fresh event loop."""
    asyncio.run(coro)


# ---------------------------------------------------------------------------
# Helper — clean up test tables after each test
# ---------------------------------------------------------------------------


def _drop_tables(conn: PostgresSyncConnection, *names: str) -> None:
    cur = conn._conn.cursor()
    for name in names:
        cur.execute(f"DROP TABLE IF EXISTS {name} CASCADE")  # noqa: S608  # pyright: ignore[reportCallIssue, reportArgumentType]
    conn._conn.commit()
    cur.close()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registered_providers_includes_postgres() -> None:
    assert "postgres" in registered_providers()


def test_get_sync_provider_postgres() -> None:
    cls = get_sync_provider("postgres")
    assert cls is PostgresSyncProvider


def test_get_async_provider_postgres() -> None:
    cls = get_async_provider("postgres")
    assert cls is PostgresAsyncProvider


# ---------------------------------------------------------------------------
# PostgresSyncProvider — connect / placeholder
# ---------------------------------------------------------------------------


def test_sync_provider_placeholder() -> None:
    p = PostgresSyncProvider()
    assert p.placeholder() == "%s"
    assert p.placeholder("x") == "%s"


def test_sync_provider_name() -> None:
    assert PostgresSyncProvider.name == "postgres"
    assert PostgresSyncProvider.param_style == "format"


def test_sync_provider_connect_returns_connection() -> None:
    p = PostgresSyncProvider()
    conn = p.connect(POSTGRES_DSN)
    assert isinstance(conn, PostgresSyncConnection)
    conn.close()


# ---------------------------------------------------------------------------
# PostgresSyncCursor
# ---------------------------------------------------------------------------


def test_sync_cursor_execute_select() -> None:
    p = PostgresSyncProvider()
    conn = p.connect(POSTGRES_DSN)
    cur = conn.cursor()
    cur.execute("SELECT 1 AS x")
    row = cur.fetchone()
    assert row == (1,)
    cur.close()
    conn.close()


def test_sync_cursor_description_after_select() -> None:
    p = PostgresSyncProvider()
    conn = p.connect(POSTGRES_DSN)
    cur = conn.cursor()
    cur.execute("SELECT 1 AS y")
    assert cur.description is not None
    assert cur.description[0][0] == "y"
    cur.close()
    conn.close()


def test_sync_cursor_description_before_execute() -> None:
    p = PostgresSyncProvider()
    conn = p.connect(POSTGRES_DSN)
    cur = conn.cursor()
    assert cur.description is None
    cur.close()
    conn.close()


def test_sync_cursor_lastrowid_is_none() -> None:
    p = PostgresSyncProvider()
    conn = p.connect(POSTGRES_DSN)
    cur = conn.cursor()
    assert cur.lastrowid is None
    cur.close()
    conn.close()


def test_sync_cursor_rowcount_after_insert() -> None:
    p = PostgresSyncProvider()
    conn = p.connect(POSTGRES_DSN)
    _drop_tables(conn, "pg_test_rowcount")
    try:
        cur = conn.cursor()
        cur.execute("CREATE TABLE pg_test_rowcount (v INTEGER)")
        cur.execute("INSERT INTO pg_test_rowcount VALUES (1)")
        assert cur.rowcount == 1
        cur.close()
        conn.commit()
    finally:
        _drop_tables(conn, "pg_test_rowcount")
        conn.close()


def test_sync_cursor_fetchmany() -> None:
    p = PostgresSyncProvider()
    conn = p.connect(POSTGRES_DSN)
    cur = conn.cursor()
    cur.execute("SELECT generate_series(1, 5)")
    rows = cur.fetchmany(3)
    assert len(rows) == 3
    cur.close()
    conn.close()


def test_sync_cursor_fetchall() -> None:
    p = PostgresSyncProvider()
    conn = p.connect(POSTGRES_DSN)
    cur = conn.cursor()
    cur.execute("SELECT generate_series(1, 3)")
    rows = cur.fetchall()
    assert len(rows) == 3
    cur.close()
    conn.close()


def test_sync_cursor_executemany() -> None:
    p = PostgresSyncProvider()
    conn = p.connect(POSTGRES_DSN)
    _drop_tables(conn, "pg_test_executemany")
    try:
        cur = conn.cursor()
        cur.execute("CREATE TABLE pg_test_executemany (v INTEGER)")
        cur.executemany("INSERT INTO pg_test_executemany VALUES (%s)", [[1], [2], [3]])
        cur.execute("SELECT COUNT(*) FROM pg_test_executemany")
        row = cur.fetchone()
        assert row == (3,)
        cur.close()
        conn.commit()
    finally:
        _drop_tables(conn, "pg_test_executemany")
        conn.close()


# ---------------------------------------------------------------------------
# PostgresSyncConnection — commit / rollback / executescript / raw
# ---------------------------------------------------------------------------


def test_sync_connection_commit() -> None:
    p = PostgresSyncProvider()
    conn = p.connect(POSTGRES_DSN)
    _drop_tables(conn, "pg_test_commit")
    try:
        cur = conn.cursor()
        cur.execute("CREATE TABLE pg_test_commit (v INTEGER)")
        conn.commit()
        cur.close()
    finally:
        _drop_tables(conn, "pg_test_commit")
        conn.close()


def test_sync_connection_rollback() -> None:
    p = PostgresSyncProvider()
    conn = p.connect(POSTGRES_DSN)
    _drop_tables(conn, "pg_test_rollback")
    try:
        cur = conn.cursor()
        cur.execute("CREATE TABLE pg_test_rollback (v INTEGER)")
        conn.commit()
        cur.execute("INSERT INTO pg_test_rollback VALUES (1)")
        conn.rollback()
        cur.execute("SELECT COUNT(*) FROM pg_test_rollback")
        assert cur.fetchone() == (0,)
        cur.close()
    finally:
        _drop_tables(conn, "pg_test_rollback")
        conn.close()


def test_sync_connection_executescript() -> None:
    p = PostgresSyncProvider()
    conn = p.connect(POSTGRES_DSN)
    _drop_tables(conn, "pg_test_script")
    try:
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS pg_test_script (v INTEGER); "
            "INSERT INTO pg_test_script VALUES (42)"
        )
        cur = conn.cursor()
        cur.execute("SELECT v FROM pg_test_script")
        row = cur.fetchone()
        assert row == (42,)
        cur.close()
    finally:
        _drop_tables(conn, "pg_test_script")
        conn.close()


def test_sync_connection_raw() -> None:
    p = PostgresSyncProvider()
    conn = p.connect(POSTGRES_DSN)
    assert isinstance(conn.raw, psycopg.Connection)
    conn.close()


# ---------------------------------------------------------------------------
# PostgresSyncProvider — execute_ddl
# ---------------------------------------------------------------------------


def test_sync_execute_ddl_empty_is_noop() -> None:
    p = PostgresSyncProvider()
    conn = p.connect(POSTGRES_DSN)
    p.execute_ddl(conn, [])  # must not raise
    conn.close()


def test_sync_execute_ddl_creates_table() -> None:
    p = PostgresSyncProvider()
    conn = p.connect(POSTGRES_DSN)
    _drop_tables(conn, "pg_test_ddl")
    try:
        stmts = [
            "CREATE TABLE IF NOT EXISTS pg_test_ddl (id SERIAL PRIMARY KEY, name TEXT NOT NULL)"
        ]
        p.execute_ddl(conn, stmts)
        conn.commit()
        cur = conn.cursor()
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name='pg_test_ddl'"
        )
        assert cur.fetchone() is not None
        cur.close()
    finally:
        _drop_tables(conn, "pg_test_ddl")
        conn.close()


def test_sync_execute_ddl_multiple_statements() -> None:
    p = PostgresSyncProvider()
    conn = p.connect(POSTGRES_DSN)
    _drop_tables(conn, "pg_test_ddl_a", "pg_test_ddl_b")
    try:
        stmts = [
            "CREATE TABLE IF NOT EXISTS pg_test_ddl_a (id SERIAL PRIMARY KEY)",
            "CREATE TABLE IF NOT EXISTS pg_test_ddl_b (id SERIAL PRIMARY KEY)",
        ]
        p.execute_ddl(conn, stmts)
        conn.commit()
        cur = conn.cursor()
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name IN ('pg_test_ddl_a', 'pg_test_ddl_b') "
            "ORDER BY table_name"
        )
        names = [row[0] for row in cur.fetchall()]
        assert "pg_test_ddl_a" in names
        assert "pg_test_ddl_b" in names
        cur.close()
    finally:
        _drop_tables(conn, "pg_test_ddl_a", "pg_test_ddl_b")
        conn.close()


def test_sync_execute_ddl_trailing_semicolons() -> None:
    p = PostgresSyncProvider()
    conn = p.connect(POSTGRES_DSN)
    _drop_tables(conn, "pg_test_ddl_semi")
    try:
        stmts = ["CREATE TABLE IF NOT EXISTS pg_test_ddl_semi (id SERIAL PRIMARY KEY);"]
        p.execute_ddl(conn, stmts)  # must not raise
        conn.commit()
    finally:
        _drop_tables(conn, "pg_test_ddl_semi")
        conn.close()


# ---------------------------------------------------------------------------
# PostgresSyncProvider — introspect
# ---------------------------------------------------------------------------


def test_sync_introspect_empty_schema() -> None:
    p = PostgresSyncProvider()
    conn = p.connect(POSTGRES_DSN)
    schema = p.introspect(conn)
    assert isinstance(schema, dict)
    conn.close()


def test_sync_introspect_created_table() -> None:
    p = PostgresSyncProvider()
    conn = p.connect(POSTGRES_DSN)
    _drop_tables(conn, "pg_test_introspect")
    try:
        p.execute_ddl(
            conn,
            [
                "CREATE TABLE IF NOT EXISTS pg_test_introspect "
                "(id SERIAL PRIMARY KEY, name TEXT NOT NULL)"
            ],
        )
        conn.commit()
        schema = p.introspect(conn)
        assert "pg_test_introspect" in schema
        tbl = schema["pg_test_introspect"]
        col_names = [c.name for c in tbl.columns]
        assert "id" in col_names
        assert "name" in col_names
    finally:
        _drop_tables(conn, "pg_test_introspect")
        conn.close()


def test_sync_introspect_index() -> None:
    p = PostgresSyncProvider()
    conn = p.connect(POSTGRES_DSN)
    _drop_tables(conn, "pg_test_idx")
    try:
        p.execute_ddl(
            conn,
            [
                "CREATE TABLE IF NOT EXISTS pg_test_idx "
                "(id SERIAL PRIMARY KEY, val INTEGER NOT NULL)",
                "CREATE INDEX IF NOT EXISTS idx_pg_test_idx__val ON pg_test_idx (val)",
            ],
        )
        conn.commit()
        schema = p.introspect(conn)
        assert "pg_test_idx" in schema
        idx_names = [i.name for i in schema["pg_test_idx"].indexes]
        assert "idx_pg_test_idx__val" in idx_names
    finally:
        _drop_tables(conn, "pg_test_idx")
        conn.close()


# ---------------------------------------------------------------------------
# introspect_postgres standalone
# ---------------------------------------------------------------------------


def test_introspect_postgres_directly() -> None:
    p = PostgresSyncProvider()
    conn = p.connect(POSTGRES_DSN)
    _drop_tables(conn, "pg_test_direct")
    try:
        p.execute_ddl(
            conn,
            ["CREATE TABLE IF NOT EXISTS pg_test_direct (x INTEGER NOT NULL, y TEXT)"],
        )
        conn.commit()
        result = introspect_postgres(conn)
        assert "pg_test_direct" in result
        cols = {c.name for c in result["pg_test_direct"].columns}
        assert cols == {"x", "y"}
    finally:
        _drop_tables(conn, "pg_test_direct")
        conn.close()


def test_introspect_postgres_nullable_columns() -> None:
    p = PostgresSyncProvider()
    conn = p.connect(POSTGRES_DSN)
    _drop_tables(conn, "pg_test_nullable")
    try:
        p.execute_ddl(
            conn,
            ["CREATE TABLE IF NOT EXISTS pg_test_nullable (a INTEGER NOT NULL, b TEXT)"],
        )
        conn.commit()
        result = introspect_postgres(conn)
        tbl = result["pg_test_nullable"]
        col_map = {c.name: c for c in tbl.columns}
        assert col_map["a"].nullable is False
        assert col_map["b"].nullable is True
    finally:
        _drop_tables(conn, "pg_test_nullable")
        conn.close()


def test_introspect_postgres_unique_index() -> None:
    p = PostgresSyncProvider()
    conn = p.connect(POSTGRES_DSN)
    _drop_tables(conn, "pg_test_uniq_idx")
    try:
        p.execute_ddl(
            conn,
            [
                "CREATE TABLE IF NOT EXISTS pg_test_uniq_idx "
                "(id INTEGER PRIMARY KEY, slug TEXT NOT NULL)",
                "CREATE UNIQUE INDEX IF NOT EXISTS uniq_pg_test_uniq_idx__slug "
                "ON pg_test_uniq_idx (slug)",
            ],
        )
        conn.commit()
        result = introspect_postgres(conn)
        tbl = result["pg_test_uniq_idx"]
        idx_map = {i.name: i for i in tbl.indexes}
        assert "uniq_pg_test_uniq_idx__slug" in idx_map
        assert idx_map["uniq_pg_test_uniq_idx__slug"].unique is True
    finally:
        _drop_tables(conn, "pg_test_uniq_idx")
        conn.close()


# ---------------------------------------------------------------------------
# PostgresAsyncProvider
# ---------------------------------------------------------------------------


def test_async_provider_placeholder() -> None:
    p = PostgresAsyncProvider()
    assert p.placeholder() == "%s"


def test_async_provider_name() -> None:
    assert PostgresAsyncProvider.name == "postgres"


def test_async_provider_connect_returns_connection() -> None:
    async def _run() -> None:
        p = PostgresAsyncProvider()
        conn = await p.connect(POSTGRES_DSN)
        assert isinstance(conn, PostgresAsyncConnection)
        await conn.close()

    run(_run())


def test_async_cursor_execute_select() -> None:
    async def _run() -> None:
        p = PostgresAsyncProvider()
        conn = await p.connect(POSTGRES_DSN)
        cur = await conn.cursor()
        await cur.execute("SELECT 1 AS x")
        row = cur.fetchone()
        assert row == (1,)
        await cur.close()
        await conn.close()

    run(_run())


def test_async_cursor_fetchall() -> None:
    async def _run() -> None:
        p = PostgresAsyncProvider()
        conn = await p.connect(POSTGRES_DSN)
        cur = await conn.cursor()
        await cur.execute("SELECT generate_series(1, 4)")
        rows = cur.fetchall()
        assert len(rows) == 4
        await cur.close()
        await conn.close()

    run(_run())


def test_async_cursor_fetchmany() -> None:
    async def _run() -> None:
        p = PostgresAsyncProvider()
        conn = await p.connect(POSTGRES_DSN)
        cur = await conn.cursor()
        await cur.execute("SELECT generate_series(1, 6)")
        rows = cur.fetchmany(2)
        assert len(rows) == 2
        await cur.close()
        await conn.close()

    run(_run())


def test_async_cursor_description() -> None:
    async def _run() -> None:
        p = PostgresAsyncProvider()
        conn = await p.connect(POSTGRES_DSN)
        cur = await conn.cursor()
        await cur.execute("SELECT 42 AS answer")
        assert cur.description is not None
        assert cur.description[0][0] == "answer"
        await cur.close()
        await conn.close()

    run(_run())


def test_async_cursor_description_before_execute() -> None:
    async def _run() -> None:
        p = PostgresAsyncProvider()
        conn = await p.connect(POSTGRES_DSN)
        cur = await conn.cursor()
        assert cur.description is None
        await cur.close()
        await conn.close()

    run(_run())


def test_async_cursor_lastrowid_is_none() -> None:
    async def _run() -> None:
        p = PostgresAsyncProvider()
        conn = await p.connect(POSTGRES_DSN)
        cur = await conn.cursor()
        assert cur.lastrowid is None
        await cur.close()
        await conn.close()

    run(_run())


def test_async_connection_raw() -> None:
    async def _run() -> None:
        p = PostgresAsyncProvider()
        conn = await p.connect(POSTGRES_DSN)
        assert isinstance(conn.raw, psycopg.AsyncConnection)
        await conn.close()

    run(_run())


def test_async_execute_ddl_empty_is_noop() -> None:
    async def _run() -> None:
        p = PostgresAsyncProvider()
        conn = await p.connect(POSTGRES_DSN)
        await p.execute_ddl(conn, [])  # must not raise
        await conn.close()

    run(_run())


def test_async_execute_ddl_creates_table() -> None:
    async def _run() -> None:
        p = PostgresAsyncProvider()
        conn = await p.connect(POSTGRES_DSN)
        await conn._conn.execute("DROP TABLE IF EXISTS pg_async_ddl CASCADE")
        try:
            stmts = ["CREATE TABLE IF NOT EXISTS pg_async_ddl (id SERIAL PRIMARY KEY, v TEXT)"]
            await p.execute_ddl(conn, stmts)
            cur2 = await conn._conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name='pg_async_ddl'"
            )
            result = await cur2.fetchone()
            assert result is not None
        finally:
            await conn._conn.execute("DROP TABLE IF EXISTS pg_async_ddl CASCADE")
            await conn.close()

    run(_run())


def test_async_execute_ddl_multiple_statements() -> None:
    async def _run() -> None:
        p = PostgresAsyncProvider()
        conn = await p.connect(POSTGRES_DSN)
        for name in ("pg_async_a", "pg_async_b"):
            await conn._conn.execute(f"DROP TABLE IF EXISTS {name} CASCADE")  # noqa: S608
        try:
            stmts = [
                "CREATE TABLE IF NOT EXISTS pg_async_a (id SERIAL PRIMARY KEY)",
                "CREATE TABLE IF NOT EXISTS pg_async_b (id SERIAL PRIMARY KEY)",
            ]
            await p.execute_ddl(conn, stmts)
            cur2 = await conn._conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name IN ('pg_async_a', 'pg_async_b') "
                "ORDER BY table_name"
            )
            names = [r[0] for r in await cur2.fetchall()]
            assert "pg_async_a" in names
            assert "pg_async_b" in names
        finally:
            for name in ("pg_async_a", "pg_async_b"):
                await conn._conn.execute(f"DROP TABLE IF EXISTS {name} CASCADE")  # noqa: S608
            await conn.close()

    run(_run())


def test_async_execute_ddl_trailing_semicolons() -> None:
    async def _run() -> None:
        p = PostgresAsyncProvider()
        conn = await p.connect(POSTGRES_DSN)
        await conn._conn.execute("DROP TABLE IF EXISTS pg_async_semi CASCADE")
        try:
            await p.execute_ddl(
                conn,
                ["CREATE TABLE IF NOT EXISTS pg_async_semi (id SERIAL PRIMARY KEY);"],
            )
        finally:
            await conn._conn.execute("DROP TABLE IF EXISTS pg_async_semi CASCADE")
            await conn.close()

    run(_run())


def test_async_cursor_execute_ddl_path() -> None:
    """Test executing DDL via the async cursor — DDL yields no rows."""

    async def _run() -> None:
        p = PostgresAsyncProvider()
        conn = await p.connect(POSTGRES_DSN)
        await conn._conn.execute("DROP TABLE IF EXISTS pg_async_cursor_ddl CASCADE")
        try:
            cur = await conn.cursor()
            await cur.execute(
                "CREATE TABLE IF NOT EXISTS pg_async_cursor_ddl (id SERIAL PRIMARY KEY)"
            )
            assert cur.fetchall() == []
            assert cur.rowcount == 0
            await cur.close()
        finally:
            await conn._conn.execute("DROP TABLE IF EXISTS pg_async_cursor_ddl CASCADE")
            await conn.close()

    run(_run())


def test_async_cursor_execute_with_mapping_params() -> None:
    """Test that Mapping-style parameters are converted correctly."""

    async def _run() -> None:
        p = PostgresAsyncProvider()
        conn = await p.connect(POSTGRES_DSN)
        cur = await conn.cursor()
        # psycopg3 supports %(key)s named params with dict
        await cur.execute("SELECT %(x)s::integer AS v", {"x": 42})
        row = cur.fetchone()
        assert row == (42,)
        await cur.close()
        await conn.close()

    run(_run())


def test_async_cursor_execute_empty_select() -> None:
    """Test that a SELECT returning 0 rows still populates description."""

    async def _run() -> None:
        p = PostgresAsyncProvider()
        conn = await p.connect(POSTGRES_DSN)
        await conn._conn.execute("DROP TABLE IF EXISTS pg_async_empty CASCADE")
        await conn._conn.execute("CREATE TABLE IF NOT EXISTS pg_async_empty (v INTEGER)")
        try:
            cur = await conn.cursor()
            await cur.execute("SELECT v FROM pg_async_empty")
            assert cur.fetchone() is None
            assert cur.fetchall() == []
            assert cur.fetchmany(5) == []
            assert cur.description is not None  # SELECT always populates description
            await cur.close()
        finally:
            await conn._conn.execute("DROP TABLE IF EXISTS pg_async_empty CASCADE")
            await conn.close()

    run(_run())


def test_async_cursor_executemany() -> None:
    async def _run() -> None:
        p = PostgresAsyncProvider()
        conn = await p.connect(POSTGRES_DSN)
        await conn._conn.execute("DROP TABLE IF EXISTS pg_async_emany CASCADE")
        await conn._conn.execute("CREATE TABLE IF NOT EXISTS pg_async_emany (v INTEGER)")
        try:
            cur = await conn.cursor()
            await cur.executemany("INSERT INTO pg_async_emany VALUES (%s)", [[1], [2], [3]])
            count_cur = await conn._conn.execute("SELECT COUNT(*) FROM pg_async_emany")
            count_row = await count_cur.fetchone()
            assert count_row is not None and count_row[0] == 3
            await cur.close()
        finally:
            await conn._conn.execute("DROP TABLE IF EXISTS pg_async_emany CASCADE")
            await conn.close()

    run(_run())


def test_async_cursor_fetchone_after_pos_exhausted() -> None:
    async def _run() -> None:
        p = PostgresAsyncProvider()
        conn = await p.connect(POSTGRES_DSN)
        cur = await conn.cursor()
        await cur.execute("SELECT generate_series(1, 2)")
        cur.fetchone()
        cur.fetchone()
        assert cur.fetchone() is None  # pos >= len(rows)
        await cur.close()
        await conn.close()

    run(_run())


def test_async_cursor_fetchmany_empty_rows() -> None:
    async def _run() -> None:
        p = PostgresAsyncProvider()
        conn = await p.connect(POSTGRES_DSN)
        cur = await conn.cursor()
        # Before any execute: _rows is None
        assert cur.fetchmany(3) == []
        await cur.close()
        await conn.close()

    run(_run())


def test_async_cursor_fetchall_empty_rows() -> None:
    async def _run() -> None:
        p = PostgresAsyncProvider()
        conn = await p.connect(POSTGRES_DSN)
        cur = await conn.cursor()
        # Before any execute: _rows is None
        assert cur.fetchall() == []
        await cur.close()
        await conn.close()

    run(_run())


def test_async_connection_commit() -> None:
    async def _run() -> None:
        p = PostgresAsyncProvider()
        conn = await p.connect(POSTGRES_DSN)
        await conn.commit()  # noop — must not raise
        await conn.close()

    run(_run())


def test_async_connection_rollback() -> None:
    async def _run() -> None:
        p = PostgresAsyncProvider()
        conn = await p.connect(POSTGRES_DSN)
        await conn.rollback()  # noop — must not raise
        await conn.close()

    run(_run())


def test_async_connection_executescript() -> None:
    async def _run() -> None:
        p = PostgresAsyncProvider()
        conn = await p.connect(POSTGRES_DSN)
        await conn._conn.execute("DROP TABLE IF EXISTS pg_async_script CASCADE")
        try:
            await conn.executescript(
                "CREATE TABLE IF NOT EXISTS pg_async_script (v INTEGER);"
                "INSERT INTO pg_async_script VALUES (7);"
            )
            val_cur = await conn._conn.execute("SELECT v FROM pg_async_script")
            val_row = await val_cur.fetchone()
            assert val_row is not None and val_row[0] == 7
        finally:
            await conn._conn.execute("DROP TABLE IF EXISTS pg_async_script CASCADE")
            await conn.close()

    run(_run())


# ---------------------------------------------------------------------------
# PostgresRenderer (DDL)
# ---------------------------------------------------------------------------


def test_postgres_renderer_sql_type_int() -> None:
    r = PostgresRenderer()
    col = Column(name="x", py_type=int)
    assert r.sql_type(col) == "INTEGER"


def test_postgres_renderer_sql_type_serial() -> None:
    r = PostgresRenderer()
    col = Column(name="id", py_type=int, primary_key=True, auto_increment=True)
    assert r.sql_type(col) == "SERIAL"


def test_postgres_renderer_sql_type_bool() -> None:
    r = PostgresRenderer()
    col = Column(name="active", py_type=bool)
    assert r.sql_type(col) == "BOOLEAN"


def test_postgres_renderer_sql_type_float() -> None:
    r = PostgresRenderer()
    col = Column(name="price", py_type=float)
    assert r.sql_type(col) == "DOUBLE PRECISION"


def test_postgres_renderer_sql_type_varchar() -> None:
    r = PostgresRenderer()
    col = Column(name="slug", py_type=str, max_len=50)
    assert r.sql_type(col) == "VARCHAR(50)"


def test_postgres_renderer_create_table_basic() -> None:
    r = PostgresRenderer()
    table = Table(
        name="post",
        columns=[
            Column(name="id", py_type=int, primary_key=True, auto_increment=True),
            Column(name="title", py_type=str, nullable=False),
        ],
    )
    sql = r.create_table(table)
    assert "CREATE TABLE IF NOT EXISTS post" in sql
    assert "id SERIAL PRIMARY KEY" in sql
    assert "title TEXT NOT NULL" in sql


def test_postgres_renderer_create_table_with_fk() -> None:
    r = PostgresRenderer()
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


def test_postgres_renderer_drop_table() -> None:
    r = PostgresRenderer()
    assert r.drop_table("post") == "DROP TABLE IF EXISTS post"


def test_postgres_renderer_add_column() -> None:
    r = PostgresRenderer()
    col = Column(name="body", py_type=str, nullable=True)
    sql = r.add_column("post", col)
    assert sql == "ALTER TABLE post ADD COLUMN IF NOT EXISTS body TEXT"


def test_postgres_renderer_drop_column() -> None:
    r = PostgresRenderer()
    assert r.drop_column("post", "body") == "ALTER TABLE post DROP COLUMN IF EXISTS body"


def test_postgres_renderer_create_index() -> None:
    r = PostgresRenderer()
    idx = Index(name="idx_post__title", columns=["title"])
    sql = r.create_index("post", idx)
    assert sql == "CREATE INDEX IF NOT EXISTS idx_post__title ON post (title)"


def test_postgres_renderer_create_unique_index() -> None:
    r = PostgresRenderer()
    idx = Index(name="unq_post__slug", columns=["slug"], unique=True)
    sql = r.create_index("post", idx)
    assert sql == "CREATE UNIQUE INDEX IF NOT EXISTS unq_post__slug ON post (slug)"


def test_postgres_renderer_drop_index() -> None:
    r = PostgresRenderer()
    assert r.drop_index("idx_post__title") == "DROP INDEX IF EXISTS idx_post__title"


def test_postgres_renderer_nullable_column() -> None:
    r = PostgresRenderer()
    col = Column(name="notes", py_type=str, nullable=True)
    sql = r.add_column("post", col)
    assert "NOT NULL" not in sql


def test_postgres_renderer_unique_column() -> None:
    r = PostgresRenderer()
    table = Table(
        name="t",
        columns=[Column(name="email", py_type=str, unique=True, nullable=False)],
    )
    sql = r.create_table(table)
    assert "UNIQUE" in sql


def test_postgres_renderer_default_value() -> None:
    r = PostgresRenderer()
    col = Column(name="created_at", py_type=str, sql_default="CURRENT_TIMESTAMP")
    sql = r.add_column("t", col)
    assert "DEFAULT CURRENT_TIMESTAMP" in sql
