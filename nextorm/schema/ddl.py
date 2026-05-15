"""DDL renderers — translate :class:`SchemaOp` instances to SQL strings."""

from __future__ import annotations

import datetime
import decimal
import enum
import uuid
from abc import ABC, abstractmethod
from typing import Any, assert_never

from nextorm.fields import ULID, DateTimeTz, Json, LongStr, Vec
from nextorm.schema.core import Column, Index, Table
from nextorm.schema.diff import (
    AddColumn,
    AddIndex,
    AlterColumnType,
    CreateTable,
    DropColumn,
    DropIndex,
    DropTable,
    SchemaOp,
)

__all__ = ["DDLRenderer", "SQLiteRenderer", "PostgresRenderer", "MariaDBRenderer"]

# SQLite affinity mapping: Python type → SQL type name
_PY_TO_SQLITE: dict[type[Any], str] = {
    int: "INTEGER",
    str: "TEXT",
    float: "REAL",
    bool: "INTEGER",  # SQLite stores booleans as 0 / 1
    bytes: "BLOB",
    datetime.datetime: "TIMESTAMP",
    datetime.date: "DATE",
    datetime.time: "TIME",
    datetime.timedelta: "INTEGER",  # stored as microseconds
    decimal.Decimal: "NUMERIC",
    uuid.UUID: "TEXT",  # UUID stored as canonical text string
    ULID: "TEXT",  # ULID stored as 26-char text
    LongStr: "TEXT",  # large text — no separate type in SQLite
    Json: "TEXT",  # JSON stored as TEXT in SQLite
    DateTimeTz: "TEXT",  # tz-aware datetime stored as ISO 8601 TEXT in SQLite
    Vec: "TEXT",  # vector stored as JSON TEXT in SQLite
}


class DDLRenderer(ABC):
    """Abstract base for dialect-specific DDL renderers.

    Subclasses implement the primitive render methods; :meth:`render` dispatches
    a :data:`SchemaOp` union value to the right primitive automatically.
    """

    # Subclasses override to set the correct quote character for identifiers
    _identifier_quote = '"'  # Default: SQL standard double-quote

    def _quote_identifier(self, name: str) -> str:
        """Quote an identifier (table or column name) for safe SQL use.

        Quoting identifiers protects against reserved keywords and special characters.
        Subclasses can override _identifier_quote to use database-specific quoting.
        """
        return f"{self._identifier_quote}{name}{self._identifier_quote}"

    @abstractmethod
    def sql_type(self, column: Column) -> str:
        """Return the SQL type string for *column*."""

    @abstractmethod
    def create_table(self, table: Table) -> str:
        """Render a ``CREATE TABLE`` statement for *table*."""

    @abstractmethod
    def drop_table(self, table_name: str) -> str:
        """Render a ``DROP TABLE`` statement."""

    @abstractmethod
    def add_column(self, table_name: str, column: Column) -> str:
        """Render an ``ALTER TABLE … ADD COLUMN`` statement."""

    @abstractmethod
    def drop_column(self, table_name: str, column_name: str) -> str:
        """Render an ``ALTER TABLE … DROP COLUMN`` statement."""

    @abstractmethod
    def alter_column_type(self, table_name: str, column: Column) -> str:
        """Render an ``ALTER TABLE … ALTER COLUMN … TYPE …`` (or equivalent) statement."""

    @abstractmethod
    def create_index(self, table_name: str, index: Index) -> str:
        """Render a ``CREATE [UNIQUE] INDEX`` statement."""

    @abstractmethod
    def drop_index(self, index_name: str) -> str:
        """Render a ``DROP INDEX`` statement."""

    def render(self, op: SchemaOp) -> str:
        """Dispatch *op* to the appropriate primitive render method."""
        match op:
            case CreateTable():
                return self.create_table(op.table)
            case DropTable():
                return self.drop_table(op.table_name)
            case AddColumn():
                return self.add_column(op.table_name, op.column)
            case DropColumn():
                return self.drop_column(op.table_name, op.column_name)
            case AlterColumnType():
                return self.alter_column_type(op.table_name, op.column)
            case AddIndex():
                return self.create_index(op.table_name, op.index)
            case DropIndex():
                return self.drop_index(op.index_name)
            case _ as unreachable:  # pragma: no cover
                assert_never(unreachable)


class SQLiteRenderer(DDLRenderer):
    """DDL renderer for SQLite (≥ 3.35 for ``DROP COLUMN`` support)."""

    def sql_type(self, column: Column) -> str:
        py_type = column.py_type
        # Map uuid4, uuid7, ulid to canonical types for DDL
        import nextorm.fields as _fields

        if py_type in (_fields.uuid4, _fields.uuid7):
            py_type = uuid.UUID
        elif py_type is _fields.ulid:
            py_type = _fields.ULID
        if column.sql_type_override is not None:
            return column.sql_type_override
        if py_type is int and column.size is not None:
            return "INTEGER"
        if column.max_len is not None:
            return f"VARCHAR({column.max_len})"
        if py_type is decimal.Decimal and (column.precision is not None or column.scale is not None):
            p = column.precision if column.precision is not None else 10
            s = column.scale if column.scale is not None else 0
            return f"NUMERIC({p}, {s})"
        if py_type is Vec:
            return "TEXT"
        if issubclass(py_type, enum.Enum):
            return "TEXT"
        return _PY_TO_SQLITE.get(py_type, "TEXT")

    def _column_def(self, column: Column, *, table_pk: bool = False) -> str:
        """Render one column definition.

        *table_pk* is ``True`` when a table-level PRIMARY KEY constraint will
        be appended separately, so the inline ``PRIMARY KEY`` keyword must be
        omitted.
        """
        col_name = self._quote_identifier(column.name)
        parts = [col_name, self.sql_type(column)]
        if column.primary_key and not table_pk:
            parts.append("PRIMARY KEY")
            if column.auto_increment:
                parts.append("AUTOINCREMENT")
        elif not column.nullable and (not column.primary_key or table_pk):
            parts.append("NOT NULL")
        if column.unique and not column.primary_key:
            parts.append("UNIQUE")
        if column.sql_default is not None:
            parts.append(f"DEFAULT {column.sql_default}")
        return " ".join(parts)

    def create_table(self, table: Table) -> str:
        pk_cols = [c for c in table.columns if c.primary_key]
        use_table_pk = len(pk_cols) >= 2
        defs: list[str] = [self._column_def(c, table_pk=use_table_pk) for c in table.columns]
        for fk in table.foreign_keys:
            fk_col = self._quote_identifier(fk.column)
            ref_col = self._quote_identifier(fk.ref_column)
            ref_table = self._quote_identifier(fk.ref_table)
            defs.append(
                f"CONSTRAINT {fk.name} FOREIGN KEY ({fk_col})"
                f" REFERENCES {ref_table} ({ref_col})"
                f" ON DELETE {fk.on_delete}"
            )
        if use_table_pk:
            pk_col_names = ", ".join(self._quote_identifier(c.name) for c in pk_cols)
            defs.append(f"PRIMARY KEY ({pk_col_names})")
        body = ",\n    ".join(defs)
        table_name = self._quote_identifier(table.name)
        return f"CREATE TABLE IF NOT EXISTS {table_name} (\n    {body}\n)"

    def drop_table(self, table_name: str) -> str:
        tbl_name = self._quote_identifier(table_name)
        return f"DROP TABLE IF EXISTS {tbl_name}"

    def add_column(self, table_name: str, column: Column) -> str:
        tbl_name = self._quote_identifier(table_name)
        return f"ALTER TABLE {tbl_name} ADD COLUMN {self._column_def(column)}"

    def drop_column(self, table_name: str, column_name: str) -> str:
        tbl_name = self._quote_identifier(table_name)
        col_name = self._quote_identifier(column_name)
        return f"ALTER TABLE {tbl_name} DROP COLUMN {col_name}"

    def alter_column_type(self, table_name: str, column: Column) -> str:
        # SQLite does not support ALTER COLUMN TYPE natively.
        # The canonical workaround is: create new table, copy data, drop old, rename.
        # This method returns an informational comment so the developer knows a
        # manual migration is required.
        new_type = self.sql_type(column)
        return (
            f"-- SQLite: cannot ALTER COLUMN TYPE for {table_name}.{column.name}"
            f" to {new_type}; recreate the table manually"
        )

    def create_index(self, table_name: str, index: Index) -> str:
        unique = "UNIQUE " if index.unique else ""
        tbl_name = self._quote_identifier(table_name)
        cols = ", ".join(self._quote_identifier(col) for col in index.columns)
        return f"CREATE {unique}INDEX IF NOT EXISTS {index.name} ON {tbl_name} ({cols})"

    def drop_index(self, index_name: str) -> str:
        return f"DROP INDEX IF EXISTS {index_name}"


_INT_SIZE_POSTGRES: dict[int, str] = {
    8: "SMALLINT",
    16: "SMALLINT",
    32: "INTEGER",
    64: "BIGINT",
}

_INT_SIZE_MARIADB: dict[int, str] = {
    8: "TINYINT",
    16: "SMALLINT",
    32: "INT",
    64: "BIGINT",
}


# PostgreSQL type mapping
_PY_TO_POSTGRES: dict[type[Any], str] = {
    int: "INTEGER",
    str: "TEXT",
    float: "DOUBLE PRECISION",
    bool: "BOOLEAN",
    bytes: "BYTEA",
    datetime.datetime: "TIMESTAMP",
    datetime.date: "DATE",
    datetime.time: "TIME",
    datetime.timedelta: "INTERVAL",  # native PostgreSQL interval type
    decimal.Decimal: "NUMERIC",
    uuid.UUID: "UUID",  # native PostgreSQL UUID type
    ULID: "CHAR(26)",  # ULID as fixed-length Crockford base32
    LongStr: "TEXT",  # LONGTEXT equivalent in PostgreSQL is just TEXT
    Json: "JSONB",  # native binary JSON in PostgreSQL
    DateTimeTz: "TIMESTAMPTZ",  # timezone-aware timestamp in PostgreSQL
    Vec: "TEXT",  # fallback; overridden by sql_type() when dimensions known
}


class PostgresRenderer(DDLRenderer):
    """DDL renderer for PostgreSQL (≥ 12)."""

    # PostgreSQL uses double-quote for identifiers (already set in base class)

    def sql_type(self, column: Column) -> str:
        py_type = column.py_type
        import nextorm.fields as _fields

        if py_type in (_fields.uuid4, _fields.uuid7):
            py_type = uuid.UUID
        elif py_type is _fields.ulid:
            py_type = _fields.ULID
        if column.sql_type_override is not None:
            return column.sql_type_override
        if column.primary_key and column.auto_increment:
            return "SERIAL" if py_type is int else "BIGSERIAL"
        if py_type is int and column.size is not None:
            return _INT_SIZE_POSTGRES.get(column.size, "INTEGER")
        if column.max_len is not None:
            return f"VARCHAR({column.max_len})"
        if py_type is decimal.Decimal and (column.precision is not None or column.scale is not None):
            p = column.precision if column.precision is not None else 10
            s = column.scale if column.scale is not None else 0
            return f"NUMERIC({p}, {s})"
        if py_type is Vec:
            dims = column.dimensions
            if dims is not None:
                return f"vector({dims})"
            return "TEXT"
        if issubclass(py_type, enum.Enum):
            vals = ", ".join(f"'{m.value}'" for m in py_type)
            return f"TEXT CHECK ({column.name} IN ({vals}))"
        return _PY_TO_POSTGRES.get(py_type, "TEXT")

    def _column_def(self, column: Column, *, table_pk: bool = False) -> str:
        col_name = self._quote_identifier(column.name)
        parts = [col_name, self.sql_type(column)]
        if column.primary_key and not table_pk:
            parts.append("PRIMARY KEY")
        elif not column.nullable:
            parts.append("NOT NULL")
        if column.unique and not column.primary_key:
            parts.append("UNIQUE")
        if column.sql_default is not None:
            parts.append(f"DEFAULT {column.sql_default}")
        return " ".join(parts)

    def create_table(self, table: Table) -> str:
        pk_cols = [c for c in table.columns if c.primary_key]
        use_table_pk = len(pk_cols) >= 2
        defs: list[str] = [self._column_def(c, table_pk=use_table_pk) for c in table.columns]
        for fk in table.foreign_keys:
            fk_col = self._quote_identifier(fk.column)
            ref_col = self._quote_identifier(fk.ref_column)
            ref_table = self._quote_identifier(fk.ref_table)
            defs.append(
                f"CONSTRAINT {fk.name} FOREIGN KEY ({fk_col})"
                f" REFERENCES {ref_table} ({ref_col})"
                f" ON DELETE {fk.on_delete}"
            )
        if use_table_pk:
            pk_col_names = ", ".join(self._quote_identifier(c.name) for c in pk_cols)
            defs.append(f"PRIMARY KEY ({pk_col_names})")
        body = ",\n    ".join(defs)
        table_name = self._quote_identifier(table.name)
        return f"CREATE TABLE IF NOT EXISTS {table_name} (\n    {body}\n)"

    def drop_table(self, table_name: str) -> str:
        tbl_name = self._quote_identifier(table_name)
        return f"DROP TABLE IF EXISTS {tbl_name}"

    def add_column(self, table_name: str, column: Column) -> str:
        tbl_name = self._quote_identifier(table_name)
        return f"ALTER TABLE {tbl_name} ADD COLUMN IF NOT EXISTS {self._column_def(column)}"

    def drop_column(self, table_name: str, column_name: str) -> str:
        tbl_name = self._quote_identifier(table_name)
        col_name = self._quote_identifier(column_name)
        return f"ALTER TABLE {tbl_name} DROP COLUMN IF EXISTS {col_name}"

    def alter_column_type(self, table_name: str, column: Column) -> str:
        new_type = self.sql_type(column)
        nullable_clause = "" if column.nullable else " NOT NULL"
        tbl_name = self._quote_identifier(table_name)
        col_name = self._quote_identifier(column.name)
        return f"ALTER TABLE {tbl_name} ALTER COLUMN {col_name} TYPE {new_type}{nullable_clause}"

    def create_index(self, table_name: str, index: Index) -> str:
        unique = "UNIQUE " if index.unique else ""
        tbl_name = self._quote_identifier(table_name)
        cols = ", ".join(self._quote_identifier(col) for col in index.columns)
        if index.method is not None and index.method.lower() != "btree":
            # ANN / specialised index: USING method (col opclass) WITH (k=v, ...)
            col_with_op = f"{cols} {index.opclass}" if index.opclass else cols
            sql = (
                f"CREATE INDEX IF NOT EXISTS {index.name}"
                f" ON {tbl_name} USING {index.method} ({col_with_op})"
            )
            if index.with_options:
                opts = ", ".join(f"{k}={v}" for k, v in index.with_options.items())
                sql += f" WITH ({opts})"
            return sql
        return f"CREATE {unique}INDEX IF NOT EXISTS {index.name} ON {tbl_name} ({cols})"

    def drop_index(self, index_name: str) -> str:
        # PostgreSQL DROP INDEX does not use ON <table>
        return f"DROP INDEX IF EXISTS {index_name}"


# MariaDB / MySQL type mapping
_PY_TO_MARIADB: dict[type[Any], str] = {
    int: "INT",
    str: "TEXT",
    float: "DOUBLE",
    bool: "TINYINT(1)",
    bytes: "BLOB",
    datetime.datetime: "DATETIME",
    datetime.date: "DATE",
    datetime.time: "TIME",
    datetime.timedelta: "BIGINT",  # stored as microseconds
    decimal.Decimal: "DECIMAL",
    uuid.UUID: "CHAR(36)",  # UUID as hyphenated hex string
    ULID: "CHAR(26)",  # ULID as fixed-length Crockford base32
    LongStr: "LONGTEXT",  # up to 4 GB text in MariaDB
    Json: "JSON",  # native JSON type in MariaDB 10.2+
    DateTimeTz: "DATETIME",  # MariaDB has no native TIMESTAMPTZ; use UTC session
    Vec: "TEXT",  # vector stored as JSON TEXT in MariaDB
}


class MariaDBRenderer(DDLRenderer):
    """DDL renderer for MariaDB (also compatible with MySQL)."""

    # MariaDB/MySQL uses backticks for identifiers
    _identifier_quote = "`"

    def sql_type(self, column: Column) -> str:
        py_type = column.py_type
        import nextorm.fields as _fields

        if py_type in (_fields.uuid4, _fields.uuid7):
            py_type = uuid.UUID
        elif py_type is _fields.ulid:
            py_type = _fields.ULID
        if column.sql_type_override is not None:
            return column.sql_type_override
        if py_type is int and column.size is not None:
            base = _INT_SIZE_MARIADB.get(column.size, "INT")
            return f"{base} UNSIGNED" if column.unsigned else base
        if column.max_len is not None:
            return f"VARCHAR({column.max_len})"
        if py_type is decimal.Decimal and (column.precision is not None or column.scale is not None):
            p = column.precision if column.precision is not None else 10
            s = column.scale if column.scale is not None else 0
            return f"DECIMAL({p}, {s})"
        if py_type is Vec:
            dims = column.dimensions
            if dims is not None:
                return f"VECTOR({dims})"
            return "TEXT"
        if issubclass(py_type, enum.Enum):
            vals = ", ".join(f"'{m.value}'" for m in py_type)
            return f"ENUM({vals})"
        base = _PY_TO_MARIADB.get(py_type, "TEXT")
        if column.unsigned and py_type is int:
            return f"{base} UNSIGNED"
        return base

    def _column_def(self, column: Column, *, table_pk: bool = False) -> str:
        col_name = self._quote_identifier(column.name)
        parts = [col_name, self.sql_type(column)]
        if column.primary_key and not table_pk:
            if column.auto_increment:
                parts.append("AUTO_INCREMENT")
            parts.append("PRIMARY KEY")
        elif not column.nullable:
            parts.append("NOT NULL")
        if column.unique and not column.primary_key:
            parts.append("UNIQUE")
        if column.sql_default is not None:
            parts.append(f"DEFAULT {column.sql_default}")
        return " ".join(parts)

    def create_table(self, table: Table) -> str:
        pk_cols = [c for c in table.columns if c.primary_key]
        use_table_pk = len(pk_cols) >= 2
        defs: list[str] = [self._column_def(c, table_pk=use_table_pk) for c in table.columns]
        for fk in table.foreign_keys:
            fk_col = self._quote_identifier(fk.column)
            ref_col = self._quote_identifier(fk.ref_column)
            ref_table = self._quote_identifier(fk.ref_table)
            defs.append(
                f"CONSTRAINT {fk.name} FOREIGN KEY ({fk_col})"
                f" REFERENCES {ref_table} ({ref_col})"
                f" ON DELETE {fk.on_delete}"
            )
        if use_table_pk:
            pk_col_names = ", ".join(self._quote_identifier(c.name) for c in pk_cols)
            defs.append(f"PRIMARY KEY ({pk_col_names})")
        body = ",\n    ".join(defs)
        table_name = self._quote_identifier(table.name)
        return f"CREATE TABLE IF NOT EXISTS {table_name} (\n    {body}\n)"

    def drop_table(self, table_name: str) -> str:
        tbl_name = self._quote_identifier(table_name)
        return f"DROP TABLE IF EXISTS {tbl_name}"

    def add_column(self, table_name: str, column: Column) -> str:
        tbl_name = self._quote_identifier(table_name)
        return f"ALTER TABLE {tbl_name} ADD COLUMN {self._column_def(column)}"

    def drop_column(self, table_name: str, column_name: str) -> str:
        tbl_name = self._quote_identifier(table_name)
        col_name = self._quote_identifier(column_name)
        return f"ALTER TABLE {tbl_name} DROP COLUMN {col_name}"

    def alter_column_type(self, table_name: str, column: Column) -> str:
        new_type = self.sql_type(column)
        nullable_clause = "" if column.nullable else " NOT NULL"
        tbl_name = self._quote_identifier(table_name)
        col_name = self._quote_identifier(column.name)
        return f"ALTER TABLE {tbl_name} MODIFY COLUMN {col_name} {new_type}{nullable_clause}"

    def create_index(self, table_name: str, index: Index) -> str:
        unique = "UNIQUE " if index.unique else ""
        tbl_name = self._quote_identifier(table_name)
        cols = ", ".join(self._quote_identifier(col) for col in index.columns)
        return f"CREATE {unique}INDEX {index.name} ON {tbl_name} ({cols})"

    def drop_index(self, index_name: str) -> str:
        # MariaDB DROP INDEX requires the table name — callers must use render(DropIndex(...))
        # At the SchemaOp level we have the table_name; override render() to pass it.
        return f"DROP INDEX {index_name}"  # pragma: no cover

    def render(self, op: SchemaOp) -> str:
        """Override to pass table_name to drop_index for MariaDB syntax."""
        if isinstance(op, DropIndex):
            return f"DROP INDEX {op.index_name} ON {op.table_name}"
        return super().render(op)
