"""SQLite schema introspection — reads live DB schema via PRAGMA calls.

This module provides a single function, :func:`introspect_sqlite`, that
queries a live SQLite connection and returns the current schema as a
``{table_name: Table}`` mapping.  The result is suitable for passing to
:func:`~nextorm.schema.diff.diff_schemas` to compute pending migrations.

Only explicitly-created indexes (``origin='c'``) are included.  Implicit
indexes auto-created by SQLite for ``PRIMARY KEY`` and ``UNIQUE`` constraints
are skipped because they are not represented as :class:`~nextorm.schema.core.Index`
objects in the target schema — their equivalent constraints are encoded in the
:class:`~nextorm.schema.core.Column` definition instead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from nextorm.fields import AttrValue
from nextorm.providers.base import SyncConnection
from nextorm.schema.core import Column, Index, Table

if TYPE_CHECKING:
    from nextorm.providers.base import AsyncConnection

__all__ = [
    "introspect_sqlite",
    "introspect_postgres",
    "introspect_mariadb",
    "async_introspect_sqlite",
]


def introspect_sqlite(conn: SyncConnection) -> dict[str, Table]:
    """Return the current schema of a live SQLite connection.

    Parameters
    ----------
    conn:
        An open :class:`~nextorm.providers.base.SyncConnection` to the target
        SQLite database.

    Returns
    -------
    dict[str, Table]
        Mapping of table name → :class:`~nextorm.schema.core.Table`.
        System tables (``sqlite_*``) are excluded.
        :class:`~nextorm.schema.core.Column` objects carry ``py_type=object``
        because the Python type cannot be reconstructed from a SQLite type
        affinity string; this is sufficient for schema-diffing, which only
        compares column *names*.
    """
    cur = conn.cursor()

    # Discover all user tables (skip sqlite_* system tables)
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    table_names: list[str] = [row[0] for row in cur.fetchall()]

    tables: dict[str, Table] = {}
    for table_name in table_names:
        # ---- columns via PRAGMA table_info ----------------------------
        # Result columns: cid, name, type, notnull, dflt_value, pk
        cur.execute(f'PRAGMA table_info("{table_name}")')  # noqa: S608
        columns = [
            Column(
                name=row[1],
                py_type=cast("type[AttrValue]", object),
                nullable=not row[3] and not bool(row[5]),  # PK implies NOT NULL
                primary_key=bool(row[5]),
            )
            for row in cur.fetchall()
        ]

        # ---- indexes via PRAGMA index_list / PRAGMA index_info -------
        # index_list result: seq, name, unique, origin, partial
        # origin values:
        #   'c'  = explicitly created via CREATE INDEX
        #   'u'  = implicit index for a UNIQUE column constraint
        #   'pk' = implicit index for a PRIMARY KEY column
        # We only keep origin='c' indexes: those correspond to our
        # explicit Index objects in the schema.
        cur.execute(f'PRAGMA index_list("{table_name}")')  # noqa: S608
        index_rows: list[tuple[object, ...]] = list(cur.fetchall())  # consume before inner exec
        indexes: list[Index] = []
        for row in index_rows:
            idx_name = str(row[1])
            unique = bool(row[2])
            origin = str(row[3])
            if origin != "c":
                # Skip implicit PK / UNIQUE constraint indexes
                continue
            cur.execute(f'PRAGMA index_info("{idx_name}")')  # noqa: S608
            idx_cols = [str(r[2]) for r in cur.fetchall()]
            indexes.append(Index(name=idx_name, columns=idx_cols, unique=unique))

        tables[table_name] = Table(name=table_name, columns=columns, indexes=indexes)

    cur.close()
    return tables


def introspect_postgres(conn: SyncConnection) -> dict[str, Table]:
    """Return the current schema of a live PostgreSQL connection.

    Parameters
    ----------
    conn:
        An open :class:`~nextorm.providers.base.SyncConnection` to the target
        PostgreSQL database.

    Returns
    -------
    dict[str, Table]
        Mapping of table name → :class:`~nextorm.schema.core.Table`.
        System tables (``pg_*``, ``information_schema``) are excluded.
        :class:`~nextorm.schema.core.Column` objects carry ``py_type=object``
        because the Python type cannot be reconstructed from a SQL type string;
        this is sufficient for schema-diffing, which only compares column *names*.
    """
    cur = conn.cursor()

    # Discover all user tables in the current (public) schema
    cur.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_type = 'BASE TABLE'
        ORDER BY table_name
        """
    )
    table_names: list[str] = [row[0] for row in cur.fetchall()]

    tables: dict[str, Table] = {}
    for table_name in table_names:
        # ---- columns via information_schema.columns -----------------------
        cur.execute(
            """
            SELECT column_name, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = %s
            ORDER BY ordinal_position
            """,
            (table_name,),
        )
        columns = [
            Column(
                name=row[0],
                py_type=cast("type[AttrValue]", object),
                nullable=(row[1] == "YES"),
            )
            for row in cur.fetchall()
        ]

        # ---- indexes via pg_indexes ----------------------------------------
        # Exclude primary key constraints (created implicitly by PostgreSQL).
        # We only keep explicitly created indexes (CREATE INDEX …).
        cur.execute(
            """
            SELECT i.relname AS index_name,
                   ix.indisunique AS is_unique,
                   array_agg(a.attname ORDER BY k.ord) AS columns
            FROM pg_class t
            JOIN pg_index ix ON t.oid = ix.indrelid
            JOIN pg_class i  ON i.oid = ix.indexrelid
            JOIN LATERAL unnest(ix.indkey) WITH ORDINALITY AS k(attnum, ord)
                 ON true
            JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
            WHERE t.relname = %s
              AND t.relkind = 'r'
              AND NOT ix.indisprimary
            GROUP BY i.relname, ix.indisunique
            ORDER BY i.relname
            """,
            (table_name,),
        )
        indexes: list[Index] = [
            Index(
                name=row[0],
                columns=list(row[2]),
                unique=bool(row[1]),
            )
            for row in cur.fetchall()
        ]

        tables[table_name] = Table(name=table_name, columns=columns, indexes=indexes)

    cur.close()
    return tables


def introspect_mariadb(conn: SyncConnection) -> dict[str, Table]:
    """Return the current schema of a live MariaDB/MySQL connection.

    Parameters
    ----------
    conn:
        An open :class:`~nextorm.providers.base.SyncConnection` to the target
        MariaDB/MySQL database.

    Returns
    -------
    dict[str, Table]
        Mapping of table name → :class:`~nextorm.schema.core.Table`.
        :class:`~nextorm.schema.core.Column` objects carry ``py_type=object``
        because the Python type cannot be reconstructed from a SQL type string;
        this is sufficient for schema-diffing, which only compares column *names*.
    """
    cur = conn.cursor()

    # Discover current database name
    cur.execute("SELECT DATABASE()")
    db_row = cur.fetchone()
    db_name = db_row[0] if db_row else None

    # Discover all user tables in the current database
    cur.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = %s
          AND table_type = 'BASE TABLE'
        ORDER BY table_name
        """,
        (db_name,),
    )
    table_names: list[str] = [row[0] for row in cur.fetchall()]

    tables: dict[str, Table] = {}
    for table_name in table_names:
        # ---- columns via information_schema.columns -----------------------
        cur.execute(
            """
            SELECT column_name, is_nullable
            FROM information_schema.columns
            WHERE table_schema = %s
              AND table_name = %s
            ORDER BY ordinal_position
            """,
            (db_name, table_name),
        )
        columns = [
            Column(
                name=row[0],
                py_type=cast("type[AttrValue]", object),
                nullable=(row[1] == "YES"),
            )
            for row in cur.fetchall()
        ]

        # ---- indexes via information_schema.statistics --------------------
        # Exclude the PRIMARY key; only keep explicitly-created indexes.
        cur.execute(
            """
            SELECT index_name,
                   MAX(non_unique = 0) AS is_unique,
                   GROUP_CONCAT(column_name ORDER BY seq_in_index) AS cols
            FROM information_schema.statistics
            WHERE table_schema = %s
              AND table_name = %s
              AND index_name != 'PRIMARY'
            GROUP BY index_name
            ORDER BY index_name
            """,
            (db_name, table_name),
        )
        indexes: list[Index] = [
            Index(
                name=row[0],
                columns=row[2].split(","),
                unique=bool(row[1]),
            )
            for row in cur.fetchall()
        ]

        tables[table_name] = Table(name=table_name, columns=columns, indexes=indexes)

    cur.close()
    return tables


async def async_introspect_sqlite(conn: AsyncConnection) -> dict[str, Table]:
    """Async version of :func:`introspect_sqlite` for use with async providers.

    Uses the async cursor API so it is safe to call from asyncio code without
    blocking the event loop.

    Parameters
    ----------
    conn:
        An open :class:`~nextorm.providers.base.AsyncConnection` to a SQLite
        database (typically an :class:`~nextorm.providers.sqlite.SQLiteAsyncConnection`).

    Returns
    -------
    dict[str, Table]
        Mapping of table name → :class:`~nextorm.schema.core.Table`, equivalent
        to :func:`introspect_sqlite`.
    """
    cur = await conn.cursor()

    await cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    table_names: list[str] = [row[0] for row in await cur.fetchall()]

    tables: dict[str, Table] = {}
    for table_name in table_names:
        await cur.execute(f'PRAGMA table_info("{table_name}")')  # noqa: S608
        col_rows = list(await cur.fetchall())
        columns = [
            Column(
                name=row[1],
                py_type=cast("type[AttrValue]", object),
                nullable=not row[3] and not bool(row[5]),
                primary_key=bool(row[5]),
            )
            for row in col_rows
        ]

        await cur.execute(f'PRAGMA index_list("{table_name}")')  # noqa: S608
        index_rows = list(await cur.fetchall())
        indexes: list[Index] = []
        for row in index_rows:
            idx_name = str(row[1])
            unique = bool(row[2])
            origin = str(row[3])
            if origin != "c":
                continue
            await cur.execute(f'PRAGMA index_info("{idx_name}")')  # noqa: S608
            idx_cols = [str(r[2]) for r in await cur.fetchall()]
            indexes.append(Index(name=idx_name, columns=idx_cols, unique=unique))

        tables[table_name] = Table(name=table_name, columns=columns, indexes=indexes)

    await cur.close()
    return tables
