"""Tests for Batch H — Optimistic locking (Option A — per-field read tracking)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from nextorm.database import Database
from nextorm.entity import Entity
from nextorm.exceptions import OptimisticCheckError
from nextorm.fields import PK, Opt, Req
from nextorm.session import db_session

if TYPE_CHECKING:
    from collections.abc import Generator


# ---------------------------------------------------------------------------
# Entity definitions
# ---------------------------------------------------------------------------


class OptArticle(Entity):
    _table_ = "opt_article"
    id: PK[int]
    title: Req[str]
    score: Req[int]
    tag: Opt[str] = Opt(nullable=True)  # nullable — used for IS NULL optimistic-check tests


class OptLazyArticle(Entity):
    _table_ = "opt_lazy_article"
    id: PK[int]
    title: Req[str]
    body: Req[str] = Req(lazy=True)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db() -> Generator[Database, None, None]:
    _db = Database(entities=[OptArticle, OptLazyArticle])
    _db.bind("sqlite", ":memory:")
    _db.generate_mapping(create_tables=True)
    yield _db
    _db.close()


# ---------------------------------------------------------------------------
# Sync tests
# ---------------------------------------------------------------------------


def test_optimistic_update_succeeds_when_no_concurrent_change(db: Database) -> None:
    """Normal read-modify-save succeeds when no external change occurred."""
    with db_session:
        art = OptArticle(title="Hello", score=0)
    with db_session:
        loaded = db.select(OptArticle).filter(OptArticle.id == art.id).fetch_one()
        assert loaded is not None
        # Read score so it enters _read_cols_ and is checked on save.
        loaded.score = loaded.score + 1
    updated = db.select(OptArticle).filter(OptArticle.id == art.id).fetch_one()
    assert updated is not None
    assert updated.score == 1


def test_optimistic_raises_on_concurrent_modification(db: Database) -> None:
    """OptimisticCheckError is raised when a READ field was modified concurrently."""
    with db_session:
        art = OptArticle(title="Race", score=0)

    with db_session:
        loaded = db.select(OptArticle).filter(OptArticle.id == art.id).fetch_one()
        assert loaded is not None
        # Explicitly read score — this puts "score" into _read_cols_.
        _ = loaded.score

        # Simulate a concurrent update bypassing the session.
        db._execute_dml(f"UPDATE opt_article SET score = 99 WHERE id = {art.id}", [])

        loaded.score = 1
        with pytest.raises(OptimisticCheckError, match="Concurrent update"):
            db.flush()


def test_optimistic_unread_field_not_checked(db: Database) -> None:
    """Concurrent change to an UNREAD field does not raise — per-field granularity."""
    with db_session:
        art = OptArticle(title="Unread", score=0)

    with db_session:
        loaded = db.select(OptArticle).filter(OptArticle.id == art.id).fetch_one()
        assert loaded is not None
        # Read only title — score is NOT in _read_cols_.
        _ = loaded.title

        # Modify score concurrently (we never read it — no conflict expected).
        db._execute_dml(f"UPDATE opt_article SET score = 99 WHERE id = {art.id}", [])

        loaded.title = "Changed"

    result = db.select(OptArticle).filter(OptArticle.id == art.id).fetch_one()
    assert result is not None
    assert result.title == "Changed"
    # nextorm does a full-entity UPDATE, so score is the entity's value (0),
    # not the concurrent value — the key property is that no OptimisticCheckError
    # was raised, proving per-field granularity.
    assert result.score == 0


def test_optimistic_false_skips_conflict_check(db: Database) -> None:
    """With optimistic=False the check is bypassed even for read fields."""
    with db_session:
        art = OptArticle(title="Skip", score=0)

    with db_session(optimistic=False):
        loaded = db.select(OptArticle).filter(OptArticle.id == art.id).fetch_one()
        assert loaded is not None
        _ = loaded.score  # read, but optimistic=False disables check

        # Simulate a concurrent update.
        db._execute_dml(f"UPDATE opt_article SET score = 99 WHERE id = {art.id}", [])

        loaded.score = 1

    result = db.select(OptArticle).filter(OptArticle.id == art.id).fetch_one()
    assert result is not None
    assert result.score == 1


def test_optimistic_no_reads_skips_check(db: Database) -> None:
    """Writing a field that was never read produces no optimistic WHERE clause."""
    with db_session:
        art = OptArticle(title="NoRead", score=0)

    # Modify externally.
    db._execute_dml(f"UPDATE opt_article SET score = 77 WHERE id = {art.id}", [])

    # Load the entity, modify a field WITHOUT reading it first, then save.
    loaded = db.select(OptArticle).filter(OptArticle.id == art.id).fetch_one()
    assert loaded is not None
    assert "_read_cols_" in vars(loaded)
    with db_session:
        loaded.score = 5  # write without read → _read_cols_ still empty

    result = db.select(OptArticle).filter(OptArticle.id == art.id).fetch_one()
    assert result is not None
    assert result.score == 5


def test_dbvals_and_read_cols_on_loaded_entity(db: Database) -> None:
    """_dbvals_ carries original DB values; _read_cols_ starts empty after load."""
    with db_session:
        art = OptArticle(title="Snap", score=10)
    loaded = db.select(OptArticle).filter(OptArticle.id == art.id).fetch_one()
    assert loaded is not None
    dbvals: dict[str, Any] = vars(loaded)["_dbvals_"]
    read_cols: set[str] = vars(loaded)["_read_cols_"]
    assert dbvals["score"] == 10
    assert dbvals["title"] == "Snap"
    assert read_cols == set()
    # After reading a field, it appears in _read_cols_.
    _ = loaded.score
    assert "score" in read_cols


def test_optimistic_no_dbvals_when_entity_not_loaded(db: Database) -> None:
    """An entity instance created directly (not via select) has no _dbvals_."""
    with db_session:
        OptArticle(title="Direct", score=7)
    # Build a new entity instance without going through select() — verify no _dbvals_.
    art = OptArticle.__new__(OptArticle)
    vars(art)["_db_"] = db
    vars(art)["_field_id"] = 999  # Different pk to avoid conflicts
    assert "_dbvals_" not in vars(art)


def test_optimistic_null_field_is_null_clause(db: Database) -> None:
    """Reading a NULL-valued field produces AND col IS NULL in the WHERE clause."""
    with db_session:
        art = OptArticle(title="Null tag", score=1)  # tag defaults to None

    with db_session:
        loaded = db.select(OptArticle).filter(OptArticle.id == art.id).fetch_one()
        assert loaded is not None
        _ = loaded.tag  # read the NULL field → "tag" enters _read_cols_

        # Simulate a concurrent update that sets tag to a non-null value.
        db._execute_dml(f"UPDATE opt_article SET tag = 'x' WHERE id = {art.id}", [])

        loaded.title = "Updated"
        with pytest.raises(OptimisticCheckError, match="Concurrent update"):
            db.flush()  # WHERE opt_article.tag IS NULL matches nothing → raises


def test_lazy_field_read_updates_dbvals_and_read_cols(db: Database) -> None:
    """Accessing a lazy field inside a session populates _dbvals_ and _read_cols_."""
    with db_session:
        item = OptLazyArticle(title="L", body="secret")

    loaded = db.select(OptLazyArticle).filter(OptLazyArticle.id == item.id).fetch_one()
    assert loaded is not None
    # _dbvals_ built at load time should NOT contain 'body' (lazy field skipped).
    dbvals: dict[str, Any] = vars(loaded)["_dbvals_"]
    assert "body" not in dbvals

    # First access triggers lazy load and registers the field.
    _ = loaded.body
    assert dbvals.get("body") == "secret"
    read_cols: set[str] = vars(loaded)["_read_cols_"]
    assert "body" in read_cols


def test_lazy_field_read_without_tracking_dicts(db: Database) -> None:
    """Accessing a lazy field on an entity without _dbvals_/_read_cols_ does not crash."""
    from nextorm.entity import _LAZY_SENTINEL  # noqa: PLC0415

    with db_session:
        item = OptLazyArticle(title="M", body="inner")

    loaded = db.select(OptLazyArticle).filter(OptLazyArticle.id == item.id).fetch_one()
    assert loaded is not None

    # Remove tracking dicts and reset sentinel to simulate an entity without tracking.
    vars(loaded).pop("_dbvals_", None)
    vars(loaded).pop("_read_cols_", None)
    vars(loaded)["_field_body"] = _LAZY_SENTINEL  # reset to unloaded state

    result = loaded.body  # must not raise; covers the None-guard branches
    assert result == "inner"


def test_optimistic_check_error_exported_from_init() -> None:
    """OptimisticCheckError is accessible from the top-level nextorm package."""
    from nextorm import OptimisticCheckError as _OE  # noqa: PLC0415

    assert _OE is OptimisticCheckError


# ---------------------------------------------------------------------------
# Async tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_optimistic_raises_on_concurrent_modification() -> None:
    """OptimisticCheckError is raised in async path for a field that was read."""
    from nextorm import AsyncDatabase  # noqa: PLC0415

    adb = AsyncDatabase(entities=[OptArticle])
    await adb.bind("sqlite", ":memory:")
    await adb.generate_mapping(create_tables=True)

    art = OptArticle(title="ASync", score=0)
    await adb.asave(art)

    loaded_list = await adb.aselect(OptArticle).filter(OptArticle.id == art.id).fetch_all()
    loaded = loaded_list[0]

    # Read score — puts it into _read_cols_.
    _ = loaded.score

    # Simulate concurrent modification.
    await adb.execute(f"UPDATE opt_article SET score = 99 WHERE id = {art.id}")

    loaded.score = 1
    with pytest.raises(OptimisticCheckError, match="Concurrent update"):
        await adb.asave(loaded)

    await adb.close()


@pytest.mark.asyncio
async def test_async_optimistic_unread_field_not_checked() -> None:
    """Concurrent change to an unread field does not raise in async path."""
    from nextorm import AsyncDatabase  # noqa: PLC0415

    adb = AsyncDatabase(entities=[OptArticle])
    await adb.bind("sqlite", ":memory:")
    await adb.generate_mapping(create_tables=True)

    art = OptArticle(title="AsyncUnread", score=0)
    await adb.asave(art)

    loaded_list = await adb.aselect(OptArticle).filter(OptArticle.id == art.id).fetch_all()
    loaded = loaded_list[0]

    # Only read title — score is NOT tracked.
    _ = loaded.title

    # Concurrent change to score (not tracked) — must not raise.
    await adb.execute(f"UPDATE opt_article SET score = 99 WHERE id = {art.id}")

    loaded.title = "Changed"
    await adb.asave(loaded)  # must NOT raise

    rows = await adb.aselect(OptArticle).filter(OptArticle.id == art.id).fetch_all()
    assert rows[0].title == "Changed"

    await adb.close()


@pytest.mark.asyncio
async def test_async_optimistic_null_field_is_null_clause() -> None:
    """IS NULL branch exercised on async path for a NULL-origin field."""
    from nextorm import AsyncDatabase  # noqa: PLC0415

    adb = AsyncDatabase(entities=[OptArticle])
    await adb.bind("sqlite", ":memory:")
    await adb.generate_mapping(create_tables=True)

    art = OptArticle(title="Async Null", score=0)  # tag = None
    await adb.asave(art)

    loaded_list = await adb.aselect(OptArticle).filter(OptArticle.id == art.id).fetch_all()
    loaded = loaded_list[0]
    _ = loaded.tag  # read NULL tag → tracked in _read_cols_

    await adb.execute(f"UPDATE opt_article SET tag = 'y' WHERE id = {art.id}")

    loaded.title = "Changed"
    with pytest.raises(OptimisticCheckError, match="Concurrent update"):
        await adb.asave(loaded)

    await adb.close()
