"""nextorm.migrations — file-based migration framework.

Usage::

    # From the command line:
    python -m nextorm.migrations.cli makemigrations --directory migrations/
    python -m nextorm.migrations.cli migrate --directory migrations/
    python -m nextorm.migrations.cli showmigrations --directory migrations/

Public API re-exports
---------------------
- :func:`makemigrations` — diff entity schema and write a migration file
- :func:`migrate` — apply pending migration files in order
- :func:`showmigrations` — report the applied/pending status of each migration
- :class:`MigrationRunner` — object-oriented runner
- :class:`MigrationStatus` — status of a single migration file
"""

from nextorm.migrations.core import (
    MigrationRunner,
    MigrationStatus,
    makemigrations,
    migrate,
    showmigrations,
)

__all__ = ["MigrationRunner", "MigrationStatus", "makemigrations", "migrate", "showmigrations"]
