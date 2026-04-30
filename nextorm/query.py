"""Query API — :class:`QuerySet` for building and executing SELECT queries.

Usage::

    from nextorm import Database, Entity, Req


    class User(Entity):
        name: Req[str]
        age: Req[int]


    db = Database(entities=[User])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)

    # INSERT
    u = User(name="alice", age=30)
    db.save(u)

    # SELECT with filter / order / limit
    results = db.select(User).filter(User.name == "alice").fetch_all()
    row = db.select(User).filter(User.age >= 18).order_by(User.name.asc()).fetch_one()
    count = db.select(User).count()
    exists = db.select(User).filter(User.name == "bob").exists()
"""

from __future__ import annotations

import sys
from typing import IO, TYPE_CHECKING, Any, TypeVar, cast

from nextorm.entity import _LAZY_SENTINEL, Entity
from nextorm.fields import RelationKind
from nextorm.schema.core import Table
from nextorm.sql.builder import SQLBuilder
from nextorm.sql.nodes import (
    Alias,
    BinOp,
    ColumnRef,
    Delete,
    FunctionCall,
    Literal,
    OrderItem,
    Select,
    SqlNode,
    Star,
    Update,
)
from nextorm.sql.nodes import (
    Param as _Param,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from nextorm.database import Database

__all__ = ["QuerySet", "EntityProxy"]

T = TypeVar("T", bound=Entity)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class EntityProxy:
    """A lightweight proxy passed to a ``where`` lambda.

    Attribute access returns a :class:`~nextorm.expr.ColumnExpr` so the lambda
    can build SQL predicates without referencing the entity class directly::

        qs.where(lambda p: p.price > 100)
        qs.where(lambda u: u.name == "alice")

    The proxy is created automatically by :meth:`QuerySet.where`; direct
    construction is only needed in tests or advanced usage.
    """

    __slots__ = ("_table_name",)

    def __init__(self, table_name: str) -> None:
        object.__setattr__(self, "_table_name", table_name)

    def __getattr__(self, name: str) -> Any:
        """Return a :class:`~nextorm.expr.ColumnExpr` for *name* on the proxied table."""
        from nextorm.expr import ColumnExpr as _CE  # noqa: PLC0415

        return _CE(name, object.__getattribute__(self, "_table_name"))


def _build_column_map_from_names(entity_cls: type[Entity], col_names: list[str]) -> list[str | None]:
    """Build a column-index → entity-field-name map from cursor description names.

    Used by :meth:`QuerySet.raw` to match raw-SQL result columns to entity
    fields by name rather than by position.
    """
    col_to_field: dict[str, str] = {}
    for fi in entity_cls._fields_.values():
        col_name = fi.spec.column or fi.name
        col_to_field[col_name] = fi.name
    for ri in entity_cls._relations_.values():
        if ri.spec.kind == RelationKind.SINGLE:
            fk_col = ri.spec.column or f"{ri.name}_id"
            col_to_field[fk_col] = f"_{ri.name}_id"
    return [col_to_field.get(name) for name in col_names]


def _build_column_map(entity_cls: type[Entity], table: Table) -> list[str | None]:
    """Return an ordered mapping of table-column index → entity field name.

    Columns corresponding to direct entity fields map to the field name.
    FK columns from ``Single`` relations map to the internal storage key
    ``'_<rel>_id'`` so that :class:`~nextorm.entity.SingleDescriptor` can
    find them.  All other columns (e.g. from joined tables) map to ``None``
    and are silently skipped during row hydration.
    """
    col_to_field: dict[str, str] = {}
    for fi in entity_cls._fields_.values():
        col_name = fi.spec.column or fi.name
        col_to_field[col_name] = fi.name
    # FK columns from Single relations
    for ri in entity_cls._relations_.values():
        if ri.spec.kind == RelationKind.SINGLE:
            fk_col = ri.spec.column or f"{ri.name}_id"
            col_to_field[fk_col] = f"_{ri.name}_id"
    return [col_to_field.get(col.name) for col in table.columns]


def _build_explicit_column_map(
    entity_cls: type[Entity],
) -> tuple[tuple[ColumnRef, ...], list[str | None]]:
    """Build an explicit column list and matching column-map for entities with lazy fields.

    Returns
    -------
    columns:
        Tuple of :class:`ColumnRef` nodes for all *non-lazy* fields and all FK
        columns from ``Single`` relations.
    col_map:
        Parallel list of field-name strings (or ``'_<rel>_id'`` for FK
        columns) in the same positional order as *columns*.
    """
    cols: list[ColumnRef] = []
    col_map: list[str | None] = []
    for fi in entity_cls._fields_.values():
        if not fi.spec.lazy:
            col_name = fi.spec.column or fi.name
            cols.append(ColumnRef(col_name))
            col_map.append(fi.name)
    for ri in entity_cls._relations_.values():
        if ri.spec.kind == RelationKind.SINGLE:
            fk_col = ri.spec.column or f"{ri.name}_id"
            cols.append(ColumnRef(fk_col))
            col_map.append(f"_{ri.name}_id")
    return tuple(cols), col_map


# ---------------------------------------------------------------------------
# QuerySet
# ---------------------------------------------------------------------------


class QuerySet[T: Entity]:
    """A lazy, immutable query builder for a single entity type.

    Each method returns a *new* :class:`QuerySet` — the original is unchanged —
    so chaining is safe::

        base = db.select(User)
        young = base.filter(User.age < 30)
        old = base.filter(User.age >= 30)  # independent of `young`
    """

    def __init__(
        self,
        entity_class: type[T],
        table: Table,
        db: Database,
        builder: SQLBuilder,
    ) -> None:
        self._entity_class = entity_class
        self._table = table
        self._db = db
        self._builder = builder
        self._where: SqlNode | None = None
        self._order: tuple[OrderItem, ...] = ()
        self._lim: int | None = None
        self._off: int | None = None
        self._joins: tuple[tuple[str, str, str | None, SqlNode], ...] = ()
        self._prefetches: tuple[str, ...] = ()
        self._distinct: bool = False
        self._for_update: bool = False
        self._for_update_skip_locked: bool = False
        self._for_update_nowait: bool = False
        # Lazy fields: if entity has any lazy fields, use explicit column SELECT.
        self._lazy_field_names: frozenset[str] = frozenset(
            fi.name for fi in entity_class._fields_.values() if fi.spec.lazy
        )
        self._explicit_columns: tuple[ColumnRef, ...] | None
        self._column_map: list[str | None]
        if self._lazy_field_names:
            self._explicit_columns, explicit_map = _build_explicit_column_map(entity_class)
            self._column_map = explicit_map
        else:
            self._explicit_columns = None
            # Pre-computed column-index → field-name mapping
            self._column_map = _build_column_map(entity_class, table)

    # ------------------------------------------------------------------
    # Clone
    # ------------------------------------------------------------------

    def _clone(self) -> QuerySet[T]:
        q: QuerySet[T] = object.__new__(type(self))
        q._entity_class = self._entity_class
        q._table = self._table
        q._db = self._db
        q._builder = self._builder
        q._where = self._where
        q._order = self._order
        q._lim = self._lim
        q._off = self._off
        q._joins = self._joins
        q._prefetches = self._prefetches
        q._distinct = self._distinct
        q._for_update = self._for_update
        q._for_update_skip_locked = self._for_update_skip_locked
        q._for_update_nowait = self._for_update_nowait
        q._lazy_field_names = self._lazy_field_names
        q._explicit_columns = self._explicit_columns
        q._column_map = self._column_map
        return q

    # ------------------------------------------------------------------
    # Chainable query modifiers (return a new QuerySet)
    # ------------------------------------------------------------------

    def filter(self, *conditions: SqlNode) -> QuerySet[T]:
        """Narrow results with one or more WHERE conditions.

        Multiple conditions are combined with ``AND``::

            qs.filter(User.age >= 18, User.active == True)
            # → WHERE age >= 18 AND active = 1
        """
        q = self._clone()
        for cond in conditions:
            q._where = cond if q._where is None else BinOp(q._where, "AND", cond)
        return q

    def where(self, predicate: Callable[[Any], BinOp]) -> QuerySet[T]:
        """Narrow results using a lambda predicate over a column proxy.

        The lambda receives an :class:`EntityProxy` whose attribute accesses
        return :class:`~nextorm.expr.ColumnExpr` objects, so comparison
        operators build SQL conditions transparently::

            qs.where(lambda p: p.price > 100)
            qs.where(lambda u: u.name == "alice")

        Chain multiple ``.where()`` calls to combine conditions with AND::

            qs.where(lambda u: u.age >= 18).where(lambda u: u.active == True)
        """
        proxy = EntityProxy(self._table.name)
        cond = predicate(proxy)
        return self.filter(cond)

    def order_by(self, *items: OrderItem) -> QuerySet[T]:
        """Set the ``ORDER BY`` clause, replacing any previous ordering.

        Use :meth:`~nextorm.expr.ColumnExpr.asc` / :meth:`~nextorm.expr.ColumnExpr.desc`
        on a column expression to build order items::

            qs.order_by(User.name.asc(), User.age.desc())
        """
        q = self._clone()
        q._order = tuple(items)
        return q

    def limit(self, n: int) -> QuerySet[T]:
        """Limit the number of rows returned."""
        q = self._clone()
        q._lim = n
        return q

    def offset(self, n: int) -> QuerySet[T]:
        """Skip the first *n* rows."""
        q = self._clone()
        q._off = n
        return q

    def join(
        self,
        table_or_entity: type[Entity] | str,
        on: BinOp,
        *,
        join_type: str = "INNER",
        alias: str | None = None,
    ) -> QuerySet[T]:
        """Add a JOIN clause to the query.

        Parameters
        ----------
        table_or_entity:
            The table (name string) or entity class to join.
        on:
            The ``ON`` condition.
        join_type:
            SQL join type: ``"INNER"`` (default), ``"LEFT"``, ``"RIGHT"``,
            or ``"FULL"``.
        alias:
            Optional alias for the joined table.

        Example::

            db.select(Post).join(Comment, Comment.post_id == Post.id, join_type="LEFT").fetch_all()
        """
        if isinstance(table_or_entity, str):
            table_name = table_or_entity
        else:
            table_name = table_or_entity._table_name_
        q = self._clone()
        q._joins = (*q._joins, (join_type, table_name, alias, on))
        return q

    def prefetch(self, *relation_attrs: Any) -> QuerySet[T]:
        """Declare relations to eager-load alongside the main query.

        After :meth:`fetch_all` executes the main SELECT, one additional query
        per prefetched relation is issued and the results are attached to the
        returned entity instances.  This avoids the N+1 query problem.

        Parameters
        ----------
        \\*relation_attrs:
            Attribute descriptors from the entity class, e.g. ``Post.tags``.
            String attribute names are also accepted.

        Example::

            posts = db.select(Post).prefetch(Post.comments).fetch_all()
            for post in posts:
                # post.comments is pre-populated — no extra query fired
                print(post.comments.count())
        """
        names: list[str] = []
        for attr in relation_attrs:
            if isinstance(attr, str):
                names.append(attr)
            else:
                # SetDescriptor / SingleDescriptor — extract name
                name = getattr(attr, "name", None)
                if name is None:
                    raise ValueError(f"Cannot determine relation name from {attr!r}.")
                names.append(name)
        q = self._clone()
        q._prefetches = (*q._prefetches, *names)
        return q

    # ------------------------------------------------------------------
    # Terminal methods — execute and return results
    # ------------------------------------------------------------------

    def fetch_all(self) -> list[T]:
        """Execute the query and return all matching entity instances."""
        stmt = self._build_select()
        sql, params = self._builder.render(stmt)
        rows = self._db._execute(sql, params)
        results = [self._map_row(row) for row in rows]
        if self._prefetches:
            self._do_prefetch(results)
        return results

    def fetch_one(self) -> T | None:
        """Execute with ``LIMIT 1`` and return the first entity, or ``None``."""
        stmt = self._build_select(extra_limit=1)
        sql, params = self._builder.render(stmt)
        rows = self._db._execute(sql, params)
        if not rows:
            return None
        result = self._map_row(rows[0])
        if self._prefetches:
            self._do_prefetch([result])
        return result

    def first(self) -> T | None:
        """Alias for :meth:`fetch_one`."""
        return self.fetch_one()

    def count(self) -> int:
        """Return the number of rows matching the current filter."""
        stmt = Select(
            columns=(Alias(FunctionCall("COUNT", (Star(),)), "_n"),),
            from_table=self._table.name,
            joins=self._joins,
            where=self._where,
        )
        sql, params = self._builder.render(stmt)
        rows = self._db._execute(sql, params)
        return int(rows[0][0]) if rows else 0

    def exists(self) -> bool:
        """Return ``True`` if at least one row matches the current filter."""
        stmt = Select(
            columns=(Literal(1),),
            from_table=self._table.name,
            joins=self._joins,
            where=self._where,
            limit=1,
        )
        sql, params = self._builder.render(stmt)
        rows = self._db._execute(sql, params)
        return bool(rows)

    def get(self) -> T | None:
        """Return the single matching entity, or ``None`` if no rows match.

        Raises :exc:`~nextorm.exceptions.MultipleObjectsFoundError` if more
        than one row matches the current filter — use :meth:`fetch_one` when
        you only want the first row regardless of how many exist.

        Example::

            user = db.select(User).filter(User.name == "alice").get()
        """
        from nextorm.exceptions import MultipleObjectsFoundError  # noqa: PLC0415

        stmt = self._build_select(extra_limit=2)
        sql, params = self._builder.render(stmt)
        rows = self._db._execute(sql, params)
        if not rows:
            return None
        if len(rows) > 1:
            raise MultipleObjectsFoundError(
                f"get() returned more than one {self._entity_class.__name__!r}. "
                "Use fetch_all() or refine the filter."
            )
        return self._map_row(rows[0])

    def get_or_raise(self) -> T:
        """Return the single matching entity; raise if zero or more-than-one match.

        Raises :exc:`~nextorm.exceptions.ObjectNotFound` when no row matches.
        Raises :exc:`~nextorm.exceptions.MultipleObjectsFoundError` when more
        than one row matches.

        Example::

            user = db.select(User).filter(User.id == pk).get_or_raise()
        """
        from nextorm.exceptions import ObjectNotFound  # noqa: PLC0415

        result = self.get()
        if result is None:
            raise ObjectNotFound(
                f"{self._entity_class.__name__!r} matching the given filter was not found."
            )
        return result

    def delete(self) -> int:
        """Delete all rows matched by the current filter.

        Returns the number of deleted rows.
        """
        stmt = Delete(table=self._table.name, where=self._where)
        sql, params = self._builder.render(stmt)
        return self._db._execute_dml(sql, params)

    def update(self, **field_values: Any) -> int:
        """Bulk-update matched rows with the given field-value pairs.

        Returns the number of updated rows.  Only columns that correspond to
        direct entity fields (not FK columns) are accepted::

            db.select(User).filter(User.active == False).update(active=True)
        """
        if not field_values:
            return 0
        assignments: list[tuple[str, SqlNode]] = []
        for field_name, value in field_values.items():
            fi = self._entity_class._fields_.get(field_name)
            if fi is None:
                raise ValueError(f"{self._entity_class.__name__!r} has no field {field_name!r}.")
            col_name = fi.spec.column or fi.name
            assignments.append((col_name, Literal(value) if value is None else _param(value)))
        stmt = Update(
            table=self._table.name,
            assignments=tuple(assignments),
            where=self._where,
        )
        sql, params = self._builder.render(stmt)
        return self._db._execute_dml(sql, params)

    def distinct(self) -> QuerySet[T]:
        """Enable ``SELECT DISTINCT`` for this query."""
        q = self._clone()
        q._distinct = True
        return q

    def without_distinct(self) -> QuerySet[T]:
        """Disable ``SELECT DISTINCT`` (reverses a previous :meth:`distinct` call)."""
        q = self._clone()
        q._distinct = False
        return q

    def for_update(self, *, skip_locked: bool = False, nowait: bool = False) -> QuerySet[T]:
        """Append a ``FOR UPDATE`` (or ``FOR UPDATE SKIP LOCKED`` / ``FOR UPDATE NOWAIT``) clause.

        Useful for pessimistic locking.  The behaviour is provider-specific:
        SQLite does not support ``FOR UPDATE`` natively (it locks at the
        connection level instead).

        Parameters
        ----------
        skip_locked:
            When ``True``, rows already locked by another transaction are
            silently skipped rather than causing a wait or error.
        nowait:
            When ``True``, an attempt to lock already-locked rows raises an
            error immediately instead of waiting.
            Mutually exclusive with *skip_locked*.
        """
        if skip_locked and nowait:
            raise ValueError("skip_locked and nowait are mutually exclusive")
        q = self._clone()
        q._for_update = True
        q._for_update_skip_locked = skip_locked
        q._for_update_nowait = nowait
        return q

    def __len__(self) -> int:
        """Return the total number of rows matching this query.

        Equivalent to :meth:`count`::

            total = len(db.select(Product))
        """
        return self.count()

    def __getitem__(self, index: int | slice) -> T | list[T]:
        """Fetch a single item by integer index or a slice of items.

        Integer index (0-based)::

            first = db.select(Product).order_by(Product.id)[0]
            third = db.select(Product).order_by(Product.id)[2]

        Slice (start:stop, step not supported)::

            page = db.select(Product).order_by(Product.id)[10:20]

        Negative indices and step values are not supported and raise
        :exc:`ValueError`.
        """
        if isinstance(index, int):
            if index < 0:
                raise ValueError("Negative indices are not supported")
            results = self.offset(index).limit(1).fetch_all()
            if not results:
                raise IndexError(f"index {index} is out of range")
            return results[0]
        if isinstance(index, slice):  # pyright: ignore[reportUnnecessaryIsInstance]
            start = index.start or 0
            stop = index.stop
            if index.step is not None:
                raise ValueError("Step in slice is not supported")
            if start < 0 or (stop is not None and stop < 0):
                raise ValueError("Negative indices are not supported")
            qs = self.offset(start)
            if stop is not None:
                qs = qs.limit(stop - start)
            return qs.fetch_all()
        raise TypeError(f"indices must be int or slice, not {type(index).__name__}")

    def page(self, pagenum: int, pagesize: int = 10) -> QuerySet[T]:
        """Return a page of results.

        Page numbers are 1-based.  Equivalent to
        ``.offset((pagenum-1)*pagesize).limit(pagesize)``::

            db.select(Product).order_by(Product.name).page(2, pagesize=20)
        """
        if pagenum < 1:
            raise ValueError("pagenum must be >= 1")
        return self.offset((pagenum - 1) * pagesize).limit(pagesize)

    def random(self, n: int) -> QuerySet[T]:
        """Return *n* randomly ordered rows.

        Uses ``ORDER BY RANDOM()`` (SQLite / PostgreSQL) or
        ``ORDER BY RAND()`` (MariaDB).
        """
        from nextorm.sql.nodes import FunctionCall  # noqa: PLC0415
        from nextorm.sql.nodes import OrderItem as _OI

        fname = "RAND" if self._db._provider == "mariadb" else "RANDOM"
        return self.order_by(_OI(FunctionCall(fname, ()))).limit(n)

    # ------------------------------------------------------------------
    # Aggregation terminal methods
    # ------------------------------------------------------------------

    def _aggregate(self, func: str, attr: str) -> Any:
        """Execute ``SELECT func(col) FROM … WHERE …`` and return the scalar result."""
        fi = self._entity_class._fields_.get(attr)
        if fi is None:
            raise ValueError(f"{self._entity_class.__name__!r} has no field {attr!r}.")
        col = fi.spec.column or fi.name
        stmt = Select(
            columns=(Alias(FunctionCall(func, (ColumnRef(col),)), "_agg"),),
            from_table=self._table.name,
            joins=self._joins,
            where=self._where,
        )
        sql, params = self._builder.render(stmt)
        rows = self._db._execute(sql, params)
        return rows[0][0] if rows else None

    def sum(self, attr: str) -> Any:
        """Return ``SUM(attr)`` or ``None`` when no rows match."""
        return self._aggregate("SUM", attr)

    def avg(self, attr: str) -> Any:
        """Return ``AVG(attr)`` or ``None`` when no rows match."""
        return self._aggregate("AVG", attr)

    def min(self, attr: str) -> Any:
        """Return ``MIN(attr)`` or ``None`` when no rows match."""
        return self._aggregate("MIN", attr)

    def max(self, attr: str) -> Any:
        """Return ``MAX(attr)`` or ``None`` when no rows match."""
        return self._aggregate("MAX", attr)

    def group_concat(self, attr: str, sep: str = ",") -> str | None:
        """Return the concatenation of all non-NULL values of *attr*.

        Uses the database-native aggregate:

        - SQLite / MariaDB: ``GROUP_CONCAT(col, sep)``
        - PostgreSQL:     ``STRING_AGG(col, sep)``

        Returns ``None`` when no rows match or all values are NULL.

        Example::

            names = db.select(User).filter(User.active == True).group_concat("name", ", ")
        """
        fi = self._entity_class._fields_.get(attr)
        if fi is None:
            raise ValueError(f"{self._entity_class.__name__!r} has no field {attr!r}.")
        col = fi.spec.column or fi.name
        provider = self._db._provider

        # Build a WHERE clause fragment if any filter is set
        where_parts: list[str] = []
        extra_params: list[Any] = []
        if self._where is not None:
            where_sql, where_params = self._builder.render(
                Select(
                    columns=(Literal(1),),
                    from_table=self._table.name,
                    where=self._where,
                )
            )
            # Extract just the WHERE clause from the rendered SELECT
            where_idx = where_sql.upper().find(" WHERE ")
            if where_idx != -1:  # pragma: no branch
                where_parts.append(where_sql[where_idx:])
                extra_params.extend(where_params)

        table = self._table.name
        where_clause = where_parts[0] if where_parts else ""  # pragma: no branch

        if provider == "postgres":  # pragma: no cover
            func_sql = f"SELECT STRING_AGG({col}::text, ?) FROM {table}{where_clause}"
        else:
            func_sql = f"SELECT GROUP_CONCAT({col}, ?) FROM {table}{where_clause}"

        params: list[Any] = [sep, *extra_params]
        rows = self._db._execute(func_sql, params)
        return rows[0][0] if rows else None

    def get_sql(self) -> str:
        """Return the SQL string that would be executed by :meth:`fetch_all`.

        Useful for debugging or logging.  Parameters are shown as ``?``
        (SQLite) or ``%s`` (PostgreSQL / MariaDB) placeholders::

            print(db.select(User).filter(User.age > 18).get_sql())
        """
        stmt = self._build_select()
        sql, _ = self._builder.render(stmt)
        return sql

    def show(self, width: int = 120, *, file: IO[str] | None = None) -> None:
        """Pretty-print query results as a plain-text table.

        Fetches all rows and renders them to *file* (default: ``sys.stdout``)
        with aligned columns bounded by *width* characters total.  Useful for
        interactive debugging.

        Parameters
        ----------
        width:
            Maximum total table width in characters.  Column content is
            truncated proportionally when the natural width exceeds this.
        file:
            Output stream; defaults to ``sys.stdout``.

        Example::

            db.select(User).show()
            # +----+---------+-----+
            # | id | name    | age |
            # +----+---------+-----+
            # |  1 | alice   |  30 |
            # |  2 | bob     |  25 |
            # +----+---------+-----+
        """
        out = file or sys.stdout
        entity_cls = self._entity_class
        field_names = [fi.name for fi in entity_cls._fields_.values() if not fi.spec.lazy]
        if not field_names:
            print("(no columns)", file=out)
            return
        results = self.fetch_all()
        if not results:
            print("(no results)", file=out)
            return
        # Build raw string cells
        rows: list[list[str]] = [[str(getattr(r, name, "")) for name in field_names] for r in results]
        # Natural per-column widths
        col_widths = [
            max(len(h), max((len(row[i]) for row in rows), default=0))
            for i, h in enumerate(field_names)
        ]
        # Constrain columns to fit within `width`
        # table: 1 (left |) + sum(w + 3) for each col (+1 cell padding each side + 1 |)
        total = 1 + sum(w + 3 for w in col_widths)
        if total > width and field_names:
            budget = max(1, (width - len(field_names) * 3 - 1) // len(field_names))
            col_widths = [min(w, budget) for w in col_widths]
            rows = [[cell[:cw] for cell, cw in zip(row, col_widths, strict=False)] for row in rows]
        sep = "+" + "+".join("-" * (w + 2) for w in col_widths) + "+"
        hdr = (
            "|" + "|".join(f" {h:<{w}} " for h, w in zip(field_names, col_widths, strict=False)) + "|"
        )
        print(sep, file=out)
        print(hdr, file=out)
        print(sep, file=out)
        for row in rows:
            line = "|" + "|".join(f" {c:<{w}} " for c, w in zip(row, col_widths, strict=False)) + "|"
            print(line, file=out)
        print(sep, file=out)

    def raw(self, sql: str, params: list[Any] | None = None) -> list[T]:
        """Execute *sql* and map each result row to an entity instance.

        Column names in the cursor description are matched to entity fields
        by name, so the column order in *sql* does not have to follow the
        schema order.  Columns that don't match any field are silently ignored.

        Parameters
        ----------
        sql:
            Raw SQL SELECT statement.
        params:
            Positional bind parameters (``?`` placeholders for SQLite,
            ``%s`` for PostgreSQL / MariaDB).

        Example::

            users = db.select(User).raw("SELECT * FROM user WHERE age > ?", [18])
        """
        rows, col_names = self._db._execute_described(sql, params or [])
        col_map = _build_column_map_from_names(self._entity_class, col_names)
        return [self._map_raw_row(row, col_map) for row in rows]

    def raw_one(self, sql: str, params: list[Any] | None = None) -> T | None:
        """Execute *sql* and return the first mapped entity, or ``None``.

        Behaves like :meth:`raw` but returns at most one result.  The SQL
        should ideally include ``LIMIT 1`` for efficiency.
        """
        rows, col_names = self._db._execute_described(sql, params or [])
        if not rows:
            return None
        col_map = _build_column_map_from_names(self._entity_class, col_names)
        return self._map_raw_row(rows[0], col_map)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_select(self, *, extra_limit: int | None = None) -> Select:
        """Build the SELECT node, optionally imposing *extra_limit*."""
        lim = self._lim
        if extra_limit is not None:
            lim = extra_limit if self._lim is None else min(self._lim, extra_limit)
        # Use explicit column list when the entity has lazy fields so that
        # the lazy column data is not transferred from the database at all.
        columns: tuple[Any, ...] = (
            self._explicit_columns if self._explicit_columns is not None else (Star(),)
        )
        return Select(
            columns=columns,
            from_table=self._table.name,
            joins=self._joins,
            where=self._where,
            order_by=self._order,
            limit=lim,
            offset=self._off,
            distinct=self._distinct,
            for_update=self._for_update or self._for_update_skip_locked or self._for_update_nowait,
            for_update_skip_locked=self._for_update_skip_locked,
            for_update_nowait=self._for_update_nowait,
        )

    def _map_raw_row(self, row: tuple[Any, ...], col_map: list[str | None]) -> T:
        """Hydrate *row* using an explicit *col_map* (no identity-map caching)."""
        obj: T = object.__new__(self._entity_class)
        vars(obj)["_db_"] = self._db
        for field_name, value in zip(col_map, row, strict=False):
            if field_name is None:
                continue
            if field_name.startswith("_") and field_name.endswith("_id"):
                vars(obj)[field_name] = value
            else:
                setattr(obj, field_name, value)
        # Stamp lazy sentinel for any lazy fields not returned in this row
        for lname in self._lazy_field_names:
            vars(obj)[f"_field_{lname}"] = _LAZY_SENTINEL
        obj.after_load()
        return obj

    def _map_row(self, row: tuple[Any, ...]) -> T:
        """Convert a raw DB row-tuple to an entity instance.

        - Non-relation columns are set via ``setattr``.
        - FK id columns (``_<rel>_id``) are written directly to ``__dict__``
          to avoid going through the ``SingleDescriptor``'s __set__.
        - The database reference (``_db_``) is attached so lazy-load works.
        - If a ``db_session`` is active the identity map is consulted first.
        """
        from nextorm.session import _get_session_stack  # noqa: PLC0415

        entity_cls = self._entity_class
        pk_fields = entity_cls._pk_fields_

        # Try identity map first when inside a session
        cache = _get_session_stack().current
        if cache is not None and pk_fields:
            # Build pk_val from the row (scalar for single PK, tuple for composite)
            pk_col_names: list[str] = [
                (entity_cls._fields_[f].spec.column or f) if f in entity_cls._fields_ else f"{f}_id"
                for f in pk_fields
            ]
            pk_idxs: list[int | None] = [
                next((i for i, c in enumerate(self._table.columns) if c.name == pkcn), None)
                for pkcn in pk_col_names
            ]
            if all(idx is not None for idx in pk_idxs):  # pragma: no branch
                pk_parts: list[Any] = [row[idx] for idx in pk_idxs]  # type: ignore[index]
                pk_val = pk_parts[0] if len(pk_parts) == 1 else tuple(pk_parts)
                cached = cache.get((entity_cls, pk_val))
                if cached is not None:
                    return cached  # type: ignore[return-value]

        obj: T = object.__new__(entity_cls)
        vars(obj)["_db_"] = self._db
        for field_name, value in zip(self._column_map, row, strict=False):
            if field_name is None:  # pragma: no cover
                continue
            if field_name.startswith("_") and field_name.endswith("_id"):
                # FK id — store directly in __dict__, bypassing descriptors
                vars(obj)[field_name] = value
            else:
                setattr(obj, field_name, value)
        # Stamp lazy sentinel for any lazy fields not returned in this row
        for lname in self._lazy_field_names:
            vars(obj)[f"_field_{lname}"] = _LAZY_SENTINEL
        obj.after_load()

        # Register in identity map when inside a db_session.
        if pk_fields:
            from nextorm.database import _get_pk_val  # noqa: PLC0415

            pk_val = _get_pk_val(obj)
            if cache is not None and pk_val is not None:
                cache.put(obj, pk_val)

            # Option A — per-field optimistic tracking: store original DB values and
            # an empty read-set on the entity.  FieldDescriptor/__get__ populates
            # _read_cols_ on access; _do_update appends AND clauses only for those cols.
            dbvals: dict[str, Any] = {}
            for fi in entity_cls._fields_.values():
                if not fi.spec.primary_key and not fi.spec.volatile and not fi.spec.lazy:
                    col = fi.spec.column or fi.name
                    dbvals[col] = getattr(obj, fi.name, None)
            for ri in entity_cls._relations_.values():
                if ri.spec.kind == RelationKind.SINGLE:
                    dbvals[ri.spec.column or f"{ri.name}_id"] = vars(obj).get(f"_{ri.name}_id")
            vars(obj)["_dbvals_"] = dbvals
            vars(obj)["_read_cols_"] = set()

        return obj

    def _do_prefetch(self, results: list[T]) -> None:
        """Execute prefetch queries and attach results to *results*."""
        if not results:
            return
        entity_cls = self._entity_class
        pk_fields = entity_cls._pk_fields_
        if not pk_fields:
            return

        from nextorm.database import _get_pk_val  # noqa: PLC0415

        owner_pks = [_get_pk_val(obj) for obj in results]
        owner_by_pk: dict[Any, T] = {
            _get_pk_val(obj): obj for obj in results if _get_pk_val(obj) is not None
        }

        for rel_name in self._prefetches:
            ri = entity_cls._relations_.get(rel_name)
            if ri is None:
                raise ValueError(f"Entity {entity_cls.__name__!r} has no relation {rel_name!r}.")
            from nextorm.collection import RelatedCollection  # noqa: PLC0415
            from nextorm.fields import RelationKind  # noqa: PLC0415

            if ri.spec.kind != RelationKind.SET:
                # Batch-load Single (FK) relation: collect all FK ids, one IN query,
                # cache loaded objects on each result entity to avoid N+1 SELECT.
                from nextorm.entity import _resolve_entity_target  # noqa: PLC0415

                resolved_target = _resolve_entity_target(ri.spec.target)
                if resolved_target is None:
                    continue
                target_cls = cast("type[Entity]", resolved_target)
                target_pk_field = target_cls._pk_field_
                if target_pk_field is None:
                    continue
                fk_key = f"_{rel_name}_id"
                obj_key = f"_{rel_name}_obj"
                fk_ids = [vars(obj).get(fk_key) for obj in results]
                unique_fk_ids = list(dict.fromkeys(fk for fk in fk_ids if fk is not None))
                if not unique_fk_ids:
                    for obj in results:
                        vars(obj)[obj_key] = None
                    continue
                target_pk_col = target_cls._fields_[target_pk_field].spec.column or target_pk_field
                ph = "?" if self._db._provider == "sqlite" else "%s"
                placeholders = ", ".join(ph for _ in unique_fk_ids)
                target_rows = self._db._execute(
                    f"SELECT * FROM {target_cls._table_name_} "
                    f"WHERE {target_pk_col} IN ({placeholders})",
                    unique_fk_ids,
                )
                target_qs = self._db.select(target_cls)
                related_by_pk: dict[Any, Any] = {}
                for trow in target_rows:
                    tobj = target_qs._map_row(trow)
                    tpk = getattr(tobj, target_pk_field)
                    related_by_pk[tpk] = tobj
                for obj, fk_id in zip(results, fk_ids, strict=True):
                    vars(obj)[obj_key] = related_by_pk.get(fk_id) if fk_id is not None else None
                continue

            target = ri.spec.target
            from nextorm.entity import _matches_entity, _resolve_entity_target  # noqa: PLC0415

            resolved = _resolve_entity_target(target)
            if resolved is None:
                continue  # Can't prefetch unresolved forward-ref
            target_cls = cast("type[Entity]", resolved)

            # Determine if M2M or O2M
            owner_cls = entity_cls
            is_m2m = any(
                r.spec.kind == RelationKind.SET and _matches_entity(r.spec.target, owner_cls)
                for r in target_cls._relations_.values()
            )

            if is_m2m:
                join_table = "_".join(sorted([owner_cls._table_name_, target_cls._table_name_]))
                owner_col = f"{owner_cls._table_name_}_id"
                ph = "?" if self._db._provider == "sqlite" else "%s"
                inline = ", ".join(ph for _ in owner_pks)
                _sql = f"SELECT * FROM {join_table} WHERE {owner_col} IN ({inline})"
                join_rows = self._db._execute(_sql, owner_pks)
                # Build owner_pk → [target_pk]
                target_pks_for: dict[Any, list[Any]] = {}
                for jrow in join_rows:
                    owner_pk_val = jrow[0] if jrow[0] in owner_by_pk else jrow[1]
                    target_pk_val = jrow[1] if owner_pk_val == jrow[0] else jrow[0]
                    target_pks_for.setdefault(owner_pk_val, []).append(target_pk_val)

                if not target_pks_for:
                    # No related items — attach empty caches
                    for obj in results:
                        col_obj: RelatedCollection[Any] = RelatedCollection(obj, ri, self._db)
                        col_obj._cache = []
                        vars(obj)[f"_{rel_name}_col"] = col_obj
                    continue

                all_target_pks = [pk for pks in target_pks_for.values() for pk in pks]
                target_pk_field = target_cls._pk_field_
                assert target_pk_field is not None
                target_pk_col = target_cls._fields_[target_pk_field].spec.column or target_pk_field
                all_inline = ", ".join(
                    "?" if self._db._provider == "sqlite" else "%s" for _ in all_target_pks
                )
                target_rows = self._db._execute(
                    f"SELECT * FROM {target_cls._table_name_} "
                    f"WHERE {target_pk_col} IN ({all_inline})",
                    all_target_pks,
                )
                target_qs = self._db.select(target_cls)
                target_by_pk: dict[Any, Any] = {}
                for trow in target_rows:
                    tobj = target_qs._map_row(trow)
                    tpk = getattr(tobj, target_pk_field)
                    target_by_pk[tpk] = tobj

                for obj in results:
                    opk = _get_pk_val(obj)
                    related_objs = [
                        target_by_pk[tpk]
                        for tpk in target_pks_for.get(opk, [])
                        if tpk in target_by_pk
                    ]
                    col: RelatedCollection[Any] = RelatedCollection(obj, ri, self._db)
                    col._cache = related_objs
                    vars(obj)[f"_{rel_name}_col"] = col
            else:
                # O2M — find back-ref FK column on target
                back_ref = next(
                    (
                        r
                        for r in target_cls._relations_.values()
                        if r.spec.kind == RelationKind.SINGLE
                        and _matches_entity(r.spec.target, entity_cls)
                    ),
                    None,
                )
                if back_ref is None:
                    continue
                fk_col = f"{back_ref.name}_id"
                ph = "?" if self._db._provider == "sqlite" else "%s"
                inline = ", ".join(ph for _ in owner_pks)
                target_rows = self._db._execute(
                    f"SELECT * FROM {target_cls._table_name_} WHERE {fk_col} IN ({inline})",
                    owner_pks,
                )
                target_qs = self._db.select(target_cls)
                # Group target rows by FK value
                grouped: dict[Any, list[Any]] = {pk: [] for pk in owner_pks}
                for trow in target_rows:
                    tobj = target_qs._map_row(trow)
                    fk_val = vars(tobj).get(f"_{back_ref.name}_id")
                    if fk_val in grouped:  # pragma: no branch
                        grouped[fk_val].append(tobj)

                for obj in results:
                    opk = _get_pk_val(obj)
                    col2: RelatedCollection[Any] = RelatedCollection(obj, ri, self._db)
                    col2._cache = grouped.get(opk, [])
                    vars(obj)[f"_{rel_name}_col"] = col2


def _param(value: Any) -> SqlNode:
    """Wrap *value* in a :class:`~nextorm.sql.nodes.Param` node."""
    return _Param(value=value)
