"""Command-line interface for nextorm migrations.

Usage::

    python -m nextorm.migrations.cli makemigrations \\
        --module myapp.models \\
        --provider sqlite \\
        --dsn ":memory:" \\
        --directory migrations/ \\
        --name "add_tags"

    python -m nextorm.migrations.cli migrate \\
        --module myapp.models \\
        --provider sqlite \\
        --dsn ":memory:" \\
        --directory migrations/

The ``--module`` flag imports the Python module that defines the entities and
database so the CLI can introspect the schema.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path


def _get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m nextorm.migrations.cli",
        description="nextorm migration CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # makemigrations
    mk = sub.add_parser("makemigrations", help="Generate a new migration file")
    mk.add_argument("--module", required=True, help="Python module that sets up the Database")
    mk.add_argument(
        "--db-attr",
        default="db",
        help="Attribute name of the Database in the module (default: db)",
    )
    mk.add_argument(
        "--directory",
        default="migrations",
        help="Migration directory (default: migrations/)",
    )
    mk.add_argument("--name", default="migration", help="Human-readable name suffix")

    # migrate
    mg = sub.add_parser("migrate", help="Apply pending migration files")
    mg.add_argument("--module", required=True)
    mg.add_argument("--db-attr", default="db")
    mg.add_argument("--directory", default="migrations")
    mg.add_argument("--fake", action="store_true", help="Record as applied without executing")

    # showmigrations
    sm = sub.add_parser("showmigrations", help="Show the status of all migration files")
    sm.add_argument("--module", required=True)
    sm.add_argument("--db-attr", default="db")
    sm.add_argument("--directory", default="migrations")

    return parser


def _load_db(module_name: str, db_attr: str) -> object:
    mod = importlib.import_module(module_name)
    db = getattr(mod, db_attr, None)
    if db is None:
        print(
            f"ERROR: attribute '{db_attr}' not found in module '{module_name}'",
            file=sys.stderr,
        )
        sys.exit(1)
    return db


def main(argv: list[str] | None = None) -> None:
    """Entry point for the nextorm migration CLI."""
    parser = _get_parser()
    args = parser.parse_args(argv)

    from nextorm.migrations.core import (  # noqa: PLC0415
        makemigrations,
        migrate,
        showmigrations,
    )

    db = _load_db(args.module, args.db_attr)

    if args.command == "makemigrations":
        path = makemigrations(db, args.name, directory=Path(args.directory))  # type: ignore[arg-type]
        if path is None:
            print("No schema changes detected.")
        else:
            print(f"Created migration: {path}")
    elif args.command == "migrate":
        applied = migrate(db, directory=Path(args.directory), fake=args.fake)  # type: ignore[arg-type]
        if applied:
            for name in applied:
                prefix = "Faked" if args.fake else "Applied"
                print(f"  {prefix}: {name}")
        else:
            print("Nothing to migrate.")
    elif args.command == "showmigrations":  # pragma: no branch
        statuses = showmigrations(db, directory=Path(args.directory))  # type: ignore[arg-type]
        if not statuses:
            print("No migration files found.")
        else:
            for status in statuses:
                print(repr(status))


if __name__ == "__main__":
    main()
