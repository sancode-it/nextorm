"""PostgreSQL provider — sync and async implementations, both using psycopg v3.

Both providers use :mod:`psycopg` v3 (install ``nextorm[postgres]``); psycopg
covers synchronous use via :class:`psycopg.Connection` and asynchronous use via
:class:`psycopg.AsyncConnection`, so no separate async driver is required.

Both are registered under the name ``"postgres"`` via :func:`register_provider`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from nextorm.providers.base import (
    AsyncConnection,
    AsyncCursor,
    AsyncProvider,
    DbRow,
    SyncConnection,
    SyncCursor,
    SyncProvider,
    register_provider,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    import psycopg
    import psycopg.rows

    from nextorm.schema.core import Table

__all__ = [
    "PostgresSyncCursor",
    "PostgresSyncConnection",
    "PostgresSyncProvider",
    "PostgresAsyncCursor",
    "PostgresAsyncConnection",
    "PostgresAsyncProvider",
]


# ---------------------------------------------------------------------------
# Sync — psycopg v3
# ---------------------------------------------------------------------------


class PostgresSyncCursor(SyncCursor):
    """Wraps a :class:`psycopg.Cursor`."""

    def __init__(self, cursor: psycopg.Cursor[psycopg.rows.TupleRow]) -> None:
        self._cursor = cursor

    @property
    def description(self) -> Sequence[tuple[str, Any, Any, Any, Any, Any, Any]] | None:
        return self._cursor.description  # type: ignore[return-value]

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @property
    def lastrowid(self) -> int | None:
        return None  # psycopg3 cursors don't expose lastrowid; use RETURNING

    def execute(
        self,
        sql: str,
        parameters: Sequence[Any] | Mapping[str, Any] = (),
    ) -> None:
        self._cursor.execute(sql, parameters)  # pyright: ignore[reportArgumentType]

    def executemany(
        self,
        sql: str,
        seq_of_parameters: Sequence[Sequence[Any]],
    ) -> None:
        self._cursor.executemany(sql, seq_of_parameters)  # pyright: ignore[reportArgumentType]

    def fetchone(self) -> DbRow | None:
        return self._cursor.fetchone()

    def fetchmany(self, size: int = 1) -> Sequence[DbRow]:
        return cast("Sequence[DbRow]", self._cursor.fetchmany(size))

    def fetchall(self) -> Sequence[DbRow]:
        return cast("Sequence[DbRow]", self._cursor.fetchall())

    def close(self) -> None:
        self._cursor.close()


class PostgresSyncConnection(SyncConnection):
    """Wraps a :class:`psycopg.Connection`."""

    def __init__(self, conn: psycopg.Connection[psycopg.rows.TupleRow]) -> None:
        self._conn = conn

    def cursor(self) -> PostgresSyncCursor:
        return PostgresSyncCursor(self._conn.cursor())

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    def executescript(self, sql: str) -> None:
        """Execute multiple SQL statements separated by semicolons."""
        self._conn.autocommit = True
        try:
            self._conn.execute(sql)  # pyright: ignore[reportCallIssue, reportArgumentType]
        finally:
            self._conn.autocommit = False

    @property
    def raw(self) -> psycopg.Connection[psycopg.rows.TupleRow]:
        """The underlying :class:`psycopg.Connection`."""
        return self._conn


class PostgresSyncProvider(SyncProvider):
    """Synchronous PostgreSQL provider (:mod:`psycopg` v3).

    Requires ``psycopg[binary]`` to be installed (``pip install nextorm[postgres]``).
    """

    name = "postgres"
    param_style = "format"

    def placeholder(self, param_name: str | None = None) -> str:
        """Return ``"%s"`` — psycopg uses format-style positional parameters."""
        return "%s"

    def connect(self, *args: Any, **kwargs: Any) -> PostgresSyncConnection:
        """Open a :class:`psycopg.Connection`.

        Parameters are forwarded to :func:`psycopg.connect`; the first
        positional argument (or ``conninfo`` keyword) is the DSN string.

        Raises
        ------
        RuntimeError
            If ``psycopg`` is not installed.
        """
        try:
            import psycopg as _pg  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "psycopg is required for sync PostgreSQL support. "
                "Install with: pip install nextorm[postgres]"
            ) from exc
        raw = _pg.connect(*args, **kwargs)
        return PostgresSyncConnection(raw)

    def execute_ddl(self, connection: SyncConnection, statements: list[str]) -> None:
        """Execute DDL *statements* on *connection*.

        Each statement is executed individually inside the current transaction.
        DDL in PostgreSQL is transactional, so all statements share the same
        transaction context.
        """
        if not statements:
            return
        assert isinstance(connection, PostgresSyncConnection)
        cur = connection._conn.cursor()
        for stmt in statements:
            cur.execute(stmt.rstrip(";"))  # pyright: ignore[reportCallIssue, reportArgumentType]
        cur.close()

    def introspect(self, connection: SyncConnection) -> dict[str, Table]:
        """Return the current schema of a live PostgreSQL connection."""
        from nextorm.schema.introspect import introspect_postgres as _introspect  # noqa: PLC0415

        return _introspect(connection)


# ---------------------------------------------------------------------------
# Async — psycopg v3 (psycopg.AsyncConnection / AsyncCursor)
# ---------------------------------------------------------------------------


class PostgresAsyncCursor(AsyncCursor):
    """Wraps a :class:`psycopg.AsyncCursor`.

    Results are eagerly buffered after :meth:`execute` so that
    :meth:`fetchone`, :meth:`fetchmany`, and :meth:`fetchall` can be called
    synchronously, matching the :class:`~nextorm.providers.base.AsyncCursor`
    interface used by the rest of the codebase.
    """

    def __init__(self, cursor: psycopg.AsyncCursor[psycopg.rows.TupleRow]) -> None:
        self._cursor = cursor
        self._rows: list[DbRow] | None = None
        self._pos: int = 0
        self._rowcount: int = -1

    @property
    def description(self) -> Sequence[tuple[str, Any, Any, Any, Any, Any, Any]] | None:
        return self._cursor.description  # type: ignore[return-value]

    @property
    def rowcount(self) -> int:
        return self._rowcount

    @property
    def lastrowid(self) -> int | None:
        return None  # use RETURNING for insert ids

    async def execute(
        self,
        sql: str,
        parameters: Sequence[Any] | Mapping[str, Any] = (),
    ) -> None:
        await self._cursor.execute(sql, parameters)  # pyright: ignore[reportArgumentType]
        if self._cursor.description is not None:
            raw = await self._cursor.fetchall()
            self._rows = list(raw)
            self._rowcount = len(self._rows)
        else:
            self._rows = []
            self._rowcount = max(self._cursor.rowcount, 0)
        self._pos = 0

    async def executemany(
        self,
        sql: str,
        seq_of_parameters: Sequence[Sequence[Any]],
    ) -> None:
        await self._cursor.executemany(sql, seq_of_parameters)  # pyright: ignore[reportArgumentType]
        self._rows = []
        self._rowcount = max(self._cursor.rowcount, 0)

    def fetchone(self) -> DbRow | None:  # type: ignore[override]
        if self._rows is None or self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchmany(self, size: int = 1) -> Sequence[DbRow]:  # type: ignore[override]
        if not self._rows:
            return []
        chunk = self._rows[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk

    def fetchall(self) -> Sequence[DbRow]:  # type: ignore[override]
        if not self._rows:
            return []
        remaining = self._rows[self._pos :]
        self._pos = len(self._rows)
        return remaining

    async def close(self) -> None:
        await self._cursor.close()


class PostgresAsyncConnection(AsyncConnection):
    """Wraps a :class:`psycopg.AsyncConnection`."""

    def __init__(self, conn: psycopg.AsyncConnection[psycopg.rows.TupleRow]) -> None:
        self._conn = conn

    async def cursor(self) -> PostgresAsyncCursor:
        return PostgresAsyncCursor(self._conn.cursor())

    async def commit(self) -> None:
        await self._conn.commit()

    async def rollback(self) -> None:
        await self._conn.rollback()

    async def close(self) -> None:
        await self._conn.close()

    async def executescript(self, sql: str) -> None:
        """Execute multiple semicolon-separated SQL statements."""
        for stmt in sql.split(";"):
            stmt = stmt.strip()
            if stmt:
                await self._conn.execute(stmt)  # pyright: ignore[reportCallIssue, reportArgumentType]

    @property
    def raw(self) -> psycopg.AsyncConnection[psycopg.rows.TupleRow]:
        """The underlying :class:`psycopg.AsyncConnection`."""
        return self._conn


class PostgresAsyncProvider(AsyncProvider):
    """Asynchronous PostgreSQL provider (:mod:`psycopg` v3).

    Uses :class:`psycopg.AsyncConnection` — the same driver as the sync
    provider, so no separate async driver is required.

    Requires ``psycopg[binary]`` to be installed (``pip install nextorm[postgres]``).
    """

    name = "postgres"
    param_style = "format"

    def placeholder(self, param_name: str | None = None) -> str:
        """Return ``"%s"`` — psycopg uses format-style positional parameters."""
        return "%s"

    async def connect(self, *args: Any, **kwargs: Any) -> PostgresAsyncConnection:
        """Open a :class:`psycopg.AsyncConnection`.

        Parameters are forwarded to :func:`psycopg.AsyncConnection.connect`;
        the first positional argument (or ``conninfo`` keyword) is the DSN string.

        Raises
        ------
        RuntimeError
            If ``psycopg`` is not installed.
        """
        try:
            import psycopg as _pg  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "psycopg is required for async PostgreSQL support. "
                "Install with: pip install nextorm[postgres]"
            ) from exc
        raw = await _pg.AsyncConnection.connect(*args, **kwargs)
        return PostgresAsyncConnection(raw)

    async def execute_ddl(self, connection: AsyncConnection, statements: list[str]) -> None:
        """Execute DDL *statements* on *connection*."""
        if not statements:
            return
        assert isinstance(connection, PostgresAsyncConnection)
        for stmt in statements:
            await connection._conn.execute(stmt.rstrip(";"))  # pyright: ignore[reportCallIssue, reportArgumentType]


# ---------------------------------------------------------------------------
# Registration — runs once at module import time
# ---------------------------------------------------------------------------

register_provider("postgres", sync=PostgresSyncProvider, async_=PostgresAsyncProvider)
