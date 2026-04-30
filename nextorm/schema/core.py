"""Pure data structures that describe a database schema.

These classes are dialect-neutral: a :class:`Column` stores the Python type
rather than a SQL type string.  DDL renderers in :mod:`nextorm.schema.ddl`
translate them to concrete SQL.
"""

import dataclasses
from typing import Any
from uuid import UUID

from nextorm.fields import AttrValue

__all__ = ["Column", "ForeignKey", "Index", "Table"]


@dataclasses.dataclass
class Column:
    """A single table column."""

    name: str
    py_type: type[AttrValue | UUID]
    nullable: bool = False
    primary_key: bool = False
    auto_increment: bool = False
    unique: bool = False
    index: bool = False
    max_len: int | None = None
    sql_default: str | None = None  # raw SQL expression, e.g. "CURRENT_TIMESTAMP"
    sql_type_override: str | None = None  # override inferred SQL type string
    precision: int | None = None  # for NUMERIC(p, s): total significant digits
    scale: int | None = None  # for NUMERIC(p, s): digits after decimal point
    unsigned: bool = False  # UNSIGNED modifier (MariaDB); ignored elsewhere
    size: int | None = None  # for int: column bit width — 8, 16, 32, or 64
    dimensions: int | None = None  # for Vec: number of vector dimensions


@dataclasses.dataclass
class ForeignKey:
    """A foreign-key constraint on a table."""

    name: str  # constraint name, e.g. "fk_comment__post_id"
    column: str  # local column
    ref_table: str  # referenced table
    ref_column: str = "id"
    on_delete: str = "CASCADE"  # "CASCADE" | "SET NULL" | "RESTRICT" | "NO ACTION"


@dataclasses.dataclass
class Index:
    """A single- or multi-column table index."""

    name: str
    columns: list[str]
    unique: bool = False
    method: str | None = None  # e.g. "hnsw", "ivfflat", "gist" — None = default (btree)
    opclass: str | None = None  # e.g. "vector_cosine_ops" for pgvector
    with_options: dict[str, int | str] | None = None  # e.g. {"m": 16, "ef_construction": 64}


@dataclasses.dataclass
class Table:
    """Complete schema representation of a single database table."""

    name: str
    columns: list[Column] = dataclasses.field(default_factory=lambda: [])
    foreign_keys: list[ForeignKey] = dataclasses.field(default_factory=lambda: [])
    indexes: list[Index] = dataclasses.field(default_factory=lambda: [])
    entity_cls: type[Any] | None = None  # None for auto-generated join tables

    def get_column(self, name: str) -> Column | None:
        """Return the column with the given name, or *None* if absent."""
        return next((c for c in self.columns if c.name == name), None)

    def column_names(self) -> list[str]:
        """Return an ordered list of column names."""
        return [c.name for c in self.columns]
