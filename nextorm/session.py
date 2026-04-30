"""Session management — identity map, dirty tracking, db_session context manager.

Usage (synchronous)::

    from nextorm.session import db_session

    with db_session:
        user = cache.get((User, 1))

Usage (async)::

    from nextorm.session import async_db_session

    async with async_db_session:
        ...

Usage as a decorator::

    @db_session
    def my_view(): ...


    @async_db_session
    async def my_async_view(): ...

Parametrised form::

    @db_session(retry=3)
    def my_view(): ...


    with db_session(sql_debug=True):
        ...
"""

from __future__ import annotations

import functools
import inspect
import threading
from contextlib import asynccontextmanager, contextmanager, suppress
from typing import TYPE_CHECKING, Any, Self, overload

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Generator

from nextorm.entity import Entity
from nextorm.exceptions import TransactionError

__all__ = ["SessionCache", "db_session", "async_db_session"]


def _collect_dbs(cache: SessionCache | None) -> list[Any]:
    """Collect unique Database / AsyncDatabase instances referenced in *cache*."""
    assert cache is not None  # always called within an active session
    seen: set[int] = set()
    dbs: list[Any] = []
    # Check identity map, pending-save queue, and dirty set
    all_entities = list(cache._objects.values()) + list(cache._to_save) + list(cache._dirty)
    for entity in all_entities:
        db = vars(entity).get("_db_")
        if db is not None and id(db) not in seen:
            seen.add(id(db))
            dbs.append(db)
    return dbs


class SessionCache:
    """Identity map and dirty-object tracker for a single database session.

    The cache is keyed by ``(entity_class, primary_key_value)`` which ensures
    that at most one Python object exists per database row inside a session.

    Attributes
    ----------
    _objects:
        The identity map: ``{(entity_cls, pk): entity_instance}``.
    _dirty:
        Set of entity instances that have been modified and need flushing.
    _to_save:
        Instances scheduled for ``INSERT`` (not yet persisted).
    _modified_collections:
        Tracks M2M collections that have pending additions/removals.
    """

    def __init__(self) -> None:
        self._objects: dict[tuple[type[Entity], Any], Entity] = {}
        self._dirty: set[Entity] = set()
        self._to_save: list[Entity] = []
        self._modified_collections: dict[tuple[Entity, str], dict[str, list[Entity]]] = {}
        #: Set by :class:`DBSessionManager` when ``serializable=True`` was requested.
        self.serializable: bool = False
        #: Set by :class:`DBSessionManager` when ``immediate=True`` was requested.
        self.immediate: bool = False
        #: Set to ``False`` by :class:`DBSessionManager` when ``optimistic=False`` was requested.
        #: When ``True`` (the default), :meth:`~nextorm.database.Database.save` adds
        #: ``AND col = orig_val`` clauses for the columns the caller has READ since load,
        #: implementing Pony-style per-field optimistic concurrency detection.
        self.optimistic: bool = True
        #: When ``True``, entity objects should not be accessed after the session ends.
        #: Currently the cache is always cleared; ``strict=False`` retains the same
        #: behaviour — no cross-session lazy-load is supported yet.
        self.strict: bool = False

    # ------------------------------------------------------------------
    # Identity map
    # ------------------------------------------------------------------

    def get(self, key: tuple[type[Entity], Any]) -> Entity | None:
        """Return the cached instance for *key*, or ``None``."""
        return self._objects.get(key)

    def put(self, entity: Entity, pk: Any) -> None:
        """Add *entity* to the identity map under its ``(type, pk)`` key."""
        self._objects[(type(entity), pk)] = entity

    def remove(self, entity: Entity, pk: Any) -> None:
        """Remove *entity* from the identity map."""
        self._objects.pop((type(entity), pk), None)
        self._dirty.discard(entity)

    def clear(self) -> None:
        """Wipe all cached state (called on session commit / rollback)."""
        self._objects.clear()
        self._dirty.clear()
        self._to_save.clear()
        self._modified_collections.clear()

    # ------------------------------------------------------------------
    # Dirty tracking
    # ------------------------------------------------------------------

    def mark_dirty(self, entity: Entity) -> None:
        """Mark *entity* as modified so it will be flushed on commit."""
        self._dirty.add(entity)

    def unmark_dirty(self, entity: Entity) -> None:
        """Remove *entity* from the dirty set (e.g. after successful flush)."""
        self._dirty.discard(entity)

    @property
    def dirty_objects(self) -> frozenset[Entity]:
        """Snapshot of currently-dirty entity instances."""
        return frozenset(self._dirty)

    # ------------------------------------------------------------------
    # New-object queue
    # ------------------------------------------------------------------

    def schedule_save(self, entity: Entity) -> None:
        """Enqueue *entity* for INSERT on the next commit."""
        if entity not in self._to_save:
            self._to_save.append(entity)

    def unschedule_save(self, entity: Entity) -> None:
        """Remove *entity* from the INSERT queue (called after a successful save)."""
        with suppress(ValueError):
            self._to_save.remove(entity)

    @property
    def objects_to_save(self) -> list[Entity]:
        """Ordered list of entities pending INSERT."""
        return list(self._to_save)

    # ------------------------------------------------------------------
    # M2M collection tracking
    # ------------------------------------------------------------------

    def track_collection_change(self, owner: Entity, attr: str, action: str, related: Entity) -> None:
        """Record that *related* was added to or removed from *owner*.*attr*.

        Parameters
        ----------
        action:
            ``"add"`` or ``"remove"``.
        """
        key = (owner, attr)
        if key not in self._modified_collections:
            self._modified_collections[key] = {"add": [], "remove": []}
        self._modified_collections[key][action].append(related)

    @property
    def modified_collections(
        self,
    ) -> dict[tuple[Entity, str], dict[str, list[Entity]]]:
        """Snapshot of pending M2M collection mutations."""
        return dict(self._modified_collections)


# ---------------------------------------------------------------------------
# Thread-local / task-local session stack
# ---------------------------------------------------------------------------


class _SessionStack:
    """Nesting-aware session holder for a single thread.

    Only ONE :class:`SessionCache` exists per thread at any time.  Nested
    ``db_session`` calls (decorator-on-decorator or nested ``with`` blocks)
    increment and decrement a depth counter instead of pushing separate caches.
    This ensures that entities created at any nesting level land in the same
    identity map, mirroring PonyORM's behaviour.
    """

    def __init__(self) -> None:
        self._cache: SessionCache | None = None
        self._depth: int = 0

    def push(self, cache: SessionCache) -> None:
        """Push *cache* as the outermost session (must be called at depth 0).

        For re-entrant / nested entries, call :meth:`enter_nested` instead.
        """
        assert self._depth == 0, "push() called while a session is already active"
        self._cache = cache
        self._depth = 1

    def enter_nested(self) -> None:
        """Increment nesting depth for an inner ``db_session`` call."""
        self._depth += 1

    def pop(self) -> SessionCache:
        """Decrement depth; return the shared cache.

        When depth reaches 0 the cache reference is cleared so that the next
        :meth:`push` starts fresh.
        """
        self._depth -= 1
        cache = self._cache
        if self._depth == 0:
            self._cache = None
        return cache  # type: ignore[return-value]

    @property
    def current(self) -> SessionCache | None:
        """The active :class:`SessionCache`, or ``None`` when no session is open."""
        return self._cache

    @property
    def depth(self) -> int:
        """Current nesting depth — 0 outside a session, ≥1 inside."""
        return self._depth


# Per-thread session stack — each thread gets its own independent stack so that
# concurrent threads cannot interfere with each other's session state.
_tls: threading.local = threading.local()


def _get_session_stack() -> _SessionStack:
    """Return the :class:`_SessionStack` for the current thread.

    Created lazily on first access so that threads that never open a session
    pay no overhead.
    """
    stack: _SessionStack | None = getattr(_tls, "stack", None)
    if stack is None:
        stack = _SessionStack()
        _tls.stack = stack
    return stack


def get_current_session() -> SessionCache:
    """Return the active :class:`SessionCache`.

    Raises
    ------
    RuntimeError
        If called outside of a ``db_session`` block.
    """
    cache = _get_session_stack().current
    if cache is None:
        raise RuntimeError("No active db_session. Use 'with db_session:' or '@db_session'.")
    return cache


# ---------------------------------------------------------------------------
# DBSessionManager — the object exposed as ``db_session``
# ---------------------------------------------------------------------------


class DBSessionManager:
    """Unified sync/async context manager and decorator for database sessions.

    The same ``db_session`` object can be used in four ways::

        with db_session:  # sync context manager (no-arg)
            ...

        with db_session():  # sync context manager (call form)
            ...


        @db_session  # sync decorator
        def f(): ...


        @db_session  # async decorator (detected at call time)
        async def f(): ...

    Parametrised form::

        with db_session(sql_debug=True):
            ...


        @db_session(retry=3)
        def f(): ...

    Nesting is supported — only the outermost context actually clears the cache
    on exit.

    Parameters
    ----------
    retry:
        Number of *additional* attempts to make after the first one fails with
        :exc:`~nextorm.exceptions.TransactionError` (e.g. a deadlock or
        serialisation failure).  ``retry=3`` means up to 4 total attempts.
        Applies only when used as a decorator.
    sql_debug:
        When ``True``, enables SQL debug logging for the duration of the
        session and restores the previous state on exit.  Equivalent to
        wrapping the body with :class:`~nextorm.debug.sql_debugging`.
    serializable:
        Hint that the session should use ``SERIALIZABLE`` transaction
        isolation.  Recorded on the session cache; enforcement is the
        responsibility of the provider / middleware layer.
    immediate:
        Hint that the session should lock immediately (SQLite
        ``BEGIN IMMEDIATE``).  Recorded on the session cache; enforcement is
        the responsibility of the provider / middleware layer.
    """

    def __init__(
        self,
        *,
        retry: int = 0,
        sql_debug: bool = False,
        show_values: bool = False,
        serializable: bool = False,
        immediate: bool = False,
        optimistic: bool = True,
        strict: bool = False,
        allowed_exceptions: list[type[BaseException]] | None = None,
        retry_exceptions: list[type[BaseException]] | None = None,
    ) -> None:
        self._retry = retry
        self._sql_debug = sql_debug
        self._show_values = show_values
        self._serializable = serializable
        self._immediate = immediate
        self._optimistic = optimistic
        self._strict = strict
        self._allowed_exceptions: tuple[type[BaseException], ...] = tuple(allowed_exceptions or ())
        # Default retry list is [TransactionError], matching PonyORM semantics
        self._retry_exceptions: tuple[type[BaseException], ...] = tuple(
            retry_exceptions if retry_exceptions is not None else [TransactionError]
        )

    # --- sync context manager -----------------------------------------------

    def _push_cache(self) -> SessionCache:
        """Return the active :class:`SessionCache`, creating one only for the outermost entry.

        Nested ``db_session`` calls increment the depth counter and reuse the
        existing cache, ensuring a single identity map for the whole nesting
        tree — matching PonyORM semantics.
        """
        stack = _get_session_stack()
        if stack.current is not None:
            # Nested entry — share the existing outermost cache.
            stack.enter_nested()
            return stack.current
        cache = SessionCache()
        cache.serializable = self._serializable
        cache.immediate = self._immediate
        cache.optimistic = self._optimistic
        cache.strict = self._strict
        stack.push(cache)
        return cache

    def __enter__(self) -> SessionCache:
        if self._sql_debug:
            from nextorm.debug import set_sql_debug  # noqa: PLC0415

            set_sql_debug(True)
        return self._push_cache()

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool | None:
        if self._sql_debug:
            from nextorm.debug import set_sql_debug  # noqa: PLC0415

            set_sql_debug(False)
        # Check outermost BEFORE popping (depth 1 = this is the only active session)
        is_outermost = _get_session_stack().depth == 1
        # allowed_exceptions: treat as success (commit, then suppress).
        suppress = False
        if (
            exc_type is not None
            and self._allowed_exceptions
            and issubclass(exc_type, self._allowed_exceptions)
        ):
            suppress = True
            exc_type = None  # treat as clean exit for commit
        dbs: list[Any] = []
        commit_exc: BaseException | None = None
        if exc_type is None:
            # Clean exit: flush all pending changes then commit, primary-first.
            if is_outermost:
                dbs = _collect_dbs(_get_session_stack().current)
                try:
                    for db in dbs:
                        db.flush()
                except Exception as _fe:
                    commit_exc = _fe
                    for _db in dbs:
                        try:  # noqa: SIM105
                            _db._rollback_transaction()
                        except Exception:
                            pass
                else:
                    primary, secondaries = (dbs[0] if dbs else None), dbs[1:]
                    if primary is not None:
                        try:
                            primary._commit_transaction()
                        except Exception as _ce:
                            commit_exc = _ce
                            for _db in secondaries:
                                try:  # noqa: SIM105
                                    _db._rollback_transaction()
                                except Exception:
                                    pass
                        else:
                            for _db in secondaries:
                                try:  # noqa: SIM105
                                    _db._commit_transaction()
                                except Exception as _se:
                                    if commit_exc is None:
                                        commit_exc = _se
        else:
            # Unhandled exception: roll back all databases.
            if is_outermost:
                dbs = _collect_dbs(_get_session_stack().current)
                for db in dbs:
                    try:  # noqa: SIM105
                        db._rollback_transaction()
                    except Exception:
                        pass
        # ALWAYS pop and clear — even if commit raised above.
        cache = _get_session_stack().pop()
        if is_outermost:
            cache.clear()
        if commit_exc is not None:
            raise commit_exc  # re-raise after cleanup
        return bool(suppress) or None

    # Calling ``with db_session():`` creates a temporary manager so that both
    # ``with db_session:`` and ``with db_session():`` work identically.
    @overload
    def __call__[T, **P](self, func: Callable[P, T], /) -> Callable[P, T]: ...

    @overload
    def __call__(
        self,
        func: None = None,
        /,
        *,
        retry: int | None = None,
        sql_debug: bool | None = None,
        show_values: bool | None = None,
        serializable: bool | None = None,
        immediate: bool | None = None,
        optimistic: bool | None = None,
        strict: bool | None = None,
        allowed_exceptions: list[type[BaseException]] | None = None,
        retry_exceptions: list[type[BaseException]] | None = None,
    ) -> Self: ...

    def __call__(
        self,
        func: Callable[..., Any] | None = None,
        /,
        *,
        retry: int | None = None,
        sql_debug: bool | None = None,
        show_values: bool | None = None,
        serializable: bool | None = None,
        immediate: bool | None = None,
        optimistic: bool | None = None,
        strict: bool | None = None,
        allowed_exceptions: list[type[BaseException]] | None = None,
        retry_exceptions: list[type[BaseException]] | None = None,
    ) -> Callable[..., Any] | Self:
        """Called as ``db_session(...)`` to create a parametrised manager, or as a decorator.

        When called **without** a callable argument (e.g. ``db_session(retry=3)``),
        returns a new :class:`DBSessionManager` configured with the given parameters.
        When called **with** a callable (``@db_session`` bare decorator), wraps it.
        """
        if func is None:
            # ``with db_session(retry=n, ...)`` or ``@db_session(retry=n)``
            return DBSessionManager(
                retry=retry if retry is not None else self._retry,
                sql_debug=sql_debug if sql_debug is not None else self._sql_debug,
                show_values=show_values if show_values is not None else self._show_values,
                serializable=serializable if serializable is not None else self._serializable,
                immediate=immediate if immediate is not None else self._immediate,
                optimistic=optimistic if optimistic is not None else self._optimistic,
                strict=strict if strict is not None else self._strict,
                allowed_exceptions=(
                    allowed_exceptions
                    if allowed_exceptions is not None
                    else list(self._allowed_exceptions)
                ),
                retry_exceptions=(
                    retry_exceptions if retry_exceptions is not None else list(self._retry_exceptions)
                ),
            )
        # Bare ``@db_session`` / ``@db_session()`` with a callable → wrap it
        if inspect.iscoroutinefunction(func):
            return self._async_wrapper(func)
        return self._sync_wrapper(func)

    def _sync_wrapper(self, func: Callable[..., Any]) -> Callable[..., Any]:
        retry = self._retry
        retry_exc = self._retry_exceptions  # already a tuple

        @functools.wraps(func)
        def _inner(*args: Any, **kwargs: Any) -> Any:
            attempts = 0
            while True:
                try:
                    with self:
                        return func(*args, **kwargs)
                except retry_exc:
                    attempts += 1
                    if attempts > retry:
                        raise

        return _inner

    def _async_wrapper(self, func: Callable[..., Any]) -> Callable[..., Any]:
        retry = self._retry
        retry_exc = self._retry_exceptions  # already a tuple

        @functools.wraps(func)
        async def _inner(*args: Any, **kwargs: Any) -> Any:
            attempts = 0
            while True:
                try:
                    async with self:
                        return await func(*args, **kwargs)
                except retry_exc:
                    attempts += 1
                    if attempts > retry:
                        raise

        return _inner

    # Allow ``@db_session`` (without call) on both sync and async functions
    def __get__(self, obj: Any, objtype: Any = None) -> Any:  # pragma: no cover
        return self

    # --- async context manager ----------------------------------------------

    async def __aenter__(self) -> SessionCache:
        if self._sql_debug:
            from nextorm.debug import set_sql_debug  # noqa: PLC0415

            set_sql_debug(True)
        return self._push_cache()

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool | None:
        if self._sql_debug:
            from nextorm.debug import set_sql_debug  # noqa: PLC0415

            set_sql_debug(False)
        is_outermost = _get_session_stack().depth == 1
        suppress = False
        if (
            exc_type is not None
            and self._allowed_exceptions
            and issubclass(exc_type, self._allowed_exceptions)
        ):
            suppress = True
            exc_type = None
        dbs: list[Any] = []
        commit_exc: BaseException | None = None
        if exc_type is None:
            # Clean exit: flush all pending changes then commit, primary-first.
            if is_outermost:
                dbs = _collect_dbs(_get_session_stack().current)
                try:
                    for db in dbs:
                        if getattr(db, "_is_async", False):
                            await db.aflush()
                        else:
                            db.flush()
                except Exception as _fe:
                    commit_exc = _fe
                    for _db in dbs:
                        try:  # noqa: SIM105
                            if getattr(_db, "_is_async", False):
                                await _db._arollback_transaction()
                            else:
                                _db._rollback_transaction()
                        except Exception:
                            pass
                else:
                    primary, secondaries = (dbs[0] if dbs else None), dbs[1:]
                    if primary is not None:
                        try:
                            if getattr(primary, "_is_async", False):
                                await primary._acommit_transaction()
                            else:
                                primary._commit_transaction()
                        except Exception as _ce:
                            commit_exc = _ce
                            for _db in secondaries:
                                try:  # noqa: SIM105
                                    if getattr(_db, "_is_async", False):
                                        await _db._arollback_transaction()
                                    else:
                                        _db._rollback_transaction()
                                except Exception:
                                    pass
                        else:
                            for _db in secondaries:
                                try:  # noqa: SIM105
                                    if getattr(_db, "_is_async", False):
                                        await _db._acommit_transaction()
                                    else:
                                        _db._commit_transaction()
                                except Exception as _se:
                                    if commit_exc is None:
                                        commit_exc = _se
        else:
            # Unhandled exception: roll back all databases.
            if is_outermost:
                dbs = _collect_dbs(_get_session_stack().current)
                for db in dbs:
                    try:  # noqa: SIM105
                        if getattr(db, "_is_async", False):
                            await db._arollback_transaction()
                        else:
                            db._rollback_transaction()
                    except Exception:
                        pass
        cache = _get_session_stack().pop()
        if is_outermost:
            cache.clear()
        if commit_exc is not None:
            raise commit_exc
        return bool(suppress) or None

    # --- generator-based helpers (for library code) -------------------------

    @contextmanager
    def as_context(self) -> Generator[SessionCache, None, None]:
        """Explicit generator-based context (useful in pytest with ``with`` blocks)."""
        with self as cache:
            yield cache

    @asynccontextmanager
    async def as_async_context(self) -> AsyncGenerator[SessionCache, None]:
        """Explicit async generator-based context."""
        async with self as cache:
            yield cache

    # Introspection helpers used in tests and framework internals

    @property
    def depth(self) -> int:
        """Current nesting depth (0 = not inside any session)."""
        return _get_session_stack().depth

    @staticmethod
    def current() -> SessionCache | None:
        """Return the innermost active :class:`SessionCache`, or ``None``."""
        return _get_session_stack().current


db_session: DBSessionManager = DBSessionManager()

#: Convenience alias — behaves identically to :data:`db_session` but signals
#: async intent to readers of the calling code.
async_db_session: DBSessionManager = db_session
