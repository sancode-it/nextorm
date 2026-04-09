"""Tests for UUID v7, UUID v4, and ULID field types and auto-PK generation."""

from __future__ import annotations

import asyncio
import re
import types
import uuid as _uuid_stdlib
from unittest.mock import patch

from nextorm import PK, Database, Entity, Req
from nextorm.fields import (
    ULID,
    FieldSpec,
    _generate_ulid,
    _generate_uuid7,
    ulid,
    uuid4,
    uuid7,
)
from nextorm.schema.core import Column
from nextorm.schema.ddl import MariaDBRenderer, PostgresRenderer, SQLiteRenderer
from nextorm.session import db_session

# ---------------------------------------------------------------------------
# UUID v7 / ULID generation helpers
# ---------------------------------------------------------------------------

_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")  # Crockford base32 uppercase


class TestGenerateUUID7:
    def test_returns_uuid(self) -> None:
        val = _generate_uuid7()
        assert isinstance(val, _uuid_stdlib.UUID)

    def test_version_7(self) -> None:
        val = _generate_uuid7()
        assert val.version == 7

    def test_variant_is_rfc4122(self) -> None:
        val = _generate_uuid7()
        # RFC 4122 variant: top 2 bits of the 8th byte are 10
        assert (val.int >> 62) & 0b11 == 0b10

    def test_monotonically_increasing(self) -> None:
        # Two consecutive UUID v7 values should have non-decreasing timestamps
        a = _generate_uuid7()
        b = _generate_uuid7()
        # Extract 48-bit timestamp (top 48 bits)
        ts_a = a.int >> 80
        ts_b = b.int >> 80
        assert ts_b >= ts_a

    def test_unique(self) -> None:
        vals = {_generate_uuid7() for _ in range(1000)}
        assert len(vals) == 1000

    def test_python312_fallback(self) -> None:
        """The pure-Python fallback path (no stdlib uuid7) produces valid v7 UUIDs."""
        import nextorm.fields as _fields  # noqa: PLC0415

        # Simulate Python 3.12: a uuid module without uuid7.
        fake_uuid_module = types.SimpleNamespace(UUID=_uuid_stdlib.UUID)
        original = _fields._uuid_stdlib  # type: ignore[attr-defined]
        _fields._uuid_stdlib = fake_uuid_module  # type: ignore[attr-defined, assignment]
        try:
            val = _generate_uuid7()
        finally:
            _fields._uuid_stdlib = original  # type: ignore[attr-defined]
        assert isinstance(val, _uuid_stdlib.UUID)
        assert val.version == 7
        assert (val.int >> 62) & 0b11 == 0b10

    def test_stdlib_path_used_when_uuid7_available(self) -> None:
        """When the stdlib has uuid7, the stdlib implementation is used."""
        import nextorm.fields as _fields  # noqa: PLC0415

        fake_uuid = _uuid_stdlib.UUID(int=(7 << 76) | (0b10 << 62))
        with patch.object(_fields, "_uuid_stdlib") as mock_uuid:
            mock_uuid.uuid7 = lambda: fake_uuid
            mock_uuid.UUID = _uuid_stdlib.UUID
            result = _generate_uuid7()
        assert result is fake_uuid


class TestGenerateULID:
    def test_returns_ulid_instance(self) -> None:
        val = _generate_ulid()
        assert isinstance(val, ULID)

    def test_ulid_is_str_subclass(self) -> None:
        val = _generate_ulid()
        assert isinstance(val, str)

    def test_length_is_26(self) -> None:
        val = _generate_ulid()
        assert len(val) == 26

    def test_crockford_alphabet(self) -> None:
        val = _generate_ulid()
        assert _ULID_RE.match(val), f"Invalid ULID characters: {val!r}"

    def test_monotonically_increasing(self) -> None:
        # ULIDs generated within the same millisecond may have any order in the
        # random component.  Only the 10-character timestamp prefix is guaranteed
        # to be non-decreasing across calls.
        a = _generate_ulid()
        b = _generate_ulid()
        assert b[:10] >= a[:10]

    def test_unique(self) -> None:
        vals = {_generate_ulid() for _ in range(1000)}
        assert len(vals) == 1000


# ---------------------------------------------------------------------------
# ULID as a str subclass
# ---------------------------------------------------------------------------


class TestULIDClass:
    def test_ulid_str_comparison(self) -> None:
        a = ULID("01ARZ3NDEKTSV4RRFFQ69G5FAV")
        b = ULID("01ARZ3NDEKTSV4RRFFQ69G5FAW")
        assert a < b

    def test_ulid_is_hashable(self) -> None:
        a = ULID("01ARZ3NDEKTSV4RRFFQ69G5FAV")
        assert hash(a) == hash("01ARZ3NDEKTSV4RRFFQ69G5FAV")


# ---------------------------------------------------------------------------
# Entity metadata: sentinel types in PK[...] annotations
# ---------------------------------------------------------------------------


class UUIDv7Entity(Entity):
    id: PK[uuid7]  # type: ignore[assignment]
    name: Req[str]


class UUIDv4Entity(Entity):
    id: PK[uuid4]  # type: ignore[assignment]
    name: Req[str]


class ULIDEntity(Entity):
    id: PK[ulid]  # type: ignore[assignment]
    name: Req[str]


class UUIDFieldEntity(Entity):
    """Entity with a non-PK UUID field declared via Req[uuid.UUID]."""

    token: Req[_uuid_stdlib.UUID]
    label: Req[str]


class ULIDFieldEntity(Entity):
    """Entity with a non-PK ULID field declared via Req[ULID]."""

    code: Req[ULID]
    label: Req[str]


class TestUUIDAv7SentinelEntityMeta:
    def test_id_field_storage_type_is_uuid(self) -> None:
        fi = UUIDv7Entity._fields_["id"]
        assert fi.py_type is _uuid_stdlib.UUID

    def test_id_field_primary_key(self) -> None:
        fi = UUIDv7Entity._fields_["id"]
        assert fi.spec.primary_key is True

    def test_id_field_auto_is_false(self) -> None:
        """DB auto-increment must be disabled — UUID v7 is generated in Python."""
        fi = UUIDv7Entity._fields_["id"]
        assert fi.spec.auto is False

    def test_id_field_uuid_auto_is_v7(self) -> None:
        fi = UUIDv7Entity._fields_["id"]
        assert fi.spec.uuid_auto == "v7"

    def test_pk_field_is_id(self) -> None:
        assert UUIDv7Entity._pk_field_ == "id"


class TestUUIDv4SentinelEntityMeta:
    def test_id_field_storage_type_is_uuid(self) -> None:
        fi = UUIDv4Entity._fields_["id"]
        assert fi.py_type is _uuid_stdlib.UUID

    def test_id_field_auto_is_false(self) -> None:
        fi = UUIDv4Entity._fields_["id"]
        assert fi.spec.auto is False

    def test_id_field_uuid_auto_is_v4(self) -> None:
        fi = UUIDv4Entity._fields_["id"]
        assert fi.spec.uuid_auto == "v4"


class TestULIDSentinelEntityMeta:
    def test_id_field_storage_type_is_ulid(self) -> None:
        fi = ULIDEntity._fields_["id"]
        assert fi.py_type is ULID

    def test_id_field_auto_is_false(self) -> None:
        fi = ULIDEntity._fields_["id"]
        assert fi.spec.auto is False

    def test_id_field_uuid_auto_is_ulid(self) -> None:
        fi = ULIDEntity._fields_["id"]
        assert fi.spec.uuid_auto == "ulid"

    def test_pk_field_is_id(self) -> None:
        assert ULIDEntity._pk_field_ == "id"


class TestNonPKUUIDField:
    def test_uuid_req_field_type(self) -> None:
        fi = UUIDFieldEntity._fields_["token"]
        assert fi.py_type is _uuid_stdlib.UUID
        assert fi.spec.primary_key is False
        assert fi.spec.uuid_auto is None

    def test_ulid_req_field_type(self) -> None:
        fi = ULIDFieldEntity._fields_["code"]
        assert fi.py_type is ULID
        assert fi.spec.primary_key is False
        assert fi.spec.uuid_auto is None


# ---------------------------------------------------------------------------
# FieldSpec.uuid_auto default is None
# ---------------------------------------------------------------------------


def test_fieldspec_uuid_auto_default_none() -> None:
    spec = FieldSpec()
    assert spec.uuid_auto is None


def test_fieldspec_uuid_auto_set() -> None:
    spec = FieldSpec(uuid_auto="v7")
    assert spec.uuid_auto == "v7"


# ---------------------------------------------------------------------------
# DDL type mappings
# ---------------------------------------------------------------------------


class TestSQLiteUUIDTypes:
    r = SQLiteRenderer()

    def test_uuid_maps_to_text(self) -> None:
        assert self.r.sql_type(Column("id", _uuid_stdlib.UUID)) == "TEXT"

    def test_ulid_maps_to_text(self) -> None:
        assert self.r.sql_type(Column("id", ULID)) == "TEXT"

    def test_uuid_pk_no_autoincrement_in_ddl(self) -> None:
        col = Column("id", _uuid_stdlib.UUID, primary_key=True, auto_increment=False)
        ddl = self.r._column_def(col)
        assert "TEXT PRIMARY KEY" in ddl
        assert "AUTOINCREMENT" not in ddl

    def test_ulid_pk_no_autoincrement_in_ddl(self) -> None:
        col = Column("id", ULID, primary_key=True, auto_increment=False)
        ddl = self.r._column_def(col)
        assert "TEXT PRIMARY KEY" in ddl
        assert "AUTOINCREMENT" not in ddl


class TestPostgresUUIDTypes:
    r = PostgresRenderer()

    def test_uuid_maps_to_uuid_type(self) -> None:
        assert self.r.sql_type(Column("id", _uuid_stdlib.UUID)) == "UUID"

    def test_ulid_maps_to_char26(self) -> None:
        assert self.r.sql_type(Column("id", ULID)) == "CHAR(26)"

    def test_uuid_pk_not_serial(self) -> None:
        # auto_increment=False → no SERIAL substitution
        assert self.r.sql_type(Column("id", _uuid_stdlib.UUID, primary_key=True)) == "UUID"


class TestMariaDBUUIDTypes:
    r = MariaDBRenderer()

    def test_uuid_maps_to_char36(self) -> None:
        assert self.r.sql_type(Column("id", _uuid_stdlib.UUID)) == "CHAR(36)"

    def test_ulid_maps_to_char26(self) -> None:
        assert self.r.sql_type(Column("id", ULID)) == "CHAR(26)"

    def test_uuid_pk_no_auto_increment_in_ddl(self) -> None:
        col = Column("id", _uuid_stdlib.UUID, primary_key=True, auto_increment=False)
        ddl = self.r._column_def(col)
        assert "AUTO_INCREMENT" not in ddl
        assert "PRIMARY KEY" in ddl


# ---------------------------------------------------------------------------
# Database INSERT — auto-generation and no lastrowid overwrite
# ---------------------------------------------------------------------------


def _make_uuid7_db() -> Database:
    db = Database(entities=[UUIDv7Entity])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    return db


def _make_uuid4_db() -> Database:
    db = Database(entities=[UUIDv4Entity])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    return db


def _make_ulid_db() -> Database:
    db = Database(entities=[ULIDEntity])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    return db


class TestDatabaseUUIDv7AutoPK:
    def test_uuid7_pk_is_generated_on_save(self) -> None:
        db = _make_uuid7_db()
        with db_session:
            obj = UUIDv7Entity(name="alice")
        assert obj.id is not None  # UUID set after save
        assert isinstance(obj.id, _uuid_stdlib.UUID)
        db.close()

    def test_uuid7_pk_version_7(self) -> None:
        db = _make_uuid7_db()
        with db_session:
            obj = UUIDv7Entity(name="alice")
        assert isinstance(obj.id, _uuid_stdlib.UUID)
        assert obj.id.version == 7
        db.close()

    def test_uuid7_pk_unique_per_insert(self) -> None:
        db = _make_uuid7_db()
        with db_session:
            a = UUIDv7Entity(name="a")
            b = UUIDv7Entity(name="b")
        assert a.id != b.id
        db.close()

    def test_preset_uuid7_pk_not_overwritten(self) -> None:
        db = _make_uuid7_db()
        preset = _generate_uuid7()
        with db_session:
            obj = UUIDv7Entity(name="alice", id=preset)
            assert obj.id == preset
        db.close()

    def test_uuid7_roundtrip_via_select(self) -> None:
        from nextorm import Database  # noqa: PLC0415

        db = Database(entities=[UUIDv7Entity])
        db.bind("sqlite", ":memory:")
        db.generate_mapping(create_tables=True)
        with db_session:
            obj = UUIDv7Entity(name="roundtrip")
        loaded = db.select(UUIDv7Entity).filter(UUIDv7Entity.name == "roundtrip").fetch_one()
        assert loaded is not None
        # SQLite returns UUIDs as text; compare string representations
        assert str(loaded.id) == str(obj.id)
        db.close()


class TestDatabaseUUIDv4AutoPK:
    def test_uuid4_pk_is_generated_on_save(self) -> None:
        db = _make_uuid4_db()
        with db_session:
            obj = UUIDv4Entity(name="test")
        assert isinstance(obj.id, _uuid_stdlib.UUID)
        db.close()

    def test_uuid4_pk_version_4(self) -> None:
        db = _make_uuid4_db()
        with db_session:
            obj = UUIDv4Entity(name="test")
        assert isinstance(obj.id, _uuid_stdlib.UUID)
        assert obj.id.version == 4
        db.close()


class TestDatabaseULIDAutoPK:
    def test_ulid_pk_is_generated_on_save(self) -> None:
        db = _make_ulid_db()
        with db_session:
            obj = ULIDEntity(name="test")
        assert obj.id is not None
        assert len(str(obj.id)) == 26
        db.close()

    def test_ulid_pk_crockford_chars(self) -> None:
        db = _make_ulid_db()
        with db_session:
            obj = ULIDEntity(name="ulid_test")
        assert obj.id is not None
        assert _ULID_RE.match(str(obj.id))
        db.close()

    def test_ulid_pk_unique_per_insert(self) -> None:
        db = _make_ulid_db()
        with db_session:
            a = ULIDEntity(name="a")
            b = ULIDEntity(name="b")
        assert a.id != b.id
        db.close()

    def test_preset_ulid_pk_not_overwritten(self) -> None:
        db = _make_ulid_db()
        preset = _generate_ulid()
        with db_session:
            obj = ULIDEntity(name="preset", id=preset)
        assert str(obj.id) == str(preset)
        db.close()


# ---------------------------------------------------------------------------
# Async Database INSERT — auto-generation
# ---------------------------------------------------------------------------


class _AsyncUUIDv7Entity(Entity):
    id: PK[uuid7]  # type: ignore[assignment]
    label: Req[str]


class _AsyncUUIDv4Entity(Entity):
    id: PK[uuid4]  # type: ignore[assignment]
    label: Req[str]


class _AsyncULIDEntity(Entity):
    id: PK[ulid]  # type: ignore[assignment]
    label: Req[str]


class TestAsyncDatabaseUUIDAutoGeneration:
    def test_async_uuid7_generated(self) -> None:
        from nextorm import AsyncDatabase  # noqa: PLC0415

        async def run() -> None:
            db = AsyncDatabase(entities=[_AsyncUUIDv7Entity])
            await db.bind("sqlite", ":memory:")
            await db.generate_mapping(create_tables=True)
            obj = _AsyncUUIDv7Entity(label="async_v7")
            await db.asave(obj)
            assert isinstance(obj.id, _uuid_stdlib.UUID)
            assert obj.id.version == 7
            await db.close()

        asyncio.run(run())

    def test_async_uuid4_generated(self) -> None:
        from nextorm import AsyncDatabase  # noqa: PLC0415

        async def run() -> None:
            db = AsyncDatabase(entities=[_AsyncUUIDv4Entity])
            await db.bind("sqlite", ":memory:")
            await db.generate_mapping(create_tables=True)
            obj = _AsyncUUIDv4Entity(label="async_v4")
            await db.asave(obj)
            assert isinstance(obj.id, _uuid_stdlib.UUID)
            assert obj.id.version == 4
            await db.close()

        asyncio.run(run())

    def test_async_ulid_generated(self) -> None:
        from nextorm import AsyncDatabase  # noqa: PLC0415

        async def run() -> None:
            db = AsyncDatabase(entities=[_AsyncULIDEntity])
            await db.bind("sqlite", ":memory:")
            await db.generate_mapping(create_tables=True)
            obj = _AsyncULIDEntity(label="async_ulid")
            await db.asave(obj)
            assert obj.id is not None
            assert len(str(obj.id)) == 26
            await db.close()

        asyncio.run(run())

    def test_async_preset_uuid_not_overwritten(self) -> None:
        from nextorm import AsyncDatabase  # noqa: PLC0415

        async def run() -> None:
            db = AsyncDatabase(entities=[_AsyncUUIDv7Entity])
            await db.bind("sqlite", ":memory:")
            await db.generate_mapping(create_tables=True)
            preset = _generate_uuid7()
            obj = _AsyncUUIDv7Entity(label="preset", id=preset)
            await db.asave(obj)
            assert obj.id == preset
            await db.close()

        asyncio.run(run())
