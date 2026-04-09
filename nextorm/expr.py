"""Column expression helpers for query predicate and ORDER BY building.

:class:`ColumnExpr` instances are returned when a field is accessed at the
class level (e.g. ``User.name``).  They support Python comparison operators
and ordering helpers so that filter expressions read naturally::

    db.select(User).filter(User.name == "alice")
    db.select(User).filter(User.age >= 18, User.active == True)
    db.select(User).order_by(User.name.asc(), User.age.desc())

Each operator produces a :class:`~nextorm.sql.nodes.BinOp` node whose right
operand is a :class:`~nextorm.sql.nodes.Param` node carrying the bound value.
The builder then collects the value into the ``params`` list during rendering,
keeping user-supplied data safely separated from SQL text.
"""

from __future__ import annotations

from nextorm.sql.nodes import BinOp, ColumnRef, Literal, OrderItem, Param

__all__ = ["ColumnExpr"]


class ColumnExpr:
    """A column reference used in query predicates and ``ORDER BY`` clauses.

    Instances are created automatically by
    :class:`~nextorm.entity.FieldDescriptor` when a field is accessed on the
    entity *class* (not on an instance).  Direct construction is also
    possible for low-level use::

        expr = ColumnExpr("name", "user")
        cond = expr == "alice"  # BinOp(ColumnRef("name", "user"), "=", Param(value="alice"))
    """

    __slots__ = ("field_name", "table_name")

    def __init__(self, field_name: str, table_name: str | None = None) -> None:
        self.field_name = field_name
        self.table_name = table_name

    def __repr__(self) -> str:
        if self.table_name:
            return f"ColumnExpr({self.table_name!r}.{self.field_name!r})"
        return f"ColumnExpr({self.field_name!r})"

    def __hash__(self) -> int:
        return hash((self.field_name, self.table_name))

    # ------------------------------------------------------------------
    # SQL column reference
    # ------------------------------------------------------------------

    @property
    def ref(self) -> ColumnRef:
        """The underlying :class:`~nextorm.sql.nodes.ColumnRef` node."""
        return ColumnRef(self.field_name, self.table_name)

    def _coerce(self, value: object) -> ColumnRef | Param:
        """Coerce *value* to a SQL node suitable as a ``BinOp`` operand.

        * Other :class:`ColumnExpr` values become a :class:`ColumnRef` —
          enabling column-to-column comparisons.
        * Everything else is wrapped in a :class:`~nextorm.sql.nodes.Param` so
          the value is passed as a bound parameter (safe, no SQL injection).
        """
        if isinstance(value, ColumnExpr):
            return value.ref
        return Param(value=value)

    # ------------------------------------------------------------------
    # Comparison operators — each returns a BinOp node
    # ------------------------------------------------------------------

    def __eq__(self, other: object) -> BinOp:  # type: ignore[override]
        return BinOp(self.ref, "=", self._coerce(other))

    def __ne__(self, other: object) -> BinOp:  # type: ignore[override]
        return BinOp(self.ref, "<>", self._coerce(other))

    def __lt__(self, other: object) -> BinOp:
        return BinOp(self.ref, "<", self._coerce(other))

    def __le__(self, other: object) -> BinOp:
        return BinOp(self.ref, "<=", self._coerce(other))

    def __gt__(self, other: object) -> BinOp:
        return BinOp(self.ref, ">", self._coerce(other))

    def __ge__(self, other: object) -> BinOp:
        return BinOp(self.ref, ">=", self._coerce(other))

    # ------------------------------------------------------------------
    # Ordering helpers
    # ------------------------------------------------------------------

    def asc(self) -> OrderItem:
        """Return an ``ASC`` :class:`~nextorm.sql.nodes.OrderItem` for this column."""
        return OrderItem(self.ref)

    def desc(self) -> OrderItem:
        """Return a ``DESC`` :class:`~nextorm.sql.nodes.OrderItem` for this column."""
        return OrderItem(self.ref, descending=True)

    # ------------------------------------------------------------------
    # Additional SQL predicates
    # ------------------------------------------------------------------

    def like(self, pattern: str) -> BinOp:
        """Return a ``LIKE`` predicate with *pattern* as a bound parameter."""
        return BinOp(self.ref, "LIKE", Param(value=pattern))

    def ilike(self, pattern: str) -> BinOp:
        """Return an ``ILIKE`` predicate (case-insensitive; Postgres-style)."""
        return BinOp(self.ref, "ILIKE", Param(value=pattern))

    def is_null(self) -> BinOp:
        """Return an ``IS NULL`` predicate."""
        return BinOp(self.ref, "IS", Literal(None))

    def is_not_null(self) -> BinOp:
        """Return an ``IS NOT NULL`` predicate."""
        return BinOp(self.ref, "IS NOT", Literal(None))

    # ------------------------------------------------------------------
    # pgvector distance operators
    # ------------------------------------------------------------------

    def dist_l2(self, vector: object) -> BinOp:
        """L2 (Euclidean) distance — pgvector ``<->`` operator."""
        return BinOp(self.ref, "<->", Param(value=vector))

    def dist_cosine(self, vector: object) -> BinOp:
        """Cosine distance — pgvector ``<=>`` operator."""
        return BinOp(self.ref, "<=>", Param(value=vector))

    def dist_inner_product(self, vector: object) -> BinOp:
        """Negative inner product — pgvector ``<#>`` operator."""
        return BinOp(self.ref, "<#>", Param(value=vector))
