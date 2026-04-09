"""Tests for composite primary key support (PrimaryKey() directive)."""

from __future__ import annotations

import sqlite3

import pytest

from nextorm import Database, Entity, Opt, PrimaryKey, Req, Single, flush
from nextorm.async_database import AsyncDatabase
from nextorm.schema import SQLiteRenderer, build_schema, entity_to_table
from nextorm.schema.ddl import MariaDBRenderer, PostgresRenderer
from nextorm.session import db_session

# ---------------------------------------------------------------------------
# Entities used across all tests
# ---------------------------------------------------------------------------


class Student(Entity):
    name: Req[str]


class Course(Entity):
    title: Req[str]


class Enrollment(Entity):
    """Scalar-scalar composite PK."""

    student_id: Req[int]
    course_id: Req[int]
    grade: Opt[str]
    _pk_ = PrimaryKey("student_id", "course_id")


class PurchaseOrder(Entity):
    ref: Req[str]


class Product(Entity):
    sku: Req[str]


class OrderLine(Entity):
    """Relation-scalar composite PK (FK columns in PK)."""

    order: Single[PurchaseOrder]
    product: Single[Product]
    quantity: Req[int]
    _pk_ = PrimaryKey("order", "product")


# ---------------------------------------------------------------------------
# Entity metaclass / class-level assertions
# ---------------------------------------------------------------------------


def test_pk_fields_tuple_on_composite() -> None:
    assert Enrollment._pk_fields_ == ("student_id", "course_id")


def test_pk_field_is_none_for_composite() -> None:
    assert Enrollment._pk_field_ is None


def test_pk_fields_for_single_pk_entity() -> None:
    class Simple(Entity):
        name: Req[str]

    assert Simple._pk_fields_ == ("id",)
    assert Simple._pk_field_ == "id"


def test_no_auto_id_injected_for_composite_pk() -> None:
    """No auto ``id`` column is injected when PrimaryKey() is declared."""
    assert "id" not in Enrollment._fields_


def test_relation_pk_fields_tuple() -> None:
    assert OrderLine._pk_fields_ == ("order", "product")
    assert OrderLine._pk_field_ is None


# ---------------------------------------------------------------------------
# get_pk() and __repr__
# ---------------------------------------------------------------------------


def test_get_pk_returns_tuple_for_composite() -> None:
    e = Enrollment(student_id=1, course_id=2, grade="A")
    assert e.get_pk() == (1, 2)


def test_get_pk_returns_none_when_any_part_missing() -> None:
    e = Enrollment(student_id=1)
    assert e.get_pk() is None


def test_repr_shows_all_pk_fields() -> None:
    e = Enrollment(student_id=3, course_id=7)
    r = repr(e)
    assert "student_id=3" in r
    assert "course_id=7" in r


# ---------------------------------------------------------------------------
# DDL — entity_to_table / build_schema
# ---------------------------------------------------------------------------


def test_entity_to_table_has_no_inline_primary_key_for_composite() -> None:
    """No column should carry primary_key=True inline when composite PK declared."""
    table = entity_to_table(Enrollment)
    pk_cols = [c for c in table.columns if c.primary_key]
    assert len(pk_cols) == 2
    names = {c.name for c in pk_cols}
    assert names == {"student_id", "course_id"}


def test_sqlite_ddl_has_table_level_primary_key() -> None:
    table = entity_to_table(Enrollment)
    sql = SQLiteRenderer().create_table(table)
    assert "PRIMARY KEY (student_id, course_id)" in sql
    # Inline PRIMARY KEY must not appear on individual column defs
    lines = sql.split("\n")
    col_lines = [
        col_line for col_line in lines if "student_id" in col_line or "course_id" in col_line
    ]
    for col_line in col_lines:
        assert "PRIMARY KEY" not in col_line or "PRIMARY KEY (student_id," in col_line


def test_postgres_ddl_has_table_level_primary_key() -> None:
    table = entity_to_table(Enrollment)
    sql = PostgresRenderer().create_table(table)
    assert "PRIMARY KEY (student_id, course_id)" in sql


def test_mysql_ddl_has_table_level_primary_key() -> None:
    table = entity_to_table(Enrollment)
    sql = MariaDBRenderer().create_table(table)
    assert "PRIMARY KEY (student_id, course_id)" in sql


def test_relation_pk_fk_column_marked_primary_key() -> None:
    """FK columns for relation PK fields are marked primary_key=True."""
    tables = build_schema([PurchaseOrder, Product, OrderLine])
    ol_table = tables["orderline"]
    pk_cols = {c.name for c in ol_table.columns if c.primary_key}
    assert pk_cols == {"order_id", "product_id"}


def test_relation_pk_ddl_table_level_constraint() -> None:
    tables = build_schema([PurchaseOrder, Product, OrderLine])
    ol_table = tables["orderline"]
    sql = SQLiteRenderer().create_table(ol_table)
    assert "PRIMARY KEY (order_id, product_id)" in sql


# ---------------------------------------------------------------------------
# Database — INSERT / UPDATE / DELETE
# ---------------------------------------------------------------------------


def test_insert_composite_pk_entity() -> None:
    db = Database(entities=[Enrollment])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)

    with db_session:
        e = Enrollment(student_id=1, course_id=10, grade="B")
    assert e.get_pk() == (1, 10)

    rows = db.select(Enrollment).fetch_all()
    assert len(rows) == 1
    assert rows[0].student_id == 1
    assert rows[0].course_id == 10
    db.close()


def test_update_composite_pk_entity() -> None:
    db = Database(entities=[Enrollment])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)

    with db_session:
        e = Enrollment(student_id=2, course_id=20, grade="C")
        e.grade = "A+"

    rows = db.select(Enrollment).fetch_all()
    assert rows[0].grade == "A+"
    db.close()


def test_delete_composite_pk_entity() -> None:
    db = Database(entities=[Enrollment])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)

    with db_session:
        e = Enrollment(student_id=3, course_id=30)
    assert db.select(Enrollment).count() == 1

    db.delete_instance(e)
    assert db.select(Enrollment).count() == 0
    db.close()


def test_delete_composite_pk_clears_pk_values() -> None:
    db = Database(entities=[Enrollment])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)

    with db_session:
        e = Enrollment(student_id=4, course_id=40)
    db.delete_instance(e)

    assert e.student_id is None
    assert e.course_id is None
    db.close()


def test_duplicate_composite_pk_raises() -> None:
    db = Database(entities=[Enrollment])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)

    with db_session:
        Enrollment(student_id=5, course_id=50)
    with pytest.raises(sqlite3.IntegrityError), db_session:
        Enrollment(student_id=5, course_id=50)
    db.close()


def test_delete_unsaved_composite_pk_raises() -> None:
    db = Database(entities=[Enrollment])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)

    e = Enrollment()  # No PK values set → _get_pk_val returns None
    with pytest.raises(ValueError, match="primary key is None"):
        db.delete_instance(e)
    db.close()


def test_insert_relation_composite_pk() -> None:
    db = Database(entities=[PurchaseOrder, Product, OrderLine])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)

    with db_session:
        o = PurchaseOrder(ref="ORD-1")
        p = Product(sku="SKU-A")
        flush()
        line = OrderLine(quantity=3)
        line.order = o
        line.product = p

    rows = db.select(OrderLine).fetch_all()
    assert len(rows) == 1
    assert rows[0].quantity == 3
    # PK value is a tuple of FK ids
    assert line.get_pk() == (o.id, p.id)
    db.close()


def test_update_relation_composite_pk() -> None:
    db = Database(entities=[PurchaseOrder, Product, OrderLine])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)

    with db_session:
        o = PurchaseOrder(ref="ORD-2")
        p = Product(sku="SKU-B")
        flush()
        line = OrderLine(quantity=1)
        line.order = o
        line.product = p
        line.quantity = 99

    rows = db.select(OrderLine).fetch_all()
    assert rows[0].quantity == 99
    db.close()


def test_delete_relation_composite_pk() -> None:
    db = Database(entities=[PurchaseOrder, Product, OrderLine])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)

    with db_session:
        o = PurchaseOrder(ref="ORD-3")
        p = Product(sku="SKU-C")
        flush()
        line = OrderLine(quantity=2)
        line.order = o
        line.product = p

    db.delete_instance(line)
    assert db.select(OrderLine).count() == 0
    db.close()


# ---------------------------------------------------------------------------
# Constraints list excludes PrimaryKey() directive
# ---------------------------------------------------------------------------


def test_constraints_excludes_primary_key_directive() -> None:
    """PrimaryKey() is not in _constraints_; it only sets _pk_fields_."""
    assert Enrollment._constraints_ == []


def test_constraints_preserved_alongside_primary_key() -> None:
    """Other composite_key/composite_index alongside PrimaryKey() still work."""
    from nextorm import composite_index, composite_key

    class Hybrid(Entity):
        a: Req[int]
        b: Req[int]
        c: Req[str]
        _pk_ = PrimaryKey("a", "b")
        _idx_ = composite_index("b", "c")
        _unq_ = composite_key("a", "c")

    assert Hybrid._pk_fields_ == ("a", "b")
    assert len(Hybrid._constraints_) == 2


# ---------------------------------------------------------------------------
# Async — delete relation-in-PK (covers async_database.py FK-id clear branch)
# ---------------------------------------------------------------------------


class AsyncVendor(Entity):
    name: Req[str]


class AsyncItem(Entity):
    code: Req[str]


class AsyncStock(Entity):
    vendor: Single[AsyncVendor]
    item: Single[AsyncItem]
    qty: Req[int]
    _pk_ = PrimaryKey("vendor", "item")


@pytest.mark.asyncio
async def test_async_delete_relation_composite_pk() -> None:
    """Async delete of a relation-composite-PK entity clears FK id fields."""
    async with AsyncDatabase(entities=[AsyncVendor, AsyncItem, AsyncStock]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        v = AsyncVendor(name="ACME")
        await db.asave(v)
        i = AsyncItem(code="X1")
        await db.asave(i)

        s = AsyncStock(qty=10)
        s.vendor = v
        s.item = i
        await db.asave(s)

        await db.adelete_instance(s)

        # FK-id fields should be cleared after async delete
        assert s.__dict__.get("_vendor_id") is None
        assert s.__dict__.get("_item_id") is None
