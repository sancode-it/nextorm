"""EntityMeta metaclass and Entity base class."""

from __future__ import annotations

import contextlib
import dataclasses
import enum as _enum_lib
import inspect
import types
from collections.abc import Iterator, Sequence  # noqa: TC003
from typing import TYPE_CHECKING, Any, ClassVar, ForwardRef, Self, cast, overload

from nextorm.collection import RelatedCollection
from nextorm.fields import (
    _UUID_SENTINEL_MAP,
    CompositeConstraint,
    FieldSpec,
    LocalSpec,
    LongStr,
    Marker,
    OptAttrValue,
    Relation,
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
from nextorm.sql.nodes import BinOp, ColumnRef, Param

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

# Marker class → default FieldSpec mapping
# ---------------------------------------------------------------------------
# Each entry maps a marker class (PK, Req, Opt) to the default FieldSpec that
# EntityMeta creates for bare annotations (e.g. ``name: Req[str]`` with no
# call-site value).  When a marker is called (``Req[str](unique=True)``), the
# options from _options override this default via dataclasses.replace().

_FIELD_ALIAS_SPECS: dict[object, FieldSpec] = {
    _PK: FieldSpec(),
    _Req: FieldSpec(),
    _Opt: FieldSpec(),
}

# Sentinel used by SingleDescriptor to distinguish "not yet loaded" from None
_UNSET: object = object()

_RELATION_ALIAS_KINDS: dict[object, str] = {
    _Set: RelationKind.SET,
    _Single: RelationKind.SINGLE,
}


# ---------------------------------------------------------------------------
# Validation helpers for field options
# ---------------------------------------------------------------------------


def _normalize_singleopts_columns(marker_opts: dict[str, Any], attr_name: str) -> None:
    # column/columns
    if "column" in marker_opts and "columns" in marker_opts:
        raise TypeError(f"Relation '{attr_name}' cannot specify both 'column' and 'columns'.")
    if "column" in marker_opts:
        if not isinstance(marker_opts["column"], str):
            raise TypeError(f"Relation '{attr_name}' 'column' option must be a string.")
        marker_opts["columns"] = [marker_opts["column"]]
    if "columns" in marker_opts:
        cols = marker_opts["columns"]
        if isinstance(cols, str):
            marker_opts["columns"] = [cols]
            marker_opts["column"] = cols
        elif isinstance(cols, (list, tuple)) and len(cast("Sequence[str]", cols)) == 1:
            marker_opts["column"] = cols[0]
        elif not isinstance(cols, (list, tuple)):
            raise TypeError(f"Relation '{attr_name}' 'columns' must be a string or list of strings.")


def _normalize_setopts_reverse_columns(marker_opts: dict[str, Any], attr_name: str) -> None:
    # reverse_column/reverse_columns
    if "reverse_column" in marker_opts and "reverse_columns" in marker_opts:
        raise TypeError(
            f"Relation '{attr_name}' cannot specify both 'reverse_column' and 'reverse_columns'."
        )
    if "reverse_column" in marker_opts:
        if not isinstance(marker_opts["reverse_column"], str):
            raise TypeError(f"Relation '{attr_name}' 'reverse_column' option must be a string.")
        marker_opts["reverse_columns"] = [marker_opts["reverse_column"]]
    if "reverse_columns" in marker_opts:
        rcols = marker_opts["reverse_columns"]
        if isinstance(rcols, str):
            marker_opts["reverse_columns"] = [rcols]
            marker_opts["reverse_column"] = rcols
        elif isinstance(rcols, (list, tuple)) and len(cast("Sequence[str]", rcols)) == 1:
            marker_opts["reverse_column"] = rcols[0]
        elif not isinstance(rcols, (list, tuple)):
            raise TypeError(
                f"Relation '{attr_name}' 'reverse_columns' must be a string or list of strings."
            )


# ---------------------------------------------------------------------------
# Field descriptors stored on the class after metaclass processing
# ---------------------------------------------------------------------------


class FieldDescriptor[T: OptAttrValue]:
    """Runtime descriptor installed on an entity class for every persistent field.

    :class:`~nextorm.entity.EntityMeta` replaces each field annotation with a
    ``FieldDescriptor`` instance.  You never instantiate this class directly.

    **Class-level access** (``Product.name``) returns a
    :class:`~nextorm.expr.ColumnExpr` for use in query predicates:

    .. code-block:: python

        qs = db.select(Product).filter(Product.price > 10)

    **Instance-level access** (``product.name``) returns the stored value, with
    the following behaviours applied automatically on read:

    - **Lazy loading** — if the field was declared with ``lazy=True`` (or is a
      :class:`~nextorm.fields.LongStr`) and has not yet been fetched, a
      single-column ``SELECT`` is issued and the result cached.  Raises
      :exc:`RuntimeError` if the entity has no attached database context or if
      called from an async database without ``await``.
    - **Read tracking** — the column name is recorded in ``_read_cols_`` for
      per-field optimistic concurrency checks during ``UPDATE``.

    **Assignment** applies in order:

    1. Enum coercion — raw ``str``/``int`` values are converted to the declared
       ``Enum`` subtype automatically.
    2. ``autostrip`` — leading/trailing whitespace is stripped from string
       values when :attr:`~nextorm.fields.FieldSpec.autostrip` is ``True``.
    3. ``min``/``max`` range validation — raises :exc:`ValueError` on violation.
    4. ``py_check`` — calls the user-supplied validator; raises :exc:`ValueError`
       when it returns ``False``.
    5. Dirty-tracking — marks the entity dirty in the active session so that
       ``UPDATE`` is issued on flush.
    """

    def __init__(self, name: str, py_type: type[T], spec: FieldSpec) -> None:
        self.name = name
        self._py_type = py_type
        self.spec = spec
        self._attr = f"_field_{name}"

    @overload
    def __get__(self, obj: None, objtype: type) -> ColumnExpr: ...

    @overload
    def __get__(self, obj: Any, objtype: type | None) -> T: ...

    def __get__(self, obj: Any, objtype: type | None = None) -> T | ColumnExpr:
        if obj is None:
            # Class-level access → return a ColumnExpr for query building.
            # Import at call time to keep the module-level import under TYPE_CHECKING.
            from nextorm.expr import ColumnExpr as _ColumnExpr

            table_name: str | None = getattr(objtype, "_table_name_", None)
            return _ColumnExpr(self.name, table_name)
        val: T = obj.__dict__.get(self._attr)
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
            loaded: T = db._load_lazy_field(obj, self.name)
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

    def __set__(self, obj: Any, value: T) -> None:
        spec = self.spec
        if value is not None and value is not _LAZY_SENTINEL:
            # enum coercion: convert raw DB primitives (str / int) to the declared Enum type
            if issubclass(self._py_type, _enum_lib.Enum) and not isinstance(value, self._py_type):
                raw = value.value if isinstance(value, _enum_lib.Enum) else value
                value = self._py_type(raw)
            # autostrip: strip whitespace from string values
            if spec.autostrip and isinstance(value, str):
                value = cast("T", value.strip())
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
            from nextorm.session import _get_session_stack

            cache = _get_session_stack().current
            if cache is not None:
                cache.mark_dirty(obj)

    def __delete__(self, obj: Any) -> None:
        obj.__dict__.pop(self._attr, None)


class LocalDescriptor:
    """Runtime descriptor installed on an entity class for every :class:`~nextorm.fields.Local` field.

    Stores values directly in ``instance.__dict__`` under the key
    ``_local_{name}``, bypassing ORM dirty-tracking entirely.  Class-level
    access returns the descriptor itself so schema tooling can inspect it.

    On read, raises :exc:`AttributeError` if the field has not been assigned
    yet and no default was declared — the expected pattern is to set the value
    inside :meth:`~nextorm.entity.Entity.after_load`.

    On write, applies ``py_check`` from the :class:`~nextorm.fields.LocalSpec`
    if one was provided.
    """

    _MISSING: Any = object()

    def __init__(self, name: str, spec: LocalSpec) -> None:
        self.name = name
        self.spec = spec
        self._attr = f"_local_{name}"

    def __get__(self, obj: Any, objtype: type | None = None) -> Any:
        if obj is None:
            return self
        try:
            return obj.__dict__[self._attr]
        except KeyError:
            raise AttributeError(
                f"Local field '{self.name}' has not been initialised. "
                "Set it in after_load() or pass a default."
            ) from None

    def __set__(self, obj: Any, value: Any) -> None:
        if value is not None and self.spec.py_check is not None and not self.spec.py_check(value):
            raise ValueError(f"py_check failed for local field '{self.name}' with value {value!r}.")
        obj.__dict__[self._attr] = value

    def __delete__(self, obj: Any) -> None:
        obj.__dict__.pop(self._attr, None)


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
            from nextorm.expr import ColumnExpr as _ColumnExpr

            table_name: str | None = getattr(objtype, "_table_name_", None)
            return _ColumnExpr(self._fk_col, table_name)
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

        cond = BinOp(ColumnRef(pk_col), "=", Param(value=fk_id))
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

    if isinstance(target, str):
        return target.lower()
    if isinstance(target, ForwardRef):
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
    """Resolved metadata for a single persistent field, stored in ``cls._fields_``.

    Read-only after creation by :class:`EntityMeta`.  Available for
    introspection at runtime:

    .. code-block:: python

        for name, fi in Product._fields_.items():
            print(name, fi.py_type, fi.spec.nullable)

    Attributes
    ----------
    name:
        The attribute name as declared in the entity class.
    py_type:
        The Python type used to store and validate the value (e.g. ``int``,
        ``str``, :class:`~nextorm.fields.Vec`).  UUID sentinel types
        (``uuid7``, ``uuid4``, ``ulid``) are normalised to ``uuid.UUID`` or
        :class:`~nextorm.fields.ULID` here.
    spec:
        The :class:`~nextorm.fields.FieldSpec` containing all column options.
    """

    __slots__ = ("name", "py_type", "spec")

    def __init__(self, name: str, py_type: type, spec: FieldSpec) -> None:
        self.name = name
        self.py_type = py_type
        self.spec = spec

    def __repr__(self) -> str:
        return f"FieldInfo({self.name!r}, {self.py_type.__name__}, {self.spec!r})"


class RelationInfo:
    """Resolved metadata for a relation field, stored in ``cls._relations_``.

    Read-only after creation by :class:`EntityMeta`.  Available for
    introspection at runtime:

    .. code-block:: python

        for name, ri in Comment._relations_.items():
            print(name, ri.spec.kind, ri.spec.target)

    Attributes
    ----------
    name:
        The attribute name as declared in the entity class.
    spec:
        The :class:`~nextorm.fields.RelationSpec` containing all relation
        options (kind, target entity, nullable, FK column name, etc.).
    """

    __slots__ = ("name", "spec")

    def __init__(self, name: str, spec: RelationSpec) -> None:
        self.name = name
        self.spec = spec

    def __repr__(self) -> str:
        return f"RelationInfo({self.name!r}, {self.spec!r})"


class LocalInfo:
    """Resolved metadata for a local (transient) field, stored in ``cls._locals_``.

    Read-only after creation by :class:`EntityMeta`.  Available for
    introspection at runtime — for example to discover which fields need
    initialisation:

    .. code-block:: python

        for name, li in MyEntity._locals_.items():
            print(name, li.py_type, li.spec.has_default)

    Attributes
    ----------
    name:
        The attribute name as declared in the entity class.
    py_type:
        The declared Python type (e.g. ``str``, ``dict``).  ``type(None)``
        when no type argument was given (bare ``Local`` without subscript).
    spec:
        The :class:`~nextorm.fields.LocalSpec` carrying the ``default`` and
        ``py_check`` options.
    """

    __slots__ = ("name", "py_type", "spec")

    def __init__(self, name: str, py_type: type, spec: LocalSpec) -> None:
        self.name = name
        self.py_type = py_type
        self.spec = spec


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
    from nextorm.database import _database_registry

    table_name: str = getattr(entity_cls, "_table_name_", entity_cls.__name__.lower())
    for db in _database_registry:
        if table_name in getattr(db, "_schema", {}):
            return db
    raise RuntimeError(
        f"Cannot find a mapped Database for entity {entity_cls.__name__!r}."
        " Call db.generate_mapping() with this entity first."
    )


class EntityMeta(type):
    """Metaclass that processes field annotations at class-definition time.

    When a class that inherits from :class:`Entity` is created, ``EntityMeta``
    scans its annotations for NextORM markers (:class:`~nextorm.fields.Req`,
    :class:`~nextorm.fields.Opt`, :class:`~nextorm.fields.PK`,
    :class:`~nextorm.fields.Single`, :class:`~nextorm.fields.Set`,
    :class:`~nextorm.fields.Local`) and performs the following steps:

    1. Collects all annotations from the full MRO so that inherited fields
       are included.
    2. For each field marker annotation it builds a
       :class:`~nextorm.fields.FieldSpec`, merging type-specific options from
       the marker call site, and installs a :class:`FieldDescriptor`.
    3. For each relation marker it builds a
       :class:`~nextorm.fields.RelationSpec` and installs a
       :class:`SingleDescriptor` or :class:`SetDescriptor`.
    4. If no primary key is declared, injects an auto-increment ``id: PK[int]``
       column automatically.
    5. Registers the class in the global ``_entity_registry`` so that
       :class:`~nextorm.database.Database` can discover it during
       ``generate_mapping()``.

    The following class-level attributes are set on every ``Entity`` subclass:

    ``_fields_`` : ``dict[str, FieldInfo]``
        All persistent scalar fields keyed by attribute name.
    ``_relations_`` : ``dict[str, RelationInfo]``
        All relation fields keyed by attribute name.
    ``_locals_`` : ``dict[str, LocalInfo]``
        All transient :class:`~nextorm.fields.Local` fields keyed by attribute
        name, each carrying its resolved :class:`~nextorm.fields.LocalSpec`
        (default value, py_check).
    ``_pk_fields_`` : ``tuple[str, ...]``
        Attribute names of all primary-key fields (one for scalar PKs, multiple
        for composite PKs declared with :func:`~nextorm.fields.PrimaryKey`).
    ``_pk_field_`` : ``str | None``
        The single PK attribute name for entities with a scalar PK; ``None``
        for composite PKs.
    ``_table_name_`` : ``str``
        SQL table name — defaults to the class name lower-cased; override by
        declaring ``_table_ = "my_table"`` in the class body.
    ``_constraints_`` : ``list[CompositeConstraint]``
        Non-PK composite unique constraints and indexes declared with
        :func:`~nextorm.fields.composite_key` or
        :func:`~nextorm.fields.composite_index`.

    **Single-table inheritance (STI)** is supported by declaring
    ``_discriminator_col_`` on the parent entity and ``_discriminator_`` on
    each child:

    .. code-block:: python

        class Animal(Entity):
            _discriminator_col_ = "kind"
            name: Req[str]


        class Dog(Animal):
            _discriminator_ = "dog"
            breed: Opt[str]
    """

    _fields_: dict[str, FieldInfo]
    _relations_: dict[str, RelationInfo]
    _locals_: dict[str, LocalInfo]
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

    id: ClassVar[FieldDescriptor[int]]

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
        local_attrs: dict[str, LocalInfo] = {}

        def _is_field_marker(o: object) -> bool:
            if o in _FIELD_ALIAS_SPECS:
                return True
            if isinstance(o, type) and any(
                base in _FIELD_ALIAS_SPECS for base in getattr(o, "__mro__", tuple())
            ):
                return True
            origin: object | None = getattr(o, "__origin__", None)
            if origin in _FIELD_ALIAS_SPECS:
                return True
            return bool(
                isinstance(origin, type)
                and any(base in _FIELD_ALIAS_SPECS for base in getattr(origin, "__mro__", tuple()))
            )

        def _is_relation_marker(o: object) -> bool:
            try:
                if isinstance(o, type) and issubclass(o, Relation):
                    return True
            except Exception:  # pragma: no cover
                pass
            origin: object | None = getattr(cast("object", o), "__origin__", None)  # type: ignore[redundant-cast]
            try:
                if isinstance(origin, type) and issubclass(origin, Relation):  # pragma: no cover
                    return True
            except Exception:  # pragma: no cover
                pass
            return False

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

            # Subscripting a marker class (e.g. Req[str]) returns a new subclass whose
            # __origin__ points back to the base marker class (Req, Opt, PK, …).
            # EntityMeta identifies marker kind by checking __origin__ against
            # _FIELD_ALIAS_SPECS and _RELATION_ALIAS_KINDS.  Plain annotations (int, str,
            # etc.) produce no __origin__ match and are silently ignored.
            origin = cast(
                "type[Marker[OptAttrValue | Entity]]", getattr(annotation, "__origin__", None)
            )
            args: tuple[Any, ...] = getattr(annotation, "__args__", ())
            inner_type = None
            if args:
                if isinstance(annotation, types.UnionType):  # pragma: no cover
                    non_none = [a for a in args if a is not type(None)]
                    inner_type = non_none[0] if non_none else type(None)
                else:
                    inner_type = args[0]
            if inner_type is None:
                inner_type = getattr(annotation, "_type_arg_", None)
            if inner_type is None:
                orig_bases = getattr(annotation, "__orig_bases__", ())
                for base in orig_bases:
                    base_args = getattr(base, "__args__", ())
                    if base_args:  # pragma: no cover
                        inner_type = base_args[0]
                        break
            if inner_type is None:
                inner_type = type(None)

            # --- Field marker (Req, Opt, PK) ---
            marker_opts = {}
            if _is_field_marker(annotation):
                base_spec = _FIELD_ALIAS_SPECS.get(origin)
                class_val = namespace.get(attr_name)
                # Always extract marker options if present
                if class_val is not None and hasattr(class_val, "_options"):
                    marker_opts = dict(class_val._options)
                    # --- FieldSpec: Only 'column' is allowed, not 'columns' ---
                    if "columns" in marker_opts:
                        raise TypeError(
                            f"Field '{attr_name}' cannot specify 'columns' - use 'column' instead."
                        )
                    if "column" in marker_opts and not isinstance(marker_opts["column"], str):
                        raise TypeError(f"Field '{attr_name}' 'column' option must be a string.")
                # For Opt, enforce correct nullable logic
                # - Opt[str]/Opt[LongStr]: nullable only if user sets it
                # - Opt[non-str]: always nullable unless explicitly set
                actual_inner_type = inner_type
                if origin is _Opt:
                    # Determine if this is a string-like Opt
                    is_str_type = issubclass(inner_type, str)
                    # If not string, force nullable True unless user set it
                    if not is_str_type and "nullable" not in marker_opts:
                        marker_opts["nullable"] = True

                # --- PK special handling: if PK[...] is an Entity, treat as relation ---
                if origin is _PK or (isinstance(annotation, type) and issubclass(annotation, _PK)):
                    # Determine the type argument for PK[...] (scalar or Entity)
                    ann = cast("object", annotation)
                    pk_type = getattr(ann, "_type_arg_", None)
                    if pk_type is None and hasattr(ann, "__args__"):  # pragma: no cover
                        pk_type = annotation.__args__[0]
                    # If the PK type is an Entity, treat as relation
                    from nextorm.entity import Entity as _Entity

                    is_entity_pk = False
                    with contextlib.suppress(Exception):
                        is_entity_pk = isinstance(pk_type, type) and issubclass(pk_type, _Entity)
                    if is_entity_pk:
                        # Forward all marker options to RelationSpec
                        rel_spec = RelationSpec(
                            kind=RelationKind.SINGLE,
                            target=pk_type,
                            primary_key=True,
                            nullable=marker_opts.get("nullable", False),
                            **{k: v for k, v in marker_opts.items() if k != "nullable"},
                        )
                        ri = RelationInfo(attr_name, rel_spec)
                        relations[attr_name] = ri
                        setattr(cls, attr_name, SingleDescriptor(attr_name, ri))
                        continue  # skip normal field handling

                # Remove 'nullable' from marker_opts to avoid duplicate argument
                marker_opts_no_nullable = dict(marker_opts)
                marker_opts_no_nullable.pop("nullable", None)
                spec: FieldSpec = dataclasses.replace(
                    base_spec or FieldSpec(),
                    **marker_opts_no_nullable,
                    nullable=marker_opts.get("nullable", base_spec.nullable if base_spec else False),
                )

                # --- UUID/ULID/Vec storage type normalization ---
                if inner_type in _UUID_SENTINEL_MAP:
                    storage_type, auto_kind = _UUID_SENTINEL_MAP[inner_type]
                    actual_inner_type = storage_type
                    # If this is a PK field by marker or by explicit option, force primary_key True
                    is_pk = (
                        spec.primary_key
                        or origin is _PK
                        or (isinstance(annotation, type) and issubclass(annotation, _PK))
                    )
                    # Respect explicit uuid_auto override from the user (incl. None to disable).
                    # Only auto-derive when the user did not explicitly pass uuid_auto.
                    user_uuid_auto = marker_opts.get("uuid_auto", auto_kind)
                    if is_pk:
                        spec = dataclasses.replace(
                            spec, primary_key=True, auto=False, uuid_auto=user_uuid_auto
                        )
                    elif spec.unique:
                        # Non-PK unique UUID field: auto-generate before INSERT by default.
                        # Pass uuid_auto=None explicitly to supply the value yourself.
                        if "uuid_auto" not in marker_opts:
                            spec = dataclasses.replace(spec, uuid_auto=auto_kind)
                        # else: user override (including None) already in spec
                    else:
                        spec = dataclasses.replace(spec, uuid_auto=None)
                elif inner_type is LongStr:
                    # Only auto-set lazy=True for LongStr if the user did NOT specify lazy
                    if (
                        "lazy" not in marker_opts
                        and not isinstance(class_val, FieldSpec)
                        and not spec.lazy
                    ):
                        spec = dataclasses.replace(spec, lazy=True)
                elif inner_type is not Vec and issubclass(inner_type, Vec):
                    # Vec[384] produces a dynamic Vec subclass carrying _dimensions_.
                    # Normalise to the base Vec type and store the dimension in FieldSpec.
                    dims: int | None = getattr(inner_type, "_dimensions_", None)
                    actual_inner_type = Vec
                    if dims is not None:
                        spec = dataclasses.replace(spec, dimensions=dims)
                if spec.lazy and spec.primary_key:
                    raise TypeError(f"Field '{attr_name}' cannot be both lazy and a primary key.")
                fields[attr_name] = FieldInfo(attr_name, actual_inner_type, spec)
                setattr(cls, attr_name, FieldDescriptor(attr_name, actual_inner_type, spec))
            # --- Relation marker (Single, Set) ---
            elif _is_relation_marker(annotation):
                kind = _RELATION_ALIAS_KINDS.get(origin)
                if kind == RelationKind.SINGLE:
                    # Detect Single[T | None] → nullable FK
                    raw_target = args[0] if args else type(None)
                    # Handle Optional/Union for nullable detection
                    if isinstance(raw_target, types.UnionType):
                        non_none = [a for a in raw_target.__args__ if a is not type(None)]
                        nullable = len(non_none) < len(getattr(raw_target, "__args__", []))
                        target: type | str = non_none[0] if non_none else type(None)
                    else:
                        nullable = False
                        target = raw_target
                    # Allow a RelationSpec class-level value to customise the relation
                    # (e.g. ``profile: Single[Profile] = RelationSpec(owner=True)``).
                    rel_class_val = namespace.get(attr_name)
                    marker_opts = {}
                    # --- Validate and normalize ---
                    if rel_class_val and hasattr(rel_class_val, "_options"):
                        # Marker instance: forward all options to RelationSpec
                        marker_opts = dict(rel_class_val._options)
                        _normalize_singleopts_columns(marker_opts, attr_name)
                        nullable = marker_opts.pop("nullable", nullable)
                        rel_spec = RelationSpec(
                            kind=kind, target=target, nullable=nullable, **marker_opts
                        )
                    elif isinstance(rel_class_val, RelationSpec):
                        rel_spec = dataclasses.replace(
                            rel_class_val, kind=kind, target=target, nullable=nullable
                        )
                    else:
                        rel_spec = RelationSpec(kind=kind, target=target, nullable=nullable)
                    ri = RelationInfo(attr_name, rel_spec)
                    relations[attr_name] = ri
                    setattr(cls, attr_name, SingleDescriptor(attr_name, ri))
                elif kind == RelationKind.SET:  # pragma: no branch
                    target = args[0] if args else attr_name
                    rel_class_val = namespace.get(attr_name)
                    if hasattr(rel_class_val, "_options"):
                        marker_opts = getattr(rel_class_val, "_options", {})
                        _normalize_setopts_reverse_columns(marker_opts, attr_name)
                        rel_spec = RelationSpec(kind=kind, target=target, **marker_opts)
                    elif isinstance(rel_class_val, RelationSpec):
                        rel_spec = dataclasses.replace(
                            rel_class_val,
                            kind=kind,
                            target=target,
                        )
                    else:
                        rel_spec = RelationSpec(kind=kind, target=target)
                    ri = RelationInfo(attr_name, rel_spec)
                    relations[attr_name] = ri
                    setattr(cls, attr_name, SetDescriptor(attr_name, ri))
            # --- Local marker (Local) ---
            elif origin is _Local or (
                isinstance(annotation, type) and issubclass(annotation, _Local)
            ):
                inner: type = getattr(cast("object", annotation), "_type_arg_", None) or type(None)
                class_val = namespace.get(attr_name)
                local_opts: dict[str, Any] = {}
                if class_val is not None and hasattr(class_val, "_options"):
                    local_opts = dict(class_val._options)
                local_spec = LocalSpec(**local_opts)
                li = LocalInfo(attr_name, inner, local_spec)
                local_attrs[attr_name] = li
                setattr(cls, attr_name, LocalDescriptor(attr_name, local_spec))

        # Check for a PrimaryKey() composite directive before injecting auto-pk
        _composite_pk_directive = next(
            (v for v in namespace.values() if isinstance(v, CompositeConstraint) and v.primary_key),
            None,
        )

        # Patch PK fields: handle marker instance with primary_key, and PK[...] annotation
        for attr_name, f in list(fields.items()):
            # Already a PK field
            if getattr(f.spec, "primary_key", False):
                continue
            ann = all_annotations.get(f.name)
            origin = cast("type[Marker[OptAttrValue | Entity]]", getattr(ann, "__origin__", None))
            class_val = namespace.get(attr_name)
            # If the class value is a marker instance with _options["primary_key"]
            if (
                class_val is not None
                and hasattr(class_val, "_options")
                and isinstance(class_val._options, dict)
                and getattr(class_val, "_options", {}).get("primary_key", False)
            ):  # pragma: no cover
                new_spec = dataclasses.replace(
                    f.spec,
                    primary_key=True,
                    auto=getattr(class_val, "_options", {}).get("auto", False),
                )
                fields[attr_name] = FieldInfo(
                    f.name,
                    f.py_type,
                    new_spec,
                )
                continue
            # If the annotation is a PK marker, patch the FieldSpec
            if origin is _PK or (isinstance(ann, type) and issubclass(ann, _PK)):
                inner_type = None
                args = getattr(cast("object", ann), "__args__", ())
                if args:  # pragma: no branch
                    inner_type = args[0]
                if inner_type is None:  # pragma: no cover
                    inner_type = getattr(cast("object", ann), "_type_arg_", None)
                if inner_type is int:
                    new_spec = dataclasses.replace(f.spec, primary_key=True, auto=True)
                else:
                    new_spec = dataclasses.replace(f.spec, primary_key=True)
                fields[attr_name] = FieldInfo(
                    f.name,
                    f.py_type,
                    new_spec,
                )
        pk_fields: list[FieldInfo] = [
            f for f in fields.values() if getattr(f.spec, "primary_key", False)
        ]
        if not pk_fields and _composite_pk_directive is None:
            if "id" in fields:
                raise TypeError(
                    f"Entity '{name}' defines 'id' field but doesn't declare it as a primary key. "
                    "Either annotate it as a PK or declare a primary key explicitly."
                )
            auto_pk = FieldInfo("id", int, FieldSpec(primary_key=True, auto=True))
            fields = {"id": auto_pk, **fields}
            fd: FieldDescriptor[int] = FieldDescriptor("id", int, auto_pk.spec)
            setattr(cls, "id", fd)  # noqa: B010
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

    Subclass ``Entity`` and annotate fields with NextORM markers to define a
    mapped table:

    .. code-block:: python

        from nextorm.fields import Req, Opt, PK, Single, Set, Local


        class User(Entity):
            name: Req[str]
            email: Req[str] = Req[str](unique=True)
            age: Opt[int]


        class Post(Entity):
            title: Req[str]
            body: Req[LongStr]
            author: Single[User]
            tags: Set[Tag]

    **Primary keys** — a ``PK[int]`` auto-increment column named ``id`` is
    injected automatically when no PK is declared.  Override explicitly:

    .. code-block:: python

        class Event(Entity):
            id: PK[uuid7]  # time-ordered UUID, Python-generated
            title: Req[str]

    **Table name** — defaults to the class name lower-cased.  Override:

    .. code-block:: python

        class BlogPost(Entity):
            _table_ = "blog_post"
            title: Req[str]

    **Composite primary keys** — use :func:`~nextorm.fields.PrimaryKey`:

    .. code-block:: python

        class OrderLine(Entity):
            order: Single[Order]
            product: Single[Product]
            quantity: Req[int]
            _pk_ = PrimaryKey("order", "product")

    **Single-table inheritance (STI)** — declare ``_discriminator_col_`` on the
    parent and ``_discriminator_`` on each child; all share one SQL table:

    .. code-block:: python

        class Animal(Entity):
            _discriminator_col_ = "kind"
            name: Req[str]


        class Dog(Animal):
            _discriminator_ = "dog"
            breed: Opt[str]
    """

    # These are populated by EntityMeta for subclasses; harmless dummies here
    # so pyright doesn't complain about missing class vars.
    _fields_: ClassVar[dict[str, FieldInfo]] = {}
    _relations_: ClassVar[dict[str, RelationInfo]] = {}
    _locals_: ClassVar[dict[str, LocalInfo]] = {}
    _pk_fields_: ClassVar[tuple[str, ...]] = ()
    _pk_field_: ClassVar[str | None] = None
    # STI defaults — overridden by EntityMeta for entities that use inheritance
    _discriminator_col_: ClassVar[str | None] = None
    _discriminator_val_: ClassVar[str | None] = None
    _sti_parent_: ClassVar[EntityMeta | None] = None

    id: _PK[int] | Any

    def __init__(self, **kwargs: Any) -> None:
        """Initialise a new (unsaved) entity instance.

        Pass field values as keyword arguments. Fields not provided here
        receive their :attr:`~nextorm.fields.FieldSpec.default` value (if one
        was declared) or ``None``:

        .. code-block:: python

            u = User(name="alice", email="alice@example.com")

        If an active session exists, the new instance is automatically
        scheduled for INSERT on the next :meth:`flush
        <nextorm.session.Session.flush>` call.
        """

        # Apply FieldSpec defaults first, then overwrite with provided kwargs
        for fi in self._fields_.values():
            if fi.spec.has_default and fi.name not in kwargs:
                default = fi.spec.default
                setattr(self, fi.name, default() if callable(default) else default)
        # Apply Local defaults
        for li in self._locals_.values():
            if li.spec.has_default and li.name not in kwargs:
                default = li.spec.default
                setattr(self, li.name, default() if callable(default) else default)
        for key, value in kwargs.items():
            setattr(self, key, value)
        # Ensure all persistent fields are initialized (including PK)
        for fi in self._fields_.values():
            if not hasattr(self, fi.name):  # pragma: no branch
                setattr(self, fi.name, None)  # pragma: no cover
        # Auto-register in the active session for INSERT at flush time.
        # Also try to find and attach the database so _do_insert can locate it.
        from nextorm.session import _get_session_stack

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
        """Called after an existing entity row is loaded from the database.

        Override to initialise :class:`~nextorm.fields.Local` fields or to
        compute derived state from persisted values:

        .. code-block:: python

            class Product(Entity):
                name: Req[str]
                price: Req[float]
                _display: Local[str]

                def after_load(self) -> None:
                    self._display = f"{self.name} — ${self.price:.2f}"
        """
        pass

    def before_insert(self) -> None:
        """Called immediately before a new entity is written to the database.

        Override to set computed fields or perform validation before the first
        ``INSERT``:

        .. code-block:: python

            class Article(Entity):
                title: Req[str]
                slug: Req[str]

                def before_insert(self) -> None:
                    if not self.slug:
                        self.slug = self.title.lower().replace(" ", "-")
        """
        pass

    def after_insert(self) -> None:
        """Called after a new entity has been written to the database.

        The auto-generated primary key (if any) is available at this point:

        .. code-block:: python

            def after_insert(self) -> None:
                print(f"Saved with id={self.id}")
        """
        pass

    def before_update(self) -> None:
        """Called immediately before a dirty entity is flushed to the database.

        Override to update timestamps or enforce invariants before ``UPDATE``:

        .. code-block:: python

            class Post(Entity):
                updated_at: Req[datetime]

                def before_update(self) -> None:
                    self.updated_at = datetime.now()
        """
        pass

    def after_update(self) -> None:
        """Called after a modified entity has been successfully written to the database."""
        pass

    def before_delete(self) -> None:
        """Called immediately before an entity is deleted from the database.

        Override to perform cleanup or cascading logic that cannot be expressed
        in the schema:

        .. code-block:: python

            def before_delete(self) -> None:
                self._cleanup_files()
        """
        pass

    def after_delete(self) -> None:
        """Called after an entity has been successfully deleted from the database."""
        pass

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
        """Bulk-assign field values in a single call.

        Each assignment goes through the descriptor, so enum coercion,
        validation, ``autostrip``, and dirty-tracking all apply:

        .. code-block:: python

            user.set(name="bob", age=31)
            # equivalent to:
            user.name = "bob"
            user.age = 31
        """
        for key, value in kwargs.items():
            setattr(self, key, value)

    def delete(self) -> None:
        """Delete this entity from the database using its attached database context.

        The entity must have been loaded or saved through a
        :class:`~nextorm.database.Database` so that the ``_db_`` context
        attribute is set.  Use
        :meth:`~nextorm.database.Database.delete_instance` directly if you want
        to pass the database explicitly.

        .. code-block:: python

            post = db.select(Post).filter(Post.id == 42).fetch_one()
            post.delete()

        Raises :exc:`RuntimeError` when the entity has no attached database
        context (``_db_`` not set).
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

        Locates the mapped database automatically via
        :func:`_find_db_for_entity`.  Raises :exc:`RuntimeError` if more than
        one row matches.

        .. code-block:: python

            user = User.get(email="alice@example.com")
            if user is None:
                print("not found")

        For more complex filtering use :meth:`select`:

        .. code-block:: python

            user = User.select().filter(User.age > 18).get()
        """
        db = _find_db_for_entity(cls)
        qs = db.select(cls)
        for field, value in kwargs.items():
            qs = qs.filter(BinOp(ColumnRef(field), "=", Param(value=value)))
        return cast("Self | None", qs.get())

    @classmethod
    def exists(cls, **kwargs: Any) -> bool:
        """Return ``True`` if at least one entity matches all given field values.

        .. code-block:: python

            if User.exists(email="alice@example.com"):
                raise ValueError("Email already in use")
        """
        db = _find_db_for_entity(cls)
        qs = db.select(cls)
        for field, value in kwargs.items():
            qs = qs.filter(BinOp(ColumnRef(field), "=", Param(value=value)))
        return cast("bool", qs.exists())

    @classmethod
    def select(cls) -> Any:
        """Return a :class:`~nextorm.query.QuerySet` for this entity.

        Locates the mapped database automatically.  Chain filter, ordering, and
        fetch methods on the returned queryset:

        .. code-block:: python

            active = User.select().filter(User.active == True).fetch_all()
            count = User.select().count()
            first = User.select().filter(User.age > 18).get()
        """
        db = _find_db_for_entity(cls)
        return db.select(cls)

    @classmethod
    def aselect(cls) -> Any:
        """Return an :class:`~nextorm.async_database.AsyncQuerySet` for this entity.

        Returns the queryset synchronously — no ``await`` needed on this call.
        ``await`` is required when executing the query:

        .. code-block:: python

            users = await User.aselect().filter(User.active == True).fetch_all()
            count = await User.aselect().count()

        Raises :exc:`RuntimeError` if the entity is mapped to a sync
        :class:`~nextorm.database.Database` rather than an
        :class:`~nextorm.async_database.AsyncDatabase`.
        """
        from nextorm.async_database import AsyncDatabase

        db = _find_db_for_entity(cls)
        if not isinstance(db, AsyncDatabase):
            raise RuntimeError(
                f"Entity {cls.__name__!r} is mapped to a sync Database, not an AsyncDatabase."
                " Use Entity.select() or db.aselect() explicitly."
            )
        return db.aselect(cls)

    @classmethod
    async def aget(cls, **kwargs: Any) -> Self | None:
        """Async equivalent of :meth:`get`.

        Return the first entity matching all given field values, or ``None``.
        Raises :exc:`RuntimeError` if more than one row matches.

        .. code-block:: python

            user = await User.aget(email="alice@example.com")
        """
        from nextorm.async_database import AsyncDatabase

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
                fi.spec.lazy and not with_lazy and vars(self).get(f"_field_{name}") is _LAZY_SENTINEL
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
