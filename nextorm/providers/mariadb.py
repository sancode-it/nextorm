"""MariaDB provider — sync (PyMySQL) and async (asyncmy) implementations.

Sync provider uses :mod:`pymysql` (install ``nextorm[mariadb]``).
Async provider uses :mod:`asyncmy` (install ``nextorm[mariadb]``).

Registered under the name ``"mariadb"`` via :func:`register_provider`."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    import asyncmy.connection
    import asyncmy.cursors
    import pymysql
    import pymysql.connections
    import pymysql.cursors

    from nextorm.schema.core import Table

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

__all__ = [
    "MariaDBSyncCursor",
    "MariaDBSyncConnection",
    "MariaDBSyncProvider",
    "MariaDBAsyncCursor",
    "MariaDBAsyncConnection",
    "MariaDBAsyncProvider",
]


# ---------------------------------------------------------------------------
# Sync — PyMySQL
# ---------------------------------------------------------------------------


class MariaDBSyncCursor(SyncCursor):
    """Wraps a :class:`pymysql.cursors.Cursor`."""

    def __init__(self, cursor: pymysql.cursors.Cursor) -> None:
        self._cursor = cursor

    @property
    def description(self) -> Sequence[tuple[str, Any, Any, Any, Any, Any, Any]] | None:
        return self._cursor.description  # type: ignore[return-value]

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @property
    def lastrowid(self) -> int | None:
        return self._cursor.lastrowid

    def execute(
        self,
        sql: str,
        parameters: Sequence[Any] | Mapping[str, Any] = (),
    ) -> None:
        self._cursor.execute(sql, parameters)

    def executemany(
        self,
        sql: str,
        seq_of_parameters: Sequence[Sequence[Any]],
    ) -> None:
        self._cursor.executemany(sql, seq_of_parameters)

    def fetchone(self) -> DbRow | None:
        return self._cursor.fetchone()

    def fetchmany(self, size: int = 1) -> Sequence[DbRow]:
        return cast("Sequence[DbRow]", self._cursor.fetchmany(size))

    def fetchall(self) -> Sequence[DbRow]:
        return cast("Sequence[DbRow]", self._cursor.fetchall())

    def close(self) -> None:
        self._cursor.close()


class MariaDBSyncConnection(SyncConnection):
    """Wraps a :class:`pymysql.connections.Connection`."""

    def __init__(self, conn: pymysql.connections.Connection) -> None:
        self._conn = conn

    def cursor(self) -> MariaDBSyncCursor:
        return MariaDBSyncCursor(self._conn.cursor())

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    def executescript(self, sql: str) -> None:
        """Execute multiple semicolon-separated SQL statements."""
        cur = self._conn.cursor()
        for stmt in sql.split(";"):
            stmt = stmt.strip()
            if stmt:
                cur.execute(stmt)
        cur.close()

    @property
    def raw(self) -> pymysql.connections.Connection:
        """The underlying :class:`pymysql.connections.Connection`."""
        return self._conn


class MariaDBSyncProvider(SyncProvider):
    """Synchronous MariaDB/MySQL provider (:mod:`pymysql`).

    Requires ``pymysql`` to be installed (``pip install nextorm[mariadb]``).
    """

    name = "mariadb"
    param_style = "format"

    def placeholder(self, param_name: str | None = None) -> str:
        """Return ``"%s"`` — PyMySQL uses format-style positional parameters."""
        return "%s"

    def connect(self, *args: Any, **kwargs: Any) -> MariaDBSyncConnection:
        """Open a :class:`pymysql.connections.Connection`.

        Parameters are forwarded to :func:`pymysql.connect`; typical keyword
        arguments are ``host``, ``user``, ``password``, ``database``.

        Raises
        ------
        RuntimeError
            If ``pymysql`` is not installed.
        """
        try:
            import pymysql as _my  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "pymysql is required for sync MariaDB/MySQL support. "
                "Install with: pip install nextorm[mariadb]"
            ) from exc
        raw = _my.connect(*args, **kwargs)
        return MariaDBSyncConnection(raw)

    def execute_ddl(self, connection: SyncConnection, statements: list[str]) -> None:
        """Execute DDL *statements* on *connection*.

        Each statement is executed individually; MariaDB DDL implicitly commits
        the current transaction.
        """
        if not statements:
            return
        assert isinstance(connection, MariaDBSyncConnection)
        cur = connection._conn.cursor()
        for stmt in statements:
            cur.execute(stmt.rstrip(";"))
        cur.close()

    def introspect(self, connection: SyncConnection) -> dict[str, Table]:
        """Return the current schema of a live MariaDB/MySQL connection."""
        from nextorm.schema.introspect import introspect_mariadb as _introspect  # noqa: PLC0415

        return _introspect(connection)


# ---------------------------------------------------------------------------
# Async — aiomysql
# ---------------------------------------------------------------------------


class MariaDBAsyncCursor(AsyncCursor):
    """Wraps an :class:`asyncmy.cursors.Cursor`.

    Instances are created by :class:`MariaDBAsyncConnection`; obtain one by
    calling ``await connection.cursor()``.
    """

    def __init__(self, cursor: asyncmy.cursors.Cursor) -> None:
        self._cursor = cursor

    @property
    def description(self) -> Sequence[tuple[str, Any, Any, Any, Any, Any, Any]] | None:
        return self._cursor.description

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @property
    def lastrowid(self) -> int | None:
        return self._cursor.lastrowid

    async def execute(
        self,
        sql: str,
        parameters: Sequence[Any] | Mapping[str, Any] = (),
    ) -> None:
        await self._cursor.execute(sql, parameters)

    async def executemany(
        self,
        sql: str,
        seq_of_parameters: Sequence[Sequence[Any]],
    ) -> None:
        await self._cursor.executemany(sql, seq_of_parameters)

    async def fetchone(self) -> DbRow | None:
        return await self._cursor.fetchone()

    async def fetchmany(self, size: int = 1) -> Sequence[DbRow]:
        return await self._cursor.fetchmany(size)

    async def fetchall(self) -> Sequence[DbRow]:
        return await self._cursor.fetchall()

    async def close(self) -> None:
        await self._cursor.close()


class MariaDBAsyncConnection(AsyncConnection):
    """Wraps an :class:`asyncmy.connection.Connection`."""

    def __init__(self, conn: asyncmy.connection.Connection) -> None:
        self._conn = conn

    async def cursor(self) -> MariaDBAsyncCursor:
        raw_cur = self._conn.cursor()
        return MariaDBAsyncCursor(raw_cur)

    async def commit(self) -> None:
        await self._conn.commit()

    async def rollback(self) -> None:
        await self._conn.rollback()

    async def close(self) -> None:
        self._conn.close()

    async def executescript(self, sql: str) -> None:
        """Execute multiple semicolon-separated SQL statements."""
        cur = self._conn.cursor()
        for stmt in sql.split(";"):
            stmt = stmt.strip()
            if stmt:
                await cur.execute(stmt)
        await cur.close()

    @property
    def raw(self) -> asyncmy.connection.Connection:
        """The underlying :class:`asyncmy.connection.Connection`."""
        return self._conn


class MariaDBAsyncProvider(AsyncProvider):
    """Asynchronous MariaDB/MySQL provider (:mod:`asyncmy`).

    Requires ``asyncmy`` to be installed (``pip install nextorm[mariadb]``).
    """

    name = "mariadb"
    param_style = "format"

    def placeholder(self, param_name: str | None = None) -> str:
        """Return ``"%s"`` — asyncmy uses format-style positional parameters."""
        return "%s"

    async def connect(self, *args: Any, **kwargs: Any) -> MariaDBAsyncConnection:
        """Open an :class:`asyncmy.connection.Connection`.

        Parameters are forwarded to :func:`asyncmy.connect`; typical keyword
        arguments are ``host``, ``user``, ``password``, ``database``.

        Raises
        ------
        RuntimeError
            If ``asyncmy`` is not installed.
        """
        try:
            import asyncmy as _am  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "asyncmy is required for async MariaDB/MySQL support. "
                "Install with: pip install nextorm[mariadb]"
            ) from exc
        raw = await _am.connect(*args, **kwargs)
        return MariaDBAsyncConnection(raw)

    async def execute_ddl(self, connection: AsyncConnection, statements: list[str]) -> None:
        """Execute DDL *statements* on *connection*."""
        if not statements:
            return
        assert isinstance(connection, MariaDBAsyncConnection)
        cur = connection._conn.cursor()
        for stmt in statements:
            await cur.execute(stmt.rstrip(";"))
        await cur.close()


# ---------------------------------------------------------------------------
# Registration — runs once at module import time
# ---------------------------------------------------------------------------

register_provider("mariadb", sync=MariaDBSyncProvider, async_=MariaDBAsyncProvider)
