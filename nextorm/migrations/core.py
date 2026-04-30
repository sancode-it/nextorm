"""Core logic for file-based migrations — snapshot diffing, file writing, and running.

Migration files are plain Python modules stored under a project directory
(e.g. ``migrations/``).  Each file is numbered sequentially:

    migrations/
        0001_initial.py
        0002_add_email_to_user.py
        ...

Each migration module **must** define an ``upgrade(db)`` function.
``downgrade(db)`` is optional.

Example migration file::

    def upgrade(db):
        db._execute_dml("ALTER TABLE user ADD COLUMN email TEXT NOT NULL DEFAULT ''", [])
        db._execute_dml("ALTER TABLE user ALTER COLUMN email DROP DEFAULT", [])


    def downgrade(db):
        db._execute_dml("ALTER TABLE user DROP COLUMN email", [])
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from nextorm.fields import AttrValue

if TYPE_CHECKING:
    from nextorm.database import Database

__all__ = ["MigrationRunner", "MigrationStatus", "makemigrations", "migrate", "showmigrations"]

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class MigrationStatus:
    """Status of a single migration file.

    Attributes
    ----------
    name:
        The migration filename (e.g. ``0001_initial.py``).
    version:
        The stem of the filename without the ``.py`` extension
        (e.g. ``0001_initial``).
    applied:
        ``True`` if the migration has been recorded in the tracking table.
    applied_at:
        ISO‑8601 timestamp string recorded when the migration was applied,
        or an empty string when not yet applied.
    """

    __slots__ = ("name", "version", "applied", "applied_at")

    def __init__(self, name: str, version: str, applied: bool, applied_at: str) -> None:
        self.name = name
        self.version = version
        self.applied = applied
        self.applied_at = applied_at

    def __repr__(self) -> str:
        mark = "x" if self.applied else " "
        ts = f"  ({self.applied_at})" if self.applied and self.applied_at else ""
        return f"[{mark}] {self.name}{ts}"


# ---------------------------------------------------------------------------
# Schema snapshot helpers
# ---------------------------------------------------------------------------

_TRACKING_TABLE = "_nextorm_migrations"


def _schema_to_dict(schema: dict[str, Any]) -> dict[str, Any]:
    """Serialize a ``{table_name: Table}`` schema to a JSON-safe dict."""
    result: dict[str, Any] = {}
    for table_name, table in schema.items():
        result[table_name] = {
            "columns": [
                {
                    "name": c.name,
                    "nullable": c.nullable,
                    "primary_key": c.primary_key,
                    "unique": c.unique,
                }
                for c in table.columns
            ],
            "foreign_keys": [
                {
                    "name": fk.name,
                    "column": fk.column,
                    "ref_table": fk.ref_table,
                    "ref_column": fk.ref_column,
                    "on_delete": fk.on_delete,
                }
                for fk in table.foreign_keys
            ],
            "indexes": [
                {"name": idx.name, "columns": idx.columns, "unique": idx.unique}
                for idx in table.indexes
            ],
        }
    return result


def _snapshot_path(directory: Path) -> Path:
    return directory / ".schema_snapshot.json"


def _load_snapshot(directory: Path) -> dict[str, Any]:
    snap = _snapshot_path(directory)
    if not snap.exists():
        return {}
    return cast("dict[str, Any]", json.loads(snap.read_text()))


def _save_snapshot(directory: Path, schema: dict[str, Any]) -> None:
    _snapshot_path(directory).write_text(json.dumps(schema, indent=2))


# ---------------------------------------------------------------------------
# Migration file helpers
# ---------------------------------------------------------------------------

_MIGRATION_RE = re.compile(r"^(\d{4})_(.+)\.py$")


def _next_migration_number(directory: Path) -> int:
    """Return the next sequential migration number."""
    existing = [int(m.group(1)) for f in directory.iterdir() if (m := _MIGRATION_RE.match(f.name))]
    return max(existing, default=0) + 1


def _migration_files(directory: Path) -> list[Path]:
    """Return sorted list of migration .py files in *directory*."""
    return sorted(f for f in directory.iterdir() if _MIGRATION_RE.match(f.name))


def _load_migration_module(path: Path) -> Any:
    """Import a migration file as a module."""
    spec = importlib.util.spec_from_file_location(f"_migration_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load migration: {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod.__name__] = mod
    spec.loader.exec_module(mod)
    return mod


def _generate_migration_body(ops: list[str]) -> str:
    """Generate the Python source for a migration file from a list of SQL statements."""
    lines: list[str] = [
        '"""Auto-generated migration."""',
        "",
        "",
        "def upgrade(db):",
    ]
    if ops:
        for sql in ops:
            escaped = sql.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
            lines.append(f'    db._execute_dml("{escaped}", [])')
    else:
        lines.append("    pass  # no schema changes")
    lines += [
        "",
        "",
        "def downgrade(db):",
        "    pass  # downgrade not auto-generated",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Tracking table
# ---------------------------------------------------------------------------


def _ensure_tracking_table(db: Database) -> None:
    """Create the _nextorm_migrations tracking table if it doesn't exist."""
    db._execute_dml(
        f"CREATE TABLE IF NOT EXISTS {_TRACKING_TABLE} "
        "(version TEXT NOT NULL PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT '')",
        [],
    )


def _applied_versions(db: Database) -> dict[str, str]:
    """Return ``{version: applied_at}`` for every recorded migration."""
    rows = db._execute(f"SELECT version, applied_at FROM {_TRACKING_TABLE}", [])
    return {row[0]: (row[1] or "") for row in rows}


def _record_version(db: Database, version: str) -> None:
    import datetime  # noqa: PLC0415

    now = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    db._execute_dml(
        f"INSERT INTO {_TRACKING_TABLE} (version, applied_at) VALUES (?, ?)",
        [version, now],
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def makemigrations(
    db: Database,
    name: str = "migration",
    *,
    directory: str | Path = "migrations",
) -> Path | None:
    """Diff the entity schema against the last snapshot and write a migration file.

    Parameters
    ----------
    db:
        A bound :class:`~nextorm.database.Database` with schema already built.
    name:
        Human-readable suffix for the migration filename.
    directory:
        Filesystem directory where migration files are stored.

    Returns
    -------
    Path
        The path of the newly written migration file, or ``None`` if there
        are no schema changes.
    """
    from nextorm.schema.diff import diff_schemas  # noqa: PLC0415

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    if not db._schema:
        raise RuntimeError(
            "Database schema is empty. Call generate_mapping() before makemigrations()."
        )

    snapshot = _load_snapshot(directory)

    # Reconstruct a minimal current-schema Table dict from the snapshot for diffing
    from nextorm.schema.core import Column, Index, Table  # noqa: PLC0415

    snapshot_tables: dict[str, Table] = {}
    for tname, tdata in snapshot.items():
        t = Table(name=tname)
        for col_data in tdata.get("columns", []):
            t.columns.append(
                Column(
                    name=col_data["name"],
                    # sentinel: type not stored in snapshot
                    py_type=cast("type[AttrValue]", object),
                    nullable=col_data.get("nullable", False),
                    primary_key=col_data.get("primary_key", False),
                    unique=col_data.get("unique", False),
                )
            )
        for idx_data in tdata.get("indexes", []):
            t.indexes.append(
                Index(
                    name=idx_data["name"],
                    columns=idx_data["columns"],
                    unique=idx_data.get("unique", False),
                )
            )
        snapshot_tables[tname] = t

    ops = diff_schemas(snapshot_tables, db._schema)
    if not ops:
        return None

    # Render ops to SQL using the database's DDL renderer
    assert db._renderer is not None
    sql_stmts = [db._renderer.render(op) for op in ops]

    num = _next_migration_number(directory)
    slug = name.lower().replace(" ", "_")
    filename = f"{num:04d}_{slug}.py"
    path = directory / filename
    path.write_text(_generate_migration_body(sql_stmts))

    # Update the snapshot
    _save_snapshot(directory, _schema_to_dict(db._schema))

    return path


def migrate(
    db: Database,
    *,
    directory: str | Path = "migrations",
    fake: bool = False,
) -> list[str]:
    """Apply all pending migration files in *directory*.

    Parameters
    ----------
    db:
        A bound, connected :class:`~nextorm.database.Database`.
    directory:
        Directory containing migration files.
    fake:
        If ``True``, record each migration as applied without executing the SQL.

    Returns
    -------
    list[str]
        The names of migration files that were applied (or faked).
    """
    directory = Path(directory)
    if not directory.exists():
        return []

    _ensure_tracking_table(db)
    applied = _applied_versions(db)
    applied_names: list[str] = []

    for path in _migration_files(directory):
        version = path.stem
        if version in applied:
            continue
        if not fake:
            mod = _load_migration_module(path)
            if hasattr(mod, "upgrade"):
                mod.upgrade(db)
        _record_version(db, version)
        applied_names.append(path.name)

    return applied_names


def showmigrations(
    db: Database,
    *,
    directory: str | Path = "migrations",
) -> list[MigrationStatus]:
    """Return the status of every migration file in *directory*.

    Each entry is a :class:`MigrationStatus` describing whether the migration
    has been applied and, if so, when.

    Parameters
    ----------
    db:
        A bound, connected :class:`~nextorm.database.Database`.
    directory:
        Directory containing migration files.

    Returns
    -------
    list[MigrationStatus]
        One entry per migration file, in ascending version order.  When the
        tracking table does not exist yet, all migrations are reported as
        pending.
    """
    directory = Path(directory)
    if not directory.exists():
        return []

    # Read applied versions — create the tracking table only if it already
    # exists in the database; we don't want showmigrations to be a side-effect.
    applied: dict[str, str] = {}
    try:
        _ensure_tracking_table(db)
        applied = _applied_versions(db)
    except Exception:  # noqa: BLE001
        pass  # DB not yet initialised — treat all as pending

    statuses: list[MigrationStatus] = []
    for path in _migration_files(directory):
        version = path.stem
        is_applied = version in applied
        statuses.append(
            MigrationStatus(
                name=path.name,
                version=version,
                applied=is_applied,
                applied_at=applied.get(version, ""),
            )
        )
    return statuses


# ---------------------------------------------------------------------------
# MigrationRunner — object-oriented interface
# ---------------------------------------------------------------------------


class MigrationRunner:
    """Object-oriented wrapper for :func:`makemigrations` and :func:`migrate`.

    Example::

        runner = MigrationRunner(db, directory="migrations/")
        runner.makemigrations(name="add_tags")
        runner.migrate()
    """

    def __init__(
        self,
        db: Database,
        *,
        directory: str | Path = "migrations",
    ) -> None:
        self._db = db
        self.directory = Path(directory)

    def makemigrations(self, name: str = "migration") -> Path | None:
        """Write a new migration file if there are schema changes."""
        return makemigrations(self._db, name, directory=self.directory)

    def migrate(self, *, fake: bool = False) -> list[str]:
        """Apply all pending migrations."""
        return migrate(self._db, directory=self.directory, fake=fake)

    def showmigrations(self) -> list[MigrationStatus]:
        """Return the status of every migration file in the migrations directory."""
        return showmigrations(self._db, directory=self.directory)
