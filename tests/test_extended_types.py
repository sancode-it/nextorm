"""Tests for extended field types: timedelta, DateTimeTz, Enum, Vec, and related FieldSpec/DDL."""

import datetime
import enum

from nextorm import Database, DateTimeTz, Entity, FieldSpec, Req, Vec
from nextorm.fields import _serialize_value
from nextorm.schema import Column, SQLiteRenderer, entity_to_table
from nextorm.schema.ddl import MariaDBRenderer, PostgresRenderer
from nextorm.session import db_session

# ---------------------------------------------------------------------------
# Sample enum types used across tests
# ---------------------------------------------------------------------------


class Status(enum.Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    PENDING = "pending"


class Priority(enum.Enum):
    LOW = 1
    HIGH = 2


# ---------------------------------------------------------------------------
# _serialize_value helper
# ---------------------------------------------------------------------------


class TestSerializeValue:
    def test_enum_str_value(self) -> None:
        assert _serialize_value(Status.ACTIVE) == "active"

    def test_enum_int_value(self) -> None:
        assert _serialize_value(Priority.HIGH) == 2

    def test_non_enum_passthrough(self) -> None:
        assert _serialize_value(42) == 42

    def test_none_passthrough(self) -> None:
        assert _serialize_value(None) is None

    def test_string_passthrough(self) -> None:
        assert _serialize_value("hello") == "hello"

    def test_timedelta_passthrough(self) -> None:
        # timedelta is handled by the sqlite3 adapter, not _serialize_value
        td = datetime.timedelta(seconds=5)
        assert _serialize_value(td) is td


# ---------------------------------------------------------------------------
# DDL: datetime.timedelta
# ---------------------------------------------------------------------------


class TestTimedeltaDDL:
    sqlite = SQLiteRenderer()
    postgres = PostgresRenderer()
    mariadb = MariaDBRenderer()

    def test_sqlite(self) -> None:
        assert self.sqlite.sql_type(Column("dur", datetime.timedelta)) == "INTEGER"

    def test_postgres(self) -> None:
        assert self.postgres.sql_type(Column("dur", datetime.timedelta)) == "INTERVAL"

    def test_mariadb(self) -> None:
        assert self.mariadb.sql_type(Column("dur", datetime.timedelta)) == "BIGINT"


# ---------------------------------------------------------------------------
# DDL: DateTimeTz sentinel
# ---------------------------------------------------------------------------


class TestDateTimeTzDDL:
    sqlite = SQLiteRenderer()
    postgres = PostgresRenderer()
    mariadb = MariaDBRenderer()

    def test_sqlite(self) -> None:
        assert self.sqlite.sql_type(Column("ts", DateTimeTz)) == "TEXT"

    def test_postgres(self) -> None:
        assert self.postgres.sql_type(Column("ts", DateTimeTz)) == "TIMESTAMPTZ"

    def test_mariadb(self) -> None:
        assert self.mariadb.sql_type(Column("ts", DateTimeTz)) == "DATETIME"


# ---------------------------------------------------------------------------
# DDL: enum.Enum subclass fields
# ---------------------------------------------------------------------------


class TestEnumDDL:
    sqlite = SQLiteRenderer()
    postgres = PostgresRenderer()
    mariadb = MariaDBRenderer()

    def test_sqlite_enum_is_text(self) -> None:
        assert self.sqlite.sql_type(Column("s", Status)) == "TEXT"

    def test_postgres_enum_check_constraint(self) -> None:
        sql = self.postgres.sql_type(Column("s", Status))
        assert "TEXT CHECK" in sql
        assert "'active'" in sql
        assert "'inactive'" in sql
        assert "'pending'" in sql

    def test_mariadb_enum_values(self) -> None:
        sql = self.mariadb.sql_type(Column("s", Status))
        assert "ENUM" in sql
        assert "'active'" in sql
        assert "'inactive'" in sql
        assert "'pending'" in sql

    def test_postgres_int_enum_check_constraint(self) -> None:
        sql = self.postgres.sql_type(Column("p", Priority))
        assert "TEXT CHECK" in sql
        assert "'1'" in sql
        assert "'2'" in sql

    def test_mariadb_int_enum(self) -> None:
        sql = self.mariadb.sql_type(Column("p", Priority))
        assert "ENUM" in sql
        assert "'1'" in sql or "'2'" in sql


# ---------------------------------------------------------------------------
# DDL: Vec[n] sentinel
# ---------------------------------------------------------------------------


class TestVecDDL:
    sqlite = SQLiteRenderer()
    postgres = PostgresRenderer()
    mariadb = MariaDBRenderer()

    def test_sqlite_vec_with_dims_is_text(self) -> None:
        # SQLite has no vector type — always TEXT
        assert self.sqlite.sql_type(Column("emb", Vec, dimensions=384)) == "TEXT"

    def test_sqlite_vec_no_dims_is_text(self) -> None:
        assert self.sqlite.sql_type(Column("emb", Vec)) == "TEXT"

    def test_postgres_vec_with_dims(self) -> None:
        assert self.postgres.sql_type(Column("emb", Vec, dimensions=1536)) == "vector(1536)"

    def test_postgres_vec_no_dims_fallback(self) -> None:
        assert self.postgres.sql_type(Column("emb", Vec)) == "TEXT"

    def test_mariadb_vec_with_dims(self) -> None:
        assert self.mariadb.sql_type(Column("emb", Vec, dimensions=512)) == "VECTOR(512)"

    def test_mariadb_vec_no_dims_fallback(self) -> None:
        assert self.mariadb.sql_type(Column("emb", Vec)) == "TEXT"


# ---------------------------------------------------------------------------
# Vec parameterised sentinel  (Vec[n] creates a subclass)
# ---------------------------------------------------------------------------


class TestVecParameterized:
    def test_vec_subscript_returns_subclass(self) -> None:
        V384 = Vec[384]  # type: ignore[type-arg, valid-type]
        assert issubclass(V384, Vec)
        assert V384._dimensions_ == 384

    def test_vec_subscript_different_dimensions(self) -> None:
        assert Vec[128]._dimensions_ == 128  # type: ignore[misc]
        assert Vec[768]._dimensions_ == 768  # type: ignore[misc]

    def test_vec_base_has_none_dimensions(self) -> None:
        assert Vec._dimensions_ is None


# ---------------------------------------------------------------------------
# Vec entity classes at module scope (inline Vec[n] annotations need ignore)
# ---------------------------------------------------------------------------


class _NullDimsVec(Vec):
    """Vec subclass with _dimensions_ = None — exercises the dims-is-None branch."""

    _dimensions_: int | None = None


class _VecArticle384(Entity):
    embedding: Req[Vec[384]]  # type: ignore[type-arg, valid-type]


class _VecArticle512(Entity):
    embedding: Req[Vec[512]]  # type: ignore[type-arg, valid-type]


class _VecArticleFieldSpec(Entity):
    embedding: Req[Vec] = Req(dimensions=1536)


class _VecArticleUnsubscripted(Entity):
    embedding: Req[Vec]


class _VecArticleNullDims(Entity):
    embedding: Req[_NullDimsVec]


# ---------------------------------------------------------------------------
# entity_to_table: Vec[n] annotation → dimensions wired through to Column
# ---------------------------------------------------------------------------


class TestVecEntityMapping:
    def test_vec_subscript_dims_in_column(self) -> None:
        table = entity_to_table(_VecArticle384)
        col = table.get_column("embedding")
        assert col is not None
        assert col.py_type is Vec
        assert col.dimensions == 384

    def test_vec_fieldspec_dims_in_column(self) -> None:
        table = entity_to_table(_VecArticleFieldSpec)
        col = table.get_column("embedding")
        assert col is not None
        assert col.dimensions == 1536

    def test_vec_in_ddl_via_entity(self) -> None:
        table = entity_to_table(_VecArticle512)
        sql = PostgresRenderer().create_table(table)
        assert "vector(512)" in sql

    def test_vec_unsubscripted_has_no_dimensions(self) -> None:
        # Req[Vec] without subscript — dimensions remain None
        table = entity_to_table(_VecArticleUnsubscripted)
        col = table.get_column("embedding")
        assert col is not None
        assert col.py_type is Vec
        assert col.dimensions is None

    def test_vec_subclass_with_none_dimensions(self) -> None:
        # A Vec subclass whose _dimensions_ is explicitly None covers the
        # `dims is None` branch inside the elif, keeping spec unchanged.
        table = entity_to_table(_VecArticleNullDims)
        col = table.get_column("embedding")
        assert col is not None
        assert col.py_type is Vec
        assert col.dimensions is None


# ---------------------------------------------------------------------------
# entity_to_table: dimensions default in Column
# ---------------------------------------------------------------------------


class TestColumnDimensionsDefault:
    def test_column_dimensions_default_none(self) -> None:
        col = Column("x", int)
        assert col.dimensions is None


# ---------------------------------------------------------------------------
# FieldSpec defaults for new fields
# ---------------------------------------------------------------------------


class TestFieldSpecNewExtendedDefaults:
    def test_dimensions_default_none(self) -> None:
        assert FieldSpec().dimensions is None


# ---------------------------------------------------------------------------
# Enum entity: write serialization (INSERT/UPDATE)
# ---------------------------------------------------------------------------


class TaskStatus(enum.Enum):
    TODO = "todo"
    DONE = "done"


# Vec subclass with _dimensions_=None — used to test the dims-is-None branch
# (defined as a proper class above the entity that uses it)


class TaskEntity(Entity):
    title: Req[str]
    status: Req[TaskStatus]


def _make_enum_db() -> Database:
    db = Database(entities=[TaskEntity])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    return db


class TestEnumEntityInsert:
    def test_enum_value_serialised_on_save(self) -> None:
        db = _make_enum_db()
        with db_session:
            task = TaskEntity(title="write tests", status=TaskStatus.TODO)
        # Read back via raw SQL to check what was actually stored
        rows = db.select_raw("SELECT status FROM taskentity WHERE id = ?", task.id)
        assert rows[0]["status"] == "todo"
        db.close()

    def test_enum_value_serialised_on_update(self) -> None:
        db = _make_enum_db()
        with db_session:
            task = TaskEntity(title="review", status=TaskStatus.TODO)
            task.status = TaskStatus.DONE
        rows = db.select_raw("SELECT status FROM taskentity WHERE id = ?", task.id)
        assert rows[0]["status"] == "done"
        db.close()

    def test_enum_read_back_from_db(self) -> None:
        """Enum fields are coerced from raw DB values (str) back to the Enum type on fetch."""
        db = _make_enum_db()
        with db_session:
            task = TaskEntity(title="read-back", status=TaskStatus.TODO)
        # Fetch via ORM — __set__ receives the raw "todo" string and must coerce it
        loaded = db.select(TaskEntity).filter(TaskEntity.id == task.id).fetch_one()
        assert loaded is not None
        assert isinstance(loaded.status, TaskStatus)
        assert loaded.status is TaskStatus.TODO
        db.close()

    def test_enum_coercion_from_different_enum_type(self) -> None:
        """Setting an enum field to a different Enum instance coerces via .value."""

        class AltStatus(enum.Enum):
            TODO = "todo"

        db = _make_enum_db()
        with db_session:
            task = TaskEntity(title="coerce", status=TaskStatus.DONE)
            # Assign a different Enum subclass whose .value matches a TaskStatus member
        task.status = AltStatus.TODO
        assert task.status is TaskStatus.TODO
        db.close()


# ---------------------------------------------------------------------------
# timedelta SQLite adapter
# ---------------------------------------------------------------------------


class DurationEntity(Entity):
    label: Req[str]
    duration: Req[datetime.timedelta]


def _make_timedelta_db() -> Database:
    db = Database(entities=[DurationEntity])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    return db


class TestTimedeltaSqliteAdapter:
    def test_timedelta_stored_as_microseconds(self) -> None:
        db = _make_timedelta_db()
        td = datetime.timedelta(seconds=3, microseconds=500_000)
        with db_session:
            obj = DurationEntity(label="test", duration=td)
        rows = db.select_raw("SELECT duration FROM durationentity WHERE id = ?", obj.id)
        stored = rows[0]["duration"]
        assert stored == int(td.total_seconds() * 1_000_000)
        db.close()

    def test_timedelta_zero(self) -> None:
        db = _make_timedelta_db()
        with db_session:
            obj = DurationEntity(label="zero", duration=datetime.timedelta(0))
        rows = db.select_raw("SELECT duration FROM durationentity WHERE id = ?", obj.id)
        assert rows[0]["duration"] == 0
        db.close()


# ---------------------------------------------------------------------------
# DateTimeTz entity: DDL via entity mapping
# ---------------------------------------------------------------------------


class EventEntity(Entity):
    name: Req[str]
    starts_at: Req[DateTimeTz]


class TestDateTimeTzEntityMapping:
    def test_datetimetz_column_sqlite_ddl(self) -> None:
        table = entity_to_table(EventEntity)
        col = table.get_column("starts_at")
        assert col is not None
        assert col.py_type is DateTimeTz
        sql = SQLiteRenderer().create_table(table)
        assert '"starts_at" TEXT NOT NULL' in sql

    def test_datetimetz_column_postgres_ddl(self) -> None:
        sql = PostgresRenderer().create_table(entity_to_table(EventEntity))
        assert "TIMESTAMPTZ" in sql

    def test_datetimetz_column_mariadb_ddl(self) -> None:
        sql = MariaDBRenderer().create_table(entity_to_table(EventEntity))
        assert "DATETIME" in sql


# ---------------------------------------------------------------------------
# LongStr — lazy default behaviour
# ---------------------------------------------------------------------------

from nextorm.fields import LongStr as _LongStr  # noqa: E402


class _LongStrBlogPost(Entity):
    title: Req[str]
    body: Req[_LongStr]


class _LongStrBlogPostEager(Entity):
    title: Req[str]
    body: Req[_LongStr] = Req(lazy=False)


class TestLongStrLazy:
    def test_longstr_auto_lazy_field_spec(self) -> None:
        """LongStr field with no explicit FieldSpec gets lazy=True in EntityMeta."""
        fi = _LongStrBlogPost._fields_["body"]
        assert fi.spec.lazy is True

    def test_longstr_explicit_fieldspec_overrides_lazy(self) -> None:
        """Providing FieldSpec(lazy=False) opts the user out of auto-lazy."""
        fi = _LongStrBlogPostEager._fields_["body"]
        assert fi.spec.lazy is False

    def test_longstr_column_still_in_schema_when_lazy(self) -> None:
        """Lazy LongStr fields still appear as table columns in the DDL."""
        table = entity_to_table(_LongStrBlogPost)
        col = table.get_column("body")
        assert col is not None
        assert col.py_type is _LongStr

    def test_longstr_lazy_field_excluded_from_select(self) -> None:
        """QuerySet for a LongStr entity uses explicit columns, omitting the lazy body."""
        from nextorm.database import Database  # noqa: PLC0415
        from nextorm.sql.nodes import ColumnRef, Star  # noqa: PLC0415

        db = Database(entities=[_LongStrBlogPost])
        db.bind("sqlite", ":memory:")
        db.generate_mapping(create_tables=True)

        qs = db.select(_LongStrBlogPost)
        select_node = qs._build_select()
        assert not any(isinstance(c, Star) for c in select_node.columns)
        selected_names = {c.column for c in select_node.columns if isinstance(c, ColumnRef)}
        assert "body" not in selected_names
        db.close()

    def test_longstr_eager_field_uses_star_select(self) -> None:
        """When all LongStr fields have lazy=False, SELECT * is used."""
        from nextorm.database import Database  # noqa: PLC0415
        from nextorm.sql.nodes import Star  # noqa: PLC0415

        db = Database(entities=[_LongStrBlogPostEager])
        db.bind("sqlite", ":memory:")
        db.generate_mapping(create_tables=True)

        qs = db.select(_LongStrBlogPostEager)
        select_node = qs._build_select()
        assert any(isinstance(c, Star) for c in select_node.columns)
        db.close()
