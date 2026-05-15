"""Debug and diagnostic utilities for NextORM.

Provides SQL debug logging, SQL capture for tests, and per-instance / global
query statistics.

Example::

    from nextorm import set_sql_debug, sql_debugging, capture_sql

    set_sql_debug(True)
    results = db.select(User).fetch_all()
    # prints: >>> SELECT id, name FROM "user"
    #           params: []

    with sql_debugging():
        result = db.select(User).filter(User.id == 1).fetch_one()

    # Capture SQL for test assertions (sync):
    with capture_sql() as queries:
        db.select(User).fetch_all()
    assert len(queries) == 1
    assert "SELECT" in queries[0].sql

    # Capture SQL for test assertions (async):
    async with async_capture_sql() as queries:
        await db.aselect(User).fetch_all()
    assert len(queries) == 1
"""

from __future__ import annotations

import contextvars
import sys
import threading
from dataclasses import dataclass, field
from typing import IO, Any

__all__ = [
    "set_sql_debug",
    "sql_debugging",
    "capture_sql",
    "async_capture_sql",
    "CapturedQuery",
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


# ---------------------------------------------------------------------------
# SQL capture (for test assertions)
# ---------------------------------------------------------------------------


@dataclass
class CapturedQuery:
    """A single SQL statement captured by :class:`capture_sql`.

    Attributes
    ----------
    sql:
        The raw SQL string as sent to the database driver.
    params:
        Bound parameter values in the same order as the ``?`` / ``%s``
        placeholders in *sql*.
    """

    sql: str
    params: list[Any] = field(default_factory=list)  # pyright: ignore[reportUnknownVariableType]

    def __str__(self) -> str:
        if self.params:
            return f"{self.sql}  -- params: {self.params}"
        return self.sql


class _CaptureStack:
    """Thread-local stack of active capture buckets.

    Each :class:`capture_sql` context manager pushes its list on entry and
    pops it on exit.  Nested ``capture_sql`` blocks accumulate into the
    innermost bucket only.
    """

    def __init__(self) -> None:
        self._tls: threading.local = threading.local()

    @property
    def current(self) -> list[CapturedQuery] | None:
        stack: list[list[CapturedQuery]] = getattr(self._tls, "stack", [])
        return stack[-1] if stack else None

    def push(self, bucket: list[CapturedQuery]) -> None:
        stack: list[list[CapturedQuery]] | None = getattr(self._tls, "stack", None)
        if stack is None:
            stack = []
            self._tls.stack = stack
        stack.append(bucket)

    def pop(self) -> None:
        stack: list[list[CapturedQuery]] = getattr(self._tls, "stack", [])
        if stack:
            stack.pop()


_capture_stack = _CaptureStack()


class capture_sql:
    """Context manager that captures every SQL statement executed within its scope.

    Returns a list of :class:`CapturedQuery` objects which is populated as
    queries run.  Ideal for test assertions::

        with capture_sql() as queries:
            users = db.select(User).fetch_all()
            count = db.select(User).count()

        assert len(queries) == 2
        assert "SELECT" in queries[0].sql
        assert queries[1].sql.startswith("SELECT COUNT")

    The list is also accessible after the ``with`` block via the variable
    bound by ``as``.  The context manager can be nested — each level
    captures independently into its own list::

        with capture_sql() as outer:
            with capture_sql() as inner:
                db.select(User).fetch_all()
            # inner has 1 entry, outer has 0 (innermost bucket wins)

    Sync-only.  For async contexts use :class:`async_capture_sql`.
    """

    def __init__(self) -> None:
        self._bucket: list[CapturedQuery] = []

    def __enter__(self) -> list[CapturedQuery]:
        _capture_stack.push(self._bucket)
        return self._bucket

    def __exit__(self, *_: object) -> None:
        _capture_stack.pop()


# ---------------------------------------------------------------------------
# Async SQL capture
# ---------------------------------------------------------------------------

_async_capture_var: contextvars.ContextVar[list[CapturedQuery] | None] = contextvars.ContextVar(
    "_async_capture", default=None
)


class async_capture_sql:
    """Async context manager that captures SQL from :class:`~nextorm.async_database.AsyncDatabase`.

    Uses a :class:`contextvars.ContextVar` so it works correctly across
    ``await`` boundaries within the same asyncio task::

        async with async_capture_sql() as queries:
            await db.aselect(User).fetch_all()
            await db.aselect(Post).filter(Post.draft == False).fetch_all()

        assert len(queries) == 2
        assert "WHERE" in queries[1].sql

    Nesting is supported — each level captures independently into its own
    list; only the innermost context captures queries::

        async with async_capture_sql() as outer:
            async with async_capture_sql() as inner:
                await db.aselect(User).fetch_all()
            # inner has 1 entry, outer has 0

    Only captures queries issued by :class:`~nextorm.async_database.AsyncDatabase`.
    For sync queries use :class:`capture_sql`.
    """

    def __init__(self) -> None:
        self._bucket: list[CapturedQuery] = []
        self._token: contextvars.Token[list[CapturedQuery] | None] | None = None

    async def __aenter__(self) -> list[CapturedQuery]:
        self._token = _async_capture_var.set(self._bucket)
        return self._bucket

    async def __aexit__(self, *_: object) -> None:
        if self._token is not None:
            _async_capture_var.reset(self._token)


def _print_sql(  # pyright: ignore[reportUnusedFunction]
    sql: str,
    params: list[Any],
    *,
    file: IO[str] | None = None,
) -> None:
    """Print *sql* and *params* to *file* (default: ``sys.stdout``) if debug is on."""
    if not _debug_enabled and _capture_stack.current is None:
        return
    if _debug_enabled:
        out = file or sys.stdout
        print(f">>> {sql}", file=out)
        if params:
            print(f"    params: {params}", file=out)
    bucket = _capture_stack.current
    if bucket is not None:
        bucket.append(CapturedQuery(sql=sql, params=list(params)))


def _record_async_capture(  # pyright: ignore[reportUnusedFunction]
    sql: str, params: list[Any]
) -> None:
    """Append *(sql, params)* to the active :class:`async_capture_sql` bucket, if any.

    Called exclusively from :class:`~nextorm.async_database.AsyncDatabase`
    execution paths so that :class:`async_capture_sql` captures only async
    queries, leaving sync queries for :class:`capture_sql`.
    """
    bucket = _async_capture_var.get()
    if bucket is not None:
        bucket.append(CapturedQuery(sql=sql, params=list(params)))


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
