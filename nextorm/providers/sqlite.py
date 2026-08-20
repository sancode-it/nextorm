"""SQLite provider — sync (stdlib sqlite3) and async (aiosqlite) implementations.

Sync provider uses the stdlib ``sqlite3`` module (always available).
Async provider uses ``aiosqlite`` (optional; install ``nextorm[sqlite]``).

Both are registered under the name ``"sqlite"`` via :func:`register_provider`.
"""

from __future__ import annotations

import datetime
import sqlite3
import uuid as _uuid_stdlib
from collections.abc import Mapping, Sequence
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
    import aiosqlite

    from nextorm.schema.core import Table

# Register UUID adapter: sqlite3 cannot bind uuid.UUID natively; convert to the
# canonical hyphenated hex string ("xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx").
sqlite3.register_adapter(_uuid_stdlib.UUID, str)

# Register datetime adapter: explicitly convert to ISO format string.
sqlite3.register_adapter(datetime.datetime, lambda dt: dt.replace(tzinfo=None).isoformat())

# Register date adapter: explicitly convert to ISO format string.
sqlite3.register_adapter(datetime.date, lambda d: d.isoformat())

# Register timedelta adapter: sqlite3 stores as INTEGER microseconds.
# Read-back returns int; the application is responsible for converting back to timedelta.
sqlite3.register_adapter(
    datetime.timedelta,
    lambda td: int(td.total_seconds() * 1_000_000),
)

__all__ = [
    "SQLiteSyncCursor",
    "SQLiteSyncConnection",
    "SQLiteSyncProvider",
    "SQLiteAsyncCursor",
    "SQLiteAsyncConnection",
    "SQLiteAsyncProvider",
]


# ---------------------------------------------------------------------------
# Sync — stdlib sqlite3
# ---------------------------------------------------------------------------


class SQLiteSyncCursor(SyncCursor):
    """Wraps a :class:`sqlite3.Cursor`."""

    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self._cursor = cursor

    @property
    def description(self) -> Sequence[tuple[str, Any, Any, Any, Any, Any, Any]] | None:
        return cast(
            "Sequence[tuple[str, Any, Any, Any, Any, Any, Any]] | None",
            self._cursor.description,
        )

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
        return cast("DbRow | None", self._cursor.fetchone())

    def fetchmany(self, size: int = 1) -> Sequence[DbRow]:
        return cast("Sequence[DbRow]", self._cursor.fetchmany(size))

    def fetchall(self) -> Sequence[DbRow]:
        return cast("Sequence[DbRow]", self._cursor.fetchall())

    def close(self) -> None:
        self._cursor.close()


class SQLiteSyncConnection(SyncConnection):
    """Wraps a :class:`sqlite3.Connection`."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def cursor(self) -> SQLiteSyncCursor:
        return SQLiteSyncCursor(self._conn.cursor())

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    def executescript(self, sql: str) -> None:
        self._conn.executescript(sql)

    @property
    def raw(self) -> sqlite3.Connection:
        """The underlying :class:`sqlite3.Connection`."""
        return self._conn


class SQLiteSyncProvider(SyncProvider):
    """Synchronous SQLite provider (stdlib :mod:`sqlite3`)."""

    name = "sqlite"
    param_style = "qmark"

    def placeholder(self, param_name: str | None = None) -> str:
        """Return ``"?"`` — sqlite3 uses qmark-style positional parameters."""
        return "?"

    def connect(self, *args: Any, **kwargs: Any) -> SQLiteSyncConnection:
        """Open a :class:`sqlite3.Connection`.

        Parameters are forwarded directly to :func:`sqlite3.connect`; the
        first positional argument is the database path or ``":memory:"``.

        The keyword argument ``filename`` is accepted as an alias for
        ``database`` for compatibility with Pony ORM-style configurations.
        """
        if "filename" in kwargs and not args:
            kwargs["database"] = kwargs.pop("filename")
        return SQLiteSyncConnection(sqlite3.connect(*args, **kwargs))

    def execute_ddl(self, connection: SyncConnection, statements: list[str]) -> None:
        """Execute DDL *statements* on *connection* as a single script.

        Uses :meth:`sqlite3.Connection.executescript` which runs all
        statements atomically and commits any pending transaction first.
        """
        if not statements:
            return
        assert isinstance(connection, SQLiteSyncConnection)
        # executescript requires each statement to end with a semicolon.
        script = ";\n".join(s.rstrip(";") for s in statements) + ";"
        connection.executescript(script)

    def introspect(self, connection: SyncConnection) -> dict[str, Table]:
        """Return the current schema of a live SQLite connection."""
        from nextorm.schema.introspect import (
            introspect_sqlite as _introspect,  # noqa: PLC0415
        )

        return _introspect(connection)


# ---------------------------------------------------------------------------
# Async — aiosqlite (lazy import; install nextorm[sqlite])
# ---------------------------------------------------------------------------


class SQLiteAsyncCursor(AsyncCursor):
    """Wraps an :class:`aiosqlite.Cursor`.

    Instances are created by :class:`SQLiteAsyncConnection`; obtain one by
    calling ``await connection.cursor()``.
    """

    def __init__(self, cursor: aiosqlite.Cursor) -> None:
        self._cursor = cursor

    @property
    def description(self) -> Sequence[tuple[str, Any, Any, Any, Any, Any, Any]] | None:
        return cast(
            "Sequence[tuple[str, Any, Any, Any, Any, Any, Any]] | None",
            self._cursor.description,
        )

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
        await self._cursor.execute(
            sql, list(parameters) if not isinstance(parameters, Mapping) else parameters
        )

    async def executemany(
        self,
        sql: str,
        seq_of_parameters: Sequence[Sequence[Any]],
    ) -> None:
        await self._cursor.executemany(sql, seq_of_parameters)

    async def fetchone(self) -> DbRow | None:
        return cast("DbRow | None", await self._cursor.fetchone())

    async def fetchmany(self, size: int = 1) -> Sequence[DbRow]:
        return cast("Sequence[DbRow]", await self._cursor.fetchmany(size))

    async def fetchall(self) -> Sequence[DbRow]:
        return cast("Sequence[DbRow]", await self._cursor.fetchall())

    async def close(self) -> None:
        await self._cursor.close()


class SQLiteAsyncConnection(AsyncConnection):
    """Wraps an :class:`aiosqlite.Connection`.

    Instances are created by :class:`SQLiteAsyncProvider`; obtain one by
    calling ``await provider.connect(database)``.
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def cursor(self) -> SQLiteAsyncCursor:
        # aiosqlite.Connection.cursor() returns a Result that is both awaitable
        # and an async context manager; awaiting it returns the Cursor directly.
        raw_cur = await self._conn.cursor()
        return SQLiteAsyncCursor(raw_cur)

    async def commit(self) -> None:
        await self._conn.commit()

    async def rollback(self) -> None:
        await self._conn.rollback()

    async def close(self) -> None:
        await self._conn.close()

    async def executescript(self, sql: str) -> None:
        await self._conn.executescript(sql)

    @property
    def raw(self) -> aiosqlite.Connection:
        """The underlying :class:`aiosqlite.Connection`."""
        return self._conn


class SQLiteAsyncProvider(AsyncProvider):
    """Asynchronous SQLite provider (:mod:`aiosqlite`).

    Requires ``aiosqlite`` to be installed (``pip install nextorm[sqlite]``).
    """

    name = "sqlite"
    param_style = "qmark"

    def placeholder(self, param_name: str | None = None) -> str:
        """Return ``"?"`` — aiosqlite uses qmark-style positional parameters."""
        return "?"

    async def connect(self, *args: Any, **kwargs: Any) -> SQLiteAsyncConnection:
        """Open an :class:`aiosqlite.Connection`.

        Parameters are forwarded to :func:`aiosqlite.connect`; the first
        positional argument is the database path or ``":memory:"``.

        The keyword argument ``filename`` is accepted as an alias for
        ``database`` for compatibility with Pony ORM-style configurations.

        Raises
        ------
        RuntimeError
            If ``aiosqlite`` is not installed.
        """
        if "filename" in kwargs and not args:
            kwargs["database"] = kwargs.pop("filename")
        try:
            import aiosqlite as _ao  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "aiosqlite is required for async SQLite support. "
                "Install with: pip install nextorm[sqlite]"
            ) from exc
        raw_conn = _ao.connect(*args, **kwargs)
        await raw_conn  # calls Connection.__await__date() to open the connection
        return SQLiteAsyncConnection(raw_conn)

    async def execute_ddl(self, connection: AsyncConnection, statements: list[str]) -> None:
        """Execute DDL *statements* on *connection* as a single script."""
        if not statements:
            return
        assert isinstance(connection, SQLiteAsyncConnection)
        script = ";\n".join(s.rstrip(";") for s in statements) + ";"
        await connection.executescript(script)


# ---------------------------------------------------------------------------
# Registration — runs once at module import time
# ---------------------------------------------------------------------------

register_provider("sqlite", sync=SQLiteSyncProvider, async_=SQLiteAsyncProvider)
