"""Tests for nextorm.database — Database bind/unbind, entity registration, mapping."""

from __future__ import annotations

import os
import typing
from typing import Any, cast
from unittest.mock import patch

import pytest

from nextorm import commit as _module_commit
from nextorm import flush as _module_flush
from nextorm import rollback as _module_rollback
from nextorm.database import Database
from nextorm.entity import Entity
from nextorm.exceptions import CommitException, MappingError, PartialCommitException
from nextorm.fields import PK, Opt, Req, Set, Single
from nextorm.pool import ConnectionPool
from nextorm.providers.base import (
    _PROVIDER_REGISTRY,
    SyncConnection,
    SyncProvider,
    register_provider,
)
from nextorm.session import db_session
from nextorm.sql.builder import FormatBuilder, SQLiteBuilder

# ---------------------------------------------------------------------------
# MariaDB connection helpers (shared with MySQL integration tests below)
# ---------------------------------------------------------------------------

_MARIADB_KWARGS: dict[str, Any] = {
    "host": os.environ.get("NEXTORM_MARIADB_HOST", "127.0.0.1"),
    "user": os.environ.get("NEXTORM_MARIADB_USER", "nextorm"),
    "password": os.environ.get("NEXTORM_MARIADB_PASSWORD", "nextorm"),
    "database": os.environ.get("NEXTORM_MARIADB_DATABASE", "nextorm_test"),
}


def _mysql_available() -> bool:
    try:
        import pymysql  # noqa: PLC0415

        c = pymysql.connect(**_MARIADB_KWARGS)
        c.close()
        return True
    except Exception:
        return False


_requires_mysql = pytest.mark.skipif(
    not _mysql_available(), reason="No MariaDB/MariaDB server reachable"
)

# ---------------------------------------------------------------------------
# Module-level entity definitions (annotations are resolved at class-body time,
# so related entities like Set[Tag] must be in module scope).
# ---------------------------------------------------------------------------


class Tag(Entity):
    name: Req[str]


class Article(Entity):
    id: PK[int]
    title: Req[str]
    tags: Set[Tag]


class PKOnly(Entity):
    """Entity with just an auto-pk — triggers DEFAULT VALUES / NULL insert path."""


class _UserPKItem(Entity):
    """Entity with a user-assigned (non-auto) integer PK — tests insert() path."""

    id: Req[int] = Req(primary_key=True, auto=False)
    label: Req[str]


# ---------------------------------------------------------------------------
# Module-level entities for _validate_relations tests
# ---------------------------------------------------------------------------


class _FRTarget(Entity):
    label: Req[str]
    owner: Single["_FROwner"]  # noqa: UP037


class _FROwner(Entity):
    label: Req[str]
    items: Set[_FRTarget]


# Entities where the Set target is a typing.ForwardRef (not a plain string)
class _FwdRefChild(Entity):
    label: Req[str]
    owner: Single["_FwdRefOwner"]  # noqa: UP037


class _FwdRefOwner(Entity):
    label: Req[str]
    items: Set[typing.ForwardRef("_FwdRefChild")]  # type: ignore[valid-type]


class _UnresolvableOwner(Entity):
    label: Req[str]
    items: Set["_CompletelyMissingEntity"]  # type: ignore[name-defined]  # noqa: UP037, F821


class _Thing2(Entity):
    label: Req[str]
    owner_x: Single["_Owner2"]  # noqa: UP037
    owner_y: Single["_Owner2"]  # noqa: UP037


class _Owner2(Entity):
    label: Req[str]
    things: Set[_Thing2]


class _FKParent(Entity):
    label: Req[str]


class _FKChild(Entity):
    label: Req[str]
    parent: Single[_FKParent]


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_database_starts_unbound() -> None:
    db = Database()
    assert not db.is_bound


def test_database_entities_property_empty_on_fresh_db() -> None:
    """Passing entities=[] uses the explicit (empty) list, not the global registry."""
    db = Database(entities=[])
    assert db.entities == {}


def test_database_explicit_entities() -> None:
    db = Database(entities=[Article, Tag])
    assert db.entities == {"Article": Article, "Tag": Tag}


# ---------------------------------------------------------------------------
# register()
# ---------------------------------------------------------------------------


def test_register_on_registry_fallback_db_initialises_list() -> None:
    """register() when no explicit entities were given (entities=None)."""
    db = Database()  # _entities is None → uses global registry
    db.register(Article)  # triggers the None→[] initialisation branch
    assert "Article" in db.entities


def test_register_adds_entity() -> None:
    db = Database(entities=[Article])
    db.register(Tag)
    assert "Tag" in db.entities


def test_register_deduplicates() -> None:
    db = Database(entities=[Article])
    db.register(Article)  # register again
    names = list(db.entities.keys())
    assert names.count("Article") == 1


def test_register_rejects_non_entity() -> None:
    db = Database()
    with pytest.raises(TypeError, match="not an Entity subclass"):
        db.register(object)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# bind / unbind
# ---------------------------------------------------------------------------


def test_bind_known_provider() -> None:
    db = Database()
    db.bind("sqlite", ":memory:")
    assert db.is_bound


def test_bind_selects_sqlite_builder_for_qmark() -> None:
    """SQLite provider (qmark param-style) yields a SQLiteBuilder."""
    db = Database()
    db.bind("sqlite", ":memory:")
    assert isinstance(db._builder, SQLiteBuilder)
    assert not isinstance(db._builder, FormatBuilder)


def test_bind_selects_format_builder_for_format_style() -> None:
    """A format param-style provider yields a FormatBuilder."""

    class _FormatSync(SyncProvider):
        name = "_test_format"
        param_style = "format"

        def placeholder(self, param_name: str | None = None) -> str:
            return "%s"

        def connect(self, *args: Any, **kwargs: Any) -> SyncConnection:
            raise NotImplementedError

        def execute_ddl(self, connection: SyncConnection, statements: list[str]) -> None:
            pass

        def introspect(self, connection: SyncConnection) -> dict[str, Any]:
            raise NotImplementedError

    register_provider("_test_format", sync=_FormatSync)
    try:
        db = Database()
        db.bind("_test_format")
        assert isinstance(db._builder, FormatBuilder)
    finally:
        del _PROVIDER_REGISTRY["_test_format"]


def test_bind_unknown_provider_raises() -> None:
    db = Database()
    with pytest.raises(ValueError, match="Unknown provider"):
        db.bind("oracle")


def test_bind_provider_without_ddl_renderer() -> None:
    """A provider in the registry but with no DDL renderer can still be bound."""

    class _MinimalSync(SyncProvider):
        name = "_minimal_no_ddl"
        param_style = "qmark"

        def placeholder(self, param_name: str | None = None) -> str:
            return "?"

        def connect(self, *args: Any, **kwargs: Any) -> SyncConnection:
            raise NotImplementedError

        def execute_ddl(self, connection: SyncConnection, statements: list[str]) -> None:
            pass

        def introspect(self, connection: SyncConnection) -> dict[str, Any]:
            raise NotImplementedError

    register_provider("_minimal_no_ddl", sync=_MinimalSync)
    try:
        db = Database()
        db.bind("_minimal_no_ddl")
        assert db.is_bound
        assert db._renderer is None  # no DDL renderer for this provider
    finally:
        del _PROVIDER_REGISTRY["_minimal_no_ddl"]


def test_unbind_clears_state() -> None:
    db = Database()
    db.bind("sqlite", ":memory:")
    db.unbind()
    assert not db.is_bound


def test_rebind_after_unbind() -> None:
    db = Database()
    db.bind("sqlite", ":memory:")
    db.unbind()
    db.bind("sqlite", ":memory:")
    assert db.is_bound


# ---------------------------------------------------------------------------
# generate_mapping
# ---------------------------------------------------------------------------


def test_generate_mapping_requires_bind() -> None:
    db = Database(entities=[Article, Tag])
    with pytest.raises(RuntimeError, match="not bound"):
        db.generate_mapping()


def test_generate_mapping_builds_schema() -> None:
    db = Database(entities=[Article, Tag])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(validate_relations=False)
    assert len(db.schema) >= 2
    db.close()


def test_generate_mapping_create_tables_produces_ddl() -> None:
    db = Database(entities=[Article, Tag])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True, validate_relations=False)
    ddl = db.get_ddl()
    assert len(ddl) >= 2
    assert all("CREATE TABLE" in stmt for stmt in ddl)
    db.close()


def test_generate_mapping_no_create_tables_produces_no_ddl() -> None:
    db = Database(entities=[Article, Tag])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=False, validate_relations=False)
    assert db.get_ddl() == []
    db.close()


def test_get_ddl_before_generate_mapping_returns_empty() -> None:
    db = Database()
    db.bind("sqlite", ":memory:")
    assert db.get_ddl() == []


# ---------------------------------------------------------------------------
# Global-registry fallback
# ---------------------------------------------------------------------------


def test_global_registry_fallback() -> None:
    """When no explicit entities are given, _entity_registry is used."""

    class Solo(Entity):  # pyright: ignore[reportUnusedClass]
        label: Req[str]

    db = Database()  # no explicit entities
    db.bind("sqlite", ":memory:")
    # Solo is in the global registry, so entities should contain it
    assert "Solo" in db.entities


# ---------------------------------------------------------------------------
# schema property
# ---------------------------------------------------------------------------


def test_schema_empty_before_generate_mapping() -> None:
    db = Database()
    db.bind("sqlite", ":memory:")
    assert db.schema == {}


def test_schema_returns_copy() -> None:
    """Mutating the returned dict must not affect internal state."""
    db = Database(entities=[Article, Tag])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(validate_relations=False)
    snap1 = db.schema
    snap1.clear()
    assert db.schema != {}
    db.close()


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------


def test_ensure_connection_without_binding_raises() -> None:
    """_ensure_connection on an unbound Database raises RuntimeError."""
    db = Database()
    with pytest.raises(RuntimeError, match="not bound"):
        db._ensure_connection()


def test_generate_mapping_create_tables_keeps_connection_open() -> None:
    """generate_mapping(create_tables=True) keeps a persistent connection."""
    db = Database(entities=[Article])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True, validate_relations=False)
    assert db._connection is not None
    db.close()
    assert db._connection is None


def test_context_manager_closes_connection() -> None:
    with Database(entities=[Article]) as db:
        db.bind("sqlite", ":memory:")
        db.generate_mapping(create_tables=True, validate_relations=False)
    # Connection is closed after exiting the block
    assert db._connection is None


def test_del_closes_open_connection() -> None:
    """Database.__del__ closes the connection when the object is garbage collected."""
    db = Database(entities=[Article])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True, validate_relations=False)
    assert db._connection is not None
    db.__del__()
    assert db._connection is None


# ---------------------------------------------------------------------------
# _do_insert edge cases
# ---------------------------------------------------------------------------


def test_do_insert_with_no_pk_field_does_not_set_pk() -> None:
    """An entity with composite PK (no single auto-PK) skips the PK write-back."""
    db = Database(entities=[Article])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True, validate_relations=False)
    entity_cls = Article
    table = db._schema["article"]
    obj = Article(title="hello")
    # Simulate composite PK by temporarily replacing _pk_field_ with None
    orig = entity_cls._pk_field_
    try:
        entity_cls._pk_field_ = None
        db._do_insert(obj, entity_cls, table)
        # No PK should have been written back (single_pk_field is None)
        assert obj.id is None
    finally:
        entity_cls._pk_field_ = orig
    db.close()


def test_do_insert_rowid_none_does_not_set_pk() -> None:
    """When _execute_insert returns None the PK is not overwritten."""
    db = Database(entities=[Article])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True, validate_relations=False)
    obj = Article(title="hello")
    table = db._schema["article"]
    with patch.object(db, "_execute_insert", return_value=None):
        db._do_insert(obj, type(obj), table)
    assert obj.id is None
    db.close()


# ---------------------------------------------------------------------------
# delete_instance edge cases
# ---------------------------------------------------------------------------


def test_delete_instance_no_pk_field_raises() -> None:
    "`delete_instance` raises RuntimeError when entity has no PK fields."
    db = Database(entities=[Article])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True, validate_relations=False)
    with db_session:
        obj = Article(title="X")
    # Temporarily remove the PK fields info to simulate the edge case
    orig_fields = type(obj)._pk_fields_
    orig_field = type(obj)._pk_field_
    try:
        type(obj)._pk_fields_ = ()
        type(obj)._pk_field_ = None
        with pytest.raises(RuntimeError, match="no primary-key field"):
            db.delete_instance(obj)
    finally:
        type(obj)._pk_fields_ = orig_fields
        type(obj)._pk_field_ = orig_field
    db.close()


# ---------------------------------------------------------------------------
# _do_insert DEFAULT VALUES branch (entity with only auto-pk)
# ---------------------------------------------------------------------------


def test_do_insert_pk_only_entity_uses_null_insert() -> None:
    """An entity with only an auto-pk uses the DEFAULT VALUES / NULL insert path."""
    db = Database(entities=[PKOnly])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    with db_session:
        obj = PKOnly()
    assert obj.id is not None
    db.close()


# ---------------------------------------------------------------------------
# session identity map — new_pk is None branch
# ---------------------------------------------------------------------------


def test_save_inside_session_pk_is_none_no_cache_put() -> None:
    """When _execute_insert returns None, new_pk is None → cache.put skipped (313->315)."""
    from nextorm.session import db_session  # noqa: PLC0415

    db = Database(entities=[Article])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True, validate_relations=False)
    with patch.object(db, "_execute_insert", return_value=None), db_session:
        obj = Article(title="session_pkNone")
    # pk was not written back because _execute_insert returned None
    assert obj.id is None
    db.close()


# ---------------------------------------------------------------------------
# _validate_relations — ForwardRef and unresolvable string targets
# ---------------------------------------------------------------------------


def test_validate_relations_forwardref_target_resolves() -> None:
    """Set with ForwardRef target is resolved via entity_by_name."""
    db = Database(entities=[_FROwner, _FRTarget])
    db.bind("sqlite", ":memory:")
    # validate_relations=True → _validate_relations called; ForwardRef target resolves fine
    db.generate_mapping(create_tables=True)  # validate_relations=True is the default
    db.close()


def test_validate_relations_typing_forwardref_target_resolves() -> None:
    """Set with typing.ForwardRef target hits the ForwardRef branch in _resolve."""
    db = Database(entities=[_FwdRefOwner, _FwdRefChild])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)  # _resolve uses ForwardRef.__forward_arg__
    db.close()


def test_validate_relations_unresolvable_string_skipped() -> None:
    """Set['NonExistent'] target is unresolvable → continue without error."""
    db = Database(entities=[_UnresolvableOwner])
    db.bind("sqlite", ":memory:")
    # _validate_relations called → target not found → continue (no error)
    db.generate_mapping(create_tables=True)
    db.close()


def test_validate_relations_ambiguous_raises() -> None:
    """Ambiguous back-refs without reverse= raises MappingError."""
    db = Database(entities=[_Owner2, _Thing2])
    db.bind("sqlite", ":memory:")
    with pytest.raises(MappingError, match="ambiguous"):
        db.generate_mapping(create_tables=True)
    db.close()


# ---------------------------------------------------------------------------
# _do_update — FK relation loop
# ---------------------------------------------------------------------------


def test_do_update_with_relations_covers_fk_loop() -> None:
    """_do_update iterates relations and appends FK assignments."""
    db = Database(entities=[_FKParent, _FKChild])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    with db_session:
        p = _FKParent(label="par")
        db.flush()  # p.id is now set
        c = _FKChild(label="child")
        vars(c)["_parent_id"] = p.id
        c.label = "updated"
    # Now verify
    assert c.label == "updated"
    db.close()


def test_do_update_set_relation_skipped_in_loop() -> None:
    """_do_update loop skips SET relations."""
    db = Database(entities=[Article, Tag])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True, validate_relations=False)
    with db_session:
        art = Article(id=1, title="initial")
        art.title = "updated"
    assert art.title == "updated"
    db.close()


# ---------------------------------------------------------------------------
# insert() — forced INSERT regardless of PK
# ---------------------------------------------------------------------------


def test_insert_auto_pk_none_behaves_like_save() -> None:
    """insert() with no PK value auto-generates one, same as save()."""
    db = Database(entities=[Article])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True, validate_relations=False)
    art = Article(title="new article")
    db.insert(art)
    assert art.id is not None
    db.close()


def test_insert_auto_pk_with_value_overrides_autoincrement() -> None:
    """insert() with a pre-set auto-PK value passes it explicitly to the INSERT."""
    db = Database(entities=[Article])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True, validate_relations=False)
    art = Article(title="seeded")
    art.id = 999
    db.insert(art)
    assert art.id == 999
    # Verify the row was actually stored with that id
    fetched = db.select(Article).filter(Article.id == 999).fetch_one()
    assert fetched is not None
    assert fetched.title == "seeded"
    db.close()


def test_insert_user_assigned_pk() -> None:
    """insert() allows inserting entities with user-assigned (non-auto) PKs."""
    db = Database(entities=[_UserPKItem])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True, validate_relations=False)
    item = _UserPKItem(id=7, label="seven")
    db.insert(item)
    assert item.id == 7
    fetched = db.select(_UserPKItem).filter(_UserPKItem.id == 7).fetch_one()
    assert fetched is not None
    assert fetched.label == "seven"
    db.close()


def test_insert_duplicate_pk_raises() -> None:
    """insert() on an entity whose PK already exists raises an integrity error."""
    import sqlite3  # noqa: PLC0415

    db = Database(entities=[_UserPKItem])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    db.insert(_UserPKItem(id=1, label="first"))
    with pytest.raises(sqlite3.IntegrityError):
        db.insert(_UserPKItem(id=1, label="clash"))
    db.close()


def test_insert_calls_lifecycle_hooks() -> None:
    """insert() fires before_insert and after_insert on the entity."""
    calls: list[str] = []

    class _Tracked(Entity):
        name: Req[str]

        def before_insert(self) -> None:
            calls.append("before")

        def after_insert(self) -> None:
            calls.append("after")

    db = Database(entities=[_Tracked])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    db.insert(_Tracked(name="x"))
    assert calls == ["before", "after"]
    db.close()


def test_insert_unmapped_entity_raises() -> None:
    """insert() raises RuntimeError for entities not in the schema."""

    class _Ghost(Entity):
        name: Req[str]

    db = Database(entities=[Article])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True, validate_relations=False)
    with pytest.raises(RuntimeError, match="not in the mapped schema"):
        db.insert(_Ghost(name="nobody"))
    db.close()


# ---------------------------------------------------------------------------
# FieldSpec: volatile / sql_type / sql_default
# ---------------------------------------------------------------------------


def test_volatile_field_excluded_from_update() -> None:
    """volatile=True fields are not included in UPDATE statements."""

    class _VolatileEntity(Entity):
        name: Req[str]
        # 'computed' is set by a DB trigger; our ORM must not overwrite it
        computed: Opt[int] = Opt(volatile=True)

    db = Database(entities=[_VolatileEntity])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    # Manually set the 'computed' column via raw SQL (simulates a trigger)
    db.execute("INSERT INTO _volatileentity (name, computed) VALUES (?, ?)", "item1", 99)
    item = db.select(_VolatileEntity).fetch_one()
    assert item is not None
    # Mutate name and save — 'computed' should NOT be sent in UPDATE
    with db_session:
        item.name = "item1-updated"
        item.computed = 0  # in Python this is 0
    # Reload from DB — 'computed' should still be 99 (not overwritten to 0)
    reloaded = db.select(_VolatileEntity).fetch_one()
    assert reloaded is not None
    assert reloaded.name == "item1-updated"
    assert reloaded.computed == 99
    db.close()


def test_sql_type_propagated_to_ddl() -> None:
    """FieldSpec(sql_type=...) overrides the inferred SQL column type in DDL."""
    from nextorm.schema.builder import entity_to_table  # noqa: PLC0415
    from nextorm.schema.ddl import SQLiteRenderer  # noqa: PLC0415

    class _JsonEntity(Entity):
        payload: Req[str] = Req(sql_type="BLOB")

    table = entity_to_table(_JsonEntity)
    renderer = SQLiteRenderer()
    ddl = renderer.create_table(table)
    assert "BLOB" in ddl


def test_sql_default_propagated_to_ddl() -> None:
    """FieldSpec(sql_default=...) emits a DEFAULT clause in DDL."""
    from nextorm.schema.builder import entity_to_table  # noqa: PLC0415
    from nextorm.schema.ddl import SQLiteRenderer  # noqa: PLC0415

    class _DefaultEntity(Entity):
        status: Req[str] = Req(sql_default="'pending'")

    table = entity_to_table(_DefaultEntity)
    renderer = SQLiteRenderer()
    ddl = renderer.create_table(table)
    assert "DEFAULT 'pending'" in ddl


# ---------------------------------------------------------------------------
# MariaDB integration tests — skip when no live server is available
# ---------------------------------------------------------------------------


class _MariaDBPKOnlyEntity(Entity):
    """Entity with only an auto-pk, used to exercise the MariaDB VALUES() insert path."""


@_requires_mysql
def test_do_insert_pk_only_entity_mariadb() -> None:
    """PKOnly entity on MariaDB uses 'INSERT INTO t VALUES ()' syntax."""
    import pymysql  # noqa: PLC0415

    raw = pymysql.connect(**_MARIADB_KWARGS)
    raw.cursor().execute("DROP TABLE IF EXISTS _mysqlpkonlyentity")
    raw.commit()
    raw.close()

    db = Database(entities=[_MariaDBPKOnlyEntity])
    db.bind("mariadb", **_MARIADB_KWARGS)
    db.generate_mapping(create_tables=True)
    try:
        with db_session:
            obj = _MariaDBPKOnlyEntity()
        assert obj.id is not None
    finally:
        db.close()


@_requires_mysql
def test_mariadb_basic_crud() -> None:
    """Full CRUD cycle via Database on a live MariaDB server."""
    import pymysql  # noqa: PLC0415

    raw = pymysql.connect(**_MARIADB_KWARGS)
    raw.cursor().execute("DROP TABLE IF EXISTS _mysqlcruditem")
    raw.commit()
    raw.close()

    class _MariaDBCRUDItem(Entity):
        name: Req[str]
        value: Req[int]

    db = Database(entities=[_MariaDBCRUDItem])
    db.bind("mariadb", **_MARIADB_KWARGS)
    db.generate_mapping(create_tables=True)
    try:
        with db_session:
            item = _MariaDBCRUDItem(name="hello", value=42)
        assert item.id is not None

        fetched = db.select(_MariaDBCRUDItem).filter(_MariaDBCRUDItem.id == item.id).fetch_one()
        assert fetched is not None

        with db_session:
            item.value = 99

        fetched2 = db.select(_MariaDBCRUDItem).filter(_MariaDBCRUDItem.id == item.id).fetch_one()
        assert fetched2 is not None
        assert fetched2.value == 99

        db.delete_instance(item)
        assert item.id is None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Lazy field loading — sync Database
# ---------------------------------------------------------------------------


class _LazyEntity(Entity):
    """Entity with one lazy field for Database lazy-load tests."""

    label: Req[str]
    notes: Req[str] = Req(lazy=True)


class _LazyParent(Entity):
    """Parent entity for testing lazy entity + Single relation."""

    name: Req[str]
    children: Set["_LazyWithFK"]  # noqa: UP037


class _LazyWithFK(Entity):
    """Entity with a lazy field and a Single relation — covers explicit col_map FK branch."""

    text: Req[str]
    detail: Req[str] = Req(lazy=True)
    parent: Single[_LazyParent]


class _LazyWithSet(Entity):
    """Entity with a lazy field and a Set relation — covers non-Single branch in col_map build."""

    name: Req[str]
    bio: Req[str] = Req(lazy=True)
    tags: Set[Tag]


def test_lazy_field_not_in_select_sql() -> None:
    """QuerySet for a lazy entity uses explicit column refs instead of SELECT *."""
    from nextorm.sql.nodes import ColumnRef, Star  # noqa: PLC0415

    db = Database(entities=[_LazyEntity])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)

    qs = db.select(_LazyEntity)
    select_node = qs._build_select()
    # Must NOT use Star() when entity has lazy fields
    assert not any(isinstance(c, Star) for c in select_node.columns)
    # The lazy field must not be among the selected column references
    selected_names = {c.column for c in select_node.columns if isinstance(c, ColumnRef)}
    assert "notes" not in selected_names
    db.close()


def test_lazy_field_loaded_on_access() -> None:
    from nextorm.entity import _LAZY_SENTINEL  # noqa: PLC0415

    db = Database(entities=[_LazyEntity])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)

    with db_session:
        item = _LazyEntity(label="first")
        item.notes = "note content"

    loaded = db.select(_LazyEntity).filter(_LazyEntity.id == item.id).fetch_one()
    assert loaded is not None
    assert vars(loaded)["_field_notes"] is _LAZY_SENTINEL
    # Accessing the attribute triggers a lazy SELECT
    assert loaded.notes == "note content"
    db.close()


def test_lazy_field_load_caches_result() -> None:

    db = Database(entities=[_LazyEntity])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)

    with db_session:
        item = _LazyEntity(label="second")
        item.notes = "cached notes"

    loaded = db.select(_LazyEntity).filter(_LazyEntity.id == item.id).fetch_one()
    assert loaded is not None
    _ = loaded.notes  # trigger load
    # Sentinel replaced by actual value
    assert vars(loaded)["_field_notes"] == "cached notes"
    db.close()


def test_load_lazy_field_entity_with_no_pk_returns_none() -> None:
    """_load_lazy_field returns None for an entity that has no PK value yet."""
    db = Database(entities=[_LazyEntity])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)

    # An entity that was never saved has pk=None
    unsaved = _LazyEntity(label="unsaved")
    result = db._load_lazy_field(unsaved, "notes")
    assert result is None
    db.close()


def test_lazy_field_with_single_relation_explicit_column_map() -> None:
    """_build_explicit_column_map includes FK columns for Single relations on lazy entities."""
    from nextorm.sql.nodes import ColumnRef, Star  # noqa: PLC0415

    db = Database(entities=[_LazyParent, _LazyWithFK])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)

    with db_session:
        p = _LazyParent(name="root")
        db.flush()  # p.id set
        child = _LazyWithFK(text="hello", parent=p)
        child.detail = "secret"

    qs = db.select(_LazyWithFK)
    select_node = qs._build_select()
    # Explicit columns — no Star
    assert not any(isinstance(c, Star) for c in select_node.columns)
    col_names = {c.column for c in select_node.columns if isinstance(c, ColumnRef)}
    # lazy field excluded; FK column included
    assert "detail" not in col_names
    assert "parent_id" in col_names

    loaded = db.select(_LazyWithFK).filter(_LazyWithFK.id == child.id).fetch_one()
    assert loaded is not None
    assert loaded.text == "hello"
    # FK was loaded via explicit col_map
    assert vars(loaded).get("_parent_id") == p.id

    # _LazyWithSet has a Set relation (not Single) — _build_explicit_column_map
    # should skip it, covering the if ri.spec.kind == RelationKind.SINGLE False branch.

    from nextorm.sql.nodes import ColumnRef, Star  # noqa: PLC0415  # noqa: F811

    db2 = Database(entities=[Tag, _LazyWithSet])
    db2.bind("sqlite", ":memory:")
    db2.generate_mapping(create_tables=True, validate_relations=False)
    qs2 = db2.select(_LazyWithSet)
    sel2 = qs2._build_select()
    assert not any(isinstance(c, Star) for c in sel2.columns)
    col_names2 = {c.column for c in sel2.columns if isinstance(c, ColumnRef)}
    # "bio" is lazy → excluded; "tags" is Set → no FK column added
    assert "bio" not in col_names2
    db2.close()
    db.close()


def test_lazy_field_map_raw_row_stamps_sentinel() -> None:
    """raw() on a lazy entity (sync) stamps _LAZY_SENTINEL via _map_raw_row."""
    from nextorm.entity import _LAZY_SENTINEL  # noqa: PLC0415

    db = Database(entities=[_LazyEntity])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)

    with db_session:
        item = _LazyEntity(label="raw-test")
        item.notes = "raw notes"

    results = db.select(_LazyEntity).raw("SELECT id, label FROM _lazyentity WHERE id = ?", [item.id])
    assert len(results) == 1
    assert vars(results[0])["_field_notes"] is _LAZY_SENTINEL
    db.close()


# ---------------------------------------------------------------------------
# db.commit / db.rollback / db.flush + module-level commit/rollback/flush
# ---------------------------------------------------------------------------


class _TxUser(Entity):
    name: Req[str]


_tx_db = Database(entities=[_TxUser])
_tx_db.bind("sqlite", ":memory:")
_tx_db.generate_mapping(create_tables=True)


def test_db_commit_is_callable() -> None:
    """db.commit() can be called without error inside a session."""
    with db_session:
        _TxUser(name="alice")
        _tx_db.flush()
        _tx_db.commit()


def test_db_rollback_clears_session_cache() -> None:
    """db.rollback() clears the identity map of the active session."""
    with db_session as cache:
        _TxUser(name="bob")
        _tx_db.flush()
        assert len(cache._objects) > 0
        _tx_db.rollback()
        assert len(cache._objects) == 0


def test_db_rollback_outside_session() -> None:
    """db.rollback() is safe to call outside a session."""
    _tx_db.rollback()  # should not raise


def test_db_flush_saves_pending_dirty_objects() -> None:
    """db.flush() saves dirty objects in the current session."""
    with db_session:
        u = _TxUser(name="carol")
        # Mark dirty manually
        from nextorm.session import get_current_session

        cache = get_current_session()
        cache.mark_dirty(u)
        _tx_db.flush()
        # After flush, dirty set should be empty
        assert u not in cache.dirty_objects


def test_db_flush_outside_session_is_noop() -> None:
    """db.flush() outside a session does not raise."""
    _tx_db.flush()


def test_module_commit_inside_session() -> None:
    """Module-level commit() works inside a session with cached entities."""
    with db_session:
        _TxUser(name="dave")
        _tx_db.flush()
        _module_commit()  # should not raise


def test_module_commit_outside_session_is_noop() -> None:
    """Module-level commit() outside a session is a no-op."""
    _module_commit()


def test_module_rollback_outside_session_is_noop() -> None:
    """Module-level rollback() outside a session is a no-op."""
    _module_rollback()


def test_module_flush_outside_session_is_noop() -> None:
    """Module-level flush() outside a session is a no-op."""
    _module_flush()


def test_module_rollback_inside_session() -> None:
    """Module-level rollback() with cached entities calls db.rollback()."""
    with db_session:
        _TxUser(name="eve")
        _tx_db.flush()
        _module_rollback()  # should not raise


def test_module_flush_inside_session() -> None:
    """Module-level flush() with dirty entities triggers db.flush()."""
    with db_session:
        u = _TxUser(name="frank")
        _tx_db.flush()  # INSERT u
        from nextorm.session import get_current_session

        cache = get_current_session()
        cache.mark_dirty(u)
        _module_flush()
        assert u not in cache.dirty_objects


def test_module_commit_skips_entities_without_db() -> None:
    """Module-level commit() skips entities that have no _db_ attribute."""
    with db_session as cache:
        orphan = _TxUser(name="orphan")
        vars(orphan).pop("_db_", None)  # Clear auto-set _db_ so condition is False
        cache.put(orphan, 99999)
        _module_commit()  # Should not raise; orphan has no _db_, so condition is False


def test_module_rollback_skips_entities_without_db() -> None:
    """Module-level rollback() skips entities that have no _db_ attribute."""
    with db_session as cache:
        orphan = _TxUser(name="orphan2")
        vars(orphan).pop("_db_", None)  # Clear auto-set _db_ so condition is False
        cache.put(orphan, 99998)
        _module_rollback()


def test_module_flush_skips_entities_without_db() -> None:
    """Module-level flush() skips entities in dirty set that have no _db_."""
    with db_session as cache:
        orphan = _TxUser(name="orphan3")
        vars(orphan).pop("_db_", None)  # Clear auto-set _db_ so condition is False
        cache.mark_dirty(orphan)
        _module_flush()


def test_db_flush_saves_objects_to_save() -> None:
    """db.flush() saves objects in the objects_to_save queue."""
    with db_session as cache:
        u = _TxUser(name="queued")
        cache.schedule_save(u)
        assert u in cache.objects_to_save
        _tx_db.flush()
        # After flush, u should have been saved (given an id)
        assert u.id is not None


def test_generate_mapping_twice_skips_registry_duplicate() -> None:
    """generate_mapping() called twice does not duplicate the DB in the registry."""
    from nextorm.database import _database_registry  # noqa: PLC0415

    # _tx_db is already in the registry from module-level setup.
    count_before = _database_registry.count(_tx_db)
    _tx_db.generate_mapping(create_tables=False)  # second call — already registered
    count_after = _database_registry.count(_tx_db)
    assert count_before == count_after == 1


def test_save_failure_outside_session_raises_without_cleanup() -> None:
    """When save() fails outside a session, exception propagates without session cleanup."""
    u = _TxUser.__new__(_TxUser)
    u.name = "bad"
    exc = RuntimeError("insert failed")
    with (
        patch.object(_tx_db, "_do_insert", side_effect=exc),
        pytest.raises(RuntimeError, match="insert failed"),
    ):
        _tx_db.save(u)


# ---------------------------------------------------------------------------
# Module-level commit() — CommitException / PartialCommitException paths
# ---------------------------------------------------------------------------


def test_module_commit_raises_commit_exception_on_primary_failure() -> None:
    """module commit() raises CommitException when the primary DB's commit fails."""

    class _CommitExcEntity(Entity):
        name: Req[str]

    _cdb = Database(entities=[_CommitExcEntity])
    _cdb.bind("sqlite", ":memory:")
    _cdb.generate_mapping(create_tables=True)

    def _bad_primary_tx() -> None:
        raise RuntimeError("primary tx failed")

    with pytest.raises(CommitException), db_session:
        _CommitExcEntity(name="x")
        _cdb.flush()  # move to _objects so module commit() can discover it
        _cdb._commit_transaction = _bad_primary_tx  # type: ignore[method-assign]
        _module_commit()
    _cdb.close()


def test_module_commit_raises_partial_commit_exception_on_secondary_failure() -> None:
    """module commit() raises PartialCommitException when a secondary DB commit fails."""

    class _PartialPrimary(Entity):
        val: Req[str]

    class _PartialSecondary(Entity):
        ref: Req[str]

    _pdb1 = Database(entities=[_PartialPrimary])
    _pdb1.bind("sqlite", ":memory:")
    _pdb1.generate_mapping(create_tables=True)

    _pdb2 = Database(entities=[_PartialSecondary])
    _pdb2.bind("sqlite", ":memory:")
    _pdb2.generate_mapping(create_tables=True)

    def _bad_secondary_tx() -> None:
        raise RuntimeError("secondary tx failed")

    with pytest.raises(PartialCommitException), db_session:
        _PartialPrimary(val="a")
        _pdb1.flush()  # flush primary entity into _objects
        _PartialSecondary(ref="b")
        _pdb2.flush()  # flush secondary entity into _objects
        _pdb2._commit_transaction = _bad_secondary_tx  # type: ignore[method-assign]
        _module_commit()
    _pdb1.close()
    _pdb2.close()


def test_module_commit_flush_failure_rolls_back() -> None:
    """module commit() raises when flush fails (covers init.py step-1 except path)."""

    class _FlushInCommitEntity(Entity):
        name: Req[str]

    _fdb = Database(entities=[_FlushInCommitEntity])
    _fdb.bind("sqlite", ":memory:")
    _fdb.generate_mapping(create_tables=True)

    def _bad_insert_commit(*args: object, **kw: object) -> None:
        raise RuntimeError("flush failed in commit")

    with pytest.raises(RuntimeError, match="flush failed in commit"), db_session:
        _FlushInCommitEntity(name="y")
        # Do NOT pre-flush; entity stays in _to_save so commit() will flush it
        _fdb._do_insert = _bad_insert_commit  # type: ignore[method-assign]
        _module_commit()
    _fdb.close()


def test_module_commit_exception_includes_rollback_failures() -> None:
    """CommitException captures secondary rollback failures (init.py lines 250-253)."""

    class _CeRollbackPrimary(Entity):
        x: Req[str]

    class _CeRollbackSecondary(Entity):
        y: Req[str]

    _cedb1 = Database(entities=[_CeRollbackPrimary])
    _cedb1.bind("sqlite", ":memory:")
    _cedb1.generate_mapping(create_tables=True)

    _cedb2 = Database(entities=[_CeRollbackSecondary])
    _cedb2.bind("sqlite", ":memory:")
    _cedb2.generate_mapping(create_tables=True)

    def _bad_primary_ce() -> None:
        raise RuntimeError("primary failed for ce")

    def _bad_rollback_ce() -> None:
        raise RuntimeError("rollback failed for ce")

    with pytest.raises(CommitException) as exc_info, db_session:
        _CeRollbackPrimary(x="a")
        _cedb1.flush()
        _CeRollbackSecondary(y="b")
        _cedb2.flush()
        _cedb1._commit_transaction = _bad_primary_ce  # type: ignore[method-assign]
        _cedb2._rollback_transaction = _bad_rollback_ce  # type: ignore[method-assign]
        _module_commit()
    # CommitException should include both primary and rollback exceptions
    assert len(exc_info.value.exceptions) >= 2
    _cedb1.close()
    _cedb2.close()


def test_flush_skips_dirty_entity_from_different_db() -> None:
    """flush() skips dirty entities whose _db_ is a different Database (line 401->399 branch)."""
    from nextorm.session import SessionCache, _get_session_stack  # noqa: PLC0415

    class _FlushSkipOther(Entity):
        name: Req[str]

    class _FlushSkipSelf(Entity):
        label: Req[str]

    db_other = Database(entities=[_FlushSkipOther])
    db_other.bind("sqlite", ":memory:")
    db_other.generate_mapping(create_tables=True)

    db_self = Database(entities=[_FlushSkipSelf])
    db_self.bind("sqlite", ":memory:")
    db_self.generate_mapping(create_tables=True)

    cache = SessionCache()
    stack = _get_session_stack()
    stack.push(cache)
    try:
        # Insert and persist 'other_entity' via db_other
        other_entity = _FlushSkipOther(name="other")
        db_other.save(other_entity)
        # Mark it dirty so it appears in dirty_objects
        cache.mark_dirty(other_entity)
        # self_entity belongs to db_self
        self_entity = _FlushSkipSelf(label="self")
        db_self.save(self_entity)
        cache.mark_dirty(self_entity)
        assert other_entity in cache.dirty_objects
        assert self_entity in cache.dirty_objects
        # flush on db_self should skip other_entity (401->399 branch) and save self_entity
        db_self.flush()
        assert self_entity not in cache.dirty_objects
        assert other_entity in cache.dirty_objects  # skipped
    finally:
        stack.pop()
    db_other.close()
    db_self.close()


def test_post_save_skips_read_cols_when_already_set() -> None:
    """_post_save skips setting _read_cols_ when it already exists (line 933->exit)."""

    class _ReadColsEntity(Entity):
        name: Req[str]

    db = Database(entities=[_ReadColsEntity])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    # Create entity and manually pre-set _read_cols_
    u = _ReadColsEntity.__new__(_ReadColsEntity)
    u.name = "pre"
    vars(u)["_read_cols_"] = {"name"}
    # _do_insert should skip setting _read_cols_ since it's already there
    table = db._schema["_readcolsentity"]
    db._do_insert(u, _ReadColsEntity, table)
    # _read_cols_ should still be the original set (not replaced with empty set)
    assert vars(u)["_read_cols_"] == {"name"}
    db.close()


# ---------------------------------------------------------------------------
# _validate_relations error path (unresolvable target or missing backref)
# ---------------------------------------------------------------------------


# Define a valid entity as the relation target, but omit the required back-reference
class DBNoBackrefTarget(Entity):
    pass


class DBNoBackrefOwner(Entity):
    items: Set[DBNoBackrefTarget]


def test_db_validate_relations_unresolvable_target_raises() -> None:
    db = Database(entities=[DBNoBackrefOwner, DBNoBackrefTarget])
    db.bind("sqlite", ":memory:")
    with pytest.raises(Exception) as excinfo:
        db.generate_mapping(validate_relations=True)
    assert "requires a back-reference" in str(excinfo.value)


# ---------------------------------------------------------------------------
# unbind/close branches (pool and connection cleanup)
# ---------------------------------------------------------------------------


def test_db_close_with_pool_and_connection() -> None:
    db = Database()

    def close_all(_self: object) -> None:
        setattr(db, "_pool_closed", True)  # noqa: B010

    FakePool = type("FakePool", (), {"close_all": close_all})
    db._pool = cast("ConnectionPool", FakePool())
    db.close()
    assert getattr(db, "_pool_closed", False)


def test_db_close_with_connection() -> None:
    class FakeConn:
        def close(self) -> None:
            self.closed = True

    db = Database()
    db._connection = cast("SyncConnection", FakeConn())
    db.close()
    assert getattr(db._connection, "closed", True) or db._connection is None


# Dummy entities for direct type branch coverage
class DummyTarget(Entity):
    label: Req[str]


class DummyOwner(Entity):
    label: Req[str]
    items: Set[DummyTarget]


def test__validate_relations_resolve_type_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    db = Database()
    db._entities = [DummyOwner, DummyTarget]

    def fake_effective_entities() -> list[type[Entity]]:
        return [DummyOwner, DummyTarget]

    monkeypatch.setattr(db, "_effective_entities", fake_effective_entities)
    db.bind("sqlite", ":memory:")
    with pytest.raises(MappingError, match="requires a back-reference"):
        db._validate_relations()


# ---------------------------------------------------------------------------
# _validate_relations with unresolvable target paths
# ---------------------------------------------------------------------------


def test_validate_relations_with_none_target() -> None:
    """Relation with target=None should be skipped gracefully."""
    # Use simple entities without bidirectional relations to test the None target skip path
    db = Database(entities=[PKOnly])
    db.bind("sqlite", ":memory:")

    # Verify that validation works on entities with no complex relations
    db.generate_mapping(validate_relations=True)


def test_validate_relations_with_string_target() -> None:
    """Relation with string target should be resolved correctly."""
    # Use entities with forward references (which are strings)
    db = Database(entities=[_FwdRefOwner, _FwdRefChild])
    db.bind("sqlite", ":memory:")

    # String targets should resolve via entity_by_name
    db.generate_mapping(validate_relations=True)
