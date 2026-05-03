"""Tests for SingleDescriptor, SetDescriptor, and _EntityIterator."""

from __future__ import annotations

from typing import Any

import pytest

from nextorm.database import Database
from nextorm.entity import (
    _LAZY_SENTINEL,
    _UNSET,
    Entity,
    RelationInfo,
    SetDescriptor,
    SingleDescriptor,
    _EntityIterator,
)
from nextorm.fields import Local, RelationKind, RelationSpec, Req, Set, Single
from nextorm.session import db_session

# ---------------------------------------------------------------------------
# Entity definitions used across tests
# ---------------------------------------------------------------------------


class DescAuthor(Entity):
    name: Req[str]
    books: Set["DescBook"]  # noqa: UP037


class DescBook(Entity):
    title: Req[str]
    author: Single[DescAuthor | None]


# ---------------------------------------------------------------------------
# Entities for FieldDescriptor validation (autostrip, min, max, py_check)
# ---------------------------------------------------------------------------


class _VFEntity(Entity):
    """Entity with validated/transformed fields for descriptor tests."""

    trimmed: Req[str] = Req(autostrip=True)
    score: Req[int] = Req(min=0, max=100)
    code: Req[str] = Req(py_check=lambda v: len(v) >= 3)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]


class _LFEntity(Entity):
    """Entity with one lazy field for FieldDescriptor lazy-load tests."""

    name: Req[str]
    bio: Req[str] = Req(lazy=True)


# ---------------------------------------------------------------------------
# _UNSET sentinel
# ---------------------------------------------------------------------------


def test_unset_sentinel_is_not_none() -> None:
    assert _UNSET is not None


def test_unset_distinct_from_any_other_value() -> None:
    assert _UNSET is not _UNSET.__class__()


# ---------------------------------------------------------------------------
# _EntityIterator
# ---------------------------------------------------------------------------


def test_entity_iterator_holds_class() -> None:
    it = _EntityIterator(DescAuthor)
    assert it.entity_cls is DescAuthor


def test_entity_iterator_is_own_iter() -> None:
    it = _EntityIterator(DescBook)
    assert iter(it) is it


def test_entity_iterator_next_raises_stop_iteration() -> None:
    it = _EntityIterator(DescAuthor)
    with pytest.raises(StopIteration):
        next(it)


def test_entity_meta_iter_returns_entity_iterator() -> None:
    it = iter(DescAuthor)
    assert isinstance(it, _EntityIterator)
    assert it.entity_cls is DescAuthor


# ---------------------------------------------------------------------------
# SetDescriptor — class-level access
# ---------------------------------------------------------------------------


def test_set_descriptor_class_access_returns_self() -> None:
    """Accessing a Set attribute on the class should return the SetDescriptor."""
    descriptor = DescAuthor.__dict__["books"]
    assert isinstance(descriptor, SetDescriptor)
    # Access via class attribute protocol
    result = DescAuthor.books
    assert isinstance(result, SetDescriptor)


def test_set_descriptor_has_correct_name() -> None:
    d = DescAuthor.__dict__["books"]
    assert isinstance(d, SetDescriptor)
    assert d.name == "books"


# ---------------------------------------------------------------------------
# SetDescriptor — instance-level access
# ---------------------------------------------------------------------------


def test_set_descriptor_instance_returns_related_collection() -> None:
    from nextorm.collection import RelatedCollection

    obj = DescAuthor.__new__(DescAuthor)
    vars(obj)["_db_"] = None
    col = obj.books
    assert isinstance(col, RelatedCollection)


def test_set_descriptor_instance_caches_collection() -> None:
    obj = DescAuthor.__new__(DescAuthor)
    vars(obj)["_db_"] = None
    col1 = obj.books
    col2 = obj.books
    assert col1 is col2


def test_set_descriptor_set_overwrites_cache() -> None:
    from nextorm.collection import RelatedCollection

    obj = DescAuthor.__new__(DescAuthor)
    vars(obj)["_db_"] = None
    ri = DescAuthor._relations_["books"]
    sentinel: RelatedCollection[Any] = RelatedCollection(obj, ri, None)
    obj.books = sentinel  # type: ignore[assignment]
    assert obj.__dict__["_books_col"] is sentinel


def test_set_descriptor_delete_removes_cache() -> None:
    obj = DescAuthor.__new__(DescAuthor)
    vars(obj)["_db_"] = None
    _ = obj.books  # create the cache
    del obj.books
    assert "_books_col" not in obj.__dict__


def test_set_descriptor_delete_idempotent_when_no_cache() -> None:
    obj = DescAuthor.__new__(DescAuthor)
    del obj.books  # no cache — must not raise


# ---------------------------------------------------------------------------
# SingleDescriptor — class-level access
# ---------------------------------------------------------------------------


def test_manytoone_class_access_returns_column_expr() -> None:
    from nextorm.expr import ColumnExpr

    result = DescBook.author
    assert isinstance(result, ColumnExpr)
    assert result.field_name == "author_id"


# ---------------------------------------------------------------------------
# SingleDescriptor — instance-level: no FK set
# ---------------------------------------------------------------------------


def test_manytoone_returns_none_when_no_fk() -> None:
    obj = DescBook.__new__(DescBook)
    assert obj.author is None


def test_manytoone_returns_none_when_fk_explicitly_none() -> None:
    obj = DescBook.__new__(DescBook)
    vars(obj)["_author_id"] = None
    assert obj.author is None


# ---------------------------------------------------------------------------
# SingleDescriptor — instance-level: FK id set, lazy load
# ---------------------------------------------------------------------------


def test_manytoone_raises_without_db_context() -> None:
    obj = DescBook.__new__(DescBook)
    vars(obj)["_author_id"] = 99
    with pytest.raises(RuntimeError, match="database"):
        _ = obj.author


def test_manytoone_lazy_load_from_db() -> None:
    db = Database(entities=[DescAuthor, DescBook])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)

    with db_session:
        author = DescAuthor(name="Alice")
    assert author.id is not None

    # Manually set FK + db context (simulates row-mapping)
    book = DescBook.__new__(DescBook)
    vars(book)["_author_id"] = author.id
    vars(book)["_db_"] = db

    loaded = book.author
    assert loaded is not None
    assert loaded.name == "Alice"


def test_manytoone_lazy_load_caches_result() -> None:
    db = Database(entities=[DescAuthor, DescBook])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)

    with db_session:
        author = DescAuthor(name="Bob")

    book = DescBook.__new__(DescBook)
    vars(book)["_author_id"] = author.id
    vars(book)["_db_"] = db

    a1 = book.author
    a2 = book.author
    assert a1 is a2


# ---------------------------------------------------------------------------
# SingleDescriptor — __set__
# ---------------------------------------------------------------------------


def test_manytoone_set_none_clears_both_keys() -> None:
    obj = DescBook.__new__(DescBook)
    vars(obj)["_author_id"] = 5
    vars(obj)["_author_obj"] = object()
    obj.author = None
    assert obj.__dict__["_author_id"] is None
    assert obj.__dict__["_author_obj"] is None


def test_manytoone_set_int_stores_fk() -> None:
    obj = DescBook.__new__(DescBook)
    obj.author = 7  # type: ignore[assignment]
    assert obj.__dict__["_author_id"] == 7
    assert "_author_obj" not in obj.__dict__


def test_manytoone_set_entity_stores_both() -> None:
    obj = DescBook.__new__(DescBook)
    author = DescAuthor.__new__(DescAuthor)
    vars(author)["_field_id"] = 42
    obj.author = author
    assert obj.__dict__["_author_obj"] is author
    assert obj.__dict__["_author_id"] == 42


def test_manytoone_set_entity_without_pk_stores_none_fk() -> None:
    """Setting a relation to an entity with no pk should store None as FK id."""
    obj = DescBook.__new__(DescBook)
    author = DescAuthor.__new__(DescAuthor)
    # id is not set → _pk_field_ is 'id' but author.id is None
    obj.author = author
    assert obj.__dict__["_author_obj"] is author
    assert obj.__dict__["_author_id"] is None


# ---------------------------------------------------------------------------
# SingleDescriptor — __delete__
# ---------------------------------------------------------------------------


def test_manytoone_delete_removes_both_keys() -> None:
    obj = DescBook.__new__(DescBook)
    vars(obj)["_author_id"] = 3
    vars(obj)["_author_obj"] = object()
    del obj.author
    assert "_author_id" not in obj.__dict__
    assert "_author_obj" not in obj.__dict__


def test_manytoone_delete_idempotent() -> None:
    obj = DescBook.__new__(DescBook)
    del obj.author  # nothing to delete — must not raise


# ---------------------------------------------------------------------------
# SingleDescriptor with forward-reference target
# ---------------------------------------------------------------------------


def test_manytoone_forward_ref_lazy_load_raises_runtime_error() -> None:
    """Lazy-loading a ManyToOne with an unresolved forward-ref target raises RuntimeError."""
    # Build a descriptor whose target is a string (unresolved forward ref)
    ri = RelationInfo(
        "forwardref",
        RelationSpec(kind=RelationKind.SINGLE, target="NonexistentClass"),
    )
    desc = SingleDescriptor("forwardref", ri)

    obj = DescBook.__new__(DescBook)
    vars(obj)["_forwardref_id"] = 1
    vars(obj)["_db_"] = object()  # any non-None value (db won't be called)

    with pytest.raises(RuntimeError, match="forward reference"):
        desc.__get__(obj, type(obj))


# ---------------------------------------------------------------------------
# Identity-map integration: db_session
# ---------------------------------------------------------------------------


def test_save_registers_entity_in_session_identity_map() -> None:
    db = Database(entities=[DescAuthor])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)

    with db_session as cache:
        a = DescAuthor(name="Carol")
        db.flush()
        assert a.id is not None
        assert cache.get((DescAuthor, a.id)) is a


def test_delete_removes_entity_from_session_identity_map() -> None:
    db = Database(entities=[DescAuthor])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)

    with db_session:
        a = DescAuthor(name="Dave")
    pk = a.id

    with db_session as cache:
        cache.put(a, pk)
        db.delete_instance(a)
        assert cache.get((DescAuthor, pk)) is None


def test_map_row_returns_cached_entity_within_session() -> None:
    db = Database(entities=[DescAuthor])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)

    with db_session:
        a = DescAuthor(name="Eve")
    pk = a.id

    with db_session as cache:
        # Pre-populate the cache with our original object
        cache.put(a, pk)
        # fetch_one should return the SAME Python object from the cache
        loaded = db.select(DescAuthor).filter(DescAuthor.id == pk).fetch_one()
        assert loaded is a


# ---------------------------------------------------------------------------
# FieldDescriptor — autostrip, min, max, py_check
# ---------------------------------------------------------------------------


def test_autostrip_strips_leading_trailing_whitespace() -> None:
    e = _VFEntity(trimmed="  hello  ", score=50, code="abc")
    assert e.trimmed == "hello"


def test_autostrip_on_reassignment() -> None:
    e = _VFEntity(trimmed="x", score=50, code="abc")
    e.trimmed = "  world  "
    assert e.trimmed == "world"


def test_autostrip_false_preserves_whitespace() -> None:
    """Fields without autostrip keep whitespace intact."""
    b = DescBook(title="  Book  ")
    assert b.title == "  Book  "


def test_min_raises_below_minimum() -> None:
    e = _VFEntity(trimmed="x", score=50, code="abc")
    with pytest.raises(ValueError, match="minimum"):
        e.score = -1


def test_max_raises_above_maximum() -> None:
    e = _VFEntity(trimmed="x", score=50, code="abc")
    with pytest.raises(ValueError, match="maximum"):
        e.score = 101


def test_min_boundary_accepted() -> None:
    e = _VFEntity(trimmed="x", score=0, code="abc")
    assert e.score == 0


def test_max_boundary_accepted() -> None:
    e = _VFEntity(trimmed="x", score=100, code="abc")
    assert e.score == 100


def test_py_check_valid_value_passes() -> None:
    e = _VFEntity(trimmed="x", score=50, code="abc")
    assert e.code == "abc"


def test_py_check_invalid_value_raises_at_init() -> None:
    with pytest.raises(ValueError, match="py_check"):
        _VFEntity(trimmed="x", score=50, code="ab")  # len 2 < 3


def test_py_check_invalid_value_raises_on_reassignment() -> None:
    e = _VFEntity(trimmed="x", score=50, code="abc")
    with pytest.raises(ValueError, match="py_check"):
        e.code = "xy"


def test_none_value_bypasses_all_validation() -> None:
    """None is always stored without running min/max/py_check (nullable pattern)."""
    e = _VFEntity(trimmed="x", score=50, code="abc")
    e.score = None
    assert e.score is None


# ---------------------------------------------------------------------------
# FieldDescriptor — lazy sentinel
# ---------------------------------------------------------------------------


def test_lazy_sentinel_raises_without_db_context() -> None:
    """Accessing a sentinel field without a DB attached raises RuntimeError."""
    e: Any = object.__new__(_LFEntity)
    vars(e)["_field_name"] = "Alice"
    vars(e)["_field_bio"] = _LAZY_SENTINEL

    with pytest.raises(RuntimeError, match="database context"):
        _ = e.bio


def test_lazy_sentinel_raises_for_async_db_context() -> None:
    """Accessing a sentinel field from an async DB raises a helpful RuntimeError."""
    from nextorm.async_database import AsyncDatabase  # noqa: PLC0415

    e: Any = object.__new__(_LFEntity)
    vars(e)["_field_name"] = "Bob"
    vars(e)["_field_bio"] = _LAZY_SENTINEL
    mock_db = AsyncDatabase.__new__(AsyncDatabase)
    mock_db._is_async = True
    vars(e)["_db_"] = mock_db

    with pytest.raises(RuntimeError, match="async context"):
        _ = e.bio


def test_lazy_field_loaded_on_first_access() -> None:
    db = Database(entities=[_LFEntity])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)

    with db_session:
        original = _LFEntity(name="Carol")
        original.bio = "Long biography text"
    pk = original.id

    loaded = db.select(_LFEntity).filter(_LFEntity.id == pk).fetch_one()
    assert loaded is not None
    # bio is lazy — the sentinel signals it hasn't been loaded yet
    assert vars(loaded)["_field_bio"] is _LAZY_SENTINEL
    # accessing the attribute triggers the lazy load
    assert loaded.bio == "Long biography text"
    db.close()


def test_lazy_field_cached_after_first_access() -> None:
    db = Database(entities=[_LFEntity])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)

    with db_session:
        original = _LFEntity(name="Dave")
        original.bio = "Bio text"

    loaded = db.select(_LFEntity).filter(_LFEntity.id == original.id).fetch_one()
    assert loaded is not None
    _ = loaded.bio  # first access loads and caches
    # sentinel is now replaced by the actual value
    assert vars(loaded)["_field_bio"] == "Bio text"
    db.close()


# ---------------------------------------------------------------------------
# LocalDescriptor — default values and py_check
# ---------------------------------------------------------------------------


class _DefaultLocal(Entity):
    """Entity with Local fields that carry scalar and callable defaults."""

    _flag: Local[bool] = Local(default=False)
    _items: Local[list[str]] = Local(default=list)


class _CheckedLocal(Entity):
    """Entity with a Local field that validates values via py_check."""

    _score: Local[int] = Local(py_check=lambda v: isinstance(v, int) and 0 <= v <= 100)  # pyright: ignore[reportUnknownLambdaType]


class _NoDefaultLocal(Entity):
    """Entity with a Local field that has no default — must be set before reading."""

    _data: Local[str]


def test_local_scalar_default_initialised_on_construction() -> None:
    """A scalar default is applied during Entity.__init__ before any kwargs."""
    obj = _DefaultLocal()
    assert obj._flag is False


def test_local_callable_default_called_per_instance() -> None:
    """A callable default produces a fresh value for each instance."""
    a = _DefaultLocal()
    b = _DefaultLocal()
    a._items.append("item")
    assert b._items == []  # not shared


def test_local_py_check_valid_value_passes() -> None:
    obj = _CheckedLocal()
    obj._score = 50
    assert obj._score == 50


def test_local_py_check_invalid_value_raises() -> None:
    obj = _CheckedLocal()
    with pytest.raises(ValueError, match="py_check"):
        obj._score = 200


def test_local_unset_without_default_raises_attribute_error() -> None:
    """Reading an uninitialised Local field with no default raises AttributeError."""
    obj = _NoDefaultLocal.__new__(_NoDefaultLocal)
    with pytest.raises(AttributeError, match="has not been initialised"):
        _ = obj._data


def test_local_descriptor_delete_clears_value() -> None:
    """Deleting a Local field removes it from instance.__dict__ without raising."""
    obj = _DefaultLocal()
    assert obj._flag is False
    del obj._flag
    # After deletion the field is gone from __dict__; accessing raises AttributeError
    with pytest.raises(AttributeError):
        _ = obj._flag


def test_local_descriptor_delete_idempotent() -> None:
    """Deleting an already-absent Local field must not raise."""
    obj = _DefaultLocal.__new__(_DefaultLocal)
    del obj._flag  # nothing to delete — must not raise


# ---------------------------------------------------------------------------
# PK descriptor placeholder tests
# ---------------------------------------------------------------------------


def test_pk_descriptor_get_raises_not_implemented() -> None:
    """PK.__get__ is a placeholder that raises NotImplementedError."""
    from nextorm.fields import PK

    pk = PK()  # type: ignore[var-annotated]
    dummy_obj = object()
    with pytest.raises(NotImplementedError):
        pk.__get__(dummy_obj, type(dummy_obj))


def test_pk_descriptor_set_raises_not_implemented() -> None:
    """PK.__set__ is a placeholder that raises NotImplementedError."""
    from nextorm.fields import PK

    pk = PK()  # type: ignore[var-annotated]
    dummy_obj = object()
    with pytest.raises(NotImplementedError):
        pk.__set__(dummy_obj, 42)  # pyright: ignore[reportUnknownMemberType]
