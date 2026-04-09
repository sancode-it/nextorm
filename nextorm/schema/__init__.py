"""Schema package — data structures, builder, diff, and DDL renderers."""

from nextorm.schema.builder import build_schema, entity_to_table
from nextorm.schema.core import Column, ForeignKey, Index, Table
from nextorm.schema.ddl import DDLRenderer, SQLiteRenderer
from nextorm.schema.diff import (
    AddColumn,
    AddIndex,
    AlterColumnType,
    CreateTable,
    DropColumn,
    DropIndex,
    DropTable,
    SchemaOp,
    diff_schemas,
)

__all__ = [
    # core
    "Column",
    "ForeignKey",
    "Index",
    "Table",
    # builder
    "entity_to_table",
    "build_schema",
    # diff
    "diff_schemas",
    "SchemaOp",
    "CreateTable",
    "DropTable",
    "AddColumn",
    "DropColumn",
    "AlterColumnType",
    "AddIndex",
    "DropIndex",
    # ddl
    "DDLRenderer",
    "SQLiteRenderer",
]
