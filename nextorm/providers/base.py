"""Provider ABCs and shared DBAPI-2 / async cursor Protocols.

A *provider* bridges nextorm's dialect-neutral SQL layer to a specific
database driver.  Two parallel hierarchies exist:

- **Sync** (DBAPI-2): ``SyncCursor``, ``SyncConnection``, ``SyncProvider``
- **Async** (native async drivers): ``AsyncCursor``, ``AsyncConnection``, ``AsyncProvider``

Both hierarchies share ``ProviderBase`` for common metadata (``name``,
``param_style``).

Implementing a new provider
---------------------------

1. Subclass ``SyncProvider`` *and/or* ``AsyncProvider``.
2. Implement the abstract methods.
3. Register the provider name in ``nextorm.database._PROVIDERS``.

Param-style
-----------
``param_style`` mirrors the DBAPI-2 ``paramstyle`` attribute:

- ``"qmark"``  — ``?``  (SQLite sync, aiosqlite)
- ``"format"`` — ``%s`` (psycopg2, mysqlclient)
- ``"numeric"`` — ``:1``, ``:2`` (cx_Oracle)
- ``"named"``  — ``:name``
- ``"pyformat"`` — ``%(name)s``
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from nextorm.schema.core import Table

__all__ = [
    # Protocols
    "DbRow",
    "SyncCursor",
    "SyncConnection",
    "AsyncCursor",
    "AsyncConnection",
    # Provider ABCs
    "ProviderBase",
    "SyncProvider",
    "AsyncProvider",
]

# ---------------------------------------------------------------------------
# Shared type aliases
# ---------------------------------------------------------------------------

DbRow = tuple[Any, ...]
"""A single row returned by a cursor — a plain tuple of column values."""

# ---------------------------------------------------------------------------
# Sync (DBAPI-2) Protocols
# ---------------------------------------------------------------------------


class SyncCursor(ABC):
    """Minimal sync cursor interface (DBAPI-2 subset)."""

    @property
    @abstractmethod
    def description(self) -> Sequence[tuple[str, Any, Any, Any, Any, Any, Any]] | None:
        """Column descriptions from the last executed query."""

    @property
    @abstractmethod
    def rowcount(self) -> int:
        """Number of rows affected / returned by the last statement."""

    @property
    @abstractmethod
    def lastrowid(self) -> int | None:
        """Row-id of the last INSERT (``None`` if not applicable)."""

    @abstractmethod
    def execute(
        self,
        sql: str,
        parameters: Sequence[Any] | Mapping[str, Any] = (),
    ) -> None:
        """Execute a single SQL statement."""

    @abstractmethod
    def executemany(
        self,
        sql: str,
        seq_of_parameters: Sequence[Sequence[Any]],
    ) -> None:
        """Execute a parameterised statement for each row in *seq_of_parameters*."""

    @abstractmethod
    def fetchone(self) -> DbRow | None:
        """Return the next row, or ``None`` if no more rows."""

    @abstractmethod
    def fetchmany(self, size: int = 1) -> Sequence[DbRow]:
        """Return up to *size* rows."""

    @abstractmethod
    def fetchall(self) -> Sequence[DbRow]:
        """Return all remaining rows."""

    @abstractmethod
    def close(self) -> None:
        """Release cursor resources."""


class SyncConnection(ABC):
    """Minimal sync connection interface (DBAPI-2 subset)."""

    @abstractmethod
    def cursor(self) -> SyncCursor:
        """Return a new cursor for this connection."""

    @abstractmethod
    def commit(self) -> None:
        """Commit the current transaction."""

    @abstractmethod
    def rollback(self) -> None:
        """Roll back the current transaction."""

    @abstractmethod
    def close(self) -> None:
        """Close the connection."""

    @abstractmethod
    def executescript(self, sql: str) -> None:
        """Execute a multi-statement SQL script (used for DDL)."""


# ---------------------------------------------------------------------------
# Async Protocols
# ---------------------------------------------------------------------------


class AsyncCursor(ABC):
    """Minimal async cursor interface (mirrors DBAPI-2 but with ``await``)."""

    @property
    @abstractmethod
    def description(self) -> Sequence[tuple[str, Any, Any, Any, Any, Any, Any]] | None:
        """Column descriptions from the last executed query."""

    @property
    @abstractmethod
    def rowcount(self) -> int:
        """Number of rows affected / returned by the last statement."""

    @property
    @abstractmethod
    def lastrowid(self) -> int | None:
        """Row-id of the last INSERT (``None`` if not applicable)."""

    @abstractmethod
    async def execute(
        self,
        sql: str,
        parameters: Sequence[Any] | Mapping[str, Any] = (),
    ) -> None:
        """Execute a single SQL statement asynchronously."""

    @abstractmethod
    async def executemany(
        self,
        sql: str,
        seq_of_parameters: Sequence[Sequence[Any]],
    ) -> None:
        """Execute a parameterised statement for each row asynchronously."""

    @abstractmethod
    async def fetchone(self) -> DbRow | None:
        """Return the next row asynchronously."""

    @abstractmethod
    async def fetchmany(self, size: int = 1) -> Sequence[DbRow]:
        """Return up to *size* rows asynchronously."""

    @abstractmethod
    async def fetchall(self) -> Sequence[DbRow]:
        """Return all remaining rows asynchronously."""

    @abstractmethod
    async def close(self) -> None:
        """Release cursor resources asynchronously."""


class AsyncConnection(ABC):
    """Minimal async connection interface."""

    @abstractmethod
    async def cursor(self) -> AsyncCursor:
        """Return a new async cursor."""

    @abstractmethod
    async def commit(self) -> None:
        """Commit the current transaction asynchronously."""

    @abstractmethod
    async def rollback(self) -> None:
        """Roll back the current transaction asynchronously."""

    @abstractmethod
    async def close(self) -> None:
        """Close the connection asynchronously."""

    @abstractmethod
    async def executescript(self, sql: str) -> None:
        """Execute a multi-statement SQL script asynchronously."""


# ---------------------------------------------------------------------------
# Provider ABCs
# ---------------------------------------------------------------------------


class ProviderBase(ABC):
    """Common metadata shared by sync and async providers."""

    #: Short name used in ``Database.bind("sqlite", …)``.
    name: str

    #: DBAPI-2 param-style: ``"qmark"``, ``"format"``, ``"numeric"``,
    #: ``"named"``, or ``"pyformat"``.
    param_style: str

    @abstractmethod
    def placeholder(self, param_name: str | None = None) -> str:
        """Return the SQL placeholder string for a single parameter.

        Parameters
        ----------
        param_name:
            The logical name of the parameter (used by named-style drivers).
            Ignored by positional-style drivers.
        """


class SyncProvider(ProviderBase):
    """Abstract base for synchronous (DBAPI-2) database providers.

    Concrete subclasses must implement :meth:`connect` and the abstract
    methods inherited from :class:`ProviderBase`.
    """

    @abstractmethod
    def connect(self, *args: Any, **kwargs: Any) -> SyncConnection:
        """Open a new synchronous connection.

        The returned object must implement :class:`SyncConnection`.

        Parameters
        ----------
        \\*args / \\*\\*kwargs:
            Driver-specific connection arguments (forwarded from
            ``Database.bind(provider, *args, **kwargs)``).
        """

    @abstractmethod
    def execute_ddl(self, connection: SyncConnection, statements: list[str]) -> None:
        """Execute a list of DDL statements on *connection*.

        Provides a hook for providers that need special handling (e.g.
        SQLite's ``executescript`` disables autocommit).
        """

    @abstractmethod
    def introspect(self, connection: SyncConnection) -> dict[str, Table]:
        """Read and return the current schema from *connection*.

        Returns a ``{table_name: Table}`` mapping reflecting the tables,
        columns, and indexes that currently exist in the database.  Used
        by :meth:`~nextorm.database.Database.migrate` to compute the diff
        between the live schema and the entity-derived target schema.
        """


class AsyncProvider(ProviderBase):
    """Abstract base for async database providers.

    Concrete subclasses must implement :meth:`connect` and the abstract
    methods inherited from :class:`ProviderBase`.
    """

    @abstractmethod
    async def connect(self, *args: Any, **kwargs: Any) -> AsyncConnection:
        """Open a new async connection."""

    @abstractmethod
    async def execute_ddl(self, connection: AsyncConnection, statements: list[str]) -> None:
        """Execute a list of DDL statements asynchronously."""


# ---------------------------------------------------------------------------
# Registry helper used by Database
# ---------------------------------------------------------------------------


#: Maps provider name → (SyncProvider class | None, AsyncProvider class | None)
ProviderEntry = tuple[type[SyncProvider] | None, type[AsyncProvider] | None]

_PROVIDER_REGISTRY: dict[str, ProviderEntry] = {}


def register_provider(
    name: str,
    *,
    sync: type[SyncProvider] | None = None,
    async_: type[AsyncProvider] | None = None,
) -> None:
    """Register sync and/or async provider classes under *name*.

    Called at module import time by each provider module.
    """
    if sync is None and async_ is None:
        raise ValueError(f"register_provider({name!r}): must supply at least one of sync=/async_=")
    _PROVIDER_REGISTRY[name] = (sync, async_)


def get_sync_provider(name: str) -> type[SyncProvider]:
    """Return the sync provider class for *name*, or raise ``ValueError``."""
    entry = _PROVIDER_REGISTRY.get(name)
    if entry is None or entry[0] is None:
        available = [k for k, v in _PROVIDER_REGISTRY.items() if v[0] is not None]
        raise ValueError(
            f"No sync provider registered for {name!r}. "
            f"Available sync providers: {sorted(available)}"
        )
    return entry[0]


def get_async_provider(name: str) -> type[AsyncProvider]:
    """Return the async provider class for *name*, or raise ``ValueError``."""
    entry = _PROVIDER_REGISTRY.get(name)
    if entry is None or entry[1] is None:
        available = [k for k, v in _PROVIDER_REGISTRY.items() if v[1] is not None]
        raise ValueError(
            f"No async provider registered for {name!r}. "
            f"Available async providers: {sorted(available)}"
        )
    return entry[1]


def registered_providers() -> list[str]:
    """Return a sorted list of all registered provider names."""
    return sorted(_PROVIDER_REGISTRY)
