"""Entity → Table schema builder."""

from __future__ import annotations

import typing
from typing import Any

from nextorm.entity import (
    Entity,
    _derive_composite_fk_cols,
    _LazyType,
    _pk_col_for_field,
    _resolve_entity_target,
)
from nextorm.fields import RelationKind
from nextorm.schema.core import Column, ForeignKey, Index, Table

__all__ = ["build_schema", "entity_to_table"]


def _target_table_name(target: type[Any] | str | typing.ForwardRef | _LazyType | None) -> str:
    """Return the SQL table name for a relation target.

    Prefers the entity's ``_table_name_`` attribute when *target* is a concrete
    class that has one (i.e. an :class:`~nextorm.entity.Entity` subclass).
    """
    if isinstance(target, str):
        return target.lower()
    if isinstance(target, (typing.ForwardRef, _LazyType)):
        return target.__forward_arg__.lower()
    if target is None:  # pragma: no cover
        return ""
    # Use _table_name_ if the class is an Entity subclass (supports _table_ override)
    table_name_attr: str | None = getattr(target, "_table_name_", None)
    if table_name_attr is not None:
        return table_name_attr
    return target.__name__.lower()  # pragma: no cover


def _target_matches(
    target: type[Any] | str | typing.ForwardRef | _LazyType | None, entity_cls: type[Any]
) -> bool:
    """Return True if *target* refers to *entity_cls* (handles forward-ref strings/ForwardRef)."""
    if target is None:  # pragma: no cover
        return False
    if target is entity_cls:
        return True
    if isinstance(target, str):
        return target.lower() == entity_cls.__name__.lower()
    if isinstance(target, (typing.ForwardRef, _LazyType)):
        return target.__forward_arg__.lower() == entity_cls.__name__.lower()
    return False


def entity_to_table(
    entity_cls: type[Entity],
    *,
    non_owning_singles: set[tuple[str, str]] | None = None,
    is_one_to_one: set[tuple[str, str]] | None = None,
) -> Table:
    """Build a :class:`Table` from an entity class.

    - Every field in ``_fields_`` becomes a :class:`Column`.
    - Every ``Single`` relation that is the *owning* side adds a ``<field>_id``
      :class:`Column` and a :class:`ForeignKey` constraint.  The column is
      nullable and uses ``ON DELETE SET NULL`` when the relation is declared as
      ``Single[T | None]``, and NOT NULL / ``ON DELETE CASCADE`` otherwise.
    - When both entities declare ``Single`` pointing at each other (one-to-one),
      the owning side's FK column carries a ``UNIQUE`` constraint; the non-owning
      side produces no column.
    - ``Set`` relations produce no column here;
      call :func:`build_schema` to also generate M2M join tables.

    When called from :func:`build_schema` the *non_owning_singles* and
    *is_one_to_one* context sets are computed automatically.  In standalone use
    (e.g. in tests) they default to ``None``, treating all ``Single`` relations
    as owning M2O FKs without UNIQUE.
    """
    table_name = entity_cls._table_name_
    table = Table(name=table_name, entity_cls=entity_cls)

    # Names of fields that form the composite PK (may include relation names)
    composite_pk_fields: tuple[str, ...] = entity_cls._pk_fields_
    # Relation fields in the composite PK → their column is "<rel>_id"
    pk_rel_names: set[str] = {f for f in composite_pk_fields if f in entity_cls._relations_}

    # Persistent fields → columns
    for fi in entity_cls._fields_.values():
        # Scalar fields that form the composite PK directive are marked primary_key
        is_composite_pk_part = entity_cls._pk_field_ is None and fi.name in composite_pk_fields
        col = Column(
            name=fi.spec.column or fi.name,
            py_type=fi.py_type,
            nullable=fi.spec.nullable,
            primary_key=fi.spec.primary_key or is_composite_pk_part,
            auto_increment=fi.spec.auto and fi.spec.primary_key,
            unique=fi.spec.unique,
            index=fi.spec.index,
            max_len=fi.spec.max_len,
            sql_default=fi.spec.sql_default,
            sql_type_override=fi.spec.sql_type,
            precision=fi.spec.precision,
            scale=fi.spec.scale,
            unsigned=fi.spec.unsigned,
            size=fi.spec.size,
            dimensions=fi.spec.dimensions,
        )
        table.columns.append(col)
        # Standalone index (not already implied by PRIMARY KEY or UNIQUE)
        if fi.spec.index and not fi.spec.primary_key and not fi.spec.unique:
            table.indexes.append(
                Index(
                    name=f"idx_{table_name}__{col.name}",
                    columns=[col.name],
                )
            )

    # Single relations → owning side creates FK column
    for ri in entity_cls._relations_.values():
        if ri.spec.kind != RelationKind.SINGLE:
            continue
        # Skip non-owning back-ref side of a one-to-one pair
        if non_owning_singles and (table_name, ri.name) in non_owning_singles:
            continue
        # Resolve target entity to check for composite PK
        resolved_target = _resolve_entity_target(ri.spec.target)
        target_pk_fields: tuple[str, ...] = (
            getattr(resolved_target, "_pk_fields_", ()) if resolved_target is not None else ()
        )
        is_composite_target = len(target_pk_fields) > 1
        # Support columns (composite FK) or column (single FK)
        if ri.spec.columns:
            col_names = ri.spec.columns
            # user-specified columns; assume simple refs
            ref_cols: list[str] = ["id"] * len(col_names)
        elif is_composite_target and resolved_target is not None:
            # Auto-derive multi-column FK for composite-PK target
            col_names = _derive_composite_fk_cols(ri.name, resolved_target)
            # Derive corresponding reference column names in the target table
            ref_cols = []
            for fname in target_pk_fields:
                t_fields = getattr(resolved_target, "_fields_", {})
                t_relations = getattr(resolved_target, "_relations_", {})
                if fname in t_fields:
                    ref_cols.append(t_fields[fname].spec.column or fname)
                elif fname in t_relations:
                    ref_cols.append(t_relations[fname].spec.column or f"{fname}_id")
                else:  # pragma: no cover — PK field must be a field or relation
                    ref_cols.append(f"{fname}_id")
        else:
            col_names = [ri.spec.column or f"{ri.name}_id"]
            # Determine the actual PK column on the target table (may not be "id").
            if resolved_target is not None and target_pk_fields:
                pk_fname = target_pk_fields[0]
                t_fields = getattr(resolved_target, "_fields_", {})
                t_relations = getattr(resolved_target, "_relations_", {})
                if pk_fname in t_fields:
                    _ref_col: str = t_fields[pk_fname].spec.column or pk_fname
                elif pk_fname in t_relations:
                    _ref_col = t_relations[pk_fname].spec.column or f"{pk_fname}_id"
                else:  # pragma: no cover — PK field must be a field or relation
                    _ref_col = pk_fname
                ref_cols = [_ref_col]
            else:
                ref_cols = ["id"]
        ref_table = _target_table_name(ri.spec.target)
        nullable: bool = ri.spec.nullable
        # Derive ON DELETE action: explicit override > nullability default
        if ri.spec.cascade_delete is True:
            on_delete = "CASCADE"
        elif ri.spec.cascade_delete is False:
            on_delete = "RESTRICT"
        else:  # None — auto-derive
            on_delete = "SET NULL" if nullable else "CASCADE"
        # One-to-one owning side: add UNIQUE on the FK column
        unique = is_one_to_one is not None and (table_name, ri.name) in is_one_to_one
        # If this relation is part of the composite PK, mark the FK column as primary_key
        is_pk_part = ri.name in pk_rel_names
        for col_name, ref_col in zip(col_names, ref_cols, strict=True):
            fk_col = Column(
                name=col_name,
                py_type=int,
                nullable=nullable,
                unique=unique,
                primary_key=is_pk_part,
            )
            table.columns.append(fk_col)
            fk_name = ri.spec.fk_name or f"fk_{table_name}__{col_name}"
            table.foreign_keys.append(
                ForeignKey(
                    name=fk_name,
                    column=col_name,
                    ref_table=ref_table,
                    ref_column=ref_col,
                    on_delete=on_delete,
                )
            )

    # Composite key/index constraints declared with composite_key() / composite_index()
    for constraint in entity_cls._constraints_:
        cols = "__".join(constraint.fields)
        prefix = "unq" if constraint.unique else "idx"
        table.indexes.append(
            Index(
                name=f"{prefix}_{table_name}__{cols}",
                columns=list(constraint.fields),
                unique=constraint.unique,
            )
        )

    return table


def _resolve_target_cls(
    target: Any,
    entities: list[type[Entity]],
) -> type[Entity] | None:
    """Resolve a relation target to an entity class from *entities*."""
    if isinstance(target, str):
        return next((e for e in entities if e.__name__.lower() == target.lower()), None)
    if isinstance(target, (typing.ForwardRef, _LazyType)):
        name = target.__forward_arg__.lower()
        return next((e for e in entities if e.__name__.lower() == name), None)
    return target if target in entities else None


def build_schema(entities: list[type[Entity]]) -> dict[str, Table]:
    """Build the complete schema for a set of entities.

    Returns a ``{table_name: Table}`` mapping that includes the entity tables
    **and** any auto-generated many-to-many join tables.

    M2M is inferred when **both** entities carry a ``Set[Other]`` relation
    pointing at each other.  A ``Set`` on only one side is treated as the
    collection side of a one-to-many relationship and produces no join table.

    O2O is inferred when **both** entities carry a ``Single`` relation pointing
    at each other.  The non-nullable (required) ``Single`` side is the owning
    side and receives the FK column with a ``UNIQUE`` constraint; the nullable
    side is the back-reference and produces no column.  When both sides share
    the same nullability the alphabetically lesser table name is the owner.
    """
    # ------------------------------------------------------------------
    # Pre-pass: determine non-owning back-refs and o2o owners for Single
    # ------------------------------------------------------------------
    non_owning_singles: set[tuple[str, str]] = set()
    is_one_to_one: set[tuple[str, str]] = set()
    seen_o2o: set[frozenset[tuple[str, str]]] = set()
    seen_single_set_pairs: set[tuple[str, str, str, str]] = set()

    for entity_cls in entities:
        for ri in entity_cls._relations_.values():
            if ri.spec.kind != RelationKind.SINGLE:
                continue
            target_cls = _resolve_target_cls(ri.spec.target, entities)
            if target_cls is None:
                continue

            # Check if this Single has an explicit reverse parameter
            # If so, look for a matching Set or Single on the target entity
            reverse_name = getattr(ri.spec, "reverse", None)
            if reverse_name:
                back_ri = target_cls._relations_.get(reverse_name)
                if back_ri:
                    # Case 1: Single-Set pair (Many-to-One with collection)
                    if back_ri.spec.kind == RelationKind.SET and _target_matches(
                        back_ri.spec.target, entity_cls
                    ):
                        # Explicit Single-Set pair found via reverse parameter
                        pair_key = (
                            entity_cls._table_name_,
                            ri.name,
                            target_cls._table_name_,
                            back_ri.name,
                        )
                        if pair_key not in seen_single_set_pairs:  # pragma: no branch
                            seen_single_set_pairs.add(pair_key)
                            # Single is owning side of M2O; Set is non-owning collection
                            # Do NOT add to is_one_to_one - this is M2O, not O2O
                        continue

                    # Case 2: Single-Single pair with mutual reverse declarations (O2O)
                    if back_ri.spec.kind == RelationKind.SINGLE and _target_matches(
                        back_ri.spec.target, entity_cls
                    ):
                        back_reverse = getattr(back_ri.spec, "reverse", None)
                        # Both sides declare reverse pointing at each other
                        if back_reverse == ri.name:
                            pair = frozenset(
                                {
                                    (entity_cls._table_name_, ri.name),
                                    (target_cls._table_name_, back_ri.name),
                                }
                            )
                            if pair not in seen_o2o:
                                seen_o2o.add(pair)

                                a_name, a_ri = entity_cls._table_name_, ri
                                b_name, b_ri = target_cls._table_name_, back_ri

                                # Owning-side rule: explicit owner parameter or nullability
                                if a_ri.spec.owner is True or b_ri.spec.owner is False:
                                    owner, owner_ri = a_name, a_ri
                                    non_owner, non_owner_ri = b_name, b_ri
                                elif b_ri.spec.owner is True or a_ri.spec.owner is False:
                                    owner, owner_ri = b_name, b_ri
                                    non_owner, non_owner_ri = a_name, a_ri
                                elif not a_ri.spec.nullable and b_ri.spec.nullable:
                                    owner, owner_ri = a_name, a_ri
                                    non_owner, non_owner_ri = b_name, b_ri
                                elif a_ri.spec.nullable and not b_ri.spec.nullable:
                                    owner, owner_ri = b_name, b_ri
                                    non_owner, non_owner_ri = a_name, a_ri
                                elif a_name <= b_name:
                                    owner, owner_ri = a_name, a_ri
                                    non_owner, non_owner_ri = b_name, b_ri
                                else:
                                    owner, owner_ri = b_name, b_ri
                                    non_owner, non_owner_ri = a_name, a_ri

                                is_one_to_one.add((owner, owner_ri.name))
                                non_owning_singles.add((non_owner, non_owner_ri.name))
                            continue

            # Look for a matching Single on the other side pointing back
            back_ri = next(
                (
                    r
                    for r in target_cls._relations_.values()
                    if r.spec.kind == RelationKind.SINGLE
                    and _target_matches(r.spec.target, entity_cls)
                ),
                None,
            )
            if back_ri is None:
                continue  # plain Many-to-One; this entity is the owner

            # O2O pair detected — process each pair only once
            pair = frozenset(
                {
                    (entity_cls._table_name_, ri.name),
                    (target_cls._table_name_, back_ri.name),
                }
            )
            if pair in seen_o2o:
                continue
            seen_o2o.add(pair)

            a_name, a_ri = entity_cls._table_name_, ri
            b_name, b_ri = target_cls._table_name_, back_ri

            # Owning-side rule (highest to lowest priority):
            #   1. Explicit owner=True / owner=False on either side.
            #   2. Required (non-nullable) beats optional (nullable).
            #   3. Alphabetically lesser table name is the owner.
            if a_ri.spec.owner is True or b_ri.spec.owner is False:
                owner, owner_ri = a_name, a_ri
                non_owner, non_owner_ri = b_name, b_ri
            elif b_ri.spec.owner is True or a_ri.spec.owner is False:
                owner, owner_ri = b_name, b_ri
                non_owner, non_owner_ri = a_name, a_ri
            elif not a_ri.spec.nullable and b_ri.spec.nullable:
                owner, owner_ri = a_name, a_ri
                non_owner, non_owner_ri = b_name, b_ri
            elif a_ri.spec.nullable and not b_ri.spec.nullable:
                owner, owner_ri = b_name, b_ri
                non_owner, non_owner_ri = a_name, a_ri
            elif a_name <= b_name:
                owner, owner_ri = a_name, a_ri
                non_owner, non_owner_ri = b_name, b_ri
            else:
                owner, owner_ri = b_name, b_ri
                non_owner, non_owner_ri = a_name, a_ri

            is_one_to_one.add((owner, owner_ri.name))
            non_owning_singles.add((non_owner, non_owner_ri.name))

    # ------------------------------------------------------------------
    # Build entity tables
    # ------------------------------------------------------------------
    # STI: identify child entities (they skip table creation and instead
    # contribute nullable columns to the parent's table).
    sti_child_set: set[type[Entity]] = set()
    sti_children_by_parent: dict[str, list[type[Entity]]] = {}
    for entity_cls in entities:
        parent = getattr(entity_cls, "_sti_parent_", None)
        if parent is not None:
            sti_child_set.add(entity_cls)
            sti_children_by_parent.setdefault(parent._table_name_, []).append(entity_cls)

    tables: dict[str, Table] = {}
    for entity_cls in entities:
        # STI children share the parent table — skip them here
        if entity_cls in sti_child_set:
            continue
        table = entity_to_table(
            entity_cls,
            non_owning_singles=non_owning_singles,
            is_one_to_one=is_one_to_one,
        )
        # For STI parent entities, inject the discriminator column + child-only columns
        disc_col = getattr(entity_cls, "_discriminator_col_", None)
        if disc_col is not None:
            children = sti_children_by_parent.get(entity_cls._table_name_, [])
            if children:
                table.columns.append(Column(name=disc_col, py_type=str, nullable=True))
                # Add child-only fields as nullable columns
                existing_col_names: set[str] = {c.name for c in table.columns}
                for child_cls in children:
                    for fi in child_cls._fields_.values():
                        col_name = fi.spec.column or fi.name
                        if col_name not in existing_col_names:
                            existing_col_names.add(col_name)
                            table.columns.append(
                                Column(
                                    name=col_name,
                                    py_type=fi.py_type,
                                    nullable=True,
                                    max_len=fi.spec.max_len,
                                    sql_default=fi.spec.sql_default,
                                    sql_type_override=fi.spec.sql_type,
                                    precision=fi.spec.precision,
                                    scale=fi.spec.scale,
                                    unsigned=fi.spec.unsigned,
                                    size=fi.spec.size,
                                    dimensions=fi.spec.dimensions,
                                )
                            )
        tables[table.name] = table

    # ------------------------------------------------------------------
    # Generate M2M join tables (deduplicating bidirectional pairs).
    # M2M is detected when both entities declare Set pointing at each other.
    # ------------------------------------------------------------------
    seen: set[frozenset[str]] = set()
    for entity_cls in entities:
        for ri in entity_cls._relations_.values():
            if ri.spec.kind != RelationKind.SET:
                continue
            table_a = entity_cls._table_name_
            target = ri.spec.target
            target_cls = _resolve_target_cls(target, entities)
            if target_cls is None:
                continue
            # Only generate a join table when the target also has Set pointing back
            is_m2m = any(
                r.spec.kind == RelationKind.SET and _target_matches(r.spec.target, entity_cls)
                for r in target_cls._relations_.values()
            )
            if not is_m2m:
                continue
            table_b = target_cls._table_name_
            m2m_pair = frozenset({table_a, table_b})
            if m2m_pair in seen:
                continue
            seen.add(m2m_pair)
            # Allow explicit join table name via RelationSpec.table
            join_name = ri.spec.table or "_".join(sorted(m2m_pair))
            # Resolve actual PK column names for each side
            pk_fields_a = entity_cls._pk_fields_
            pk_fields_b = target_cls._pk_fields_
            ref_col_a = _pk_col_for_field(entity_cls, pk_fields_a[0]) if pk_fields_a else "id"
            ref_col_b = _pk_col_for_field(target_cls, pk_fields_b[0]) if pk_fields_b else "id"
            # Use reverse/reverse_column/reverse_columns if present for join columns
            # Prefer explicit columns, else default to <table>_id
            col_a = None
            col_b = None
            # ri is the Set on entity_cls (table_a)
            if getattr(ri.spec, "reverse_column", None):
                col_b = ri.spec.reverse_column
            elif getattr(ri.spec, "reverse_columns", None):
                rcols = ri.spec.reverse_columns
                col_b = rcols[0] if isinstance(rcols, (list, tuple)) and rcols else None
            if getattr(ri.spec, "column", None):
                col_a = ri.spec.column
            elif getattr(ri.spec, "columns", None):
                cols = ri.spec.columns
                col_a = cols[0] if isinstance(cols, (list, tuple)) and cols else None
            if not col_a:
                col_a = f"{table_a}_id"
            if not col_b:
                col_b = f"{table_b}_id"
            tables[join_name] = Table(
                name=join_name,
                columns=[
                    Column(name=col_a, py_type=int, nullable=False),
                    Column(name=col_b, py_type=int, nullable=False),
                ],
                foreign_keys=[
                    ForeignKey(
                        name=f"fk_{join_name}__{col_a}",
                        column=col_a,
                        ref_table=table_a,
                        ref_column=ref_col_a,
                        on_delete="CASCADE",
                    ),
                    ForeignKey(
                        name=f"fk_{join_name}__{col_b}",
                        column=col_b,
                        ref_table=table_b,
                        ref_column=ref_col_b,
                        on_delete="CASCADE",
                    ),
                ],
            )

    return tables
