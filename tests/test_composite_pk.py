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


class GradeNote(Entity):
    """FK to a composite-PK entity (Enrollment)."""

    enrollment: Single[Enrollment]
    note: Opt[str]


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


class OrderLineSummary(Entity):
    """FK to a relation-based-composite-PK entity (OrderLine).

    Used to exercise the ``elif fname in relations`` branch of
    ``_derive_composite_fk_cols`` when the PK fields of the target entity are
    themselves relations.
    """

    order_line: Single[OrderLine]
    comment: Opt[str]


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
    assert 'PRIMARY KEY ("student_id", "course_id")' in sql
    # Inline PRIMARY KEY must not appear on individual column defs
    lines = sql.split("\n")
    col_lines = [
        col_line for col_line in lines if "student_id" in col_line or "course_id" in col_line
    ]
    for col_line in col_lines:
        assert "PRIMARY KEY" not in col_line or 'PRIMARY KEY ("student_id",' in col_line


def test_postgres_ddl_has_table_level_primary_key() -> None:
    table = entity_to_table(Enrollment)
    sql = PostgresRenderer().create_table(table)
    assert 'PRIMARY KEY ("student_id", "course_id")' in sql


def test_mysql_ddl_has_table_level_primary_key() -> None:
    table = entity_to_table(Enrollment)
    sql = MariaDBRenderer().create_table(table)
    assert "PRIMARY KEY (`student_id`, `course_id`)" in sql


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
    assert 'PRIMARY KEY ("order_id", "product_id")' in sql


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


# ---------------------------------------------------------------------------
# Composite FK: Single relation pointing to a composite-PK entity
# ---------------------------------------------------------------------------


def test_build_schema_composite_fk_columns() -> None:
    """build_schema derives two FK columns for a Single pointing at Enrollment."""
    schema = build_schema([Enrollment, GradeNote])
    table = schema["gradenote"]
    col_names = [c.name for c in table.columns]
    assert "enrollment_student_id" in col_names
    assert "enrollment_course_id" in col_names


def test_insert_composite_fk_entity() -> None:
    """Saving a GradeNote with a composite-PK FK writes both FK columns."""
    db = Database(entities=[Enrollment, GradeNote])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)

    with db_session:
        e = Enrollment(student_id=1, course_id=10, grade="A")
        flush()
        gn = GradeNote(note="great")
        gn.enrollment = e

    rows = db.select(GradeNote).fetch_all()
    assert len(rows) == 1
    assert rows[0].note == "great"
    # FK tuple stored in __dict__
    assert rows[0].__dict__.get("_enrollment_id") == (1, 10)
    db.close()


def test_fetch_composite_fk_entity() -> None:
    """Fetching GradeNote builds composite FK tuple from two DB columns."""
    db = Database(entities=[Enrollment, GradeNote])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)

    with db_session:
        e = Enrollment(student_id=2, course_id=20, grade="B")
        flush()
        gn = GradeNote(note="ok")
        gn.enrollment = e

    loaded = db.select(GradeNote).fetch_one()
    assert loaded is not None
    assert loaded.__dict__.get("_enrollment_id") == (2, 20)
    db.close()


def test_lazy_load_composite_fk_entity() -> None:
    """Accessing .enrollment on a GradeNote triggers lazy-load via tuple FK."""
    db = Database(entities=[Enrollment, GradeNote])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)

    with db_session:
        e = Enrollment(student_id=3, course_id=30, grade="C")
        flush()
        gn = GradeNote(note="lazy")
        gn.enrollment = e

    loaded = db.select(GradeNote).fetch_one()
    assert loaded is not None
    # Lazy-load via composite FK tuple
    with db_session:
        related = loaded.enrollment
    assert related is not None
    assert related.student_id == 3
    assert related.course_id == 30
    db.close()


def test_update_composite_fk_entity() -> None:
    """Updating GradeNote note re-writes both FK columns."""
    db = Database(entities=[Enrollment, GradeNote])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)

    with db_session:
        e = Enrollment(student_id=4, course_id=40, grade="D")
        flush()
        gn = GradeNote(note="first")
        gn.enrollment = e

    pk = gn.id
    with db_session:
        loaded = db.select(GradeNote).filter(GradeNote.id == pk).fetch_one()
        assert loaded is not None
        loaded.note = "revised"

    result = db.select(GradeNote).filter(GradeNote.id == pk).fetch_one()
    assert result is not None
    assert result.note == "revised"
    db.close()


def test_entity_getitem_composite_pk_via_relation_based() -> None:
    """Entity[pk] on OrderLine (relation-based composite PK) exercises _pk_col_for_field
    through the 'fname in relations' branch."""
    db = Database(entities=[PurchaseOrder, Product, OrderLine])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)

    with db_session:
        o = PurchaseOrder(ref="REF-1")
        p = Product(sku="SKU-Z")
        flush()
        line = OrderLine(quantity=5)
        line.order = o
        line.product = p

    with db_session:
        result = OrderLine[o.id, p.id]  # type: ignore[type-arg, name-defined]
        assert result.quantity == 5

    db.close()


def test_build_schema_composite_fk_relation_based_pk() -> None:
    """build_schema derives FK cols for Single pointing at relation-based composite PK.

    Covers the ``elif fname in relations`` branch of ``_derive_composite_fk_cols``
    where the target entity's PK fields are themselves relations.
    """
    schema = build_schema([PurchaseOrder, Product, OrderLine, OrderLineSummary])
    table = schema["orderlinesummary"]
    col_names = [c.name for c in table.columns]
    # FK to OrderLine which has composite PK (order_id, product_id)
    assert "order_line_order_id" in col_names
    assert "order_line_product_id" in col_names


def test_insert_composite_fk_relation_based_pk() -> None:
    """Saving an OrderLineSummary with FK to relation-based-PK entity covers
    the 'elif fname in relations' branch of _derive_composite_fk_cols (entity.py)
    and also the __set__ relation-PK branch (vals.append(value.__dict__.get(...))).
    """
    db = Database(entities=[PurchaseOrder, Product, OrderLine, OrderLineSummary])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)

    with db_session:
        o = PurchaseOrder(ref="ORD-S")
        p = Product(sku="SKU-S")
        flush()
        line = OrderLine(quantity=2)
        line.order = o
        line.product = p
        flush()

        summary = OrderLineSummary(comment="good")
        summary.order_line = line  # covers __set__ with relation-based composite PK

    rows = db.select(OrderLineSummary).fetch_all()
    assert len(rows) == 1
    assert rows[0].comment == "good"
    db.close()


# ---------------------------------------------------------------------------
# Async composite FK tests
# ---------------------------------------------------------------------------


class AsyncEnrollment(Entity):
    student_id: Req[int]
    course_id: Req[int]
    _pk_ = PrimaryKey("student_id", "course_id")


class AsyncGradeNote(Entity):
    enrollment: Single[AsyncEnrollment]
    note: Opt[str]


@pytest.mark.asyncio
async def test_async_insert_composite_fk_entity() -> None:
    """Async insert of an entity with composite FK writes both FK columns."""
    async with AsyncDatabase(entities=[AsyncEnrollment, AsyncGradeNote]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        e = AsyncEnrollment(student_id=11, course_id=110)
        await db.asave(e)

        gn = AsyncGradeNote(note="async note")
        gn.enrollment = e
        await db.asave(gn)

        rows = await db.aselect(AsyncGradeNote).fetch_all()
        assert len(rows) == 1
        assert rows[0].note == "async note"
        assert rows[0].__dict__.get("_enrollment_id") == (11, 110)


@pytest.mark.asyncio
async def test_async_update_composite_fk_entity() -> None:
    """Async update of an entity with composite FK updates FK-tracking columns."""
    async with AsyncDatabase(entities=[AsyncEnrollment, AsyncGradeNote]) as db:
        await db.bind("sqlite", ":memory:")
        await db.generate_mapping(create_tables=True)

        e = AsyncEnrollment(student_id=22, course_id=220)
        await db.asave(e)
        gn = AsyncGradeNote(note="before")
        gn.enrollment = e
        await db.asave(gn)

        pk = gn.id
        loaded = await db.aselect(AsyncGradeNote).filter(AsyncGradeNote.id == pk).fetch_one()
        assert loaded is not None
        loaded.note = "after"
        await db.asave(loaded)

        result = await db.aselect(AsyncGradeNote).filter(AsyncGradeNote.id == pk).fetch_one()
        assert result is not None
        assert result.note == "after"
