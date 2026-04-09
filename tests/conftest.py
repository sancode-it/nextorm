"""Shared pytest configuration and fixtures for the nextorm test suite."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator


@pytest.fixture(autouse=True)
def _restore_database_registry() -> Generator[None, None, None]:  # pyright: ignore[reportUnusedFunction]
    """Snapshot the global _database_registry before each test and restore it afterwards.

    This prevents tests that create Database objects without calling ``db.close()``
    from polluting ``_database_registry`` and causing ``_find_db_for_entity()`` to
    return the wrong DB in subsequent tests.
    """
    from nextorm.database import _database_registry  # noqa: PLC0415

    snapshot = list(_database_registry)
    yield
    # Remove any entries added during the test that weren't there before.
    _database_registry[:] = snapshot
