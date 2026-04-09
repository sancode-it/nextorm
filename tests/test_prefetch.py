"""Tests for Batch G — Single/FK batch prefetch (N+1 coalescing)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from nextorm.database import Database
from nextorm.entity import Entity
from nextorm.fields import PK, Req, Set, Single
from nextorm.session import db_session

if TYPE_CHECKING:
    from collections.abc import Generator


# ---------------------------------------------------------------------------
# Entity definitions
# ---------------------------------------------------------------------------


class PrefetchAuthor(Entity):
    _table_ = "prefetch_author"
    id: PK[int]
    name: Req[str]
    books: Set["PrefetchBook"]  # noqa: UP037


class PrefetchBook(Entity):
    _table_ = "prefetch_book"
    id: PK[int]
    title: Req[str]
    author: Single[PrefetchAuthor | None]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db() -> Generator[Database, None, None]:
    _db = Database(entities=[PrefetchAuthor, PrefetchBook])
    _db.bind("sqlite", ":memory:")
    _db.generate_mapping(create_tables=True)
    yield _db
    _db.close()


@pytest.fixture
def db_with_data(db: Database) -> Database:
    """Database seeded with two authors and three books."""
    with db_session:
        a1 = PrefetchAuthor(name="Alice")
        a2 = PrefetchAuthor(name="Bob")
        db.flush()  # a1.id and a2.id set
        b1 = PrefetchBook(title="Alpha")
        b1.author = a1
        b2 = PrefetchBook(title="Beta")
        b2.author = a1
        b3 = PrefetchBook(title="Gamma")
        b3.author = a2
    return db


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_prefetch_single_loads_related_objects(db_with_data: Database) -> None:
    """prefetch('author') on PrefetchBook batch-loads all authors in one IN query."""
    books = db_with_data.select(PrefetchBook).prefetch("author").fetch_all()
    assert len(books) == 3
    for book in books:
        loaded = vars(book).get("_author_obj")
        assert loaded is not None
        assert isinstance(loaded, PrefetchAuthor)


def test_prefetch_single_deduplicates_fk_ids(db_with_data: Database) -> None:
    """Two books share the same author — only one PrefetchAuthor instance is created."""
    books = (
        db_with_data.select(PrefetchBook)
        .order_by(PrefetchBook.id.asc())
        .prefetch("author")
        .fetch_all()
    )
    alpha = books[0]
    beta = books[1]
    # Both books share PrefetchAuthor "Alice" — should be the same Python object
    assert vars(alpha)["_author_obj"] is vars(beta)["_author_obj"]


def test_prefetch_single_null_fk_sets_none(db: Database) -> None:
    """Books with no author FK get None cached in _author_obj."""
    with db_session:
        PrefetchBook(title="Lonely")
        # no author set → fk is None
    books = db.select(PrefetchBook).prefetch("author").fetch_all()
    assert len(books) == 1
    assert vars(books[0]).get("_author_obj") is None


def test_prefetch_single_all_null_fks(db: Database) -> None:
    """All books have null author → batch query is skipped, all get None."""
    with db_session:
        PrefetchBook(title="X")
        PrefetchBook(title="Y")
    books = db.select(PrefetchBook).prefetch("author").fetch_all()
    for book in books:
        assert vars(book).get("_author_obj") is None


def test_prefetch_single_unresolvable_target_skipped() -> None:
    """prefetch on a Single with unresolvable target is silently skipped."""

    class _GhostParent2(Entity):
        label: Req[str]
        ghost: Single["_CompletelyMissingEntity2"]  # type: ignore[name-defined]  # noqa: F821, UP037

    db2 = Database(entities=[_GhostParent2])
    db2.bind("sqlite", ":memory:")
    db2.generate_mapping(create_tables=True)
    p = _GhostParent2(label="x")
    vars(p)["_ghost_id"] = 1  # set FK directly, skip None constraint
    db2.save(p)
    results = db2.select(_GhostParent2).prefetch("ghost").fetch_all()
    assert len(results) == 1
    db2.close()


def test_prefetch_single_target_no_pk_skipped(db: Database) -> None:
    """prefetch on a Single where the target entity has no PK is silently skipped."""

    class _NoPKOwner2(Entity):
        label: Req[str]
        ref: Single[PrefetchAuthor | None]

    db2 = Database(entities=[PrefetchAuthor, _NoPKOwner2])
    db2.bind("sqlite", ":memory:")
    db2.generate_mapping(create_tables=True)
    a = PrefetchAuthor(name="target")
    db2.save(a)
    o = _NoPKOwner2(label="o")
    o.ref = a
    db2.save(o)

    orig_pk_field = PrefetchAuthor._pk_field_
    orig_pk_fields = PrefetchAuthor._pk_fields_
    try:
        PrefetchAuthor._pk_field_ = None
        PrefetchAuthor._pk_fields_ = ()
        results = db2.select(_NoPKOwner2).prefetch("ref").fetch_all()
        assert len(results) == 1
    finally:
        PrefetchAuthor._pk_field_ = orig_pk_field
        PrefetchAuthor._pk_fields_ = orig_pk_fields
    db2.close()


def test_prefetch_single_snapshot_captured(db_with_data: Database) -> None:
    """After prefetch, loaded entities have _dbvals_ and _read_cols_ attached."""
    with db_session:
        books = db_with_data.select(PrefetchBook).prefetch("author").fetch_all()
        authors = db_with_data.select(PrefetchAuthor).fetch_all()
        assert len(books) == 3
        assert len(authors) == 2
        # Each book loaded through _map_row must carry optimistic tracking data.
        for book in books:
            assert "_dbvals_" in vars(book)
            assert "_read_cols_" in vars(book)


# ---------------------------------------------------------------------------
# Async _map_row coverage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_map_row_set_relation_branch() -> None:
    """Async _map_row with a SET relation covers the non-SINGLE branch in relations loop."""
    from nextorm import AsyncDatabase  # noqa: PLC0415

    adb = AsyncDatabase(entities=[PrefetchAuthor, PrefetchBook])
    await adb.bind("sqlite", ":memory:")
    await adb.generate_mapping(create_tables=True)
    a = PrefetchAuthor(name="Async Author")
    await adb.asave(a)
    authors = await adb.aselect(PrefetchAuthor).fetch_all()
    assert len(authors) == 1
    # _dbvals_ is set; the SET relation "books" was skipped (non-SINGLE).
    dbvals: dict[str, Any] = vars(authors[0])["_dbvals_"]
    assert "name" in dbvals
    assert "books_id" not in dbvals  # SET relations never add FK to dbvals
    await adb.close()


@pytest.mark.asyncio
async def test_async_map_row_no_pk_skips_dbvals() -> None:
    """Async _map_row skips dbvals/read_cols setup for entities without PK fields."""
    from nextorm import AsyncDatabase  # noqa: PLC0415

    adb = AsyncDatabase(entities=[PrefetchAuthor])
    await adb.bind("sqlite", ":memory:")
    await adb.generate_mapping(create_tables=True)
    a = PrefetchAuthor(name="NoPK")
    await adb.asave(a)
    orig_pk_fields = PrefetchAuthor._pk_fields_
    try:
        PrefetchAuthor._pk_fields_ = ()
        rows = await adb.aselect(PrefetchAuthor).fetch_all()
        assert len(rows) == 1
        # Without PK fields, _dbvals_ must NOT be set on the entity.
        assert "_dbvals_" not in vars(rows[0])
    finally:
        PrefetchAuthor._pk_fields_ = orig_pk_fields
    await adb.close()
