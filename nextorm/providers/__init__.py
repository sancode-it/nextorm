"""Built-in database providers for nextorm.

Importing this package triggers registration of all built-in providers.
Currently registered providers:

- ``"sqlite"`` — sync: :class:`~nextorm.providers.sqlite.SQLiteSyncProvider`,
  async: :class:`~nextorm.providers.sqlite.SQLiteAsyncProvider`
- ``"postgres"`` — sync: :class:`~nextorm.providers.postgres.PostgresSyncProvider`,
  async: :class:`~nextorm.providers.postgres.PostgresAsyncProvider`
- ``"mariadb"`` — sync: :class:`~nextorm.providers.mariadb.MariaDBSyncProvider`,
  async: :class:`~nextorm.providers.mariadb.MariaDBAsyncProvider`

Public ABC re-exports
---------------------
The following names are re-exported here for convenience so that library
consumers only need to import from ``nextorm.providers``:

- :class:`ProviderBase`
- :class:`SyncProvider`, :class:`SyncConnection`, :class:`SyncCursor`
- :class:`AsyncProvider`, :class:`AsyncConnection`, :class:`AsyncCursor`
- :data:`DbRow`
- :func:`register_provider`, :func:`get_sync_provider`,
  :func:`get_async_provider`, :func:`registered_providers`
"""

from __future__ import annotations

# Side-effect imports: register built-in providers.
from nextorm.providers import (
    mariadb as _mariadb,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)
from nextorm.providers import (
    postgres as _postgres,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)
from nextorm.providers import sqlite as _sqlite  # noqa: F401  # pyright: ignore[reportUnusedImport]
from nextorm.providers.base import (
    AsyncConnection,
    AsyncCursor,
    AsyncProvider,
    DbRow,
    ProviderBase,
    SyncConnection,
    SyncCursor,
    SyncProvider,
    get_async_provider,
    get_sync_provider,
    register_provider,
    registered_providers,
)

__all__ = [
    # ABCs
    "ProviderBase",
    "SyncProvider",
    "SyncConnection",
    "SyncCursor",
    "AsyncProvider",
    "AsyncConnection",
    "AsyncCursor",
    # Type aliases
    "DbRow",
    # Registry helpers
    "register_provider",
    "get_sync_provider",
    "get_async_provider",
    "registered_providers",
]
