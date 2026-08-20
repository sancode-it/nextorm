"""SQL builders — render :mod:`nextorm.sql.nodes` AST nodes to SQL strings.

Each builder produces a ``(sql_string, params)`` pair where *params* is a
flat ``list`` of bound parameter values in the order they appear in the
statement.

Usage::

    from nextorm.sql import Select, ColumnRef, Param, BinOp, SQLiteBuilder, render

    stmt = Select(
        columns=(ColumnRef("id"), ColumnRef("name")),
        from_table="user",
        where=BinOp(ColumnRef("id"), "=", Param()),
    )
    sql, params = SQLiteBuilder().render(stmt)
    # sql   → 'SELECT id, name FROM user WHERE id = ?'
    # params → []   (Param() has no value — caller supplies it)

    # Convenience wrapper
    sql, params = render(stmt)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, assert_never

from nextorm.sql.nodes import (
    Alias,
    BinOp,
    Case,
    CaseWhen,
    Cast,
    ColumnRef,
    Delete,
    ExistsNode,
    FuncCall,
    FunctionCall,
    Insert,
    Literal,
    OrderItem,
    Param,
    RawSql,
    Select,
    SqlNode,
    Star,
    UnaryOp,
    Update,
)

__all__ = ["SQLBuilder", "SQLiteBuilder", "render"]


class SQLBuilder(ABC):
    """Abstract base — subclass to implement a dialect-specific renderer.

    :meth:`render` is the main entry point; it dispatches to the typed
    ``_render_*`` helpers via ``match/case``.  Subclasses only need to
    override helpers whose dialect behaviour differs.
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def render(self, node: SqlNode) -> tuple[str, list[Any]]:
        """Render *node* to ``(sql, params)``.

        *params* is a flat list of bound values in left-to-right order.
        """
        parts: list[str] = []
        params: list[Any] = []
        self._emit(node, parts, params)
        return "".join(parts), params

    # ------------------------------------------------------------------
    # Internal dispatch
    # ------------------------------------------------------------------

    def _emit(self, node: SqlNode, parts: list[str], params: list[Any]) -> None:
        match node:
            case Param():
                self._emit_param(node, parts, params)
            case Literal():
                self._emit_literal(node, parts, params)
            case FuncCall():
                self._emit_func_call(node, parts, params)
            case ExistsNode():
                self._emit_exists(node, parts, params)
            case ColumnRef():
                self._emit_column_ref(node, parts, params)
            case Star():
                self._emit_star(node, parts, params)
            case BinOp():
                self._emit_bin_op(node, parts, params)
            case UnaryOp():
                self._emit_unary_op(node, parts, params)
            case Cast():
                self._emit_cast(node, parts, params)
            case FunctionCall():
                self._emit_function_call(node, parts, params)
            case CaseWhen():
                self._emit_case_when(node, parts, params)
            case Case():
                self._emit_case(node, parts, params)
            case Alias():
                self._emit_alias(node, parts, params)
            case OrderItem():
                self._emit_order_item(node, parts, params)
            case Select():
                self._emit_select(node, parts, params)
            case Insert():
                self._emit_insert(node, parts, params)
            case Update():
                self._emit_update(node, parts, params)
            case Delete():
                self._emit_delete(node, parts, params)
            case RawSql():
                self._emit_raw_sql(node, parts, params)
            case _ as unreachable:  # pragma: no cover
                assert_never(unreachable)

    # ------------------------------------------------------------------
    # Leaf renderers — subclasses may override
    # ------------------------------------------------------------------

    @abstractmethod
    def _param_placeholder(self, name: str | None) -> str:
        """Return the placeholder string for a single parameter."""

    def _quote_identifier(self, name: str) -> str:
        """Return *name* double-quoted if it is a reserved SQL keyword, else as-is.

        Only keywords that are STRICT reserved words (cannot appear as
        unquoted identifiers) across SQLite, PostgreSQL, and MariaDB are
        included.  Subclasses may override to use dialect-specific quoting
        (e.g. backtick for MariaDB/MySQL).
        """
        # Strict reserved keywords that cannot be used as unquoted table/column names.
        # These are words that cause syntax errors when used bare as identifiers.
        _RESERVED = frozenset(
            {
                "order",
                "group",
                "select",
                "from",
                "where",
                "table",
                "index",
                "check",
                "by",
                "case",
                "default",
                "values",
                "into",
                "all",
                "and",
                "or",
                "not",
                "null",
                "union",
                "intersect",
                "except",
                "with",
                "transaction",
            }
        )
        return f'"{name}"' if name.lower() in _RESERVED else name

    def _emit_param(self, node: Param, parts: list[str], params: list[Any]) -> None:
        parts.append(self._param_placeholder(node.name))
        if node.has_value:
            from nextorm.fields import _serialize_value  # noqa: PLC0415

            params.append(_serialize_value(node.value))

    def _emit_literal(self, node: Literal, parts: list[str], params: list[Any]) -> None:
        val = node.value
        if val is None:
            parts.append("NULL")
        elif isinstance(val, bool):
            # Render before int check — bool is a subclass of int
            parts.append("1" if val else "0")
        elif isinstance(val, (int, float)):
            parts.append(str(val))
        else:
            # Escape single quotes by doubling them (standard SQL)
            escaped = str(val).replace("'", "''")
            parts.append(f"'{escaped}'")

    def _emit_func_call(self, node: FuncCall, parts: list[str], params: list[Any]) -> None:
        parts.append(f"{node.func}(")
        self._emit(node.arg, parts, params)
        parts.append(")")

    def _emit_exists(self, node: ExistsNode, parts: list[str], params: list[Any]) -> None:
        parts.append(f"EXISTS ({node.sql})")
        params.extend(node.params)

    def _emit_column_ref(self, node: ColumnRef, parts: list[str], params: list[Any]) -> None:
        if node.table:
            parts.append(f"{self._quote_identifier(node.table)}.{node.column}")
        else:
            parts.append(node.column)

    def _emit_star(self, node: Star, parts: list[str], params: list[Any]) -> None:
        if node.table:
            parts.append(f"{node.table}.*")
        else:
            parts.append("*")

    # ------------------------------------------------------------------
    # Expression renderers
    # ------------------------------------------------------------------

    def _emit_bin_op(self, node: BinOp, parts: list[str], params: list[Any]) -> None:
        # Special case: IN / NOT IN with a list Param → expand to (?, ?, ...)
        from nextorm.sql.nodes import Param as _Param  # noqa: PLC0415

        if (
            node.op.upper() in ("IN", "NOT IN")
            and isinstance(node.right, _Param)
            and isinstance(node.right.value, (list, tuple))
        ):
            self._emit(node.left, parts, params)
            parts.append(f" {node.op} (")
            items: list[Any] = list(node.right.value)  # pyright: ignore[reportUnknownArgumentType,reportUnknownMemberType]
            for i, item_val in enumerate(items):
                if i:
                    parts.append(", ")
                self._emit(_Param(value=item_val), parts, params)
            parts.append(")")
            return

        # Parenthesise sub-expressions for AND/OR to preserve precedence
        _needs_paren = isinstance(node.left, BinOp) and node.op.upper() in ("AND", "OR")
        if _needs_paren:
            parts.append("(")
        self._emit(node.left, parts, params)
        if _needs_paren:
            parts.append(")")
        parts.append(f" {node.op} ")
        _needs_paren_r = isinstance(node.right, BinOp) and node.op.upper() in (
            "AND",
            "OR",
        )
        if _needs_paren_r:
            parts.append("(")
        self._emit(node.right, parts, params)
        if _needs_paren_r:
            parts.append(")")

    def _emit_unary_op(self, node: UnaryOp, parts: list[str], params: list[Any]) -> None:
        op_upper = node.op.upper()
        # Word operators (NOT, EXISTS) need a space; symbol operators (-,+) don't
        sep = " " if op_upper.isalpha() else ""
        parts.append(f"{node.op}{sep}")
        # Parenthesise complex sub-expressions under NOT/EXISTS
        needs_paren = isinstance(node.operand, BinOp)
        if needs_paren:
            parts.append("(")
        self._emit(node.operand, parts, params)
        if needs_paren:
            parts.append(")")

    def _emit_cast(self, node: Cast, parts: list[str], params: list[Any]) -> None:
        parts.append("CAST(")
        self._emit(node.expr, parts, params)
        parts.append(f" AS {node.sql_type})")

    def _emit_function_call(self, node: FunctionCall, parts: list[str], params: list[Any]) -> None:
        parts.append(f"{node.name}(")
        if node.distinct:
            parts.append("DISTINCT ")
        for i, arg in enumerate(node.args):
            if i:
                parts.append(", ")
            self._emit(arg, parts, params)
        parts.append(")")

    def _emit_case_when(self, node: CaseWhen, parts: list[str], params: list[Any]) -> None:
        parts.append("WHEN ")
        self._emit(node.condition, parts, params)
        parts.append(" THEN ")
        self._emit(node.result, parts, params)

    def _emit_case(self, node: Case, parts: list[str], params: list[Any]) -> None:
        parts.append("CASE")
        if node.subject is not None:
            parts.append(" ")
            self._emit(node.subject, parts, params)
        for when in node.whens:
            parts.append(" ")
            self._emit_case_when(when, parts, params)
        if node.else_ is not None:
            parts.append(" ELSE ")
            self._emit(node.else_, parts, params)
        parts.append(" END")

    # ------------------------------------------------------------------
    # Clause helpers
    # ------------------------------------------------------------------

    def _emit_alias(self, node: Alias, parts: list[str], params: list[Any]) -> None:
        self._emit(node.expr, parts, params)
        parts.append(f" AS {node.alias}")

    def _emit_order_item(self, node: OrderItem, parts: list[str], params: list[Any]) -> None:
        self._emit(node.expr, parts, params)
        parts.append(" DESC" if node.descending else " ASC")
        if node.nulls_first is True:
            parts.append(" NULLS FIRST")
        elif node.nulls_first is False:
            parts.append(" NULLS LAST")

    # ------------------------------------------------------------------
    # Statement renderers
    # ------------------------------------------------------------------

    def _emit_select(self, node: Select, parts: list[str], params: list[Any]) -> None:
        parts.append("SELECT ")
        if node.distinct:
            parts.append("DISTINCT ")
        for i, col in enumerate(node.columns):
            if i:
                parts.append(", ")
            self._emit(col, parts, params)
        parts.append(f" FROM {self._quote_identifier(node.from_table)}")
        if node.from_alias:
            parts.append(f" AS {node.from_alias}")
        for join_type, join_table, join_alias, join_cond in node.joins:
            parts.append(f" {join_type} JOIN {self._quote_identifier(join_table)}")
            if join_alias:
                parts.append(f" AS {join_alias}")
            parts.append(" ON ")
            self._emit(join_cond, parts, params)
        if node.where is not None:
            parts.append(" WHERE ")
            self._emit(node.where, parts, params)
        if node.group_by:
            parts.append(" GROUP BY ")
            for i, expr in enumerate(node.group_by):
                if i:
                    parts.append(", ")
                self._emit(expr, parts, params)
        if node.having is not None:
            parts.append(" HAVING ")
            self._emit(node.having, parts, params)
        if node.order_by:
            parts.append(" ORDER BY ")
            for i, item in enumerate(node.order_by):
                if i:
                    parts.append(", ")
                self._emit_order_item(item, parts, params)
        if node.limit is not None:
            parts.append(f" LIMIT {node.limit}")
        if node.offset is not None:
            if node.limit is None:
                # Emit a sentinel LIMIT so OFFSET is syntactically valid.
                # Subclasses may override _emit_offset_limit for dialect differences.
                self._emit_implicit_limit(parts)
            parts.append(f" OFFSET {node.offset}")
        if node.for_update:
            parts.append(" FOR UPDATE")
            if node.for_update_skip_locked:
                parts.append(" SKIP LOCKED")
            elif node.for_update_nowait:
                parts.append(" NOWAIT")

    def _emit_implicit_limit(self, parts: list[str]) -> None:
        """Emit a LIMIT clause when only OFFSET was specified.

        The default renders ``LIMIT -1`` (no upper bound in SQLite / SQLite-like
        dialects).  Override this in a subclass for dialects that use
        ``LIMIT ALL`` or similar.
        """
        parts.append(" LIMIT -1")

    def _emit_insert(self, node: Insert, parts: list[str], params: list[Any]) -> None:
        cols = ", ".join(node.columns)
        parts.append(f"INSERT INTO {self._quote_identifier(node.table)} ({cols}) ")
        if isinstance(node.values, Select):
            self._emit_select(node.values, parts, params)
        else:
            parts.append("VALUES (")
            for i, val in enumerate(node.values):
                if i:
                    parts.append(", ")
                self._emit(val, parts, params)
            parts.append(")")
        if node.returning is not None:
            self._emit_returning(node.returning, parts)

    def _emit_returning(self, column: str, parts: list[str]) -> None:
        """Render a ``RETURNING`` clause.  Override to suppress for dialects
        that don't support it (e.g. older MySQL)."""
        parts.append(f" RETURNING {column}")

    def _emit_update(self, node: Update, parts: list[str], params: list[Any]) -> None:
        parts.append(f"UPDATE {self._quote_identifier(node.table)} SET ")
        for i, (col, val) in enumerate(node.assignments):
            if i:
                parts.append(", ")
            parts.append(f"{col} = ")
            self._emit(val, parts, params)
        if node.where is not None:
            parts.append(" WHERE ")
            self._emit(node.where, parts, params)

    def _emit_delete(self, node: Delete, parts: list[str], params: list[Any]) -> None:
        parts.append(f"DELETE FROM {self._quote_identifier(node.table)}")
        if node.where is not None:
            parts.append(" WHERE ")
            self._emit(node.where, parts, params)

    def _emit_raw_sql(self, node: RawSql, parts: list[str], params: list[Any]) -> None:
        """Render a :class:`RawSql` node.

        Named placeholders (```:name```) are replaced with positional
        placeholders in left-to-right order of first appearance, and the
        corresponding values are appended to *params*.
        """
        import re  # noqa: PLC0415

        fragment = node.fragment

        def _replace(match: re.Match[str]) -> str:
            name = match.group(1)
            params.append(node.params[name])
            return self._param_placeholder(name)

        rendered = re.sub(r":([A-Za-z_]\w*)", _replace, fragment)
        parts.append(rendered)


# ---------------------------------------------------------------------------
# SQLite dialect
# ---------------------------------------------------------------------------


class SQLiteBuilder(SQLBuilder):
    """Renders SQL AST nodes for SQLite (positional ``?`` placeholders)."""

    def _param_placeholder(self, name: str | None) -> str:
        return "?"


# ---------------------------------------------------------------------------
# Format dialect (PostgreSQL, MySQL)
# ---------------------------------------------------------------------------


class FormatBuilder(SQLiteBuilder):
    """Renders SQL AST nodes using ``%s`` placeholders (format param-style).

    Used for providers whose drivers follow the DBAPI-2 ``"format"``
    param-style, including psycopg (PostgreSQL) and PyMySQL / asyncmy (MySQL).
    """

    def _param_placeholder(self, name: str | None) -> str:
        return "%s"


# ---------------------------------------------------------------------------
# Convenience helper
# ---------------------------------------------------------------------------


#: Default builder instance — SQLite dialect.
_default_builder: SQLiteBuilder = SQLiteBuilder()


def render(node: SqlNode, *, builder: SQLBuilder | None = None) -> tuple[str, list[Any]]:
    """Render *node* using *builder* (default: :class:`SQLiteBuilder`).

    Returns a ``(sql, params)`` pair.
    """
    return (builder or _default_builder).render(node)


#: Map from DBAPI-2 param-style name to builder class.
PARAM_STYLE_BUILDERS: dict[str, type[SQLBuilder]] = {
    "qmark": SQLiteBuilder,
    "format": FormatBuilder,
    "pyformat": FormatBuilder,
}
