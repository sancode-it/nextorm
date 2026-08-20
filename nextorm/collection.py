"""RelatedCollection — lazy collection returned by ``Set[T]`` relation access.

Usage::

    post = db.select(Post).fetch_one()
    post.comments  # → RelatedCollection[Comment]
    post.comments.count()  # → int (SELECT COUNT(*))
    for c in post.comments:
        print(c.text)  # lazy-loads all comments on first iteration

    post.comments.add(Comment(text="hello"))
    post.comments.remove(comment)
    post.comments.clear()
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator  # noqa: TC003
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from nextorm.database import Database
    from nextorm.entity import Entity, RelationInfo
    from nextorm.query import QuerySet

__all__ = ["RelatedCollection"]


class RelatedCollection[ET: Entity]:
    """A lazy, database-backed collection representing one side of a relation.

    Instances are created by :class:`~nextorm.entity.SetDescriptor` on
    first attribute access.  They hold a weak logical reference to the owner
    entity so that individual collections can be GC'd when not needed.

    The collection is *not* thread-safe.  Use separate collection objects in
    separate threads.

    Parameters
    ----------
    owner:
        The entity instance that owns this relation attribute.
    ri:
        The :class:`~nextorm.entity.RelationInfo` for this relation.
    db:
        The :class:`~nextorm.database.Database` to query.  ``None`` when the
        owner entity was not loaded from a database (e.g. a freshly created
        but unsaved entity).

    """

    def __init__(
        self,
        owner: Entity,
        ri: RelationInfo,
        db: Database | None,
    ) -> None:
        self._owner = owner
        self._ri = ri
        self._db = db
        self._cache: list[ET] | None = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_db(self) -> Database:
        if self._db is None:
            raise RuntimeError(
                f"Cannot access related collection '{self._ri.name}': "
                "the owner entity has no database context. "
                "Load it via db.select(...).fetch_all() first."
            )
        return self._db

    def _owner_pk(self) -> Any:
        from nextorm.database import _get_pk_val  # noqa: PLC0415

        return _get_pk_val(self._owner)

    def _resolve_target(self) -> type[Entity]:
        """Return the concrete target entity class."""
        from nextorm.entity import _resolve_entity_target  # noqa: PLC0415

        target = self._ri.spec.target
        resolved = _resolve_entity_target(target)
        if resolved is None:
            tname = target if isinstance(target, str) else repr(target)
            raise RuntimeError(
                f"Cannot resolve forward-reference target '{tname}'. "
                "Ensure the entity is defined and registered."
            )
        return cast("type[Entity]", resolved)

    def _is_m2m(self) -> bool:
        """Return True when the relation is many-to-many (join table exists).

        A relation is M2M when both sides carry a Set pointing at each other.
        When ``reverse`` is declared we check only that specific back-reference
        to avoid false positives when the target entity happens to have another
        unrelated Set pointing at the owner (e.g. User has both Set[Shop]
        via 'owner' and Set[Shop] via 'followers').
        """
        from nextorm.entity import _matches_entity  # noqa: PLC0415
        from nextorm.fields import RelationKind  # noqa: PLC0415

        target_cls = self._resolve_target()
        owner_cls = type(self._owner)
        reverse_name = getattr(self._ri.spec, "reverse", None)
        if reverse_name is not None:
            back_ri = target_cls._relations_.get(reverse_name)
            if back_ri is not None:
                return back_ri.spec.kind == RelationKind.SET
        # Fallback: check if any Set on target points back at the owner.
        return any(
            r.spec.kind == RelationKind.SET and _matches_entity(r.spec.target, owner_cls)
            for r in target_cls._relations_.values()
        )

    def _join_table_name(self) -> str:
        target_cls = self._resolve_target()
        owner_table = type(self._owner)._table_name_
        target_table = target_cls._table_name_
        return "_".join(sorted([owner_table, target_table]))

    def _target_pk_col(self) -> str:
        """Return the actual PK column name for the target entity."""
        from nextorm.entity import _pk_col_for_field  # noqa: PLC0415

        target_cls = self._resolve_target()
        pk_fields: tuple[str, ...] = getattr(target_cls, "_pk_fields_", ())
        if not pk_fields:
            return "id"
        return _pk_col_for_field(target_cls, pk_fields[0])

    def _build_queryset(self) -> QuerySet[ET]:
        """Return a :class:`~nextorm.query.QuerySet` scoped to this collection."""
        db = self._require_db()
        target_cls = self._resolve_target()
        qs: QuerySet[ET] = db.select(target_cls)  # type: ignore[arg-type]

        if self._is_m2m():
            # M2M — join through the join table
            join_table = self._join_table_name()
            owner_table = type(self._owner)._table_name_
            owner_pk = self._owner_pk()
            target_table = target_cls._table_name_
            # Use the actual PK column of the target entity (may not be 'id')
            target_pk_col = self._target_pk_col()

            from nextorm.sql.nodes import BinOp, ColumnRef, Param  # noqa: PLC0415

            # SELECT target.* FROM target JOIN <join> ON target.<pk> = <join>.target_id
            # WHERE <join>.owner_id = <pk>
            join_cond = BinOp(
                ColumnRef(target_pk_col, target_table),
                "=",
                ColumnRef(f"{target_table}_id", join_table),
            )
            owner_cond = BinOp(
                ColumnRef(f"{owner_table}_id", join_table),
                "=",
                Param(value=owner_pk),
            )
            return qs.join(join_table, join_cond).filter(owner_cond)
        else:
            # O2M — filter by FK column on target side
            owner_cls = type(self._owner)
            owner_pk = self._owner_pk()

            # Find the Single back-ref on target pointing at us
            from nextorm.entity import _matches_entity  # noqa: PLC0415
            from nextorm.fields import RelationKind  # noqa: PLC0415

            back_ref = next(
                (
                    r
                    for r in target_cls._relations_.values()
                    if r.spec.kind == RelationKind.SINGLE
                    and _matches_entity(r.spec.target, owner_cls)
                ),
                None,
            )
            if back_ref is None:
                raise RuntimeError(
                    f"Cannot find Single back-reference on "
                    f"{target_cls.__name__} pointing at {owner_cls.__name__}. "
                    "Ensure a Single[...] attribute is declared on the target."
                )

            from nextorm.sql.nodes import BinOp, ColumnRef, Param  # noqa: PLC0415

            # For composite-PK owners, the FK on the target has multiple columns.
            if isinstance(owner_pk, tuple):
                from nextorm.entity import _derive_composite_fk_cols  # noqa: PLC0415

                fk_col_names = _derive_composite_fk_cols(back_ref.name, owner_cls)
                if len(fk_col_names) == len(owner_pk):  # pyright: ignore[reportUnknownArgumentType]
                    # Build AND of individual column conditions
                    cond: BinOp = BinOp(ColumnRef(fk_col_names[0]), "=", Param(value=owner_pk[0]))
                    for col_name, pk_part in zip(fk_col_names[1:], owner_pk[1:], strict=False):  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]
                        cond = BinOp(
                            cond,
                            "AND",
                            BinOp(ColumnRef(col_name), "=", Param(value=pk_part)),
                        )
                    return qs.filter(cond)

            # Simple (single-column) FK
            fk_col = back_ref.spec.column or f"{back_ref.name}_id"
            return qs.filter(BinOp(ColumnRef(fk_col), "=", Param(value=owner_pk)))

    def _ensure_loaded(self) -> list[ET]:
        """Load all items into the cache and return it."""
        if self._cache is None:
            if self._owner_pk() is None:
                return []
            self._cache = self._build_queryset().fetch_all()
        return self._cache

    def _invalidate(self) -> None:
        """Invalidate the in-memory cache so the next access re-queries."""
        self._cache = None

    # ------------------------------------------------------------------
    # Sequence-like interface
    # ------------------------------------------------------------------

    def __iter__(self) -> Iterator[ET]:
        """Iterate over all related objects, loading them from the database if necessary."""
        yield from self._ensure_loaded()

    def __len__(self) -> int:
        """Return the number of related objects via ``SELECT COUNT(*)``."""
        return self.count()

    def __contains__(self, item: object) -> bool:
        target_cls = self._resolve_target()
        if not isinstance(item, target_cls):
            return False
        from nextorm.database import _get_pk_val  # noqa: PLC0415

        pk = _get_pk_val(item)
        if pk is None:
            return False

        if self._is_m2m():
            join_table = self._join_table_name()
            owner_table = type(self._owner)._table_name_
            target_table = target_cls._table_name_
            owner_pk = self._owner_pk()
            db = self._require_db()

            from nextorm.sql.nodes import BinOp, ColumnRef, Param, Select, Star  # noqa: PLC0415

            stmt = Select(
                columns=(Star(),),
                from_table=join_table,
                where=BinOp(
                    BinOp(
                        ColumnRef(f"{owner_table}_id"),
                        "=",
                        Param(value=owner_pk),
                    ),
                    "AND",
                    BinOp(
                        ColumnRef(f"{target_table}_id"),
                        "=",
                        Param(value=pk),
                    ),
                ),
                limit=1,
            )
            assert db._builder is not None
            sql, params = db._builder.render(stmt)
            return bool(db._execute(sql, params))
        else:
            # Check whether the FK column on target points to our owner
            back_ref_name = self._ri.spec.reverse
            if back_ref_name is None:
                owner_cls = type(self._owner)
                from nextorm.entity import _matches_entity as _me  # noqa: PLC0415
                from nextorm.fields import RelationKind  # noqa: PLC0415

                back_ref = next(
                    (
                        r
                        for r in target_cls._relations_.values()
                        if r.spec.kind == RelationKind.SINGLE and _me(r.spec.target, owner_cls)
                    ),
                    None,
                )
                if back_ref is None:
                    return False
                back_ref_name = back_ref.name
            br_id = f"_{back_ref_name}_id"
            br_obj = f"_{back_ref_name}_obj"
            stored = item.__dict__.get(br_id) or item.__dict__.get(br_obj)
            if stored is None:
                return False
            if isinstance(stored, int):
                return bool(stored == self._owner_pk())
            from nextorm.database import _get_pk_val as _gpv  # noqa: PLC0415
            from nextorm.entity import Entity as _Entity  # noqa: PLC0415

            stored_pk = _gpv(stored) if isinstance(stored, _Entity) else None
            return bool(stored_pk == self._owner_pk())

    # ------------------------------------------------------------------
    # Query methods
    # ------------------------------------------------------------------

    def count(self) -> int:
        """Return the number of items without loading them."""
        if self._owner_pk() is None:
            return 0
        return self._build_queryset().count()

    def is_empty(self) -> bool:
        """Return ``True`` if the collection has no items."""
        return not self._build_queryset().exists()

    def copy(self) -> set[ET]:
        """Return a plain Python ``set`` of all loaded items."""
        return set(self._ensure_loaded())

    def load(self) -> list[ET]:
        """Eagerly load all items and return them as a list."""
        return self._ensure_loaded()

    def filter(self, *conditions: Any, **kwargs: Any) -> QuerySet[ET]:
        """Return a filtered :class:`~nextorm.query.QuerySet` for this collection.

        Accepts :class:`~nextorm.sql.nodes.SqlNode` conditions and keyword
        equality shortcuts.  For lambda predicates use :meth:`where` instead:

        .. code-block:: python

            post.comments.filter(Comment.approved == True)
            post.comments.filter(approved=True)
            post.comments.filter(Comment.score > 5, approved=True)
        """
        from nextorm.sql.nodes import BinOp, ColumnRef, Param

        qs = self._build_queryset()
        for cond in conditions:
            qs = qs.filter(cond)
        for field, value in kwargs.items():
            qs = qs.filter(BinOp(ColumnRef(field), "=", Param(value=value)))
        return qs

    def where(self, predicate: Callable[[Any], Any]) -> QuerySet[ET]:
        """Narrow results using a lambda predicate over a column proxy.

        The lambda receives a proxy whose attribute accesses return
        :class:`~nextorm.expr.ColumnExpr` objects, so comparison operators
        build SQL conditions transparently.  M2M containment and boolean
        attribute checks are also supported via bytecode decompilation:

        .. code-block:: python

            post.comments.where(lambda c: c.approved == True)
            post.comments.where(lambda c: c.score > 5)
        """
        from nextorm.generators import _apply_predicate  # noqa: PLC0415

        qs = self._build_queryset()
        target_cls = self._resolve_target()
        return _apply_predicate(qs, predicate, target_cls)  # type: ignore[no-any-return]

    def select(
        self,
        predicate: Callable[[Any], Any] | None = None,
        /,
        *conditions: Any,
        **kwargs: Any,
    ) -> QuerySet[ET]:
        """Return a :class:`~nextorm.query.QuerySet` for this collection.

        Accepts the same optional arguments as :meth:`~nextorm.entity.Entity.select`:
        an optional lambda *predicate*, positional :class:`~nextorm.sql.nodes.SqlNode`
        *conditions*, and keyword equality *kwargs* — all combined with ``AND``:

        .. code-block:: python

            # Whole collection (no filter)
            post.comments.select()

            # Lambda predicate
            post.comments.select(lambda c: c.approved == True)

            # SqlNode conditions
            post.comments.select(Comment.approved == True)

            # Keyword equality shortcuts
            post.comments.select(approved=True)

            # All combined
            post.comments.select(lambda c: c.score > 5, Comment.approved == True, spam=False)
        """
        from nextorm.sql.nodes import BinOp, ColumnRef, Param  # noqa: PLC0415

        qs = self._build_queryset()
        if predicate is not None:
            from nextorm.generators import _apply_predicate  # noqa: PLC0415

            target_cls = self._resolve_target()
            qs = _apply_predicate(qs, predicate, target_cls)
        for cond in conditions:
            qs = qs.filter(cond)
        for field, value in kwargs.items():
            qs = qs.filter(BinOp(ColumnRef(field), "=", Param(value=value)))
        return qs

    def order_by(self, *items: Any) -> QuerySet[ET]:
        """Return an ordered :class:`~nextorm.query.QuerySet` for this collection."""
        return self._build_queryset().order_by(*items)

    def page(self, pagenum: int, pagesize: int = 10) -> QuerySet[ET]:
        """Return a page of this collection's items (1-based page numbers)."""
        return self._build_queryset().page(pagenum, pagesize)

    def random(self, n: int) -> QuerySet[ET]:
        """Return *n* randomly selected items from this collection."""
        return self._build_queryset().random(n)

    # ------------------------------------------------------------------
    # Mutation methods
    # ------------------------------------------------------------------

    def add(self, *items: ET | Iterable[ET]) -> None:
        """Add one or more items to this collection.

        Accepts either individual items or an iterable of items::

            collection.add(item1, item2)  # Individual items
            collection.add([item1, item2])  # List or other iterable of items

        For M2M: inserts rows into the join table.
        For O2M: updates the FK column on each item.
        """
        # Flatten items: accept both individual args and iterables
        flat_items: list[ET] = []
        for item in items:
            if isinstance(item, (list, tuple)):
                flat_items.extend(cast("Iterable[ET]", item))
            else:
                flat_items.append(cast("ET", item))

        db = self._require_db()
        owner_pk = self._owner_pk()
        if owner_pk is None:
            raise RuntimeError(
                f"Cannot add to '{self._ri.name}': owner entity has no primary key. "
                "Save or flush the owner first."
            )
        owner_cls = type(self._owner)

        if self._is_m2m():
            join_table = self._join_table_name()
            owner_table = owner_cls._table_name_
            target_cls = self._resolve_target()
            target_table = target_cls._table_name_

            from nextorm.database import _get_pk_val as _gpv  # noqa: PLC0415
            from nextorm.sql.nodes import Insert, Param  # noqa: PLC0415

            owner_col = f"{owner_table}_id"
            target_col = f"{target_table}_id"
            assert db._builder is not None
            for item in flat_items:
                item_pk = _gpv(item)
                stmt = Insert(
                    table=join_table,
                    columns=(owner_col, target_col),
                    values=(Param(value=owner_pk), Param(value=item_pk)),
                )
                sql, params = db._builder.render(stmt)
                db._execute_dml(sql, params)
        else:
            target_cls = self._resolve_target()
            from nextorm.entity import _matches_entity as _me  # noqa: PLC0415
            from nextorm.fields import RelationKind  # noqa: PLC0415

            back_ref = next(
                (
                    r
                    for r in target_cls._relations_.values()
                    if r.spec.kind == RelationKind.SINGLE and _me(r.spec.target, owner_cls)
                ),
                None,
            )
            if back_ref is None:
                raise RuntimeError(f"Cannot add to {self._ri.name}: no Single back-reference found.")
            for item in flat_items:
                setattr(item, back_ref.name, self._owner)
                db.save(item)

        self._invalidate()

    def remove(self, *items: ET | Iterable[ET]) -> None:
        """Remove one or more items from this collection.

        Accepts either individual items or an iterable of items::

            collection.remove(item1, item2)  # Individual items
            collection.remove([item1, item2])  # List or other iterable of items

        For M2M: deletes rows from the join table.
        For O2M: sets the FK column to NULL (requires nullable FK).
        """
        # Flatten items: accept both individual args and iterables
        flat_items: list[ET] = []
        for item in items:
            if isinstance(item, (list, tuple)):
                flat_items.extend(cast("Iterable[ET]", item))
            else:
                flat_items.append(cast("ET", item))

        db = self._require_db()
        owner_pk = self._owner_pk()
        owner_cls = type(self._owner)

        if self._is_m2m():
            join_table = self._join_table_name()
            owner_table = owner_cls._table_name_
            target_cls = self._resolve_target()
            target_table = target_cls._table_name_

            from nextorm.database import _get_pk_val as _gpv  # noqa: PLC0415
            from nextorm.sql.nodes import BinOp, ColumnRef, Delete, Param  # noqa: PLC0415

            owner_col = f"{owner_table}_id"
            target_col = f"{target_table}_id"
            assert db._builder is not None
            for item in flat_items:
                item_pk = _gpv(item)
                stmt = Delete(
                    table=join_table,
                    where=BinOp(
                        BinOp(ColumnRef(owner_col), "=", Param(value=owner_pk)),
                        "AND",
                        BinOp(ColumnRef(target_col), "=", Param(value=item_pk)),
                    ),
                )
                sql, params = db._builder.render(stmt)
                db._execute_dml(sql, params)
        else:
            target_cls = self._resolve_target()
            from nextorm.entity import _matches_entity as _me  # noqa: PLC0415
            from nextorm.fields import RelationKind  # noqa: PLC0415

            # Find any Single back-reference (nullable or not) on the target
            back_ref = next(
                (
                    r
                    for r in target_cls._relations_.values()
                    if r.spec.kind == RelationKind.SINGLE and _me(r.spec.target, owner_cls)
                ),
                None,
            )
            if back_ref is None:
                raise RuntimeError(
                    f"Cannot remove from {self._ri.name}: no Single "
                    "back-reference found on the target entity."
                )
            for item in flat_items:
                if back_ref.spec.nullable:
                    # Nullable FK: set to NULL
                    setattr(item, back_ref.name, None)
                    db.save(item)
                else:
                    # Required FK: item cannot exist without owner — delete it
                    db.delete_instance(item)

        self._invalidate()

    def clear(self) -> None:
        """Remove all items from this collection."""
        db = self._require_db()
        owner_pk = self._owner_pk()
        owner_cls = type(self._owner)

        if self._is_m2m():
            join_table = self._join_table_name()
            owner_table = owner_cls._table_name_
            from nextorm.sql.nodes import BinOp, ColumnRef, Delete, Param  # noqa: PLC0415

            owner_col = f"{owner_table}_id"
            assert db._builder is not None
            stmt = Delete(
                table=join_table,
                where=BinOp(ColumnRef(owner_col), "=", Param(value=owner_pk)),
            )
            sql, params = db._builder.render(stmt)
            db._execute_dml(sql, params)
        else:
            target_cls = self._resolve_target()
            from nextorm.entity import _matches_entity as _me  # noqa: PLC0415
            from nextorm.fields import RelationKind  # noqa: PLC0415

            back_ref = next(
                (
                    r
                    for r in target_cls._relations_.values()
                    if r.spec.kind == RelationKind.SINGLE
                    and r.spec.nullable
                    and _me(r.spec.target, owner_cls)
                ),
                None,
            )
            if back_ref is None:
                raise RuntimeError(
                    f"Cannot clear {self._ri.name}: no nullable Single "
                    "back-reference found. Declare Single[...| None] on the target."
                )
            fk_col = f"{back_ref.name}_id"
            from nextorm.sql.nodes import BinOp, ColumnRef, Literal, Param, Update  # noqa: PLC0415

            assert db._builder is not None
            stmt_u = Update(
                table=target_cls._table_name_,
                assignments=((fk_col, Literal(None)),),
                where=BinOp(ColumnRef(fk_col), "=", Param(value=owner_pk)),
            )
            sql, params = db._builder.render(stmt_u)
            db._execute_dml(sql, params)

        self._invalidate()

    def drop_table(self, *, with_all_data: bool = False) -> None:
        """Drop the join table associated with this M2M collection.

        Only valid for many-to-many relations. For one-to-many relations, use
        the target entity's :meth:`~nextorm.entity.Entity.drop_table` method instead.

        Parameters
        ----------
        with_all_data:
            When ``False``, raises an error if the join table contains data.
            When ``True``, drops the table regardless of content.

        Raises :exc:`RuntimeError` if the relation is not M2M or if the join table
        contains data and ``with_all_data`` is ``False``.

        Example::

            # For a many-to-many relation like: items = Set[OrderItem](...)
            order.items.drop_table(with_all_data=True)
        """
        if not self._is_m2m():
            raise RuntimeError(
                f"drop_table() is only valid for many-to-many relations. "
                f"Relation {self._ri.name} is one-to-many. "
                f"Use {self._resolve_target().__name__}.drop_table() instead."
            )
        db = self._require_db()
        join_table = self._join_table_name()
        db.drop_table(join_table, if_exists=True, with_all_data=with_all_data)

    def create(self, **kwargs: Any) -> ET:
        """Create a related entity and immediately link it to this collection.

        The entity is saved to the database first (to obtain a primary key),
        then linked via :meth:`add`.  Returns the newly created entity.
        """
        db = self._require_db()
        target_cls = self._resolve_target()
        item: ET = cast("ET", target_cls(**kwargs))
        # Save the item first so it gets a primary key (required for M2M join inserts
        # and for O2M where add() will UPDATE the FK column).
        db.save(item)
        self.add(item)
        return item

    # ------------------------------------------------------------------
    # Repr
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        from nextorm.entity import _target_name  # noqa: PLC0415

        target = self._ri.spec.target
        target_label = _target_name(target) or repr(target)
        owner_repr = repr(self._owner)
        return f"RelatedCollection[{target_label}]({owner_repr}.{self._ri.name})"
