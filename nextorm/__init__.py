"""Public API surface for nextorm."""

import contextlib

from nextorm.async_database import AsyncDatabase, AsyncQuerySet
from nextorm.database import Database
from nextorm.debug import (
    QueryStat,
    clear_global_stats,
    global_stats,
    set_sql_debug,
    sql_debugging,
)
from nextorm.entity import Entity, LocalDescriptor
from nextorm.exceptions import (
    CommitException,
    ConstraintError,
    MappingError,
    MultipleObjectsFoundError,
    ObjectNotFound,
    OptimisticCheckError,
    PartialCommitException,
    TransactionError,
)  # noqa: F401
from nextorm.expr import ColumnExpr
from nextorm.fields import (
    PK,
    ULID,
    CompositeConstraint,
    DateTimeTz,
    FieldSpec,
    Json,
    Local,
    LocalSpec,
    LongStr,
    Opt,
    PrimaryKey,
    RelationSpec,
    Req,
    Set,
    Single,
    Vec,
    composite_index,
    composite_key,
    ulid,
    uuid4,
    uuid7,
)
from nextorm.generators import DecompileError, avg, count, max, min, select, sum
from nextorm.migrations import MigrationRunner, makemigrations, migrate
from nextorm.pool import AsyncConnectionPool, ConnectionPool, PoolTimeoutError
from nextorm.providers import (
    AsyncConnection,
    AsyncCursor,
    AsyncProvider,
    SyncConnection,
    SyncCursor,
    SyncProvider,
    get_async_provider,
    get_sync_provider,
    register_provider,
    registered_providers,
)
from nextorm.query import EntityProxy, QuerySet
from nextorm.session import SessionCache, async_db_session, db_session
from nextorm.sql import (
    Alias,
    BinOp,
    Case,
    CaseWhen,
    Cast,
    ColumnRef,
    Delete,
    FormatBuilder,
    FunctionCall,
    Insert,
    Literal,
    OrderItem,
    Param,
    RawSql,
    Select,
    SQLiteBuilder,
    Star,
    UnaryOp,
    Update,
    render,
    sql,
)

__all__ = [
    "Entity",
    "FieldSpec",
    "RelationSpec",
    "LocalSpec",
    "Local",
    "Set",
    "PK",
    "Req",
    "Opt",
    "Single",
    "CompositeConstraint",
    "composite_key",
    "composite_index",
    "PrimaryKey",
    "LocalDescriptor",
    # UUID / ULID
    "ULID",
    "uuid7",
    "uuid4",
    "ulid",
    # Extended types
    "LongStr",
    "Json",
    "DateTimeTz",
    "Vec",
    "Database",
    "AsyncDatabase",
    "AsyncQuerySet",
    "SessionCache",
    "db_session",
    "async_db_session",
    # Generators
    "select",
    "DecompileError",
    "count",
    "avg",
    "sum",
    "min",
    "max",
    # Transaction helpers
    "commit",
    "rollback",
    "flush",
    # Migrations
    "MigrationRunner",
    "makemigrations",
    "migrate",
    # SQL nodes & builder
    "Param",
    "Literal",
    "ColumnRef",
    "Star",
    "BinOp",
    "UnaryOp",
    "Cast",
    "FunctionCall",
    "Case",
    "CaseWhen",
    "Alias",
    "OrderItem",
    "Select",
    "Insert",
    "Update",
    "Delete",
    "RawSql",
    "sql",
    "SQLiteBuilder",
    "FormatBuilder",
    "render",
    # Providers
    "SyncProvider",
    "SyncConnection",
    "SyncCursor",
    "AsyncProvider",
    "AsyncConnection",
    "AsyncCursor",
    "register_provider",
    "get_sync_provider",
    "get_async_provider",
    "registered_providers",
    # Query DSL
    "ColumnExpr",
    "EntityProxy",
    "QuerySet",
    # Debug / diagnostics
    "set_sql_debug",
    "sql_debugging",
    "QueryStat",
    "global_stats",
    "clear_global_stats",
    # Exceptions
    "MappingError",
    "ObjectNotFound",
    "MultipleObjectsFoundError",
    "ConstraintError",
    "TransactionError",
    "OptimisticCheckError",
    "CommitException",
    "PartialCommitException",
    # Connection pool
    "ConnectionPool",
    "AsyncConnectionPool",
    "PoolTimeoutError",
]

# ---------------------------------------------------------------------------
# Module-level transaction helpers  (mirrors PonyORM's commit/rollback/flush)
# ---------------------------------------------------------------------------


def commit() -> None:
    """Commit all sync databases that have entities in the current session.

    Mirrors PonyORM's staged-commit strategy:

    1. Flush all databases first.  On any flush error every database is rolled
       back and the exception is re-raised.
    2. Commit the *primary* database (first in the list) first.  On failure
       all secondary databases are rolled back and a
       :exc:`~nextorm.exceptions.CommitException` is raised.
    3. Commit the remaining databases.  On any failure a
       :exc:`~nextorm.exceptions.PartialCommitException` is raised (the
       primary commit is already durable at this point).

    When called outside a session this is a no-op.
    For async databases use ``await db.acommit()`` directly.
    """
    from nextorm.session import _get_session_stack  # noqa: PLC0415

    cache = _get_session_stack().current
    if cache is None:
        return
    seen: set[int] = set()
    dbs: list[Database] = []
    for entity in list(cache._objects.values()) + list(cache._to_save) + list(cache._dirty):
        db = vars(entity).get("_db_")
        if db is not None and isinstance(db, Database) and id(db) not in seen:
            seen.add(id(db))
            dbs.append(db)
    if not dbs:
        return

    # Step 1 — flush all; on error roll back everything.
    try:
        for db in dbs:
            db.flush()
    except Exception:
        for _db in dbs:
            with contextlib.suppress(Exception):
                _db._rollback_transaction()
        raise

    # Step 2 — commit primary first.
    primary, secondaries = dbs[0], dbs[1:]
    rollback_exceptions: list[Exception] = []
    try:
        primary._commit_transaction()
    except Exception as primary_exc:
        for _db in secondaries:
            try:
                _db._rollback_transaction()
            except Exception as re:
                rollback_exceptions.append(re)
        raise CommitException(str(primary_exc), [primary_exc, *rollback_exceptions]) from primary_exc

    # Step 3 — commit secondaries; primary's work is already durable.
    secondary_exceptions: list[Exception] = []
    for db in secondaries:
        try:
            db._commit_transaction()
        except Exception as e:
            secondary_exceptions.append(e)
    if secondary_exceptions:
        raise PartialCommitException(
            str(secondary_exceptions[0]), secondary_exceptions
        ) from secondary_exceptions[0]


def rollback() -> None:
    """Roll back all databases that have entities in the current session.

    Iterates the identity map of the active :func:`~nextorm.session.db_session`,
    collects the unique :class:`Database` objects referenced by cached entities,
    calls :meth:`~nextorm.database.Database.rollback` on each, and then clears
    the session cache.

    When called outside a session this is a no-op.
    """
    from nextorm.session import _get_session_stack  # noqa: PLC0415

    cache = _get_session_stack().current
    if cache is None:
        return
    dbs: set[Database] = set()
    for entity in list(cache._objects.values()) + list(cache._to_save) + list(cache._dirty):
        db = vars(entity).get("_db_")
        if db is not None and isinstance(db, Database):
            dbs.add(db)
    for db in dbs:
        db.rollback()


def flush() -> None:
    """Flush all pending changes to all databases in the current session.

    Iterates the dirty set and new-objects queue of the active
    :func:`~nextorm.session.db_session`, groups them by :class:`Database`,
    and calls :meth:`~nextorm.database.Database.flush` on each.

    When called outside a session this is a no-op.
    """
    from nextorm.session import _get_session_stack  # noqa: PLC0415

    cache = _get_session_stack().current
    if cache is None:
        return
    # Collect both new and dirty objects to find the involved databases.
    pending = list(cache.objects_to_save) + list(cache.dirty_objects)
    dbs: set[Database] = set()
    for entity in pending:
        db = vars(entity).get("_db_")
        if db is not None and isinstance(db, Database):
            dbs.add(db)
    for db in dbs:
        db.flush()
