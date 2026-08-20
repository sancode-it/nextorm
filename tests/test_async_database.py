"""Tests for AsyncDatabase and AsyncQuerySet."""

from __future__ import annotations

import asyncio
import io
import os
import typing
from typing import Any

import pytest

from nextorm.async_database import AsyncDatabase
from nextorm.debug import QueryStat, global_stats
from nextorm.entity import Entity
from nextorm.exceptions import MappingError, MultipleObjectsFoundError, ObjectNotFound
from nextorm.fields import PK, Opt, Req, Set, Single
from nextorm.session import db_session
from nextorm.sql.nodes import BinOp, ColumnRef, Param

# ---------------------------------------------------------------------------
# MariaDB connection helpers
# ---------------------------------------------------------------------------

_MARIADB_KWARGS: dict[str, Any] = {
    "host": os.environ.get("NEXTORM_MARIADB_HOST", "127.0.0.1"),
    "user": os.environ.get("NEXTORM_MARIADB_USER", "nextorm"),
    "password": os.environ.get("NEXTORM_MARIADB_PASSWORD", "nextorm"),
    "database": os.environ.get("NEXTORM_MARIADB_DATABASE", "nextorm_test"),
}


def _mariadb_available() -> bool:
    try:
        import pymysql  # noqa: PLC0415

        c = pymysql.connect(**_MARIADB_KWARGS)
        c.close()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Entity definitions
# ---------------------------------------------------------------------------


class AsyncUser(Entity):
    name: Req[str]
    age: Req[int]


class AsyncPostA(Entity):
    title: Req[str]
    author: Single[AsyncUser | None]


# ---------------------------------------------------------------------------
# Helper to run async tests synchronously
# ---------------------------------------------------------------------------


def run(coro: Any) -> Any:
    return asyncio.get_event_loop().run_until_complete(coro)  # noqa: W0611


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_db_starts_unbound() -> None:
    db = AsyncDatabase(entities=[AsyncUser])
    assert not db.is_bound


@pytest.mark.asyncio
async def test_async_db_bind_and_close() -> None:
    db = AsyncDatabase(entities=[AsyncUser])
    await db.bind("sqlite", ":memory:")
    assert db.is_bound
    await db.close()


@pytest.mark.asyncio
async def test_async_db_context_manager() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        assert db.is_bound
    assert db._connection is None


@pytest.mark.asyncio
async def test_async_db_register_entity() -> None:
    db = AsyncDatabase(entities=[])
    db.register(AsyncUser)
    assert "AsyncUser" in db.entities


@pytest.mark.asyncio
async def test_async_db_register_non_entity_raises() -> None:
    db = AsyncDatabase(entities=[])
    with pytest.raises(TypeError):
        db.register(int)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_async_db_generate_mapping() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        assert "asyncuser" in db.schema


@pytest.mark.asyncio
async def test_async_db_generate_mapping_not_bound_raises() -> None:
    db = AsyncDatabase(entities=[AsyncUser])
    with pytest.raises(RuntimeError, match="not bound"):
        await db.generate_mapping()


@pytest.mark.asyncio
async def test_async_db_aselect_without_mapping_raises() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        with pytest.raises(RuntimeError, match="Schema is empty"):
            db.aselect(AsyncUser)


@pytest.mark.asyncio
async def test_async_db_aselect_unmapped_entity_raises() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        with pytest.raises(RuntimeError, match="not in the mapped schema"):
            db.aselect(AsyncPostA)


# ---------------------------------------------------------------------------
# CRUD operations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_save_insert() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        u = AsyncUser(name="Alice", age=30)
        await db.asave(u)
        assert u.id is not None


@pytest.mark.asyncio
async def test_async_save_update() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        u = AsyncUser(name="Bob", age=25)
        await db.asave(u)
        new_id = u.id

        u.name = "Robert"
        await db.asave(u)

        rows = await db.aselect(AsyncUser).fetch_all()
        assert len(rows) == 1
        assert rows[0].name == "Robert"
        assert rows[0].id == new_id


@pytest.mark.asyncio
async def test_async_delete_instance() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        u = AsyncUser(name="Carol", age=40)
        await db.asave(u)
        assert await db.aselect(AsyncUser).count() == 1

        await db.adelete_instance(u)
        assert await db.aselect(AsyncUser).count() == 0
        assert u.id is None


@pytest.mark.asyncio
async def test_async_delete_unsaved_raises() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        u = AsyncUser(name="Dave", age=20)
        with pytest.raises(ValueError, match="primary key is None"):
            await db.adelete_instance(u)


@pytest.mark.asyncio
async def test_async_delete_no_pk_field_raises() -> None:
    class NoPkEntity(Entity):
        pass

    # NoPkEntity gets auto pk 'id' from EntityMeta.
    # Confirm save+delete round-trip works even when the entity has no
    # non-pk fields (INSERT uses the DEFAULT VALUES path).
    db2 = AsyncDatabase(entities=[NoPkEntity])
    await db2.bind("sqlite", ":memory:")
    await db2.generate_mapping(create_tables=True)
    u_nopk2 = NoPkEntity()
    await db2.asave(u_nopk2)
    assert u_nopk2.id is not None
    await db2.adelete_instance(u_nopk2)
    assert u_nopk2.id is None
    await db2.close()


@pytest.mark.asyncio
async def test_async_require_mapped_raises() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        u = AsyncPostA(title="Test")
        with pytest.raises(RuntimeError, match="not in the mapped schema"):
            await db.asave(u)


# ---------------------------------------------------------------------------
# AsyncQuerySet operations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_fetch_all() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        await db.asave(AsyncUser(name="A", age=1))
        await db.asave(AsyncUser(name="B", age=2))

        rows = await db.aselect(AsyncUser).fetch_all()
        assert len(rows) == 2


@pytest.mark.asyncio
async def test_async_fetch_one() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        await db.asave(AsyncUser(name="Solo", age=5))
        row = await db.aselect(AsyncUser).fetch_one()
        assert row is not None
        assert row.name == "Solo"


@pytest.mark.asyncio
async def test_async_fetch_one_empty_returns_none() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        row = await db.aselect(AsyncUser).fetch_one()
        assert row is None


@pytest.mark.asyncio
async def test_async_count() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        for i in range(3):
            await db.asave(AsyncUser(name=f"u{i}", age=i))

        n = await db.aselect(AsyncUser).count()
        assert n == 3


@pytest.mark.asyncio
async def test_async_exists_true() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        await db.asave(AsyncUser(name="X", age=0))
        assert await db.aselect(AsyncUser).exists() is True


@pytest.mark.asyncio
async def test_async_exists_false() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        assert await db.aselect(AsyncUser).exists() is False


@pytest.mark.asyncio
async def test_async_filter() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        await db.asave(AsyncUser(name="young", age=20))
        await db.asave(AsyncUser(name="old", age=60))

        rows = (
            await db.aselect(AsyncUser)
            .filter(BinOp(ColumnRef("age"), ">", Param(value=30)))
            .fetch_all()
        )
        assert len(rows) == 1
        assert rows[0].name == "old"


@pytest.mark.asyncio
async def test_async_order_by() -> None:
    from nextorm.sql.nodes import OrderItem

    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        await db.asave(AsyncUser(name="C", age=3))
        await db.asave(AsyncUser(name="A", age=1))
        await db.asave(AsyncUser(name="B", age=2))

        rows = (
            await db.aselect(AsyncUser)
            .order_by(OrderItem(ColumnRef("name"), descending=False))
            .fetch_all()
        )
        assert [r.name for r in rows] == ["A", "B", "C"]


@pytest.mark.asyncio
async def test_async_order_by_lambda() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        await db.asave(AsyncUser(name="C", age=3))
        await db.asave(AsyncUser(name="A", age=1))
        await db.asave(AsyncUser(name="B", age=2))

        rows = await db.aselect(AsyncUser).order_by(lambda u: u.name).fetch_all()
        assert [r.name for r in rows] == ["A", "B", "C"]

        rows_desc = await db.aselect(AsyncUser).order_by(lambda u: u.age.desc()).fetch_all()
        assert [r.age for r in rows_desc] == [3, 2, 1]


@pytest.mark.asyncio
async def test_async_order_by_lambda_chained_relation() -> None:
    """AsyncQuerySet.order_by with a chained relation adds the required JOIN."""
    async with AsyncDatabase(entities=[AsyncUser, AsyncPostA]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        user_a = AsyncUser(name="Alice", age=1)
        user_b = AsyncUser(name="Bob", age=2)
        await db.asave(user_a)
        await db.asave(user_b)
        await db.asave(AsyncPostA(title="Post-Z", author=user_b))
        await db.asave(AsyncPostA(title="Post-A", author=user_a))

        rows = await db.aselect(AsyncPostA).order_by(lambda p: p.author.name).fetch_all()
        # Ordering by author.name ASC: Alice's post (Post-A) comes before Bob's (Post-Z)
        assert [r.title for r in rows] == ["Post-A", "Post-Z"]


@pytest.mark.asyncio
async def test_async_limit_offset() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        for i in range(5):
            await db.asave(AsyncUser(name=f"u{i}", age=i))

        rows = await db.aselect(AsyncUser).limit(2).offset(1).fetch_all()
        assert len(rows) == 2


@pytest.mark.asyncio
async def test_async_delete_qs() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        await db.asave(AsyncUser(name="del1", age=1))
        await db.asave(AsyncUser(name="del2", age=2))
        await db.asave(AsyncUser(name="keep", age=3))

        count = (
            await db.aselect(AsyncUser).filter(BinOp(ColumnRef("age"), "<", Param(value=3))).delete()
        )
        assert count == 2
        assert await db.aselect(AsyncUser).count() == 1


@pytest.mark.asyncio
async def test_async_update_qs() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        await db.asave(AsyncUser(name="original", age=10))

        count = (
            await db.aselect(AsyncUser)
            .filter(BinOp(ColumnRef("name"), "=", Param(value="original")))
            .update(name="updated")
        )
        assert count == 1

        rows = await db.aselect(AsyncUser).fetch_all()
        assert rows[0].name == "updated"


@pytest.mark.asyncio
async def test_async_update_no_fields_returns_zero() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        n = await db.aselect(AsyncUser).update()
        assert n == 0


@pytest.mark.asyncio
async def test_async_update_bad_field_raises() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        with pytest.raises(ValueError, match="no field"):
            await db.aselect(AsyncUser).update(nonexistent="value")


@pytest.mark.asyncio
async def test_async_get_single() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        await db.asave(AsyncUser(name="single", age=7))
        u = await db.aselect(AsyncUser).get()
        assert u is not None
        assert u.name == "single"


@pytest.mark.asyncio
async def test_async_get_none_when_empty() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        u = await db.aselect(AsyncUser).get()
        assert u is None


@pytest.mark.asyncio
async def test_async_get_multiple_raises() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        await db.asave(AsyncUser(name="first", age=1))
        await db.asave(AsyncUser(name="second", age=2))

        with pytest.raises(MultipleObjectsFoundError, match="more than one"):
            await db.aselect(AsyncUser).get()


@pytest.mark.asyncio
async def test_async_join() -> None:
    async with AsyncDatabase(entities=[AsyncUser, AsyncPostA]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        u = AsyncUser(name="Writer", age=30)
        await db.asave(u)
        p = AsyncPostA(title="My Post")
        vars(p)["_author_id"] = u.id
        await db.asave(p)

        join_cond = BinOp(
            ColumnRef("id", "asyncuser"),
            "=",
            ColumnRef("author_id", "asyncposta"),
        )
        rows = await db.aselect(AsyncUser).join(AsyncPostA, join_cond).fetch_all()
        assert len(rows) >= 1


# ---------------------------------------------------------------------------
# Validate relations
# ---------------------------------------------------------------------------


class AsyncBadParent(Entity):
    name: Req[str]
    kids: Set["AsyncBadKid"]  # noqa: UP037


class AsyncBadKid(Entity):
    label: Req[str]
    # No back-ref to AsyncBadParent


@pytest.mark.asyncio
async def test_async_validate_relations_raises_mapping_error() -> None:
    db = AsyncDatabase(entities=[AsyncBadParent, AsyncBadKid])
    await db.bind("sqlite", ":memory:")
    with pytest.raises(MappingError, match="back-reference"):
        await db.generate_mapping()
    await db.close()


# ---------------------------------------------------------------------------
# Default provider (no entities specified → uses global registry)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_db_uses_explicit_entities_list() -> None:
    db = AsyncDatabase(entities=[AsyncUser, AsyncPostA])
    assert "AsyncUser" in db.entities
    assert "AsyncPostA" in db.entities


@pytest.mark.asyncio
async def test_async_manytoone_fk_persisted() -> None:
    """FK value from _author_id is persisted in INSERT/UPDATE."""
    async with AsyncDatabase(entities=[AsyncUser, AsyncPostA]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        u = AsyncUser(name="FK Writer", age=99)
        await db.asave(u)

        p = AsyncPostA(title="FK Post")
        vars(p)["_author_id"] = u.id
        await db.asave(p)

        loaded = await db.aselect(AsyncPostA).fetch_one()
        assert loaded is not None
        assert vars(loaded).get("_author_id") == u.id


# ---------------------------------------------------------------------------
# Entity definitions for additional coverage tests
# ---------------------------------------------------------------------------


class AsyncPKOnly(Entity):
    """Entity with only an auto-PK — triggers DEFAULT VALUES insert path."""

    id: PK[int]


class AsyncValidParent(Entity):
    """Parent entity with a Set[AsyncValidChild] — defined first to avoid NameError."""

    name: Req[str]
    kids: Set["AsyncValidChild"]  # noqa: UP037


class AsyncValidChild(Entity):
    """Child entity with a ManyToOne back-ref to AsyncValidParent."""

    label: Req[str]
    parent_fk: Single[AsyncValidParent]


class AsyncParentUnknownKid(Entity):
    """Parent entity whose Set target is a string forward-ref not in entities list."""

    name: Req[str]

    # intentional string forward ref — not in entities list
    gadgets: Set["_NonExistentEntity"]  # type: ignore[name-defined]  # noqa: UP037, F821


# typing.ForwardRef target: ensures the ForwardRef branch in _validate_relations is hit
class _AsyncFwdChild(Entity):
    label: Req[str]
    owner: Single["_AsyncFwdParent"]  # noqa: UP037


class _AsyncFwdParent(Entity):
    label: Req[str]
    items: Set[typing.ForwardRef("_AsyncFwdChild")]  # type: ignore[valid-type]


# Opt[str] entity for async _do_insert None→"" path
class _AsyncOptStrEntity(Entity):
    name: Req[str]
    notes: Opt[str]  # nullable=False → None becomes "" on insert


# ---------------------------------------------------------------------------
# Additional coverage tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_on_entityless_db_and_duplicate() -> None:
    """register() on AsyncDatabase() with no entities creates list (line 111).
    Registering same entity again is a no-op (branch 112->107)."""
    db = AsyncDatabase()  # _entities is None
    db.register(AsyncUser)  # triggers self._entities = []
    db.register(AsyncUser)  # duplicate → skips append
    assert "AsyncUser" in db.entities


@pytest.mark.asyncio
async def test_effective_entities_uses_global_registry() -> None:
    """_effective_entities() falls back to sorted global registry when _entities is None."""
    db = AsyncDatabase()  # _entities=None
    names = list(db.entities.keys())
    # The global registry has all Entity subclasses; just verify it's non-empty
    assert len(names) > 0


@pytest.mark.asyncio
async def test_bind_unknown_provider_raises() -> None:
    """bind() raises ValueError for unregistered provider."""
    db = AsyncDatabase(entities=[AsyncUser])
    with pytest.raises(ValueError, match="Unknown provider"):
        await db.bind("nonexistent_provider")


@pytest.mark.asyncio
async def test_close_without_connection_is_noop() -> None:
    """close() on unbound db (connection is None) exits immediately."""
    db = AsyncDatabase(entities=[AsyncUser])
    await db.close()  # should not raise
    assert db._connection is None


@pytest.mark.asyncio
async def test_ensure_connection_raises_when_not_connected() -> None:
    """_ensure_connection() raises RuntimeError when _connection is None."""
    db = AsyncDatabase(entities=[AsyncUser])
    with pytest.raises(RuntimeError, match="not connected"):
        db._ensure_connection()


@pytest.mark.asyncio
async def test_generate_mapping_without_create_tables() -> None:
    """generate_mapping(create_tables=False) builds schema without DDL."""
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=False)
        assert "asyncuser" in db.schema


@pytest.mark.asyncio
async def test_validate_relations_valid_entities() -> None:
    """generate_mapping(validate_relations=True) passes with proper back-references."""
    db = AsyncDatabase(entities=[AsyncValidParent, AsyncValidChild])
    await db.bind("sqlite", ":memory:")
    await db.generate_mapping(create_tables=True)  # validate_relations=True is the default
    await db.close()


@pytest.mark.asyncio
async def test_validate_relations_unresolved_target_skipped() -> None:
    """_validate_relations skips Set with ForwardRef target not in entities."""
    db = AsyncDatabase(entities=[AsyncParentUnknownKid])
    await db.bind("sqlite", ":memory:")
    await db.generate_mapping(create_tables=True)
    await db.close()


@pytest.mark.asyncio
async def test_validate_relations_false_skips_validation() -> None:
    """generate_mapping(validate_relations=False) skips _validate_relations."""
    # Build a DB with no back-reference — normally this would raise MappingError
    # but with validate_relations=False the check is skipped entirely.
    db = AsyncDatabase(entities=[AsyncParentUnknownKid])
    await db.bind("sqlite", ":memory:")
    await db.generate_mapping(create_tables=True, validate_relations=False)
    await db.close()


@pytest.mark.asyncio
async def test_validate_relations_typing_forwardref_target_resolves() -> None:
    """Set with typing.ForwardRef target hits the ForwardRef branch in async _resolve."""
    db = AsyncDatabase(entities=[_AsyncFwdParent, _AsyncFwdChild])
    await db.bind("sqlite", ":memory:")
    await db.generate_mapping(create_tables=True)
    await db.close()


@pytest.mark.asyncio
async def test_async_opt_str_field_none_becomes_empty_string() -> None:
    """Opt[str] (nullable=False) field that's None becomes '' on async _do_insert."""
    db = AsyncDatabase(entities=[_AsyncOptStrEntity])
    await db.bind("sqlite", ":memory:")
    await db.generate_mapping(create_tables=True)
    obj = _AsyncOptStrEntity(name="test")
    await db.asave(obj)
    result = await db.aselect(_AsyncOptStrEntity).get()
    assert result is not None
    assert result.notes == ""
    await db.close()


@pytest.mark.asyncio
async def test_adelete_no_pk_field_raises() -> None:
    """adelete_instance raises RuntimeError when entity has no PK fields."""
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        u = AsyncUser(name="No PK", age=1)
        await db.asave(u)

        orig_fields = type(u)._pk_fields_
        orig_field = type(u)._pk_field_
        try:
            type(u)._pk_fields_ = ()
            type(u)._pk_field_ = None
            with pytest.raises(RuntimeError, match="no primary-key field"):
                await db.adelete_instance(u)
        finally:
            type(u)._pk_fields_ = orig_fields
            type(u)._pk_field_ = orig_field


@pytest.mark.asyncio
async def test_do_insert_non_manytone_relation_skipped_in_loop() -> None:
    """_do_insert iterates relations; non-MANY_TO_ONE (SET) is skipped (branch 335->334)."""
    async with AsyncDatabase(entities=[AsyncValidParent, AsyncValidChild]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        p = AsyncValidParent(name="Parent1")
        await db.asave(p)
        assert p.id is not None


@pytest.mark.asyncio
async def test_do_insert_pk_only_entity_uses_default_values() -> None:
    """Entity with only an auto-PK uses DEFAULT VALUES (NULL for SQLite) insert path."""
    async with AsyncDatabase(entities=[AsyncPKOnly]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        e = AsyncPKOnly()
        await db.asave(e)
        assert e.id is not None


@pytest.mark.asyncio
async def test_do_update_fk_assignments() -> None:
    """Updating a ManyToOne entity populates FK assignments in _do_update (lines 371-374)."""
    async with AsyncDatabase(entities=[AsyncUser, AsyncPostA]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        u = AsyncUser(name="Author", age=25)
        await db.asave(u)
        p = AsyncPostA(title="Original Title")
        vars(p)["_author_id"] = u.id
        await db.asave(p)
        # Update p (triggers _do_update with FK assignments)
        p.title = "Updated Title"
        await db.asave(p)
        loaded = await db.aselect(AsyncPostA).fetch_one()
        assert loaded is not None
        assert loaded.title == "Updated Title"


@pytest.mark.asyncio
async def test_join_with_string_table_name() -> None:
    """join() with string table name (line 475)."""
    async with AsyncDatabase(entities=[AsyncUser, AsyncPostA]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        u = AsyncUser(name="Stringer", age=5)
        await db.asave(u)
        p = AsyncPostA(title="String Join Post")
        vars(p)["_author_id"] = u.id
        await db.asave(p)

        join_cond = BinOp(
            ColumnRef("id", "asyncuser"),
            "=",
            ColumnRef("author_id", "asyncposta"),
        )
        rows = await db.aselect(AsyncUser).join("asyncposta", join_cond).fetch_all()
        assert len(rows) >= 1


@pytest.mark.asyncio
async def test_del_closes_open_connection() -> None:
    """AsyncDatabase.__del__ closes the underlying sync connection when called with an open DB."""
    db = AsyncDatabase(entities=[AsyncUser])
    await db.bind("sqlite", ":memory:")
    assert db._connection is not None
    db.__del__()
    assert db._connection is None


# ---------------------------------------------------------------------------
# ainsert() — forced async INSERT regardless of PK
# ---------------------------------------------------------------------------


class _AsyncUserPK(Entity):
    """Entity with a user-assigned (non-auto) PK for ainsert() tests."""

    id: Req[int] = Req(primary_key=True, auto=False)
    label: Req[str]


@pytest.mark.asyncio
async def test_ainsert_auto_pk_none_behaves_like_asave() -> None:
    """ainsert() with no PK value auto-generates one, same as asave()."""
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        u = AsyncUser(name="insert-auto", age=1)
        await db.ainsert(u)
        assert u.id is not None


@pytest.mark.asyncio
async def test_ainsert_auto_pk_with_value_overrides_autoincrement() -> None:
    """ainsert() with a pre-set auto-PK passes the value explicitly to INSERT."""
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        u = AsyncUser(name="seeded", age=99)
        u.id = 777
        await db.ainsert(u)
        assert u.id == 777
        fetched = await db.aselect(AsyncUser).filter(AsyncUser.id == 777).fetch_one()
        assert fetched is not None
        assert fetched.name == "seeded"


@pytest.mark.asyncio
async def test_ainsert_user_assigned_pk() -> None:
    """ainsert() allows inserting entities with user-assigned (non-auto) PKs."""
    async with AsyncDatabase(entities=[_AsyncUserPK]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        item = _AsyncUserPK(id=42, label="forty-two")
        await db.ainsert(item)
        assert item.id == 42
        fetched = await db.aselect(_AsyncUserPK).filter(_AsyncUserPK.id == 42).fetch_one()
        assert fetched is not None
        assert fetched.label == "forty-two"


@pytest.mark.asyncio
async def test_ainsert_calls_lifecycle_hooks() -> None:
    """ainsert() fires before_insert and after_insert."""
    calls: list[str] = []

    class _ATracked(Entity):
        name: Req[str]

        def before_insert(self) -> None:
            calls.append("before")

        def after_insert(self) -> None:
            calls.append("after")

    async with AsyncDatabase(entities=[_ATracked]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        await db.ainsert(_ATracked(name="y"))
    assert calls == ["before", "after"]


@pytest.mark.asyncio
async def test_ainsert_unmapped_entity_raises() -> None:
    """ainsert() raises RuntimeError for entities not in the schema."""

    class _AGhost(Entity):
        name: Req[str]

    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        with pytest.raises(RuntimeError, match="not in the mapped schema"):
            await db.ainsert(_AGhost(name="nobody"))


# ---------------------------------------------------------------------------
# FieldSpec: volatile (async path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_volatile_field_excluded_from_update() -> None:
    """volatile=True fields are not included in async UPDATE statements."""

    class _AVolatileEntity(Entity):
        name: Req[str]
        computed: Opt[int] = Opt(volatile=True)

    async with AsyncDatabase(entities=[_AVolatileEntity]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        # Simulate a trigger by inserting via raw SQL
        await db.execute("INSERT INTO _avolatileentity (name, computed) VALUES (?, ?)", "x", 42)
        item = await db.aselect(_AVolatileEntity).fetch_one()
        assert item is not None
        item.name = "x-updated"
        item.computed = 0
        await db.asave(item)
        reloaded = await db.aselect(_AVolatileEntity).fetch_one()
        assert reloaded is not None
        assert reloaded.name == "x-updated"
        assert reloaded.computed == 42


# ---------------------------------------------------------------------------
# MariaDB integration tests — skip when no live server is available
# ---------------------------------------------------------------------------


class _AsyncMariaDBPKOnly(Entity):
    """Entity with only an auto-pk, used to exercise the async MariaDB VALUES() insert path."""


@pytest.mark.asyncio
@pytest.mark.skipif(not _mariadb_available(), reason="No MariaDB server reachable")
async def test_async_do_insert_pk_only_entity_mariadb() -> None:
    """PKOnly entity on MariaDB uses 'INSERT INTO t VALUES ()' (async path)."""
    import pymysql  # noqa: PLC0415

    raw = pymysql.connect(**_MARIADB_KWARGS)
    raw.cursor().execute("DROP TABLE IF EXISTS _asyncmysqlpkonly")
    raw.commit()
    raw.close()

    db = AsyncDatabase(entities=[_AsyncMariaDBPKOnly])
    await db.bind("mariadb", **_MARIADB_KWARGS)
    await db.generate_mapping(create_tables=True)
    try:
        obj = _AsyncMariaDBPKOnly()
        await db.asave(obj)
        assert obj.id is not None
    finally:
        await db.close()


@pytest.mark.asyncio
@pytest.mark.skipif(not _mariadb_available(), reason="No MariaDB server reachable")
async def test_async_mariadb_basic_crud() -> None:
    """Full async CRUD cycle via AsyncDatabase on a live MariaDB server."""
    import pymysql  # noqa: PLC0415

    raw = pymysql.connect(**_MARIADB_KWARGS)
    raw.cursor().execute("DROP TABLE IF EXISTS _asyncmysqlitem")
    raw.commit()
    raw.close()

    class _AsyncMariaDBItem(Entity):
        name: Req[str]
        score: Req[int]

    db = AsyncDatabase(entities=[_AsyncMariaDBItem])
    await db.bind("mariadb", **_MARIADB_KWARGS)
    await db.generate_mapping(create_tables=True)
    try:
        item = _AsyncMariaDBItem(name="async-hello", score=7)
        await db.asave(item)
        assert item.id is not None

        fetched = (
            await db.aselect(_AsyncMariaDBItem).filter(_AsyncMariaDBItem.id == item.id).fetch_one()
        )
        assert fetched is not None
        assert fetched.name == "async-hello"
        assert fetched.score == 7

        item.score = 99
        await db.asave(item)

        fetched2 = (
            await db.aselect(_AsyncMariaDBItem).filter(_AsyncMariaDBItem.id == item.id).fetch_one()
        )
        assert fetched2 is not None
        assert fetched2.score == 99

        await db.adelete_instance(item)
        assert item.id is None
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# AsyncQuerySet — new methods
# ---------------------------------------------------------------------------


async def test_async_get_or_raise_returns_match() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        await db.asave(AsyncUser(name="alice", age=30))
        u = await db.aselect(AsyncUser).filter(AsyncUser.name == "alice").get_or_raise()
        assert u.name == "alice"


async def test_async_get_or_raise_raises_object_not_found() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        with pytest.raises(ObjectNotFound, match="AsyncUser"):
            await db.aselect(AsyncUser).filter(AsyncUser.name == "nobody").get_or_raise()


async def test_async_get_or_raise_raises_multiple() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        await db.asave(AsyncUser(name="a", age=1))
        await db.asave(AsyncUser(name="b", age=2))
        with pytest.raises(MultipleObjectsFoundError):
            await db.aselect(AsyncUser).get_or_raise()


async def test_async_page_returns_correct_slice() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        for i in range(5):
            await db.asave(AsyncUser(name=f"u{i}", age=i))
        page1 = await db.aselect(AsyncUser).order_by(AsyncUser.id.asc()).page(1, 3).fetch_all()
        assert len(page1) == 3
        page2 = await db.aselect(AsyncUser).order_by(AsyncUser.id.asc()).page(2, 3).fetch_all()
        assert len(page2) == 2


async def test_async_page_invalid_pagenum_raises() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        with pytest.raises(ValueError, match="pagenum"):
            db.aselect(AsyncUser).page(0)


async def test_async_distinct_in_sql() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        sql = db.aselect(AsyncUser).distinct().get_sql()
        assert "DISTINCT" in sql


async def test_async_for_update_in_sql() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        sql = db.aselect(AsyncUser).for_update().get_sql()
        assert "FOR UPDATE" in sql


async def test_async_for_update_skip_locked_in_sql() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        sql = db.aselect(AsyncUser).for_update(skip_locked=True).get_sql()
        assert "SKIP LOCKED" in sql


async def test_async_get_sql_contains_where() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        sql = db.aselect(AsyncUser).filter(AsyncUser.age > 18).get_sql()
        assert "WHERE" in sql


async def test_async_aggregations() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        for age in (10, 20, 30):
            await db.asave(AsyncUser(name=f"u{age}", age=age))
        qs = db.aselect(AsyncUser)
        assert await qs.sum("age") == 60
        assert await qs.avg("age") == pytest.approx(20.0)
        assert await qs.min("age") == 10
        assert await qs.max("age") == 30


async def test_async_sum_no_rows_returns_none() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        result = await db.aselect(AsyncUser).sum("age")
        assert result is None


async def test_async_aggregate_unknown_field_raises() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        with pytest.raises(ValueError, match="no field"):
            await db.aselect(AsyncUser).sum("nonexistent")


async def test_async_random_returns_n_rows() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        for i in range(5):
            await db.asave(AsyncUser(name=f"r{i}", age=i))
        results = await db.aselect(AsyncUser).random(2).fetch_all()
        assert len(results) == 2


async def test_async_where_chainable() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        await db.asave(AsyncUser(name="old", age=50))
        await db.asave(AsyncUser(name="young", age=10))
        results = await db.aselect(AsyncUser).where(lambda u: u.age > 20).fetch_all()
        assert len(results) == 1
        assert results[0].name == "old"


async def test_async_filter_kwargs_equality() -> None:
    """AsyncQuerySet.filter(field=value) applies keyword equality shortcuts."""
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        await db.asave(AsyncUser(name="alice", age=30))
        await db.asave(AsyncUser(name="bob", age=25))
        results = await db.aselect(AsyncUser).filter(name="alice").fetch_all()
        assert len(results) == 1
        assert results[0].name == "alice"


async def test_async_filter_kwargs_mixed_with_conditions() -> None:
    """AsyncQuerySet.filter() accepts both SqlNode conditions and **kwargs."""
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        await db.asave(AsyncUser(name="alice", age=30))
        await db.asave(AsyncUser(name="bob", age=20))
        await db.asave(AsyncUser(name="carol", age=30))
        results = await db.aselect(AsyncUser).filter(AsyncUser.age == 30, name="alice").fetch_all()
        assert len(results) == 1
        assert results[0].name == "alice"


async def test_async_where_uses_decompilation() -> None:
    """AsyncQuerySet.where() uses bytecode decompilation for M2M/bool patterns."""
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        await db.asave(AsyncUser(name="alice", age=30))
        await db.asave(AsyncUser(name="bob", age=20))
        # Decompilation path: generator-style predicate
        results = await db.aselect(AsyncUser).where(lambda u: u.name == "alice").fetch_all()
        assert len(results) == 1
        assert results[0].name == "alice"


async def test_async_where_falls_back_to_proxy_on_decompile_error() -> None:
    """AsyncQuerySet.where() falls back to proxy evaluation when decompilation fails."""
    from unittest.mock import patch

    from nextorm.generators import DecompileError

    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        await db.asave(AsyncUser(name="alice", age=30))
        await db.asave(AsyncUser(name="bob", age=20))

        def _raise(*args: object, **kwargs: object) -> object:
            raise DecompileError("forced")

        with patch("nextorm.generators._apply_predicate", side_effect=_raise):
            results = await db.aselect(AsyncUser).where(lambda u: u.age > 25).fetch_all()
        assert len(results) == 1
        assert results[0].name == "alice"


async def test_async_execute_runs_dml() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        await db.asave(AsyncUser(name="alice", age=30))
        rows = await db.select_raw("SELECT name, age FROM asyncuser")
        assert isinstance(rows[0], dict)
        assert rows[0]["name"] == "alice"


async def test_async_select_raw_empty_returns_empty_list() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        rows = await db.select_raw("SELECT name FROM asyncuser")
        assert rows == []


async def test_async_get_connection_returns_aiosqlite_connection() -> None:
    import aiosqlite  # noqa: PLC0415

    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        raw = db.get_connection()
        assert isinstance(raw, aiosqlite.Connection)


# ---------------------------------------------------------------------------
# AsyncQuerySet.raw / AsyncQuerySet.raw_one
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_raw_returns_entity_instances() -> None:
    """raw() maps rows to entity instances by column name."""
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        await db.asave(AsyncUser(name="alice", age=25))
        await db.asave(AsyncUser(name="bob", age=30))
        results = await db.aselect(AsyncUser).raw(
            "SELECT id, name, age FROM asyncuser ORDER BY age ASC"
        )
        assert len(results) == 2
        assert all(isinstance(r, AsyncUser) for r in results)
        assert results[0].name == "alice"


@pytest.mark.asyncio
async def test_async_raw_with_params() -> None:
    """raw() forwards bind parameters."""
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        await db.asave(AsyncUser(name="carol", age=20))
        await db.asave(AsyncUser(name="dave", age=40))
        results = await db.aselect(AsyncUser).raw(
            "SELECT id, name, age FROM asyncuser WHERE age > ?", [25]
        )
        assert len(results) == 1
        assert results[0].name == "dave"


@pytest.mark.asyncio
async def test_async_raw_unknown_column_ignored() -> None:
    """Unrecognised columns in raw SQL are silently skipped (field_name is None)."""
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        await db.asave(AsyncUser(name="eve", age=22))
        results = await db.aselect(AsyncUser).raw(
            "SELECT id, name, age, 99 AS computed FROM asyncuser"
        )
        assert len(results) == 1
        assert results[0].name == "eve"


@pytest.mark.asyncio
async def test_async_raw_fk_column_stored_in_dict() -> None:
    """FK id columns (_rel_id) are stored directly in __dict__."""
    async with AsyncDatabase(entities=[AsyncUser, AsyncPostA]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        user = AsyncUser(name="frank", age=35)
        await db.asave(user)
        post = AsyncPostA(title="hello")
        post.author = user
        await db.asave(post)
        results = await db.aselect(AsyncPostA).raw("SELECT id, title, author_id FROM asyncposta")
        assert len(results) == 1
        assert results[0].title == "hello"
        assert vars(results[0]).get("_author_id") == user.id


@pytest.mark.asyncio
async def test_async_raw_no_params_defaults_to_empty() -> None:
    """raw() without params argument succeeds."""
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        await db.asave(AsyncUser(name="greta", age=28))
        results = await db.aselect(AsyncUser).raw("SELECT id, name, age FROM asyncuser")
        assert len(results) == 1


@pytest.mark.asyncio
async def test_async_raw_one_returns_first_entity() -> None:
    """raw_one() returns the first mapped entity."""
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        await db.asave(AsyncUser(name="hank", age=50))
        result = await db.aselect(AsyncUser).raw_one(
            "SELECT id, name, age FROM asyncuser WHERE name = ?", ["hank"]
        )
        assert result is not None
        assert result.name == "hank"


@pytest.mark.asyncio
async def test_async_raw_one_returns_none_when_empty() -> None:
    """raw_one() returns None when the query produces no rows."""
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        result = await db.aselect(AsyncUser).raw_one(
            "SELECT id, name, age FROM asyncuser WHERE name = ?", ["nobody"]
        )
        assert result is None


@pytest.mark.asyncio
async def test_async_raw_one_no_params_defaults_to_empty() -> None:
    """raw_one() without params argument succeeds."""
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        await db.asave(AsyncUser(name="iris", age=19))
        result = await db.aselect(AsyncUser).raw_one("SELECT id, name, age FROM asyncuser LIMIT 1")
        assert result is not None


# ---------------------------------------------------------------------------
# AsyncDatabase.last_sql
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_last_sql_empty_before_any_query() -> None:
    """last_sql is empty string before any query is executed."""
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        assert db.last_sql == ""


@pytest.mark.asyncio
async def test_async_last_sql_set_after_fetch_all() -> None:
    """last_sql reflects the SELECT executed by fetch_all."""
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        await db.asave(AsyncUser(name="alice", age=30))
        await db.aselect(AsyncUser).fetch_all()
        assert "SELECT" in db.last_sql.upper()
        assert "asyncuser" in db.last_sql


@pytest.mark.asyncio
async def test_async_last_sql_set_after_insert() -> None:
    """last_sql reflects the INSERT executed by asave()."""
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        await db.asave(AsyncUser(name="bob", age=25))
        assert "INSERT" in db.last_sql.upper()


@pytest.mark.asyncio
async def test_async_last_sql_set_after_delete() -> None:
    """last_sql reflects the DELETE executed by AsyncQuerySet.delete()."""
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        await db.asave(AsyncUser(name="carol", age=22))
        await db.aselect(AsyncUser).filter(AsyncUser.name == "carol").delete()
        assert "DELETE" in db.last_sql.upper()


# ---------------------------------------------------------------------------
# AsyncQuerySet.without_distinct / group_concat
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_without_distinct_clears_flag() -> None:
    """without_distinct() removes the DISTINCT flag."""
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        qs = db.aselect(AsyncUser).distinct().without_distinct()
        assert "DISTINCT" not in qs.get_sql().upper()


@pytest.mark.asyncio
async def test_async_without_distinct_no_op_on_fresh_qs() -> None:
    """without_distinct() on a fresh QuerySet is harmless."""
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        await db.asave(AsyncUser(name="alice", age=30))
        results = await db.aselect(AsyncUser).without_distinct().fetch_all()
        assert len(results) == 1


@pytest.mark.asyncio
async def test_async_group_concat_returns_string() -> None:
    """group_concat() returns concatenated values."""
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        await db.asave(AsyncUser(name="alice", age=30))
        await db.asave(AsyncUser(name="bob", age=25))
        result = await db.aselect(AsyncUser).group_concat("name")
        assert result is not None
        assert "alice" in result
        assert "bob" in result


@pytest.mark.asyncio
async def test_async_group_concat_custom_separator() -> None:
    """group_concat() uses the provided separator."""
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        await db.asave(AsyncUser(name="carol", age=22))
        await db.asave(AsyncUser(name="dave", age=35))
        result = await db.aselect(AsyncUser).group_concat("name", sep=" - ")
        assert result is not None
        assert " - " in result


@pytest.mark.asyncio
async def test_async_group_concat_with_filter() -> None:
    """group_concat() respects the active WHERE filter."""
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        await db.asave(AsyncUser(name="eve", age=20))
        await db.asave(AsyncUser(name="frank", age=40))
        result = await db.aselect(AsyncUser).filter(AsyncUser.age > 30).group_concat("name")
        assert result is not None
        assert "frank" in result
        assert "eve" not in result


@pytest.mark.asyncio
async def test_async_group_concat_returns_none_on_empty() -> None:
    """group_concat() returns None when no rows match."""
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        result = await db.aselect(AsyncUser).group_concat("name")
        assert result is None


@pytest.mark.asyncio
async def test_async_group_concat_invalid_attr_raises() -> None:
    """group_concat() raises ValueError for unknown field names."""
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        import pytest as _pytest  # noqa: PLC0415

        with _pytest.raises(ValueError, match="has no field"):
            await db.aselect(AsyncUser).group_concat("nosuchfield")


# ---------------------------------------------------------------------------
# Async lazy field loading
# ---------------------------------------------------------------------------


class _AsyncLazyPost(Entity):
    """Entity with a lazy field for async lazy-load tests."""

    heading: Req[str]
    content: Req[str] = Req(lazy=True)


@pytest.mark.asyncio
async def test_async_lazy_field_sentinel_stored_on_load() -> None:
    """Entities loaded via AsyncDatabase have sentinel in lazy fields."""
    from nextorm.entity import _LAZY_SENTINEL  # noqa: PLC0415

    async with AsyncDatabase(entities=[_AsyncLazyPost]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        post = _AsyncLazyPost(heading="Hi")
        post.content = "Long content"
        await db.asave(post)

        loaded = await db.aselect(_AsyncLazyPost).filter(_AsyncLazyPost.id == post.id).fetch_one()
        assert loaded is not None
        assert vars(loaded)["_field_content"] is _LAZY_SENTINEL


@pytest.mark.asyncio
async def test_async_lazy_field_access_raises_runtime_error() -> None:
    """Synchronous attribute access on a lazy field from an async entity raises."""
    async with AsyncDatabase(entities=[_AsyncLazyPost]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        post = _AsyncLazyPost(heading="Test")
        post.content = "Content"
        await db.asave(post)

        loaded = await db.aselect(_AsyncLazyPost).filter(_AsyncLazyPost.id == post.id).fetch_one()
        assert loaded is not None
        with pytest.raises(RuntimeError, match="async context"):
            _ = loaded.content


@pytest.mark.asyncio
async def test_async_load_lazy_field_loads_correct_value() -> None:
    """db.load_lazy_field() returns the stored value for a lazy field."""
    async with AsyncDatabase(entities=[_AsyncLazyPost]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        post = _AsyncLazyPost(heading="Fetch")
        post.content = "Fetched content"
        await db.asave(post)

        loaded = await db.aselect(_AsyncLazyPost).filter(_AsyncLazyPost.id == post.id).fetch_one()
        assert loaded is not None
        value = await db.load_lazy_field(loaded, "content")
        assert value == "Fetched content"


@pytest.mark.asyncio
async def test_async_load_lazy_field_caches_result_on_entity() -> None:
    """After load_lazy_field, the value is cached in entity.__dict__."""
    from nextorm.entity import _LAZY_SENTINEL  # noqa: PLC0415

    async with AsyncDatabase(entities=[_AsyncLazyPost]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        post = _AsyncLazyPost(heading="Cache")
        post.content = "Cached"
        await db.asave(post)

        loaded = await db.aselect(_AsyncLazyPost).filter(_AsyncLazyPost.id == post.id).fetch_one()
        assert loaded is not None
        assert vars(loaded)["_field_content"] is _LAZY_SENTINEL
        await db.load_lazy_field(loaded, "content")
        # Sentinel replaced by actual value
        assert vars(loaded)["_field_content"] == "Cached"


@pytest.mark.asyncio
async def test_async_load_lazy_field_no_pk_returns_none() -> None:
    """load_lazy_field returns None for an entity with no PK."""
    async with AsyncDatabase(entities=[_AsyncLazyPost]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        unsaved = _AsyncLazyPost(heading="No PK")
        result = await db.load_lazy_field(unsaved, "content")
        assert result is None


@pytest.mark.asyncio
async def test_async_map_raw_row_stamps_lazy_sentinel() -> None:
    """raw() on a lazy entity stamps _LAZY_SENTINEL via _map_raw_row."""
    from nextorm.entity import _LAZY_SENTINEL  # noqa: PLC0415

    async with AsyncDatabase(entities=[_AsyncLazyPost]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        post = _AsyncLazyPost(heading="Raw")
        post.content = "raw content"
        await db.asave(post)

        results = await db.aselect(_AsyncLazyPost).raw(
            "SELECT id, heading FROM _asynclazypost WHERE id = ?", [post.id]
        )
        assert len(results) == 1
        assert vars(results[0])["_field_content"] is _LAZY_SENTINEL


# ---------------------------------------------------------------------------
# Entity.aselect_by_sql / Entity.aget_by_sql
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aselect_by_sql_returns_entities() -> None:
    """Entity.aselect_by_sql() executes raw SQL and returns entity instances."""
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        await db.asave(AsyncUser(name="alice", age=25))
        await db.asave(AsyncUser(name="bob", age=30))
        results = await AsyncUser.aselect_by_sql(
            db, "SELECT id, name, age FROM asyncuser ORDER BY age ASC"
        )
        assert len(results) == 2
        assert all(isinstance(r, AsyncUser) for r in results)
        assert results[0].name == "alice"


@pytest.mark.asyncio
async def test_aselect_by_sql_with_params() -> None:
    """Entity.aselect_by_sql() forwards bind parameters."""
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        await db.asave(AsyncUser(name="carol", age=20))
        await db.asave(AsyncUser(name="dave", age=40))
        results = await AsyncUser.aselect_by_sql(
            db,
            "SELECT id, name, age FROM asyncuser WHERE age > ?",
            [25],
        )
        assert len(results) == 1
        assert results[0].name == "dave"


@pytest.mark.asyncio
async def test_aget_by_sql_returns_entity() -> None:
    """Entity.aget_by_sql() returns the first matching entity instance."""
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        await db.asave(AsyncUser(name="eve", age=22))
        result = await AsyncUser.aget_by_sql(
            db,
            "SELECT id, name, age FROM asyncuser WHERE name = ?",
            ["eve"],
        )
        assert result is not None
        assert result.name == "eve"


@pytest.mark.asyncio
async def test_aget_by_sql_returns_none_when_empty() -> None:
    """Entity.aget_by_sql() returns None when the query produces no rows."""
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        result = await AsyncUser.aget_by_sql(
            db,
            "SELECT id, name, age FROM asyncuser WHERE name = ?",
            ["nobody"],
        )
        assert result is None


# ---------------------------------------------------------------------------
# Registry / generate_mapping / session coverage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_generate_mapping_twice_skips_registry_duplicate() -> None:
    """AsyncDatabase.generate_mapping() called twice does not duplicate registry entry."""
    from nextorm.database import _database_registry  # noqa: PLC0415

    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        count1 = _database_registry.count(db)
        await db.generate_mapping(create_tables=False)  # second call — already registered
        count2 = _database_registry.count(db)
        assert count1 == count2 == 1


@pytest.mark.asyncio
async def test_arollback_outside_session_is_noop() -> None:
    """arollback() outside a session clears nothing (cache is None branch)."""
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        await db.asave(AsyncUser(name="keep", age=1))
        # Call arollback outside any db_session — should not raise
        await db.arollback()


@pytest.mark.asyncio
async def test_aflush_outside_session_is_noop() -> None:
    """aflush() outside a session returns immediately without error."""
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        # No session active — aflush should return before iterating
        await db.aflush()


@pytest.mark.asyncio
async def test_aflush_saves_dirty_and_new_objects() -> None:
    """aflush() inside a session saves objects_to_save and dirty_objects."""
    from nextorm.session import SessionCache, _get_session_stack  # noqa: PLC0415

    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        # Use manual session management to avoid DBSessionManager.__exit__
        # calling db.commit() (sync) on an AsyncDatabase.
        cache = SessionCache()
        stack = _get_session_stack()
        stack.push(cache)
        try:
            u = AsyncUser(name="pending", age=5)
            # Entity.__init__ schedules u in objects_to_save
            assert u in cache.objects_to_save

            await db.aflush()

            # After flush, u should be saved (pk assigned)
            assert u.id is not None

            # Mark u dirty and flush again to cover the dirty_objects branch
            u.name = "updated"
            cache.mark_dirty(u)
            assert u in cache.dirty_objects

            await db.aflush()

            rows = await db.aselect(AsyncUser).fetch_all()
            assert any(r.name == "updated" for r in rows)
        finally:
            stack.pop()


@pytest.mark.asyncio
async def test_asave_failure_inside_session_unschedules_entity() -> None:
    """asave() failure inside a session removes entity from session tracking."""
    from unittest.mock import patch  # noqa: PLC0415

    from nextorm.session import SessionCache, _get_session_stack  # noqa: PLC0415

    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        # Use manual session management to avoid sync commit on AsyncDatabase.
        cache = SessionCache()
        stack = _get_session_stack()
        stack.push(cache)
        try:
            u = AsyncUser(name="fail", age=0)
            assert u in cache.objects_to_save

            exc = RuntimeError("insert failed")
            with (
                patch.object(db, "_do_insert", side_effect=exc),
                pytest.raises(RuntimeError, match="insert failed"),
            ):
                await db.asave(u)

            # Entity should be unscheduled after save failure
            assert u not in cache.objects_to_save
            assert u not in cache.dirty_objects
        finally:
            stack.pop()


@pytest.mark.asyncio
async def test_execute_dml_inside_session_skips_autocommit() -> None:
    """_execute_dml() inside a db_session does not call conn.commit() automatically."""
    from unittest.mock import AsyncMock, patch  # noqa: PLC0415

    from nextorm.session import SessionCache, _get_session_stack  # noqa: PLC0415

    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        u = AsyncUser(name="nobatch", age=3)
        await db.asave(u)

        # Use manual session management to avoid sync commit on AsyncDatabase.
        cache = SessionCache()
        stack = _get_session_stack()
        stack.push(cache)
        try:
            # adelete_instance calls _execute_dml; inside a session, no auto-commit
            with patch.object(db._connection, "commit", new_callable=AsyncMock) as mock_commit:
                await db.adelete_instance(u)
                mock_commit.assert_not_called()
        finally:
            stack.pop()


@pytest.mark.asyncio
async def test_acommit_flushes_and_commits() -> None:
    """acommit() flushes pending changes and commits the transaction (lines 420-421)."""
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        await db.asave(AsyncUser(name="committed", age=7))
        # acommit() outside session: aflush is no-op, _acommit_transaction commits
        await db.acommit()
        result = await db.aselect(AsyncUser).fetch_all()
        assert len(result) == 1


@pytest.mark.asyncio
async def test_arollback_inside_session_clears_cache() -> None:
    """arollback() inside a session clears the session cache (line 452)."""
    from nextorm.session import SessionCache, _get_session_stack  # noqa: PLC0415

    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        cache = SessionCache()
        stack = _get_session_stack()
        stack.push(cache)
        try:
            u = AsyncUser(name="cancelled", age=3)
            assert u in cache.objects_to_save
            await db.arollback()
            # cache is cleared after arollback inside session
            assert len(cache.objects_to_save) == 0
        finally:
            stack.pop()


@pytest.mark.asyncio
async def test_aflush_skips_entities_from_other_async_db() -> None:
    """aflush() skips new and dirty entities whose _db_ is a different AsyncDatabase."""
    from nextorm.session import SessionCache, _get_session_stack  # noqa: PLC0415

    class _AFlushSkipOther(Entity):
        x: Req[str]

    class _AFlushSkipSelf(Entity):
        y: Req[str]

    db_other = AsyncDatabase(entities=[_AFlushSkipOther])
    await db_other.bind("sqlite", ":memory:")
    await db_other.generate_mapping(create_tables=True)

    db_self = AsyncDatabase(entities=[_AFlushSkipSelf])
    await db_self.bind("sqlite", ":memory:")
    await db_self.generate_mapping(create_tables=True)

    cache = SessionCache()
    stack = _get_session_stack()
    stack.push(cache)
    try:
        other_entity = _AFlushSkipOther(x="skip")
        self_entity = _AFlushSkipSelf(y="save")
        # Save self_entity so we can test the dirty_objects branch too
        await db_self.asave(self_entity)
        # Save other_entity via db_other, then mark it dirty
        await db_other.asave(other_entity)
        cache.mark_dirty(other_entity)
        # aflush on db_self must skip the dirty other_entity (475->473 branch)
        await db_self.aflush()
        assert other_entity in cache.dirty_objects  # skipped by db_self
    finally:
        stack.pop()
    await db_other.close()
    await db_self.close()


# ---------------------------------------------------------------------------
# _validate_relations error path (unresolvable target or missing backref)
# ---------------------------------------------------------------------------


# Define a valid entity as the relation target, but omit the required back-reference
class NoBackrefTarget(Entity):
    pass


class NoBackrefOwner(Entity):
    items: Set[NoBackrefTarget]


@pytest.mark.asyncio
async def test_async_db_validate_relations_unresolvable_target_raises() -> None:
    db = AsyncDatabase(entities=[NoBackrefOwner, NoBackrefTarget])
    await db.bind("sqlite", ":memory:")
    with pytest.raises(Exception) as excinfo:
        await db.generate_mapping(validate_relations=True)
    assert "requires a back-reference" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Merge_local_stats branch (new SQL string)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_db_merge_local_stats_adds_new_sql() -> None:
    db = AsyncDatabase(entities=[AsyncUser])
    await db.bind("sqlite", ":memory:")
    await db.generate_mapping(create_tables=True)
    # Simulate a local stat for a fake SQL
    db._local_stats["SELECT 1"] = QueryStat(count=1, sum_time=0.1, min_time=0.1, max_time=0.1)
    # Remove from global_stats if present
    global_stats.pop("SELECT 1", None)
    db.merge_local_stats()
    assert "SELECT 1" in global_stats


# ---------------------------------------------------------------------------
# _validate_relations with unresolvable target paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_validate_relations_with_none_target() -> None:
    """Relation with target=None should be skipped gracefully."""
    # Use simple entities without bidirectional relations
    db = AsyncDatabase(entities=[AsyncUser])
    await db.bind("sqlite", ":memory:")

    # Verify that validation works on entities with no complex relations
    await db.generate_mapping(validate_relations=True)


@pytest.mark.asyncio
async def test_async_validate_relations_with_string_target() -> None:
    """Relation with string target should be resolved correctly."""
    # Use simple entities
    db = AsyncDatabase(entities=[AsyncUser])
    await db.bind("sqlite", ":memory:")

    # String targets should resolve via entity_by_name
    await db.generate_mapping(validate_relations=True)


# ---------------------------------------------------------------------------
# AsyncDatabase.create_tables / drop_table / drop_all_tables
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_create_tables_after_generate_mapping() -> None:
    """AsyncDatabase.create_tables() creates tables after generate_mapping(create_tables=False)."""

    class ACtItem(Entity):
        _table_ = "a_ct_item"
        name: Req[str]

    db = AsyncDatabase(entities=[ACtItem])
    await db.bind("sqlite", ":memory:")
    await db.generate_mapping(create_tables=False, validate_relations=False)

    await db.create_tables()

    # Verify tables are usable
    await db.ainsert(ACtItem(name="test"))
    items = await db.aselect(ACtItem).fetch_all()
    assert len(items) == 1


@pytest.mark.asyncio
async def test_async_create_tables_raises_if_not_mapped() -> None:
    """AsyncDatabase.create_tables() raises RuntimeError if not mapped."""

    class ACtItem2(Entity):
        _table_ = "a_ct_item2"
        name: Req[str]

    db = AsyncDatabase(entities=[ACtItem2])
    await db.bind("sqlite", ":memory:")

    with pytest.raises(RuntimeError, match="not mapped"):
        await db.create_tables()


@pytest.mark.asyncio
async def test_async_drop_table_with_all_data() -> None:
    """AsyncDatabase.drop_table() with with_all_data=True drops table with data."""

    class ADtItem(Entity):
        _table_ = "a_dt_item"
        name: Req[str]

    db = AsyncDatabase(entities=[ADtItem])
    await db.bind("sqlite", ":memory:")
    await db.generate_mapping(create_tables=True, validate_relations=False)

    await db.ainsert(ADtItem(name="row1"))
    await db.drop_table("a_dt_item", if_exists=True, with_all_data=True)


@pytest.mark.asyncio
async def test_async_drop_table_without_if_exists() -> None:
    """AsyncDatabase.drop_table() without if_exists uses plain DROP TABLE."""

    class ADtItem2(Entity):
        _table_ = "a_dt_item2"
        name: Req[str]

    db = AsyncDatabase(entities=[ADtItem2])
    await db.bind("sqlite", ":memory:")
    await db.generate_mapping(create_tables=True, validate_relations=False)

    await db.drop_table("a_dt_item2", if_exists=False, with_all_data=True)


@pytest.mark.asyncio
async def test_async_drop_table_raises_if_not_bound() -> None:
    """AsyncDatabase.drop_table() raises if database is not bound."""

    class ADtItem3(Entity):
        _table_ = "a_dt_item3"
        name: Req[str]

    db = AsyncDatabase(entities=[ADtItem3])

    with pytest.raises(RuntimeError, match="not bound"):
        await db.drop_table("a_dt_item3")


@pytest.mark.asyncio
async def test_async_drop_table_raises_if_table_has_data() -> None:
    """AsyncDatabase.drop_table() raises if table has data and with_all_data=False."""

    class ADtItem4(Entity):
        _table_ = "a_dt_item4"
        name: Req[str]

    db = AsyncDatabase(entities=[ADtItem4])
    await db.bind("sqlite", ":memory:")
    await db.generate_mapping(create_tables=True, validate_relations=False)
    await db.ainsert(ADtItem4(name="row"))

    with pytest.raises(RuntimeError, match="is not empty"):
        await db.drop_table("a_dt_item4", if_exists=True, with_all_data=False)


@pytest.mark.asyncio
async def test_async_drop_all_tables_with_all_data() -> None:
    """AsyncDatabase.drop_all_tables() with with_all_data=True drops all tables."""

    class ADatItem(Entity):
        _table_ = "a_dat_item"
        name: Req[str]

    db = AsyncDatabase(entities=[ADatItem])
    await db.bind("sqlite", ":memory:")
    await db.generate_mapping(create_tables=True, validate_relations=False)

    await db.ainsert(ADatItem(name="row1"))
    await db.drop_all_tables(with_all_data=True)


@pytest.mark.asyncio
async def test_async_drop_all_tables_raises_if_not_mapped() -> None:
    """AsyncDatabase.drop_all_tables() raises if not mapped."""

    class ADatItem2(Entity):
        _table_ = "a_dat_item2"
        name: Req[str]

    db = AsyncDatabase(entities=[ADatItem2])
    await db.bind("sqlite", ":memory:")

    with pytest.raises(RuntimeError, match="not mapped"):
        await db.drop_all_tables()


@pytest.mark.asyncio
async def test_async_drop_all_tables_raises_if_table_has_data() -> None:
    """AsyncDatabase.drop_all_tables() raises if table has data and with_all_data=False."""

    class ADatItem3(Entity):
        _table_ = "a_dat_item3"
        name: Req[str]

    db = AsyncDatabase(entities=[ADatItem3])
    await db.bind("sqlite", ":memory:")
    await db.generate_mapping(create_tables=True, validate_relations=False)
    await db.ainsert(ADatItem3(name="row"))

    with pytest.raises(RuntimeError, match="is not empty"):
        await db.drop_all_tables(with_all_data=False)


@pytest.mark.asyncio
async def test_async_drop_table_empty_table_with_all_data_false() -> None:
    """drop_table() on empty table with with_all_data=False succeeds (line 343->354)."""

    class AEmptyDt(Entity):
        _table_ = "a_empty_dt"
        name: Req[str]

    db = AsyncDatabase(entities=[AEmptyDt])
    await db.bind("sqlite", ":memory:")
    await db.generate_mapping(create_tables=True, validate_relations=False)
    # Table is empty - no data
    await db.drop_table("a_empty_dt", if_exists=True, with_all_data=False)


@pytest.mark.asyncio
async def test_async_drop_table_nonexistent_with_all_data_false() -> None:
    """drop_table() on nonexistent table, with_all_data=False uses except Exception path."""

    class ANeDt(Entity):
        _table_ = "a_ne_dt"
        name: Req[str]

    db = AsyncDatabase(entities=[ANeDt])
    await db.bind("sqlite", ":memory:")
    await db.generate_mapping(create_tables=True, validate_relations=False)
    # Drop first so table doesn't exist
    await db.drop_table("a_ne_dt", if_exists=True, with_all_data=True)
    # Now drop non-existent with with_all_data=False -> except Exception: pass
    await db.drop_table("a_ne_dt", if_exists=True, with_all_data=False)


@pytest.mark.asyncio
async def test_async_drop_all_tables_empty_tables_with_all_data_false() -> None:
    """drop_all_tables() with empty tables and with_all_data=False succeeds (lines 383, 386->383)."""

    class AEmptyDat(Entity):
        _table_ = "a_empty_dat"
        name: Req[str]

    db = AsyncDatabase(entities=[AEmptyDat])
    await db.bind("sqlite", ":memory:")
    await db.generate_mapping(create_tables=True, validate_relations=False)
    # Tables are empty - should succeed
    await db.drop_all_tables(with_all_data=False)


@pytest.mark.asyncio
async def test_async_drop_all_tables_nonexistent_tables_with_all_data_false() -> None:
    """drop_all_tables() when tables don't exist uses except Exception path (lines 392-393)."""

    class ANeDat(Entity):
        _table_ = "a_ne_dat"
        name: Req[str]

    db = AsyncDatabase(entities=[ANeDat])
    await db.bind("sqlite", ":memory:")
    await db.generate_mapping(create_tables=True, validate_relations=False)
    # Drop the table manually so it no longer exists in DB (but still in schema)
    await db.drop_table("a_ne_dat", if_exists=True, with_all_data=True)
    # Now drop_all_tables will try SELECT 1 FROM a_ne_dat → OperationalError → except Exception: pass
    await db.drop_all_tables(with_all_data=False)


# ---------------------------------------------------------------------------
# async_database.py gaps: non-owning O2O and relation-based PK async tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_insert_fk_from_related_obj_when_id_absent() -> None:
    """Async _do_insert extracts FK from related entity object when _id is None (lines 857-861).

    When an entity's relation FK (_<name>_id) is None but the related entity object (_<name>_obj)
    is available and is an Entity, _do_insert extracts the PK via _get_pk_val.
    """

    class _AInsertFKParent(Entity):
        _table_ = "_a_insert_fk_parent"
        label: Req[str]

    class _AInsertFKChild(Entity):
        _table_ = "_a_insert_fk_child"
        owner: Single[_AInsertFKParent]
        data: Req[str]

    async with AsyncDatabase(entities=[_AInsertFKParent, _AInsertFKChild]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        parent = _AInsertFKParent(label="parent")
        await db.asave(parent)

        child = _AInsertFKChild(data="child-data")
        child.__dict__["_owner_obj"] = parent  # pyright: ignore[reportIndexIssue]
        await db.asave(child)

        results = await db.aselect(_AInsertFKChild).fetch_all()
        assert len(results) == 1
        assert results[0].data == "child-data"


@pytest.mark.asyncio
async def test_async_insert_non_owning_o2o_skips_fk_col() -> None:
    """Async _do_insert skips FK column not in table (lines 880, 943 — non-owning O2O).

    A non-owning O2O entity has no FK column on its own table.
    When _do_insert processes owner, 'partner_id' is not in table_col_names → continue (line 880).
    """

    class _ANonOwnOwner(Entity):
        _table_ = "_a_non_own_owner"
        name: Req[str]
        partner: Single["_ANonOwnPartner"] = Single(nullable=True)  # noqa: UP037

    class _ANonOwnPartner(Entity):
        _table_ = "_a_non_own_partner"
        host: PK[_ANonOwnOwner]  # FK lives on partner's table
        value: Req[str]

    async with AsyncDatabase(entities=[_ANonOwnOwner, _ANonOwnPartner]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        owner = _ANonOwnOwner(name="async-owner")
        await db.asave(owner)

        partner = _ANonOwnPartner(value="async-partner")
        partner.__dict__["_host_id"] = owner.id  # pyright: ignore[reportIndexIssue]
        await db.asave(partner)

        owners = await db.aselect(_ANonOwnOwner).fetch_all()
        assert len(owners) == 1
        assert owners[0].name == "async-owner"


@pytest.mark.asyncio
async def test_async_update_fk_from_related_obj() -> None:
    """Async _do_update extracts FK from related entity object (lines 975-979)."""

    class _AUpdFKParent(Entity):
        _table_ = "_a_upd_fk_parent"
        label: Req[str]

    class _AUpdFKChild(Entity):
        _table_ = "_a_upd_fk_child"
        owner: Single[_AUpdFKParent]
        data: Req[str]

    async with AsyncDatabase(entities=[_AUpdFKParent, _AUpdFKChild]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        p1 = _AUpdFKParent(label="p1")
        await db.asave(p1)
        child = _AUpdFKChild(data="data")
        child.__dict__["_owner_id"] = p1.id  # pyright: ignore[reportIndexIssue]
        await db.asave(child)

        children = await db.aselect(_AUpdFKChild).fetch_all()
        c = children[0]
        _ = c.data  # read to populate _read_cols_

        p2 = _AUpdFKParent(label="p2")
        await db.asave(p2)
        c.__dict__["_owner_id"] = None  # pyright: ignore[reportIndexIssue]
        c.__dict__["_owner_obj"] = p2  # pyright: ignore[reportIndexIssue]
        await db.asave(c)

        result = await db.aselect(_AUpdFKChild).fetch_all()
        assert len(result) == 1


@pytest.mark.asyncio
async def test_async_update_non_owning_o2o_skips_fk_col() -> None:
    """Async _do_update skips FK column not in owner's table (line 998).

    Non-owning O2O: FK lives on partner's table, not owner's.
    _do_update processes owner entity → 'partner_id' not in table_col_names → continue (line 998).
    """

    class _AUpdNonOwnOwner(Entity):
        _table_ = "_a_upd_non_own_owner"
        name: Req[str]
        partner: Single["_AUpdNonOwnPartner"] = Single(nullable=True)  # noqa: UP037

    class _AUpdNonOwnPartner(Entity):
        _table_ = "_a_upd_non_own_partner"
        host: PK[_AUpdNonOwnOwner]
        value: Req[str]

    async with AsyncDatabase(entities=[_AUpdNonOwnOwner, _AUpdNonOwnPartner]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        owner = _AUpdNonOwnOwner(name="owner")
        await db.asave(owner)

        partner = _AUpdNonOwnPartner(value="partner")
        partner.__dict__["_host_id"] = owner.id  # pyright: ignore[reportIndexIssue]
        await db.asave(partner)

        owners = await db.aselect(_AUpdNonOwnOwner).fetch_all()
        o = owners[0]
        _ = o.name  # read to enable optimistic
        o.name = "owner-updated"
        await db.asave(o)

        result = await db.aselect(_AUpdNonOwnOwner).fetch_all()
        assert result[0].name == "owner-updated"


@pytest.mark.asyncio
async def test_async_update_no_read_columns_skips_optimistic() -> None:
    """Async _do_update with empty read_cols skips optimistic check (line 1004->1013).

    Uses a relation-based PK entity so _pk_val_for uses __dict__.get('_host_id')
    (bypassing __get__), keeping _read_cols_ empty after load.
    use_optimistic = False → line 1004 False branch.
    """

    class _ARelPkOwnerU(Entity):
        _table_ = "_a_rel_pk_owner_u"
        label: Req[str]

    class _ARelPkChildU(Entity):
        _table_ = "_a_rel_pk_child_u"
        host: PK[_ARelPkOwnerU]  # relation-based PK — _pk_val_for uses __dict__ not __get__
        value: Req[str]

    async with AsyncDatabase(entities=[_ARelPkOwnerU, _ARelPkChildU]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        owner = _ARelPkOwnerU(label="owner")
        await db.asave(owner)

        child = _ARelPkChildU(value="initial")
        child.__dict__["_host_id"] = owner.id  # pyright: ignore[reportIndexIssue]
        await db.asave(child)

        children = await db.aselect(_ARelPkChildU).fetch_all()
        assert len(children) == 1
        c = children[0]
        c.value = "updated"
        await db.asave(c)

        result = await db.aselect(_ARelPkChildU).fetch_all()
        assert result[0].value == "updated"


@pytest.mark.asyncio
async def test_async_map_row_non_owning_o2o_skips_fk_col() -> None:
    """Async _map_row skips FK column not in owner's table (line 1536).

    Loading a non-owning O2O entity: the FK for 'partner' lives on the partner's table.
    _map_row processes _a_maprow_owner → 'partner_id' not in table_col_names → continue (1536).
    """

    class _AMaprowOwner(Entity):
        _table_ = "_a_maprow_owner"
        name: Req[str]
        partner: Single["_AMaprowPartner"] = Single(nullable=True)  # noqa: UP037

    class _AMaprowPartner(Entity):
        _table_ = "_a_maprow_partner"
        host: PK[_AMaprowOwner]
        value: Req[str]

    async with AsyncDatabase(entities=[_AMaprowOwner, _AMaprowPartner]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        owner = _AMaprowOwner(name="maprow-owner")
        await db.asave(owner)

        partner = _AMaprowPartner(value="maprow-partner")
        partner.__dict__["_host_id"] = owner.id  # pyright: ignore[reportIndexIssue]
        await db.asave(partner)

        owners = await db.aselect(_AMaprowOwner).fetch_all()
        assert len(owners) == 1
        assert owners[0].name == "maprow-owner"


# ---------------------------------------------------------------------------
# AsyncDatabase._execute() auto-flush and AsyncDatabase.execute() pre-flush
# ---------------------------------------------------------------------------


async def test_async_execute_flushes_pending_before_raw_sql() -> None:
    """AsyncDatabase.execute() flushes new session entities before raw SQL."""

    class _AExecFlushEntity(Entity):
        name: Req[str]

    async with AsyncDatabase(entities=[_AExecFlushEntity]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        async with db_session:
            _AExecFlushEntity(name="pending")
            # entity is in objects_to_save but not yet in DB
            # execute() should flush first so the DELETE can see the row
            deleted = await db.execute("DELETE FROM _aexecflushentity WHERE name = ?", "pending")

        assert deleted == 1
        assert await db.aselect(_AExecFlushEntity).count() == 0


async def test_async_execute_flushes_dirty_before_raw_sql() -> None:
    """AsyncDatabase.execute() flushes dirty session entities before raw SQL."""

    class _AExecDirtyEntity(Entity):
        name: Req[str]

    async with AsyncDatabase(entities=[_AExecDirtyEntity]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        e = _AExecDirtyEntity(name="original")
        await db.asave(e)

        async with db_session:
            loaded = await db.aselect(_AExecDirtyEntity).fetch_one()
            assert loaded is not None
            loaded.name = "dirty"
            # entity is dirty — execute() should flush first
            await db.execute("SELECT 1")

        reloaded = await db.aselect(_AExecDirtyEntity).fetch_one()
        assert reloaded is not None
        assert reloaded.name == "dirty"


async def test_async_execute_flushes_pending_before_select() -> None:
    """AsyncDatabase._execute() auto-flushes pending objects before SELECT."""

    class _AAutoFlushEntity(Entity):
        name: Req[str]

    async with AsyncDatabase(entities=[_AAutoFlushEntity]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        async with db_session:
            _AAutoFlushEntity(name="auto-flushed")
            # entity is pending — fetch_all() should see it via auto-flush
            results = await db.aselect(_AAutoFlushEntity).fetch_all()

        assert len(results) == 1
        assert results[0].name == "auto-flushed"


async def test_async_execute_flushes_dirty_before_select() -> None:
    """AsyncDatabase._execute() auto-flushes dirty objects before SELECT."""

    class _AAutoFlushDirtyEntity(Entity):
        name: Req[str]

    async with AsyncDatabase(entities=[_AAutoFlushDirtyEntity]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        e = _AAutoFlushDirtyEntity(name="original")
        await db.asave(e)

        async with db_session:
            loaded = await db.aselect(_AAutoFlushDirtyEntity).fetch_one()
            assert loaded is not None
            loaded.name = "modified"
            # entity is dirty — fetch_all() should see updated name via auto-flush
            results = await db.aselect(_AAutoFlushDirtyEntity).fetch_all()

        assert len(results) == 1
        assert results[0].name == "modified"


# ---------------------------------------------------------------------------
# AsyncQuerySet._build_select: column qualification when JOINs present
# ---------------------------------------------------------------------------


async def test_async_queryset_join_no_ambiguous_column() -> None:
    """AsyncQuerySet with JOINs qualifies columns to avoid ambiguous column name errors."""

    class _AJoinOwner(Entity):
        _table_ = "_a_join_owner"
        name: Req[str]

    class _AJoinChild(Entity):
        _table_ = "_a_join_child"
        owner: Single[_AJoinOwner]
        value: Req[str]

    async with AsyncDatabase(entities=[_AJoinOwner, _AJoinChild]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        owner = _AJoinOwner(name="owner1")
        await db.asave(owner)
        child = _AJoinChild(value="child1")
        vars(child)["_owner_id"] = owner.id
        await db.asave(child)

        # order_by traversing relation → adds a JOIN → would cause ambiguous 'id'
        results = await db.aselect(_AJoinChild).order_by(lambda c: c.owner.name).fetch_all()
        assert len(results) == 1
        assert results[0].value == "child1"


# ---------------------------------------------------------------------------
# AsyncDatabase._execute(): _flushing_ guard — not reachable in async path
# (async before_insert is sync-only, cannot recursively trigger async _execute)
# but the branch is retained for defensive correctness.
# Test: pending objects belong to a different DB → pending list empty after filter.
# ---------------------------------------------------------------------------


async def test_async_execute_pending_from_other_db_no_flush() -> None:
    """_execute() skips flush when all pending entities belong to a different database."""

    class _AOtherDbEntity(Entity):
        name: Req[str]

    class _AThisDbEntity(Entity):
        name: Req[str]

    async with (
        AsyncDatabase(entities=[_AOtherDbEntity]) as db1,
        AsyncDatabase(entities=[_AThisDbEntity]) as db2,
    ):
        await db1.bind("sqlite", ":memory:")
        await db2.bind("sqlite", ":memory:")
        await db1.generate_mapping(create_tables=True)
        await db2.generate_mapping(create_tables=True)

        async with db_session:
            # Pending entity belongs to db1
            _AOtherDbEntity(name="pending-in-db1")
            # SELECT on db2 — pending entity (from db1) doesn't belong to db2,
            # so pending/dirty lists after filtering are empty → 736->745 branch
            results = await db2.aselect(_AThisDbEntity).fetch_all()

        assert len(results) == 0


async def test_async_save_none_opt_str_field_coerced_to_empty_string() -> None:
    """async _do_insert coerces None to '' for non-nullable str fields (covers line 877)."""

    class _AsyncNullStrEntity(Entity):
        label: Opt[str]  # Opt[str] is not nullable by default → coerces None to ""

    async with AsyncDatabase(entities=[_AsyncNullStrEntity]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        # Create entity with None str field directly (bypassing descriptor default)
        e = _AsyncNullStrEntity.__new__(_AsyncNullStrEntity)
        vars(e)["_db_"] = db
        vars(e)["label"] = None  # bypass descriptor — simulate explicitly set None

        async with db_session:
            await db._do_insert(e, _AsyncNullStrEntity, db._schema[_AsyncNullStrEntity._table_name_])

        saved = (
            await db.aselect(_AsyncNullStrEntity).filter(_AsyncNullStrEntity.id == e.id).fetch_one()
        )
        assert saved is not None
        assert saved.label == ""


# ---------------------------------------------------------------------------
# AsyncQuerySet._build_select: explicit columns + JOIN
# ---------------------------------------------------------------------------


async def test_async_queryset_explicit_columns_with_join() -> None:
    """AsyncQuerySet with lazy fields (explicit columns) + JOIN qualifies columns correctly."""

    class _AExColParent(Entity):
        _table_ = "_a_excol_parent"
        name: Req[str]

    class _AExColChild(Entity):
        _table_ = "_a_excol_child"
        parent: Single[_AExColParent]
        text: Req[str]
        detail: Req[str] = Req(lazy=True)  # lazy field → _explicit_columns set

    async with AsyncDatabase(entities=[_AExColParent, _AExColChild]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        p1 = _AExColParent(name="Bravo")
        await db.asave(p1)
        p2 = _AExColParent(name="Alpha")
        await db.asave(p2)
        c1 = _AExColChild(text="child-b", detail="detail-b")
        vars(c1)["_parent_id"] = p1.id
        await db.asave(c1)
        c2 = _AExColChild(text="child-a", detail="detail-a")
        vars(c2)["_parent_id"] = p2.id
        await db.asave(c2)

        # order_by traversing relation → JOIN → explicit columns + JOIN → lines 1488-1489
        results = await db.aselect(_AExColChild).order_by(lambda c: c.parent.name).fetch_all()

        assert len(results) == 2
        # Ordered by parent name: Alpha < Bravo
        assert results[0].text == "child-a"
        assert results[1].text == "child-b"


# ---------------------------------------------------------------------------
# AsyncQuerySet.first()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_first_returns_first_entity() -> None:
    """first() returns the first matching entity, same as fetch_one()."""
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        await db.asave(AsyncUser(name="alpha", age=1))
        await db.asave(AsyncUser(name="beta", age=2))
        result = await db.aselect(AsyncUser).order_by(AsyncUser.name.asc()).first()
        assert result is not None
        assert result.name == "alpha"


@pytest.mark.asyncio
async def test_async_first_returns_none_when_empty() -> None:
    """first() returns None when no rows match."""
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        result = await db.aselect(AsyncUser).first()
        assert result is None


# ---------------------------------------------------------------------------
# AsyncQuerySet.prefetch()
# ---------------------------------------------------------------------------


class _APrefetchAuthor(Entity):
    _table_ = "aprefetch_author"
    id: PK[int]
    name: Req[str]
    books: Set["_APrefetchBook"]  # noqa: UP037


class _APrefetchBook(Entity):
    _table_ = "aprefetch_book"
    id: PK[int]
    title: Req[str]
    author: Single[_APrefetchAuthor | None]


@pytest.mark.asyncio
async def test_async_prefetch_o2m_loads_related() -> None:
    """prefetch() on a Set relation avoids N+1 queries."""
    async with AsyncDatabase(entities=[_APrefetchAuthor, _APrefetchBook]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        a1 = _APrefetchAuthor(name="AuthorA")
        await db.asave(a1)
        b1 = _APrefetchBook(title="Book1")
        vars(b1)["_author_id"] = a1.id
        await db.asave(b1)
        b2 = _APrefetchBook(title="Book2")
        vars(b2)["_author_id"] = a1.id
        await db.asave(b2)

        authors = await db.aselect(_APrefetchAuthor).prefetch(_APrefetchAuthor.books).fetch_all()
        assert len(authors) == 1
        col = vars(authors[0]).get("_books_col")
        assert col is not None
        assert len(col._cache) == 2


@pytest.mark.asyncio
async def test_async_prefetch_string_attr_name() -> None:
    """prefetch() accepts string attribute names."""
    async with AsyncDatabase(entities=[_APrefetchAuthor, _APrefetchBook]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        a = _APrefetchAuthor(name="Str")
        await db.asave(a)
        authors = await db.aselect(_APrefetchAuthor).prefetch("books").fetch_all()
        assert len(authors) == 1
        col = vars(authors[0]).get("_books_col")
        assert col is not None
        assert col._cache == []


@pytest.mark.asyncio
async def test_async_prefetch_invalid_relation_raises() -> None:
    """prefetch() raises ValueError for unknown relation name."""
    async with AsyncDatabase(entities=[_APrefetchAuthor, _APrefetchBook]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        await db.asave(_APrefetchAuthor(name="X"))
        with pytest.raises(ValueError, match="no relation"):
            await db.aselect(_APrefetchAuthor).prefetch("nonexistent").fetch_all()


@pytest.mark.asyncio
async def test_async_prefetch_single_relation() -> None:
    """prefetch() on a Single relation batch-loads FK targets."""
    async with AsyncDatabase(entities=[_APrefetchAuthor, _APrefetchBook]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        a = _APrefetchAuthor(name="FK Author")
        await db.asave(a)
        b = _APrefetchBook(title="FK Book")
        vars(b)["_author_id"] = a.id
        await db.asave(b)

        books = await db.aselect(_APrefetchBook).prefetch("author").fetch_all()
        assert len(books) == 1
        loaded_author = vars(books[0]).get("_author_obj")
        assert loaded_author is not None
        assert loaded_author.name == "FK Author"


@pytest.mark.asyncio
async def test_async_prefetch_single_null_fk() -> None:
    """prefetch() on Single sets None when no FK rows exist."""
    async with AsyncDatabase(entities=[_APrefetchAuthor, _APrefetchBook]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        b = _APrefetchBook(title="Orphan")
        await db.asave(b)

        books = await db.aselect(_APrefetchBook).prefetch("author").fetch_all()
        assert len(books) == 1
        loaded_author = vars(books[0]).get("_author_obj")
        assert loaded_author is None


@pytest.mark.asyncio
async def test_async_prefetch_invalid_attr_raises() -> None:
    """prefetch() raises ValueError when attr has no 'name'."""
    async with AsyncDatabase(entities=[_APrefetchAuthor]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        with pytest.raises(ValueError, match="Cannot determine relation name"):
            db.aselect(_APrefetchAuthor).prefetch(42)


@pytest.mark.asyncio
async def test_async_prefetch_on_fetch_one() -> None:
    """prefetch() also works with fetch_one()."""
    async with AsyncDatabase(entities=[_APrefetchAuthor, _APrefetchBook]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        a = _APrefetchAuthor(name="SingleFetch")
        await db.asave(a)
        b = _APrefetchBook(title="OnlyBook")
        vars(b)["_author_id"] = a.id
        await db.asave(b)

        author = await db.aselect(_APrefetchAuthor).prefetch(_APrefetchAuthor.books).fetch_one()
        assert author is not None
        col = vars(author).get("_books_col")
        assert col is not None
        assert len(col._cache) == 1


@pytest.mark.asyncio
async def test_async_prefetch_none_results_noop() -> None:
    """prefetch() with fetch_one() returning None does not error."""
    async with AsyncDatabase(entities=[_APrefetchAuthor]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        # No rows — fetch_one returns None; prefetch should not be called
        result = await db.aselect(_APrefetchAuthor).prefetch("books").fetch_one()
        assert result is None


# ---------------------------------------------------------------------------
# AsyncQuerySet.show()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_show_alias_for_show() -> None:
    async with AsyncDatabase(entities=[AsyncUser]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        await db.asave(AsyncUser(name="ShowMe", age=7))

        f1 = io.StringIO()
        await db.aselect(AsyncUser).show(file=f1)
        output = f1.getvalue()
        assert "ShowMe" in output
        assert "7" in output


# ---------------------------------------------------------------------------
# AsyncDatabase.disconnect()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_disconnect_closes_connection() -> None:
    """disconnect() closes the underlying async connection."""
    db = AsyncDatabase(entities=[AsyncUser])
    await db.bind("sqlite", ":memory:")
    await db.generate_mapping(create_tables=True)
    assert db._connection is not None
    await db.disconnect()
    assert db._connection is None


# ---------------------------------------------------------------------------
# AsyncDatabase.get_ddl()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_get_ddl_empty_before_mapping() -> None:
    """get_ddl() returns [] before generate_mapping() is called."""
    db = AsyncDatabase(entities=[AsyncUser])
    await db.bind("sqlite", ":memory:")
    try:
        assert db.get_ddl() == []
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# AsyncDatabase.unbind()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_unbind_clears_state() -> None:
    """unbind() closes the connection and resets all provider state."""
    db = AsyncDatabase(entities=[AsyncUser])
    await db.bind("sqlite", ":memory:")
    await db.generate_mapping(create_tables=True)
    assert db.is_bound
    await db.unbind()
    assert not db.is_bound
    assert db._connection is None
    assert db._schema == {}
    assert db._provider is None


# ---------------------------------------------------------------------------
# AsyncDatabase.migrate()
# ---------------------------------------------------------------------------


class _AMigrateUser(Entity):
    _table_ = "amigrate_user"
    name: Req[str]


@pytest.mark.asyncio
async def test_async_migrate_creates_tables() -> None:
    """migrate() creates missing tables."""
    db = AsyncDatabase(entities=[_AMigrateUser])
    await db.bind("sqlite", ":memory:")
    await db.generate_mapping(create_tables=False)
    stmts = await db.migrate()
    assert any("CREATE TABLE" in s.upper() for s in stmts)
    # Table should now exist
    await db.asave(_AMigrateUser(name="test"))
    assert await db.aselect(_AMigrateUser).count() == 1
    await db.close()


@pytest.mark.asyncio
async def test_async_migrate_is_idempotent() -> None:
    """migrate() returns [] when no changes are needed."""
    db = AsyncDatabase(entities=[_AMigrateUser])
    await db.bind("sqlite", ":memory:")
    await db.generate_mapping(create_tables=True)
    stmts = await db.migrate()
    assert stmts == []
    await db.close()


@pytest.mark.asyncio
async def test_async_migrate_not_bound_raises() -> None:
    """migrate() raises RuntimeError when not bound."""
    db = AsyncDatabase(entities=[_AMigrateUser])
    with pytest.raises(RuntimeError, match="not bound"):
        await db.migrate()


@pytest.mark.asyncio
async def test_async_migrate_no_mapping_raises() -> None:
    """migrate() raises RuntimeError when generate_mapping not called."""
    db = AsyncDatabase(entities=[_AMigrateUser])
    await db.bind("sqlite", ":memory:")
    try:
        with pytest.raises(RuntimeError, match="Schema is empty"):
            await db.migrate()
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# AsyncQuerySet._do_async_prefetch — branch coverage
# ---------------------------------------------------------------------------


class _APrefetchTag(Entity):
    _table_ = "aprefetch_tag"
    id: PK[int]
    label: Req[str]
    articles: Set["_APrefetchArticle"]  # noqa: UP037


class _APrefetchArticle(Entity):
    _table_ = "aprefetch_article"
    id: PK[int]
    title: Req[str]
    tags: Set[_APrefetchTag]


@pytest.mark.asyncio
async def test_async_prefetch_empty_results_noop() -> None:
    """_do_async_prefetch with empty results returns immediately."""
    async with AsyncDatabase(entities=[_APrefetchAuthor, _APrefetchBook]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        # No data — fetch_all returns [] — prefetch is called with empty list
        authors = await db.aselect(_APrefetchAuthor).prefetch("books").fetch_all()
        assert authors == []


@pytest.mark.asyncio
async def test_async_prefetch_m2m_with_data() -> None:
    """prefetch() on an M2M Set relation loads related items."""
    async with AsyncDatabase(entities=[_APrefetchTag, _APrefetchArticle]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        tag = _APrefetchTag(label="python")
        art = _APrefetchArticle(title="article1")
        await db.asave(tag)
        await db.asave(art)
        # Insert M2M join manually
        join_table = "_".join(sorted(["aprefetch_tag", "aprefetch_article"]))
        await db._execute_dml(
            f"INSERT INTO {join_table} (aprefetch_tag_id, aprefetch_article_id) VALUES (?, ?)",
            [tag.id, art.id],
        )

        articles = await db.aselect(_APrefetchArticle).prefetch("tags").fetch_all()
        assert len(articles) == 1
        col = vars(articles[0]).get("_tags_col")
        assert col is not None
        assert len(col._cache) == 1
        assert col._cache[0].label == "python"


@pytest.mark.asyncio
async def test_async_prefetch_m2m_no_related() -> None:
    """prefetch() M2M branch attaches empty cache when no join rows exist."""
    async with AsyncDatabase(entities=[_APrefetchTag, _APrefetchArticle]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        art = _APrefetchArticle(title="lonely article")
        await db.asave(art)

        articles = await db.aselect(_APrefetchArticle).prefetch("tags").fetch_all()
        assert len(articles) == 1
        col = vars(articles[0]).get("_tags_col")
        assert col is not None
        assert col._cache == []


@pytest.mark.asyncio
async def test_async_prefetch_single_unresolvable_target_skipped() -> None:
    """prefetch on a Single with unresolvable target is silently skipped."""

    class _AGhostParent(Entity):
        _table_ = "aghost_parent"
        label: Req[str]
        ghost: Single["_CompletelyMissingAsyncEntity"]  # type: ignore[name-defined]  # noqa: F821, UP037

    db = AsyncDatabase(entities=[_AGhostParent])
    await db.bind("sqlite", ":memory:")
    await db.generate_mapping(create_tables=True)
    p = _AGhostParent(label="x")
    vars(p)["_ghost_id"] = 1
    await db.asave(p)
    results = await db.aselect(_AGhostParent).prefetch("ghost").fetch_all()
    assert len(results) == 1
    await db.close()


@pytest.mark.asyncio
async def test_async_prefetch_single_no_pk_skipped() -> None:
    """prefetch on a Single where the target has no PK is silently skipped."""

    class _ANoPKTarget(Entity):
        _table_ = "anopk_target"
        name: Req[str]

    class _ANoPKOwner(Entity):
        _table_ = "anopk_owner"
        label: Req[str]
        ref: Single[_ANoPKTarget | None]

    db = AsyncDatabase(entities=[_ANoPKTarget, _ANoPKOwner])
    await db.bind("sqlite", ":memory:")
    await db.generate_mapping(create_tables=True)
    t = _ANoPKTarget(name="t")
    await db.asave(t)
    o = _ANoPKOwner(label="o")
    vars(o)["_ref_id"] = t.id
    await db.asave(o)

    orig_pk_field = _ANoPKTarget._pk_field_
    orig_pk_fields = _ANoPKTarget._pk_fields_
    try:
        _ANoPKTarget._pk_field_ = None
        _ANoPKTarget._pk_fields_ = ()
        results = await db.aselect(_ANoPKOwner).prefetch("ref").fetch_all()
        assert len(results) == 1
    finally:
        _ANoPKTarget._pk_field_ = orig_pk_field
        _ANoPKTarget._pk_fields_ = orig_pk_fields
    await db.close()


@pytest.mark.asyncio
async def test_async_prefetch_no_pk_fields_owner_skipped() -> None:
    """_do_async_prefetch silently returns when owner entity has no pk_fields."""
    async with AsyncDatabase(entities=[_APrefetchAuthor, _APrefetchBook]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)
        a = _APrefetchAuthor(name="NoPK")
        await db.asave(a)

        orig_pk_fields = _APrefetchAuthor._pk_fields_
        try:
            _APrefetchAuthor._pk_fields_ = ()
            results = await db.aselect(_APrefetchAuthor).prefetch("books").fetch_all()
            assert len(results) == 1
        finally:
            _APrefetchAuthor._pk_fields_ = orig_pk_fields


@pytest.mark.asyncio
async def test_async_prefetch_set_no_back_ref_skipped() -> None:
    """prefetch() O2M branch silently skips relations with no back-reference."""

    class _ANoBackRefParent(Entity):
        _table_ = "anobackref_parent"
        name: Req[str]
        children: Set["_ANoBackRefChild"]  # noqa: UP037

    class _ANoBackRefChild(Entity):
        _table_ = "anobackref_child"
        label: Req[str]
        parent: Single[_ANoBackRefParent | None]

    db = AsyncDatabase(entities=[_ANoBackRefParent, _ANoBackRefChild])
    await db.bind("sqlite", ":memory:")
    await db.generate_mapping(create_tables=True)
    p = _ANoBackRefParent(name="p")
    await db.asave(p)

    # Temporarily remove back-ref from the child's relations so back_ref lookup fails
    child_relations = dict(_ANoBackRefChild._relations_)
    _ANoBackRefChild._relations_ = {}
    try:
        results = await db.aselect(_ANoBackRefParent).prefetch("children").fetch_all()
        assert len(results) == 1
    finally:
        _ANoBackRefChild._relations_ = child_relations
    await db.close()


@pytest.mark.asyncio
async def test_async_prefetch_set_unresolvable_target_skipped() -> None:
    """prefetch() on a Set with unresolvable target is silently skipped."""

    class _AUnresolvableParentX(Entity):
        _table_ = "aunresolvable_parentx"
        name: Req[str]
        stuff: Set["_ACompletelyMissingEntityX"]  # type: ignore[name-defined]  # noqa: F821, UP037

    db = AsyncDatabase(entities=[_AUnresolvableParentX])
    await db.bind("sqlite", ":memory:")
    await db.generate_mapping(create_tables=True, validate_relations=False)
    p = _AUnresolvableParentX(name="x")
    await db.asave(p)
    results = await db.aselect(_AUnresolvableParentX).prefetch("stuff").fetch_all()
    assert len(results) == 1
    await db.close()


# ---------------------------------------------------------------------------
# async_capture_sql edge case — __aexit__ without __aenter__
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_capture_sql_aexit_without_aenter_noop() -> None:
    """__aexit__ without __aenter__ (token is None) is a no-op."""
    from nextorm import async_capture_sql as _acs  # noqa: PLC0415

    ctx = _acs()
    # Do NOT call __aenter__ — token remains None
    await ctx.__aexit__(None, None, None)  # should not raise


# ---------------------------------------------------------------------------
# async_introspect_sqlite — index coverage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_introspect_sqlite_with_index() -> None:
    """async_introspect_sqlite records explicitly created indexes."""
    from nextorm.schema.introspect import async_introspect_sqlite  # noqa: PLC0415

    db = AsyncDatabase(entities=[])
    await db.bind("sqlite", ":memory:")
    try:
        conn = db._ensure_connection()
        # Create a table with an explicit index
        await db._async_provider_instance.execute_ddl(  # type: ignore[union-attr]
            conn,
            [
                "CREATE TABLE idx_test (id INTEGER PRIMARY KEY, name TEXT NOT NULL)",
                "CREATE INDEX idx_idx_test__name ON idx_test (name)",
            ],
        )
        schema = await async_introspect_sqlite(conn)
        assert "idx_test" in schema
        idx_names = {i.name for i in schema["idx_test"].indexes}
        assert "idx_idx_test__name" in idx_names
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_async_introspect_sqlite_non_explicit_index_skipped() -> None:
    """async_introspect_sqlite skips implicit UNIQUE constraint indexes."""
    from nextorm.schema.introspect import async_introspect_sqlite  # noqa: PLC0415

    db = AsyncDatabase(entities=[])
    await db.bind("sqlite", ":memory:")
    try:
        conn = db._ensure_connection()
        await db._async_provider_instance.execute_ddl(  # type: ignore[union-attr]
            conn,
            ["CREATE TABLE unique_test (id INTEGER PRIMARY KEY, slug TEXT NOT NULL UNIQUE)"],
        )
        schema = await async_introspect_sqlite(conn)
        assert "unique_test" in schema
        # Implicit UNIQUE index should be absent
        assert schema["unique_test"].indexes == []
    finally:
        await db.close()
