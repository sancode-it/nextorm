"""Debug and diagnostic utilities for NextORM.

Provides SQL debug logging and per-instance / global query statistics.

Example::

    from nextorm import set_sql_debug, sql_debugging

    set_sql_debug(True)
    results = db.select(User).fetch_all()
    # prints: >>> SELECT id, name FROM "user"
    #           params: []

    with sql_debugging():
        result = db.select(User).filter(User.id == 1).fetch_one()
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass
from typing import IO, Any

__all__ = [
    "set_sql_debug",
    "sql_debugging",
    "QueryStat",
    "global_stats",
    "clear_global_stats",
]


# ---------------------------------------------------------------------------
# SQL debug
# ---------------------------------------------------------------------------

_debug_lock = threading.Lock()
_debug_enabled: bool = False


def set_sql_debug(debug: bool = True) -> None:
    """Enable or disable global SQL debug logging.

    When enabled, every SQL statement executed by :class:`~nextorm.database.Database`
    or :class:`~nextorm.async_database.AsyncDatabase` is printed to *stdout*
    before execution.  Use :class:`sql_debugging` as a context manager for
    scoped temporary debugging.

    Parameters
    ----------
    debug:
        ``True`` to enable (default), ``False`` to disable.
    """
    global _debug_enabled
    with _debug_lock:
        _debug_enabled = debug


class sql_debugging:
    """Context manager that temporarily enables SQL debug output.

    On exit the previous debug state is restored, regardless of exceptions::

        with sql_debugging():
            users = db.select(User).fetch_all()
        # debug logging is off again here
    """

    def __init__(self) -> None:
        self._previous: bool = False

    def __enter__(self) -> sql_debugging:
        global _debug_enabled
        with _debug_lock:
            self._previous = _debug_enabled
            _debug_enabled = True
        return self

    def __exit__(self, *_: object) -> None:
        global _debug_enabled
        with _debug_lock:
            _debug_enabled = self._previous


def _print_sql(  # pyright: ignore[reportUnusedFunction]
    sql: str,
    params: list[Any],
    *,
    file: IO[str] | None = None,
) -> None:
    """Print *sql* and *params* to *file* (default: ``sys.stdout``) if debug is on."""
    if not _debug_enabled:
        return
    out = file or sys.stdout
    print(f">>> {sql}", file=out)
    if params:
        print(f"    params: {params}", file=out)


# ---------------------------------------------------------------------------
# Query statistics
# ---------------------------------------------------------------------------


@dataclass
class QueryStat:
    """Per-query-string execution statistics.

    Attributes
    ----------
    count:
        Number of times the query was executed.
    sum_time:
        Total execution time in seconds.
    min_time:
        Minimum single execution time (``inf`` when *count* is 0).
    max_time:
        Maximum single execution time in seconds.
    avg_time:
        Average execution time in seconds (computed property).
    """

    count: int = 0
    sum_time: float = 0.0
    min_time: float = float("inf")
    max_time: float = 0.0

    @property
    def avg_time(self) -> float:
        """Average execution time in seconds."""
        return self.sum_time / self.count if self.count else 0.0

    def _record(self, elapsed: float) -> None:
        """Record one query execution taking *elapsed* seconds."""
        self.count += 1
        self.sum_time += elapsed
        if elapsed < self.min_time:
            self.min_time = elapsed
        if elapsed > self.max_time:
            self.max_time = elapsed

    def _merge(self, other: QueryStat) -> None:
        """Merge all observations from *other* into this instance."""
        if other.count == 0:
            return
        self.count += other.count
        self.sum_time += other.sum_time
        if other.min_time < self.min_time:
            self.min_time = other.min_time
        if other.max_time > self.max_time:
            self.max_time = other.max_time


#: Module-level global query statistics.
#:
#: Populated by :meth:`~nextorm.database.Database.merge_local_stats`;
#: cleared by :func:`clear_global_stats`.
global_stats: dict[str, QueryStat] = {}

_global_stats_lock = threading.Lock()


def clear_global_stats() -> None:
    """Clear the module-level :data:`global_stats` dictionary."""
    with _global_stats_lock:
        global_stats.clear()
