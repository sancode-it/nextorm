"""SQL Infrastructure — typed AST nodes and dialect-aware SQL builders.

Public API::

    from nextorm.sql import (
        # Leaf nodes
        Param,
        Literal,
        ColumnRef,
        Star,
        # Expressions
        BinOp,
        UnaryOp,
        Cast,
        FunctionCall,
        Case,
        CaseWhen,
        # Clauses
        Alias,
        OrderItem,
        # Statements
        Select,
        Insert,
        Update,
        Delete,
        # Renderers
        SQLBuilder,
        SQLiteBuilder,
        FormatBuilder,
        # Helpers
        render,
    )
"""

from nextorm.sql.builder import FormatBuilder, SQLBuilder, SQLiteBuilder, render
from nextorm.sql.nodes import (
    Alias,
    BinOp,
    Case,
    CaseWhen,
    Cast,
    ColumnRef,
    Delete,
    FunctionCall,
    Insert,
    Literal,
    OrderItem,
    Param,
    RawSql,
    Select,
    Star,
    UnaryOp,
    Update,
    sql,
)

__all__ = [
    # Leaf nodes
    "Param",
    "Literal",
    "ColumnRef",
    "Star",
    # Expressions
    "BinOp",
    "UnaryOp",
    "Cast",
    "FunctionCall",
    "Case",
    "CaseWhen",
    # Clauses / helpers
    "Alias",
    "OrderItem",
    # Statements
    "Select",
    "Insert",
    "Update",
    "Delete",
    "RawSql",
    "sql",
    # Renderers
    "SQLBuilder",
    "SQLiteBuilder",
    "FormatBuilder",
    "render",
]
