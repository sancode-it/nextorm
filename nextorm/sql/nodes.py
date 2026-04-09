"""Typed SQL AST node classes.

Every node is an immutable dataclass.  Nodes are dialect-neutral — they
describe *what* to query, not how to render it.  Rendering is delegated to
:mod:`nextorm.sql.builder`.

``SqlNode`` is a ``type`` union alias (PEP 695) of all concrete node classes.
Using a union (not a base class) makes ``match/case`` dispatches provably
exhaustive to pyright and mypy — the same pattern used by
:data:`~nextorm.schema.diff.SchemaOp`.

Node hierarchy
--------------

``SqlNode``  = (union of all concrete nodes below)
├── Leaf nodes
│   ├── ``Param``        — bound parameter placeholder ( ``?`` / ``%s`` )
│   ├── ``Literal``      — inline SQL literal  ( 42, 'hello', NULL, TRUE … )
│   ├── ``ColumnRef``    — ``[table.]column`` reference
│   └── ``Star``         — bare ``*`` or ``table.*``
├── Expression nodes
│   ├── ``BinOp``        — binary operator  ( ``a + b``, ``x = ?``, … )
│   ├── ``UnaryOp``      — unary operator   ( ``NOT x``, ``-n``, … )
│   ├── ``Cast``         — ``CAST(expr AS type)``
│   ├── ``FunctionCall`` — ``func([DISTINCT] args…)``
│   ├── ``CaseWhen``     — single ``WHEN … THEN …`` branch
│   └── ``Case``         — full ``CASE … END`` expression
├── Clause helpers
│   ├── ``Alias``        — ``expr AS name``
│   └── ``OrderItem``    — ``expr [ASC|DESC] [NULLS FIRST|LAST]``
└── Statement nodes
    ├── ``Select``
    ├── ``Insert``
    ├── ``Update``
    └── ``Delete``
"""

from __future__ import annotations

import dataclasses
from typing import Any

__all__ = [
    "SqlNode",
    "Param",
    "Literal",
    "ColumnRef",
    "Star",
    "BinOp",
    "UnaryOp",
    "Cast",
    "FunctionCall",
    "CaseWhen",
    "Case",
    "Alias",
    "OrderItem",
    "Select",
    "Insert",
    "Update",
    "Delete",
    "RawSql",
    "sql",
]

# ---------------------------------------------------------------------------
# Leaf nodes
# ---------------------------------------------------------------------------


# Sentinel that means "no bound value was supplied" — distinct from None.
_PARAM_MISSING: object = object()


@dataclasses.dataclass(frozen=True)
class Param:
    """A bound parameter — rendered as ``?`` (SQLite) or ``%s`` (Postgres).

    *name* is optional; named parameters (e.g. ``:name`` or ``%(name)s``)
    are used when it is set.

    *value* is an optional bound value.  When supplied the builder collects it
    into the ``params`` list returned by :func:`~nextorm.sql.builder.render`,
    so callers do not need to track parameter order manually::

        stmt = BinOp(ColumnRef("id"), "=", Param(value=5))
        sql, params = render(stmt)  # params == [5]
    """

    name: str | None = None
    value: Any = dataclasses.field(default=_PARAM_MISSING, compare=False, hash=False, repr=False)

    @property
    def has_value(self) -> bool:
        """``True`` when a bound value was provided at construction time."""
        return self.value is not _PARAM_MISSING


@dataclasses.dataclass(frozen=True)
class Literal:
    """An inline literal value — rendered directly into the SQL string.

    Use ``Param`` instead of ``Literal`` for user-supplied values to avoid
    SQL injection.  ``Literal`` is appropriate for compile-time constants
    such as ``NULL``, ``TRUE``, ``1``, default expressions, etc.
    """

    value: Any  # int | float | str | bool | None


@dataclasses.dataclass(frozen=True)
class ColumnRef:
    """A column reference, optionally qualified by a table/alias name.

    Examples::

        ColumnRef("name")           →  name
        ColumnRef("name", "u")      →  u.name
    """

    column: str
    table: str | None = None


@dataclasses.dataclass(frozen=True)
class Star:
    """The wildcard ``*`` or ``table.*``.

    Examples::

        Star()          →  *
        Star("u")       →  u.*
    """

    table: str | None = None


# ---------------------------------------------------------------------------
# Expression nodes
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class BinOp:
    """A binary infix operator: ``left op right``.

    Common operators: ``=``, ``!=``, ``<``, ``>``, ``<=``, ``>=``,
    ``AND``, ``OR``, ``+``, ``-``, ``*``, ``/``, ``%``,
    ``LIKE``, ``ILIKE``, ``IN``, ``NOT IN``, ``IS``, ``IS NOT``.
    """

    left: SqlNode
    op: str
    right: SqlNode


@dataclasses.dataclass(frozen=True)
class UnaryOp:
    """A unary prefix operator: ``op operand``.

    Common operators: ``NOT``, ``-``, ``+``, ``EXISTS``.
    """

    op: str
    operand: SqlNode


@dataclasses.dataclass(frozen=True)
class Cast:
    """``CAST(expr AS sql_type)``."""

    expr: SqlNode
    sql_type: str


@dataclasses.dataclass(frozen=True)
class FunctionCall:
    """A SQL function call: ``name([DISTINCT] args…)``.

    Examples::

        FunctionCall("COUNT", (Star(),))
        FunctionCall("MAX", (ColumnRef("price"),))
        FunctionCall("COALESCE", (ColumnRef("x"), Literal(0)))
        FunctionCall("COUNT", (Star(),), distinct=True)
    """

    name: str
    args: tuple[SqlNode, ...]
    distinct: bool = False


@dataclasses.dataclass(frozen=True)
class CaseWhen:
    """A single ``WHEN condition THEN result`` branch inside a ``CASE``."""

    condition: SqlNode
    result: SqlNode


@dataclasses.dataclass(frozen=True)
class Case:
    """A ``CASE … END`` expression.

    Simple (``CASE expr WHEN val THEN …``) and searched
    (``CASE WHEN cond THEN …``) forms are both supported::

        # Searched form
        Case(
            whens=[CaseWhen(BinOp(ColumnRef("x"), ">", Literal(0)), Literal("pos"))],
            else_=Literal("non-pos"),
        )

        # Simple form — set `subject` to the expression to compare against
        Case(
            subject=ColumnRef("status"),
            whens=[CaseWhen(Literal("A"), Literal("active"))],
            else_=Literal("inactive"),
        )
    """

    whens: tuple[CaseWhen, ...]
    else_: SqlNode | None = None
    subject: SqlNode | None = None  # present iff simple form


# ---------------------------------------------------------------------------
# Clause helpers
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Alias:
    """``expr AS alias``."""

    expr: SqlNode
    alias: str


@dataclasses.dataclass(frozen=True)
class OrderItem:
    """A single entry in an ``ORDER BY`` clause.

    Parameters
    ----------
    expr:
        The expression to sort by.
    descending:
        ``True`` → ``DESC``, ``False`` (default) → ``ASC``.
    nulls_first:
        ``True`` → ``NULLS FIRST``, ``False`` → ``NULLS LAST``,
        ``None`` (default) → database default (no explicit clause).
    """

    expr: SqlNode
    descending: bool = False
    nulls_first: bool | None = None


# ---------------------------------------------------------------------------
# Statement nodes
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Select:
    """A full ``SELECT`` statement.

    Parameters
    ----------
    columns:
        Projection — a sequence of :class:`SqlNode` (e.g. ``ColumnRef``,
        ``Alias``, ``FunctionCall``, ``Star``).
    from_table:
        Primary table name (required).
    from_alias:
        Optional alias for the primary table.
    joins:
        List of ``(join_type, table, alias, condition)`` tuples, where
        *join_type* is ``"INNER"``, ``"LEFT"``, ``"RIGHT"``, or ``"FULL"``.
    where:
        Optional ``WHERE`` condition node.
    group_by:
        Sequence of grouping expressions.
    having:
        Optional ``HAVING`` condition (only meaningful with *group_by*).
    order_by:
        Sequence of :class:`OrderItem` nodes.
    limit:
        Optional row limit.
    offset:
        Optional row offset (requires *limit* in SQLite).
    distinct:
        If ``True``, render ``SELECT DISTINCT``.
    """

    columns: tuple[SqlNode, ...]
    from_table: str
    from_alias: str | None = None
    joins: tuple[tuple[str, str, str | None, SqlNode], ...] = ()
    where: SqlNode | None = None
    group_by: tuple[SqlNode, ...] = ()
    having: SqlNode | None = None
    order_by: tuple[OrderItem, ...] = ()
    limit: int | None = None
    offset: int | None = None
    distinct: bool = False
    for_update: bool = False
    for_update_skip_locked: bool = False
    for_update_nowait: bool = False


@dataclasses.dataclass(frozen=True)
class Insert:
    """An ``INSERT INTO table (cols…) VALUES (?,?…)`` statement.

    Parameters
    ----------
    table:
        Target table name.
    columns:
        Column names to insert into.
    values:
        One row of values (a parameter node per column) *or* a
        :class:`Select` node for ``INSERT … SELECT``.
    returning:
        Optional column name for a ``RETURNING`` clause (Postgres / SQLite ≥ 3.35).
    """

    table: str
    columns: tuple[str, ...]
    values: tuple[SqlNode, ...] | Select
    returning: str | None = None


@dataclasses.dataclass(frozen=True)
class Update:
    """An ``UPDATE table SET col=val … WHERE …`` statement.

    Parameters
    ----------
    table:
        Target table name.
    assignments:
        Sequence of ``(column_name, value_node)`` pairs.
    where:
        Optional ``WHERE`` condition.
    """

    table: str
    assignments: tuple[tuple[str, SqlNode], ...]
    where: SqlNode | None = None


@dataclasses.dataclass(frozen=True)
class Delete:
    """A ``DELETE FROM table WHERE …`` statement.

    Parameters
    ----------
    table:
        Target table name.
    where:
        Optional ``WHERE`` condition.
    """

    table: str
    where: SqlNode | None = None


@dataclasses.dataclass(frozen=True)
class RawSql:
    """A raw SQL fragment with named or positional parameters.

    Use :func:`sql` to construct instances conveniently::

        from nextorm.sql import sql

        qs.filter(sql("status = :s", s="active"))
        qs.filter(sql("age > :min AND age < :max", min=18, max=65))

    Named placeholders (```:name```) are converted to positional
    placeholders (``?`` / ``%s``) when the query is rendered.  The
    substitution order follows the order in which the names appear in
    *fragment*.
    """

    fragment: str
    params: dict[str, Any] = dataclasses.field(default_factory=dict[str, Any])


def sql(fragment: str, **params: Any) -> RawSql:
    """Return a :class:`RawSql` node for the given *fragment* and *params*.

    Named placeholders in *fragment* (```:name``` style) are bound to the
    corresponding keyword arguments.  The result can be passed directly
    to :meth:`~nextorm.query.QuerySet.filter` or any other method that
    accepts a :data:`~nextorm.sql.nodes.SqlNode`::

        from nextorm import sql

        qs = db.select(Product).filter(sql("status = :s AND price < :p", s="active", p=100))
    """
    return RawSql(fragment=fragment, params=params)


# ---------------------------------------------------------------------------
# Union type alias — enables exhaustive match/case dispatch
# ---------------------------------------------------------------------------

type SqlNode = (  # noqa: UP040
    Param
    | Literal
    | ColumnRef
    | Star
    | BinOp
    | UnaryOp
    | Cast
    | FunctionCall
    | CaseWhen
    | Case
    | Alias
    | OrderItem
    | Select
    | Insert
    | Update
    | Delete
    | RawSql
)
