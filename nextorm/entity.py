"""EntityMeta metaclass and Entity base class."""

from __future__ import annotations

import dataclasses
import enum as _enum_lib
import inspect
import types
from collections.abc import Iterator  # noqa: TC003
from typing import TYPE_CHECKING, Any, ClassVar, Self, cast, overload

from nextorm.fields import (
    _UUID_SENTINEL_MAP,
    CompositeConstraint,
    FieldSpec,
    LongStr,
    RelationKind,
    RelationSpec,
    Vec,
)
from nextorm.fields import (
    PK as _PK,
)
from nextorm.fields import (
    Local as _Local,
)
from nextorm.fields import (
    Opt as _Opt,
)
from nextorm.fields import (
    Req as _Req,
)
from nextorm.fields import (
    Set as _Set,
)
from nextorm.fields import (
    Single as _Single,
)

if TYPE_CHECKING:
    from nextorm.async_database import AsyncDatabase
    from nextorm.database import Database
    from nextorm.expr import ColumnExpr

__all__ = [
    "Entity",
    "EntityMeta",
    "_resolve_entity_target",
    "_matches_entity",
    "_LAZY_SENTINEL",
    "_find_db_for_entity",
]

# Sentinel stored in instance.__dict__ for lazy fields that haven't been loaded yet.
# FieldDescriptor.__get__ detects this and fires a per-field SELECT.
_LAZY_SENTINEL: object = object()

# ---------------------------------------------------------------------------
# Alias → FieldSpec mapping  (keyed by TypeAliasType identity)
# ---------------------------------------------------------------------------
# Each entry maps a TypeAliasType object (e.g. ``Req``) to the FieldSpec that
# EntityMeta should create for annotations using that alias.

_FIELD_ALIAS_SPECS: dict[object, FieldSpec] = {
    _PK: FieldSpec(primary_key=True, auto=True),
    _Req: FieldSpec(),
    _Opt: FieldSpec(nullable=True),
}

# Sentinel used by SingleDescriptor to distinguish "not yet loaded" from None
_UNSET: object = object()

_RELATION_ALIAS_KINDS: dict[object, str] = {
    _Set: RelationKind.SET,
    _Single: RelationKind.SINGLE,
}


# ---------------------------------------------------------------------------
# Field descriptors stored on the class after metaclass processing
# ---------------------------------------------------------------------------


class FieldDescriptor:
    """Runtime descriptor for a persistent field."""

    def __init__(self, name: str, py_type: type, spec: FieldSpec) -> None:
        self.name = name
        self._py_type = py_type
        self.spec = spec
        self._attr = f"_field_{name}"

    @overload
    def __get__(self, obj: None, objtype: type) -> ColumnExpr: ...

    @overload
    def __get__(self, obj: Any, objtype: type | None) -> Any: ...

    def __get__(self, obj: Any, objtype: type | None = None) -> Any:
        if obj is None:
            # Class-level access → return a ColumnExpr for query building.
            # Import at call time to keep the module-level import under TYPE_CHECKING.
            from nextorm.expr import ColumnExpr as _ColumnExpr  # noqa: PLC0415

            table_name: str | None = getattr(objtype, "_table_name_", None)
            return _ColumnExpr(self.name, table_name)
        val = obj.__dict__.get(self._attr)
        if val is _LAZY_SENTINEL:
            db = obj.__dict__.get("_db_")
            if db is None:
                raise RuntimeError(
                    f"Cannot lazy-load '{self.name}': entity has no attached database context. "
                    "Load the entity via db.select(...) or save it first."
                )
            if getattr(db, "_is_async", False):
                raise RuntimeError(
                    f"Cannot synchronously lazy-load '{self.name}' from an async context. "
                    "Use 'await db.load_lazy_field(entity, field_name)' instead."
                )
            loaded = db._load_lazy_field(obj, self.name)
            obj.__dict__[self._attr] = loaded
            # Track lazy-field read: update dbvals and mark column as read.
            col = self.spec.column or self.name
            dbvals: dict[str, Any] | None = obj.__dict__.get("_dbvals_")
            if dbvals is not None:
                dbvals[col] = loaded
            read_cols: set[str] | None = obj.__dict__.get("_read_cols_")
            if read_cols is not None:
                read_cols.add(col)
            return loaded
        # Track read for per-field optimistic concurrency check.
        read_cols = obj.__dict__.get("_read_cols_")
        if read_cols is not None:
            read_cols.add(self.spec.column or self.name)
        return val

    def __set__(self, obj: Any, value: Any) -> None:
        spec = self.spec
        if value is not None and value is not _LAZY_SENTINEL:
            # enum coercion: convert raw DB primitives (str / int) to the declared Enum type
            if issubclass(self._py_type, _enum_lib.Enum) and not isinstance(value, self._py_type):
                raw = value.value if isinstance(value, _enum_lib.Enum) else value
                value = self._py_type(raw)
            # autostrip: strip whitespace from string values
            if spec.autostrip and isinstance(value, str):
                value = value.strip()
            # min / max range validation
            if spec.min is not None and value < spec.min:
                raise ValueError(
                    f"Value {value!r} for field '{self.name}' is below the minimum {spec.min!r}."
                )
            if spec.max is not None and value > spec.max:
                raise ValueError(
                    f"Value {value!r} for field '{self.name}' exceeds the maximum {spec.max!r}."
                )
            # py_check: user-supplied callable validator
            if spec.py_check is not None and not spec.py_check(value):
                raise ValueError(f"py_check failed for field '{self.name}' with value {value!r}.")
        obj.__dict__[self._attr] = value
        # Auto dirty-tracking: mark the entity dirty in the active session when it has
        # already been persisted (_dbvals_ is set by _do_insert / _map_row).
        if obj.__dict__.get("_dbvals_") is not None:
            from nextorm.session import _get_session_stack  # noqa: PLC0415

            cache = _get_session_stack().current
            if cache is not None:
                cache.mark_dirty(obj)

    def __delete__(self, obj: Any) -> None:
        obj.__dict__.pop(self._attr, None)


class LocalDescriptor:
    """Runtime descriptor for a local (transient) field.

    Accessing this on the class returns the descriptor itself; on an instance
    it reads/writes directly from ``instance.__dict__``.  It never touches
    the database.
    """

    _MISSING: Any = object()

    def __init__(self, name: str) -> None:
        self.name = name

    def __get__(self, obj: Any, objtype: type | None = None) -> Any:
        if obj is None:
            return self
        try:
            return obj.__dict__[self.name]
        except KeyError:
            raise AttributeError(
                f"Local attribute '{self.name}' has not been set yet. "
                "Initialise it in after_load() / after_insert()."
            ) from None

    def __set__(self, obj: Any, value: Any) -> None:
        obj.__dict__[self.name] = value

    def __delete__(self, obj: Any) -> None:
        obj.__dict__.pop(self.name, None)


class SingleDescriptor:
    """Runtime descriptor for a ``Single`` relation.

    - Class-level access → :class:`~nextorm.expr.ColumnExpr` for the FK column.
    - Instance-level access → lazily loads and caches the related entity.

    The FK integer value is stored in ``instance.__dict__`` under the key
    ``'_<name>_id'``.  The loaded entity object is cached under ``'_<name>_obj'``.
    Setting the attribute accepts either an entity instance or ``None``.
    """

    def __init__(self, name: str, ri: RelationInfo) -> None:  # noqa: F821
        self.name = name
        self.ri = ri
        self._fk_col = ri.spec.column or f"{name}_id"  # SQL column name (overridable)
        self._fk_key = f"_{name}_id"  # Python __dict__ storage key (always attr-based)
        self._obj_key = f"_{name}_obj"

    def __get__(self, obj: Any, objtype: type | None = None) -> Any:
        if obj is None:
            # Class-level: return a ColumnExpr for the FK column
            from nextorm.expr import ColumnExpr as _CE  # noqa: PLC0415

            table_name: str | None = getattr(objtype, "_table_name_", None)
            return _CE(self._fk_col, table_name)
        # Track FK column read for per-field optimistic concurrency check.
        read_cols: set[str] | None = obj.__dict__.get("_read_cols_")
        if read_cols is not None:
            read_cols.add(self._fk_col)
        # Cached object takes priority
        loaded = obj.__dict__.get(self._obj_key, _UNSET)
        if loaded is not _UNSET:
            return loaded
        # Try FK id → lazy load
        fk_id = obj.__dict__.get(self._fk_key)
        if fk_id is None:
            return None
        db = obj.__dict__.get("_db_")
        if db is None:
            raise RuntimeError(
                f"Cannot lazy-load '{self.name}': entity was not loaded from a Database. "
                "Call db.select(...).fetch_all() or db.save() to attach a database context."
            )
        target_spec = self.ri.spec.target
        entity_cls = _resolve_entity_target(target_spec)
        if entity_cls is None:
            raise RuntimeError(
                f"Cannot lazy-load '{self.name}': target {target_spec!r} is an "
                "unresolved forward reference."
            )
        pk_fields_target = entity_cls._pk_fields_
        assert pk_fields_target, f"Target entity {entity_cls.__name__!r} has no primary key"
        pk_field = pk_fields_target[0]
        pk_col = entity_cls._fields_[pk_field].spec.column or pk_field
        from nextorm.sql.nodes import BinOp as _BinOp  # noqa: PLC0415
        from nextorm.sql.nodes import ColumnRef as _CR  # noqa: PLC0415
        from nextorm.sql.nodes import Param as _P  # noqa: PLC0415

        cond = _BinOp(_CR(pk_col), "=", _P(value=fk_id))
        result = db.select(entity_cls).filter(cond).fetch_one()
        obj.__dict__[self._obj_key] = result
        return result

    def __set__(self, obj: Any, value: Any) -> None:
        if value is None:
            obj.__dict__[self._obj_key] = None
            obj.__dict__[self._fk_key] = None
        elif isinstance(value, int):
            # Direct FK id assignment (used during row mapping)
            obj.__dict__[self._fk_key] = value
            obj.__dict__.pop(self._obj_key, None)
        else:
            # Related entity instance — also cache the FK id
            obj.__dict__[self._obj_key] = value
            value_cls = cast("EntityMeta", type(value))
            pk_fields = value_cls._pk_fields_
            pk = getattr(value, pk_fields[0]) if pk_fields else None
            obj.__dict__[self._fk_key] = pk

    def __delete__(self, obj: Any) -> None:
        obj.__dict__.pop(self._fk_key, None)
        obj.__dict__.pop(self._obj_key, None)


class SetDescriptor:
    """Runtime descriptor for a ``Set[T]`` relation.

    - Class-level access → the descriptor itself (for schema introspection).
    - Instance-level access → a :class:`~nextorm.collection.RelatedCollection`.

    The collection is created lazily and cached in ``instance.__dict__``
    under the key ``'_<name>_col'``.
    """

    def __init__(self, name: str, ri: RelationInfo) -> None:  # noqa: F821
        self.name = name
        self.ri = ri
        self._cache_key = f"_{name}_col"

    def __get__(self, obj: Any, objtype: type | None = None) -> Any:
        if obj is None:
            return self
        from nextorm.collection import RelatedCollection  # noqa: PLC0415

        cached: RelatedCollection[Any] | None = obj.__dict__.get(self._cache_key)
        if cached is None:
            db = obj.__dict__.get("_db_")
            cached = RelatedCollection(obj, self.ri, db)
            obj.__dict__[self._cache_key] = cached
        return cached

    def __set__(self, obj: Any, value: Any) -> None:
        obj.__dict__[self._cache_key] = value

    def __delete__(self, obj: Any) -> None:
        obj.__dict__.pop(self._cache_key, None)


# ---------------------------------------------------------------------------
# Generator-syntax support
# ---------------------------------------------------------------------------


class _EntityIterator:
    """A one-item iterator that holds a reference to the entity class.

    When :class:`EntityMeta` implements ``__iter__``, it returns one of
    these.  The generator expression ``(p for p in Product if p.price > 0)``
    calls ``iter(Product)`` at creation time, receiving this iterator.
    The :func:`~nextorm.generators.select` function reads ``iterator.entity_cls``
    to discover the entity being queried, then decompiles the generator's
    bytecode for the filter condition.
    """

    __slots__ = ("entity_cls",)

    def __init__(self, cls: EntityMeta) -> None:
        self.entity_cls: EntityMeta = cls

    def __iter__(self) -> _EntityIterator:
        return self

    def __next__(self) -> Any:
        raise StopIteration


# ---------------------------------------------------------------------------
# Forward-reference resolution helpers
# ---------------------------------------------------------------------------


def _target_name(target: Any) -> str | None:
    """Return the lower-case entity name for *target* (str / ForwardRef / class)."""
    import typing  # noqa: PLC0415

    if isinstance(target, str):
        return target.lower()
    if isinstance(target, typing.ForwardRef):
        return target.__forward_arg__.lower()
    if isinstance(target, type):
        return target.__name__.lower()
    return None


def _resolve_entity_target(target: Any) -> EntityMeta | None:
    """Resolve *target* to the entity class or ``None`` if unresolvable.

    Accepts a concrete class, a plain string name, or a :class:`typing.ForwardRef`.
    """
    if isinstance(target, type) and isinstance(target, EntityMeta):
        return target
    name = _target_name(target)
    if name is None:
        return None
    return next(
        (cls for cls in _entity_registry if cls.__name__.lower() == name),
        None,
    )


def _matches_entity(target: Any, cls: type) -> bool:
    """Return ``True`` when *target* refers to entity class *cls*.

    Handles plain class identity, string name comparison, and
    :class:`typing.ForwardRef` (Python's lazy annotation mechanism).
    """
    if target is cls:
        return True
    name = _target_name(target)
    return name is not None and name == cls.__name__.lower()


# ---------------------------------------------------------------------------
# Resolved field metadata (attached to cls._fields_ / cls._locals_)
# ---------------------------------------------------------------------------


class FieldInfo:
    """Resolved information about a single persistent field."""

    __slots__ = ("name", "py_type", "spec")

    def __init__(self, name: str, py_type: type, spec: FieldSpec) -> None:
        self.name = name
        self.py_type = py_type
        self.spec = spec

    def __repr__(self) -> str:
        return f"FieldInfo({self.name!r}, {self.py_type.__name__}, {self.spec!r})"


class RelationInfo:
    """Resolved information about a relation field."""

    __slots__ = ("name", "spec")

    def __init__(self, name: str, spec: RelationSpec) -> None:
        self.name = name
        self.spec = spec

    def __repr__(self) -> str:
        return f"RelationInfo({self.name!r}, {self.spec!r})"


# ---------------------------------------------------------------------------
# EntityMeta
# ---------------------------------------------------------------------------

# Global registry — every Entity subclass registers itself here so Database
# can discover them without explicit registration.
_entity_registry: set[EntityMeta] = set()


def _find_db_for_entity(entity_cls: type) -> Any:
    """Return the first :class:`~nextorm.database.Database` mapped for *entity_cls*.

    Looks up the global database registry populated by
    :meth:`~nextorm.database.Database.generate_mapping`.  Raises
    :exc:`RuntimeError` when no mapped database can be found.
    """
    from nextorm.database import _database_registry  # noqa: PLC0415

    table_name: str = getattr(entity_cls, "_table_name_", entity_cls.__name__.lower())
    for db in _database_registry:
        if table_name in getattr(db, "_schema", {}):
            return db
    raise RuntimeError(
        f"Cannot find a mapped Database for entity {entity_cls.__name__!r}."
        " Call db.generate_mapping() with this entity first."
    )


class EntityMeta(type):
    """Metaclass that processes ``Annotated`` field specs at class definition time."""

    _fields_: dict[str, FieldInfo]
    _relations_: dict[str, RelationInfo]
    _locals_: set[str]
    _pk_fields_: tuple[str, ...]  # all PK attribute names (scalar or relation)
    _pk_field_: str | None  # first (and only) PK field name for single-PK; None for composite
    _table_name_: str  # SQL table name; defaults to class name lower-cased
    _constraints_: list[CompositeConstraint]  # composite unique/index constraints
    # Single-table inheritance (STI) support
    _discriminator_col_: str | None  # column name for the discriminator (set on STI parent)
    _discriminator_val_: (
        str | None
    )  # value identifying this class in the disc column (set on STI children)
    _sti_parent_: EntityMeta | None  # parent entity class for STI children; None otherwise
    id: _PK[int]

    def __new__(
        mcs,
        name: str,
        bases: tuple[type, ...],
        namespace: dict[str, Any],
        **kwargs: Any,
    ) -> EntityMeta:
        cls = super().__new__(mcs, name, bases, namespace, **kwargs)

        # Skip the bare Entity base itself
        if not any(isinstance(b, EntityMeta) for b in bases):
            return cls

        fields: dict[str, FieldInfo] = {}
        relations: dict[str, RelationInfo] = {}
        local_attrs: set[str] = set()

        # Collect annotations from the full MRO (excluding Entity itself and object)
        all_annotations: dict[str, Any] = {}
        for klass in reversed(cls.__mro__):
            if klass is object:
                continue
            if not isinstance(klass, EntityMeta):
                continue
            if klass.__name__ == "Entity" and not any(
                isinstance(b, EntityMeta) for b in klass.__bases__
            ):
                # Skip the bare Entity base — its annotations are type-checker
                # helpers only and should not be treated as field declarations.
                continue
            all_annotations.update(inspect.get_annotations(klass, eval_str=True))

        for attr_name, annotation in all_annotations.items():
            if attr_name.startswith("_") and not attr_name.startswith("__"):
                # Allow _private_ virtual fields
                pass
            elif attr_name.startswith("__"):
                continue

            # Each nextorm alias produces a _GenericAlias whose .__origin__ is the
            # TypeAliasType object itself.  Plain annotations (int, str, Annotated[…]
            # with non-nextorm metadata, etc.) are silently ignored.
            origin = getattr(annotation, "__origin__", None)
            args: tuple[Any, ...] = getattr(annotation, "__args__", ())
            inner_type: type = args[0] if args else type(None)

            if origin is _Local:
                local_attrs.add(attr_name)
                setattr(cls, attr_name, LocalDescriptor(attr_name))
            elif origin in _RELATION_ALIAS_KINDS:
                kind = _RELATION_ALIAS_KINDS[origin]
                if kind == RelationKind.SINGLE:
                    # Detect Single[T | None] → nullable FK
                    raw_target = args[0] if args else type(None)
                    if isinstance(raw_target, types.UnionType):
                        non_none = [a for a in raw_target.__args__ if a is not type(None)]
                        nullable = len(non_none) < len(raw_target.__args__)
                        target: type | str = non_none[0] if non_none else type(None)
                    else:
                        nullable = False
                        target = raw_target
                    # Allow a RelationSpec class-level value to customise the relation
                    # (e.g. ``profile: Single[Profile] = RelationSpec(owner=True)``).
                    rel_class_val = namespace.get(attr_name)
                    if isinstance(rel_class_val, RelationSpec):
                        rel_spec = dataclasses.replace(
                            rel_class_val,
                            kind=kind,
                            target=target,
                            nullable=nullable,
                        )
                    else:
                        rel_spec = RelationSpec(kind=kind, target=target, nullable=nullable)
                    ri = RelationInfo(attr_name, rel_spec)
                    relations[attr_name] = ri
                    setattr(cls, attr_name, SingleDescriptor(attr_name, ri))
                else:
                    target = args[0] if args else attr_name
                    ri = RelationInfo(attr_name, RelationSpec(kind=kind, target=target))
                    relations[attr_name] = ri
                    setattr(cls, attr_name, SetDescriptor(attr_name, ri))
            elif origin in _FIELD_ALIAS_SPECS:
                base_spec = _FIELD_ALIAS_SPECS[origin]
                # Allow the user to supply a FieldSpec as the attribute value to
                # customise the field (e.g. ``score: Req[int] = FieldSpec(default=0)``).
                # The alias provides primary_key and nullable; everything else may
                # be overridden by the user-supplied spec.
                class_val = namespace.get(attr_name)
                if isinstance(class_val, FieldSpec):
                    spec = dataclasses.replace(
                        class_val,
                        primary_key=base_spec.primary_key,
                        auto=class_val.auto or base_spec.auto,
                        nullable=base_spec.nullable,
                    )
                else:
                    spec = base_spec
                # Handle UUID / ULID sentinel types (e.g. PK[uuid7], PK[ulid]):
                # replace the sentinel with the real storage type and mark the
                # field for Python-side auto-generation (auto=False — no DB sequence).
                actual_inner_type: type = inner_type
                if inner_type in _UUID_SENTINEL_MAP:
                    storage_type, auto_kind = _UUID_SENTINEL_MAP[inner_type]
                    actual_inner_type = storage_type
                    spec = dataclasses.replace(spec, auto=False, uuid_auto=auto_kind)
                elif inner_type is not Vec and (issubclass(inner_type, Vec)):
                    # Vec[384] produces a dynamic Vec subclass carrying _dimensions_.
                    # Normalise to the base Vec type and store the dimension in FieldSpec.
                    dims: int | None = getattr(inner_type, "_dimensions_", None)
                    actual_inner_type = Vec
                    if dims is not None:
                        spec = dataclasses.replace(spec, dimensions=dims)
                # Auto-apply lazy=True for LongStr when the user did NOT provide an
                # explicit FieldSpec (they just wrote ``name: Req[LongStr]``).
                # If the user did provide a FieldSpec they control lazy themselves.
                if (
                    actual_inner_type is LongStr
                    and not isinstance(class_val, FieldSpec)
                    and not spec.lazy
                ):
                    spec = dataclasses.replace(spec, lazy=True)
                # Lazy fields cannot be primary keys
                if spec.lazy and spec.primary_key:
                    raise TypeError(f"Field '{attr_name}' cannot be both lazy and a primary key.")
                fields[attr_name] = FieldInfo(attr_name, actual_inner_type, spec)
                setattr(cls, attr_name, FieldDescriptor(attr_name, actual_inner_type, spec))

        # Check for a PrimaryKey() composite directive before injecting auto-pk
        _composite_pk_directive = next(
            (v for v in namespace.values() if isinstance(v, CompositeConstraint) and v.primary_key),
            None,
        )

        # Auto-add `id: PK[int]` if no primary key declared anywhere (and no PrimaryKey() directive)
        pk_fields = [f for f in fields.values() if f.spec.primary_key]
        if not pk_fields and _composite_pk_directive is None:
            auto_pk = FieldInfo("id", int, FieldSpec(primary_key=True, auto=True))
            fields = {"id": auto_pk, **fields}
            cls.id = FieldDescriptor("id", int, auto_pk.spec)  # type: ignore[assignment]
            pk_fields = [auto_pk]

        cls._fields_ = fields
        cls._relations_ = relations
        cls._locals_ = local_attrs
        cls._table_name_ = namespace.get("_table_", name.lower())
        # Collect CompositeConstraint declarations from the class body
        all_constraints = [v for v in namespace.values() if isinstance(v, CompositeConstraint)]
        # Composite PKs: constraints created by PrimaryKey() have primary_key=True
        if _composite_pk_directive is not None:
            cls._pk_fields_ = _composite_pk_directive.fields
            cls._pk_field_ = None  # composite — no single pk_field shortcut
        else:
            cls._pk_fields_ = (pk_fields[0].name,) if pk_fields else ()
            cls._pk_field_ = pk_fields[0].name if pk_fields else None
        # Non-pk constraints only (PrimaryKey() directives are handled above)
        cls._constraints_ = [c for c in all_constraints if not c.primary_key]

        # -------------------------------------------------------------------
        # Single-table inheritance (STI) detection
        # -------------------------------------------------------------------
        # A child entity declares ``_discriminator_ = "dog"`` and inherits from
        # a parent that has ``_discriminator_col_ = "type"`` set.  The child
        # shares the parent's table; its extra fields become nullable columns.
        _disc_col_in_ns = "_discriminator_col_" in namespace
        sti_parent: EntityMeta | None = None
        if not _disc_col_in_ns:
            # Check direct bases for an STI-enabled parent
            for base in bases:
                if (
                    isinstance(base, EntityMeta)
                    and getattr(base, "_discriminator_col_", None) is not None
                ):
                    sti_parent = base
                    break
        cls._sti_parent_ = sti_parent
        cls._discriminator_val_ = namespace.get("_discriminator_")
        # STI child inherits parent's table name (unless the user explicitly set _table_)
        if sti_parent is not None and "_table_" not in namespace:
            cls._table_name_ = sti_parent._table_name_
        # _discriminator_col_ is a plain class attribute; inherit naturally from EntityMeta
        # definitions below — nothing extra needed here.

        _entity_registry.add(cls)

        return cls

    def __iter__(cls) -> Iterator[Any]:
        """Return an ``_EntityIterator`` for use in generator-expression queries.

        ``select(p for p in Product if p.price > 0)`` calls ``iter(Product)``
        to obtain the iterator before creating the generator object.  The
        :func:`~nextorm.generators.select` function reads the iterator's
        ``entity_cls`` attribute to discover what entity is being queried.
        """
        return _EntityIterator(cls)

    def __getitem__(cls, pk: Any) -> Any:
        """Look up an entity by primary key — single or composite.

        For a **single-column** PK pass the value directly::

            user = User[1]

        For a **composite** PK pass a tuple of values in the order the PK
        fields are declared::

            line = OrderLine[order_id, product_id]  # Python tuple syntax
            line = OrderLine[(order_id, product_id)]  # explicit tuple also works

        Raises :exc:`KeyError` if no row with the given primary key exists.
        Raises :exc:`ValueError` when the number of values does not match the
        number of PK fields on a composite key.
        """
        from nextorm.sql.nodes import BinOp, ColumnRef, Param  # noqa: PLC0415

        pk_fields = cls._pk_fields_
        pk_any: Any = pk

        if cls._pk_field_ is not None:
            # Single-column PK
            if isinstance(pk_any, tuple):
                pk_tuple: tuple[Any, ...] = pk_any  # pyright: ignore[reportUnknownVariableType]
                if len(pk_tuple) != 1:
                    raise ValueError(
                        f"{cls.__name__!r} has a single primary key; "
                        f"expected one value, got {len(pk_tuple)}."
                    )
                pk_any = pk_tuple[0]
            db = _find_db_for_entity(cls)
            qs = db.select(cls).filter(BinOp(ColumnRef(cls._pk_field_), "=", Param(value=pk_any)))
            result = qs.get()
            if result is None:
                raise KeyError(pk_any)
            return result

        # Composite PK
        pk_tuple2: tuple[Any, ...] = pk_any if isinstance(pk_any, tuple) else (pk_any,)  # pyright: ignore[reportUnknownVariableType]
        if len(pk_tuple2) != len(pk_fields):
            raise ValueError(
                f"{cls.__name__!r} has {len(pk_fields)} PK fields "
                f"({', '.join(pk_fields)}); got {len(pk_tuple2)} value(s)."
            )
        db = _find_db_for_entity(cls)
        conditions = [
            BinOp(ColumnRef(field), "=", Param(value=val))
            for field, val in zip(pk_fields, pk_tuple2, strict=False)
        ]
        node: Any = conditions[0]
        for cond in conditions[1:]:
            node = BinOp(node, "AND", cond)
        qs = db.select(cls).filter(node)
        result = qs.get()
        if result is None:
            raise KeyError(pk_tuple2)
        return result


class Entity(metaclass=EntityMeta):
    """Base class for all NextORM entities.

    Define fields using type aliases::

        class Product(Entity):
            name: Req[str]
            price: Req[float]
            description: Opt[str]

    Fields are discovered automatically; no ``db.Entity`` coupling needed.
    """

    # These are populated by EntityMeta for subclasses; harmless dummies here
    # so pyright doesn't complain about missing class vars.
    _fields_: ClassVar[dict[str, FieldInfo]] = {}
    _relations_: ClassVar[dict[str, RelationInfo]] = {}
    _locals_: ClassVar[set[str]] = set()
    _pk_fields_: ClassVar[tuple[str, ...]] = ()
    _pk_field_: ClassVar[str | None] = None
    # STI defaults — overridden by EntityMeta for entities that use inheritance
    _discriminator_col_: ClassVar[str | None] = None
    _discriminator_val_: ClassVar[str | None] = None
    _sti_parent_: ClassVar[EntityMeta | None] = None
    id: _PK[int]

    def __init__(self, **kwargs: Any) -> None:
        # Apply FieldSpec defaults first, then overwrite with provided kwargs
        for fi in self._fields_.values():
            if fi.spec.has_default and fi.name not in kwargs:
                default = fi.spec.default
                setattr(self, fi.name, default() if callable(default) else default)
        for key, value in kwargs.items():
            setattr(self, key, value)
        # Auto-register in the active session for INSERT at flush time.
        # Also try to find and attach the database so _do_insert can locate it.
        from nextorm.session import _get_session_stack  # noqa: PLC0415

        cache = _get_session_stack().current
        if cache is not None:
            try:
                db = _find_db_for_entity(type(self))
                vars(self)["_db_"] = db
            except RuntimeError:
                pass  # not yet mapped — skip auto-attach
            cache.schedule_save(self)

    # ------------------------------------------------------------------
    # Lifecycle hooks — override in subclasses as needed
    # ------------------------------------------------------------------

    def after_load(self) -> None:
        """Called after an existing entity is loaded from the database."""

    def before_insert(self) -> None:
        """Called before a new entity is saved to the database for the first time."""

    def after_insert(self) -> None:
        """Called after a new entity is saved to the database for the first time."""

    def before_update(self) -> None:
        """Called before a modified entity is saved to the database."""

    def after_update(self) -> None:
        """Called after a modified entity is saved to the database."""

    def before_delete(self) -> None:
        """Called before an entity is deleted from the database."""

    def after_delete(self) -> None:
        """Called after an entity is deleted from the database."""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def get_pk(self) -> Any:
        """Return the primary key value of this entity instance.

        Returns ``None`` when the entity has no primary key field or when the
        PK has not been set yet (e.g. before the first ``db.save()`` call).

        Example::

            u = User(name="alice")
            db.save(u)
            pk = u.get_pk()  # → 1
        """
        pk_fields = self.__class__._pk_fields_
        if not pk_fields:
            return None
        relations = self.__class__._relations_

        def _pk_part(fname: str) -> Any:
            if fname in relations:
                return self.__dict__.get(f"_{fname}_id")
            return getattr(self, fname)

        if len(pk_fields) == 1:
            return _pk_part(pk_fields[0])
        parts = tuple(_pk_part(f) for f in pk_fields)
        if any(p is None for p in parts):
            return None
        return parts

    def set(self, **kwargs: Any) -> None:
        """Bulk-assign attribute values in a single call.

        Equivalent to calling ``setattr(self, k, v)`` for every provided
        keyword argument.  Dirty-tracking in the active session is updated
        automatically because each assignment goes through the descriptor::

            user.set(name="bob", age=30)
        """
        for key, value in kwargs.items():
            setattr(self, key, value)

    def delete(self) -> None:
        """Delete this entity from the database using its attached database context.

        The entity must have been loaded or saved via a :class:`~nextorm.database.Database`
        (or :class:`~nextorm.database.AsyncDatabase`) so that the ``_db_`` context
        attribute is available.  Use :meth:`~nextorm.database.Database.delete_instance`
        directly if you want to pass the database explicitly.

        Raises :exc:`RuntimeError` when the entity has no attached database context.
        """
        db = vars(self).get("_db_")
        if db is None:
            raise RuntimeError(
                f"Cannot delete {self.__class__.__name__!r}: "
                "entity has no database context (_db_ not set). "
                "Load the entity via db.select(...) or save it first."
            )
        db.delete_instance(self)

    @classmethod
    def get(cls, **kwargs: Any) -> Self | None:
        """Return the first entity matching all given field values, or ``None``.

        Raises :exc:`RuntimeError` if more than one row matches.
        Uses :func:`_find_db_for_entity` to locate the mapped database.

        Example::

            user = User.get(name="alice")
        """
        from nextorm.sql.nodes import BinOp, ColumnRef, Param  # noqa: PLC0415

        db = _find_db_for_entity(cls)
        qs = db.select(cls)
        for field, value in kwargs.items():
            qs = qs.filter(BinOp(ColumnRef(field), "=", Param(value=value)))
        return cast("Self | None", qs.get())

    @classmethod
    def exists(cls, **kwargs: Any) -> bool:
        """Return ``True`` if at least one entity matches all given field values.

        Uses :func:`_find_db_for_entity` to locate the mapped database.

        Example::

            if User.exists(name="alice"):
                ...
        """
        from nextorm.sql.nodes import BinOp, ColumnRef, Param  # noqa: PLC0415

        db = _find_db_for_entity(cls)
        qs = db.select(cls)
        for field, value in kwargs.items():
            qs = qs.filter(BinOp(ColumnRef(field), "=", Param(value=value)))
        return cast("bool", qs.exists())

    @classmethod
    def select(cls) -> Any:
        """Return a :class:`~nextorm.query.QuerySet` for this entity.

        Convenience shortcut for ``db.select(Entity)`` that locates the mapped
        database automatically.

        Example::

            active_users = User.select().filter(User.active == True).fetch_all()
            count = User.select().count()
        """
        db = _find_db_for_entity(cls)
        return db.select(cls)

    @classmethod
    def aselect(cls) -> Any:
        """Return an :class:`~nextorm.async_database.AsyncQuerySet` for this entity.

        Sync builder — returns an ``AsyncQuerySet`` directly (no ``await`` needed).
        Async counterpart of :meth:`select`. Locates the mapped
        :class:`~nextorm.async_database.AsyncDatabase` automatically.

        Example::

            users = await User.aselect().filter(User.active == True).fetch_all()
        """
        from nextorm.async_database import AsyncDatabase  # noqa: PLC0415

        db = _find_db_for_entity(cls)
        if not isinstance(db, AsyncDatabase):
            raise RuntimeError(
                f"Entity {cls.__name__!r} is mapped to a sync Database, not an AsyncDatabase."
                " Use Entity.select() or db.aselect() explicitly."
            )
        return db.aselect(cls)

    @classmethod
    async def aget(cls, **kwargs: Any) -> Self | None:
        """Return the first entity matching all given field values, or ``None``.

        Async counterpart of :meth:`get`. Raises :exc:`RuntimeError` if more
        than one row matches.

        Example::

            user = await User.aget(email="alice@example.com")
        """
        from nextorm.async_database import AsyncDatabase  # noqa: PLC0415
        from nextorm.sql.nodes import BinOp, ColumnRef, Param  # noqa: PLC0415

        db = _find_db_for_entity(cls)
        if not isinstance(db, AsyncDatabase):
            raise RuntimeError(
                f"Entity {cls.__name__!r} is mapped to a sync Database, not an AsyncDatabase."
                " Use Entity.get() instead."
            )
        qs = db.aselect(cls)
        for field, value in kwargs.items():
            qs = qs.filter(BinOp(ColumnRef(field), "=", Param(value=value)))
        return await qs.get()

    def to_dict(
        self,
        only: list[str] | None = None,
        exclude: list[str] | None = None,
        *,
        with_collections: bool = False,
        with_lazy: bool = False,
        related_objects: bool = False,
    ) -> dict[str, Any]:
        """Serialize this entity to a plain Python dictionary.

        Parameters
        ----------
        only:
            If given, only the listed field names are included.  Relations are
            included only when *with_collections* or *related_objects* is also
            ``True``.
        exclude:
            Field names to exclude.  Applied after *only*.
        with_collections:
            When ``True``, ``Set[T]`` relation attributes are included as
            lists of their ``to_dict()`` results.  Requires the collections to
            have been prefetched or lazily loaded beforehand.
        with_lazy:
            When ``False`` (the default) lazy fields that have not yet been
            loaded are omitted from the result.  Pass ``True`` to include them;
            unloaded lazy fields are fetched on demand (sync databases only).
        related_objects:
            When ``True``, loaded ``Single[T]`` relation attributes are
            included as nested ``to_dict()`` results.  If the related object
            has not been loaded, the raw FK value (``{name}_id``) is included
            instead.

        Example::

            user.to_dict()
            # → {"id": 1, "name": "alice", "age": 30}

            user.to_dict(exclude=["id"])
            # → {"name": "alice", "age": 30}

            user.to_dict(with_collections=True)
            # → {"id": 1, ..., "posts": [{"id": 5, "title": "hi", ...}]}

            article.to_dict(with_lazy=True)
            # → {"id": 1, "title": "hi", "body": "<full text>"}

            comment.to_dict(related_objects=True)
            # → {"id": 1, "text": "great", "author": {"id": 2, "name": "bob"}}
        """
        from nextorm.fields import RelationKind  # noqa: PLC0415

        result: dict[str, Any] = {}
        cls = self.__class__

        # Regular fields
        for fi in cls._fields_.values():
            name = fi.name
            if only is not None and name not in only:
                continue
            if exclude is not None and name in exclude:
                continue
            unloaded_lazy = (
                fi.spec.lazy
                and not with_lazy
                and vars(self).get(f"_field_{name}") is _LAZY_SENTINEL
            )
            if unloaded_lazy:
                continue
            result[name] = getattr(self, name)

        # Relations
        if with_collections or related_objects:
            for ri in cls._relations_.values():
                name = ri.name
                if only is not None and name not in only:
                    continue
                if exclude is not None and name in exclude:
                    continue
                if with_collections and ri.spec.kind == RelationKind.SET:
                    # Only include relations that have already been loaded (prefetched)
                    # into the collection cache.  Unloaded collections are included as
                    # empty lists — matching PonyORM's to_dict() behaviour.
                    cache_key = f"_{name}_col"
                    cached = vars(self).get(cache_key)
                    if cached is not None and cached._cache is not None:
                        result[name] = [item.to_dict() for item in cached._cache]
                    else:
                        result[name] = []
                elif related_objects and ri.spec.kind == RelationKind.SINGLE:
                    # Include the related object as a nested dict if it has already
                    # been loaded; otherwise fall back to the raw FK id value.
                    obj_cache_key = f"_{name}_obj"
                    cached_obj = vars(self).get(obj_cache_key)
                    if cached_obj is not None:
                        result[name] = cached_obj.to_dict()
                    else:
                        fk_id = vars(self).get(f"_{name}_id")
                        if fk_id is not None:
                            result[f"{name}_id"] = fk_id

        return result

    def __repr__(self) -> str:
        pk_fields = self.__class__._pk_fields_
        if not pk_fields:
            return f"{self.__class__.__name__}()"
        if len(pk_fields) == 1:
            pk_val = getattr(self, pk_fields[0], None)
            return f"{self.__class__.__name__}({pk_fields[0]}={pk_val!r})"
        pairs = ", ".join(f"{f}={getattr(self, f, None)!r}" for f in pk_fields)
        return f"{self.__class__.__name__}({pairs})"

    # ------------------------------------------------------------------
    # Raw-SQL class-level entry points
    # ------------------------------------------------------------------

    @classmethod
    def select_by_sql(
        cls,
        db: Database,
        sql: str,
        params: list[Any] | None = None,
    ) -> list[Self]:
        """Execute *sql* and return all rows mapped to instances of this entity.

        Convenience wrapper around ``db.select(cls).raw(sql, params)``.

        Example::

            users = User.select_by_sql(db, "SELECT * FROM user WHERE age > ?", [18])
        """
        return db.select(cls).raw(sql, params)

    @classmethod
    def get_by_sql(
        cls,
        db: Database,
        sql: str,
        params: list[Any] | None = None,
    ) -> Self | None:
        """Execute *sql* and return the first row as an entity instance, or ``None``.

        Convenience wrapper around ``db.select(cls).raw_one(sql, params)``.

        Example::

            user = User.get_by_sql(db, "SELECT * FROM user WHERE id = ?", [1])
        """
        return db.select(cls).raw_one(sql, params)

    @classmethod
    async def aselect_by_sql(
        cls,
        db: AsyncDatabase,
        sql: str,
        params: list[Any] | None = None,
    ) -> list[Self]:
        """Async equivalent of :meth:`select_by_sql`.

        Example::

            users = await User.aselect_by_sql(db, "SELECT * FROM user WHERE age > %s", [18])
        """
        return await db.aselect(cls).raw(sql, params)

    @classmethod
    async def aget_by_sql(
        cls,
        db: AsyncDatabase,
        sql: str,
        params: list[Any] | None = None,
    ) -> Self | None:
        """Async equivalent of :meth:`get_by_sql`.

        Example::

            user = await User.aget_by_sql(db, "SELECT * FROM user WHERE id = %s", [1])
        """
        return await db.aselect(cls).raw_one(sql, params)
