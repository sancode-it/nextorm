"""Tests for Database.migrate() and SQLite schema introspection.

All tests use in-memory SQLite databases via the ``db`` fixture or inline
``Database`` construction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from nextorm.database import Database
from nextorm.entity import Entity
from nextorm.fields import PK, Opt, Req
from nextorm.schema.core import Column, Index
from nextorm.schema.introspect import introspect_sqlite

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

# ---------------------------------------------------------------------------
# Module-level entity definitions
# ---------------------------------------------------------------------------


class BlogPost(Entity):
    id: PK[int]
    title: Req[str]
    slug: Req[str]
    body: Opt[str]


class Author(Entity):
    id: PK[int]
    name: Req[str]
    email: Req[str]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def empty_db() -> Generator[Database, None, None]:
    """Bound DB with no tables created yet."""
    db = Database(entities=[BlogPost])
    db.bind("sqlite", ":memory:")
    yield db
    db.close()


@pytest.fixture
def mapped_db() -> Generator[Database, None, None]:
    """Bound DB with BlogPost table created."""
    db = Database(entities=[BlogPost])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    yield db
    db.close()


# ---------------------------------------------------------------------------
# introspect_sqlite — tables, columns, nullability, PKs
# ---------------------------------------------------------------------------


def test_introspect_empty_db(empty_db: Database) -> None:
    """Freshly bound DB with no tables returns an empty schema."""
    schema = introspect_sqlite(empty_db._ensure_connection())
    assert schema == {}


def test_introspect_returns_created_table(mapped_db: Database) -> None:
    schema = introspect_sqlite(mapped_db._ensure_connection())
    assert "blogpost" in schema


def test_introspect_all_columns_present(mapped_db: Database) -> None:
    schema = introspect_sqlite(mapped_db._ensure_connection())
    col_names = {c.name for c in schema["blogpost"].columns}
    assert {"id", "title", "slug", "body"} <= col_names


def test_introspect_nullable_column(mapped_db: Database) -> None:
    schema = introspect_sqlite(mapped_db._ensure_connection())
    col_map = {c.name: c for c in schema["blogpost"].columns}
    assert col_map["body"].nullable is True


def test_introspect_not_null_column(mapped_db: Database) -> None:
    schema = introspect_sqlite(mapped_db._ensure_connection())
    col_map = {c.name: c for c in schema["blogpost"].columns}
    assert col_map["title"].nullable is False


def test_introspect_primary_key_detected(mapped_db: Database) -> None:
    schema = introspect_sqlite(mapped_db._ensure_connection())
    pk_cols = [c for c in schema["blogpost"].columns if c.primary_key]
    assert len(pk_cols) == 1
    assert pk_cols[0].name == "id"


def test_introspect_non_pk_column_not_primary_key(mapped_db: Database) -> None:
    schema = introspect_sqlite(mapped_db._ensure_connection())
    col_map = {c.name: c for c in schema["blogpost"].columns}
    assert col_map["title"].primary_key is False


# ---------------------------------------------------------------------------
# introspect_sqlite — indexes
# ---------------------------------------------------------------------------


def _create_indexed_table(db: Database) -> None:
    """Helper: create a table with one explicit index and one UNIQUE column."""
    db._execute_dml(
        "CREATE TABLE indexed_tbl (id INTEGER PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT)",
        [],
    )
    db._execute_dml("CREATE INDEX idx_indexed_tbl__name ON indexed_tbl (name)", [])


def test_introspect_explicit_index_included() -> None:
    """Explicitly-created indexes (origin='c') appear in the result."""
    db = Database(entities=[])
    db.bind("sqlite", ":memory:")
    _create_indexed_table(db)
    schema = introspect_sqlite(db._ensure_connection())
    tbl = schema["indexed_tbl"]
    idx_names = {i.name for i in tbl.indexes}
    assert "idx_indexed_tbl__name" in idx_names
    db.close()


def test_introspect_implicit_unique_index_excluded() -> None:
    """Implicit UNIQUE-constraint indexes (origin='u') are skipped."""
    db = Database(entities=[])
    db.bind("sqlite", ":memory:")
    _create_indexed_table(db)
    schema = introspect_sqlite(db._ensure_connection())
    tbl = schema["indexed_tbl"]
    # The implicit UNIQUE index on 'slug' should not appear
    for idx in tbl.indexes:
        assert "slug" not in idx.columns, (
            f"Implicit UNIQUE index on slug should be skipped; got {idx}"
        )
    db.close()


def test_introspect_no_explicit_indexes_returns_empty(mapped_db: Database) -> None:
    """A table with no explicit indexes has an empty indexes list."""
    schema = introspect_sqlite(mapped_db._ensure_connection())
    assert schema["blogpost"].indexes == []


def test_introspect_index_columns_correct() -> None:
    """Index column list is correctly read from PRAGMA index_info."""
    db = Database(entities=[])
    db.bind("sqlite", ":memory:")
    _create_indexed_table(db)
    schema = introspect_sqlite(db._ensure_connection())
    idx = next(i for i in schema["indexed_tbl"].indexes if i.name == "idx_indexed_tbl__name")
    assert idx.columns == ["name"]
    db.close()


# ---------------------------------------------------------------------------
# Database.migrate() — table creation and idempotency
# ---------------------------------------------------------------------------


def test_migrate_creates_missing_tables() -> None:
    """migrate() issues CREATE TABLE when tables are absent."""
    db = Database(entities=[BlogPost])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=False)  # schema built but NO tables created

    schema_before = introspect_sqlite(db._ensure_connection())
    assert "blogpost" not in schema_before

    stmts = db.migrate()

    assert len(stmts) > 0
    schema_after = introspect_sqlite(db._ensure_connection())
    assert "blogpost" in schema_after
    db.close()


def test_migrate_returns_sql_statements() -> None:
    """migrate() returns the list of executed DDL strings."""
    db = Database(entities=[BlogPost])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=False)
    stmts = db.migrate()
    assert all(isinstance(s, str) for s in stmts)
    assert any("blogpost" in s.lower() for s in stmts)
    db.close()


def test_migrate_is_idempotent() -> None:
    """Calling migrate() after create_tables=True returns an empty list."""
    db = Database(entities=[BlogPost])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    stmts = db.migrate()
    assert stmts == []
    db.close()


def test_migrate_idempotent_after_migrate() -> None:
    """Calling migrate() twice in a row yields empty list on second call."""
    db = Database(entities=[BlogPost])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=False)
    db.migrate()
    stmts = db.migrate()
    assert stmts == []
    db.close()


# ---------------------------------------------------------------------------
# Database.migrate() — column changes
# ---------------------------------------------------------------------------


def test_migrate_adds_new_column() -> None:
    """migrate() issues ALTER TABLE ADD COLUMN for a new column."""
    db = Database(entities=[BlogPost])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)

    # Simulate adding a 'views' column to the entity schema after initial deploy
    db._schema["blogpost"].columns.append(
        Column(name="views", py_type=int, nullable=False, sql_default="0")
    )

    stmts = db.migrate()

    assert any("views" in s for s in stmts)
    schema = introspect_sqlite(db._ensure_connection())
    col_names = {c.name for c in schema["blogpost"].columns}
    assert "views" in col_names
    db.close()


def test_migrate_drops_removed_column() -> None:
    """migrate() issues ALTER TABLE DROP COLUMN for a removed column."""
    db = Database(entities=[BlogPost])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)

    # Simulate removing the 'slug' column from the entity schema
    db._schema["blogpost"].columns = [c for c in db._schema["blogpost"].columns if c.name != "slug"]

    stmts = db.migrate()

    assert any("slug" in s for s in stmts)
    schema = introspect_sqlite(db._ensure_connection())
    col_names = {c.name for c in schema["blogpost"].columns}
    assert "slug" not in col_names
    db.close()


# ---------------------------------------------------------------------------
# Database.migrate() — table removal
# ---------------------------------------------------------------------------


def test_migrate_drops_removed_table() -> None:
    """migrate() issues DROP TABLE for tables no longer in the schema."""
    db = Database(entities=[BlogPost, Author])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)

    # Simulate Author entity being removed from the project
    del db._schema["author"]

    stmts = db.migrate()

    assert any("author" in s.lower() for s in stmts)
    schema = introspect_sqlite(db._ensure_connection())
    assert "author" not in schema
    db.close()


# ---------------------------------------------------------------------------
# Database.migrate() — index changes
# ---------------------------------------------------------------------------


def test_migrate_adds_missing_index() -> None:
    """migrate() creates indexes that exist in the target schema but not in the DB."""
    db = Database(entities=[BlogPost])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)

    # Add an explicit index to the target schema
    db._schema["blogpost"].indexes.append(Index(name="idx_blogpost__slug", columns=["slug"]))

    stmts = db.migrate()

    assert any("idx_blogpost__slug" in s for s in stmts)
    schema = introspect_sqlite(db._ensure_connection())
    idx_names = {i.name for i in schema["blogpost"].indexes}
    assert "idx_blogpost__slug" in idx_names
    db.close()


def test_migrate_drops_removed_index() -> None:
    """migrate() drops indexes that exist in the DB but not in the target schema."""
    db = Database(entities=[BlogPost])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)

    # Create a stray index in the DB that is NOT in the entity schema
    db._execute_dml("CREATE INDEX idx_blogpost__slug ON blogpost (slug)", [])

    stmts = db.migrate()

    assert any("idx_blogpost__slug" in s for s in stmts)
    schema = introspect_sqlite(db._ensure_connection())
    idx_names = {i.name for i in schema["blogpost"].indexes}
    assert "idx_blogpost__slug" not in idx_names
    db.close()


# ---------------------------------------------------------------------------
# Database.migrate() — error cases
# ---------------------------------------------------------------------------


def test_migrate_raises_if_not_bound() -> None:
    db = Database(entities=[BlogPost])
    with pytest.raises(RuntimeError, match="not bound"):
        db.migrate()


def test_migrate_raises_without_generate_mapping() -> None:
    db = Database(entities=[BlogPost])
    db.bind("sqlite", ":memory:")
    with pytest.raises(RuntimeError, match="generate_mapping"):
        db.migrate()
    db.close()


# ---------------------------------------------------------------------------
# showmigrations
# ---------------------------------------------------------------------------


def test_showmigrations_empty_directory(tmp_path: Path) -> None:
    """Non-existent directory returns an empty list."""
    from nextorm.migrations import showmigrations  # noqa: PLC0415

    db = Database(entities=[BlogPost])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    result = showmigrations(db, directory=tmp_path / "no_such_dir")
    assert result == []
    db.close()


def test_showmigrations_no_files(tmp_path: Path) -> None:
    """Directory with no migration files returns an empty list."""
    from nextorm.migrations import showmigrations  # noqa: PLC0415

    db = Database(entities=[BlogPost])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    result = showmigrations(db, directory=tmp_path)
    assert result == []
    db.close()


def test_showmigrations_all_pending(tmp_path: Path) -> None:
    """Migration files that have never been applied show applied=False."""
    from nextorm.migrations import MigrationStatus, showmigrations  # noqa: PLC0415

    # Write two stub migration files
    (tmp_path / "0001_initial.py").write_text("def upgrade(db): pass\n")
    (tmp_path / "0002_add_slug.py").write_text("def upgrade(db): pass\n")

    db = Database(entities=[BlogPost])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    result = showmigrations(db, directory=tmp_path)

    assert len(result) == 2
    assert all(isinstance(s, MigrationStatus) for s in result)
    assert result[0].name == "0001_initial.py"
    assert result[0].version == "0001_initial"
    assert result[0].applied is False
    assert result[0].applied_at == ""
    assert result[1].name == "0002_add_slug.py"
    assert result[1].applied is False
    db.close()


def test_showmigrations_after_migrate(tmp_path: Path) -> None:
    """Applied migrations are marked applied=True with a non-empty applied_at."""
    from nextorm.migrations import migrate, showmigrations  # noqa: PLC0415

    (tmp_path / "0001_initial.py").write_text("def upgrade(db): pass\n")
    (tmp_path / "0002_add_slug.py").write_text("def upgrade(db): pass\n")

    db = Database(entities=[BlogPost])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    migrate(db, directory=tmp_path)
    result = showmigrations(db, directory=tmp_path)

    assert len(result) == 2
    assert result[0].applied is True
    assert result[0].applied_at != ""
    assert result[1].applied is True
    db.close()


def test_showmigrations_partial(tmp_path: Path) -> None:
    """Only applied migrations are marked applied=True; pending ones are False."""
    from nextorm.migrations import migrate, showmigrations  # noqa: PLC0415

    (tmp_path / "0001_initial.py").write_text("def upgrade(db): pass\n")
    (tmp_path / "0002_pending.py").write_text("def upgrade(db): pass\n")

    db = Database(entities=[BlogPost])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    # Apply only the first migration by faking the second
    migrate(db, directory=tmp_path, fake=False)
    # Undo second by removing its tracking record isn't feasible, so instead
    # use a fresh DB so only one is applied
    db.close()

    db2 = Database(entities=[BlogPost])
    db2.bind("sqlite", ":memory:")
    db2.generate_mapping(create_tables=True)
    # Apply just first
    from nextorm.migrations.core import _ensure_tracking_table, _record_version  # noqa: PLC0415

    _ensure_tracking_table(db2)
    _record_version(db2, "0001_initial")

    result = showmigrations(db2, directory=tmp_path)
    assert result[0].applied is True
    assert result[1].applied is False
    db2.close()


def test_showmigrations_sorted_order(tmp_path: Path) -> None:
    """Migration files are returned in ascending version order."""
    from nextorm.migrations import showmigrations  # noqa: PLC0415

    (tmp_path / "0003_third.py").write_text("def upgrade(db): pass\n")
    (tmp_path / "0001_first.py").write_text("def upgrade(db): pass\n")
    (tmp_path / "0002_second.py").write_text("def upgrade(db): pass\n")

    db = Database(entities=[BlogPost])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    result = showmigrations(db, directory=tmp_path)

    assert [s.name for s in result] == ["0001_first.py", "0002_second.py", "0003_third.py"]
    db.close()


def test_migrationstatus_repr_pending(tmp_path: Path) -> None:
    """Pending status renders as '[ ] filename'."""
    from nextorm.migrations import showmigrations  # noqa: PLC0415

    (tmp_path / "0001_initial.py").write_text("def upgrade(db): pass\n")

    db = Database(entities=[BlogPost])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    result = showmigrations(db, directory=tmp_path)

    assert repr(result[0]) == "[ ] 0001_initial.py"
    db.close()


def test_migrationstatus_repr_applied(tmp_path: Path) -> None:
    """Applied status renders as '[x] filename  (timestamp)'."""
    from nextorm.migrations import migrate, showmigrations  # noqa: PLC0415

    (tmp_path / "0001_initial.py").write_text("def upgrade(db): pass\n")

    db = Database(entities=[BlogPost])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    migrate(db, directory=tmp_path)
    result = showmigrations(db, directory=tmp_path)

    r = repr(result[0])
    assert r.startswith("[x] 0001_initial.py")
    assert "(" in r  # timestamp present
    db.close()


def test_migrationrunner_showmigrations(tmp_path: Path) -> None:
    """MigrationRunner.showmigrations() delegates correctly."""
    from nextorm.migrations import MigrationRunner  # noqa: PLC0415

    (tmp_path / "0001_initial.py").write_text("def upgrade(db): pass\n")

    db = Database(entities=[BlogPost])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    runner = MigrationRunner(db, directory=tmp_path)

    result = runner.showmigrations()
    assert len(result) == 1
    assert result[0].applied is False

    runner.migrate()
    result = runner.showmigrations()
    assert result[0].applied is True
    db.close()


def test_showmigrations_cli_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI 'showmigrations' command prints each migration status."""
    import sys  # noqa: PLC0415

    from nextorm.migrations.cli import main  # noqa: PLC0415

    (tmp_path / "0001_initial.py").write_text("def upgrade(db): pass\n")

    # Write a tiny module that exposes a ready-to-use Database
    mod_path = tmp_path / "_testmod_show.py"
    mod_path.write_text(
        "from nextorm.database import Database\n"
        "from nextorm.entity import Entity\n"
        "from nextorm.fields import Req\n"
        "class Item(Entity):\n"
        "    name: Req[str]\n"
        "db = Database(entities=[Item])\n"
        "db.bind('sqlite', ':memory:')\n"
        "db.generate_mapping(create_tables=True)\n"
    )
    sys.path.insert(0, str(tmp_path))
    try:
        main(
            [
                "showmigrations",
                "--module",
                "_testmod_show",
                "--directory",
                str(tmp_path),
            ]
        )
    finally:
        sys.path.pop(0)
        sys.modules.pop("_testmod_show", None)

    out = capsys.readouterr().out
    assert "0001_initial.py" in out
    assert "[ ]" in out


# ---------------------------------------------------------------------------
# makemigrations() — file-based migration generation
# ---------------------------------------------------------------------------


def test_makemigrations_creates_migration_file(tmp_path: Path) -> None:
    """makemigrations() writes a migration file when there is no snapshot yet."""
    from nextorm.migrations import makemigrations  # noqa: PLC0415

    db = Database(entities=[BlogPost])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    path = makemigrations(db, name="initial", directory=tmp_path)
    db.close()

    assert path is not None
    assert path.exists()
    assert path.name.startswith("0001_")
    content = path.read_text()
    assert "def upgrade" in content
    assert "def downgrade" in content


def test_makemigrations_no_changes_returns_none(tmp_path: Path) -> None:
    """After saving a snapshot, a second makemigrations() call returns None."""
    from nextorm.migrations import makemigrations  # noqa: PLC0415

    db = Database(entities=[BlogPost])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    makemigrations(db, name="initial", directory=tmp_path)
    result = makemigrations(db, name="second", directory=tmp_path)
    db.close()

    assert result is None


def test_makemigrations_sequential_numbering(tmp_path: Path) -> None:
    """Successive makemigrations() calls produce 0001_, 0002_, ... files."""
    from nextorm.migrations import makemigrations  # noqa: PLC0415

    db = Database(entities=[BlogPost])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    p1 = makemigrations(db, name="first", directory=tmp_path)
    db.close()

    # Second DB adds an Author entity — new table → diff detected
    db2 = Database(entities=[BlogPost, Author])
    db2.bind("sqlite", ":memory:")
    db2.generate_mapping(create_tables=True)
    p2 = makemigrations(db2, name="second", directory=tmp_path)
    db2.close()

    assert p1 is not None and p1.name.startswith("0001_")
    assert p2 is not None and p2.name.startswith("0002_")


def test_makemigrations_raises_without_generate_mapping(tmp_path: Path) -> None:
    """makemigrations() raises RuntimeError when generate_mapping() was skipped."""
    from nextorm.migrations import makemigrations  # noqa: PLC0415

    db = Database(entities=[BlogPost])
    db.bind("sqlite", ":memory:")
    # generate_mapping() not called → _schema is empty
    with pytest.raises(RuntimeError, match="generate_mapping"):
        makemigrations(db, name="fail", directory=tmp_path)
    db.close()


# ---------------------------------------------------------------------------
# migrate() — file-based migration application
# ---------------------------------------------------------------------------


def test_migrate_nonexistent_directory_returns_empty(tmp_path: Path) -> None:
    """migrate() returns an empty list when the directory does not exist."""
    from nextorm.migrations import migrate  # noqa: PLC0415

    db = Database(entities=[BlogPost])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    result = migrate(db, directory=tmp_path / "nonexistent")
    assert result == []
    db.close()


def test_migrate_applies_migration_and_records_version(tmp_path: Path) -> None:
    """migrate() runs upgrade() and records the version so it isn't re-applied."""
    from nextorm.migrations import makemigrations, migrate, showmigrations  # noqa: PLC0415

    db = Database(entities=[BlogPost])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    makemigrations(db, name="initial", directory=tmp_path)
    db.close()

    db2 = Database(entities=[BlogPost])
    db2.bind("sqlite", ":memory:")
    db2.generate_mapping(create_tables=True)

    applied = migrate(db2, directory=tmp_path)
    assert len(applied) == 1

    # Running again should not re-apply
    applied2 = migrate(db2, directory=tmp_path)
    assert applied2 == []

    statuses = showmigrations(db2, directory=tmp_path)
    assert statuses[0].applied is True
    db2.close()


def test_migrate_fake_records_without_running_upgrade(tmp_path: Path) -> None:
    """migrate(fake=True) records the version without calling upgrade()."""
    from nextorm.migrations import makemigrations, migrate  # noqa: PLC0415

    db = Database(entities=[BlogPost])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    makemigrations(db, name="initial", directory=tmp_path)
    db.close()

    db2 = Database(entities=[BlogPost])
    db2.bind("sqlite", ":memory:")
    db2.generate_mapping(create_tables=True)

    applied = migrate(db2, directory=tmp_path, fake=True)
    assert len(applied) == 1
    # Should not be re-applied on next call
    applied2 = migrate(db2, directory=tmp_path, fake=True)
    assert applied2 == []
    db2.close()


def test_migrationrunner_makemigrations_and_migrate(tmp_path: Path) -> None:
    """MigrationRunner.makemigrations() and .migrate() delegate correctly."""
    from nextorm.migrations import MigrationRunner  # noqa: PLC0415

    db = Database(entities=[BlogPost])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)

    runner = MigrationRunner(db, directory=tmp_path)
    path = runner.makemigrations(name="initial")
    assert path is not None

    applied = runner.migrate()
    assert len(applied) == 1

    applied2 = runner.migrate()
    assert applied2 == []
    db.close()


# ---------------------------------------------------------------------------
# CLI — missing coverage paths
# ---------------------------------------------------------------------------


def _write_test_module(path: Path, name: str) -> None:
    """Write a tiny self-contained module that sets up a Database named 'db'."""
    (path / f"{name}.py").write_text(
        "from nextorm.database import Database\n"
        "from nextorm.entity import Entity\n"
        "from nextorm.fields import Req\n"
        "\n"
        "class _CLIItem(Entity):\n"
        "    title: Req[str]\n"
        "\n"
        "db = Database(entities=[_CLIItem])\n"
        "db.bind('sqlite', ':memory:')\n"
        "db.generate_mapping(create_tables=True)\n"
    )


def test_load_db_missing_attr_exits(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """_load_db raises SystemExit when the attribute is absent from the module."""
    import sys  # noqa: PLC0415

    from nextorm.migrations.cli import _load_db  # noqa: PLC0415

    mod_name = "_testmod_missing_attr"
    _write_test_module(tmp_path, mod_name)
    sys.path.insert(0, str(tmp_path))
    try:
        with pytest.raises(SystemExit):
            _load_db(mod_name, "nonexistent_db_attr")
    finally:
        sys.path.pop(0)
        sys.modules.pop(mod_name, None)
    captured = capsys.readouterr()
    assert "ERROR" in captured.err


def test_makemigrations_cli_creates_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI makemigrations command prints 'Created migration:' on success."""
    import sys  # noqa: PLC0415

    from nextorm.migrations.cli import main  # noqa: PLC0415

    mod_name = "_testmod_mk_create"
    _write_test_module(tmp_path, mod_name)
    sys.path.insert(0, str(tmp_path))
    try:
        main(
            [
                "makemigrations",
                "--module",
                mod_name,
                "--directory",
                str(tmp_path),
                "--name",
                "initial",
            ]
        )
    finally:
        sys.path.pop(0)
        sys.modules.pop(mod_name, None)
    out = capsys.readouterr().out
    assert "Created migration:" in out


def test_makemigrations_cli_no_changes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI makemigrations prints 'No schema changes detected.' when snapshot is current."""
    import sys  # noqa: PLC0415

    from nextorm.migrations.cli import main  # noqa: PLC0415

    mod_name = "_testmod_mk_nochange"
    _write_test_module(tmp_path, mod_name)
    sys.path.insert(0, str(tmp_path))
    try:
        # First call creates migration
        main(["makemigrations", "--module", mod_name, "--directory", str(tmp_path)])
        sys.modules.pop(mod_name, None)
        # Re-import to get the same schema
        main(["makemigrations", "--module", mod_name, "--directory", str(tmp_path)])
    finally:
        sys.path.pop(0)
        sys.modules.pop(mod_name, None)
    out = capsys.readouterr().out
    assert "No schema changes detected." in out


def test_migrate_cli_applies(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI migrate command prints 'Applied:' for each applied migration."""
    import sys  # noqa: PLC0415

    from nextorm.migrations.cli import main  # noqa: PLC0415

    mod_name = "_testmod_migrate_apply"
    _write_test_module(tmp_path, mod_name)
    sys.path.insert(0, str(tmp_path))
    try:
        # Create a migration first
        main(["makemigrations", "--module", mod_name, "--directory", str(tmp_path)])
        sys.modules.pop(mod_name, None)
        # Now apply it
        main(["migrate", "--module", mod_name, "--directory", str(tmp_path)])
    finally:
        sys.path.pop(0)
        sys.modules.pop(mod_name, None)
    out = capsys.readouterr().out
    assert "Applied:" in out


def test_migrate_cli_nothing_to_migrate(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI migrate prints 'Nothing to migrate.' when no pending migrations exist."""
    import sys  # noqa: PLC0415

    from nextorm.migrations.cli import main  # noqa: PLC0415

    mod_name = "_testmod_migrate_nothing"
    _write_test_module(tmp_path, mod_name)
    sys.path.insert(0, str(tmp_path))
    try:
        main(["migrate", "--module", mod_name, "--directory", str(tmp_path)])
    finally:
        sys.path.pop(0)
        sys.modules.pop(mod_name, None)
    out = capsys.readouterr().out
    assert "Nothing to migrate." in out


def test_showmigrations_cli_no_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI showmigrations prints 'No migration files found.' on empty directory."""
    import sys  # noqa: PLC0415

    from nextorm.migrations.cli import main  # noqa: PLC0415

    mod_name = "_testmod_show_nofiles"
    _write_test_module(tmp_path, mod_name)
    sys.path.insert(0, str(tmp_path))
    try:
        main(
            [
                "showmigrations",
                "--module",
                mod_name,
                "--directory",
                str(tmp_path / "empty_dir"),
            ]
        )
    finally:
        sys.path.pop(0)
        sys.modules.pop(mod_name, None)
    out = capsys.readouterr().out
    assert "No migration files found." in out


def test_cli_main_module_block(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Executing the module as __main__ (the if __name__ == '__main__' block)."""
    import runpy  # noqa: PLC0415
    import sys  # noqa: PLC0415

    mod_name = "_testmod_mainblock"
    _write_test_module(tmp_path, mod_name)
    sys.path.insert(0, str(tmp_path))
    orig_argv = sys.argv[:]
    sys.argv = [
        "nextorm.migrations.cli",
        "showmigrations",
        "--module",
        mod_name,
        "--directory",
        str(tmp_path / "no_migs"),
    ]
    try:
        runpy.run_module("nextorm.migrations.cli", run_name="__main__", alter_sys=True)
    finally:
        sys.argv = orig_argv
        sys.path.pop(0)
        sys.modules.pop(mod_name, None)
    out = capsys.readouterr().out
    assert "No migration files found." in out


# ---------------------------------------------------------------------------
# _load_migration_module — spec is None edge case (line 154)
# ---------------------------------------------------------------------------


def test_load_migration_module_spec_none_raises(tmp_path: Path) -> None:
    """_load_migration_module raises ImportError when spec_from_file_location returns None."""
    from unittest.mock import patch  # noqa: PLC0415

    from nextorm.migrations.core import _load_migration_module  # noqa: PLC0415

    fake_path = tmp_path / "0001_fake.py"
    fake_path.write_text("def upgrade(db): pass\n")
    with (
        patch("importlib.util.spec_from_file_location", return_value=None),
        pytest.raises(ImportError, match="Cannot load migration"),
    ):
        _load_migration_module(fake_path)


# ---------------------------------------------------------------------------
# _generate_migration_body — empty ops list (line 174)
# ---------------------------------------------------------------------------


def test_generate_migration_body_empty_ops_produces_pass() -> None:
    """_generate_migration_body([]) emits 'pass  # no schema changes'."""
    from nextorm.migrations.core import _generate_migration_body  # noqa: PLC0415

    body = _generate_migration_body([])
    assert "pass  # no schema changes" in body


# ---------------------------------------------------------------------------
# migrate — migration file without upgrade function (line 336→338)
# ---------------------------------------------------------------------------


def test_migrate_skips_upgrade_when_not_defined(tmp_path: Path) -> None:
    """A migration file without an upgrade() function is still recorded (336→338)."""
    from nextorm.migrations import migrate  # noqa: PLC0415

    # Create a migration file with no upgrade function
    mig_file = tmp_path / "0001_no_upgrade.py"
    mig_file.write_text('"""No upgrade defined."""\n')

    db = Database(entities=[BlogPost])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)
    applied = migrate(db, directory=tmp_path)
    assert len(applied) == 1
    db.close()


# ---------------------------------------------------------------------------
# showmigrations — DB not yet initialised exception path (lines 378-379)
# ---------------------------------------------------------------------------


def test_showmigrations_with_uninitialised_db(tmp_path: Path) -> None:
    """showmigrations silently treats all migrations as pending when DB throws (lines 378-379)."""
    from unittest.mock import patch  # noqa: PLC0415

    from nextorm.migrations import showmigrations  # noqa: PLC0415

    # Create a migration file
    mig_file = tmp_path / "0001_seed.py"
    mig_file.write_text("def upgrade(db): pass\n")

    db = Database(entities=[BlogPost])
    db.bind("sqlite", ":memory:")
    db._ensure_connection()

    # Simulate a DB error (e.g. read-only or not yet initialised)
    with patch(
        "nextorm.migrations.core._ensure_tracking_table",
        side_effect=Exception("DB not initialised"),
    ):
        statuses = showmigrations(db, directory=tmp_path)

    # All migrations should be pending since the exception was swallowed
    assert len(statuses) == 1
    assert statuses[0].applied is False
    db.close()


# ---------------------------------------------------------------------------
# makemigrations snapshot with index — line 271 (t.indexes.append)
# ---------------------------------------------------------------------------


def test_makemigrations_snapshot_with_index(tmp_path: Path) -> None:
    """After saving a snapshot with indexes, loading it hits the t.indexes.append path."""
    from nextorm.migrations import makemigrations  # noqa: PLC0415
    from nextorm.schema.core import Index  # noqa: PLC0415

    db = Database(entities=[BlogPost])
    db.bind("sqlite", ":memory:")
    db.generate_mapping(create_tables=True)

    # Manually inject an index into the schema so the snapshot contains index data
    db._schema["blogpost"].indexes.append(
        Index(name="idx_blogpost__title", columns=["title"], unique=False)
    )

    # First makemigrations — creates snapshot with index data
    first = makemigrations(db, name="with_index", directory=tmp_path)
    assert first is not None

    # Second call — snapshot is loaded (including t.indexes.append path), no diff → None
    result = makemigrations(db, name="second", directory=tmp_path)
    assert result is None
    db.close()
