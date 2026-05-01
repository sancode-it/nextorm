"""Field type aliases and metadata for NextORM entity definitions."""

from __future__ import annotations

import os
import uuid as _uuid_stdlib
from dataclasses import dataclass, field, is_dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from time import time as time_time
from typing import TYPE_CHECKING, Any, Literal, cast, overload

if TYPE_CHECKING:
    from collections.abc import Callable

    from nextorm.collection import RelatedCollection
    from nextorm.entity import Entity
    from nextorm.expr import ColumnExpr

__all__ = [
    "FieldSpec",
    "RelationSpec",
    "RelationKind",
    "LocalSpec",
    "CompositeConstraint",
    "composite_key",
    "composite_index",
    "PrimaryKey",
    "PK",
    "Req",
    "Opt",
    "Local",
    "Set",
    "Single",
    # UUID / ULID types and sentinels
    "ULID",
    "uuid7",
    "uuid4",
    "ulid",
    "_uuid_stdlib",
    # Extended sentinel types
    "LongStr",
    "Json",
    "DateTimeTz",
    "Vec",
    # UUID / ULID generation helpers (used by database layer and tests)
    "_generate_uuid7",
    "_generate_ulid",
    # Value serialisation helper (used by database layer)
    "_serialize_value",
]

type Numeric = bool | int | float | Decimal
type DateTime = datetime | date | time | timedelta | DateTimeTz
type Uuid = uuid4 | uuid7 | ulid
type AttrValue = str | Numeric | DateTime | Uuid | Json | bytes | LongStr | Enum | Vec
type OptAttrValue = AttrValue | None

# Same as AttrValue but excludes float
type UniqueAttrValue = (
    bool | int | Uuid | str | Decimal | DateTime | Json | bytes | LongStr | Enum | Vec
)
type PkValue = UniqueAttrValue | Entity | tuple[UniqueAttrValue | Entity, ...]


# ---------------------------------------------------------------------------
# Field, Relation and Local options
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LocalOpts:
    default: Any = field(default_factory=lambda: _MISSING, compare=False)
    py_check: Callable[[object], bool] | None = field(default=None, compare=False)


@dataclass(frozen=True)
class RelationOpts:
    cascade_delete: bool | None = None  # None = auto-derive from nullable
    reverse: str | None = None
    column: str | None = None  # DB column name for mapping this attribute (default: attribute name)
    columns: list[str] | None = None  # DB column names for mapping a composite attribute


@dataclass(frozen=True)
class SingleOpts:
    fk_name: str | None = None  # (str) Name of the foreign key constraint in the database
    nullable: bool = False  # nullable FK column
    owner: bool | None = None  # O2O only: explicit owning-side override
    primary_key: bool = False


@dataclass(frozen=True)
class SetOpts:
    table: str | None = None  # M2M only: override join table name
    reverse_column: str | None = None
    reverse_columns: list[str] | None = None


# FieldOpts lists options available to ALL field types regardless of their
# storage type. Type-specific options (max_len, size, precision, …) live in
# *TypeOpts dataclasses and are merged into allowed_keywords by
# field_class_getitem.  Every field in FieldOpts must also be a field in
# FieldSpec — EntityMeta passes these kwargs straight to dataclasses.replace().


@dataclass(frozen=True)
class FieldOpts:
    column: str | None = None
    default: Any = None
    index: bool = False
    lazy: bool = False
    primary_key: bool = False
    py_check: Callable[[object], bool] | None = None
    sql_default: str | None = None
    sql_type: str | None = None
    unique: bool = False
    volatile: bool = False


@dataclass(frozen=True)
class IntTypeOpts:
    size: Literal[8] | Literal[16] | Literal[32] | Literal[64] = 64
    min: int | None = None
    max: int | None = None
    unsigned: bool = False
    auto: bool = False  # auto-incrementing integer


@dataclass(frozen=True)
class FloatTypeOpts:
    tolerance: float | None = None  # for approximate equality checks
    min: float | None = None
    max: float | None = None


@dataclass(frozen=True)
class DecimalTypeOpts:
    precision: int | None = None  # total significant digits
    scale: int | None = None  # digits after decimal point
    min: float | None = None
    max: float | None = None


@dataclass(frozen=True)
class StrTypeOpts:
    max_len: int | None = None
    autostrip: bool = True
    db_encoding: str | None = None  # e.g. "utf8mb4" for full Unicode support on MariaDB
    nullable: bool = False


@dataclass(frozen=True)
class LongStrTypeOpts:
    autostrip: bool = False
    db_encoding: str | None = None  # e.g. "utf8mb4" for full Unicode support on MariaDB
    nullable: bool = False


@dataclass(frozen=True)
class DateTimeTypeOpts:
    precision: int | None = None  # for fractional seconds, e.g. 3 for millisecond precision


@dataclass(frozen=True)
class VecTypeOpts:
    dimensions: int | None = None  # number of vector dimensions (e.g. 384, 1536)


@dataclass(frozen=True)
class UuidTypeOpts:
    uuid_auto: str | None = None  # "v7", "v4", or "ulid" — Python-side auto-generation


# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------
# Markers for persistent fields (PK, Req, Opt), relations (Single, Set)
# and local fields (Local)


# Shared helper for Req/Opt field marker class creation
def field_class_getitem[FT: Field[OptAttrValue], T](
    cls: type[FT],
    item: type[T],
    prefix: str,
) -> type[FT]:
    # Determine allowed positional and keyword arguments based on type
    opts_groups: list[type] = []
    allowed_positional: tuple[str, ...] = ()
    exclude_opts: tuple[str, ...] = ()
    if item is int:
        opts_groups = [IntTypeOpts]
        allowed_positional = ("size",)
        if prefix == "Opt":
            exclude_opts = ("auto",)
    elif item is float:
        opts_groups = [FloatTypeOpts]
        allowed_positional = ("tolerance",)
        exclude_opts = ("primary_key",)
    elif item is Decimal:
        opts_groups = [DecimalTypeOpts]
        allowed_positional = ("precision", "scale")
    elif item is str:
        opts_groups = [StrTypeOpts]
        allowed_positional = ("max_len",)
        if prefix != "Opt":
            exclude_opts = ("nullable",)
    elif item is LongStr:
        opts_groups = [LongStrTypeOpts]
        if prefix != "Opt":
            exclude_opts = ("nullable",)
    elif item in (time, timedelta, datetime, DateTimeTz):  # date excluded: precision is not needed
        opts_groups = [DateTimeTypeOpts]
        allowed_positional = ("precision",)
    elif item is Vec:
        opts_groups = [VecTypeOpts]
        allowed_positional = ("dimensions",)
    elif item in (uuid7, uuid4, ulid):
        opts_groups = [UuidTypeOpts]
        if prefix != "Opt":
            allowed_positional = ("uuid_auto",)
        else:
            exclude_opts = ("uuid_auto",)

    base_opts = FieldOpts()
    allowed_keywords = set(base_opts.__dataclass_fields__.keys())
    for group in opts_groups:
        if is_dataclass(group):
            allowed_keywords.update(group.__dataclass_fields__.keys())
    if exclude_opts:
        allowed_keywords.difference_update(set(exclude_opts))

    def __init__(self: FT, *args: Any, **kwargs: Any) -> None:
        opts: dict[str, Any] = {}
        # Accept positional arguments, but do not require them
        if len(args) > len(allowed_positional):
            raise TypeError(
                f"Too many positional arguments for {prefix}["
                + f"{getattr(item, '__name__', repr(item))}"
                + f"]: expected at most {len(allowed_positional)}"
            )
        for pname, value in zip(allowed_positional, args, strict=False):
            opts[pname] = value
        # Check for duplicate (positional + kwarg)
        for pname in allowed_positional:
            if pname in kwargs and pname in opts:
                raise TypeError(
                    f"{prefix}[{getattr(item, '__name__', repr(item))}]() "
                    + f"got multiple values for argument '{pname}'"
                )
        opts.update(kwargs)
        # PK marker: set primary_key and auto by default unless overridden
        if prefix == "PK":
            if "primary_key" in allowed_keywords:
                opts.setdefault("primary_key", True)
            if "auto" in allowed_keywords:
                opts.setdefault("auto", True)
        if prefix == "Opt" and "nullable" in allowed_keywords and "nullable" not in opts:
            opts.setdefault("nullable", True)

        # Only allow known keywords
        for k in opts:
            if k not in allowed_keywords:
                raise TypeError(
                    f"{prefix}[{getattr(item, '__name__', repr(item))}]() "
                    + f"got unexpected keyword argument '{k}'"
                )
        self._options = opts

    name = f"{prefix}[{getattr(item, '__name__', repr(item))}]"
    bases = (cls,)
    marker_origin: type[Marker[OptAttrValue]] = PK if cls is Req and prefix == "PK" else cls
    namespace: dict[str, Any] = {
        "__origin__": marker_origin,
        "__args__": (item,),
        "__init__": __init__,
        "_type_arg_": item,
        "_field_opts_": base_opts,
        "_excluded_opts_": set(exclude_opts),
        "_options": {},  # populated by __init__
    }
    marker_cls = type(name, bases, namespace)
    return cast("type[FT]", marker_cls)


# Shared helper for Set/Single relation marker class creation
def relation_class_getitem[RT: Relation[Entity | None], T](
    cls: type[RT],
    item: type[T],
    prefix: str,
) -> type[RT]:

    # Build the allowed keyword set from RelationOpts plus the kind-specific opts
    base_relation_opts = RelationOpts()
    allowed_keywords: set[str] = set(base_relation_opts.__dataclass_fields__.keys())
    if issubclass(cls, Single):
        allowed_keywords.update(SingleOpts.__dataclass_fields__.keys())
    elif issubclass(cls, Set):
        allowed_keywords.update(SetOpts.__dataclass_fields__.keys())

    def __init__(self: RT, *args: Any, **kwargs: Any) -> None:
        opts: dict[str, Any] = {}
        for k, v in kwargs.items():
            if k not in allowed_keywords:
                raise TypeError(f"{name}() got unexpected keyword argument '{k}'")
            opts[k] = v
        if prefix == "PK" and "primary_key" in allowed_keywords:
            opts.setdefault("primary_key", True)
        self._options = opts

    name = f"{prefix}[{getattr(item, '__name__', repr(item))}]"
    bases = cast("tuple[type[Marker[Entity | None]], ...]", (cls,))
    marker_origin = cast(
        "type[Marker[Entity | None]]", PK if issubclass(cls, Single) and prefix == "PK" else cls
    )
    namespace: dict[str, Any] = {
        "__origin__": marker_origin,
        "__args__": (item,),
        "__init__": __init__,
        "_type_arg_": item,
        "_options": {},  # populated by __init__
    }
    marker_cls = type(name, bases, namespace)
    return cast("type[RT]", marker_cls)


class Marker[T]:
    """Generic base for all field and relation markers.

    At class-definition time :class:`~nextorm.entity.EntityMeta` inspects every
    annotation whose type is a ``Marker`` subclass and converts it into the
    appropriate descriptor (``FieldDescriptor``, ``SingleDescriptor``, …).

    Subscripting a marker class (e.g. ``Req[str]``) returns a new subclass with
    ``__origin__`` pointing back to the base marker class so that ``EntityMeta``
    can identify the marker kind, and with a custom ``__init__`` that validates
    and stores the field options passed at the call site.

    You should never need to subclass ``Marker`` directly — use :class:`Req`,
    :class:`Opt`, :class:`PK`, :class:`Single`, :class:`Set`, or :class:`Local`
    instead.
    """

    __origin__: type[Marker[OptAttrValue | Entity]]
    __args__: tuple[type, ...]

    _options: dict[str, Any]

    def __init__(self, **options: Any) -> None:
        self._options = options


# ---------------------------------------------------------------------------
# Primary key marker
# ---------------------------------------------------------------------------


class PK[T: PkValue](Marker[T]):
    """Primary-key marker for a field or a relation.

    **Scalar primary keys** — subscript with a scalar type:

    .. code-block:: python

        class User(Entity):
            id: PK[int]  # auto-increment integer (default)
            id: PK[uuid7]  # time-ordered UUID v7, Python-generated
            id: PK[uuid4]  # random UUID v4, Python-generated
            id: PK[ulid]  # ULID, Python-generated
            id: PK[str]  # user-assigned string PK

    **UUID / ULID primary keys** — ``PK[uuid7]``, ``PK[uuid4]``, and ``PK[ulid]``
    automatically set ``uuid_auto`` to ``"v7"``, ``"v4"``, or ``"ulid"``
    respectively, so NextORM generates a new value in Python before every INSERT.
    To assign the value yourself, pass ``uuid_auto=None`` explicitly:

    .. code-block:: python

        class Event(Entity):
            id: PK[uuid7]  # auto-generated UUID v7


        class EventManual(Entity):
            id: PK[uuid7] = PK(uuid_auto=None)  # you must set id before INSERT

    **User-assigned integer PK** (disable auto-increment):

    .. code-block:: python

        class Item(Entity):
            id: Req[int] = Req(primary_key=True, auto=False)

    **Relation primary keys** — subscript with an :class:`~nextorm.entity.Entity`
    subclass to declare a FK column that is also the primary key (typical for
    one-to-one extension tables):

    .. code-block:: python

        class UserProfile(Entity):
            user: PK[User]  # FK to User.id that is also this table's PK

    When no ``PK`` field (and no :func:`PrimaryKey` directive) is declared,
    :class:`~nextorm.entity.EntityMeta` automatically injects an ``id: PK[int]``
    auto-increment column.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._options = kwargs

    @overload
    def __get__(self, obj: None, owner: type) -> ColumnExpr: ...
    @overload
    def __get__(self, obj: Any, owner: type | None) -> T: ...

    def __get__(self, obj: Any, owner: type | None = None) -> T | ColumnExpr:
        raise NotImplementedError

    @overload
    def __set__(self, obj: Any, value: T) -> None: ...
    @overload
    def __set__(self, obj: Any, value: FieldSpec) -> None: ...

    def __set__(self, obj: Any, value: T | FieldSpec) -> None:
        raise NotImplementedError

    def __class_getitem__(cls, item: type[T]) -> type[PK[T]]:
        from nextorm.entity import Entity

        # If item is a subclass of Entity, treat as relation marker, else as field marker
        if issubclass(item, Entity):
            # Use Relation as the base for the relation marker factory
            return cast("type[PK[T]]", relation_class_getitem(Single, item, "PK"))
        else:
            # Use Field as the base for the field marker factory
            return cast("type[PK[T]]", field_class_getitem(Req, item, "PK"))


# ---------------------------------------------------------------------------
# Public field markers
# ---------------------------------------------------------------------------
# Type checkers see these as proper descriptors:
#   - Class-level access  (e.g. ``Product.name``)  → ColumnExpr  (query building)
#   - Instance-level access (e.g. ``product.name``) → T           (the actual value)
#
# At *runtime* EntityMeta replaces each annotated attribute with a
# FieldDescriptor, so these __get__/__set__ implementations are never invoked.
# They exist purely for the type checker's benefit.
#
# Using the Python 3.12 ``class Cls[T]:`` generic syntax:
#   ``PK[int].__origin__ is PK``, ``Req[str].__origin__ is Req``, etc.
# EntityMeta detects fields by checking ``__origin__`` against these classes,
# the same way it previously checked against TypeAliasType objects.


class Field[T](Marker[T]):
    """Generic base for all persistent scalar-field markers (:class:`Req`, :class:`Opt`).

    Provides the descriptor protocol stubs seen by the type checker:

    - Class-level access (``Product.name``) → :class:`~nextorm.expr.ColumnExpr`
      for use in query predicates.
    - Instance-level access (``product.name``) → the stored value of type ``T``.

    At runtime :class:`~nextorm.entity.EntityMeta` replaces every ``Field``
    annotation with a :class:`FieldDescriptor`, so these ``__get__``/``__set__``
    implementations are never actually called.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._options = kwargs

    @overload
    def __get__(self, obj: None, owner: type) -> ColumnExpr: ...
    @overload
    def __get__(self, obj: Any, owner: type | None) -> T: ...
    def __get__(self, obj: Any, owner: type | None = None) -> T | ColumnExpr:
        raise NotImplementedError

    def __set__(self, obj: Any, value: object) -> None:
        raise NotImplementedError


class Req[T: AttrValue](Field[T]):
    """Required (non-nullable) persistent field.

    The column is declared ``NOT NULL`` in DDL.  Subscript with the Python type
    to get a specialised marker class that validates keyword arguments:

    .. code-block:: python

        class Product(Entity):
            name: Req[str]
            price: Req[float]
            stock: Req[int]

    **Passing options** — call the subscripted marker as the default value:

    .. code-block:: python

        class Product(Entity):
            name: Req[str] = Req[str](max_len=120, unique=True)
            stock: Req[int] = Req[int](default=0, min=0)
            slug: Req[str] = Req[str](unique=True, index=True)

    **Type-specific positional shorthand** (first positional arg maps to the
    primary type option):

    .. code-block:: python

        name: Req[str](120)  # max_len=120
        score: Req[int](32)  # size=32  (column bit-width)
        price: Req[Decimal](10, 2)  # precision=10, scale=2

    Accepted keyword arguments depend on the subscript type and are validated at
    class-definition time.  See the type-specific ``*TypeOpts`` dataclasses in
    ``fields.py`` for the full per-type option lists.

    **Auto-generated non-PK UUID fields** — subscript with a UUID sentinel type
    (``uuid7``, ``uuid4``, or ``ulid``) and add ``unique=True``; a new value is
    generated in Python before every INSERT.  ``uuid_auto`` is derived
    automatically from the sentinel type (``"v7"``, ``"v4"``, or ``"ulid"``).
    To supply the UUID yourself instead, pass ``uuid_auto=None`` explicitly:

    .. code-block:: python

        class Invitation(Entity):
            token: Req[uuid7] = Req[uuid7](unique=True)  # uuid_auto="v7" — auto-generated
            manual: Req[uuid7] = Req[uuid7](unique=True, uuid_auto=None)  # you set it
            recipient: Single[User]

    For non-unique, non-PK UUID fields ``uuid_auto`` is always ``None`` regardless
    of what is passed — auto-generation only applies to PK and unique columns.
    """

    def __class_getitem__(cls, item: type[T]) -> type[Req[T]]:
        return field_class_getitem(cls, item, "Req")


class Opt[T: OptAttrValue](Field[T]):
    """Optional (nullable) persistent field — value may be ``None``.

    The column is declared ``NULL`` in DDL for non-string types.
    For ``Opt[str]`` and ``Opt[LongStr]`` the column is ``NOT NULL``
    by default (empty string is used for ``None`` values) – pass
    ``nullable=True`` explicitly to allow SQL ``NULL``:

    .. code-block:: python

        class Article(Entity):
            subtitle: Opt[str]  # NOT NULL, empty string allowed
            description: Opt[str] = Opt[str](nullable=True)  # NULLable
            published_at: Opt[datetime]  # NULLable (non-string default)

    All options accepted by :class:`Req` are available on ``Opt`` as well,
    except ``primary_key`` (use :class:`PK` for that) and ``auto`` (not
    meaningful for optional fields).
    """

    def __class_getitem__(cls, item: type[T]) -> type[Opt[T]]:
        return field_class_getitem(cls, item, "Opt")


# ---------------------------------------------------------------------------
# Relation markers
# ---------------------------------------------------------------------------


class Relation[E: Entity | None](Marker[E]):
    """Generic base for all relation markers (Set, Single)."""

    pass


class Single[E: Entity | None](Relation[E]):
    """Many-to-one (or one-to-one) FK relation attribute.

    Use ``Single[Other]`` for a **required** (NOT NULL) FK with ON DELETE CASCADE.
    Use ``Single[Other | None]`` for an **optional** (NULLABLE) FK with ON DELETE
    SET NULL.

    **Many-to-one** — only one side uses ``Single``; the other side uses
    :class:`Set` or has no back-reference:

    .. code-block:: python

        class Comment(Entity):
            post: Single[Post]  # required FK, cascade-delete
            author: Single[User | None]  # nullable FK, set-null on delete

    **One-to-one** — both sides declare ``Single``; NextORM adds a ``UNIQUE``
    constraint on the FK column and infers the owning side automatically:

    .. code-block:: python

        class UserProfile(Entity):
            user: Single[User]  # owning side (FK column here)


        class User(Entity):
            profile: Single[UserProfile] = Single[UserProfile](owner=False)

    **Options** — pass as keyword arguments to the subscripted marker:

    .. code-block:: python

        author: Single[User](fk_name="fk_comment_author", nullable=True)
        dept: Single[Dept](column="department_id", cascade_delete=False)

    Available options:

    - ``nullable`` (``bool``, default ``False``) — make the FK column NULLable.
    - ``column`` (``str``) — override the FK column name (default: ``{attr}_id``).
    - ``columns`` (``list[str]``) — composite FK column names.
    - ``fk_name`` (``str``) — override the FK constraint name in DDL.
    - ``cascade_delete`` (``bool | None``) — ``True`` force CASCADE, ``False``
      force RESTRICT, ``None`` (default) auto-derive from ``nullable``.
    - ``owner`` (``bool | None``) — one-to-one only: ``True`` = this side owns
      the FK column; ``False`` = this is the non-owning back-reference;
      ``None`` (default) = auto-detect.
    - ``reverse`` (``str``) — name of the reverse relation on the target entity.

    Class-level access (``Comment.author``) returns a
    :class:`~nextorm.expr.ColumnExpr` for the FK column for use in query
    predicates.  Instance-level access lazily loads and caches the related entity.
    """

    @overload
    def __get__(self, obj: None, owner: type) -> ColumnExpr: ...
    @overload
    def __get__(self, obj: Any, owner: type | None) -> E: ...
    def __get__(self, obj: Any, owner: type | None = None) -> E | ColumnExpr:
        raise NotImplementedError

    def __set__(self, obj: Any, value: E) -> None:
        raise NotImplementedError

    def __class_getitem__(cls, item: type[E]) -> type[Single[E]]:
        return relation_class_getitem(cls, item, "Single")


class Set[E: Entity](Relation[E]):
    """One-to-many or many-to-many collection relation attribute.

    **One-to-many** — declare ``Set[Child]`` here and ``Single[Parent]`` on the
    child entity:

    .. code-block:: python

        class Post(Entity):
            comments: Set[Comment]  # one-to-many back-reference


        class Comment(Entity):
            post: Single[Post]

    **Many-to-many** — declare ``Set[Other]`` on **both** entities; NextORM
    infers a join table automatically:

    .. code-block:: python

        class Student(Entity):
            courses: Set[Course]


        class Course(Entity):
            students: Set[Student]

    **Options** — pass as keyword arguments to the subscripted marker:

    .. code-block:: python

        courses: Set[Course](table="enrollment", reverse="students")

    Available options:

    - ``table`` (``str``) — override the M2M join table name.
    - ``reverse`` (``str``) — name of the reverse relation on the target entity.
    - ``reverse_column`` (``str``) — override the join table column that points
      back to this entity.
    - ``reverse_columns`` (``list[str]``) — composite version of the above.

    Class-level access (``Post.comments``) returns the descriptor itself (useful
    for schema introspection).  Instance-level access returns a
    :class:`~nextorm.collection.RelatedCollection` that supports iteration,
    ``add()``, ``remove()``, and prefetching.
    """

    @overload
    def __get__(self, obj: None, owner: type) -> Set[E]: ...
    @overload
    def __get__(self, obj: Any, owner: type | None) -> RelatedCollection[E]: ...
    def __get__(self, obj: Any, owner: type | None = None) -> Set[E] | RelatedCollection[E]:
        raise NotImplementedError

    def __set__(self, obj: Any, value: list[E]) -> None:
        raise NotImplementedError

    def __class_getitem__(cls, item: type[E]) -> type[Set[E]]:
        return relation_class_getitem(cls, item, "Set")


# ---------------------------------------------------------------------------
# Local marker
# ---------------------------------------------------------------------------


class Local[T](Marker[T]):
    """Local (transient) in-memory field — never persisted to the database.

    ``Local`` fields behave like regular attributes from the entity's
    perspective (readable, writable, supports defaults and validation) but are
    completely invisible to the database layer: they are excluded from all SQL
    queries, DDL generation, and migrations.

    **Basic usage** — initialise in :meth:`~nextorm.entity.Entity.after_load`:

    .. code-block:: python

        class Product(Entity):
            name: Req[str]
            price: Req[float]
            _display: Local[str]  # must be set before first read

            def after_load(self) -> None:
                self._display = f"{self.name} — ${self.price:.2f}"

    **With a default value** — the field is initialised automatically on
    construction, so ``after_load`` is not required:

    .. code-block:: python

        class Order(Entity):
            total: Req[float]
            _cache: Local[dict] = Local[dict](default=dict)  # factory
            _verified: Local[bool] = Local[bool](default=False)  # scalar

    **With validation** — ``py_check`` runs on every assignment:

    .. code-block:: python

        class Report(Entity):
            _score: Local[float] = Local[float](
                default=0.0,
                py_check=lambda v: 0.0 <= v <= 1.0,
            )

    Accessing an uninitialised ``Local`` field (no default, not yet assigned)
    raises :exc:`AttributeError`.

    Available options (passed to the subscripted marker):

    - ``default`` — scalar value or zero-argument callable used as the initial
      value.  Applied in :meth:`~nextorm.entity.Entity.__init__` before any
      kwargs are processed.
    - ``py_check`` — ``(value) -> bool`` validator; raises :exc:`ValueError`
      when it returns ``False``.
    """

    @overload
    def __get__(self, obj: None, owner: type) -> Local[T]: ...

    @overload
    def __get__(self, obj: Any, owner: type | None) -> T: ...

    def __get__(self, obj: Any, owner: type | None = None) -> T | Local[T]:
        raise NotImplementedError

    def __set__(self, obj: Any, value: T) -> None:
        raise NotImplementedError

    def __class_getitem__(cls, item: type[T]) -> type[Local[T]]:
        allowed_keywords = set(LocalOpts.__dataclass_fields__.keys())

        def __init__(self: Local[T], *args: Any, **kwargs: Any) -> None:
            for k in kwargs:
                if k not in allowed_keywords:
                    raise TypeError(
                        f"Local[{getattr(item, '__name__', repr(item))}]() "
                        f"got unexpected keyword argument '{k}'"
                    )
            self._options = kwargs

        name = f"Local[{getattr(item, '__name__', repr(item))}]"
        namespace: dict[str, Any] = {
            "__origin__": cls,
            "__args__": (item,),
            "__init__": __init__,
            "_type_arg_": item,
            "_options": {},
        }
        marker_cls = type(name, (cls,), namespace)
        return cast("type[Local[T]]", marker_cls)


# ---------------------------------------------------------------------------
# Internal specification dataclasses  (used by EntityMeta and the schema layer)
# ---------------------------------------------------------------------------

# Sentinel for "no default provided" — distinct from None
_MISSING: object = object()


@dataclass(frozen=True)
class FieldSpec:
    """Metadata that describes a single persistent column.

    :class:`~nextorm.entity.EntityMeta` creates a ``FieldSpec`` automatically
    for every annotated field.  You can set options through the marker call syntax
    (validated at class-definition time):

    .. code-block:: python

        class Product(Entity):
            name: Req[str] = Req[str](max_len=120, unique=True)
            price: Req[float] = Req[float](min=0.0)
            updated_at: Req[datetime] = FieldSpec(volatile=True, sql_default="CURRENT_TIMESTAMP")

    Parameters
    ----------
    auto:
        ``True`` for auto-increment integer PKs or UUID/ULID PKs generated by
        Python before INSERT.  Set automatically by :class:`PK`; rarely needed
        manually.
    autostrip:
        Strip leading/trailing whitespace on every string assignment (default
        ``False`` for ``LongStr``; ``True`` for plain ``str`` fields when set via
        :class:`StrTypeOpts`).
    column:
        Override the database column name.  Defaults to the attribute name.
    db_encoding:
        Database character encoding for the column, e.g. ``"utf8mb4"`` for full
        Unicode support on MariaDB.  Ignored on PostgreSQL and SQLite.
    default:
        Python-side default value or zero-argument callable.  Applied before
        ``INSERT`` when the field was not explicitly set.  Use ``_MISSING`` (the
        module-level sentinel) to indicate no default.
    dimensions:
        Number of dimensions for :class:`Vec` columns (e.g. ``384``, ``1536``).
        Set automatically when you write ``Req[Vec[384]]``.
    index:
        Create a single-column index on this column.
    lazy:
        Exclude the column from the main ``SELECT``; load its value on first
        access via a separate query.  Set automatically for :class:`LongStr`
        fields.
    max:
        Inclusive upper bound enforced in Python on every assignment.
    max_len:
        Maximum string length enforced in Python and reflected in DDL
        (``VARCHAR(n)``).
    min:
        Inclusive lower bound enforced in Python on every assignment.
    nullable:
        Allow SQL ``NULL``.  Set automatically for :class:`Opt` fields.
    precision:
        Total significant digits for ``DECIMAL``/``NUMERIC`` columns, or
        fractional-second precision for ``DATETIME``/``TIMESTAMP`` columns.
    primary_key:
        Mark this column as (part of) the primary key.
    py_check:
        Zero-argument callable ``(value) -> bool``; raises :exc:`ValueError`
        when it returns falsy.  Runs on every assignment.
    scale:
        Digits after the decimal point for ``DECIMAL``/``NUMERIC`` columns.
    size:
        Integer column bit-width — one of ``8``, ``16``, ``32``, or ``64``.
    sql_default:
        Raw SQL expression used in DDL ``DEFAULT``, e.g.
        ``"CURRENT_TIMESTAMP"``.  Not applied in Python — use ``default`` for
        that.
    sql_type:
        Override the inferred SQL type string, e.g. ``"JSONB"`` or
        ``"GEOMETRY"``.
    unique:
        Add a ``UNIQUE`` constraint on this column.
    unsigned:
        Add the ``UNSIGNED`` modifier (MariaDB only; ignored on other backends).
    uuid_auto:
        Python-side UUID/ULID generation strategy: ``"v7"``, ``"v4"``, or
        ``"ulid"``.  Set automatically by the :class:`PK` marker for UUID/ULID
        types.
    volatile:
        Exclude this column from ``UPDATE`` statements.  Use for columns whose
        value is maintained entirely by a database trigger or ``DEFAULT``
        expression.
    """

    auto: bool = False
    autostrip: bool = False  # strip leading/trailing whitespace on string assignment
    column: str | None = None
    db_encoding: str | None = None  # e.g. "utf8mb4" for full Unicode support on MariaDB
    default: Any = field(default_factory=lambda: _MISSING, compare=False)
    dimensions: int | None = None  # for Vec: number of vector dimensions (e.g. 384, 1536)
    index: bool = False
    lazy: bool = False  # deferred loading: field excluded from main SELECT; loaded on first access
    max: Any = field(
        default=None, compare=False
    )  # inclusive upper bound for numeric/comparable fields
    max_len: int | None = None
    min: Any = field(
        default=None, compare=False
    )  # inclusive lower bound for numeric/comparable fields
    nullable: bool = False
    precision: int | None = None  # for Decimal/NUMERIC: total significant digits
    primary_key: bool = False
    py_check: Callable[[object], bool] | None = field(
        default=None, compare=False
    )  # callable validator; receives value; raises/returns falsy on failure
    scale: int | None = None  # for Decimal/NUMERIC: digits after decimal point
    sql_default: str | None = None  # raw SQL expression for DDL DEFAULT, e.g. "CURRENT_TIMESTAMP"
    sql_type: str | None = None  # override inferred SQL type string, e.g. "JSONB"
    size: int | None = None  # for int: column bit width — 8, 16, 32, or 64
    unique: bool = False
    uuid_auto: str | None = None  # "v7", "v4", or "ulid" — Python-side auto-generation
    unsigned: bool = False  # UNSIGNED modifier (MariaDB); ignored on other providers
    volatile: bool = False  # excluded from UPDATE; value is set by a DB trigger

    @property
    def has_default(self) -> bool:
        """Return ``True`` when a default value or factory has been set."""
        return self.default is not _MISSING


class RelationKind:
    """Constants describing how a relation is stored.

    ``SET`` — ``Set[T]`` on one or both sides; one-to-many vs many-to-many is
    inferred during :meth:`~nextorm.database.Database.generate_mapping`.

    ``SINGLE`` — ``Single[T]`` on (at least) one side; becomes a foreign-key
    column.  When both sides carry ``Single``, a ``UNIQUE`` constraint is added
    to implement a one-to-one relation.
    """

    SET = "set"
    SINGLE = "single"


@dataclass(frozen=True)
class RelationSpec:
    """Metadata that describes a relation (FK or join-table) column.

    :class:`~nextorm.entity.EntityMeta` creates a ``RelationSpec`` automatically
    from :class:`Single` and :class:`Set` annotations.

    .. code-block:: python

        class Comment(Entity):
            post: Single[Post](fk_name="fk_comment_post", cascade_delete=True)

    .. note::

        ``kind`` and ``target`` are always filled in by ``EntityMeta`` from the
        annotation.

    Parameters — ``Single`` relations
    ----------------------------------
    nullable:
        ``True`` → NULLABLE FK column with ON DELETE SET NULL.
        ``False`` (default) → NOT NULL column with ON DELETE CASCADE.
    cascade_delete:
        ``True`` — force ON DELETE CASCADE regardless of ``nullable``.
        ``False`` — force ON DELETE RESTRICT regardless of ``nullable``.
        ``None`` (default) — derive automatically from ``nullable``.
    column:
        Override the FK column name.  Defaults to ``{attr_name}_id``.
    columns:
        Composite FK column names (list).  Mutually exclusive with ``column``.
    fk_name:
        Override the foreign-key constraint name in DDL.
    owner:
        One-to-one only.  ``True`` = this side owns the FK column (UNIQUE
        constraint added here).  ``False`` = non-owning back-reference.
        ``None`` (default) = auto-detect from ``nullable`` or alphabetical order.
    primary_key:
        ``True`` when the FK column is also the table's primary key.  Set
        automatically by :class:`PK` when subscripted with an entity type.

    Parameters — ``Set`` relations
    --------------------------------
    table:
        Override the many-to-many join table name.
    reverse_column:
        Override the join-table column that points back to the declaring entity.
    reverse_columns:
        Composite version of ``reverse_column``.

    Parameters — both kinds
    ------------------------
    reverse:
        Attribute name of the reverse relation on the target entity, used for
        explicit back-reference wiring when NextORM cannot infer it automatically.
    """

    kind: str = ""  # filled in by EntityMeta when used as class-level value
    target: type[Any] | str | None = (
        None  # filled in by EntityMeta; None when used as inline override
    )
    cascade_delete: bool | None = None  # None = auto-derive from nullable
    column: str | None = None  # Single only: DB column name for mapping this attribute
    columns: list[str] | None = None  # Single only: DB column names for mapping a composite attribute
    fk_name: str | None = None  # Single only: name of the foreign key constraint in the database
    nullable: bool = False  # Single only: nullable FK column
    owner: bool | None = None  # Single O2O only: explicit owning-side override
    primary_key: bool = False
    reverse: str | None = None
    reverse_column: str | None = None
    reverse_columns: list[str] | None = None
    table: str | None = None  # M2M only: override join table name


@dataclass(frozen=True)
class LocalSpec:
    """Metadata for a single local (transient) field.

    Created by :class:`~nextorm.entity.EntityMeta` from a :class:`Local`
    annotation and stored in ``cls._locals_``.  Never reaches the database
    layer — it exists solely to support Python-side behaviour (defaults and
    validation) that mirrors what :class:`FieldSpec` provides for persistent
    fields.

    Parameters
    ----------
    default:
        Value or zero-argument callable applied before the first assignment
        when the field was not supplied to ``__init__``.  If omitted, accessing
        an uninitialised ``Local`` field raises :exc:`AttributeError` until the
        field is assigned — typically inside :meth:`~nextorm.entity.Entity.after_load`.
    py_check:
        Callable ``(value) -> bool`` run on every assignment.  Raises
        :exc:`ValueError` when it returns ``False``.
    """

    default: Any = field(default_factory=lambda: _MISSING, compare=False)
    py_check: Callable[[object], bool] | None = field(default=None, compare=False)

    @property
    def has_default(self) -> bool:
        """Return ``True`` when a default value has been set."""
        return self.default is not _MISSING


@dataclass(frozen=True)
class CompositeConstraint:
    """Multi-column index, unique constraint, or primary key declared at class level.

    Do not instantiate directly — use :func:`composite_key`,
    :func:`composite_index`, or :func:`PrimaryKey` instead."""

    fields: tuple[str, ...]
    unique: bool = False
    primary_key: bool = False


def composite_key(*field_names: str) -> CompositeConstraint:
    """Declare a multi-column unique constraint (equivalent to ``UNIQUE (a, b)``).

    Place this inside the entity body as a class attribute::

        class Booking(Entity):
            slot: Req[int]
            room: Req[int]
            _ck_slot_room_ = composite_key("slot", "room")
    """
    return CompositeConstraint(fields=field_names, unique=True)


def composite_index(*field_names: str) -> CompositeConstraint:
    """Declare a multi-column non-unique index (equivalent to ``INDEX (a, b)``).

    Place this inside the entity body as a class attribute::

        class LogEntry(Entity):
            source: Req[str]
            level: Req[str]
            _idx_source_level_ = composite_index("source", "level")
    """
    return CompositeConstraint(fields=field_names, unique=False)


def PrimaryKey(*field_names: str) -> CompositeConstraint:  # noqa: N802
    """Declare a composite primary key spanning two or more fields.

    *field_names* may be scalar field names **or** relation names (``Single``
    relations whose FK column then becomes part of the PK). All referenced
    relations must be required (non-nullable).

    Place this inside the entity body as a class attribute::

        class OrderLine(Entity):
            order: Single[Order]
            product: Single[Product]
            quantity: Req[int]
            _pk_ = PrimaryKey("order", "product")


        class Enrollment(Entity):
            student_id: Req[int]
            course_id: Req[int]
            grade: Opt[str]
            _pk_ = PrimaryKey("student_id", "course_id")
    """
    return CompositeConstraint(fields=field_names, unique=True, primary_key=True)


# ---------------------------------------------------------------------------
# UUID / ULID value type and sentinel types
# ---------------------------------------------------------------------------


class ULID(str):
    """26-character Crockford base32 ULID value type.

    Used as the Python type for ``PK[ulid]`` fields.  ULID values are stored
    as ``CHAR(26)`` (MariaDB), ``TEXT`` (SQLite), or the native UUID column
    cast to base32 (not supported — stored as TEXT on all backends).

    A ``ULID`` instance is an ordinary string and sorts lexicographically in
    creation order (time-order), which is the key property of ULIDs.
    """

    pass


class uuid7:
    """Sentinel type for UUID v7 auto-generated primary keys.

    Use in entity annotations to declare a time-ordered, sortable UUID PK::

        class Event(Entity):
            id: PK[uuid7]

    The field is stored as ``uuid.UUID`` in Python and mapped to
    ``UUID`` (PostgreSQL), ``CHAR(36)`` (MariaDB), or ``TEXT`` (SQLite).
    A UUID v7 value is auto-generated before every INSERT if the field is
    not already set.
    """

    pass


class uuid4:
    """Sentinel type for UUID v4 auto-generated primary keys.

    Use like ``PK[uuid4]``.  Same storage as ``uuid7`` but uses random
    UUID v4 generation (not time-ordered).
    """

    pass


class ulid:
    """Sentinel type for ULID auto-generated primary keys.

    Use like ``PK[ulid]``.  The field is stored as a :class:`ULID` string
    (26-character Crockford base32) mapped to ``CHAR(26)`` in DDL.
    """

    pass


class LongStr(str):
    """Sentinel type for large text columns.

    Maps to ``LONGTEXT`` (MariaDB), ``TEXT`` (PostgreSQL / SQLite).  Use when
    the content may exceed the ~65 KB limit of a standard MariaDB ``TEXT``
    column:

    .. code-block:: python

        class Article(Entity):
            body: Req[LongStr]

    By default ``LongStr`` fields are *lazy* — they are omitted from the main
    ``SELECT`` and fetched on first access via a separate query.  To load
    eagerly, pass ``lazy=False`` explicitly:

    .. code-block:: python

        class Article(Entity):
            body: Req[LongStr] = Req[LongStr](lazy=False)
    """

    pass


class Json:
    """Sentinel type for JSON columns.

    Maps to ``JSONB`` (PostgreSQL), ``JSON`` (MariaDB), ``TEXT`` (SQLite).
    Values are stored as Python :class:`dict` / :class:`list` objects and
    serialised/deserialised automatically::

        class Config(Entity):
            data: Req[Json]
    """

    pass


class DateTimeTz:
    """Sentinel type for timezone-aware datetime columns.

    Maps to ``TIMESTAMPTZ`` (PostgreSQL), ``DATETIME`` (MariaDB, assumes UTC
    session), ``TEXT`` ISO 8601 (SQLite).

    PostgreSQL: psycopg / asyncpg automatically return a timezone-aware
    :class:`~datetime.datetime` object.  For MariaDB and SQLite the application
    is responsible for serialising to/from UTC::

        class Event(Entity):
            start_at: Req[DateTimeTz]
    """

    pass


class Vec:
    """Parameterized sentinel type for fixed-dimension vector columns.

    Maps to ``vector(n)`` (PostgreSQL with pgvector extension), ``TEXT``
    (MariaDB / SQLite — JSON-serialised list).

    Specify the dimension by subscripting::

        class Article(Entity):
            embedding: Req[Vec[384]]

    Alternatively use ``FieldSpec(dimensions=n)`` explicitly::

        class Article(Entity):
            embedding: Req[Vec] = FieldSpec(dimensions=384)
    """

    _dimensions_: int | None = None

    def __class_getitem__(cls, n: int) -> type[Vec]:
        """Return a Vec subclass carrying the given dimension count."""
        return type(f"Vec{n}", (Vec,), {"_dimensions_": n})


# Maps each sentinel type to (storage Python type, uuid_auto kind string)
_UUID_SENTINEL_MAP: dict[type, tuple[type, str]] = {
    uuid7: (_uuid_stdlib.UUID, "v7"),
    uuid4: (_uuid_stdlib.UUID, "v4"),
    ulid: (ULID, "ulid"),
}


# ---------------------------------------------------------------------------
# UUID / ULID generation helpers
# ---------------------------------------------------------------------------


def _generate_uuid7() -> _uuid_stdlib.UUID:
    """Generate a time-ordered UUID v7 (RFC 9562).

    Uses ``uuid.uuid7()`` from the Python 3.13 stdlib when available;
    falls back to a pure-Python implementation on Python 3.12.
    """
    if hasattr(_uuid_stdlib, "uuid7"):  # Python 3.13+
        return _uuid_stdlib.uuid7()  # type: ignore[no-any-return]
    # Python 3.12 fallback — manual bit-packing per RFC 9562 §5.7:
    #   bits 127-80 : 48-bit unix_ts_ms
    #   bits 79-76  : version = 7
    #   bits 75-64  : rand_a  (12 random bits)
    #   bits 63-62  : variant = 0b10
    #   bits 61-0   : rand_b  (62 random bits)
    ts_ms = int(time_time() * 1000) & 0xFFFF_FFFF_FFFF  # 48 bits
    rand_bytes = int.from_bytes(os.urandom(10), "big")  # 80 random bits
    rand_a = (rand_bytes >> 68) & 0xFFF  # top 12 bits
    rand_b = rand_bytes & 0x3FFF_FFFF_FFFF_FFFF  # bottom 62 bits
    int_val = (ts_ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return _uuid_stdlib.UUID(int=int_val)


_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _generate_ulid() -> ULID:
    """Generate a ULID — 26-character Crockford base32 string.

    Layout (128 bits):
    - bits 127-80 : 48-bit unix_ts_ms
    - bits 79-0   : 80 random bits
    """
    ts_ms = int(time_time() * 1000) & 0xFFFF_FFFF_FFFF  # 48 bits
    rand = int.from_bytes(os.urandom(10), "big") & 0xFFFF_FFFF_FFFF_FFFF_FFFF  # 80 bits
    value = (ts_ms << 80) | rand  # 128-bit integer
    # Encode as 26 base32 characters (Crockford alphabet, big-endian)
    chars: list[str] = []
    for _ in range(26):
        chars.append(_ULID_ALPHABET[value & 0x1F])
        value >>= 5
    return ULID("".join(reversed(chars)))


# ---------------------------------------------------------------------------
# Value serialisation helper — used by the database layer
# ---------------------------------------------------------------------------


def _serialize_value(value: Any) -> Any:
    """Coerce a Python value to a form accepted by all DB drivers.

    - :class:`enum.Enum` instances → ``.value``  (str / int / …)
    - All other values are returned unchanged; driver-level adapters (e.g.
      ``sqlite3.register_adapter``) handle the remaining conversions.
    """
    if isinstance(value, Enum):
        return value.value
    return value
