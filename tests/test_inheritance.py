"""Tests for Single-Table Inheritance (STI).

STI is triggered when a parent entity declares ``_discriminator_col_`` and
child entities declare ``_discriminator_``.  All classes share a single
database table; the discriminator column identifies each row's type.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Generator

import pytest

from nextorm.async_database import AsyncDatabase
from nextorm.database import Database
from nextorm.entity import Entity
from nextorm.fields import Opt, Req
from nextorm.session import db_session

# ---------------------------------------------------------------------------
# Entity definitions
# ---------------------------------------------------------------------------


class Animal(Entity):
    _discriminator_col_ = "kind"

    name: Req[str]
    weight: Opt[float]


class Dog(Animal):
    _discriminator_ = "dog"

    breed: Opt[str]


class Cat(Animal):
    _discriminator_ = "cat"

    indoor: Opt[bool]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db() -> Generator[Database, None, None]:
    _db = Database(entities=[Animal, Dog, Cat])
    _db.bind("sqlite", ":memory:")
    _db.generate_mapping(create_tables=True)
    # Seed data
    with db_session:
        Dog(name="Rex", weight=30.0, breed="Labrador")
        Cat(name="Whiskers", weight=4.5, indoor=True)
        Animal(name="Unknown", weight=10.0)
    yield _db
    _db.close()


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


def test_sti_child_shares_parent_table() -> None:
    assert Dog._table_name_ == Animal._table_name_
    assert Cat._table_name_ == Animal._table_name_


def test_sti_discriminator_col_set_on_parent() -> None:
    assert Animal._discriminator_col_ == "kind"


def test_sti_discriminator_val_set_on_children() -> None:
    assert Dog._discriminator_val_ == "dog"
    assert Cat._discriminator_val_ == "cat"


def test_sti_parent_on_children() -> None:
    assert Dog._sti_parent_ is Animal
    assert Cat._sti_parent_ is Animal


def test_sti_parent_is_none_on_parent() -> None:
    assert Animal._sti_parent_ is None


# ---------------------------------------------------------------------------
# Insert tests — discriminator column is injected automatically
# ---------------------------------------------------------------------------


def test_sti_insert_dog_sets_discriminator(db: Database) -> None:
    rows = db.select_raw("SELECT * FROM animal WHERE name='Rex'")
    assert len(rows) == 1
    assert rows[0]["kind"] == "dog"


def test_sti_insert_cat_sets_discriminator(db: Database) -> None:
    rows = db.select_raw("SELECT * FROM animal WHERE name='Whiskers'")
    assert len(rows) == 1
    assert rows[0]["kind"] == "cat"


def test_sti_insert_parent_no_discriminator(db: Database) -> None:
    rows = db.select_raw("SELECT * FROM animal WHERE name='Unknown'")
    assert len(rows) == 1
    assert rows[0]["kind"] is None


# ---------------------------------------------------------------------------
# Select tests — discriminator filter is injected automatically for children
# ---------------------------------------------------------------------------


def test_sti_select_dog_filters_by_discriminator(db: Database) -> None:
    results = db.select(Dog).fetch_all()
    assert len(results) == 1
    assert results[0].name == "Rex"


def test_sti_select_cat_filters_by_discriminator(db: Database) -> None:
    results = db.select(Cat).fetch_all()
    assert len(results) == 1
    assert results[0].name == "Whiskers"


def test_sti_select_parent_returns_all(db: Database) -> None:
    """Selecting the parent returns all rows (no discriminator filter)."""
    results = db.select(Animal).fetch_all()
    assert len(results) == 3


def test_sti_dog_has_breed_field(db: Database) -> None:
    dog = db.select(Dog).fetch_one()
    assert dog is not None
    assert dog.breed == "Labrador"


def test_sti_cat_has_indoor_field(db: Database) -> None:
    cat = db.select(Cat).fetch_one()
    assert cat is not None
    assert bool(cat.indoor) is True


def test_sti_select_dog_where_filter(db: Database) -> None:
    results = db.select(Dog).filter(Dog.name == "Rex").fetch_all()
    assert len(results) == 1


def test_sti_select_dog_count(db: Database) -> None:
    assert db.select(Dog).count() == 1


def test_sti_select_dog_exists(db: Database) -> None:
    assert db.select(Dog).exists() is True
    assert db.select(Dog).filter(Dog.name == "NotExist").exists() is False


# ---------------------------------------------------------------------------
# Async STI tests
# ---------------------------------------------------------------------------


@pytest.fixture
async def adb() -> AsyncGenerator[AsyncDatabase, None]:
    _db = AsyncDatabase(entities=[Animal, Dog, Cat])
    await _db.bind("sqlite", ":memory:")
    await _db.generate_mapping(create_tables=True)
    d = Dog(name="Ace", weight=25.0, breed="Poodle")
    c = Cat(name="Luna", weight=3.0, indoor=False)
    await _db.asave(d)
    await _db.asave(c)
    yield _db
    await _db.close()


@pytest.mark.asyncio
async def test_sti_async_insert_sets_discriminator(adb: AsyncDatabase) -> None:
    rows = await adb.select_raw("SELECT * FROM animal WHERE name='Ace'")
    assert len(rows) == 1
    assert rows[0]["kind"] == "dog"


@pytest.mark.asyncio
async def test_sti_async_select_filters_by_discriminator(adb: AsyncDatabase) -> None:
    results = await adb.aselect(Dog).fetch_all()
    assert len(results) == 1
    assert results[0].name == "Ace"


@pytest.mark.asyncio
async def test_sti_async_select_cat_filters(adb: AsyncDatabase) -> None:
    results = await adb.aselect(Cat).fetch_all()
    assert len(results) == 1
    assert results[0].name == "Luna"


# ---------------------------------------------------------------------------
# Schema builder: STI parent with disc col but no registered children
# (covers schema/builder.py 289->313 branch)
# ---------------------------------------------------------------------------


def test_sti_parent_with_disc_col_but_no_children_in_build_schema() -> None:
    """build_schema with only the STI parent (no children) skips child column injection."""
    from nextorm.schema.builder import build_schema  # noqa: PLC0415

    class _LoneParent(Entity):
        _discriminator_col_ = "ptype"
        label: Req[str]

    # Build schema with just the parent — no children are passed.
    # The branch `if disc_col is not None: if children:` → False path is taken.
    tables = build_schema([_LoneParent])
    table = tables["_loneparent"]
    col_names = {c.name for c in table.columns}
    # The discriminator column is NOT added because there are no children
    assert "ptype" not in col_names
    assert "label" in col_names
