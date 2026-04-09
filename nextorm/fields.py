"""Field type aliases and metadata for NextORM entity definitions."""

from __future__ import annotations

import dataclasses
import os
import time
import uuid as _uuid_stdlib
from typing import TYPE_CHECKING, Any, overload

if TYPE_CHECKING:
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

# ---------------------------------------------------------------------------
# Internal specification dataclasses  (used by EntityMeta and the schema layer)
# ---------------------------------------------------------------------------

# Sentinel for "no default provided" — distinct from None
_MISSING: object = object()


@dataclasses.dataclass(frozen=True)
class FieldSpec:
    """Metadata that describes a persistent field.

    Created internally by :class:`~nextorm.entity.EntityMeta` based on which
    type alias was used in the annotation (``PK``, ``Req``, ``Opt``, …).
    Direct construction is only needed when customising field behaviour::

        from nextorm.fields import FieldSpec


        class MyEntity(Entity):
            slug: Req[str]  # EntityMeta creates FieldSpec() automatically


        # With custom options:
        # slug = FieldSpec(unique=True, max_len=64)
    """

    primary_key: bool = False
    auto: bool = False
    nullable: bool = False
    unique: bool = False
    index: bool = False
    max_len: int | None = None
    column: str | None = None
    default: Any = dataclasses.field(default_factory=lambda: _MISSING, compare=False)
    sql_default: str | None = None  # raw SQL expression for DDL DEFAULT, e.g. "CURRENT_TIMESTAMP"
    sql_type: str | None = None  # override inferred SQL type string, e.g. "JSONB"
    volatile: bool = False  # excluded from UPDATE; value is set by a DB trigger
    uuid_auto: str | None = None  # "v7", "v4", or "ulid" — Python-side auto-generation
    precision: int | None = None  # for Decimal/NUMERIC: total significant digits
    scale: int | None = None  # for Decimal/NUMERIC: digits after decimal point
    unsigned: bool = False  # UNSIGNED modifier (MariaDB); ignored on other providers
    size: int | None = None  # for int: column bit width — 8, 16, 32, or 64
    dimensions: int | None = None  # for Vec: number of vector dimensions (e.g. 384, 1536)
    lazy: bool = False  # deferred loading: field excluded from main SELECT; loaded on first access
    py_check: Any = dataclasses.field(
        default=None, compare=False
    )  # callable validator; receives value; raises/returns falsy on failure
    autostrip: bool = False  # strip leading/trailing whitespace on string assignment
    min: Any = dataclasses.field(
        default=None, compare=False
    )  # inclusive lower bound for numeric/comparable fields
    max: Any = dataclasses.field(
        default=None, compare=False
    )  # inclusive upper bound for numeric/comparable fields

    @property
    def has_default(self) -> bool:
        """Return ``True`` when a default value or factory has been set."""
        return self.default is not _MISSING


@dataclasses.dataclass(frozen=True)
class LocalSpec:
    """Marker for local (transient) fields that are never persisted.

    Local fields are stored in ``instance.__dict__`` only.  They are never
    included in SQL, schema DDL, or migrations, and accessing them never
    triggers a database query — making them safe to initialise inside
    lifecycle hooks such as ``after_load``.
    """


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


@dataclasses.dataclass(frozen=True)
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


@dataclasses.dataclass(frozen=True)
class RelationSpec:
    """Metadata that describes a relation field.

    For ``Single`` relations:

    - ``nullable=False`` (default): NOT NULL column, ON DELETE CASCADE
    - ``nullable=True``: NULLABLE column, ON DELETE SET NULL
    - ``cascade_delete=True``: override to CASCADE regardless of nullable
    - ``cascade_delete=False``: override to RESTRICT regardless of nullable
    - ``cascade_delete=None`` (default): auto-derive from nullable
    - ``owner=True``: explicitly mark this ``Single`` as the owning side in a
      one-to-one pair (creates FK + UNIQUE here).
    - ``owner=False``: explicitly mark this ``Single`` as the non-owning
      back-reference (no FK column generated here).
    - ``owner=None`` (default): auto-detect from nullable / alphabetical order.
    - ``column=None``: override the FK column name (defaults to
      ``reverse_column`` or ``{relation_name}_id``).
    - ``columns=None``: for composite FK, override the list of FK column names
      (defaults to ``[reverse_column + "_id"]`` or ``[{relation_name}_id]``).
    """

    kind: str = ""  # filled in by EntityMeta when used as class-level value
    target: type[Any] | str | None = (
        None  # filled in by EntityMeta; None when used as inline override
    )
    reverse: str | None = None
    nullable: bool = False  # Single only: nullable FK column
    cascade_delete: bool | None = None  # None = auto-derive from nullable
    table: str | None = None  # M2M only: override join table name
    owner: bool | None = None  # Single O2O only: explicit owning-side override
    column: str | None = None  # Single only: override FK column name (reverse_column)
    columns: list[str] | None = None  # Single only: composite FK column names override


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


class uuid7:  # noqa: N801
    """Sentinel type for UUID v7 auto-generated primary keys.

    Use in entity annotations to declare a time-ordered, sortable UUID PK::

        class Event(Entity):
            id: PK[uuid7]

    The field is stored as ``uuid.UUID`` in Python and mapped to
    ``UUID`` (PostgreSQL), ``CHAR(36)`` (MariaDB), or ``TEXT`` (SQLite).
    A UUID v7 value is auto-generated before every INSERT if the field is
    not already set.
    """


class uuid4:  # noqa: N801
    """Sentinel type for UUID v4 auto-generated primary keys.

    Use like ``PK[uuid4]``.  Same storage as ``uuid7`` but uses random
    UUID v4 generation (not time-ordered).
    """


class ulid:  # noqa: N801
    """Sentinel type for ULID auto-generated primary keys.

    Use like ``PK[ulid]``.  The field is stored as a :class:`ULID` string
    (26-character Crockford base32) mapped to ``CHAR(26)`` in DDL.
    """


class LongStr:  # noqa: N801
    """Sentinel type for large text columns.

    Maps to ``LONGTEXT`` on MariaDB and ``TEXT`` on PostgreSQL / SQLite.
    Use when the content may exceed the ~65 KB limit of MariaDB ``TEXT``
    (which is limited to 65,535 bytes)::

        class Article(Entity):
            body: Req[LongStr]

    By default ``LongStr`` fields are *lazy* — they are omitted from the main
    ``SELECT`` and loaded on first access.  Override with
    ``body: Req[LongStr] = FieldSpec(lazy=False)`` to load eagerly.
    """


class Json:  # noqa: N801
    """Sentinel type for JSON columns.

    Maps to ``JSONB`` (PostgreSQL), ``JSON`` (MariaDB), ``TEXT`` (SQLite).
    Values are stored as Python :class:`dict` / :class:`list` objects and
    serialised/deserialised automatically::

        class Config(Entity):
            data: Req[Json]
    """


class DateTimeTz:  # noqa: N801
    """Sentinel type for timezone-aware datetime columns.

    Maps to ``TIMESTAMPTZ`` (PostgreSQL), ``DATETIME`` (MariaDB, assumes UTC
    session), ``TEXT`` ISO 8601 (SQLite).

    PostgreSQL: psycopg / asyncpg automatically return a timezone-aware
    :class:`~datetime.datetime` object.  For MariaDB and SQLite the application
    is responsible for serialising to/from UTC::

        class Event(Entity):
            start_at: Req[DateTimeTz]
    """


class Vec:  # noqa: N801
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
    ts_ms = int(time.time() * 1000) & 0xFFFF_FFFF_FFFF  # 48 bits
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
    ts_ms = int(time.time() * 1000) & 0xFFFF_FFFF_FFFF  # 48 bits
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

import enum as _enum_stdlib  # noqa: E402


def _serialize_value(value: Any) -> Any:
    """Coerce a Python value to a form accepted by all DB drivers.

    - :class:`enum.Enum` instances → ``.value``  (str / int / …)
    - All other values are returned unchanged; driver-level adapters (e.g.
      ``sqlite3.register_adapter``) handle the remaining conversions.
    """
    if isinstance(value, _enum_stdlib.Enum):
        return value.value
    return value


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


class PK[T]:
    """Primary-key field — auto-generated integer by default."""

    @overload
    def __get__(self, obj: None, owner: type) -> ColumnExpr: ...

    @overload
    def __get__(self, obj: Any, owner: type | None) -> T: ...

    def __get__(self, obj: Any, owner: type | None = None) -> Any:  # pragma: no cover
        raise NotImplementedError

    def __set__(self, obj: Any, value: T) -> None:  # pragma: no cover
        raise NotImplementedError


class Req[T]:
    """Required (non-nullable) field."""

    @overload
    def __get__(self, obj: None, owner: type) -> ColumnExpr: ...

    @overload
    def __get__(self, obj: Any, owner: type | None) -> T: ...

    def __get__(self, obj: Any, owner: type | None = None) -> Any:  # pragma: no cover
        raise NotImplementedError

    def __set__(self, obj: Any, value: T) -> None:  # pragma: no cover
        raise NotImplementedError


class Opt[T]:
    """Optional (nullable) field — value may be ``None``."""

    @overload
    def __get__(self, obj: None, owner: type) -> ColumnExpr: ...

    @overload
    def __get__(self, obj: Any, owner: type | None) -> T | None: ...

    def __get__(self, obj: Any, owner: type | None = None) -> Any:  # pragma: no cover
        raise NotImplementedError

    def __set__(self, obj: Any, value: T | None) -> None:  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Relation and local markers
# ---------------------------------------------------------------------------


class Local[T]:
    """Local (transient) in-memory field — never persisted to the database.

    Use alongside the ``after_load`` lifecycle hook to initialise computed
    state when an entity is loaded from the DB.
    """

    @overload
    def __get__(self, obj: None, owner: type) -> Local[T]: ...

    @overload
    def __get__(self, obj: Any, owner: type | None) -> T: ...

    def __get__(self, obj: Any, owner: type | None = None) -> Any:  # pragma: no cover
        raise NotImplementedError

    def __set__(self, obj: Any, value: T) -> None:  # pragma: no cover
        raise NotImplementedError


class Set[T: Entity]:
    """Collection relation attribute — used for both one-to-many and many-to-many.

    One-to-many:  declare ``Set[Child]`` here and ``Single[Parent]`` on the child.
    Many-to-many: declare ``Set[Other]`` on **both** entities; nextorm infers a join table.

    Class-level access returns the descriptor itself (for use with ``reverse=`` and
    schema building).  Instance-level access returns [future] a :class:`RelatedCollection`.
    """

    @overload
    def __get__(self, obj: None, owner: type) -> Set[T]: ...

    @overload
    def __get__(self, obj: Any, owner: type | None) -> RelatedCollection[T]: ...

    def __get__(self, obj: Any, owner: type | None = None) -> Any:  # pragma: no cover
        raise NotImplementedError

    def __set__(self, obj: Any, value: list[T]) -> None:  # pragma: no cover
        raise NotImplementedError


class Single[T]:
    """Single-entity relation attribute.

    Use ``Single[Other]`` for a required (NOT NULL) FK with CASCADE delete.
    Use ``Single[Other | None]`` for an optional (NULLABLE) FK with SET NULL.

    When the other entity *also* declares ``Single`` pointing back, nextorm
    infers a one-to-one relationship and adds a ``UNIQUE`` constraint on the
    FK column.  When only one side uses ``Single`` (the other uses ``Set[...]``
    or has no back-reference), a plain many-to-one FK is generated.

    Class-level access returns a :class:`~nextorm.expr.ColumnExpr` for use
    in query predicates.
    """

    @overload
    def __get__(self, obj: None, owner: type) -> ColumnExpr: ...

    @overload
    def __get__(self, obj: Any, owner: type | None) -> T: ...

    def __get__(self, obj: Any, owner: type | None = None) -> Any:  # pragma: no cover
        raise NotImplementedError

    def __set__(self, obj: Any, value: T) -> None:  # pragma: no cover
        raise NotImplementedError
